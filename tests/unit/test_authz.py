"""The permission table, the role matrix on every route,
composition with tenant / namespace / node scopes, role escalation, the
admin key API, and the key → role migration."""

from __future__ import annotations

import json
import time

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute

from k7.api import authz
from k7.api.authz import PERMISSIONS, Principal, authorize_cluster, authorize_namespace, highest
from k7.api.main import app, require
from k7.core.models import OperationResult
from k7.deploy import k7_api_keys
from tests.unit.auth_fixtures import ALL_ROLES, ROLE_SCOPES, api

NS = {"namespace": "default"}
# One request per route, aimed at namespace `default` (tenant acme) where a namespace applies.
ROUTE_REQUESTS: dict[str, tuple[str, str, dict]] = {
    "root": ("GET", "/", {}),
    "health": ("GET", "/health", {}),
    "oidc_config": ("GET", "/api/v1/auth/oidc", {}),
    "whoami": ("GET", "/api/v1/auth/whoami", {}),
    "list_sandboxes": ("GET", "/api/v1/sandboxes", {"params": NS}),
    "get_sandbox": ("GET", "/api/v1/sandboxes/sb1", {"params": NS}),
    "get_sandbox_metrics": ("GET", "/api/v1/sandboxes/metrics", {"params": NS}),
    "list_snapshots": ("GET", "/api/v1/snapshots", {"params": NS}),
    "get_snapshot": ("GET", "/api/v1/snapshots/snap1", {"params": NS}),
    "create_sandbox": ("POST", "/api/v1/sandboxes", {"json": {"name": "t", "image": "alpine", **NS}}),
    "delete_sandbox": ("DELETE", "/api/v1/sandboxes/sb1", {"params": NS}),
    "delete_all_sandboxes": ("DELETE", "/api/v1/sandboxes", {"params": NS}),
    "pause_sandbox": ("POST", "/api/v1/sandboxes/sb1/pause", {"json": NS}),
    "resume_sandbox": ("POST", "/api/v1/sandboxes/sb1/resume", {"json": NS}),
    "fork_sandbox": ("POST", "/api/v1/sandboxes/sb1/fork", {"json": {"new_name": "f", **NS}}),
    "get_sandbox_logs": ("GET", "/api/v1/sandboxes/sb1/logs", {"params": NS}),
    "exec_command": ("POST", "/api/v1/sandboxes/sb1/exec", {"params": NS, "json": {"command": "id"}}),
    "create_snapshot": ("POST", "/api/v1/sandboxes/sb1/snapshot", {"json": {"snapshot_name": "s", **NS}}),
    "delete_snapshot": ("DELETE", "/api/v1/snapshots/snap1", {"params": NS}),
    "restore_snapshot": ("POST", "/api/v1/snapshots/snap1/restore", {"json": {"new_sandbox_name": "r", **NS}}),
    "gc_snapshots": ("POST", "/api/v1/snapshots/gc", {"json": NS}),
    "get_nodes_storage": ("GET", "/api/v1/nodes/storage", {}),
    "dedicate_node": ("POST", "/api/v1/nodes/n1/dedicate", {"json": {"tenant": "acme"}}),
    "undedicate_node": ("POST", "/api/v1/nodes/n1/undedicate", {}),
    "install_node": ("POST", "/api/v1/install", {"json": {"nodes": ["n1"]}}),
    "install_status": ("GET", "/api/v1/install/0123456789abcdef", {}),
    "create_key": (
        "POST",
        "/api/v1/keys",
        {"json": {"name": "n", "role": "user", "tenant": "acme", "expires_days": 1}},
    ),
    "list_keys": ("GET", "/api/v1/keys", {}),
    "delete_key": ("DELETE", "/api/v1/keys/n", {}),
    "get_role_bindings": ("GET", "/api/v1/role-bindings", {}),
    "put_role_bindings": ("PUT", "/api/v1/role-bindings", {"json": {"bindings": [{"group": "g", "role": "admin"}]}}),
    "list_tenants_route": ("GET", "/api/v1/tenants", {}),
    "get_tenant_route": ("GET", "/api/v1/tenants/acme", {}),
    "create_tenant_route": ("POST", "/api/v1/tenants", {"json": {"name": "t2", "spec": {"namespaces": ["t2"]}}}),
    "update_tenant_route": ("PATCH", "/api/v1/tenants/acme", {"json": {"quotas": None}}),
    "delete_tenant_route": ("DELETE", "/api/v1/tenants/acme", {}),
    "get_usage": ("GET", "/api/v1/usage", {"params": {"tenant": "acme", "from": "0", "to": "60"}}),
}
MATRIX = [(route, role) for route in sorted(ROUTE_REQUESTS) for role in ALL_ROLES]


# ── the permission table ───────────────────────────────────────────


def test_every_route_has_a_permission_entry_and_vice_versa():
    names = {r.name for r in app.routes if isinstance(r, APIRoute)}
    assert names - set(PERMISSIONS) == set(), "add new routes to authz.PERMISSIONS"
    assert set(PERMISSIONS) - names == set(), "PERMISSIONS names a route that no longer exists"
    assert set(ROUTE_REQUESTS) == names, "the role matrix below must exercise every route"


def test_require_runs_on_every_route():
    assert any(d.dependency is require for d in app.router.dependencies)


async def test_route_missing_from_the_table_is_a_loud_500():
    unlisted = FastAPI(dependencies=[Depends(require)])

    @unlisted.get("/x")
    async def not_in_permissions():
        return {}

    import httpx

    transport = httpx.ASGITransport(app=unlisted)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/x", headers={"X-API-Key": "k7_aaaaaaaa_x"})
    assert r.status_code == 500 and "not_in_permissions" in r.text


def test_table_shape_matches_the_spec():
    tenant_write = {"create_sandbox", "delete_sandbox", "exec_command", "get_sandbox_logs", "restore_snapshot"}
    for route in tenant_write:
        assert PERMISSIONS[route] == {"user", "admin"}, route
    for route in ("get_nodes_storage", "dedicate_node", "undedicate_node", "gc_snapshots"):
        assert PERMISSIONS[route] == {"operator", "admin"}, route
    for route in (
        "create_key",
        "list_keys",
        "delete_key",
        "get_role_bindings",
        "put_role_bindings",
        "install_node",
        "install_status",
    ):
        assert PERMISSIONS[route] == {"admin"}, route
    for route in ("list_sandboxes", "get_sandbox", "get_sandbox_metrics", "list_snapshots", "get_snapshot", "whoami"):
        assert PERMISSIONS[route] == set(ALL_ROLES), route
    assert {r for r, v in PERMISSIONS.items() if v is None} == {"root", "health", "oidc_config"}


def test_highest_ranks_roles():
    assert highest(["read-only", "admin", "user"]) == "admin"
    assert highest(["user", "operator"]) == "operator"


# ── the role matrix: every role × every route, for keys and OIDC ───


@pytest.mark.parametrize("source", ["key", "oidc"])
@pytest.mark.parametrize(("route", "role"), MATRIX)
async def test_role_matrix(route, role, source):
    method, path, kw = ROUTE_REQUESTS[route]
    with api() as a:
        if source == "key":
            a.add_key(role, **ROLE_SCOPES[role])
        else:
            a.add_oidc(role, groups=[f"g-{role}"])
            a.bindings = [{"group": f"g-{role}", **ROLE_SCOPES[role]}]
        a.core.list_sandboxes.return_value = []
        resp = await a.call(method, path, role, **kw)
        allowed = PERMISSIONS[route]
        if allowed is not None and role not in allowed:
            assert resp.status_code == 403, resp.text
            msg = resp.json()["error"]["message"]
            assert f"{route} needs role" in msg and role in msg
            assert a.cluster_calls() == [], "a refused request must not touch the cluster"
        else:
            assert resp.status_code not in (401, 403), resp.text


@pytest.mark.parametrize(("route", "role"), [(r, "x") for r in sorted(ROUTE_REQUESTS)])
async def test_unauthenticated_matrix(route, role):
    method, path, kw = ROUTE_REQUESTS[route]
    with api() as a:
        resp = await a.call(method, path, **kw)
    assert (resp.status_code == 401) == (PERMISSIONS[route] is not None), resp.text


# The spec's named matrix cases, spelled out.


@pytest.mark.parametrize("route", ["get_sandbox_logs", "exec_command"])
async def test_read_only_gets_403_on_logs_and_exec(route):
    method, path, kw = ROUTE_REQUESTS[route]
    with api() as a:
        a.add_key("ro", role="read-only", tenant="acme")
        resp = await a.call(method, path, "ro", **kw)
    assert resp.status_code == 403
    assert "needs role user or admin" in resp.json()["error"]["message"]


@pytest.mark.parametrize("route", ["exec_command", "get_sandbox_logs", "create_key", "create_sandbox"])
async def test_operator_gets_403_on_exec_logs_and_key_creation(route):
    method, path, kw = ROUTE_REQUESTS[route]
    with api() as a:
        a.add_key("op", role="operator")
        resp = await a.call(method, path, "op", **kw)
    assert resp.status_code == 403
    assert a.core.patch_secret_data.await_count == 0


async def test_operator_reads_every_tenant():
    with api() as a:
        a.add_key("op", role="operator")
        a.core.list_sandboxes.return_value = []
        for ns in ("default", "globex", "no-tenant-ns"):
            assert (await a.call("GET", "/api/v1/sandboxes", "op", params={"namespace": ns})).status_code == 200


@pytest.mark.parametrize("source", ["key", "oidc"])
@pytest.mark.parametrize("route", ["list_sandboxes", "create_sandbox", "delete_sandbox", "exec_command"])
async def test_tenant_user_gets_403_on_another_tenant(route, source):
    method, path, kw = ROUTE_REQUESTS[route]
    kw = json.loads(json.dumps(kw).replace('"default"', '"globex"'))
    with api() as a:
        if source == "key":
            a.add_key("u", role="user", tenant="acme")
        else:
            a.add_oidc("u", groups=["acme-devs"])
            a.bindings = [{"group": "acme-devs", "role": "user", "tenant": "acme"}]
        resp = await a.call(method, path, "u", **kw)
        assert resp.status_code == 403, resp.text
        assert "belongs to tenant globex" in resp.json()["error"]["message"]
        assert a.cluster_calls() == []


# ── composition: role, then tenant, then key narrowing ─────────────


async def test_tenant_role_in_a_namespace_no_tenant_claims_is_403():
    with api() as a:
        a.add_key("u", role="user", tenant="acme")
        resp = await a.call("GET", "/api/v1/sandboxes", "u", params={"namespace": "kube-system"})
    assert resp.status_code == 403 and "belongs to no tenant" in resp.json()["error"]["message"]


@pytest.mark.parametrize("params", [{}, {"all_namespaces": "true"}])
async def test_tenant_role_cannot_list_all_namespaces(params):
    with api() as a:
        a.add_key("u", role="user", tenant="acme")
        path = "/api/v1/snapshots" if params else "/api/v1/sandboxes"
        resp = await a.call("GET", path, "u", params=params)
    assert resp.status_code == 403 and "all-namespaces" in resp.json()["error"]["message"]


async def test_key_namespaces_narrow_a_tenant_role():
    with api() as a:
        a.add_key("u", role="user", tenant="acme", namespaces=["acme-prod"])
        a.core.list_sandboxes.return_value = []
        ok = await a.call("GET", "/api/v1/sandboxes", "u", params={"namespace": "acme-prod"})
        narrowed = await a.call("GET", "/api/v1/sandboxes", "u", params={"namespace": "default"})
    assert ok.status_code == 200
    assert narrowed.status_code == 403 and "not authorized for namespace 'default'" in narrowed.text


async def test_key_nodes_narrow_a_tenant_role():
    with api() as a:
        a.add_key("u", role="user", tenant="acme", nodes=["n1"])
        a.core.create_sandbox.return_value = OperationResult(success=True)
        body = {"name": "t", "image": "alpine", "namespace": "default"}
        other = await a.call("POST", "/api/v1/sandboxes", "u", json={**body, "node_name": "n2"})
        pinned = await a.call("POST", "/api/v1/sandboxes", "u", json=body)
    assert other.status_code == 403
    assert pinned.status_code == 201 and a.core.create_sandbox.await_args.args[0].node_name == "n1"


@pytest.mark.parametrize(
    ("scope", "message"),
    [({"namespaces": ["default"]}, "all-namespaces"), ({"nodes": ["n1"]}, "cluster-wide node")],
)
@pytest.mark.parametrize("route", ["get_nodes_storage", "dedicate_node", "create_key", "put_role_bindings"])
async def test_narrowed_cluster_role_cannot_use_cluster_routes(route, scope, message):
    method, path, kw = ROUTE_REQUESTS[route]
    with api() as a:
        a.add_key("a", role="admin", **scope)
        resp = await a.call(method, path, "a", **kw)
        assert resp.status_code == 403 and message in resp.json()["error"]["message"]
        assert a.core.patch_secret_data.await_count == 0


async def test_narrowed_admin_still_works_inside_its_namespace():
    with api() as a:
        a.add_key("a", role="admin", namespaces=["globex"])
        a.core.list_sandboxes.return_value = []
        assert (await a.call("GET", "/api/v1/sandboxes", "a", params={"namespace": "globex"})).status_code == 200
        assert (await a.call("GET", "/api/v1/sandboxes", "a", params={"namespace": "default"})).status_code == 403


async def test_legacy_namespace_scoped_key_needs_no_tenant_crd():
    with api(tenants={}) as a:
        a.add_key("legacy", role="user", namespaces=["team-a"])
        a.core.list_sandboxes.return_value = []
        ok = await a.call("GET", "/api/v1/sandboxes", "legacy", params={"namespace": "team-a"})
        other = await a.call("GET", "/api/v1/sandboxes", "legacy", params={"namespace": "team-b"})
        cluster = await a.call("GET", "/api/v1/nodes/storage", "legacy")
    assert ok.status_code == 200
    assert other.status_code == 403
    assert cluster.status_code == 403


async def test_namespace_claimed_by_two_tenants_is_a_loud_500():
    with api(tenants={"acme": ["shared"], "globex": ["shared"]}) as a:
        a.add_key("u", role="user", tenant="acme")
        resp = await a.call("GET", "/api/v1/sandboxes", "u", params={"namespace": "shared"})
    err = resp.json()["error"]
    assert resp.status_code == 500 and err["code"] == "TenantConflict" and "acme and globex" in err["message"]
    assert err["details"] == {"namespace": "shared", "tenants": ["acme", "globex"]}


async def test_missing_tenant_crd_is_loud_for_tenant_roles():
    from unittest.mock import AsyncMock

    from kubernetes_asyncio.client.rest import ApiException

    from k7.api import tenants

    custom = AsyncMock()
    custom.list_cluster_custom_object.side_effect = ApiException(status=404)
    core = AsyncMock()
    core._get_custom_objects_client.return_value = custom
    with pytest.raises(RuntimeError, match="Tenant CRD tenants.k7.katakate.org is not installed"):
        await tenants.namespace_tenant(core, "default")
    custom.list_cluster_custom_object.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        await tenants.list_tenants(core)
    custom.list_cluster_custom_object.side_effect = None
    custom.list_cluster_custom_object.return_value = {
        "items": [{"metadata": {"name": "acme"}, "spec": {"namespaces": ["default"]}}, {"metadata": {"name": "e"}}]
    }
    assert await tenants.list_tenants(core) == {"acme": ["default"], "e": []}
    assert await tenants.namespace_tenant(core, "default") == "acme"
    assert await tenants.namespace_tenant(core, "other") is None


@pytest.mark.parametrize(
    ("entry", "status", "message"),
    [
        ({}, 403, "k7 api keys migrate"),
        ({"admin": True}, 403, "k7 api keys migrate"),
        ({"role": "root"}, 403, "role must be one of"),
        ({"role": "admin", "tenant": "acme"}, 403, "cluster-scoped"),
        ({"role": "user"}, 403, "tenant-scoped"),
    ],
)
async def test_keys_without_a_valid_role_are_refused(entry, status, message):
    with api() as a:
        a.add_key("k", **entry)
        resp = await a.call("GET", "/api/v1/auth/whoami", "k")
    assert resp.status_code == status and message in resp.json()["error"]["message"]


async def test_expired_and_unknown_keys_are_401_before_any_role_check():
    with api() as a:
        a.add_key("old", role="admin", expires=int(time.time()) - 1)
        assert (await a.call("GET", "/api/v1/auth/whoami", "old")).status_code == 401
        assert (await a.call("GET", "/api/v1/auth/whoami", headers={"X-API-Key": "k7_aaaaaaaa_x"})).status_code == 401


def _p(**kw) -> Principal:
    return Principal(type="key", subject="s", label="L", **kw)


async def test_authorize_namespace_returns_the_granting_role_and_tenant():
    from unittest.mock import AsyncMock, patch

    p = _p(tenant_roles={"acme": "read-only", "globex": "user"})
    p.allowed = frozenset(ALL_ROLES)
    with patch("k7.api.tenants.list_tenants", AsyncMock(return_value={"acme": ["a"], "globex": ["g"]})):
        assert await authorize_namespace(p, AsyncMock(), "a") == ("read-only", "acme")
        assert await authorize_namespace(p, AsyncMock(), "g") == ("user", "globex")
        p.allowed = frozenset({"user", "admin"})
        with pytest.raises(HTTPException) as e:
            await authorize_namespace(p, AsyncMock(), "a")
    assert e.value.status_code == 403 and "needs user there" in str(e.value.detail)


def test_authorize_cluster_refuses_tenant_roles_and_narrowing():
    for p in (
        _p(tenant_roles={"acme": "user"}),
        _p(cluster_role="admin", namespaces=["x"]),
        _p(cluster_role="admin", nodes=["n"]),
    ):
        p.allowed = frozenset(ALL_ROLES)
        with pytest.raises(HTTPException):
            authorize_cluster(p)
    ok = _p(cluster_role="operator")
    ok.allowed = frozenset({"operator", "admin"})
    authorize_cluster(ok)


def test_grants_describe_every_scope():
    p = _p(cluster_role="operator", tenant_roles={"b": "user", "a": "read-only"})
    assert p.grants() == ["operator", "read-only on tenant a", "user on tenant b"]
    assert _p(legacy_role="user", namespaces=["x", "y"]).grants() == ["user on namespaces x, y"]
    assert _p().grants() == ["no role"]
    assert authz.apply_node_scope(_p(), None) is None


# ── role escalation ────────────────────────────────────────────────


@pytest.mark.parametrize("caller", ["read-only", "user", "operator"])
@pytest.mark.parametrize("target", ["admin", "operator", "user"])
async def test_non_admins_cannot_mint_keys(caller, target):
    body = {"name": "x", "role": target, "expires_days": 1, **({"tenant": "acme"} if target == "user" else {})}
    with api() as a:
        a.add_key(caller, **ROLE_SCOPES[caller])
        resp = await a.call("POST", "/api/v1/keys", caller, json=body)
        assert resp.status_code == 403
        assert a.core.patch_secret_data.await_count == 0


@pytest.mark.parametrize("caller", ["read-only", "user", "operator"])
async def test_non_admins_cannot_edit_role_bindings_or_revoke_keys(caller):
    with api() as a:
        a.add_key(caller, **ROLE_SCOPES[caller])
        put = await a.call("PUT", "/api/v1/role-bindings", caller, json={"bindings": [{"user": "me", "role": "admin"}]})
        dele = await a.call("DELETE", "/api/v1/keys/someone", caller)
        get = await a.call("GET", "/api/v1/role-bindings", caller)
    assert put.status_code == dele.status_code == get.status_code == 403


@pytest.mark.parametrize("caller", ["read-only", "user"])
async def test_tenant_roles_cannot_dedicate_nodes(caller):
    with api() as a:
        a.add_key(caller, **ROLE_SCOPES[caller])
        resp = await a.call("POST", "/api/v1/nodes/n1/dedicate", caller, json={"tenant": "acme"})
        assert resp.status_code == 403 and a.core.dedicate_node.await_count == 0


async def test_tenant_admin_binding_cannot_exist():
    with api() as a:
        a.add_key("a", role="admin")
        resp = await a.call(
            "PUT", "/api/v1/role-bindings", "a", json={"bindings": [{"group": "g", "role": "admin", "tenant": "acme"}]}
        )
    assert resp.status_code == 400 and "cluster-scoped" in resp.text


async def test_user_on_one_tenant_and_read_only_on_another_stays_separate():
    with api() as a:
        a.add_oidc("mixed", groups=["acme-devs", "globex-auditors"])
        a.bindings = [
            {"group": "acme-devs", "role": "user", "tenant": "acme"},
            {"group": "globex-auditors", "role": "read-only", "tenant": "globex"},
        ]
        a.core.list_sandboxes.return_value = []
        assert (await a.call("GET", "/api/v1/sandboxes", "mixed", params={"namespace": "globex"})).status_code == 200
        exec_g = await a.call(
            "POST", "/api/v1/sandboxes/sb/exec", "mixed", params={"namespace": "globex"}, json={"command": "id"}
        )
        assert exec_g.status_code == 403 and "needs user there" in exec_g.text
        assert a.core.exec_command.await_count == 0


# ── admin key API ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"role": "user", "tenant": "acme", "expires_days": 1}, "name is required"),
        ({"name": "x", "role": "user", "tenant": "acme"}, "expires_days"),
        ({"name": "x", "role": "user", "tenant": "acme", "expires_days": 0}, "expires_days"),
        ({"name": "x", "role": "user", "tenant": "acme", "expires_days": "7"}, "expires_days"),
        ({"name": "x", "role": "user", "expires_days": 1}, "tenant-scoped"),
        ({"name": "x", "role": "admin", "tenant": "acme", "expires_days": 1}, "cluster-scoped"),
        ({"name": "x", "role": "superuser", "expires_days": 1}, "role must be one of"),
        ({"name": "x", "expires_days": 1}, "role must be one of"),
        ({"name": "x", "role": "user", "tenant": "ghost", "expires_days": 1}, "does not exist"),
    ],
)
async def test_create_key_rejects_bad_requests(body, message):
    with api() as a:
        a.add_key("a", role="admin")
        resp = await a.call("POST", "/api/v1/keys", "a", json=body)
        assert resp.status_code == 400 and message in resp.json()["error"]["message"]
        assert a.core.patch_secret_data.await_count == 0


async def test_create_key_returns_token_once_and_the_new_key_carries_its_role():
    with api() as a:
        a.add_key("a", role="admin")
        body = {"name": "ci", "role": "user", "tenant": "acme", "expires_days": 7, "nodes": ["n1"]}
        resp = await a.call("POST", "/api/v1/keys", "a", json=body)
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert "hash" not in data and data["role"] == "user" and data["tenant"] == "acme" and data["nodes"] == ["n1"]
        name, _ns, patch_data = a.core.patch_secret_data.await_args.args
        assert name == "k7-api-keys"
        stored = json.loads(patch_data[data["id"]])
        assert data["token"] not in json.dumps(stored)
        assert k7_api_keys.match_token({data["id"]: stored}, data["token"]) == (data["id"], stored)
        a.store.keys[data["id"]] = stored
        a.tokens["ci"] = data["token"]
        assert (await a.call("POST", "/api/v1/keys", "ci", json=body)).status_code == 403
        who = (await a.call("GET", "/api/v1/auth/whoami", "ci")).json()["data"]
    assert who["grants"] == ["user on tenant acme"] and who["type"] == "key"


async def test_list_keys_hides_hashes_and_merges_last_used():
    with api() as a:
        a.add_key("a", role="admin")
        stored = {"aaaaaaaa": json.dumps({"name": "x", "hash": "h", "role": "user", "tenant": "acme"})}
        a.core.read_secret_data.side_effect = lambda name, _ns: stored if name == "k7-api-keys" else {"aaaaaaaa": "9"}
        resp = await a.call("GET", "/api/v1/keys", "a")
    assert resp.status_code == 200
    assert resp.json()["data"] == [{"id": "aaaaaaaa", "name": "x", "role": "user", "tenant": "acme", "last_used": "9"}]


async def test_delete_key_revokes_every_key_with_the_name_and_404s_unknown():
    with api() as a:
        a.add_key("a", role="admin")
        stored = {
            k: json.dumps({"name": n, "hash": "h", "role": "admin"})
            for k, n in [("k1", "dup"), ("k2", "dup"), ("k3", "keep")]
        }
        a.core.read_secret_data.return_value = stored
        ok = await a.call("DELETE", "/api/v1/keys/dup", "a")
        missing = await a.call("DELETE", "/api/v1/keys/ghost", "a")
        assert ok.status_code == 200 and ok.json()["data"]["revoked"] == ["k1", "k2"]
        assert a.core.patch_secret_data.await_args.args[2] == {"k1": None, "k2": None}
    assert missing.status_code == 404


@pytest.mark.parametrize("role", ["operator", "admin"])
async def test_dedicate_and_undedicate_over_the_api(role):
    with api() as a:
        a.add_key(role, role=role)
        a.core.dedicate_node.return_value = OperationResult(success=True, message="dedicated")
        a.core.undedicate_node.return_value = OperationResult(success=False, error="node n9 not found")
        ok = await a.call("POST", "/api/v1/nodes/n1/dedicate", role, json={"tenant": "acme"})
        missing = await a.call("POST", "/api/v1/nodes/n9/undedicate", role)
        a.core.dedicate_node.assert_awaited_once_with("n1", "acme")
    assert ok.status_code == 200 and "dedicated to tenant acme" in ok.json()["data"]["message"]
    assert missing.status_code == 404


async def test_audit_line_carries_the_granting_role_and_tenant():
    import logging

    from k7 import logging as k7_logging
    from k7.api.main import audit_log

    lines: list[str] = []
    handler = logging.Handler()
    handler.setFormatter(k7_logging.JsonFormatter())
    handler.emit = lambda rec: lines.append(handler.format(rec))  # type: ignore[method-assign]
    audit_log.addHandler(handler)
    try:
        with api() as a:
            a.add_key("u", role="user", tenant="acme")
            a.core.delete_sandbox.return_value = OperationResult(success=True, message="ok")
            await a.call("DELETE", "/api/v1/sandboxes/sb1", "u", params={"namespace": "acme-prod"})
            await a.call("DELETE", "/api/v1/sandboxes/sb1", "u", params={"namespace": "globex"})
    finally:
        audit_log.removeHandler(handler)
    ok, denied = (json.loads(x) for x in lines)
    assert (ok["principal_type"], ok["role"], ok["tenant"], ok["status"]) == ("key", "user", "acme", 200)
    assert ok["subject"] == a.key_ids["u"] and ok["idp_issuer"] is None
    assert (denied["status"], denied["namespace"], denied["subject"]) == (403, "globex", a.key_ids["u"])


# ── migration ──────────────────────────────────────────────────────


def test_migrate_roles_maps_each_legacy_shape():
    keys = {
        "a": {"name": "root", "admin": True},
        "b": {"name": "team", "namespaces": ["x"]},
        "c": {"name": "tenant", "tenant": "acme"},
        "d": {"name": "open"},
        "e": {"name": "done", "role": "operator"},
        "f": {"name": "not-admin", "admin": False},
    }
    patch, lines = k7_api_keys.migrate_roles(keys)
    got = {k: json.loads(v) for k, v in patch.items()}
    assert set(got) == {"a", "b", "c", "d", "f"}
    assert got["a"] == {"name": "root", "role": "admin"}
    assert got["b"] == {"name": "team", "namespaces": ["x"], "role": "user"}
    assert got["c"] == {"name": "tenant", "tenant": "acme", "role": "user"}
    assert got["d"] == {"name": "open", "role": "admin", "unscoped_legacy": True}
    assert got["f"] == {"name": "not-admin", "role": "admin", "unscoped_legacy": True}
    assert len(lines) == 5 and all("-> role" in line for line in lines)
    assert "development mode only" in next(line for line in lines if "key d" in line)
    assert k7_api_keys.migrate_roles({**keys, **{k: json.loads(v) for k, v in patch.items()}}) == ({}, [])


def test_migrate_is_idempotent_through_the_secret(tmp_path):
    secrets: dict[str, dict[str, str]] = {"k7-api-keys": {"a": json.dumps({"name": "root", "admin": True})}}

    class Kc:
        namespace = "kube-system"

        def read(self, name):
            return dict(secrets[name]) if name in secrets else None

        def ensure(self, name):
            secrets.setdefault(name, {})

        def patch(self, name, data):
            secrets[name].update(data)

    first = k7_api_keys.migrate(Kc(), tmp_path / "none.json")  # type: ignore[arg-type]
    assert "key a (root): admin: true -> role admin" in first
    assert json.loads(secrets["k7-api-keys"]["a"]) == {"name": "root", "role": "admin"}
    assert "every key already has a role" in k7_api_keys.migrate(Kc(), tmp_path / "none.json")  # type: ignore[arg-type]


async def test_migrated_unscoped_key_works_as_admin():
    with api() as a:
        a.add_key("open", role="admin", unscoped_legacy=True)
        who = await a.call("GET", "/api/v1/auth/whoami", "open")
    assert who.status_code == 200 and who.json()["data"]["grants"] == ["admin"]


@pytest.mark.parametrize(
    ("role", "tenant", "ok"),
    [
        ("read-only", "t", True),
        ("user", "t", True),
        ("operator", None, True),
        ("admin", None, True),
        ("read-only", None, False),
        ("user", None, False),
        ("operator", "t", False),
        ("admin", "t", False),
        ("root", None, False),
        ("", None, False),
    ],
)
def test_validate_scope(role, tenant, ok):
    if ok:
        k7_api_keys.validate_scope(role, tenant)
    else:
        with pytest.raises(ValueError):
            k7_api_keys.validate_scope(role, tenant)
