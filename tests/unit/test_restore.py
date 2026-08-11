"""Unit tests for the Spec 10f restore-from-snapshot surface.

Coverage:

- ``_rehydrate_config_from_snapshot``: annotations supply defaults;
  overrides win; missing image without override is a loud error.
- ``_collect_source_annotations``: reads the source Deployment + PVC and
  returns the ``k7.io/source-*`` annotation dict that gets stamped onto
  pause/named snapshots so restore can rehydrate later.
- API restore endpoint: maps ``new_sandbox_name`` / overrides into
  ``K7Core.restore_sandbox`` and the response status codes
  (``201`` / ``400`` / ``404`` / ``409``).
- SDK ``Client.restore``: builds the right body and returns a
  ``SandboxProxy`` for the new sandbox.
"""

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from k7.api.main import app
from k7.core.core import K7Core
from k7.core.models import (
    SNAPSHOT_KIND_PAUSE,
    OperationResult,
    SandboxConfig,
    SandboxConfigOverrides,
)
from k7_sdk.client import Client, SandboxProxy

TEST_KEY = "k7-test-secret-key-spec10f"
TEST_KEY_HASH = hashlib.sha256(TEST_KEY.encode()).hexdigest()


# --- _rehydrate_config_from_snapshot ----------------------------------------


class TestRehydrateConfigFromSnapshot:
    def test_uses_annotations_when_no_override(self, core: K7Core):
        ann = {
            "k7.io/source-image": "python:3.12-slim",
            "k7.io/source-backend": "kata-qemu-longhorn",
            "k7.io/source-root-disk-size": "20Gi",
            "k7.io/source-sidecar": "docker",
            "k7.io/source-limits": json.dumps({"cpu": "2", "memory": "4Gi"}),
        }
        result = core._rehydrate_config_from_snapshot(
            annotations=ann, new_name="restored", namespace="default", overrides=None
        )
        assert result.success
        config: SandboxConfig = result.data
        assert config.image == "python:3.12-slim"
        assert config.backend == "kata-qemu-longhorn"
        assert config.root_disk_size == "20Gi"
        assert config.sidecar == "docker"
        assert config.limits == {"cpu": "2", "memory": "4Gi"}
        assert config.name == "restored"
        assert config.namespace == "default"

    def test_overrides_win_over_annotations(self, core: K7Core):
        ann = {
            "k7.io/source-image": "python:3.12-slim",
            "k7.io/source-backend": "kata-qemu-longhorn",
            "k7.io/source-root-disk-size": "10Gi",
        }
        overrides = SandboxConfigOverrides(
            image="alpine:3.20",
            root_disk_size="50Gi",
            limits={"memory": "16Gi"},
            cmd=["sleep", "infinity"],
        )
        result = core._rehydrate_config_from_snapshot(
            annotations=ann, new_name="r", namespace="default", overrides=overrides
        )
        assert result.success
        config = result.data
        assert config.image == "alpine:3.20"
        assert config.root_disk_size == "50Gi"
        assert config.limits == {"memory": "16Gi"}
        assert config.cmd == ["sleep", "infinity"]
        # Backend not overridden → from annotation.
        assert config.backend == "kata-qemu-longhorn"

    def test_missing_image_and_no_override_errors_helpfully(self, core: K7Core):
        ann = {"k7.io/source-backend": "kata-qemu-longhorn"}  # no image
        result = core._rehydrate_config_from_snapshot(
            annotations=ann, new_name="r", namespace="default", overrides=None
        )
        assert not result.success
        assert "k7.io/source-image" in result.error
        assert "--image" in result.error

    def test_image_override_when_annotation_missing(self, core: K7Core):
        ann = {}
        overrides = SandboxConfigOverrides(image="alpine:3.20")
        result = core._rehydrate_config_from_snapshot(
            annotations=ann, new_name="r", namespace="default", overrides=overrides
        )
        assert result.success
        assert result.data.image == "alpine:3.20"
        # Falls back to default backend when neither annotation nor override set.
        assert result.data.backend == "kata-qemu-longhorn"

    def test_malformed_limits_annotation_falls_back_to_none(self, core: K7Core):
        ann = {"k7.io/source-image": "alpine:3.20", "k7.io/source-limits": "not json"}
        result = core._rehydrate_config_from_snapshot(
            annotations=ann, new_name="r", namespace="default", overrides=None
        )
        assert result.success
        # SandboxConfig.__post_init__ normalises None → {} so we just check it didn't crash
        # and that the bad annotation didn't poison the limits dict.
        assert result.data.limits in (None, {})


# --- _collect_source_annotations -------------------------------------------


class TestCollectSourceAnnotations:
    async def test_reads_image_backend_sidecar_limits_pvc_size(self, core: K7Core):
        # Build a fake Deployment + PVC reachable through the apps_v1 / v1 clients.
        deployment = MagicMock()
        deployment.metadata = MagicMock()
        deployment.metadata.annotations = {
            "k7.katakate.org/backend": "kata-qemu-longhorn",
            "k7.katakate.org/sidecar": "docker",
            "k7.katakate.org/root-pvc-name": "demo-root-lh",
        }
        sandbox_container = MagicMock()
        sandbox_container.name = "sandbox"
        sandbox_container.image = "python:3.12-slim"
        sandbox_container.resources = MagicMock()
        sandbox_container.resources.limits = {"cpu": "2", "memory": "4Gi", "ephemeral-storage": "10Gi"}
        deployment.spec = MagicMock()
        deployment.spec.template = MagicMock()
        deployment.spec.template.spec = MagicMock()
        deployment.spec.template.spec.containers = [sandbox_container]

        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = deployment

        pvc = MagicMock()
        pvc.spec = MagicMock()
        pvc.spec.resources = MagicMock()
        pvc.spec.resources.requests = {"storage": "20Gi"}
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc

        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._config_loaded = True

        ann = await core._collect_source_annotations("demo", "default")
        assert ann["k7.io/source-image"] == "python:3.12-slim"
        assert ann["k7.io/source-backend"] == "kata-qemu-longhorn"
        assert ann["k7.io/source-sidecar"] == "docker"
        assert ann["k7.io/source-root-disk-size"] == "20Gi"
        limits = json.loads(ann["k7.io/source-limits"])
        assert limits == {"cpu": "2", "memory": "4Gi", "ephemeral-storage": "10Gi"}

    async def test_missing_deployment_returns_empty_dict(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.side_effect = Exception("404")
        core._apps_v1_client = mock_apps
        core._config_loaded = True
        ann = await core._collect_source_annotations("ghost", "default")
        assert ann == {}


# --- API: POST /api/v1/snapshots/{name}/restore -----------------------------


@pytest.fixture()
def keys_file(tmp_path: Path):
    p = tmp_path / "api_keys.json"
    future_ts = int(time.time()) + 86400
    p.write_text(json.dumps({TEST_KEY_HASH: {"name": "test-key", "expires": future_ts, "last_used": None}}))
    with patch("k7.api.main.API_KEYS_FILE", p):
        yield p


async def _post(client, path, body):
    return await client.post(path, json=body, headers={"X-API-Key": TEST_KEY})


class TestRestoreApiEndpoint:
    async def test_success_returns_201_with_location(self, keys_file):
        fake = AsyncMock(
            return_value=OperationResult(
                success=True,
                message="Sandbox demo-r restored from snapshot demo-paused-1",
                data={"source_snapshot": "demo-paused-1"},
            )
        )
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.restore_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(
                    client,
                    "/api/v1/snapshots/demo-paused-1/restore",
                    {
                        "new_sandbox_name": "demo-r",
                        "namespace": "ns",
                        "overrides": {"image": "alpine:3.20", "root_disk_size": "20Gi"},
                        "keep_snapshot": False,
                    },
                )
        assert resp.status_code == 201
        body = resp.json()["data"]
        assert body["name"] == "demo-r"
        assert body["namespace"] == "ns"
        assert body["source_snapshot"] == "demo-paused-1"
        assert resp.headers["location"] == "/api/v1/sandboxes/demo-r?namespace=ns"
        fake.assert_awaited_once()
        kwargs = fake.await_args.kwargs
        assert kwargs["snapshot_name"] == "demo-paused-1"
        assert kwargs["new_sandbox_name"] == "demo-r"
        assert kwargs["namespace"] == "ns"
        assert kwargs["keep_snapshot"] is False
        overrides: SandboxConfigOverrides = kwargs["overrides"]
        assert overrides.image == "alpine:3.20"
        assert overrides.root_disk_size == "20Gi"

    async def test_missing_new_sandbox_name_returns_400(self, keys_file):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await _post(client, "/api/v1/snapshots/snap/restore", {})
        assert resp.status_code == 400
        assert "new_sandbox_name is required" in resp.json()["error"]["message"]

    async def test_snapshot_not_found_returns_404(self, keys_file):
        fake = AsyncMock(
            return_value=OperationResult(success=False, error="VolumeSnapshot ghost not found in namespace default")
        )
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.restore_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/snapshots/ghost/restore", {"new_sandbox_name": "r"})
        assert resp.status_code == 404

    async def test_target_already_exists_returns_409(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=False, error="Sandbox demo-r already exists"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.restore_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(client, "/api/v1/snapshots/s/restore", {"new_sandbox_name": "demo-r"})
        assert resp.status_code == 409

    async def test_overrides_filtered_to_allowed_keys(self, keys_file):
        fake = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        with patch("k7.api.main.K7Core") as core_cls:
            core_cls.return_value.restore_sandbox = fake
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await _post(
                    client,
                    "/api/v1/snapshots/s/restore",
                    {
                        "new_sandbox_name": "r",
                        "overrides": {
                            "image": "alpine:3.20",
                            "bogus_field": "ignored",
                        },
                    },
                )
        assert resp.status_code == 201
        overrides: SandboxConfigOverrides = fake.await_args.kwargs["overrides"]
        assert overrides.image == "alpine:3.20"
        # bogus_field must not have leaked into the dataclass.
        assert not hasattr(overrides, "bogus_field")


# --- SDK Client.restore -----------------------------------------------------


def _mock_response(payload: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class TestSdkClientRestore:
    def test_returns_proxy_and_posts_correct_body(self):
        c = Client(endpoint="http://api.test", api_key="k")
        with patch.object(
            c.session,
            "post",
            return_value=_mock_response({"data": {"name": "child", "source_snapshot": "snap"}}, 201),
        ) as post:
            proxy = c.restore(
                "snap",
                "child",
                namespace="ns",
                overrides={"image": "alpine:3.20"},
                keep_snapshot=False,
            )
        assert isinstance(proxy, SandboxProxy)
        assert proxy.name == "child"
        assert proxy.namespace == "ns"
        assert proxy._client is c
        assert post.call_args[0][0] == "http://api.test/api/v1/snapshots/snap/restore"
        body = post.call_args[1]["json"]
        assert body == {
            "new_sandbox_name": "child",
            "namespace": "ns",
            "keep_snapshot": False,
            "overrides": {"image": "alpine:3.20"},
        }

    def test_omits_overrides_when_none(self):
        c = Client(endpoint="http://api.test", api_key="k")
        with patch.object(c.session, "post", return_value=_mock_response({"data": {"name": "child"}}, 201)) as post:
            c.restore("snap", "child")
        body = post.call_args[1]["json"]
        assert "overrides" not in body
        assert body["keep_snapshot"] is True


# --- Snapshot creation stamps source annotations ---------------------------


class TestCreateVolumeSnapshotStampsAnnotations:
    async def test_named_snapshot_calls_collect_source_annotations(self, core: K7Core):
        mock_custom = AsyncMock()
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        collected = {
            "k7.io/source-image": "python:3.12-slim",
            "k7.io/source-backend": "kata-qemu-longhorn",
        }
        with patch.object(
            core,
            "_collect_source_annotations",
            new_callable=AsyncMock,
            return_value=collected,
        ) as collect:
            result = await core._create_volume_snapshot(
                pvc_name="demo-root-lh",
                snapshot_name="demo-v1",
                snapshot_class="longhorn",
                namespace="default",
                kind=SNAPSHOT_KIND_PAUSE,
                source_sandbox="demo",
            )
        assert result.success
        collect.assert_awaited_once_with("demo", "default")
        mock_custom.create_namespaced_custom_object.assert_called_once()
        body = mock_custom.create_namespaced_custom_object.call_args.kwargs["body"]
        ann = body["metadata"]["annotations"]
        assert ann["k7.io/kind"] == SNAPSHOT_KIND_PAUSE
        assert ann["k7.io/source-sandbox"] == "demo"
        assert ann["k7.io/source-image"] == "python:3.12-slim"
        assert ann["k7.io/source-backend"] == "kata-qemu-longhorn"

    async def test_fork_snapshot_does_not_collect_source_annotations(self, core: K7Core):
        """Fork-temp snapshots are auto-deleted; no point in the extra K8s read."""
        from k7.core.models import SNAPSHOT_KIND_FORK

        mock_custom = AsyncMock()
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        with patch.object(core, "_collect_source_annotations", new_callable=AsyncMock, return_value={}) as collect:
            result = await core._create_volume_snapshot(
                pvc_name="demo-root-lh",
                snapshot_name="demo-fork-1",
                snapshot_class="longhorn",
                namespace="default",
                kind=SNAPSHOT_KIND_FORK,
                source_sandbox="demo",
            )
        assert result.success
        collect.assert_not_awaited()
