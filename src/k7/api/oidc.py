"""Stateless OIDC bearer-token validation and group/user → role bindings.

Config comes from ``K7_OIDC_*`` env (ConfigMap ``k7-oidc``, written by
``k7 install --oidc-*`` or chart ``auth.oidc``): all five or none.
Signatures are checked against the issuer's JWKS. The cache is trusted
for ``JWKS_TTL_SECONDS`` (``K7_OIDC_JWKS_TTL_SECONDS``; minimum
``REFRESH_SECONDS``) and is also refreshed on an unknown ``kid``, at most
every ``REFRESH_SECONDS``. A failed refresh is 503, including for the
following ``REFRESH_SECONDS``, and an expired cache is not used. ``alg``
must be RS256 or ES256. ``iss``/``aud``/``exp``/``nbf`` are checked with
``LEEWAY`` seconds. When the username claim is ``email``,
``email_verified`` must be true. k7-api stores no sessions.

Bindings live in ConfigMap ``k7-role-bindings`` (keys ``bindings`` and
``deny-subjects``, YAML lists) and are read on every OIDC request, so a
``deny-subjects`` edit applies on the next request on every replica.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
import jwt
import yaml
from fastapi import HTTPException
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException

from ..core.core import K7Core
from ..deploy.k7_api_keys import CLUSTER_ROLES, validate_scope
from .authz import Principal, highest

ALGORITHMS = {"RS256": "RSA", "ES256": "EC"}  # alg → the JWK kty it must be verified with
LEEWAY = 60
REFRESH_SECONDS = 10
JWKS_TTL_SECONDS = 3600  # pinned default for K7_OIDC_JWKS_TTL_SECONDS; minimum is REFRESH_SECONDS
BINDINGS_CM = "k7-role-bindings"
_MATCHERS = ("group", "user", "subject")
_ENV = {
    "issuer": "K7_OIDC_ISSUER",
    "audience": "K7_OIDC_AUDIENCE",
    "client_id": "K7_OIDC_CLIENT_ID",
    "groups_claim": "K7_OIDC_GROUPS_CLAIM",
    "username_claim": "K7_OIDC_USERNAME_CLAIM",
}


def _jwks_ttl(raw: str | None) -> int:
    """Pinned default when unset; anything else must be an integer >= ``REFRESH_SECONDS``."""
    text = (raw or "").strip()
    if not text:
        return JWKS_TTL_SECONDS
    if not text.isdecimal() or int(text) < REFRESH_SECONDS:
        raise RuntimeError(f"K7_OIDC_JWKS_TTL_SECONDS must be an integer >= {REFRESH_SECONDS} (got {text!r})")
    return int(text)


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    audience: str
    client_id: str
    groups_claim: str
    username_claim: str
    jwks_ttl_seconds: int = JWKS_TTL_SECONDS

    @classmethod
    def from_env(cls) -> OIDCConfig | None:
        vals = {k: os.environ.get(v, "").strip() for k, v in _ENV.items()}
        if not any(vals.values()):
            return None
        missing = [_ENV[k] for k, v in vals.items() if not v]
        if missing:
            raise RuntimeError(f"OIDC is partially configured; also set {', '.join(missing)}")
        if not vals["issuer"].startswith("https://") and os.environ.get("K7_OIDC_ALLOW_HTTP_ISSUER") != "1":
            raise RuntimeError(f"K7_OIDC_ISSUER must be https:// (got {vals['issuer']})")
        return cls(**vals, jwks_ttl_seconds=_jwks_ttl(os.environ.get("K7_OIDC_JWKS_TTL_SECONDS")))


class OIDCVerifier:
    def __init__(self, cfg: OIDCConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=5, transport=transport)
        self._jwks_uri: str | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = float("-inf")
        self._attempted_at = float("-inf")
        self._refresh_error: str | None = None
        self._lock = asyncio.Lock()

    async def _get_json(self, url: str) -> dict:
        r = await self._http.get(url)
        r.raise_for_status()
        return r.json()

    async def _refresh(self) -> None:
        if self._jwks_uri is None:
            doc = await self._get_json(f"{self.cfg.issuer.rstrip('/')}/.well-known/openid-configuration")
            if doc.get("issuer") != self.cfg.issuer:
                raise RuntimeError(f"discovery document names issuer {doc.get('issuer')!r}, not {self.cfg.issuer!r}")
            self._jwks_uri = doc["jwks_uri"]
        jwks = jwt.PyJWKSet.from_dict(await self._get_json(self._jwks_uri))
        self._keys = {k.key_id: k for k in jwks.keys if k.key_id}

    async def _key(self, kid: str) -> jwt.PyJWK:
        if kid in self._keys and time.monotonic() - self._fetched_at < self.cfg.jwks_ttl_seconds:
            return self._keys[kid]
        # Requests that arrive during a refresh wait for its result, not the 503 of a rate-limited retry.
        async with self._lock:
            return await self._key_locked(kid)

    async def _key_locked(self, kid: str) -> jwt.PyJWK:
        now = time.monotonic()
        fresh = now - self._fetched_at < self.cfg.jwks_ttl_seconds
        if kid in self._keys and fresh:
            return self._keys[kid]
        # Unknown kid, or the cache has expired. An expired cache is never used:
        # a failed refresh fails closed (503), and so does the next REFRESH_SECONDS,
        # instead of answering 401 for a key we could not re-check.
        if now - self._attempted_at < REFRESH_SECONDS:
            if self._refresh_error is not None or not fresh:
                detail = self._refresh_error or f"cannot fetch the OIDC signing keys of {self.cfg.issuer}"
                raise HTTPException(503, detail)
            raise HTTPException(401, f"token is signed with unknown key id {kid!r}")
        self._attempted_at = now
        try:
            await self._refresh()
        except Exception as e:
            self._refresh_error = f"cannot fetch the OIDC signing keys of {self.cfg.issuer}: {e}"
            if not fresh:
                self._keys = {}
                self._fetched_at = float("-inf")
            raise HTTPException(503, self._refresh_error)
        self._refresh_error = None
        self._fetched_at = now
        if kid not in self._keys:
            raise HTTPException(401, f"token is signed with unknown key id {kid!r}")
        return self._keys[kid]

    async def verify(self, token: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise HTTPException(401, f"malformed bearer token: {e}")
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in ALGORITHMS:
            raise HTTPException(401, f"token alg {alg!r} is not allowed (only {', '.join(ALGORITHMS)})")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise HTTPException(401, "token header has no kid")
        key = await self._key(kid)
        if key.key_type != ALGORITHMS[alg]:
            raise HTTPException(401, f"token alg {alg} does not match key {kid!r}, which is a {key.key_type} key")
        try:
            return jwt.decode(
                token,
                key.key,
                algorithms=[alg],
                audience=self.cfg.audience,
                issuer=self.cfg.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            raise HTTPException(401, f"invalid OIDC token: {e}")


# ── bindings ────────────────────────────────────────────────────────


def parse_bindings(data: dict[str, str]) -> tuple[list[dict], list[str]]:
    bindings = yaml.safe_load(data.get("bindings") or "[]") or []
    deny = yaml.safe_load(data.get("deny-subjects") or "[]") or []
    return bindings, deny


def validate_bindings(bindings: Any, deny: Any, tenants: set[str]) -> tuple[list[dict], list[str]]:
    """Raise ``ValueError`` on an unknown tenant, a role/tenant mismatch or a duplicate."""
    if not isinstance(bindings, list) or not isinstance(deny, list):
        raise ValueError("bindings and deny_subjects must be lists")
    if not all(isinstance(s, str) and s for s in deny):
        raise ValueError("deny_subjects must be non-empty strings")
    if not all(isinstance(b, dict) for b in bindings):
        raise ValueError("every binding must be an object")
    rows = cast(list[dict[str, Any]], bindings)
    seen: set[tuple] = set()
    for i, b in enumerate(rows):
        unknown = set(b) - {*_MATCHERS, "role", "tenant"}
        matchers = [m for m in _MATCHERS if m in b]
        if unknown or len(matchers) != 1 or not isinstance(b[matchers[0]], str) or not b[matchers[0]]:
            raise ValueError(f"binding {i} needs exactly one of {', '.join(_MATCHERS)} plus role/tenant: {b}")
        validate_scope(b.get("role", ""), b.get("tenant"))
        if b.get("tenant") and b["tenant"] not in tenants:
            raise ValueError(f"binding {i}: tenant {b['tenant']!r} does not exist")
        ident = (matchers[0], b[matchers[0]], b.get("tenant"))
        if ident in seen:
            raise ValueError(f"binding {i} duplicates another binding for {matchers[0]} {b[matchers[0]]!r}")
        seen.add(ident)
    return rows, deny


def principal_from_claims(cfg: OIDCConfig, claims: dict, bindings: list[dict], deny: list[str]) -> Principal:
    sub = claims["sub"]
    username = claims.get(cfg.username_claim)
    if not isinstance(username, str) or not username:
        raise HTTPException(401, f"token has no {cfg.username_claim!r} claim (auth.oidc.usernameClaim)")
    if cfg.username_claim == "email" and claims.get("email_verified") is not True:
        raise HTTPException(401, "username claim is email but email_verified is not true")
    groups = claims.get(cfg.groups_claim) or []
    groups = [groups] if isinstance(groups, str) else [g for g in groups if isinstance(g, str)]
    if sub in deny or username in deny:
        raise HTTPException(403, f"OIDC subject {sub!r} ({username}) is in deny-subjects")
    hit = [b for b in bindings if b.get("group") in groups or b.get("user") == username or b.get("subject") == sub]
    if not hit:
        raise HTTPException(
            403, f"no role binding matches OIDC subject {sub!r} (user {username!r}, groups {sorted(groups)})"
        )
    cluster = [b["role"] for b in hit if b["role"] in CLUSTER_ROLES]
    tenant_roles: dict[str, list[str]] = {}
    for b in hit:
        if b.get("tenant"):
            tenant_roles.setdefault(b["tenant"], []).append(b["role"])
    return Principal(
        type="oidc",
        subject=sub,
        label=f"OIDC user {username!r}",
        cluster_role=highest(cluster) if cluster else None,
        tenant_roles={t: highest(rs) for t, rs in tenant_roles.items()},
        issuer=cfg.issuer,
        groups=sorted(groups),
    )


async def read_bindings(core: K7Core, namespace: str) -> tuple[list[dict], list[str]]:
    v1 = await core._get_core_v1_client()
    try:
        cm = await v1.read_namespaced_config_map(BINDINGS_CM, namespace)
    except ApiException as e:
        if e.status == 404:
            return [], []
        raise
    return parse_bindings(cm.data or {})


async def write_bindings(core: K7Core, namespace: str, bindings: list[dict], deny: list[str]) -> None:
    v1 = await core._get_core_v1_client()
    data = {"bindings": yaml.safe_dump(bindings, sort_keys=False), "deny-subjects": yaml.safe_dump(deny)}
    body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=BINDINGS_CM, labels={"app": "k7-api"}), data=data)
    try:
        await v1.replace_namespaced_config_map(BINDINGS_CM, namespace, body)
    except ApiException as e:
        if e.status != 404:
            raise
        await v1.create_namespaced_config_map(namespace, body)
