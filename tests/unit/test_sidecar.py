"""Unit tests for the sidecar framework: registry, spec, and core injection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig
from k7.core.sidecar import SIDECAR_REGISTRY, SidecarSpec
from tests.unit.conftest import mock_pod


def _setup_clients(core, apps=None, v1=None, net=None, metrics=None, custom=None):
    core._config_loaded = True
    if apps is not None:
        core._apps_v1_client = apps
    if v1 is not None:
        core._core_v1_client = v1
    if net is not None:
        core._networking_v1_client = net
    if metrics is not None:
        core._metrics_client = metrics
    if custom is not None:
        core._custom_objects_client = custom


# --- Registry tests ---


class TestSidecarRegistry:
    def test_registry_lookup_valid(self):
        spec = SIDECAR_REGISTRY["docker"]
        assert isinstance(spec, SidecarSpec)
        assert spec.image == "docker:27.5-dind"

    def test_registry_lookup_invalid(self):
        with pytest.raises(KeyError):
            SIDECAR_REGISTRY["nonexistent"]

    def test_sidecar_spec_fields(self):
        spec = SIDECAR_REGISTRY["docker"]
        assert isinstance(spec.image, str) and spec.image
        assert isinstance(spec.socket_mount, str) and spec.socket_mount
        assert isinstance(spec.socket_name, str) and spec.socket_name
        assert isinstance(spec.data_path, str) and spec.data_path
        assert isinstance(spec.pvc_subdir, str) and spec.pvc_subdir
        assert isinstance(spec.readiness_cmd, list) and len(spec.readiness_cmd) > 0
        assert isinstance(spec.privileged, bool)
        assert isinstance(spec.env, dict)
        assert isinstance(spec.args, list)


# --- create_sandbox with sidecar ---


class TestCreateSandboxWithSidecar:
    async def test_sidecar_adds_container_fd(self, core: K7Core):
        """Verify create_sandbox adds a sidecar container when config.sidecar is set (fd backend)."""
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net)

        cfg = SandboxConfig(
            name="sc-test",
            image="ubuntu:24.04",
            sidecar="docker",
            backend="kata-firecracker-devmapper",
        )

        with patch("os.path.exists", return_value=False):
            result = await core.create_sandbox(cfg)

        assert result.success is True, f"create failed: {result.error}"
        call_args = mock_apps.create_namespaced_deployment.call_args
        deployment = call_args[1]["body"] if "body" in call_args[1] else call_args[0][1]
        containers = deployment.spec.template.spec.containers
        assert len(containers) == 2
        names = [c.name for c in containers]
        assert "sandbox" in names
        assert "sidecar" in names

        sidecar_c = next(c for c in containers if c.name == "sidecar")
        assert sidecar_c.image == "docker:27.5-dind"
        assert sidecar_c.security_context.privileged is True

        volumes = deployment.spec.template.spec.volumes or []
        vol_names = [v.name for v in volumes]
        assert "sidecar-socket" in vol_names
        assert "sidecar-data" in vol_names

    async def test_no_sidecar_no_extra_container(self, core: K7Core):
        """Verify no sidecar when config.sidecar is None."""
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net)

        cfg = SandboxConfig(
            name="no-sc",
            image="ubuntu:24.04",
            backend="kata-firecracker-devmapper",
        )

        with patch("os.path.exists", return_value=False):
            result = await core.create_sandbox(cfg)

        assert result.success is True
        call_args = mock_apps.create_namespaced_deployment.call_args
        deployment = call_args[1]["body"] if "body" in call_args[1] else call_args[0][1]
        containers = deployment.spec.template.spec.containers
        assert len(containers) == 1
        assert containers[0].name == "sandbox"

    async def test_sidecar_ql_uses_pvc_subpath(self, core: K7Core):
        """Verify ql backend uses sub_path on the state-disk PVC for sidecar data."""
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = __import__(
            "kubernetes_asyncio.client.exceptions", fromlist=["ApiException"]
        ).ApiException(status=404)
        mock_net = AsyncMock()
        mock_v1.read_namespaced_config_map.side_effect = __import__(
            "kubernetes_asyncio.client.exceptions", fromlist=["ApiException"]
        ).ApiException(status=404)
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net)

        cfg = SandboxConfig(
            name="sc-ql",
            image="ubuntu:24.04",
            sidecar="docker",
            backend="kata-qemu-longhorn",
        )

        with (
            patch.object(core, "_load_persist_bind_script", return_value="#!/bin/sh\nexec $@"),
            patch.object(core, "_get_image_entrypoint_cmd", new_callable=AsyncMock, return_value=([], ["/bin/sh"])),
        ):
            result = await core.create_sandbox(cfg)

        assert result.success is True, f"create failed: {result.error}"
        call_args = mock_apps.create_namespaced_deployment.call_args
        deployment = call_args[1]["body"] if "body" in call_args[1] else call_args[0][1]
        containers = deployment.spec.template.spec.containers
        sidecar_c = next(c for c in containers if c.name == "sidecar")

        data_mount = next(m for m in sidecar_c.volume_mounts if m.mount_path == "/var/lib/docker")
        assert data_mount.name == "state-disk"
        assert data_mount.sub_path == "docker"

        volumes = deployment.spec.template.spec.volumes or []
        vol_names = [v.name for v in volumes]
        assert "sidecar-socket" in vol_names
        assert "sidecar-data" not in vol_names

    async def test_sidecar_annotation_set(self, core: K7Core):
        """Verify the sidecar type is annotated on the deployment."""
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net)

        cfg = SandboxConfig(
            name="sc-ann",
            image="ubuntu:24.04",
            sidecar="docker",
            backend="kata-firecracker-devmapper",
        )

        with patch("os.path.exists", return_value=False):
            result = await core.create_sandbox(cfg)

        assert result.success is True
        call_args = mock_apps.create_namespaced_deployment.call_args
        deployment = call_args[1]["body"] if "body" in call_args[1] else call_args[0][1]
        annotations = deployment.metadata.annotations or {}
        assert annotations.get("k7.katakate.org/sidecar") == "docker"


# --- _wait_for_pod_container_started with wait_all_containers ---


class TestWaitAllContainers:
    async def test_wait_all_returns_when_all_running(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(name="sc-pod", phase="Running")
        cs_sandbox = MagicMock()
        cs_sandbox.name = "sandbox"
        cs_sandbox.state = MagicMock()
        cs_sandbox.state.running = True
        cs_sidecar = MagicMock()
        cs_sidecar.name = "sidecar"
        cs_sidecar.state = MagicMock()
        cs_sidecar.state.running = True
        pod.status.container_statuses = [cs_sandbox, cs_sidecar]
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        _setup_clients(core, v1=mock_v1)

        result = await core._wait_for_pod_container_started(
            "sc-pod", "default", timeout_seconds=5, wait_all_containers=True
        )
        assert result == "sc-pod"

    async def test_wait_all_waits_for_sidecar(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod_partial = mock_pod(name="sc-pod", phase="Running")
        cs_sandbox = MagicMock()
        cs_sandbox.name = "sandbox"
        cs_sandbox.state = MagicMock()
        cs_sandbox.state.running = True
        cs_sidecar_not_ready = MagicMock()
        cs_sidecar_not_ready.name = "sidecar"
        cs_sidecar_not_ready.state = MagicMock()
        cs_sidecar_not_ready.state.running = None
        pod_partial.status.container_statuses = [cs_sandbox, cs_sidecar_not_ready]

        pod_ready = mock_pod(name="sc-pod", phase="Running")
        cs_sidecar_ready = MagicMock()
        cs_sidecar_ready.name = "sidecar"
        cs_sidecar_ready.state = MagicMock()
        cs_sidecar_ready.state.running = True
        pod_ready.status.container_statuses = [cs_sandbox, cs_sidecar_ready]

        mock_v1.list_namespaced_pod.side_effect = [
            MagicMock(items=[pod_partial]),
            MagicMock(items=[pod_ready]),
        ]
        _setup_clients(core, v1=mock_v1)

        with patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock):
            result = await core._wait_for_pod_container_started(
                "sc-pod", "default", timeout_seconds=60, wait_all_containers=True
            )
        assert result == "sc-pod"

    async def test_wait_all_false_only_checks_sandbox(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(name="sc-pod", phase="Running")
        cs_sandbox = MagicMock()
        cs_sandbox.name = "sandbox"
        cs_sandbox.state = MagicMock()
        cs_sandbox.state.running = True
        cs_sidecar = MagicMock()
        cs_sidecar.name = "sidecar"
        cs_sidecar.state = MagicMock()
        cs_sidecar.state.running = None
        pod.status.container_statuses = [cs_sandbox, cs_sidecar]
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        _setup_clients(core, v1=mock_v1)

        result = await core._wait_for_pod_container_started(
            "sc-pod", "default", timeout_seconds=5, wait_all_containers=False
        )
        assert result == "sc-pod"
