"""Unit tests for API authentication (mocked file I/O, no k8s)."""

import hashlib
import json
import logging.handlers
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from kubernetes_asyncio.client.exceptions import ApiException

from k7 import logging as k7_logging
from k7 import metrics
from k7.api.authz import Principal, apply_node_scope, authorize_namespace
from k7.api.keystore import FileKeyStore, SecretKeyStore
from k7.api.main import app, build_key_store
from k7.api.remote_install import RemoteInstallError
from k7.core.models import OperationResult
from k7.deploy.k7_api_keys import match_token, mint_token, token_key_id

TEST_KEY = "k7-test-secret-key-abc123"
TEST_KEY_HASH = hashlib.sha256(TEST_KEY.encode()).hexdigest()


def _make_keys_data(
    *,
    expires: int | None = None,
    last_used: int | None = None,
    namespaces: list[str] | None = None,
    nodes: list[str] | None = None,
    role: str = "admin",
) -> dict:
    entry: dict = {"name": "test-key", "role": role}
    if expires is not None:
        entry["expires"] = expires
    if last_used is not None:
        entry["last_used"] = last_used
    if namespaces is not None:
        entry["namespaces"] = namespaces
    if nodes is not None:
        entry["nodes"] = nodes
    return {TEST_KEY_HASH: entry}


@pytest.fixture()
def keys_file(tmp_path: Path) -> Path:
    return tmp_path / "api_keys.json"


@pytest.fixture()
def _patch_keys_file(keys_file: Path):
    with patch("k7.api.main.STORE", FileKeyStore(keys_file)):
        yield


# --- key stores and token format ---

LEGACY_ID = f"legacy-{TEST_KEY_HASH[:8]}"


class TestFileKeyStore:
    def test_missing_file(self, keys_file: Path):
        assert FileKeyStore(keys_file).snapshot() == {}

    def test_empty_file_fails_loud(self, keys_file: Path):
        # An existing-but-unparseable store is a deployment bug: it must be
        # a loud 500, not a silent {} that rejects every key as "invalid".
        keys_file.write_text("")
        with pytest.raises(HTTPException) as exc_info:
            FileKeyStore(keys_file).snapshot()
        assert exc_info.value.status_code == 500
        assert "corrupt" in exc_info.value.detail

    def test_legacy_layout_is_converted(self, keys_file: Path):
        keys_file.write_text(json.dumps(_make_keys_data(last_used=5)))
        snap = FileKeyStore(keys_file).snapshot()
        assert snap[LEGACY_ID]["hash"] == TEST_KEY_HASH
        assert snap[LEGACY_ID]["name"] == "test-key"
        assert "last_used" not in snap[LEGACY_ID]


class TestTokenFormat:
    def test_mint_shape_and_hash(self):
        token, key_id, digest = mint_token()
        assert token.startswith(f"k7_{key_id}_") and len(key_id) == 8
        assert token_key_id(token) == key_id
        assert digest == hashlib.sha256(token.split("_", 2)[2].encode()).hexdigest()
        assert match_token({key_id: {"hash": digest}}, token) == (key_id, {"hash": digest})

    def test_wrong_secret_with_known_id_rejected(self):
        token, key_id, digest = mint_token()
        other, _, _ = mint_token()
        forged = f"k7_{key_id}_{other.split('_', 2)[2]}"
        assert match_token({key_id: {"hash": digest}}, forged) is None

    def test_legacy_token_matched_by_scan(self):
        keys = {LEGACY_ID: {"hash": TEST_KEY_HASH, "name": "old"}}
        assert match_token(keys, TEST_KEY) == (LEGACY_ID, keys[LEGACY_ID])
        assert token_key_id(TEST_KEY) is None

    def test_new_format_never_scans_legacy(self):
        token, _, _ = mint_token()
        keys = {LEGACY_ID: {"hash": hashlib.sha256(token.encode()).hexdigest()}}
        assert match_token(keys, token) is None


class _MemStore:
    """In-memory KeyStore double; has no write path at all."""

    def __init__(self, keys: dict):
        self.keys = keys
        self.touched: list[str] = []

    def snapshot(self) -> dict:
        return self.keys

    def touch(self, key_id: str) -> None:
        self.touched.append(key_id)

    def health(self) -> dict:
        return {}


class TestSecretBackedAuth:
    async def _get(self, token: str) -> httpx.Response:
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.list_sandboxes = AsyncMock(return_value=[])
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get("/api/v1/sandboxes", headers={"X-API-Key": token})

    async def test_many_requests_no_store_writes(self):
        token, key_id, digest = mint_token()
        store = _MemStore({key_id: {"name": "n", "hash": digest, "expires": int(time.time()) + 60, "role": "admin"}})
        with patch("k7.api.main.STORE", store), patch("k7.api.keystore.K7Core") as kc:
            for _ in range(50):
                assert (await self._get(token)).status_code == 200
        kc.assert_not_called()
        assert store.touched == [key_id] * 50

    async def test_expired_key_rejected_not_purged(self):
        token, key_id, digest = mint_token()
        store = _MemStore({key_id: {"name": "n", "hash": digest, "expires": int(time.time()) - 1}})
        with patch("k7.api.main.STORE", store):
            resp = await self._get(token)
        assert resp.status_code == 401
        assert key_id in store.keys

    async def test_never_synced_secret_store_is_503(self):
        with patch("k7.api.main.STORE", SecretKeyStore("kube-system")):
            resp = await self._get(mint_token()[0])
        assert resp.status_code == 503
        assert "stale" in resp.json()["error"]["message"]

    async def test_stale_secret_store_is_503(self, monkeypatch):
        token, key_id, digest = mint_token()
        store = SecretKeyStore("kube-system")
        core = SimpleNamespace(
            read_secret_data=AsyncMock(
                return_value={key_id: json.dumps({"name": "n", "hash": digest, "role": "admin"})}
            )
        )
        await store.refresh(core)  # type: ignore[arg-type]
        with patch("k7.api.main.STORE", store):
            assert (await self._get(token)).status_code == 200
            synced = time.monotonic()
            monkeypatch.setattr("k7.api.keystore.time.monotonic", lambda: synced + 61)
            assert (await self._get(token)).status_code == 503

    async def test_missing_secret_is_loud(self):
        store = SecretKeyStore("kube-system")
        with pytest.raises(RuntimeError, match="k7 api keys migrate"):
            await store.refresh(SimpleNamespace(read_secret_data=AsyncMock(return_value=None)))  # type: ignore[arg-type]

    async def test_usage_flush_max_merges_into_usage_secret(self):
        store = SecretKeyStore("kube-system")
        store.touch("aaaaaaaa")
        store.touch("bbbbbbbb")
        far_future = str(int(time.time()) + 10_000)
        core = SimpleNamespace(
            read_secret_data=AsyncMock(return_value={"aaaaaaaa": far_future}),
            patch_secret_data=AsyncMock(),
        )
        await store.flush(core)  # type: ignore[arg-type]
        name, _ns, data = core.patch_secret_data.await_args.args
        assert name == "k7-api-keys-usage"
        assert data["aaaaaaaa"] == far_future
        assert int(data["bbbbbbbb"]) <= int(time.time())
        assert store.health()["usage_flush_error"] is None

    def test_store_kind_is_required(self):
        with pytest.raises(RuntimeError, match="K7_API_KEY_STORE"):
            build_key_store(None)


# --- verify_api_key via httpx.AsyncClient ---


class TestVerifyApiKey:
    @pytest.fixture(autouse=True)
    def _setup(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts)
        keys_file.write_text(json.dumps(data))
        self.keys_file = keys_file

    async def test_valid_x_api_key(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200

            resp = await client.post(
                "/api/v1/sandboxes",
                headers={"X-API-Key": TEST_KEY},
                json={"name": "t", "image": "alpine"},
            )
            # May fail with 400/500 (no k8s), but should NOT be 401
            assert resp.status_code != 401

    async def test_valid_bearer_token(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/sandboxes",
                headers={"Authorization": f"Bearer {TEST_KEY}"},
                json={"name": "t", "image": "alpine"},
            )
            assert resp.status_code != 401

    async def test_missing_key_returns_401(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/sandboxes",
                json={"name": "t", "image": "alpine"},
            )
            assert resp.status_code == 401

    async def test_wrong_key_returns_401(self):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/sandboxes",
                headers={"X-API-Key": "wrong-key-value"},
                json={"name": "t", "image": "alpine"},
            )
            assert resp.status_code == 401

    async def test_expired_key_returns_401(self):
        expired_ts = int(time.time()) - 3600
        data = _make_keys_data(expires=expired_ts)
        self.keys_file.write_text(json.dumps(data))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/sandboxes",
                headers={"X-API-Key": TEST_KEY},
                json={"name": "t", "image": "alpine"},
            )
            assert resp.status_code == 401


# --- authorize_namespace ---


def _key(**kw) -> Principal:
    p = Principal(type="key", subject="k", label="API key k", **kw)
    p.allowed = frozenset({"read-only", "user", "operator", "admin"})
    return p


class TestAuthorizeNamespace:
    async def test_unrestricted_key_allowed_everywhere(self):
        core = AsyncMock()
        assert await authorize_namespace(_key(cluster_role="admin"), core, "alpha") == ("admin", None)
        await authorize_namespace(_key(cluster_role="admin", namespaces=[]), core, "beta")
        await authorize_namespace(_key(cluster_role="admin"), core, None, all_namespaces=True)

    async def test_scoped_key_allowed_in_listed_namespace(self):
        await authorize_namespace(_key(cluster_role="admin", namespaces=["alpha", "gamma"]), AsyncMock(), "alpha")

    @pytest.mark.parametrize(("ns", "all_ns"), [("beta", False), ("alpha", True), (None, False)])
    async def test_scoped_key_denied(self, ns, all_ns):
        with pytest.raises(HTTPException) as exc:
            await authorize_namespace(
                _key(cluster_role="admin", namespaces=["alpha"]), AsyncMock(), ns, all_namespaces=all_ns
            )
        assert exc.value.status_code == 403

    async def test_scoped_key_list_other_namespace_returns_403(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts, namespaces=["alpha"])
        keys_file.write_text(json.dumps(data))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/sandboxes",
                headers={"X-API-Key": TEST_KEY},
                params={"namespace": "beta"},
            )
        assert resp.status_code == 403

    async def test_scoped_key_nodes_storage_returns_403(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts, namespaces=["alpha"])
        keys_file.write_text(json.dumps(data))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/nodes/storage",
                headers={"X-API-Key": TEST_KEY},
            )
        assert resp.status_code == 403
        assert "all-namespaces" in resp.json()["error"]["message"]

    async def test_unscoped_key_nodes_storage_returns_200(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts)
        keys_file.write_text(json.dumps(data))
        payload = {"node-a": {"kata_thinpool": {"size_bytes": 1}}}

        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.nodes_storage = AsyncMock(return_value=payload)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/nodes/storage",
                    headers={"X-API-Key": TEST_KEY},
                )
        assert resp.status_code == 200
        assert resp.json()["data"] == payload
        core_cls.return_value.nodes_storage.assert_awaited_once()


# --- POST /api/v1/install, GET /api/v1/install/{operation} (k7.api.remote_install) ---

OP_ID = "0123456789abcdef"


class TestInstallEndpoints:
    @pytest.fixture()
    def env(self, _patch_keys_file, keys_file: Path, monkeypatch):
        monkeypatch.setenv("K7_API_ENABLE_REMOTE_INSTALL", "1")
        self.keys_file = keys_file
        with (
            patch("k7.api.main.K7Core") as core_cls,
            patch("k7.api.main.remote_install.start", new_callable=AsyncMock) as start,
            patch("k7.api.main.remote_install.status", new_callable=AsyncMock) as status,
        ):
            core_cls.return_value._get_core_v1_client = AsyncMock(return_value="v1")
            core_cls.return_value._get_apps_v1_client = AsyncMock(return_value="apps")
            yield SimpleNamespace(start=start, status=status, core=core_cls.return_value)

    async def _call(
        self, method: str, url: str, *, admin: bool = True, scope: dict | None = None, **kw
    ) -> httpx.Response:
        data = _make_keys_data(expires=int(time.time()) + 86400, role="admin" if admin else "operator", **(scope or {}))
        self.keys_file.write_text(json.dumps(data))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, headers={"X-API-Key": TEST_KEY}, **kw)

    @pytest.mark.parametrize("method,url", [("POST", "/api/v1/install"), ("GET", f"/api/v1/install/{OP_ID}")])
    async def test_disabled_by_default_is_404(self, env, monkeypatch, method, url):
        monkeypatch.delenv("K7_API_ENABLE_REMOTE_INSTALL")
        resp = await self._call(method, url, json={"nodes": ["n1"]})
        assert resp.status_code == 404
        env.start.assert_not_called()
        env.status.assert_not_called()

    @pytest.mark.parametrize("method,url", [("POST", "/api/v1/install"), ("GET", f"/api/v1/install/{OP_ID}")])
    async def test_non_admin_is_403(self, env, method, url):
        resp = await self._call(method, url, admin=False, json={"nodes": ["n1"]})
        assert resp.status_code == 403
        env.start.assert_not_called()
        env.status.assert_not_called()

    @pytest.mark.parametrize("method,url", [("POST", "/api/v1/install"), ("GET", f"/api/v1/install/{OP_ID}")])
    @pytest.mark.parametrize("scope", [{"namespaces": ["team-a"]}, {"nodes": ["n1"]}])
    async def test_scoped_admin_is_403(self, env, method, url, scope):
        resp = await self._call(method, url, scope=scope, json={"nodes": ["n1"]})
        assert resp.status_code == 403 and "scoped" in resp.text, resp.text
        env.start.assert_not_called()
        env.status.assert_not_called()

    @pytest.mark.parametrize(
        "body",
        [
            {"nodes": ["n1"], "playbook": "- hosts: all"},
            {"nodes": ["n1"], "image": "ghcr.io/evil/k7-node-installer@sha256:" + "b" * 64},
            {"nodes": ["n1"], "inventory": "[k7_servers]\nh"},
            {"nodes": ["n1"], "config": {"allowServerRestart": True}},
            {"inventory": "x"},
            ["n1"],
        ],
    )
    async def test_anything_but_nodes_is_400(self, env, body):
        resp = await self._call("POST", "/api/v1/install", json=body)
        assert resp.status_code == 400, resp.text
        assert "fixed at deploy time" in resp.text
        env.start.assert_not_called()

    async def test_non_json_body_is_400(self, env):
        resp = await self._call("POST", "/api/v1/install", content=b"nodes=n1")
        assert resp.status_code == 400 and "JSON" in resp.text
        env.start.assert_not_called()

    async def test_admin_starts_an_operation_and_it_is_audited(self, env):
        from k7.api.main import audit_log

        handler = logging.handlers.BufferingHandler(capacity=100)
        env.start.side_effect = lambda v1, apps, op, nodes, subject: {"operation": op, "nodes": nodes}
        audit_log.addHandler(handler)
        try:
            resp = await self._call("POST", "/api/v1/install", json={"nodes": ["n1", "n2"]})
        finally:
            audit_log.removeHandler(handler)
        assert resp.status_code == 202, resp.text
        v1, apps, op, nodes, subject = env.start.await_args.args
        assert (v1, apps, nodes, subject) == ("v1", "apps", ["n1", "n2"], LEGACY_ID)
        assert re.fullmatch(r"[0-9a-f]{16}", op) and resp.json()["data"] == {"operation": op, "nodes": ["n1", "n2"]}
        (line,) = [json.loads(k7_logging.JsonFormatter().format(r)) for r in handler.buffer]
        assert line["nodes"] == ["n1", "n2"] and line["operation"] == op and line["status"] == 202
        assert line["route"] == "/api/v1/install" and line["role"] == "admin"

    async def test_refusals_keep_their_status_and_detail(self, env):
        env.start.side_effect = RemoteInstallError(409, "n1: k3s server node(s)")
        resp = await self._call("POST", "/api/v1/install", json={"nodes": ["n1"]})
        assert resp.status_code == 409 and "k3s server" in resp.text

    async def test_kubernetes_errors_are_a_loud_500_naming_the_operation(self, env):
        env.start.side_effect = ApiException(status=403, reason="Forbidden")
        resp = await self._call("POST", "/api/v1/install", json={"nodes": ["n1"]})
        op = env.start.await_args.args[2]
        assert resp.status_code == 500 and f"operation {op}" in resp.text and "403 Forbidden" in resp.text

    async def test_status_passes_through(self, env):
        env.status.return_value = {"operation": OP_ID, "state": "running"}
        resp = await self._call("GET", f"/api/v1/install/{OP_ID}")
        assert resp.status_code == 200 and resp.json()["data"]["state"] == "running"
        env.status.assert_awaited_once_with("v1", OP_ID)
        env.status.side_effect = RemoteInstallError(404, f"operation {OP_ID!r} not found")
        resp = await self._call("GET", f"/api/v1/install/{OP_ID}")
        assert resp.status_code == 404 and "not found" in resp.text


def test_sdk_install_sends_only_nodes_and_polls():
    from k7_sdk import Client

    client = Client(endpoint="http://k7.invalid", api_key="k")
    with patch.object(client.session, "post") as post, patch.object(client.session, "get") as get:
        post.return_value.json.return_value = {"data": {"operation": OP_ID}}
        get.return_value.json.return_value = {"data": {"state": "succeeded"}}
        assert client.install(["n1"]) == {"operation": OP_ID}
        assert client.install_status(OP_ID) == {"state": "succeeded"}
    assert post.call_args.kwargs["json"] == {"nodes": ["n1"]}
    assert get.call_args.args[0] == f"http://k7.invalid/api/v1/install/{OP_ID}"


# --- apply_node_scope ---


class TestApplyNodeScope:
    def test_unrestricted_key_keeps_caller_pin(self):
        assert apply_node_scope(_key(), None) is None
        assert apply_node_scope(_key(nodes=[]), "k7-node-01") == "k7-node-01"

    def test_single_node_key_auto_pins(self):
        assert apply_node_scope(_key(nodes=["k7-node-01"]), None) == "k7-node-01"

    def test_single_node_key_allows_listed_node(self):
        assert apply_node_scope(_key(nodes=["k7-node-01"]), "k7-node-01") == "k7-node-01"

    def test_scoped_key_denied_other_node(self):
        with pytest.raises(HTTPException) as exc:
            apply_node_scope(_key(nodes=["k7-node-01"]), "k7-node-02")
        assert exc.value.status_code == 403
        assert "k7-node-02" in str(exc.value.detail)

    def test_multi_node_key_requires_explicit_node(self):
        with pytest.raises(HTTPException) as exc:
            apply_node_scope(_key(nodes=["k7-node-01", "k7-node-02"]), None)
        assert exc.value.status_code == 403
        assert "explicit allowed node" in str(exc.value.detail)

    def test_multi_node_key_allows_listed_node(self):
        assert apply_node_scope(_key(nodes=["k7-node-01", "k7-node-02"]), "k7-node-02") == "k7-node-02"

    async def test_single_node_key_create_stamps_node_name(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts, nodes=["k7-node-01"])
        keys_file.write_text(json.dumps(data))

        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.create_sandbox = AsyncMock(return_value=OperationResult(success=True))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/sandboxes",
                    headers={"X-API-Key": TEST_KEY},
                    json={"name": "t", "image": "alpine"},
                )
        assert resp.status_code == 201, resp.text
        cfg = core_cls.return_value.create_sandbox.await_args.args[0]
        assert cfg.node_name == "k7-node-01"

    @pytest.mark.parametrize(("transient", "code"), [(True, 503), (False, 400)])
    async def test_create_failure_status(self, _patch_keys_file, keys_file: Path, transient: bool, code: int):
        keys_file.write_text(json.dumps(_make_keys_data(expires=int(time.time()) + 86400)))
        failed = OperationResult(success=False, error="Cannot resolve registry host ghcr.io", transient=transient)
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.create_sandbox = AsyncMock(return_value=failed)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/sandboxes", headers={"X-API-Key": TEST_KEY}, json={"name": "t", "image": "ghcr.io/x:1"}
                )
        assert resp.status_code == code, resp.text
        assert "Cannot resolve registry host ghcr.io" in resp.text

    async def test_scoped_key_create_other_node_returns_403(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts, nodes=["k7-node-01"])
        keys_file.write_text(json.dumps(data))

        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.create_sandbox = AsyncMock(return_value=OperationResult(success=True))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/sandboxes",
                    headers={"X-API-Key": TEST_KEY},
                    json={"name": "t", "image": "alpine", "node_name": "k7-node-02"},
                )
        assert resp.status_code == 403
        core_cls.return_value.create_sandbox.assert_not_called()

    async def test_node_scoped_key_nodes_storage_returns_403(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts, nodes=["k7-node-01"])
        keys_file.write_text(json.dumps(data))

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/nodes/storage",
                headers={"X-API-Key": TEST_KEY},
            )
        assert resp.status_code == 403
        assert "cluster-wide node" in resp.json()["error"]["message"]

    async def test_restore_stamps_node_name(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts, nodes=["k7-node-01"])
        keys_file.write_text(json.dumps(data))

        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.restore_sandbox = AsyncMock(return_value=OperationResult(success=True))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/snapshots/snap1/restore",
                    headers={"X-API-Key": TEST_KEY},
                    json={"new_sandbox_name": "restored"},
                )
        assert resp.status_code == 201, resp.text
        kwargs = core_cls.return_value.restore_sandbox.await_args.kwargs
        assert kwargs["node_name"] == "k7-node-01"

    async def test_fork_denied_when_source_on_other_node(self, _patch_keys_file, keys_file: Path):
        future_ts = int(time.time()) + 86400
        data = _make_keys_data(expires=future_ts, nodes=["k7-node-01"])
        keys_file.write_text(json.dumps(data))
        source = SimpleNamespace(name="src", node="k7-node-02")

        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.list_sandboxes = AsyncMock(return_value=[source])
            core_cls.return_value.fork_sandbox = AsyncMock(return_value=OperationResult(success=True))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/sandboxes/src/fork",
                    headers={"X-API-Key": TEST_KEY},
                    json={"new_name": "forked"},
                )
        assert resp.status_code == 403
        core_cls.return_value.fork_sandbox.assert_not_called()


# --- tenant keys ---

ACME = {"metadata": {"name": "acme"}, "spec": {"namespaces": ["acme-dev", "acme-prod"]}}
GLOBEX = {"metadata": {"name": "globex"}, "spec": {"namespaces": ["globex"]}}


def _routes(ns: str) -> list[tuple[str, str, dict | None, dict | None]]:
    """Every namespaced route: (method, path, query, body). Covers read, write and delete of
    sandboxes, snapshots, forks and restores."""
    return [
        ("GET", "/api/v1/sandboxes", {"namespace": ns}, None),
        ("GET", "/api/v1/sandboxes/sb", {"namespace": ns}, None),
        ("GET", "/api/v1/sandboxes/sb/logs", {"namespace": ns}, None),
        ("POST", "/api/v1/sandboxes/sb/exec", {"namespace": ns}, {"command": "id"}),
        ("POST", "/api/v1/sandboxes", None, {"name": "sb", "image": "alpine:3.20", "namespace": ns}),
        ("POST", "/api/v1/sandboxes/sb/pause", None, {"namespace": ns}),
        ("POST", "/api/v1/sandboxes/sb/pause", None, {"namespace": ns, "snapshot": "s1"}),
        ("POST", "/api/v1/sandboxes/sb/resume", None, {"namespace": ns}),
        ("POST", "/api/v1/sandboxes/sb/fork", None, {"new_name": "f1", "namespace": ns}),
        ("DELETE", "/api/v1/sandboxes/sb", {"namespace": ns}, None),
        ("DELETE", "/api/v1/sandboxes", {"namespace": ns}, None),
        ("GET", "/api/v1/snapshots", {"namespace": ns}, None),
        ("GET", "/api/v1/snapshots/s1", {"namespace": ns}, None),
        ("POST", "/api/v1/sandboxes/sb/snapshot", None, {"snapshot_name": "s1", "namespace": ns}),
        ("DELETE", "/api/v1/snapshots/s1", {"namespace": ns}, None),
        ("POST", "/api/v1/snapshots/s1/restore", None, {"new_sandbox_name": "r1", "namespace": ns}),
    ]


CLUSTER_ROUTES = [
    ("POST", "/api/v1/snapshots/gc", None, {"namespace": "acme-dev"}),
    ("GET", "/api/v1/snapshots", {"all_namespaces": "true"}, None),
    ("POST", "/api/v1/snapshots/gc", None, {"all_namespaces": True}),
    ("GET", "/api/v1/nodes/storage", None, None),
    ("GET", "/api/v1/sandboxes", None, None),
]
TENANT_READ_ROUTES = [
    ("GET", "/api/v1/tenants", None, None),
    ("GET", "/api/v1/tenants/globex", None, None),
]
TENANT_WRITE_ROUTES = [
    ("POST", "/api/v1/tenants", None, {"name": "x", "spec": {"namespaces": ["x"]}}),
    ("PATCH", "/api/v1/tenants/globex", None, {"quotas": None}),
    ("DELETE", "/api/v1/tenants/globex", {"cascade": "true"}, None),
]
TENANT_ADMIN_ROUTES = TENANT_READ_ROUTES + TENANT_WRITE_ROUTES
# Admin-only routes an unscoped key from before roles would reach as a migrated admin.
ADMIN_ONLY_ROUTES = [
    ("POST", "/api/v1/keys", None, {"name": "minted", "role": "admin"}),
    ("GET", "/api/v1/keys", None, None),
    ("PUT", "/api/v1/role-bindings", None, {"bindings": [], "deny": []}),
    ("POST", "/api/v1/install", None, {"inventory": "x"}),
    *TENANT_WRITE_ROUTES,
]


def _route_id(r) -> str:
    return f"{r[0]} {r[1]} {sorted((r[3] or {}).keys()) if r[3] else ''}"


class TestTenantKeys:
    @pytest.fixture(autouse=True)
    def _keys(self, monkeypatch):
        self.tokens: dict[str, str] = {}
        keys: dict = {}
        for label, extra in {
            "acme": {"role": "user", "tenant": "acme"},
            "acme-ro": {"role": "read-only", "tenant": "acme"},
            "acme-prod-only": {"role": "user", "tenant": "acme", "namespaces": ["acme-prod"]},
            "globex": {"role": "user", "tenant": "globex"},
            "admin": {"role": "admin"},
            "operator": {"role": "operator"},
            "unscoped": {"role": "admin", "unscoped_legacy": True},
            "legacy-ns": {"role": "user", "namespaces": ["acme-dev"]},
            "orphan": {"role": "user", "tenant": "gone"},
        }.items():
            token, key_id, digest = mint_token()
            keys[key_id] = {"name": label, "hash": digest, **extra}
            self.tokens[label] = token
        self.tenants = [ACME, GLOBEX]
        crs = AsyncMock(side_effect=lambda _core: list(self.tenants))
        monkeypatch.setattr("k7.api.main.STORE", _MemStore(keys))
        monkeypatch.setattr("k7.api.main.list_tenant_crs", crs)
        monkeypatch.setattr("k7.core.tenancy.list_tenants", crs)

    async def _call(self, who: str, route, core_cls=None) -> httpx.Response:
        method, path, params, body = route
        with patch("k7.api.main.K7Core") as cls:
            core = cls.return_value
            ok = OperationResult(success=True, message="ok")
            for m in (
                "create_sandbox",
                "delete_sandbox",
                "delete_all_sandboxes",
                "pause_sandbox",
                "resume_sandbox",
                "fork_sandbox",
                "create_snapshot",
                "delete_snapshot",
                "restore_sandbox",
                "gc_snapshots",
                "get_logs",
            ):
                setattr(core, m, AsyncMock(return_value=ok))
            core.list_sandboxes = AsyncMock(return_value=[])
            core.list_snapshots = AsyncMock(return_value=[])
            core.get_snapshot = AsyncMock(return_value=None)
            core.exec_command = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {}))
            core.nodes_storage = AsyncMock(return_value={})
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.request(
                    method, path, params=params, json=body, headers={"X-API-Key": self.tokens[who]}
                )
            self.core = core
            return resp

    def _core_untouched(self) -> None:
        assert [c for c in self.core.mock_calls if c[0] not in ("aclose",) and not c[0].startswith("__")] == []

    @pytest.mark.parametrize("route", _routes("globex"), ids=_route_id)
    async def test_cross_tenant_request_refused_before_core(self, route):
        resp = await self._call("acme", route)
        assert resp.status_code == 403, resp.text
        assert "globex" in resp.json()["error"]["message"]
        self._core_untouched()

    @pytest.mark.parametrize("route", _routes("acme-dev"), ids=_route_id)
    async def test_other_tenant_cannot_reach_acme(self, route):
        resp = await self._call("globex", route)
        assert resp.status_code == 403, resp.text
        self._core_untouched()

    @pytest.mark.parametrize("route", _routes("default"), ids=_route_id)
    async def test_tenant_key_refused_outside_any_tenant(self, route):
        resp = await self._call("acme", route)
        assert resp.status_code == 403, resp.text
        self._core_untouched()

    @pytest.mark.parametrize("ns", ["acme-dev", "acme-prod"])
    @pytest.mark.parametrize("route_index", range(len(_routes("x"))))
    async def test_tenant_key_reaches_every_own_namespace(self, ns, route_index):
        resp = await self._call("acme", _routes(ns)[route_index])
        assert resp.status_code != 403, resp.text
        assert self.core.admission.tenant is ACME and self.core.admission.admin is False

    async def test_narrowed_key_is_intersection(self):
        assert (await self._call("acme-prod-only", _routes("acme-prod")[0])).status_code == 200
        assert (await self._call("acme-prod-only", _routes("acme-dev")[0])).status_code == 403

    async def test_new_tenant_namespace_works_without_reissuing_key(self):
        route = _routes("acme-ci")[0]
        assert (await self._call("acme", route)).status_code == 403
        self.tenants = [{"metadata": {"name": "acme"}, "spec": {"namespaces": ["acme-dev", "acme-ci"]}}, GLOBEX]
        assert (await self._call("acme", route)).status_code == 200

    async def test_removed_tenant_namespace_is_refused_at_once(self):
        route = _routes("acme-prod")[0]
        assert (await self._call("acme", route)).status_code == 200
        self.tenants = [{"metadata": {"name": "acme"}, "spec": {"namespaces": ["acme-dev"]}}, GLOBEX]
        assert (await self._call("acme", route)).status_code == 403

    @pytest.mark.parametrize("route", CLUSTER_ROUTES, ids=_route_id)
    async def test_tenant_key_refused_cluster_wide(self, route):
        assert (await self._call("acme", route)).status_code == 403
        self._core_untouched()

    @pytest.mark.parametrize("who", ["acme", "acme-ro", "legacy-ns"])
    @pytest.mark.parametrize("route", TENANT_ADMIN_ROUTES, ids=_route_id)
    async def test_tenant_routes_refuse_tenant_roles(self, route, who):
        resp = await self._call(who, route)
        assert resp.status_code == 403 and "needs role" in resp.json()["error"]["message"], resp.text
        self._core_untouched()

    @pytest.mark.parametrize("route", TENANT_WRITE_ROUTES, ids=_route_id)
    async def test_operator_reads_tenants_but_cannot_manage_them(self, route, monkeypatch):
        monkeypatch.setattr("k7.core.tenancy.get_tenant", AsyncMock(return_value=GLOBEX))
        assert (await self._call("operator", TENANT_READ_ROUTES[0])).json()["data"] == [ACME, GLOBEX]
        assert (await self._call("operator", TENANT_READ_ROUTES[1])).json()["data"] == GLOBEX
        resp = await self._call("operator", route)
        assert resp.status_code == 403 and "needs role admin" in resp.json()["error"]["message"]

    def test_tenant_routes_are_in_the_permission_table(self):
        from k7.api.authz import PERMISSIONS

        assert {k: sorted(v or []) for k, v in PERMISSIONS.items() if k.endswith("_route") and "tenant" in k} == {
            "list_tenants_route": ["admin", "operator"],
            "get_tenant_route": ["admin", "operator"],
            "create_tenant_route": ["admin"],
            "update_tenant_route": ["admin"],
            "delete_tenant_route": ["admin"],
        }

    async def test_key_of_deleted_tenant_is_refused(self):
        for route in (_routes("acme-dev")[0], _routes("default")[4], CLUSTER_ROUTES[4]):
            resp = await self._call("orphan", route)
            assert resp.status_code == 403 and "tenant gone, which does not exist" in resp.text, resp.text
            self._core_untouched()

    async def test_double_claimed_namespace_is_500_naming_both(self):
        self.tenants = [ACME, GLOBEX, {"metadata": {"name": "initech"}, "spec": {"namespaces": ["globex"]}}]
        resp = await self._call("admin", _routes("globex")[0])
        assert resp.status_code == 500
        err = resp.json()["error"]
        assert err["code"] == "TenantConflict" and "globex and initech" in err["message"]
        self._core_untouched()

    async def test_admin_and_unscoped_dev_keys_reach_tenant_namespaces(self):
        for who in ("admin", "unscoped"):
            assert (await self._call(who, _routes("globex")[0])).status_code == 200
            assert self.core.admission.tenant is GLOBEX and self.core.admission.admin is True
        assert (await self._call("legacy-ns", _routes("acme-dev")[0])).status_code == 200
        assert (await self._call("legacy-ns", _routes("acme-prod")[0])).status_code == 403

    async def test_development_mode_lets_an_unscoped_legacy_key_act_as_admin(self):
        resp = await self._call("unscoped", ("GET", "/api/v1/auth/whoami", None, None))
        assert resp.status_code == 200 and resp.json()["data"]["grants"] == ["admin"]
        assert (await self._call("unscoped", CLUSTER_ROUTES[3])).status_code == 200

    @pytest.mark.parametrize("route", _routes("acme-dev")[:1] + CLUSTER_ROUTES[:1] + ADMIN_ONLY_ROUTES, ids=_route_id)
    async def test_production_refuses_unscoped_legacy_key_on_every_route(self, route, monkeypatch):
        monkeypatch.setenv("K7_API_MODE", "production")
        resp = await self._call("unscoped", route)
        msg = resp.json()["error"]["message"]
        assert resp.status_code == 403 and "production mode refuses" in msg and "unscoped_legacy" in msg, msg
        assert "--role user --tenant" in msg and "k7 revoke-api-key" in msg
        assert resp.headers["X-K7-Mode"] == "production"
        self._core_untouched()

    async def test_production_refuses_namespace_only_legacy_key(self, monkeypatch):
        monkeypatch.setenv("K7_API_MODE", "production")
        resp = await self._call("legacy-ns", _routes("acme-dev")[0])
        msg = resp.json()["error"]["message"]
        assert resp.status_code == 403 and "production mode refuses" in msg and "k7 tenant adopt" in msg
        assert (await self._call("acme", _routes("acme-dev")[0])).status_code == 200
        assert (await self._call("admin", CLUSTER_ROUTES[3])).status_code == 200

    async def test_quota_refusal_is_403_with_numbers_and_audited(self):
        from k7.api.main import audit_log
        from k7.core.tenancy import AdmissionError

        audit = logging.handlers.BufferingHandler(capacity=100)
        audit_log.addHandler(audit)

        details = {"tenant": "acme", "dimension": "sandboxes", "limit": "10", "usage": "10", "requested": "1"}
        route = _routes("acme-dev")[4]
        forbidden = metrics.API.get_sample_value("k7_auth_failures_total", {"reason": "forbidden"})
        try:
            with patch("k7.api.main.K7Core") as cls:
                cls.return_value.create_sandbox = AsyncMock(
                    side_effect=AdmissionError(403, "QuotaExceeded", "tenant acme sandboxes quota exceeded", details)
                )
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.post(route[1], json=route[3], headers={"X-API-Key": self.tokens["acme"]})
        finally:
            audit_log.removeHandler(audit)
        assert resp.status_code == 403
        assert resp.json()["error"] == {
            "code": "QuotaExceeded",
            "message": "tenant acme sandboxes quota exceeded",
            "details": details,
        }
        line = json.loads(k7_logging.JsonFormatter().format(audit.buffer[-1]))
        assert line["quota"] == details and line["tenant"] == "acme" and line["status"] == 403
        assert metrics.API.get_sample_value("k7_auth_failures_total", {"reason": "forbidden"}) == forbidden

    @pytest.mark.parametrize(("code", "status"), [("QuotaLockTimeout", 503), ("QuotaLockLost", 503)])
    async def test_lease_errors_are_503(self, code, status):
        from k7.core.tenancy import AdmissionError

        with patch("k7.api.main.K7Core") as cls:
            cls.return_value.fork_sandbox = AsyncMock(side_effect=AdmissionError(status, code, "retry"))
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/sandboxes/sb/fork",
                    json={"new_name": "f", "namespace": "acme-dev"},
                    headers={"X-API-Key": self.tokens["acme"]},
                )
        assert resp.status_code == status and resp.json()["error"]["code"] == code

    async def test_mode_header_and_health(self):
        resp = await self._call("acme", _routes("acme-dev")[0])
        assert resp.headers["X-K7-Mode"] == "development"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
        assert health.json()["mode"] == "development" and health.headers["X-K7-Mode"] == "development"


# --- SecretKeyStore background loop ---


class _Stop(Exception):
    pass


class TestSecretKeyStoreLoop:
    async def test_flush_without_usage_secret_is_loud_and_keeps_pending(self):
        store = SecretKeyStore("kube-system")
        store.touch("aaaaaaaa")
        core = SimpleNamespace(read_secret_data=AsyncMock(return_value=None), patch_secret_data=AsyncMock())
        with pytest.raises(RuntimeError, match="k7-api-keys-usage not found"):
            await store.flush(core)  # type: ignore[arg-type]
        core.patch_secret_data.assert_not_called()
        assert "aaaaaaaa" in store._used

    async def test_flush_with_nothing_used_reads_nothing(self):
        core = SimpleNamespace(read_secret_data=AsyncMock())
        await SecretKeyStore("kube-system").flush(core)  # type: ignore[arg-type]
        core.read_secret_data.assert_not_called()

    async def test_touch_during_flush_survives_it(self):
        store = SecretKeyStore("kube-system")
        store._used["aaaaaaaa"] = 100

        async def patch_secret_data(*_a):
            store._used["aaaaaaaa"] = 200
            store._used["bbbbbbbb"] = 200

        core = SimpleNamespace(read_secret_data=AsyncMock(return_value={}), patch_secret_data=patch_secret_data)
        await store.flush(core)  # type: ignore[arg-type]
        assert store._used == {"aaaaaaaa": 200, "bbbbbbbb": 200}

    async def test_step_logs_the_error_and_always_closes_the_client(self, caplog):
        store = SecretKeyStore("kube-system")
        with patch("k7.api.keystore.K7Core") as kc, patch("k7.api.keystore.aclose_k7_core", AsyncMock()) as close:
            assert await store._step(AsyncMock(side_effect=RuntimeError("apiserver down")), "sync") == "apiserver down"
            assert await store._step(AsyncMock(), "sync") is None
        assert close.await_count == 2 and all(c.args == (kc.return_value,) for c in close.await_args_list)
        assert "key store sync failed: apiserver down" in caplog.text

    async def test_run_syncs_every_poll_and_flushes_every_flush_interval(self, monkeypatch):
        from k7.api import keystore as ks

        store = SecretKeyStore("kube-system")
        now = [1000.0]
        sleeps: list[float] = []

        async def sleep(s):
            sleeps.append(s)
            if len(sleeps) == 3:
                raise _Stop
            now[0] += ks.FLUSH_SECONDS / 2

        steps: list[str] = []

        async def step(op, label):
            steps.append(label)
            return "flush failed" if label == "usage flush" else None

        monkeypatch.setattr(ks, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(ks.asyncio, "sleep", sleep)
        monkeypatch.setattr(store, "_step", step)
        with pytest.raises(_Stop):
            await store.run()
        assert steps == ["sync", "sync", "sync", "usage flush"]
        assert sleeps == [ks.POLL_SECONDS] * 3
        assert store.sync_error is None and store.flush_error == "flush failed"

    async def test_health_endpoint_shows_sync_and_flush_errors(self):
        store = SecretKeyStore("kube-system")
        store.sync_error, store.flush_error = "forbidden", "usage secret gone"
        with patch("k7.api.main.STORE", store):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["key_store"] == {
            "store": "secret",
            "synced_age_s": None,
            "sync_error": "forbidden",
            "usage_flush_error": "usage secret gone",
        }

    def test_unreadable_key_file_is_500(self, tmp_path: Path):
        (tmp_path / "keys").mkdir()
        with pytest.raises(HTTPException) as exc:
            FileKeyStore(tmp_path / "keys").snapshot()
        assert exc.value.status_code == 500 and "unreadable" in str(exc.value.detail)
