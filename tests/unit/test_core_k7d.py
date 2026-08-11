"""Unit tests for the k7d backend paths in K7Core (spec 9a M11).

Everything here is mocked — the daemon socket, crictl, and the k8s API
are never touched. The real end-to-end behaviour is covered by
``tests/integration/test_k7d.py`` on a k7d-capable node.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7.core.core import (
    K7D_ANN_FORK_SOURCE_CLUSTER,
    K7D_ANN_FORK_SOURCE_VM,
    K7D_ANN_PAUSED,
    K7Core,
)
from k7.core.models import SandboxConfig
from tests.unit.conftest import mock_deployment, mock_pod


def _local_node(core: K7Core):
    """Patch the spec-18g node resolution so the sandbox appears LOCAL —
    these tests exercise the direct daemon-socket path, not forwarding."""
    return patch.object(core, "_k7d_sandbox_node", new=AsyncMock(return_value=core._k7d_local_node()))


def _k7d_deployment(name: str = "src-sb", sidecar: str | None = None):
    annotations = {"k7.katakate.org/backend": "k7d"}
    if sidecar:
        annotations["k7.katakate.org/sidecar"] = sidecar
    dep = mock_deployment(name=name, runtime_class="k7", annotations=annotations)
    dep.spec.template.metadata.annotations = dict(annotations)
    dep.spec.template.metadata.labels = {"app": name, "katakate.org/sandbox": name}
    dep.metadata.labels = {"app": name, "runtime": "kata", "katakate.org/sandbox": name}
    dep.spec.selector.match_labels = {"app": name}
    return dep


# --- backend canonicalization / detection ---


class TestK7dBackendDetection:
    def test_k7_alias_canonicalizes_to_k7d(self):
        assert K7Core._canonicalize_backend("k7") == "k7d"

    def test_k7d_passes_through(self):
        assert K7Core._canonicalize_backend("k7d") == "k7d"

    async def test_detect_backend_accepts_k7d_annotation(self, core: K7Core):
        dep = mock_deployment(annotations={"k7.katakate.org/backend": "k7d"})
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        core._apps_v1_client = mock_apps

        assert await core._detect_backend("test-sb", "default") == "k7d"


# --- create_sandbox rendering ---


class TestK7dCreateSandbox:
    async def test_create_uses_runtime_class_k7_and_no_pvc(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._networking_v1_client = mock_net

        sched_ok = MagicMock()
        sched_ok.success = True
        with patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched_ok)):
            cfg = SandboxConfig(name="k7d-sb", image="alpine:3.20", backend="k7d")
            result = await core.create_sandbox(cfg)

        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        pod_spec = deployment.spec.template.spec
        assert pod_spec.runtime_class_name == "k7"
        assert pod_spec.node_selector == {"k7.katakate.org/backend-k7d": "true"}
        # No Longhorn PVC, no persist-wrapper ConfigMap, no init container.
        mock_v1.create_namespaced_persistent_volume_claim.assert_not_called()
        mock_v1.create_namespaced_config_map.assert_not_called()
        assert not pod_spec.init_containers
        # Deployment carries the backend annotation for later detection.
        assert deployment.metadata.annotations["k7.katakate.org/backend"] == "k7d"

    async def test_create_skips_kata_memory_annotation(self, core: K7Core):
        mock_apps = AsyncMock()
        core._apps_v1_client = mock_apps
        core._core_v1_client = AsyncMock()
        core._networking_v1_client = AsyncMock()

        sched_ok = MagicMock()
        sched_ok.success = True
        with patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched_ok)):
            cfg = SandboxConfig(
                name="k7d-mem", image="alpine:3.20", backend="k7d", limits={"memory": "512Mi", "cpu": "1"}
            )
            result = await core.create_sandbox(cfg)

        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        annotations = deployment.spec.template.metadata.annotations
        assert "io.katacontainers.config.hypervisor.default_memory" not in annotations
        # Pod resources still flow through so the k7d shim sizes the VM.
        container = deployment.spec.template.spec.containers[0]
        assert container.resources.limits == {"memory": "512Mi", "cpu": "1"}


# --- pause / resume via the daemon socket ---


class TestK7dPauseResume:
    async def test_pause_freezes_vm_in_place(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("k7d-sb")
        core._apps_v1_client = mock_apps

        vm = {"vm_id": "vm-1-0", "guest_cid": 77, "sandbox_id": "cri123", "cluster_id": "cri123"}
        with (
            _local_node(core),
            patch.object(core, "_k7d_vm_for_sandbox", new=AsyncMock(return_value=vm)) as lookup,
            patch.object(core, "_k7d_request", new=AsyncMock(return_value={"status": "ok"})) as request,
        ):
            result = await core.pause_sandbox("k7d-sb")

        assert result.success, result.error
        lookup.assert_awaited_once_with("k7d-sb", "default")
        request.assert_awaited_once_with({"op": "pause_vm", "vm_id": "vm-1-0"})
        # The deployment is NOT scaled down — the VM is frozen in place.
        mock_apps.patch_namespaced_deployment_scale.assert_not_called()
        # Paused marker annotation stamped for list/resume.
        patch_body = mock_apps.patch_namespaced_deployment.call_args.kwargs["body"]
        assert patch_body["metadata"]["annotations"][K7D_ANN_PAUSED] == "true"

    async def test_pause_with_snapshot_is_loud_error(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("k7d-sb")
        core._apps_v1_client = mock_apps

        result = await core.pause_sandbox("k7d-sb", snapshot_name="snap1")
        assert not result.success
        assert "k7d" in result.error

    async def test_resume_unfreezes_vm(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("k7d-sb")
        core._apps_v1_client = mock_apps

        vm = {"vm_id": "vm-1-0", "guest_cid": 77, "sandbox_id": "cri123"}
        with (
            _local_node(core),
            patch.object(core, "_k7d_vm_for_sandbox", new=AsyncMock(return_value=vm)),
            patch.object(core, "_k7d_request", new=AsyncMock(return_value={"status": "ok"})) as request,
        ):
            result = await core.resume_sandbox("k7d-sb")

        assert result.success, result.error
        request.assert_awaited_once_with({"op": "resume_vm", "vm_id": "vm-1-0"})
        mock_apps.patch_namespaced_deployment_scale.assert_not_called()

    async def test_pause_daemon_error_is_loud(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("k7d-sb")
        core._apps_v1_client = mock_apps

        with (
            _local_node(core),
            patch.object(
                core,
                "_k7d_vm_for_sandbox",
                new=AsyncMock(side_effect=RuntimeError("k7d control socket /run/k7d/k7d.sock not found")),
            ),
        ):
            result = await core.pause_sandbox("k7d-sb")
        assert not result.success
        assert "k7d.sock" in result.error


# --- fork via the shim's fork-source annotations ---


class TestK7dFork:
    async def test_fork_creates_annotated_deployment(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("src-sb")
        core._apps_v1_client = mock_apps

        vm = {"vm_id": "vm-1-0", "guest_cid": 77, "sandbox_id": "cri-src", "cluster_id": "cri-src"}
        src_pod = mock_pod(name="src-sb-abc")
        src_pod.spec.node_name = "k7-node-01"
        with (
            _local_node(core),
            patch.object(core, "_k7d_vm_for_sandbox", new=AsyncMock(return_value=vm)),
            patch.object(core, "_k7d_running_pod", new=AsyncMock(return_value=src_pod)),
            patch.object(core, "_wait_for_pod_container_started", new=AsyncMock(return_value="dst-sb-xyz")),
        ):
            result = await core.fork_sandbox("src-sb", "dst-sb")

        assert result.success, result.error
        assert "warm fork" in result.message
        new_dep = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        assert new_dep.metadata.name == "dst-sb"
        annotations = new_dep.spec.template.metadata.annotations
        assert annotations[K7D_ANN_FORK_SOURCE_CLUSTER] == "cri-src"
        assert annotations[K7D_ANN_FORK_SOURCE_VM] == "cri-src"
        # Pinned to the source VM's node — the daemon socket is node-local.
        assert new_dep.spec.template.spec.node_name == "k7-node-01"
        # No Longhorn snapshot machinery involved.
        assert new_dep.spec.template.metadata.labels["app"] == "dst-sb"

    async def test_fork_with_sidecar_is_loud_error(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("src-sb", sidecar="docker")
        core._apps_v1_client = mock_apps

        result = await core.fork_sandbox("src-sb", "dst-sb")
        assert not result.success
        assert "sidecar" in result.error
        mock_apps.create_namespaced_deployment.assert_not_called()

    async def test_fork_with_snapshot_name_is_loud_error(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("src-sb")
        core._apps_v1_client = mock_apps

        result = await core.fork_sandbox("src-sb", "dst-sb", snapshot_name="keep-me")
        assert not result.success
        assert "k7d" in result.error

    async def test_fork_dead_source_is_loud_error(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("src-sb")
        core._apps_v1_client = mock_apps

        with (
            _local_node(core),
            patch.object(
                core,
                "_k7d_vm_for_sandbox",
                new=AsyncMock(side_effect=RuntimeError("no Running pod found for sandbox src-sb")),
            ),
        ):
            result = await core.fork_sandbox("src-sb", "dst-sb")
        assert not result.success
        assert "no Running pod" in result.error
        mock_apps.create_namespaced_deployment.assert_not_called()


# --- named snapshots are rejected loudly ---


class TestK7dSnapshotRejected:
    async def test_create_snapshot_rejected(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = _k7d_deployment("k7d-sb")
        core._apps_v1_client = mock_apps

        result = await core.create_snapshot("k7d-sb", "snap1")
        assert not result.success
        assert "k7d" in result.error
        assert "katakate/k7d" in result.error


# --- daemon socket helper ---


class TestK7dRequest:
    async def test_missing_socket_is_loud(self, core: K7Core):
        with (
            patch.dict("os.environ", {"K7D_SOCKET": "/nonexistent/k7d.sock"}),
            pytest.raises(RuntimeError, match="control socket"),
        ):
            await core._k7d_request({"op": "ping"})

    async def test_error_response_raises(self, core: K7Core):
        async def fake_thread(fn):
            return {"status": "error", "message": "no such VM: vm-9"}

        with (
            patch("k7.core.core.asyncio.to_thread", new=fake_thread),
            patch("k7.core.core.os.path.exists", return_value=True),
            pytest.raises(RuntimeError, match="no such VM"),
        ):
            await core._k7d_request({"op": "pause_vm", "vm_id": "vm-9"})
