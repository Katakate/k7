import hashlib
import json
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .. import __version__
from ..core.core import K7Core
from ..core.models import SandboxConfig

app = FastAPI(title="K7 Sandbox API", version=__version__)

API_KEYS_FILE = Path(os.getenv("K7_API_KEYS_FILE", "/etc/k7/api_keys.json"))


def load_api_keys() -> dict:
    """Load API keys from file.

    A missing file means "no keys yet" — that is a normal state. An
    *unreadable* file is a deployment bug (e.g. the store is not owned by
    the API uid) and must fail loudly: silently returning {} would reject
    every valid key with a misleading "Invalid API key".
    """
    if not API_KEYS_FILE.exists():
        return {}
    try:
        with open(API_KEYS_FILE) as f:
            data = json.load(f)
    except (PermissionError, OSError) as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"API key store {API_KEYS_FILE} is unreadable by the API process ({e}). "
                "It must be owned by the k7-api container uid — regenerate a key with "
                "`k7 generate-api-key` (which fixes ownership) on the node hosting the pod."
            ),
        )
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"API key store {API_KEYS_FILE} is corrupt: {e}")
    # Purge expired keys opportunistically
    now_ts = int(time.time())
    changed = False
    for h, v in list(data.items()):
        exp = v.get("expires")
        if isinstance(exp, int) and now_ts > exp:
            del data[h]
            changed = True
    if changed:
        save_api_keys(data)
    return data


def save_api_keys(keys: dict):
    """Save API keys to file with proper permissions."""
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)
    try:
        os.chmod(API_KEYS_FILE, 0o600)
    except OSError:
        pass


async def verify_api_key(
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
):
    """Verify API key via X-API-Key or Authorization: Bearer header.

    Uses timing-attack-resistant comparison and updates last_used on success.
    """
    token: str | None = None
    if x_api_key and x_api_key.strip():
        token = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")

    api_keys = load_api_keys()
    key_hash = hashlib.sha256(token.encode()).hexdigest()

    valid_hash = None
    valid_data = None
    for stored_hash, key_data in api_keys.items():
        if secrets.compare_digest(key_hash, stored_hash):
            valid_hash = stored_hash
            valid_data = key_data
            break

    if valid_data is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Enforce expiry if present
    now_ts = int(time.time())
    expires_ts = valid_data.get("expires")
    if isinstance(expires_ts, int) and now_ts > expires_ts:
        raise HTTPException(status_code=401, detail="API key expired")

    api_keys[valid_hash]["last_used"] = now_ts
    save_api_keys(api_keys)

    return valid_data


def success_response(
    data: Any, status_code: int = status.HTTP_200_OK, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(content={"data": data}, status_code=status_code, headers=headers)


def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(content={"error": {"code": code, "message": message}}, status_code=status_code)


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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # type: ignore[override]
    return error_response("InternalServerError", str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "K7 Sandbox API", "version": __version__}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/api/v1/sandboxes", dependencies=[Depends(verify_api_key)])
async def create_sandbox(config: dict):
    """Create a new sandbox."""
    try:
        sandbox_config = SandboxConfig.from_dict(config)
        core = K7Core()
        result = await core.create_sandbox(sandbox_config)

        if result.success:
            resource = {
                "name": sandbox_config.name,
                "namespace": sandbox_config.namespace,
                "image": sandbox_config.image,
            }
            location = f"/api/v1/sandboxes/{sandbox_config.name}?namespace={sandbox_config.namespace}"
            return success_response(resource, status_code=status.HTTP_201_CREATED, headers={"Location": location})
        else:
            raise HTTPException(status_code=400, detail=result.error)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/sandboxes", dependencies=[Depends(verify_api_key)])
async def list_sandboxes(namespace: str | None = None):
    """List all sandboxes."""
    core = K7Core()
    sandboxes = await core.list_sandboxes(namespace)
    return success_response([sandbox.to_dict() for sandbox in sandboxes])


@app.get("/api/v1/sandboxes/{name}", dependencies=[Depends(verify_api_key)])
async def get_sandbox(name: str, namespace: str = "default"):
    """Get a single sandbox by name."""
    core = K7Core()
    items = await core.list_sandboxes(namespace)
    for s in items:
        if s.name == name:
            return success_response(s.to_dict())
    raise HTTPException(status_code=404, detail=f"Sandbox {name} not found in namespace {namespace}")


@app.delete("/api/v1/sandboxes/{name}", dependencies=[Depends(verify_api_key)])
async def delete_sandbox(name: str, namespace: str = "default"):
    """Delete a sandbox."""
    core = K7Core()
    result = await core.delete_sandbox(name, namespace)

    if result.success:
        return success_response({"message": result.message})
    else:
        raise HTTPException(status_code=400, detail=result.error)


@app.delete("/api/v1/sandboxes", dependencies=[Depends(verify_api_key)])
async def delete_all_sandboxes(namespace: str = "default"):
    """Delete all sandboxes in a namespace."""
    core = K7Core()
    result = await core.delete_all_sandboxes(namespace)

    if result.success:
        return success_response({"message": result.message, "results": result.data})
    else:
        raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/sandboxes/{name}/pause", dependencies=[Depends(verify_api_key)])
async def pause_sandbox(name: str, body: dict | None = None):
    """Pause a sandbox (scale to 0) and optionally take a Longhorn VolumeSnapshot.

    Body keys (all optional):
      ``namespace`` (default ``"default"``),
      ``snapshot`` (when set, snapshot the sandbox's root PVC under this name).
    """
    body = body or {}
    namespace = body.get("namespace", "default")
    core = K7Core()
    result = await core.pause_sandbox(
        name=name,
        namespace=namespace,
        snapshot_name=body.get("snapshot"),
    )
    if result.success:
        return success_response({"message": result.message})
    raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/sandboxes/{name}/resume", dependencies=[Depends(verify_api_key)])
async def resume_sandbox(name: str, body: dict | None = None):
    """Resume a paused sandbox (scale back to 1)."""
    body = body or {}
    namespace = body.get("namespace", "default")
    core = K7Core()
    result = await core.resume_sandbox(name=name, namespace=namespace)
    if result.success:
        return success_response({"message": result.message})
    raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/sandboxes/{name}/fork", dependencies=[Depends(verify_api_key)])
async def fork_sandbox(name: str, body: dict):
    """Fork a kata-qemu-longhorn sandbox into a new name with a cloned root disk.

    Required body key: new_name. Optional: namespace, snapshot.
    The handler blocks until the cloned PVC is bound (matches CLI behaviour).
    """
    new_name = (body or {}).get("new_name")
    if not new_name or not isinstance(new_name, str):
        raise HTTPException(status_code=400, detail="new_name is required")
    namespace = body.get("namespace", "default")
    snapshot = body.get("snapshot")
    core = K7Core()
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


@app.get("/api/v1/sandboxes/{name}/logs", dependencies=[Depends(verify_api_key)])
async def get_sandbox_logs(
    name: str,
    namespace: str = "default",
    container: str = "sandbox",
    tail: int = 200,
    since: int = 0,
):
    """Read pod logs (snapshot; no streaming yet — see Spec 10g risks)."""
    core = K7Core()
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


@app.post("/api/v1/sandboxes/{name}/exec", dependencies=[Depends(verify_api_key)])
async def exec_command(name: str, command_data: dict, namespace: str = "default"):
    """Execute a command in a sandbox."""
    command = command_data.get("command", "")
    if not command:
        raise HTTPException(status_code=400, detail="Command is required")

    core = K7Core()
    result = await core.exec_command(name, command, namespace)
    return success_response(result.to_dict())


@app.post("/api/v1/install", dependencies=[Depends(verify_api_key)])
async def install_node(install_data: dict):
    """Install K7 on target hosts."""
    core = K7Core()
    result = core.install_node(
        install_data.get("playbook"),
        install_data.get("inventory"),
        install_data.get("verbose", False),
    )

    if result.success:
        return success_response({"message": result.message})
    else:
        raise HTTPException(status_code=400, detail=result.error)


@app.get("/api/v1/nodes/storage", dependencies=[Depends(verify_api_key)])
async def get_nodes_storage():
    """Per-node storage-pool utilization (kfd thin-pool + k7d disks pool),
    aggregated from the k7-agent DaemonSet (spec 18g). A node whose agent
    is unreachable gets an ``{"error": ...}`` entry — never omitted."""
    core = K7Core()
    return success_response(await core.nodes_storage())


@app.get("/api/v1/sandboxes/metrics", dependencies=[Depends(verify_api_key)])
async def get_sandbox_metrics(namespace: str | None = None):
    """Get resource usage metrics for sandboxes."""
    core = K7Core()
    metrics = await core.get_sandbox_metrics(namespace)
    return success_response(metrics)


# ---------------------------------------------------------------------------
# Spec 10e: VolumeSnapshot lifecycle endpoints.
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


@app.get("/api/v1/snapshots", dependencies=[Depends(verify_api_key)])
async def list_snapshots(
    namespace: str | None = "default",
    all_namespaces: bool = False,
    sandbox: str | None = None,
    kind: str | None = None,
):
    """List VolumeSnapshots, optionally filtered by namespace / sandbox / kind."""
    core = K7Core()
    snaps = await core.list_snapshots(
        namespace=namespace,
        all_namespaces=all_namespaces,
        sandbox=sandbox,
        kind=kind,
    )
    return success_response([s.to_dict() for s in snaps])


@app.get("/api/v1/snapshots/{name}", dependencies=[Depends(verify_api_key)])
async def get_snapshot(name: str, namespace: str = "default"):
    """Inspect a single VolumeSnapshot by name."""
    core = K7Core()
    snap = await core.get_snapshot(name, namespace=namespace)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"Snapshot {name} not found in namespace {namespace}")
    return success_response(snap.to_dict())


@app.post("/api/v1/sandboxes/{name}/snapshot", dependencies=[Depends(verify_api_key)])
async def create_snapshot(name: str, body: dict):
    """Snapshot a running sandbox's root PVC without pausing it (kind=named).

    Body keys: ``snapshot_name`` (required), ``namespace`` (default ``"default"``).
    """
    snapshot_name = (body or {}).get("snapshot_name")
    if not snapshot_name or not isinstance(snapshot_name, str):
        raise HTTPException(status_code=400, detail="snapshot_name is required")
    namespace = body.get("namespace", "default")
    core = K7Core()
    result = await core.create_snapshot(sandbox_name=name, snapshot_name=snapshot_name, namespace=namespace)
    if result.success:
        resource = {"name": snapshot_name, "namespace": namespace, "source_sandbox": name}
        location = f"/api/v1/snapshots/{snapshot_name}?namespace={namespace}"
        return success_response(resource, status_code=status.HTTP_201_CREATED, headers={"Location": location})
    err = (result.error or "").lower()
    if "already exists" in err:
        raise HTTPException(status_code=409, detail=result.error)
    raise HTTPException(status_code=400, detail=result.error)


@app.delete("/api/v1/snapshots/{name}", dependencies=[Depends(verify_api_key)])
async def delete_snapshot(name: str, namespace: str = "default"):
    """Delete a VolumeSnapshot by name."""
    core = K7Core()
    result = await core.delete_snapshot(name, namespace=namespace)
    if result.success:
        return success_response({"message": result.message})
    if "not found" in (result.error or "").lower():
        raise HTTPException(status_code=404, detail=result.error)
    raise HTTPException(status_code=400, detail=result.error)


@app.post("/api/v1/snapshots/{name}/restore", dependencies=[Depends(verify_api_key)])
async def restore_snapshot(name: str, body: dict):
    """Boot a brand-new sandbox from a standalone VolumeSnapshot (Spec 10f).

    Body keys:
      ``new_sandbox_name`` (required),
      ``namespace`` (default ``"default"``),
      ``overrides`` (optional dict: image, backend, root_disk_size, sidecar,
      limits, entrypoint, cmd, before_script),
      ``keep_snapshot`` (default ``true``).
    """
    body = body or {}
    new_name = body.get("new_sandbox_name")
    if not new_name or not isinstance(new_name, str):
        raise HTTPException(status_code=400, detail="new_sandbox_name is required")
    namespace = body.get("namespace", "default")
    keep_snapshot = bool(body.get("keep_snapshot", True))

    overrides_dict = body.get("overrides") or {}
    if not isinstance(overrides_dict, dict):
        raise HTTPException(status_code=400, detail="overrides must be a JSON object")
    # Filter to known SandboxConfigOverrides keys; ignore garbage.
    allowed = {"image", "backend", "root_disk_size", "sidecar", "limits", "entrypoint", "cmd", "before_script"}
    filtered = {k: v for k, v in overrides_dict.items() if k in allowed}
    from k7.core.models import SandboxConfigOverrides

    overrides = SandboxConfigOverrides(**filtered) if filtered else None

    core = K7Core()
    result = await core.restore_sandbox(
        snapshot_name=name,
        new_sandbox_name=new_name,
        namespace=namespace,
        overrides=overrides,
        keep_snapshot=keep_snapshot,
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


@app.post("/api/v1/snapshots/gc", dependencies=[Depends(verify_api_key)])
async def gc_snapshots(body: dict | None = None):
    """Sweep stale ``kind=fork`` snapshots older than ``keep_fork_for``.

    Body (all optional):
      ``namespace`` (default ``"default"``),
      ``all_namespaces`` (default ``false``),
      ``keep_fork_for`` (default ``"10m"``, also accepts ``2h`` / ``45s`` / plain seconds),
      ``dry_run`` (default ``false``).
    """
    body = body or {}
    keep_for = _parse_keep_fork_for(body.get("keep_fork_for"))
    core = K7Core()
    result = await core.gc_snapshots(
        namespace=body.get("namespace", "default"),
        all_namespaces=bool(body.get("all_namespaces", False)),
        keep_fork_for=keep_for,
        dry_run=bool(body.get("dry_run", False)),
    )
    if result.success:
        return success_response({"message": result.message, "results": result.data})
    raise HTTPException(status_code=400, detail=result.error)
