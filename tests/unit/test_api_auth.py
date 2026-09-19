"""Unit tests for API authentication (mocked file I/O, no k8s)."""

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from k7.api.main import app, apply_node_scope, authorize_namespace, load_api_keys
from k7.core.models import OperationResult

TEST_KEY = "k7-test-secret-key-abc123"
TEST_KEY_HASH = hashlib.sha256(TEST_KEY.encode()).hexdigest()


def _make_keys_data(
    *,
    expires: int | None = None,
    last_used: int | None = None,
    namespaces: list[str] | None = None,
    nodes: list[str] | None = None,
) -> dict:
    entry: dict = {"name": "test-key"}
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
    with patch("k7.api.main.API_KEYS_FILE", keys_file):
        yield


# --- load_api_keys ---


class TestLoadApiKeys:
    def test_missing_file(self, _patch_keys_file, keys_file: Path):
        assert load_api_keys() == {}

    def test_empty_file_fails_loud(self, _patch_keys_file, keys_file: Path):
        # An existing-but-unparseable store is a deployment bug: it must be
        # a loud 500, not a silent {} that rejects every key as "invalid".
        from fastapi import HTTPException

        keys_file.write_text("")
        with pytest.raises(HTTPException) as exc_info:
            load_api_keys()
        assert exc_info.value.status_code == 500
        assert "corrupt" in exc_info.value.detail

    def test_valid_json(self, _patch_keys_file, keys_file: Path):
        data = _make_keys_data()
        keys_file.write_text(json.dumps(data))
        result = load_api_keys()
        assert TEST_KEY_HASH in result
        assert result[TEST_KEY_HASH]["name"] == "test-key"

    def test_expired_keys_purged(self, _patch_keys_file, keys_file: Path):
        expired_ts = int(time.time()) - 3600
        data = _make_keys_data(expires=expired_ts)
        keys_file.write_text(json.dumps(data))
        result = load_api_keys()
        assert TEST_KEY_HASH not in result


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


class TestAuthorizeNamespace:
    def test_unrestricted_key_allowed_everywhere(self):
        authorize_namespace({"name": "u"}, "alpha")
        authorize_namespace({"name": "u", "namespaces": []}, "beta")
        authorize_namespace({"name": "u"}, None, all_namespaces=True)

    def test_scoped_key_allowed_in_listed_namespace(self):
        authorize_namespace({"namespaces": ["alpha", "gamma"]}, "alpha")

    def test_scoped_key_denied_other_namespace(self):
        with pytest.raises(HTTPException) as exc:
            authorize_namespace({"namespaces": ["alpha"]}, "beta")
        assert exc.value.status_code == 403

    def test_scoped_key_denied_all_namespaces(self):
        with pytest.raises(HTTPException) as exc:
            authorize_namespace({"namespaces": ["alpha"]}, "alpha", all_namespaces=True)
        assert exc.value.status_code == 403

    def test_scoped_key_denied_implicit_all(self):
        with pytest.raises(HTTPException) as exc:
            authorize_namespace({"namespaces": ["alpha"]}, None)
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


# --- apply_node_scope ---


class TestApplyNodeScope:
    def test_unrestricted_key_keeps_caller_pin(self):
        assert apply_node_scope({"name": "u"}, None) is None
        assert apply_node_scope({"name": "u", "nodes": []}, "k7-node-01") == "k7-node-01"

    def test_single_node_key_auto_pins(self):
        assert apply_node_scope({"nodes": ["k7-node-01"]}, None) == "k7-node-01"

    def test_single_node_key_allows_listed_node(self):
        assert apply_node_scope({"nodes": ["k7-node-01"]}, "k7-node-01") == "k7-node-01"

    def test_scoped_key_denied_other_node(self):
        with pytest.raises(HTTPException) as exc:
            apply_node_scope({"nodes": ["k7-node-01"]}, "k7-node-02")
        assert exc.value.status_code == 403
        assert "k7-node-02" in str(exc.value.detail)

    def test_multi_node_key_requires_explicit_node(self):
        with pytest.raises(HTTPException) as exc:
            apply_node_scope({"nodes": ["k7-node-01", "k7-node-02"]}, None)
        assert exc.value.status_code == 403
        assert "explicit allowed node" in str(exc.value.detail)

    def test_multi_node_key_allows_listed_node(self):
        assert apply_node_scope({"nodes": ["k7-node-01", "k7-node-02"]}, "k7-node-02") == "k7-node-02"

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
