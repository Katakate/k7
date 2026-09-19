"""Unit tests for the k7d backend paths in K7Core.

Everything here is mocked — the daemon socket, crictl, and the k8s API
are never touched. The real end-to-end behaviour is covered by
``tests/integration/test_k7d.py`` on a k7d-capable node.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7.core.core import (
    K7_TENANT_LABEL,
    K7D_ANN_FORK_SOURCE_CLUSTER,
    K7D_ANN_FORK_SOURCE_VM,
    K7D_ANN_PAUSED,
    K7D_FC_VIRTIOFS_REFUSED,
    K7Core,
)
from k7.core.models import OperationResult, SandboxConfig
from tests.unit.conftest import mock_deployment, mock_pod


@contextmanager
def _local_node(core: K7Core, node: str = "k7-node-01", pod: str = "src-sb-abc"):
    """Sandbox appears LOCAL — direct daemon-socket path, not agent forward."""
    with (
        patch.object(core, "_k7d_local_node", return_value=node),
        patch.object(core, "_k7d_sandbox_node", new=AsyncMock(return_value=node)),
        patch.object(core, "_k7d_sandbox_placement", new=AsyncMock(return_value=(node, pod))),
    ):
        yield


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

    def test_k7_fc_alias_canonicalizes_to_k7d_fc(self):
        assert K7Core._canonicalize_backend("k7-fc") == "k7d-fc"

    def test_k7d_fc_passes_through(self):
        assert K7Core._canonicalize_backend("k7d-fc") == "k7d-fc"

    async def test_detect_backend_accepts_k7d_fc_annotation(self, core: K7Core):
        dep = mock_deployment(annotations={"k7.katakate.org/backend": "k7d-fc"})
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        core._apps_v1_client = mock_apps

        assert await core._detect_backend("test-sb", "default") == "k7d-fc"


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

    async def test_node_name_uses_hostname_selector_not_spec_node_name(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        node = MagicMock()
        node.spec.taints = []
        mock_v1.read_node.return_value = node
        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._networking_v1_client = mock_net

        sched_ok = MagicMock()
        sched_ok.success = True
        with patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched_ok)):
            cfg = SandboxConfig(name="k7d-pin", image="alpine:3.20", backend="k7d", node_name="k7-node-01")
            result = await core.create_sandbox(cfg)

        assert result.success, result.error
        pod_spec = mock_apps.create_namespaced_deployment.call_args.kwargs["body"].spec.template.spec
        assert pod_spec.node_name in (None, "")
        assert pod_spec.node_selector == {
            "k7.katakate.org/backend-k7d": "true",
            "kubernetes.io/hostname": "k7-node-01",
        }
        assert not pod_spec.tolerations
        mock_v1.read_node.assert_awaited_once_with("k7-node-01")

    async def test_dedicated_node_adds_tenant_toleration(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        taint = MagicMock()
        taint.key = "k7.katakate.org/tenant"
        taint.value = "acme"
        taint.effect = "NoSchedule"
        node = MagicMock()
        node.spec.taints = [taint]
        mock_v1.read_node.return_value = node
        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._networking_v1_client = mock_net

        sched_ok = MagicMock()
        sched_ok.success = True
        with patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched_ok)):
            cfg = SandboxConfig(name="k7d-ten", image="alpine:3.20", backend="k7d", node_name="k7-node-01")
            result = await core.create_sandbox(cfg)

        assert result.success, result.error
        pod_spec = mock_apps.create_namespaced_deployment.call_args.kwargs["body"].spec.template.spec
        assert pod_spec.tolerations
        tol = pod_spec.tolerations[0]
        assert tol.key == "k7.katakate.org/tenant"
        assert tol.value == "acme"
        assert tol.effect == "NoSchedule"

    async def test_missing_pin_node_fails_loud(self, core: K7Core):
        from kubernetes_asyncio.client.exceptions import ApiException

        mock_v1 = AsyncMock()
        mock_v1.read_node.side_effect = ApiException(status=404)
        core._apps_v1_client = AsyncMock()
        core._core_v1_client = mock_v1
        core._networking_v1_client = AsyncMock()
        cfg = SandboxConfig(name="k7d-gone", image="alpine:3.20", backend="k7d", node_name="no-such-node")
        result = await core.create_sandbox(cfg)
        assert not result.success
        assert "no-such-node" in (result.error or "")
        core._apps_v1_client.create_namespaced_deployment.assert_not_called()

    async def test_create_k7d_fc_uses_runtime_class_k7_fc(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._networking_v1_client = mock_net

        sched_ok = MagicMock()
        sched_ok.success = True
        with patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched_ok)):
            cfg = SandboxConfig(name="k7d-fc-sb", image="alpine:3.20", backend="k7d-fc")
            result = await core.create_sandbox(cfg)

        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        pod_spec = deployment.spec.template.spec
        assert pod_spec.runtime_class_name == "k7-fc"
        assert pod_spec.node_selector == {"k7.katakate.org/backend-k7d-fc": "true"}
        assert deployment.metadata.annotations["k7.katakate.org/backend"] == "k7d-fc"

    async def test_create_k7d_fc_refuses_hostpath_before_pod(self, core: K7Core):
        core._apps_v1_client = AsyncMock()
        core._core_v1_client = AsyncMock()
        core._networking_v1_client = AsyncMock()
        cfg = SandboxConfig(
            name="k7d-fc-hp",
            image="alpine:3.20",
            backend="k7d-fc",
            volumes=[{"name": "hp", "hostPath": {"path": "/var/log"}}],
        )
        result = await core.create_sandbox(cfg)
        assert not result.success
        assert result.error == K7D_FC_VIRTIOFS_REFUSED
        core._apps_v1_client.create_namespaced_deployment.assert_not_called()

    async def test_docker_stamps_annotations_on_k7d_fc(self, core: K7Core):
        mock_apps = AsyncMock()
        core._apps_v1_client = mock_apps
        core._core_v1_client = AsyncMock()
        core._networking_v1_client = AsyncMock()

        sched_ok = MagicMock()
        sched_ok.success = True
        with (
            patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched_ok)),
            patch("k7.core.core.k7d_supports_docker", return_value=True),
        ):
            cfg = SandboxConfig(name="k7d-fc-dock", image="ubuntu:24.04", backend="k7-fc", docker=True)
            result = await core.create_sandbox(cfg)

        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        assert deployment.spec.template.spec.runtime_class_name == "k7-fc"
        assert deployment.spec.template.metadata.annotations["k7d.katakate.org/docker"] == "true"

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

    async def test_docker_stamps_annotations_no_sidecar_no_privileged(self, core: K7Core):
        mock_apps = AsyncMock()
        core._apps_v1_client = mock_apps
        core._core_v1_client = AsyncMock()
        core._networking_v1_client = AsyncMock()

        sched_ok = MagicMock()
        sched_ok.success = True
        with (
            patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched_ok)),
            patch("k7.core.core.k7d_supports_docker", return_value=True),
        ):
            cfg = SandboxConfig(
                name="k7d-dock",
                image="ubuntu:24.04",
                backend="k7d",
                docker=True,
                docker_disk="40Gi",
            )
            result = await core.create_sandbox(cfg)

        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        ann = deployment.metadata.annotations
        pod_ann = deployment.spec.template.metadata.annotations
        assert ann["k7.katakate.org/docker"] == "true"
        assert pod_ann["k7d.katakate.org/docker"] == "true"
        assert pod_ann["k7d.katakate.org/docker-disk"] == "40Gi"
        assert "k7.katakate.org/sidecar" not in ann
        assert "k7.katakate.org/sidecar" not in pod_ann
        containers = deployment.spec.template.spec.containers
        assert [c.name for c in containers] == ["sandbox"]
        sandbox = containers[0]
        assert sandbox.security_context.privileged is not True
        assert sandbox.security_context.allow_privilege_escalation is False
        assert sandbox.security_context.capabilities.drop == ["ALL"]
        assert sandbox.security_context.seccomp_profile.type == "RuntimeDefault"

    async def test_docker_against_old_k7d_fails_loud(self, core: K7Core):
        core._apps_v1_client = AsyncMock()
        core._core_v1_client = AsyncMock()
        core._networking_v1_client = AsyncMock()
        with patch("k7.core.core.k7d_supports_docker", return_value=False):
            cfg = SandboxConfig(name="k7d-old", image="ubuntu:24.04", backend="k7d", docker=True)
            result = await core.create_sandbox(cfg)
        assert not result.success
        assert "no docker service" in result.error
        assert "upgrade" in result.error

    async def test_docker_and_sidecar_mutually_exclusive(self, core: K7Core):
        core._apps_v1_client = AsyncMock()
        core._core_v1_client = AsyncMock()
        core._networking_v1_client = AsyncMock()
        cfg = SandboxConfig(
            name="both",
            image="ubuntu:24.04",
            backend="k7d",
            docker=True,
            sidecar="docker",
        )
        result = await core.create_sandbox(cfg)
        assert not result.success
        assert "mutually exclusive" in result.error


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

    async def test_pause_k7d_fc_uses_daemon_socket(self, core: K7Core):
        mock_apps = AsyncMock()
        dep = mock_deployment(name="fc-sb", runtime_class="k7-fc", annotations={"k7.katakate.org/backend": "k7d-fc"})
        mock_apps.read_namespaced_deployment.return_value = dep
        core._apps_v1_client = mock_apps
        vm = {"vm_id": "vm-fc-0", "guest_cid": 88, "sandbox_id": "cri-fc"}
        with (
            _local_node(core),
            patch.object(core, "_k7d_vm_for_sandbox", new=AsyncMock(return_value=vm)),
            patch.object(core, "_k7d_request", new=AsyncMock(return_value={"status": "ok"})) as request,
        ):
            result = await core.pause_sandbox("fc-sb")
        assert result.success, result.error
        request.assert_awaited_once_with({"op": "pause_vm", "vm_id": "vm-fc-0"})
        mock_apps.patch_namespaced_deployment_scale.assert_not_called()

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
            patch.object(
                core, "_read_sandbox_egress_whitelist", new=AsyncMock(return_value=["example.com"])
            ) as read_egress,
            patch.object(core, "_read_sandbox_ingress_rules", new=AsyncMock(return_value=([8000], ["sandbox:a"]))),
            patch.object(
                core, "_apply_sandbox_network_policies", new=AsyncMock(return_value=OperationResult(True))
            ) as apply_policies,
        ):
            result = await core.fork_sandbox("src-sb", "dst-sb")

        assert result.success, result.error
        # The warm fork inherits the source's policy too.
        read_egress.assert_awaited_once_with("src-sb", "default")
        assert apply_policies.call_args.kwargs["egress_whitelist"] == ["example.com"]
        assert apply_policies.call_args.kwargs["ingress_ports"] == [8000]
        assert apply_policies.call_args.kwargs["ingress_from"] == ["sandbox:a"]
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
        assert "real CRI sidecar" in result.error
        mock_apps.create_namespaced_deployment.assert_not_called()

    async def test_fork_docker_annotation_alone_is_not_a_sidecar(self, core: K7Core):
        mock_apps = AsyncMock()
        annotations = {
            "k7.katakate.org/backend": "k7d",
            "k7.katakate.org/docker": "true",
            "k7d.katakate.org/docker": "true",
        }
        dep = mock_deployment(name="src-sb", runtime_class="k7", annotations=annotations)
        dep.spec.template.metadata.annotations = dict(annotations)
        dep.spec.template.metadata.labels = {"app": "src-sb", "katakate.org/sandbox": "src-sb"}
        dep.metadata.labels = {"app": "src-sb", "runtime": "kata", "katakate.org/sandbox": "src-sb"}
        dep.spec.selector.match_labels = {"app": "src-sb"}
        mock_apps.read_namespaced_deployment.return_value = dep
        core._apps_v1_client = mock_apps

        vm = {"vm_id": "vm-1-0", "guest_cid": 77, "sandbox_id": "cri-src", "cluster_id": "cri-src"}
        src_pod = mock_pod(name="src-sb-abc")
        src_pod.spec.node_name = "k7-node-01"
        with (
            _local_node(core),
            patch.object(core, "_k7d_vm_for_sandbox", new=AsyncMock(return_value=vm)),
            patch.object(core, "_k7d_running_pod", new=AsyncMock(return_value=src_pod)),
            patch.object(core, "_wait_for_pod_container_started", new=AsyncMock(return_value="dst-sb-xyz")),
            patch.object(core, "_read_sandbox_egress_whitelist", new=AsyncMock(return_value=[])),
            patch.object(core, "_read_sandbox_ingress_rules", new=AsyncMock(return_value=([], []))),
            patch.object(core, "_apply_sandbox_network_policies", new=AsyncMock(return_value=OperationResult(True))),
        ):
            result = await core.fork_sandbox("src-sb", "dst-sb")

        assert result.success, result.error
        mock_apps.create_namespaced_deployment.assert_called_once()

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


class TestDedicateNode:
    async def test_dedicate_writes_label_and_taint(self, core: K7Core):
        mock_v1 = AsyncMock()
        node = MagicMock()
        node.spec.taints = []
        mock_v1.read_node.return_value = node
        core._core_v1_client = mock_v1

        result = await core.dedicate_node("k7-node-01", "acme")
        assert result.success, result.error
        body = mock_v1.patch_node.await_args.args[1]
        assert body["metadata"]["labels"][K7_TENANT_LABEL] == "acme"
        assert body["spec"]["taints"] == [{"key": K7_TENANT_LABEL, "value": "acme", "effect": "NoSchedule"}]

    async def test_dedicate_missing_node_fails_loud(self, core: K7Core):
        from kubernetes_asyncio.client.exceptions import ApiException

        mock_v1 = AsyncMock()
        mock_v1.read_node.side_effect = ApiException(status=404)
        core._core_v1_client = mock_v1
        result = await core.dedicate_node("missing", "acme")
        assert not result.success
        assert "not found" in (result.error or "")
        mock_v1.patch_node.assert_not_called()

    async def test_undedicate_drops_label_and_taint(self, core: K7Core):
        mock_v1 = AsyncMock()
        other = MagicMock()
        other.key = "node.kubernetes.io/unreachable"
        other.value = None
        other.effect = "NoExecute"
        ours = MagicMock()
        ours.key = K7_TENANT_LABEL
        ours.value = "acme"
        ours.effect = "NoSchedule"
        node = MagicMock()
        node.spec.taints = [other, ours]
        mock_v1.read_node.return_value = node
        core._core_v1_client = mock_v1

        result = await core.undedicate_node("k7-node-01")
        assert result.success, result.error
        body = mock_v1.patch_node.await_args.args[1]
        assert body["metadata"]["labels"][K7_TENANT_LABEL] is None
        assert body["spec"]["taints"] == [
            {"key": "node.kubernetes.io/unreachable", "value": None, "effect": "NoExecute"}
        ]
