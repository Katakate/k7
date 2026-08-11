"""Unit tests for the pause/resume/fork API endpoints (Spec 10a).

Hits the FastAPI ASGI app directly with a mocked K7Core, so these tests
need no Kubernetes cluster. They verify request/response shape, body
forwarding into K7Core, and HTTP status mapping for fork errors.
"""

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from k7.api.main import app
from k7.core.models import OperationResult

TEST_KEY = "k7-test-secret-key-spec10a"
TEST_KEY_HASH = hashlib.sha256(TEST_KEY.encode()).hexdigest()


@pytest.fixture()
def keys_file(tmp_path: Path):
    p = tmp_path / "api_keys.json"
    future_ts = int(time.time()) + 86400
    p.write_text(json.dumps({TEST_KEY_HASH: {"name": "test-key", "expires": future_ts, "last_used": None}}))
    with patch("k7.api.main.API_KEYS_FILE", p):
        yield p


async def _post(client, path, body=None, headers=None):
    return await client.post(path, json=body, headers=headers or {"X-API-Key": TEST_KEY})


# --- /pause -----------------------------------------------------------------


class TestPauseEndpoint:
    async def test_pause_forwards_body_to_core(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=True, message="Sandbox demo paused (replicas=0)."))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.pause_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(
                    client,
                    "/api/v1/sandboxes/demo/pause",
                    {"namespace": "ns", "snapshot": "snap1"},
                )
        assert resp.status_code == 200
        assert resp.json() == {"data": {"message": "Sandbox demo paused (replicas=0)."}}
        fake.assert_awaited_once_with(name="demo", namespace="ns", snapshot_name="snap1")

    async def test_pause_empty_body_defaults(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.pause_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/sandboxes/demo/pause", {})
        assert resp.status_code == 200
        fake.assert_awaited_once_with(name="demo", namespace="default", snapshot_name=None)

    async def test_pause_ignores_legacy_pvc_and_snapshot_class(self, keys_file):
        """Spec 10c: pvc/snapshot_class in the body must NOT be forwarded to Core.

        We accept them silently for one release rather than 400ing — but the
        server-side behaviour is exactly as if they weren't sent.
        """
        fake = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.pause_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(
                    client,
                    "/api/v1/sandboxes/demo/pause",
                    {"snapshot": "snap1", "pvc": "ignored", "snapshot_class": "ignored"},
                )
        assert resp.status_code == 200
        fake.assert_awaited_once_with(name="demo", namespace="default", snapshot_name="snap1")

    async def test_pause_core_failure_returns_400(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=False, error="boom"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.pause_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/sandboxes/demo/pause", {})
        assert resp.status_code == 400
        assert resp.json()["error"]["message"] == "boom"

    async def test_pause_requires_auth(self, keys_file):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/sandboxes/demo/pause", json={})
        assert resp.status_code == 401


# --- /resume ----------------------------------------------------------------


class TestResumeEndpoint:
    async def test_resume_forwards_namespace(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=True, message="Sandbox demo resumed (replicas=1)."))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.resume_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/sandboxes/demo/resume", {"namespace": "ns"})
        assert resp.status_code == 200
        fake.assert_awaited_once_with(name="demo", namespace="ns")

    async def test_resume_empty_body_uses_default_namespace(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.resume_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/sandboxes/demo/resume", None)
        assert resp.status_code == 200
        fake.assert_awaited_once_with(name="demo", namespace="default")


# --- /fork ------------------------------------------------------------------


class TestForkEndpoint:
    async def test_fork_success_returns_201(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=True, message="Forked demo -> child in 1.0s"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.fork_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(
                    client,
                    "/api/v1/sandboxes/demo/fork",
                    {"new_name": "child", "namespace": "ns", "snapshot": "snap"},
                )
        assert resp.status_code == 201
        body = resp.json()["data"]
        assert body["name"] == "child"
        assert body["namespace"] == "ns"
        assert body["source"] == "demo"
        assert "Forked" in body["message"]
        assert resp.headers["location"] == "/api/v1/sandboxes/child?namespace=ns"
        fake.assert_awaited_once_with(
            source_name="demo",
            new_name="child",
            namespace="ns",
            snapshot_name="snap",
        )

    async def test_fork_missing_new_name_returns_400(self, keys_file):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await _post(client, "/api/v1/sandboxes/demo/fork", {})
        assert resp.status_code == 400
        assert "new_name is required" in resp.json()["error"]["message"]

    async def test_fork_conflict_returns_409(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=False, error="Sandbox child already exists"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.fork_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(
                    client,
                    "/api/v1/sandboxes/demo/fork",
                    {"new_name": "child"},
                )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]["message"]

    async def test_fork_source_not_found_returns_404(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=False, error="Source root PVC demo-root-lh not found"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.fork_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/sandboxes/demo/fork", {"new_name": "child"})
        assert resp.status_code == 404

    async def test_fork_other_error_returns_400(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=False, error="some other failure"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.fork_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/sandboxes/demo/fork", {"new_name": "child"})
        assert resp.status_code == 400
