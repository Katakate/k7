"""Shared doubles for the role / OIDC tests: drive the real FastAPI
app (``require`` on every route) with an in-memory key store, a fixed set of
tenants, a mocked K7Core and, optionally, an OIDC verifier + bindings."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from k7.api.main import app
from k7.api.oidc import OIDCConfig
from k7.deploy.k7_api_keys import mint_token

TENANTS = {"acme": ["default", "acme-prod"], "globex": ["globex"]}
ISSUER = "https://idp.example.com"
CFG = OIDCConfig(issuer=ISSUER, audience="k7-api", client_id="k7-cli", groups_claim="groups", username_claim="email")
ALL_ROLES = ("read-only", "user", "operator", "admin")
# One principal per role; tenant roles live on tenant acme (namespaces default, acme-prod).
ROLE_SCOPES: dict[str, dict] = {
    "read-only": {"role": "read-only", "tenant": "acme"},
    "user": {"role": "user", "tenant": "acme"},
    "operator": {"role": "operator"},
    "admin": {"role": "admin"},
}


class MemStore:
    def __init__(self) -> None:
        self.keys: dict[str, dict] = {}
        self.touched: list[str] = []

    def snapshot(self) -> dict:
        return self.keys

    def touch(self, key_id: str) -> None:
        self.touched.append(key_id)

    def health(self) -> dict:
        return {}


def jwt_shaped(name: str) -> str:
    """A bearer that takes the OIDC path (two dots, no ``k7_`` prefix); the fake verifier maps it to claims."""
    return f"eyJhbGciOiJSUzI1NiJ9.{name}.sig"


@dataclass
class Api:
    store: MemStore
    core: Any
    tokens: dict[str, str] = field(default_factory=dict)
    claims: dict[str, dict] = field(default_factory=dict)
    bindings: list[dict] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    key_ids: dict[str, str] = field(default_factory=dict)

    def add_key(self, label: str, *, expires: int | None = None, **entry: Any) -> str:
        token, key_id, digest = mint_token()
        self.store.keys[key_id] = {
            "name": label,
            "hash": digest,
            "expires": expires if expires is not None else int(time.time()) + 3600,
            **entry,
        }
        self.tokens[label] = token
        self.key_ids[label] = key_id
        return token

    def add_oidc(self, label: str, *, sub: str | None = None, groups: list[str] | None = None, **extra: Any) -> str:
        token = jwt_shaped(label)
        self.claims[token] = {
            "sub": sub or f"sub-{label}",
            "email": f"{label}@example.com",
            "email_verified": True,
            "groups": groups or [],
            "iss": ISSUER,
            "aud": "k7-api",
            **extra,
        }
        self.tokens[label] = token
        return token

    def headers(self, label: str) -> dict[str, str]:
        token = self.tokens[label]
        return {"Authorization": f"Bearer {token}"} if token in self.claims else {"X-API-Key": token}

    async def call(self, method: str, path: str, label: str | None = None, **kw: Any) -> httpx.Response:
        headers = {**(self.headers(label) if label else {}), **kw.pop("headers", {})}
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers, **kw)

    def cluster_calls(self) -> list:
        return [c for c in self.core.method_calls if c[0] != "aclose"]


@contextmanager
def api(*, tenants: dict[str, list[str]] | None = None, oidc: bool = True) -> Iterator[Api]:
    store = MemStore()
    core = AsyncMock()
    a = Api(store=store, core=core)

    async def verify(token: str) -> dict:
        return a.claims[token]

    verifier = SimpleNamespace(cfg=CFG, verify=verify) if oidc else None

    async def read_bindings(_core, _ns):
        return a.bindings, a.deny

    tenants = TENANTS if tenants is None else tenants
    list_tenants = AsyncMock(return_value=tenants)
    crs = AsyncMock(return_value=[{"metadata": {"name": t}, "spec": {"namespaces": n}} for t, n in tenants.items()])
    with (
        patch("k7.api.main.STORE", store),
        patch("k7.api.main.K7Core", MagicMock(return_value=core)),
        patch("k7.api.tenants.list_tenants", list_tenants),
        patch("k7.api.main.list_tenants", list_tenants),
        patch("k7.api.main.list_tenant_crs", crs),
        patch("k7.core.tenancy.list_tenants", crs),
        patch("k7.api.main.OIDC", verifier),
        patch("k7.api.main.read_bindings", read_bindings),
    ):
        yield a
