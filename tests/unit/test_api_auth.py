"""Unit tests for API authentication (mocked file I/O, no k8s)."""

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from k7.api.main import app, load_api_keys

TEST_KEY = "k7-test-secret-key-abc123"
TEST_KEY_HASH = hashlib.sha256(TEST_KEY.encode()).hexdigest()


def _make_keys_data(
    *,
    expires: int | None = None,
    last_used: int | None = None,
) -> dict:
    entry: dict = {"name": "test-key"}
    if expires is not None:
        entry["expires"] = expires
    if last_used is not None:
        entry["last_used"] = last_used
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
