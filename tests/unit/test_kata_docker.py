"""Unit tests for the Kata docker vehicle."""

from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes_asyncio.client.exceptions import ApiException

from k7.core.core import K7Core
from k7.core.docker import (
    ANN_DOCKER_PVC,
    ANN_K7_DOCKER,
    CLI_PATH,
    CLI_STAGING_DIR,
    DIND_IMAGE,
    DOCKER_HOST_URL,
    GRAPH_DEVICE_PATH,
    KATA_FORK_GRAPH_REJECT,
    KATA_SOCKET_DIR,
    KFD_DOCKER_STORAGE_CLASS,
    VEHICLE_CONTAINER_NAME,
)
from k7.core.models import ExecResult, SandboxConfig


def _setup(core, apps=None, v1=None, net=None):
    core._config_loaded = True
    if apps is not None:
        core._apps_v1_client = apps
    if v1 is not None:
        core._core_v1_client = v1
    if net is not None:
        core._networking_v1_client = net


def _ok():
    r = MagicMock()
    r.success = True
    r.error = ""
    r.data = {"created": True, "cm_name": "x"}
    return r


async def _create_kata_docker(core: K7Core, backend: str):
    mock_apps = AsyncMock()
    mock_v1 = AsyncMock()
    mock_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
    mock_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    mock_net = AsyncMock()
    _setup(core, apps=mock_apps, v1=mock_v1, net=mock_net)
    sched = _ok()
    overlay = _ok()
    with (
        patch.object(core, "_check_scheduling", new=AsyncMock(return_value=sched)),
        patch.object(core, "_wait_kata_docker_overlay2", new=AsyncMock(return_value=overlay)),
        patch.object(core, "_load_persist_bind_script", return_value='#!/bin/sh\nexec "$@"'),
        patch.object(core, "_load_docker_vehicle_script", return_value="#!/bin/sh\n"),
        patch.object(core, "_load_docker_cli_copy_script", return_value="#!/bin/sh\n"),
        patch.object(core, "_load_docker_cli_stage_script", return_value='#!/bin/sh\nexec "$@"\n'),
        patch.object(core, "_get_image_entrypoint_cmd", new=AsyncMock(return_value=([], ["/bin/sh"]))),
        patch.object(core, "_apply_sandbox_network_policies", new=AsyncMock(return_value=_ok())),
    ):
        cfg = SandboxConfig(
            name="dock-kata",
            image="ubuntu:24.04",
            backend=backend,
            docker=True,
            docker_disk="20Gi",
        )
        result = await core.create_sandbox(cfg)
    return result, mock_apps


class TestKataDockerPod:
    async def test_kql_vehicle_block_socket_cli_no_sidecar_ann(self, core: K7Core):
        result, mock_apps = await _create_kata_docker(core, "kata-qemu-longhorn")
        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        pod = deployment.spec.template.spec
        names = [c.name for c in pod.containers]
        assert names == ["sandbox", VEHICLE_CONTAINER_NAME]
        vehicle = next(c for c in pod.containers if c.name == VEHICLE_CONTAINER_NAME)
        sandbox = next(c for c in pod.containers if c.name == "sandbox")
        assert vehicle.security_context.privileged is True
        assert sandbox.security_context.privileged is True  # persist-bind, not docker
        devices = vehicle.volume_devices or []
        assert [d.device_path for d in devices] == [GRAPH_DEVICE_PATH]
        graph_mounts = [m for m in (vehicle.volume_mounts or []) if m.mount_path == "/var/lib/docker"]
        assert graph_mounts == []
        sock_mounts = [m.mount_path for m in (sandbox.volume_mounts or [])]
        assert KATA_SOCKET_DIR in sock_mounts
        assert CLI_STAGING_DIR in sock_mounts
        assert "/tmp" in sock_mounts
        assert "/var/run" not in sock_mounts
        tmp_vol = {m.name for m in (sandbox.volume_mounts or []) if m.mount_path == "/tmp"}
        vehicle_tmp = {m.name for m in (vehicle.volume_mounts or []) if m.mount_path == "/tmp"}
        assert tmp_vol == {"path-share-tmp"}
        assert vehicle_tmp == {"path-share-tmp"}
        env = {e.name: e.value for e in (sandbox.env or [])}
        assert env.get("DOCKER_HOST") == DOCKER_HOST_URL
        cli = [m for m in sandbox.volume_mounts if m.mount_path == CLI_PATH]
        assert cli and cli[0].read_only is True
        assert pod.share_process_namespace is True
        assert not pod.host_pid
        assert all(v.host_path is None for v in (pod.volumes or []))
        assert deployment.metadata.annotations.get(ANN_K7_DOCKER) == "true"
        assert "k7.katakate.org/sidecar" not in (deployment.metadata.annotations or {})
        assert deployment.metadata.annotations.get(ANN_DOCKER_PVC) == "dock-kata-docker-lh"
        inits = [c.name for c in (pod.init_containers or [])]
        assert "docker-cli-copy" in inits
        assert DIND_IMAGE in {vehicle.image, *(c.image for c in pod.init_containers or [])}


def _exec_ok():
    return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=0)


class TestKataDockerSnapshot:
    async def test_create_snapshot_waits_for_root_and_docker(self, core: K7Core):
        dep = MagicMock()
        dep.metadata.annotations = {
            ANN_K7_DOCKER: "true",
            ANN_DOCKER_PVC: "sb-docker-lh",
        }
        dep.status.ready_replicas = 1
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        _setup(core, apps=mock_apps, v1=AsyncMock())
        wait = AsyncMock(return_value=_ok())
        exec_cmd = AsyncMock(return_value=_exec_ok())
        with (
            patch.object(core, "_detect_backend", new=AsyncMock(return_value="kata-qemu-longhorn")),
            patch.object(core, "_create_volume_snapshot", new=AsyncMock(return_value=_ok())),
            patch.object(core, "_wait_for_snapshot_ready", new=wait),
            patch.object(core, "exec_command", new=exec_cmd),
        ):
            result = await core.create_snapshot("sb", "snap1")
        assert result.success, result.error
        assert wait.await_count == 2
        names = [c.args[0] for c in wait.await_args_list]
        assert names == ["snap1", "snap1-docker"]
        assert exec_cmd.await_count == 2
        assert exec_cmd.await_args_list[0].kwargs.get("container") in (None, "sandbox")
        assert exec_cmd.await_args_list[1].kwargs.get("container") == VEHICLE_CONTAINER_NAME

    async def test_create_snapshot_skips_sync_when_paused(self, core: K7Core):
        dep = MagicMock()
        dep.metadata.annotations = {ANN_K7_DOCKER: "true", ANN_DOCKER_PVC: "sb-docker-lh"}
        dep.status.ready_replicas = 0
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        _setup(core, apps=mock_apps, v1=AsyncMock())
        exec_cmd = AsyncMock(return_value=_exec_ok())
        with (
            patch.object(core, "_detect_backend", new=AsyncMock(return_value="kata-qemu-longhorn")),
            patch.object(core, "_create_volume_snapshot", new=AsyncMock(return_value=_ok())),
            patch.object(core, "_wait_for_snapshot_ready", new=AsyncMock(return_value=_ok())),
            patch.object(core, "exec_command", new=exec_cmd),
        ):
            result = await core.create_snapshot("sb", "snap1")
        assert result.success, result.error
        exec_cmd.assert_not_awaited()


class TestKataDockerRestore:
    async def test_restore_clones_docker_block_pvc(self, core: K7Core):
        snap_info = MagicMock()
        snap_info.ready_to_use = True
        custom = AsyncMock()
        custom.get_namespaced_custom_object.return_value = {
            "metadata": {
                "annotations": {
                    "k7.io/source-image": "ubuntu:24.04",
                    "k7.io/source-backend": "kata-qemu-longhorn",
                    "k7.io/source-docker": "true",
                    "k7.io/source-docker-disk": "20Gi",
                }
            }
        }
        apps = AsyncMock()
        apps.read_namespaced_deployment.side_effect = ApiException(status=404)
        _setup(core, apps=apps, v1=AsyncMock())
        core._custom_objects_client = custom
        clones: list[dict] = []

        async def fake_clone(**kwargs):
            clones.append(kwargs)
            return _ok()

        with (
            patch.object(core, "get_snapshot", new=AsyncMock(return_value=snap_info)),
            patch.object(core, "_create_pvc_from_snapshot", new=fake_clone),
            patch.object(core, "create_sandbox", new=AsyncMock(return_value=_ok())),
        ):
            result = await core.restore_sandbox("snap1", "dst")
        assert result.success, result.error
        assert [c["snapshot_name"] for c in clones] == ["snap1", "snap1-docker"]
        assert clones[1]["volume_mode"] == "Block"
        assert clones[1]["target_pvc_name"] == "dst-docker-lh"

    async def test_restore_without_docker_skips_docker_pvc(self, core: K7Core):
        snap_info = MagicMock()
        snap_info.ready_to_use = True
        custom = AsyncMock()
        custom.get_namespaced_custom_object.return_value = {
            "metadata": {
                "annotations": {
                    "k7.io/source-image": "ubuntu:24.04",
                    "k7.io/source-backend": "kata-qemu-longhorn",
                }
            }
        }
        apps = AsyncMock()
        apps.read_namespaced_deployment.side_effect = ApiException(status=404)
        _setup(core, apps=apps, v1=AsyncMock())
        core._custom_objects_client = custom
        clones: list[dict] = []

        async def fake_clone(**kwargs):
            clones.append(kwargs)
            return _ok()

        with (
            patch.object(core, "get_snapshot", new=AsyncMock(return_value=snap_info)),
            patch.object(core, "_create_pvc_from_snapshot", new=fake_clone),
            patch.object(core, "create_sandbox", new=AsyncMock(return_value=_ok())),
        ):
            result = await core.restore_sandbox("snap1", "dst")
        assert result.success, result.error
        assert [c["snapshot_name"] for c in clones] == ["snap1"]

    async def test_kfd_ephemeral_block_not_privileged_sandbox(self, core: K7Core):
        result, mock_apps = await _create_kata_docker(core, "kata-firecracker-devmapper")
        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        pod = deployment.spec.template.spec
        sandbox = next(c for c in pod.containers if c.name == "sandbox")
        vehicle = next(c for c in pod.containers if c.name == VEHICLE_CONTAINER_NAME)
        assert sandbox.security_context.privileged is not True
        assert sandbox.security_context.allow_privilege_escalation is False
        assert sandbox.security_context.capabilities.drop == ["ALL"]
        assert sandbox.security_context.seccomp_profile.type == "RuntimeDefault"
        assert vehicle.security_context.privileged is True
        graph = next(v for v in pod.volumes if v.name == "docker-graph")
        assert graph.ephemeral is not None
        claim = graph.ephemeral.volume_claim_template.spec
        assert claim.volume_mode == "Block"
        assert claim.storage_class_name == KFD_DOCKER_STORAGE_CLASS
        assert ANN_DOCKER_PVC not in (deployment.metadata.annotations or {})
        assert sandbox.command == ["/bin/sh", "/opt/k7/docker/k7-docker-cli-stage.sh"]
        assert sandbox.args == ["/bin/sh", "-c", "sleep 365d"]

    async def test_sidecar_docker_alias_is_vehicle_not_dind(self, core: K7Core):
        """API/core path with docker=True (what the CLI alias produces)."""
        result, mock_apps = await _create_kata_docker(core, "kata-qemu-longhorn")
        assert result.success, result.error
        deployment = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        names = [c.name for c in deployment.spec.template.spec.containers]
        assert "sidecar" not in names
        assert VEHICLE_CONTAINER_NAME in names
        images = [c.image for c in deployment.spec.template.spec.containers]
        assert "docker:27.5-dind" not in images


class TestKataDockerForkReject:
    async def test_kfd_docker_fork_rejected(self, core: K7Core):
        dep = MagicMock()
        dep.metadata.annotations = {
            "k7.katakate.org/backend": "kata-firecracker-devmapper",
            ANN_K7_DOCKER: "true",
        }
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        _setup(core, apps=mock_apps, v1=AsyncMock())
        with patch.object(core, "_detect_backend", new=AsyncMock(return_value="kata-firecracker-devmapper")):
            result = await core.fork_sandbox("src", "dst")
        assert not result.success
        assert "cannot be cloned" in result.error
        assert result.error == KATA_FORK_GRAPH_REJECT


class TestDindImagePin:
    def test_dind_image_is_digest_pinned(self):
        assert DIND_IMAGE.startswith("docker:27.5.1-dind@sha256:")
        digest = DIND_IMAGE.split("sha256:", 1)[1]
        assert len(digest) == 64
