import asyncio
import hashlib
import json
import logging
import os
import secrets
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from kubernetes_asyncio.client.exceptions import ApiException

from .. import __version__, metrics
from .. import logging as k7_logging
from ..core import cluster_config, tenancy
from ..core.core import K7Core, aclose_k7_core
from ..core.models import OperationResult, SandboxConfig
from ..core.tenancy import list_tenants as list_tenant_crs
from ..deploy.k7_api_keys import (
    KEYS_SECRET,
    TENANT_ROLES,
    USAGE_SECRET,
    decode_keys,
    match_token,
    new_entry,
    token_key_id,
    unscoped_keys,
    validate_scope,
)
from ..metering import config as metering_config
from ..metering import showback, storage
from . import gauges, remote_install
from .authz import (
    ADMIN,
    OPERATOR,
    PERMISSIONS,
    Principal,
    apply_node_scope,
    authorize_cluster,
    authorize_namespace,
    highest,
)
from .keystore import FileKeyStore, KeyStore, SecretKeyStore
from .oidc import OIDCConfig, OIDCVerifier, principal_from_claims, read_bindings, validate_bindings, write_bindings
from .tenants import list_tenants

k7_logging.configure_from_env()
log = logging.getLogger("k7.api")
API_KEYS_FILE = Path(os.getenv("K7_API_KEYS_FILE", "/etc/k7/api_keys.json"))
STORE: KeyStore | None = None
OIDC: OIDCVerifier | None = None
STATUS_SECONDS = 60
STATUS_LEASE_TTL = 90
STATUS_ERROR: str | None = None
METERING: metering_config.Config | None = None
STORAGE: storage.StorageSampler | None = None
log = logging.getLogger("k7.api")


def build_key_store(kind: str | None) -> KeyStore:
    if kind == "secret":
        return SecretKeyStore(cluster_config.system_namespace())
    if kind == "file":
        return FileKeyStore(API_KEYS_FILE)
    raise RuntimeError(f"K7_API_KEY_STORE must be 'secret' or 'file' (deprecated), got {kind!r}")


async def production_preflight(store: KeyStore) -> None:
    """Production mode refuses to start rather than run with any of these."""
    if os.environ.get("K7_API_ENABLE_REMOTE_INSTALL") == "1":
        raise RuntimeError("production mode refuses K7_API_ENABLE_REMOTE_INSTALL=1; run `k7 install` from a node")
    transport = os.environ.get("K7_API_TRANSPORT")
    if transport != "https":
        raise RuntimeError(
            f"production mode refuses K7_API_TRANSPORT={transport!r}; reinstall without --api-insecure-http"
        )
    if isinstance(store, SecretKeyStore):
        err = await store._step(store.refresh, "sync")
        if err:
            raise RuntimeError(f"production mode could not read the API key store at startup: {err}")
    bad = unscoped_keys(store.snapshot())
    if bad:
        listed = ", ".join(f"{k} ({e.get('name')})" for k, e in sorted(bad.items()))
        raise RuntimeError(
            f"production mode refuses to start: API key(s) {listed} have no tenant and no cluster role "
            "(unscoped, from before roles or migrated unscoped_legacy). Fix: `k7 api keys migrate`, "
            "`k7 tenant adopt --from-keys --apply`, or `k7 revoke-api-key NAME` and reissue with "
            "`k7 generate-api-key NAME --role user --tenant T`"
        )


async def tenant_status_loop() -> None:
    """Leader replica (Lease k7-tenant-status) writes Tenant.status.usage and the ResourceQuota backstop."""
    global STATUS_ERROR
    holder = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    while True:
        core = K7Core()
        try:
            if await tenancy.try_lead(core, tenancy.STATUS_LEASE, holder, STATUS_LEASE_TTL):
                errors = []
                for tenant in await list_tenant_crs(core):
                    try:
                        await tenancy.reconcile_tenant(core, tenant)
                    except Exception as e:
                        errors.append(f"{tenancy.name_of(tenant)}: {e}")
                STATUS_ERROR = "; ".join(errors) or None
        except Exception as e:
            STATUS_ERROR = str(e)
        finally:
            await aclose_k7_core(core)
        if STATUS_ERROR:
            log.error("tenant status reconcile failed: %s", STATUS_ERROR)
        await asyncio.sleep(STATUS_SECONDS)


async def reconcile_sandbox_egress() -> None:
    """Rewrite FQDN egress policies whose DNS rule predates the allowlist-only DNS fix (idempotent)."""
    core = K7Core()
    try:
        changed = await core.reconcile_cilium_egress_policies()
    finally:
        await aclose_k7_core(core)
    for ref in changed:
        log.warning("egress policy %s: DNS rule now allows only the sandbox's allowlisted names", ref)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global STORE, OIDC, METERING, STORAGE
    mode = cluster_config.api_mode()
    METERING = metering_config.load()
    STORAGE = storage.from_env(METERING.storage_interval)
    STORE = build_key_store(os.environ.get("K7_API_KEY_STORE"))
    cfg = OIDCConfig.from_env()
    OIDC = OIDCVerifier(cfg) if cfg else None
    if mode == "production":
        await production_preflight(STORE)
    await reconcile_sandbox_egress()
    tls = gauges.tls_config()
    server = metrics.serve(metrics.API)
    tasks = [asyncio.create_task(gauges.run(tls)), asyncio.create_task(STORAGE.run())]
    if isinstance(STORE, SecretKeyStore):
        tasks += [asyncio.create_task(STORE.run()), asyncio.create_task(tenant_status_loop())]
    metrics.API_INFO.labels(__version__, mode).set(1)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        if server:
            server.shutdown()
            server.server_close()


# One JSON line per mutating or auth-failed request, on stdout. Never the
# token, request bodies or exec command text (sha256 + length only).
audit_log = logging.getLogger("k7.audit")
if not audit_log.handlers:
    _audit_handler = k7_logging.ConsoleHandler("stdout")
    _audit_handler.setFormatter(k7_logging.JsonFormatter())
    audit_log.addHandler(_audit_handler)
    audit_log.setLevel(logging.INFO)
    audit_log.propagate = False

_AUDIT: ContextVar[dict | None] = ContextVar("k7_audit", default=None)
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def audit_note(**fields: Any) -> None:
    """Attach fields to the current request's audit line (no-op outside a request)."""
    ctx = _AUDIT.get()
    if ctx is not None:
        ctx.update(fields)
    k7_logging.bind(**fields)


def _client_ip(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    # Only the Caddy sidecar (localhost) is trusted; it appends the real peer last.
    if xff and peer in ("127.0.0.1", "::1"):
        return xff.split(",")[-1].strip()
    return peer


def _emit_audit(request: Request, ctx: dict, request_id: str, status_code: int, start: float) -> None:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    path_params = request.scope.get("path_params") or {}
    line = {
        "request_id": request_id,
        "principal_type": ctx.get("principal_type"),
        "subject": ctx.get("subject"),
        "role": ctx.get("role"),
        "tenant": ctx.get("tenant"),
        "idp_issuer": ctx.get("idp_issuer"),
        "key_id": ctx.get("key_id"),
        "key_name": ctx.get("key_name"),
        "method": request.method,
        "route": template,
        "namespace": ctx.get("namespace"),
        "sandbox": ctx.get("sandbox")
        or (path_params.get("name") if template and "/sandboxes/{name}" in template else None),
        "status": status_code,
        "latency_ms": round((time.monotonic() - start) * 1000, 1),
        "client_ip": _client_ip(request),
    }
    line.update(
        {k: v for k, v in ctx.items() if k.startswith("command_") or k in ("snapshot", "quota", "nodes", "operation")}
    )
    audit_log.info("audit %s %s %d", request.method, template, status_code, extra=line)


_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", *_AUDIT_METHODS}


def _observe(request: Request, status_code: int, start: float) -> None:
    elapsed = time.monotonic() - start
    route = getattr(request.scope.get("route"), "path", None) or "<unmatched>"
    method = request.method if request.method in _HTTP_METHODS else "OTHER"
    metrics.HTTP_REQUESTS.labels(route, method, str(status_code)).inc()
    metrics.HTTP_DURATION.labels(route, method).observe(elapsed)
    ctx = k7_logging.context()
    if status_code == 403 and "quota" not in (ctx or {}):
        metrics.AUTH_FAILURES.labels("forbidden").inc()
    if ctx is not None and "/sandboxes/{name}" in route:
        ctx.setdefault("sandbox", (request.scope.get("path_params") or {}).get("name"))
    k7_logging.access(log, method, route, status_code, elapsed, client_ip=_client_ip(request))


def key_store() -> KeyStore:
    if STORE is None:
        raise HTTPException(status_code=503, detail="API key store is not initialised")
    return STORE


def key_principal(token: str) -> Principal:
    """Match a key token against the store's in-memory snapshot (constant-time
    hash compare); expired keys are rejected, never purged here."""
    store = key_store()
    audit_note(principal_type="key", key_id=token_key_id(token))
    match = match_token(store.snapshot(), token)
    if match is None:
        metrics.AUTH_FAILURES.labels("invalid").inc()
        raise HTTPException(status_code=401, detail="Invalid API key")
    key_id, entry = match
    audit_note(key_id=key_id, key_name=entry.get("name"), subject=key_id)
    expires_ts = entry.get("expires")
    if isinstance(expires_ts, int) and int(time.time()) > expires_ts:
        metrics.AUTH_FAILURES.labels("expired").inc()
        raise HTTPException(status_code=401, detail="API key expired")
    store.touch(key_id)
    label = f"API key {key_id} ({entry.get('name')})"
    role, tenant = entry.get("role"), entry.get("tenant")
    if role is None:
        raise HTTPException(403, f"{label} has no role (from before roles); run `k7 api keys migrate` on a server node")
    p = Principal(
        type="key",
        subject=key_id,
        label=label,
        namespaces=list(entry.get("namespaces") or []),
        nodes=list(entry.get("nodes") or []),
    )
    production = cluster_config.api_mode() == "production"
    if production and entry.get("unscoped_legacy"):
        raise HTTPException(
            403,
            f"production mode refuses {label}: an unscoped key from before roles that `k7 api keys migrate` made admin "
            "(unscoped_legacy). Re-scope it: `k7 generate-api-key NAME --role user --tenant T` (or `--role admin` "
            "for a real admin), switch clients to the new key, then `k7 revoke-api-key "
            f"{entry.get('name')}`",
        )
    if role in TENANT_ROLES and not tenant and p.namespaces:
        if production:
            raise HTTPException(
                403,
                f"production mode refuses {label}: role {role} on namespaces {', '.join(p.namespaces)} with no "
                "tenant. Re-scope it: `k7 tenant adopt --from-keys --apply`",
            )
        p.legacy_role = role
        return p
    try:
        validate_scope(role, tenant)
    except ValueError as e:
        raise HTTPException(403, f"{label} is invalid: {e}")
    if tenant:
        p.tenant_roles = {tenant: role}
    else:
        p.cluster_role = role
    return p


async def oidc_principal(token: str) -> Principal:
    audit_note(principal_type="oidc")
    if OIDC is None:
        raise HTTPException(401, "OIDC is not configured on this API (auth.oidc.issuer is unset); use an API key")
    claims = await OIDC.verify(token)
    audit_note(subject=claims["sub"], idp_issuer=OIDC.cfg.issuer)
    core = K7Core()
    try:
        bindings, deny = await read_bindings(core, cluster_config.system_namespace())
    finally:
        await aclose_k7_core(core)
    return principal_from_claims(OIDC.cfg, claims, bindings, deny)


async def require(
    request: Request,
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
) -> Principal | None:
    """Runs on every route: authenticate, then check the route's roles in ``PERMISSIONS``.

    ``X-API-Key`` and ``Bearer k7_…`` take the key path; a JWT-shaped bearer
    token takes the OIDC path.
    """
    name = getattr(request.scope.get("route"), "name", None)
    if name not in PERMISSIONS:
        raise HTTPException(500, f"route {name!r} has no entry in authz.PERMISSIONS")
    allowed = PERMISSIONS[name]
    if allowed is None:
        return None
    bearer = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    token = (x_api_key or "").strip() or bearer
    if not token:
        metrics.AUTH_FAILURES.labels("missing").inc()
        raise HTTPException(status_code=401, detail="Missing API key or bearer token")
    jwt_shaped = token == bearer and not token.startswith("k7_") and token.count(".") == 2
    if jwt_shaped:
        try:
            p = await oidc_principal(token)
        except HTTPException as e:
            if e.status_code == 401:
                metrics.AUTH_FAILURES.labels("oidc_invalid").inc()
            raise
    else:
        p = key_principal(token)
    p.check_route(name, allowed)
    audit_note(role=highest([r for r in (p.cluster_role, p.legacy_role, *p.tenant_roles.values()) if r in allowed]))
    return p


async def principal(p: Principal | None = Depends(require)) -> Principal:
    if p is None:
        raise HTTPException(500, "a public route asked for a principal")
    return p


async def authorize(p: Principal, core: K7Core, namespace: str | None, *, all_namespaces: bool = False) -> None:
    """Role, tenant and narrowing checks, then hand core the namespace's tenant for admission."""
    audit_note(namespace=namespace)
    crs = await list_tenant_crs(core)
    gone = sorted(set(p.tenant_roles) - {tenancy.name_of(t) for t in crs})
    if p.type == "key" and gone:
        raise HTTPException(403, f"{p.label} is bound to tenant {gone[0]}, which does not exist")
    role, _ = await authorize_namespace(p, core, namespace, all_namespaces=all_namespaces)
    tenant = tenancy.tenant_of_namespace(namespace, crs) if namespace else None
    audit_note(role=role, tenant=tenancy.name_of(tenant) if tenant else None)
    core.admission = tenancy.Admission(
        key_id=p.subject if p.type == "key" else None,
        admin=p.cluster_role == ADMIN,
        tenant=tenant,
        mode=cluster_config.api_mode(),
    )


app = FastAPI(title="K7 Sandbox API", version=__version__, lifespan=lifespan, dependencies=[Depends(require)])


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    ctx: dict = {}
    _AUDIT.set(ctx)
    request_id = k7_logging.start_request(request.headers.get(k7_logging.REQUEST_ID_HEADER))
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        _observe(request, 500, start)
        _emit_audit(request, ctx, request_id, 500, start)
        raise
    _observe(request, response.status_code, start)
    if request.method in _AUDIT_METHODS or response.status_code in (401, 403):
        try:
            _emit_audit(request, ctx, request_id, response.status_code, start)
        except Exception as e:
            return error_response("InternalServerError", f"audit log emission failed: {e}", 500)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-K7-Mode"] = cluster_config.api_mode()
    return response


def success_response(
    data: Any, status_code: int = status.HTTP_200_OK, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(content={"data": data}, status_code=status_code, headers=headers)


def error_response(code: str, message: str, status_code: int, details: dict | None = None) -> JSONResponse:
    error: dict = {"code": code, "message": message}
    if details:
        error["details"] = details
    return JSONResponse(content={"error": error}, status_code=status_code)


def sandbox_failure(result: OperationResult, name: str) -> HTTPException:
    """404 when core reports the sandbox itself missing, else 400."""
    missing = (result.error or "").startswith(f"Sandbox {name} not found")
    return HTTPException(status_code=404 if missing else 400, detail=result.error)


async def get_k7_core() -> AsyncIterator[K7Core]:
    """Per-request K7Core; close the kube aiohttp session when the handler ends."""
    core = K7Core()
    try:
        yield core
    finally:
        await aclose_k7_core(core)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):  # type: ignore[override]
    # Map common status codes to generic error codes
    code_map = {
        400: "BadRequest",
        401: "Unauthorized",
        403: "Forbidden",
        404: "NotFound",
        409: "Conflict",
        422: "UnprocessableEntity",
        500: "InternalServerError",
    }
    code = code_map.get(exc.status_code, "Error")
    # FastAPI often sets detail to str or dict; normalize to str
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return error_response(code, detail, exc.status_code)


@app.exception_handler(tenancy.AdmissionError)
async def admission_error_handler(request: Request, exc: tenancy.AdmissionError):  # type: ignore[override]
    if exc.code == "QuotaExceeded":
        audit_note(quota=exc.details)
    return error_response(exc.code, exc.message, exc.status, exc.details)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # type: ignore[override]
    return error_response("InternalServerError", str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "K7 Sandbox API", "version": __version__}


@app.get("/health")
async def health():
    """Liveness/readiness; key-store sync and usage-flush state as detail."""
    return {
        "status": "healthy",
        "mode": cluster_config.api_mode(),
        "key_store": STORE.health() if STORE else None,
        "oidc_issuer": OIDC.cfg.issuer if OIDC else None,
        "tenant_status_error": STATUS_ERROR,
        "metering": STORAGE.health() if STORAGE else None,
    }


@app.post("/api/v1/sandboxes")
async def create_sandbox(config: dict, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Create a new sandbox."""
    try:
        sandbox_config = SandboxConfig.from_dict(config)
        audit_note(sandbox=sandbox_config.name)
        await authorize(p, core, sandbox_config.namespace)
        sandbox_config.node_name = apply_node_scope(p, sandbox_config.node_name)
        result = await core.create_sandbox(sandbox_config)

        if result.success:
            resource = {
                "name": sandbox_config.name,
                "namespace": sandbox_config.namespace,
                "image": sandbox_config.image,
            }
            # NodePort URLs for --expose-port, when there are any.
            if result.data and result.data.get("endpoints"):
                resource["endpoints"] = result.data["endpoints"]
            location = f"/api/v1/sandboxes/{sandbox_config.name}?namespace={sandbox_config.namespace}"
            return success_response(resource, status_code=status.HTTP_201_CREATED, headers={"Location": location})
        else:
            raise HTTPException(status_code=503 if result.transient else 400, detail=result.error)
    except (HTTPException, tenancy.AdmissionError):
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/sandboxes")
async def list_sandboxes(
    namespace: str | None = None,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """List all sandboxes."""
    await authorize(p, core, namespace)
    sandboxes = await core.list_sandboxes(namespace)
    return success_response([sandbox.to_dict() for sandbox in sandboxes])


@app.get("/api/v1/sandboxes/{name}")
async def get_sandbox(
    name: str,
    namespace: str = "default",
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Get a single sandbox by name."""
    await authorize(p, core, namespace)
    items = await core.list_sandboxes(namespace)
    for s in items:
        if s.name == name:
            return success_response(s.to_dict())
    raise HTTPException(status_code=404, detail=f"Sandbox {name} not found in namespace {namespace}")


@app.delete("/api/v1/sandboxes/{name}")
async def delete_sandbox(
    name: str,
    namespace: str = "default",
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Delete a sandbox."""
    await authorize(p, core, namespace)
    result = await core.delete_sandbox(name, namespace)

    if result.success:
        return success_response({"message": result.message})
    else:
        raise HTTPException(status_code=400, detail=result.error)


@app.delete("/api/v1/sandboxes")
async def delete_all_sandboxes(
    namespace: str = "default",
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Delete all sandboxes in a namespace."""
    await authorize(p, core, namespace)
    result = await core.delete_all_sandboxes(namespace)

    if result.success:
        return success_response({"message": result.message, "results": result.data})
    else:
        raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/sandboxes/{name}/pause")
async def pause_sandbox(
    name: str,
    body: dict | None = None,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Pause a sandbox (scale to 0) and optionally take a Longhorn VolumeSnapshot.

    Body keys (all optional):
      ``namespace`` (default ``"default"``),
      ``snapshot`` (when set, snapshot the sandbox's root PVC under this name).
    """
    body = body or {}
    namespace = body.get("namespace", "default")
    await authorize(p, core, namespace)
    result = await core.pause_sandbox(
        name=name,
        namespace=namespace,
        snapshot_name=body.get("snapshot"),
    )
    if result.success:
        return success_response({"message": result.message})
    raise sandbox_failure(result, name)


@app.post("/api/v1/sandboxes/{name}/resume")
async def resume_sandbox(
    name: str,
    body: dict | None = None,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Resume a paused sandbox (scale back to 1)."""
    body = body or {}
    namespace = body.get("namespace", "default")
    await authorize(p, core, namespace)
    result = await core.resume_sandbox(name=name, namespace=namespace)
    if result.success:
        return success_response({"message": result.message})
    raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/sandboxes/{name}/fork")
async def fork_sandbox(
    name: str,
    body: dict,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Fork a sandbox. kql clones the root disk; k7d/k7d-fc warm-fork the VM on the source node.

    Required body key: new_name. Optional: namespace, snapshot (kql only).
    kql blocks until the cloned PVC is bound (matches CLI behaviour).
    """
    new_name = (body or {}).get("new_name")
    if not new_name or not isinstance(new_name, str):
        raise HTTPException(status_code=400, detail="new_name is required")
    namespace = body.get("namespace", "default")
    await authorize(p, core, namespace)
    snapshot = body.get("snapshot")
    if p.nodes:
        items = await core.list_sandboxes(namespace)
        source = next((s for s in items if s.name == name), None)
        if source is None:
            raise HTTPException(status_code=404, detail=f"Sandbox {name} not found in namespace {namespace}")
        if not source.node:
            raise HTTPException(
                status_code=409,
                detail=f"Sandbox {name} is not scheduled to a node yet",
            )
        apply_node_scope(p, source.node)
    result = await core.fork_sandbox(
        source_name=name,
        new_name=new_name,
        namespace=namespace,
        snapshot_name=snapshot,
    )
    if result.success:
        resource = {
            "name": new_name,
            "namespace": namespace,
            "source": name,
            "message": result.message,
        }
        location = f"/api/v1/sandboxes/{new_name}?namespace={namespace}"
        return success_response(resource, status_code=status.HTTP_201_CREATED, headers={"Location": location})
    err = (result.error or "").lower()
    if "already exists" in err:
        raise HTTPException(status_code=409, detail=result.error)
    if "not found" in err:
        raise HTTPException(status_code=404, detail=result.error)
    raise HTTPException(status_code=400, detail=result.error)


@app.get("/api/v1/sandboxes/{name}/logs")
async def get_sandbox_logs(
    name: str,
    namespace: str = "default",
    container: str = "sandbox",
    tail: int = 200,
    since: int = 0,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Read pod logs (snapshot; no streaming yet)."""
    await authorize(p, core, namespace)
    result = await core.get_logs(
        sandbox_name=name,
        namespace=namespace,
        container=container,
        tail_lines=tail if tail > 0 else None,
        since_seconds=since if since > 0 else None,
    )
    if result.success:
        return success_response(result.data or {"logs": ""})
    err = (result.error or "").lower()
    if "no pod found" in err or "not found" in err:
        raise HTTPException(status_code=404, detail=result.error)
    raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/sandboxes/{name}/exec")
async def exec_command(
    name: str,
    command_data: dict,
    namespace: str = "default",
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Execute a command in a sandbox."""
    await authorize(p, core, namespace)
    command = command_data.get("command", "")
    raw = command if isinstance(command, str) else json.dumps(command)
    audit_note(command_sha256=hashlib.sha256(raw.encode()).hexdigest(), command_len=len(raw))
    if not command:
        raise HTTPException(status_code=400, detail="Command is required")

    result = await core.exec_command(name, command, namespace)
    return success_response(result.to_dict())


def remote_install_gate(p: Principal) -> None:
    """404 unless ``K7_API_ENABLE_REMOTE_INSTALL=1`` (deploy time); then cluster-wide only.

    The admin role itself is ``authz.PERMISSIONS`` (checked before this, on or off)."""
    if not remote_install.enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    authorize_cluster(p)


@app.post("/api/v1/install")
async def install_node(request: Request, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Prepare existing cluster nodes with the k7-node-installer; 202 + operation id.

    Body: exactly ``{"nodes": [...]}``. The installer image and config are fixed at deploy time.
    """
    remote_install_gate(p)
    try:
        nodes = remote_install.parse_nodes(await request.json())
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")
    except remote_install.RemoteInstallError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    op = secrets.token_hex(8)
    audit_note(nodes=nodes, operation=op)
    v1, apps = await core._get_core_v1_client(), await core._get_apps_v1_client()
    try:
        started = await remote_install.start(v1, apps, op, nodes, p.subject)
    except remote_install.RemoteInstallError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"operation {op}: Kubernetes API {e.status} {e.reason}")
    return success_response(started, status_code=status.HTTP_202_ACCEPTED)


@app.get("/api/v1/install/{operation}")
async def install_status(operation: str, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Per-node progress of a ``POST /api/v1/install`` operation, read live from the cluster."""
    remote_install_gate(p)
    try:
        return success_response(await remote_install.status(await core._get_core_v1_client(), operation))
    except remote_install.RemoteInstallError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"Kubernetes API {e.status} {e.reason}")


@app.get("/api/v1/nodes/storage")
async def get_nodes_storage(p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Per-node storage-pool utilization (kfd thin-pool + k7d disks pool),
    aggregated from the k7-agent DaemonSet. A node whose agent
    is unreachable gets an ``{"error": ...}`` entry — never omitted.

    Cluster-scoped: a namespace-scoped or node-scoped key is 403. Fail
    loud — do not silently narrow the per-node map.
    """
    authorize_cluster(p)
    return success_response(await core.nodes_storage())


@app.post("/api/v1/nodes/{name}/dedicate")
async def dedicate_node(name: str, body: dict, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Label and taint node ``name`` for ``body.tenant`` and record it in the Tenant (same as `k7 nodes dedicate`)."""
    authorize_cluster(p)
    tenant = (body or {}).get("tenant")
    if not isinstance(tenant, str) or not tenant:
        raise HTTPException(400, "tenant is required")
    audit_note(tenant=tenant)
    await _node_call(tenancy.dedicate(core, name, tenant))
    return success_response({"message": f"node {name} dedicated to tenant {tenant} (label+taint, spec.nodes)"})


@app.post("/api/v1/nodes/{name}/undedicate")
async def undedicate_node(name: str, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    authorize_cluster(p)
    await _node_call(tenancy.undedicate(core, name))
    return success_response({"message": f"node {name} undedicated"})


async def _node_call(op) -> None:
    try:
        await op
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))


# ---------------------------------------------------------------------------
# Identity: OIDC discovery for the CLI, whoami, API keys, role bindings.
# ---------------------------------------------------------------------------


@app.get("/api/v1/auth/oidc")
async def oidc_config():
    """Public: what `k7 login` needs to start a device or PKCE flow."""
    if OIDC is None:
        raise HTTPException(404, "OIDC is not configured on this API (auth.oidc.issuer is unset)")
    cfg = OIDC.cfg
    return success_response({"issuer": cfg.issuer, "client_id": cfg.client_id, "audience": cfg.audience})


@app.get("/api/v1/auth/whoami")
async def whoami(p: Principal = Depends(principal)):
    return success_response(
        {"type": p.type, "subject": p.subject, "grants": p.grants(), "issuer": p.issuer, "groups": p.groups}
    )


def _public_key(key_id: str, entry: dict, last_used: str | None = None) -> dict:
    return {"id": key_id, **{k: v for k, v in entry.items() if k != "hash"}, "last_used": last_used}


async def _stored_keys(core: K7Core) -> dict[str, dict]:
    data = await core.read_secret_data(KEYS_SECRET, cluster_config.system_namespace())
    if data is None:
        raise HTTPException(500, f"Secret {KEYS_SECRET} not found; run `k7 api keys migrate`")
    return decode_keys(data)


@app.post("/api/v1/keys")
async def create_key(body: dict, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Mint a key. Body: ``name``, ``role``, ``tenant`` (tenant roles), ``expires_days``, ``namespaces``, ``nodes``."""
    authorize_cluster(p)
    name, tenant = body.get("name"), body.get("tenant")
    if not isinstance(name, str) or not name:
        raise HTTPException(400, "name is required")
    audit_note(key_name=name)
    expires_days = body.get("expires_days")
    if not isinstance(expires_days, int) or expires_days <= 0:
        raise HTTPException(400, "expires_days must be a positive integer")
    try:
        token, key_id, entry = new_entry(
            name,
            role=body.get("role") or "",
            tenant=tenant,
            expires_days=expires_days,
            namespaces=list(body.get("namespaces") or []),
            nodes=list(body.get("nodes") or []),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if tenant and tenant not in await list_tenants(core):
        raise HTTPException(400, f"tenant {tenant!r} does not exist")
    await core.patch_secret_data(KEYS_SECRET, cluster_config.system_namespace(), {key_id: json.dumps(entry)})
    return success_response({**_public_key(key_id, entry), "token": token}, status_code=201)


@app.get("/api/v1/keys")
async def list_keys(p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    authorize_cluster(p)
    keys = await _stored_keys(core)
    usage = await core.read_secret_data(USAGE_SECRET, cluster_config.system_namespace()) or {}
    return success_response([_public_key(k, e, usage.get(k)) for k, e in keys.items()])


@app.delete("/api/v1/keys/{name}")
async def delete_key(name: str, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Revoke every key named ``name`` (all replicas within one store poll)."""
    authorize_cluster(p)
    audit_note(key_name=name)
    ids = [k for k, e in (await _stored_keys(core)).items() if e.get("name") == name]
    if not ids:
        raise HTTPException(404, f"API key {name!r} not found")
    await core.patch_secret_data(KEYS_SECRET, cluster_config.system_namespace(), dict.fromkeys(ids))
    return success_response({"revoked": ids})


@app.get("/api/v1/role-bindings")
async def get_role_bindings(p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    authorize_cluster(p)
    bindings, deny = await read_bindings(core, cluster_config.system_namespace())
    return success_response({"bindings": bindings, "deny_subjects": deny})


@app.put("/api/v1/role-bindings")
async def put_role_bindings(body: dict, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Replace ConfigMap ``k7-role-bindings``. 400 on an unknown tenant, a
    role/tenant mismatch, a duplicate, or a write that leaves no admin
    binding while no admin key exists (lock-out)."""
    authorize_cluster(p)
    bindings, deny = body.get("bindings"), body.get("deny_subjects", [])
    needs_tenants = isinstance(bindings, list) and any(isinstance(b, dict) and b.get("tenant") for b in bindings)
    try:
        bindings, deny = validate_bindings(bindings, deny, set(await list_tenants(core)) if needs_tenants else set())
    except ValueError as e:
        raise HTTPException(400, str(e))
    now = int(time.time())
    admin_keys = [
        k for k, e in (await _stored_keys(core)).items() if e.get("role") == "admin" and e.get("expires", now + 1) > now
    ]
    if not admin_keys and not any(b["role"] == "admin" for b in bindings):
        raise HTTPException(400, "refusing to remove the last admin binding while no admin API key exists")
    await write_bindings(core, cluster_config.system_namespace(), bindings, deny)
    return success_response({"bindings": bindings, "deny_subjects": deny})


@app.get("/api/v1/sandboxes/metrics")
async def get_sandbox_metrics(
    namespace: str | None = None,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Get resource usage metrics for sandboxes."""
    await authorize(p, core, namespace)
    metrics = await core.get_sandbox_metrics(namespace)
    return success_response(metrics)


# ---------------------------------------------------------------------------
# VolumeSnapshot lifecycle endpoints.
# ---------------------------------------------------------------------------


def _parse_keep_fork_for(value: str | None) -> timedelta:
    """Accept ``10m`` / ``2h`` / ``3600`` (seconds) — fail loudly on garbage."""
    if value is None or value == "":
        return timedelta(minutes=10)
    if value.endswith("m"):
        return timedelta(minutes=int(value[:-1]))
    if value.endswith("h"):
        return timedelta(hours=int(value[:-1]))
    if value.endswith("s"):
        return timedelta(seconds=int(value[:-1]))
    return timedelta(seconds=int(value))


@app.get("/api/v1/snapshots")
async def list_snapshots(
    namespace: str | None = "default",
    all_namespaces: bool = False,
    sandbox: str | None = None,
    kind: str | None = None,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """List VolumeSnapshots, optionally filtered by namespace / sandbox / kind."""
    await authorize(p, core, None if all_namespaces else namespace, all_namespaces=all_namespaces)
    snaps = await core.list_snapshots(
        namespace=namespace,
        all_namespaces=all_namespaces,
        sandbox=sandbox,
        kind=kind,
    )
    return success_response([s.to_dict() for s in snaps])


@app.get("/api/v1/snapshots/{name}")
async def get_snapshot(
    name: str,
    namespace: str = "default",
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Inspect a single VolumeSnapshot by name."""
    await authorize(p, core, namespace)
    snap = await core.get_snapshot(name, namespace=namespace)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"Snapshot {name} not found in namespace {namespace}")
    return success_response(snap.to_dict())


@app.post("/api/v1/sandboxes/{name}/snapshot")
async def create_snapshot(
    name: str,
    body: dict,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Snapshot a running sandbox's root PVC without pausing it (kind=named).

    Body keys: ``snapshot_name`` (required), ``namespace`` (default ``"default"``).
    """
    snapshot_name = (body or {}).get("snapshot_name")
    if not snapshot_name or not isinstance(snapshot_name, str):
        raise HTTPException(status_code=400, detail="snapshot_name is required")
    audit_note(snapshot=snapshot_name)
    namespace = body.get("namespace", "default")
    await authorize(p, core, namespace)
    result = await core.create_snapshot(sandbox_name=name, snapshot_name=snapshot_name, namespace=namespace)
    if result.success:
        resource = {"name": snapshot_name, "namespace": namespace, "source_sandbox": name}
        location = f"/api/v1/snapshots/{snapshot_name}?namespace={namespace}"
        return success_response(resource, status_code=status.HTTP_201_CREATED, headers={"Location": location})
    err = (result.error or "").lower()
    if "already exists" in err:
        raise HTTPException(status_code=409, detail=result.error)
    raise sandbox_failure(result, name)


@app.delete("/api/v1/snapshots/{name}")
async def delete_snapshot(
    name: str,
    namespace: str = "default",
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Delete a VolumeSnapshot by name."""
    audit_note(snapshot=name)
    await authorize(p, core, namespace)
    result = await core.delete_snapshot(name, namespace=namespace)
    if result.success:
        return success_response({"message": result.message})
    if "not found" in (result.error or "").lower():
        raise HTTPException(status_code=404, detail=result.error)
    raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/snapshots/{name}/restore")
async def restore_snapshot(
    name: str,
    body: dict,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Boot a brand-new sandbox from a standalone VolumeSnapshot.

    Body keys:
      ``new_sandbox_name`` (required),
      ``namespace`` (default ``"default"``),
      ``overrides`` (optional dict: image, backend, root_disk_size, sidecar,
      limits, entrypoint, cmd, before_script),
      ``keep_snapshot`` (default ``true``),
      ``node_name`` (optional pin; a node-scoped key stamps or rejects this).
    """
    body = body or {}
    new_name = body.get("new_sandbox_name")
    if not new_name or not isinstance(new_name, str):
        raise HTTPException(status_code=400, detail="new_sandbox_name is required")
    audit_note(snapshot=name, sandbox=new_name)
    namespace = body.get("namespace", "default")
    await authorize(p, core, namespace)
    pinned_node = apply_node_scope(p, body.get("node_name"))
    keep_snapshot = bool(body.get("keep_snapshot", True))

    overrides_dict = body.get("overrides") or {}
    if not isinstance(overrides_dict, dict):
        raise HTTPException(status_code=400, detail="overrides must be a JSON object")
    # Filter to known SandboxConfigOverrides keys; ignore garbage.
    allowed = {"image", "backend", "root_disk_size", "sidecar", "limits", "entrypoint", "cmd", "before_script"}
    filtered = {k: v for k, v in overrides_dict.items() if k in allowed}
    from k7.core.models import SandboxConfigOverrides

    overrides = SandboxConfigOverrides(**filtered) if filtered else None

    result = await core.restore_sandbox(
        snapshot_name=name,
        new_sandbox_name=new_name,
        namespace=namespace,
        overrides=overrides,
        keep_snapshot=keep_snapshot,
        node_name=pinned_node,
    )
    if result.success:
        resource = {
            "name": new_name,
            "namespace": namespace,
            "source_snapshot": name,
            "message": result.message,
        }
        location = f"/api/v1/sandboxes/{new_name}?namespace={namespace}"
        return success_response(resource, status_code=status.HTTP_201_CREATED, headers={"Location": location})

    err = (result.error or "").lower()
    if "not found" in err:
        raise HTTPException(status_code=404, detail=result.error)
    if "already exists" in err:
        raise HTTPException(status_code=409, detail=result.error)
    raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/snapshots/gc")
async def gc_snapshots(
    body: dict | None = None,
    p: Principal = Depends(principal),
    core: K7Core = Depends(get_k7_core),
):
    """Sweep stale ``kind=fork`` snapshots older than ``keep_fork_for``.

    Body (all optional):
      ``namespace`` (default ``"default"``),
      ``all_namespaces`` (default ``false``),
      ``keep_fork_for`` (default ``"10m"``, also accepts ``2h`` / ``45s`` / plain seconds),
      ``dry_run`` (default ``false``).
    """
    body = body or {}
    all_namespaces = bool(body.get("all_namespaces", False))
    namespace = body.get("namespace", "default")
    await authorize(p, core, None if all_namespaces else namespace, all_namespaces=all_namespaces)
    keep_for = _parse_keep_fork_for(body.get("keep_fork_for"))
    result = await core.gc_snapshots(
        namespace=namespace,
        all_namespaces=all_namespaces,
        keep_fork_for=keep_for,
        dry_run=bool(body.get("dry_run", False)),
    )
    if result.success:
        return success_response({"message": result.message, "results": result.data})
    raise HTTPException(status_code=400, detail=result.error)


# ---------------------------------------------------------------------------
# Tenants. Reads for operator/admin, management for admin.
# ---------------------------------------------------------------------------


async def _tenant_call(op, *args):
    try:
        return await op(*args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409 if "already exists" in str(e) else 400, detail=str(e))


@app.get("/api/v1/tenants")
async def list_tenants_route(p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    authorize_cluster(p)
    return success_response(await list_tenant_crs(core))


@app.get("/api/v1/tenants/{name}")
async def get_tenant_route(name: str, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    authorize_cluster(p)
    tenant = await tenancy.get_tenant(core, name)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"tenant {name} not found")
    return success_response(tenant)


@app.post("/api/v1/tenants")
async def create_tenant_route(body: dict, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)):
    """Body: ``{"name": ..., "spec": {namespaces, nodes, defaultSize, quotas, limits}}``."""
    authorize_cluster(p)
    name, spec = body.get("name"), body.get("spec")
    if not isinstance(name, str) or not isinstance(spec, dict):
        raise HTTPException(status_code=400, detail="name (string) and spec (object) are required")
    audit_note(tenant=name)
    created = await _tenant_call(tenancy.create_tenant, core, name, spec)
    return success_response(
        created, status_code=status.HTTP_201_CREATED, headers={"Location": f"/api/v1/tenants/{name}"}
    )


@app.patch("/api/v1/tenants/{name}")
async def update_tenant_route(
    name: str, body: dict, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)
):
    """Body: spec sections to replace (``null`` removes a section)."""
    authorize_cluster(p)
    audit_note(tenant=name)
    return success_response(await _tenant_call(tenancy.update_tenant, core, name, body))


@app.delete("/api/v1/tenants/{name}")
async def delete_tenant_route(
    name: str, cascade: bool = False, p: Principal = Depends(principal), core: K7Core = Depends(get_k7_core)
):
    authorize_cluster(p)
    audit_note(tenant=name)
    return success_response({"deleted": await _tenant_call(tenancy.delete_tenant, core, name, cascade)})


def _instant(raw: str, what: str) -> float:
    if raw.replace(".", "", 1).isdigit():
        return float(raw)
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        dt = None
    if dt is None or dt.tzinfo is None:
        raise HTTPException(400, f"'{what}' must be RFC 3339 with a UTC offset or unix seconds, got {raw!r}")
    return dt.timestamp()


def authorize_usage(p: Principal, tenant: str | None) -> None:
    """A tenant role sees its own tenant; cluster-wide or other tenants need operator/admin."""
    if tenant is not None and tenant in p.tenant_roles:
        return
    if p.cluster_role not in (OPERATOR, ADMIN) or p.namespaces:
        scope = f"tenant {tenant}" if tenant else "all tenants"
        raise HTTPException(
            403,
            f"usage for {scope} needs an operator or admin key, or a role on that tenant; {p.label} has "
            f"{'; '.join(p.grants())}",
        )


@app.get("/api/v1/usage")
async def get_usage(
    from_: str = Query(alias="from"),
    to: str = Query(),
    tenant: str | None = None,
    group_by: str | None = None,
    p: Principal = Depends(principal),
):
    """Showback over ``[from, to]`` from Prometheus; k7 keeps no usage database."""
    authorize_usage(p, tenant)
    audit_note(tenant=tenant)
    if METERING is None:
        raise HTTPException(500, "metering config is not loaded")
    if not METERING.prometheus_url:
        raise HTTPException(
            400, "metering.prometheusUrl is unset: install with --monitoring or --metering-prometheus-url URL"
        )
    start, end = _instant(from_, "from"), _instant(to, "to")
    try:
        groups = showback.parse_group_by(group_by)
        async with httpx.AsyncClient(timeout=30) as http:
            rows = await showback.usage(METERING.prometheus_url, tenant, start, end, groups, http)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except (httpx.HTTPError, RuntimeError) as e:
        return error_response("PrometheusUnavailable", f"usage query failed: {e}", 502)
    return success_response({"from": start, "to": end, "tenant": tenant, "group_by": groups, "rows": rows})
