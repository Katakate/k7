"""Unit tests for K7Core methods that use async K8s API or HTTP (all mocked)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7.core.core import K7Core

# --- _parse_image_reference ---


class TestParseImageReference:
    def test_simple_dockerhub(self, core: K7Core):
        reg, repo, tag = core._parse_image_reference("alpine:3.21")
        assert reg == "registry-1.docker.io"
        assert repo == "library/alpine"
        assert tag == "3.21"

    def test_dockerhub_no_tag(self, core: K7Core):
        reg, repo, tag = core._parse_image_reference("alpine")
        assert reg == "registry-1.docker.io"
        assert repo == "library/alpine"
        assert tag == "latest"

    def test_dockerhub_user_repo(self, core: K7Core):
        reg, repo, tag = core._parse_image_reference("katakate/k7-api:0.0.1")
        assert reg == "registry-1.docker.io"
        assert repo == "katakate/k7-api"
        assert tag == "0.0.1"

    def test_ghcr(self, core: K7Core):
        reg, repo, tag = core._parse_image_reference("ghcr.io/katakate/k7-api:v1")
        assert reg == "ghcr.io"
        assert repo == "katakate/k7-api"
        assert tag == "v1"

    def test_digest(self, core: K7Core):
        reg, repo, tag = core._parse_image_reference("alpine@sha256:abc123")
        assert reg == "registry-1.docker.io"
        assert repo == "library/alpine"
        assert tag == "sha256:abc123"

    def test_registry_with_port(self, core: K7Core):
        reg, repo, tag = core._parse_image_reference("localhost:5000/myimage:dev")
        assert reg == "localhost:5000"
        assert repo == "myimage"
        assert tag == "dev"

    def test_quay(self, core: K7Core):
        reg, repo, tag = core._parse_image_reference("quay.io/prometheus/node-exporter:v1.8.0")
        assert reg == "quay.io"
        assert repo == "prometheus/node-exporter"
        assert tag == "v1.8.0"


# --- _get_image_entrypoint_cmd (registry-based) ---


class TestGetImageEntrypointCmd:
    def _mock_registry_config(self, entrypoint=None, cmd=None):
        return {"config": {"Entrypoint": entrypoint, "Cmd": cmd}}

    async def test_returns_entrypoint_and_cmd(self, core: K7Core):
        config = self._mock_registry_config(["/init"], ["run"])
        with patch.object(core, "_get_registry_image_config", new_callable=AsyncMock, return_value=config):
            ep, cmd = await core._get_image_entrypoint_cmd("alpine:3.18")
        assert ep == ["/init"]
        assert cmd == ["run"]

    async def test_entrypoint_only(self, core: K7Core):
        config = self._mock_registry_config(["/start"], None)
        with patch.object(core, "_get_registry_image_config", new_callable=AsyncMock, return_value=config):
            ep, cmd = await core._get_image_entrypoint_cmd("img:1")
        assert ep == ["/start"]
        assert cmd == []

    async def test_cmd_only(self, core: K7Core):
        config = self._mock_registry_config(None, ["/bin/sh"])
        with patch.object(core, "_get_registry_image_config", new_callable=AsyncMock, return_value=config):
            ep, cmd = await core._get_image_entrypoint_cmd("img:1")
        assert ep == []
        assert cmd == ["/bin/sh"]

    async def test_registry_failure_returns_empty(self, core: K7Core):
        with patch.object(core, "_get_registry_image_config", new_callable=AsyncMock, side_effect=Exception("network")):
            ep, cmd = await core._get_image_entrypoint_cmd("img:1")
        assert ep == []
        assert cmd == []

    async def test_empty_image_string(self, core: K7Core):
        ep, cmd = await core._get_image_entrypoint_cmd("")
        assert ep == []
        assert cmd == []


# --- _create_volume_snapshot ---


class TestCreateVolumeSnapshot:
    async def test_success(self, core: K7Core):
        mock_custom = AsyncMock()
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        result = await core._create_volume_snapshot("pvc1", "snap1")

        assert result.success is True
        assert "snap1" in result.message
        mock_custom.create_namespaced_custom_object.assert_called_once()

    async def test_failure(self, core: K7Core):
        from kubernetes_asyncio.client.exceptions import ApiException

        mock_custom = AsyncMock()
        mock_custom.create_namespaced_custom_object.side_effect = ApiException(status=500, reason="connection refused")
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        result = await core._create_volume_snapshot("pvc1", "snap1")

        assert result.success is False
        assert "connection refused" in result.error


# --- _wait_for_snapshot_ready ---


class TestWaitForSnapshotReady:
    async def test_immediately_ready(self, core: K7Core):
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.return_value = {"status": {"readyToUse": True}}
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        result = await core._wait_for_snapshot_ready("snap1", timeout=5)

        assert result.success is True

    async def test_ready_after_retry(self, core: K7Core):
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.side_effect = [
            {"status": {"readyToUse": False}},
            {"status": {"readyToUse": True}},
        ]
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        with patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock):
            result = await core._wait_for_snapshot_ready("snap1", timeout=60)

        assert result.success is True

    async def test_timeout(self, core: K7Core):
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.return_value = {"status": {"readyToUse": False}}
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        elapsed = 0.0

        def advancing_time():
            nonlocal elapsed
            elapsed += 1.0
            return elapsed

        with (
            patch("k7.core.core.time.time", side_effect=advancing_time),
            patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await core._wait_for_snapshot_ready("snap1", timeout=2)

        assert result.success is False
        assert "not ready" in result.error


# --- _get_registry_image_config ---


def _mock_httpx_response(data: dict):
    """Build a MagicMock that behaves like an httpx.Response (sync .json()/.raise_for_status())."""
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


def _patch_httpx(get_fn):
    """Context manager that patches httpx.AsyncClient with a mock whose .get is `get_fn`."""
    p = patch("k7.core.core.httpx.AsyncClient")
    mock_cls = p.start()
    mock_client = AsyncMock()
    mock_client.get = get_fn
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return p


class TestGetRegistryImageConfig:
    @pytest.fixture(autouse=True)
    def _public_dns(self, monkeypatch):
        """Keep SSRF allowlist checks offline-friendly in unit tests."""
        monkeypatch.setattr(
            "k7.core.core.socket.getaddrinfo",
            lambda *a, **k: [(None, None, None, None, ("1.1.1.1", 0))],
        )

    async def test_simple_manifest(self, core: K7Core):
        """Non-Docker-Hub registry with a direct manifest (not a list)."""
        manifest = {
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"digest": "sha256:cfgabc"},
        }
        image_config = {"config": {"Entrypoint": ["/start"]}}

        async def mock_get(url, **kwargs):
            if "/manifests/" in url:
                return _mock_httpx_response(manifest)
            return _mock_httpx_response(image_config)

        p = _patch_httpx(mock_get)
        try:
            result = await core._get_registry_image_config("ghcr.io/org/repo:v1")
        finally:
            p.stop()

        assert result == image_config

    async def test_dockerhub_token_auth(self, core: K7Core):
        """Docker Hub images trigger a token fetch."""
        token_resp_data = {"token": "test-token"}
        manifest = {
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"digest": "sha256:cfgabc"},
        }
        image_config = {"config": {"Cmd": ["/bin/sh"]}}
        calls: list[str] = []

        async def mock_get(url, **kwargs):
            calls.append(url)
            if "auth.docker.io" in url:
                return _mock_httpx_response(token_resp_data)
            if "/manifests/" in url:
                return _mock_httpx_response(manifest)
            return _mock_httpx_response(image_config)

        p = _patch_httpx(mock_get)
        try:
            result = await core._get_registry_image_config("alpine:3.21")
        finally:
            p.stop()

        assert result == image_config
        assert any("auth.docker.io" in c for c in calls)

    async def test_manifest_list_picks_amd64(self, core: K7Core):
        """Manifest index resolves to linux/amd64 sub-manifest."""
        index = {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
            ],
        }
        inner_manifest = {
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"digest": "sha256:cfgabc"},
        }
        image_config = {"config": {"Entrypoint": ["/ep"]}}

        async def mock_get(url, **kwargs):
            if "/manifests/latest" in url:
                return _mock_httpx_response(index)
            if "/manifests/sha256:amd" in url:
                return _mock_httpx_response(inner_manifest)
            return _mock_httpx_response(image_config)

        p = _patch_httpx(mock_get)
        try:
            result = await core._get_registry_image_config("ghcr.io/org/repo:latest")
        finally:
            p.stop()

        assert result == image_config

    async def test_no_amd64_raises(self, core: K7Core):
        """Manifest index with no linux/amd64 raises ValueError."""
        index = {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            ],
        }

        async def mock_get(url, **kwargs):
            return _mock_httpx_response(index)

        import pytest as _pytest

        p = _patch_httpx(mock_get)
        try:
            with _pytest.raises(ValueError, match="No linux/amd64"):
                await core._get_registry_image_config("ghcr.io/org/repo:latest")
        finally:
            p.stop()

    async def test_no_config_digest_raises(self, core: K7Core):
        """Manifest with no config digest raises ValueError."""
        manifest = {
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {},
        }

        async def mock_get(url, **kwargs):
            return _mock_httpx_response(manifest)

        import pytest as _pytest

        p = _patch_httpx(mock_get)
        try:
            with _pytest.raises(ValueError, match="No config digest"):
                await core._get_registry_image_config("ghcr.io/org/repo:v1")
        finally:
            p.stop()
