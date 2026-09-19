"""Mock-based unit tests for K7Core lifecycle methods (no real k8s needed)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes_asyncio.client.exceptions import ApiException

from k7.core.core import K7_TENANT_LABEL, K7Core
from k7.core.models import ExecResult, OperationResult, SandboxConfig
from tests.unit.conftest import mock_deployment, mock_pod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_clients(core, apps=None, v1=None, net=None, metrics=None, custom=None, batch=None, apiext=None):
    """Wire MagicMock clients into a K7Core instance."""
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
    if batch is not None:
        core._batch_v1_client = batch
    if apiext is not None:
        core._apiextensions_v1_client = apiext


# --- _detect_backend ---


class TestDetectBackend:
    async def test_annotation_takes_priority(self, core: K7Core):
        dep = mock_deployment(annotations={"k7.katakate.org/backend": "kata-qemu-longhorn"})
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        assert await core._detect_backend("test-sb", "default") == "kata-qemu-longhorn"

    async def test_host_file_when_no_sandbox(self, core: K7Core, tmp_path):
        backend_file = tmp_path / "backend"
        backend_file.write_text("kata-qemu-longhorn\n")

        with patch("os.path.exists", return_value=True), patch("builtins.open", return_value=open(backend_file)):
            assert await core._detect_backend(None) == "kata-qemu-longhorn"

    async def test_default_when_nothing_found(self, core: K7Core):
        with patch("os.path.exists", return_value=False):
            assert await core._detect_backend(None) == "kata-firecracker-devmapper"

    async def test_default_when_annotation_missing(self, core: K7Core):
        dep = mock_deployment(annotations={})
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        with patch("os.path.exists", return_value=False):
            assert await core._detect_backend("test-sb", "default") == "kata-firecracker-devmapper"

    async def test_annotation_api_error_falls_through(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.side_effect = ApiException(status=404)
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        with patch("os.path.exists", return_value=False):
            assert await core._detect_backend("missing", "default") == "kata-firecracker-devmapper"

    async def test_none_file_is_not_kfd(self, core: K7Core, tmp_path):
        backend_file = tmp_path / "backend"
        backend_file.write_text("none\n")
        with patch("os.path.exists", return_value=True), patch("builtins.open", return_value=open(backend_file)):
            assert await core._detect_backend(None) == ""


# --- delete_sandbox ---


class TestDeleteSandbox:
    async def test_successful_delete(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        mock_custom = AsyncMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._networking_v1_client = mock_net
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        result = await core.delete_sandbox("my-sb", "default")

        assert result.success is True
        mock_apps.delete_namespaced_deployment.assert_called_once_with(name="my-sb", namespace="default")
        mock_v1.delete_namespaced_secret.assert_called_once_with(name="my-sb-env", namespace="default")
        mock_v1.delete_namespaced_config_map.assert_any_call(name="my-sb-persist-wrapper", namespace="default")
        mock_v1.delete_namespaced_config_map.assert_any_call(name="my-sb-docker-vehicle", namespace="default")
        mock_v1.delete_namespaced_persistent_volume_claim.assert_any_call(name="my-sb-root-lh", namespace="default")
        mock_v1.delete_namespaced_persistent_volume_claim.assert_any_call(name="my-sb-docker-lh", namespace="default")

    async def test_delete_ignores_404s(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.delete_namespaced_deployment.side_effect = ApiException(status=404)
        mock_v1 = AsyncMock()
        mock_v1.delete_namespaced_secret.side_effect = ApiException(status=404)
        mock_v1.delete_namespaced_config_map.side_effect = ApiException(status=404)
        mock_v1.delete_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
        mock_v1.patch_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
        mock_net = AsyncMock()
        mock_net.delete_namespaced_network_policy.side_effect = ApiException(status=404)
        mock_custom = AsyncMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._networking_v1_client = mock_net
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        result = await core.delete_sandbox("gone-sb", "default")
        assert result.success is True

    async def test_delete_reports_non_404_errors(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.delete_namespaced_deployment.side_effect = ApiException(status=500, reason="Server Error")
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        mock_custom = AsyncMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        core._apps_v1_client = mock_apps
        core._core_v1_client = mock_v1
        core._networking_v1_client = mock_net
        core._custom_objects_client = mock_custom
        core._config_loaded = True

        result = await core.delete_sandbox("err-sb", "default")
        assert result.success is False
        assert "deployment" in result.error


# --- list_sandboxes ---


class TestListSandboxes:
    async def test_returns_sandbox_info_list(self, core: K7Core):
        dep = mock_deployment(name="sb1", annotations={"k7.katakate.org/backend": "kata-firecracker-devmapper"})
        pod = mock_pod(name="sb1-abc", image="ubuntu:22.04")

        mock_apps = AsyncMock()
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        core._core_v1_client = mock_v1

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=[dep]):
            result = await core.list_sandboxes(namespace="default")

        assert len(result) == 1
        assert result[0].name == "sb1"
        assert result[0].image == "ubuntu:22.04"
        assert result[0].backend == "kata-firecracker-devmapper"
        assert result[0].ready == "True"

    async def test_returns_empty_on_no_sandboxes(self, core: K7Core):
        mock_apps = AsyncMock()
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=[]):
            result = await core.list_sandboxes(namespace="default")

        assert result == []

    async def test_handles_pod_with_no_items(self, core: K7Core):
        dep = mock_deployment(name="sb-no-pod", annotations={"k7.katakate.org/backend": "kata-qemu-longhorn"})

        mock_apps = AsyncMock()
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        core._core_v1_client = mock_v1

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=[dep]):
            result = await core.list_sandboxes(namespace="default")

        assert len(result) == 1
        assert result[0].status == "No Pods"
        assert result[0].ready == "False"

    async def test_exception_returns_empty_list(self, core: K7Core):
        core._config_loaded = True

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, side_effect=Exception("k8s down")):
            result = await core.list_sandboxes(namespace="default")

        assert result == []


# --- _get_kata_sandboxes ---


class TestGetKataSandboxes:
    async def test_filters_by_runtime_class(self, core: K7Core):
        kata_dep = mock_deployment(name="kata-sb", runtime_class="kata")
        non_kata = mock_deployment(name="regular", runtime_class="runc")
        non_kata.metadata.labels = {}

        mock_apps = AsyncMock()
        mock_apps.list_namespaced_deployment.return_value = MagicMock(items=[kata_dep, non_kata])
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        result = await core._get_kata_sandboxes(namespace="default")
        assert len(result) == 1
        assert result[0].metadata.name == "kata-sb"

    async def test_filters_by_label_fallback(self, core: K7Core):
        dep = mock_deployment(name="label-sb", runtime_class="runc", labels={"runtime": "kata"})

        mock_apps = AsyncMock()
        mock_apps.list_namespaced_deployment.return_value = MagicMock(items=[dep])
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        result = await core._get_kata_sandboxes(namespace="default")
        assert len(result) == 1

    async def test_all_namespaces_when_none(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.list_deployment_for_all_namespaces.return_value = MagicMock(items=[])
        core._apps_v1_client = mock_apps
        core._config_loaded = True

        await core._get_kata_sandboxes(namespace=None)
        mock_apps.list_deployment_for_all_namespaces.assert_called_once()


# --- _ensure_root_pvc ---


class TestEnsureRootPvc:
    async def test_exists_with_filesystem_mode(self, core: K7Core):
        mock_v1 = AsyncMock()
        pvc = MagicMock()
        pvc.spec.volume_mode = "Filesystem"
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_root_pvc("sb1", "default", "10Gi")
        assert result.success is True
        assert result.data["created"] is False

    async def test_exists_with_wrong_volume_mode(self, core: K7Core):
        mock_v1 = AsyncMock()
        pvc = MagicMock()
        pvc.spec.volume_mode = "Block"
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_root_pvc("sb1", "default", "10Gi")
        assert result.success is False
        assert "Block" in result.error

    async def test_creates_when_not_found(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_root_pvc("sb1", "default", "10Gi")
        assert result.success is True
        assert result.data["created"] is True
        mock_v1.create_namespaced_persistent_volume_claim.assert_called_once()

    async def test_create_fails(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
        mock_v1.create_namespaced_persistent_volume_claim.side_effect = ApiException(status=500)
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_root_pvc("sb1", "default", "10Gi")
        assert result.success is False

    async def test_non_404_read_error(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=500)
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_root_pvc("sb1", "default", "10Gi")
        assert result.success is False
        assert "lookup error" in result.error


# --- _ensure_persist_wrapper_configmap ---


class TestEnsurePersistWrapperConfigmap:
    async def test_exists(self, core: K7Core):
        mock_v1 = AsyncMock()
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_persist_wrapper_configmap("default", "sb1", "#!/bin/bash")
        assert result.success is True
        assert result.data["created"] is False

    async def test_creates_when_not_found(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_persist_wrapper_configmap("default", "sb1", "#!/bin/bash")
        assert result.success is True
        assert result.data["created"] is True
        mock_v1.create_namespaced_config_map.assert_called_once()

    async def test_non_404_read_error(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_config_map.side_effect = ApiException(status=500)
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_persist_wrapper_configmap("default", "sb1", "#!/bin/bash")
        assert result.success is False
        assert "lookup error" in result.error

    async def test_create_fails(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_config_map.side_effect = ApiException(status=404)
        mock_v1.create_namespaced_config_map.side_effect = ApiException(status=500)
        _setup_clients(core, v1=mock_v1)

        result = await core._ensure_persist_wrapper_configmap("default", "sb1", "#!/bin/bash")
        assert result.success is False


# --- _update_deployment_annotation ---


class TestUpdateDeploymentAnnotation:
    async def test_patches_deployment(self, core: K7Core):
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps)

        await core._update_deployment_annotation("sb1", "default", "key", "val")
        mock_apps.patch_namespaced_deployment.assert_called_once_with(
            name="sb1",
            namespace="default",
            body={"metadata": {"annotations": {"key": "val"}}},
        )


# --- pause_sandbox ---


class TestPauseSandbox:
    async def test_scales_to_zero(self, core: K7Core):
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps)

        result = await core.pause_sandbox("sb1")
        assert result.success is True
        assert "paused" in result.message
        mock_apps.patch_namespaced_deployment_scale.assert_called_once()

    async def test_with_snapshot_explicit_pvc(self, core: K7Core):
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps)

        with patch.object(
            core,
            "_create_volume_snapshot",
            new_callable=AsyncMock,
            return_value=OperationResult(success=True, message="snap ok"),
        ) as snap_mock:
            result = await core.pause_sandbox("sb1", pvc_name="pvc1", snapshot_name="snap1")

        assert result.success is True
        assert "Snapshot snap1 created" in result.message
        snap_mock.assert_called_once()
        assert snap_mock.call_args.kwargs["pvc_name"] == "pvc1"
        assert snap_mock.call_args.kwargs["snapshot_name"] == "snap1"

    async def test_snapshot_only_defaults_pvc_to_root(self, core: K7Core):
        """Regression: snapshot_name alone triggers snapshot.

        The snapshot used to fire only when ``pvc_name AND snapshot_name`` were
        both set, so ``k7 pause foo --snapshot bar`` silently did nothing.
        Now ``snapshot_name`` alone is sufficient and ``pvc_name``
        defaults to ``_root_pvc_name(name)``.
        """
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps)

        with patch.object(
            core,
            "_create_volume_snapshot",
            new_callable=AsyncMock,
            return_value=OperationResult(success=True, message="snap ok"),
        ) as snap_mock:
            result = await core.pause_sandbox("sb1", snapshot_name="snap1")

        assert result.success is True
        assert "Snapshot snap1 created" in result.message
        snap_mock.assert_called_once()
        assert snap_mock.call_args.kwargs["pvc_name"] == "sb1-root-lh"
        assert snap_mock.call_args.kwargs["snapshot_name"] == "snap1"

    async def test_no_snapshot_when_name_unset(self, core: K7Core):
        """Without ``snapshot_name`` we must NOT call _create_volume_snapshot."""
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps)

        with patch.object(
            core,
            "_create_volume_snapshot",
            new_callable=AsyncMock,
        ) as snap_mock:
            result = await core.pause_sandbox("sb1", pvc_name="ignored-without-name")

        assert result.success is True
        assert "Snapshot" not in result.message
        snap_mock.assert_not_called()

    async def test_snapshot_fails(self, core: K7Core):
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps)

        with patch.object(
            core,
            "_create_volume_snapshot",
            new_callable=AsyncMock,
            return_value=OperationResult(success=False, error="snap err"),
        ):
            result = await core.pause_sandbox("sb1", snapshot_name="snap1")

        assert result.success is False
        assert "snap err" in result.error

    async def test_api_exception(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.patch_namespaced_deployment_scale.side_effect = ApiException(status=500)
        _setup_clients(core, apps=mock_apps)

        result = await core.pause_sandbox("sb1")
        assert result.success is False


# --- resume_sandbox ---


class TestResumeSandbox:
    async def test_scales_to_one(self, core: K7Core):
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps)

        result = await core.resume_sandbox("sb1")
        assert result.success is True
        assert "resumed" in result.message

    async def test_api_exception(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.patch_namespaced_deployment_scale.side_effect = ApiException(status=500)
        _setup_clients(core, apps=mock_apps)

        result = await core.resume_sandbox("sb1")
        assert result.success is False


# --- delete_all_sandboxes ---


class TestDeleteAllSandboxes:
    async def test_deletes_multiple(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        mock_custom = AsyncMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net, custom=mock_custom)

        deps = [mock_deployment(name="a"), mock_deployment(name="b")]
        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=deps):
            result = await core.delete_all_sandboxes()

        assert result.success is True
        assert "2" in result.message

    async def test_one_fails(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.delete_namespaced_deployment.side_effect = [
            None,
            ApiException(status=500, reason="fail"),
        ]
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        mock_custom = AsyncMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net, custom=mock_custom)

        deps = [mock_deployment(name="ok"), mock_deployment(name="fail")]
        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=deps):
            result = await core.delete_all_sandboxes()

        assert result.success is False

    async def test_no_sandboxes(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        mock_custom = AsyncMock()
        mock_custom.list_namespaced_custom_object.return_value = {"items": []}
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net, custom=mock_custom)

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=[]):
            result = await core.delete_all_sandboxes()

        assert result.success is True
        assert result.data == []

    async def test_exception_from_listing(self, core: K7Core):
        _setup_clients(core)

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, side_effect=Exception("boom")):
            result = await core.delete_all_sandboxes()

        assert result.success is False


# --- fork_sandbox ---


class TestForkSandbox:
    def _setup_fork(self, core):
        """Set up mock clients for fork tests and return them."""
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        # Source has no egress policy at all → the fork inherits open egress.
        mock_net = AsyncMock()
        mock_net.read_namespaced_network_policy.side_effect = ApiException(status=404)
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net, custom=mock_custom)
        core._detect_backend = AsyncMock(return_value="kata-qemu-longhorn")

        src_dep = MagicMock()
        src_dep.metadata.name = "src"
        src_dep.metadata.labels = {"app": "src", "katakate.org/sandbox": "src"}
        src_dep.spec.selector.match_labels = {"app": "src"}
        src_dep.spec.template.metadata.labels = {"app": "src", "katakate.org/sandbox": "src"}
        src_dep.spec.template.spec.volumes = []
        src_dep.spec.replicas = 1
        # 0 ready replicas ⇒ fork skips the pre-snapshot guest `sync`
        # (a paused/quiesced source needs no flush) — keeps these mocked
        # tests off the exec_command path.
        src_dep.status.ready_replicas = 0
        mock_apps.read_namespaced_deployment.return_value = src_dep

        src_pvc = MagicMock()
        src_pvc.spec.access_modes = ["ReadWriteOnce"]
        src_pvc.spec.resources = MagicMock()
        src_pvc.spec.storage_class_name = "longhorn"
        src_pvc.spec.volume_mode = "Filesystem"
        mock_v1.read_namespaced_persistent_volume_claim.return_value = src_pvc

        return mock_apps, mock_v1

    async def test_source_deployment_not_found(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.side_effect = ApiException(status=404)
        _setup_clients(core, apps=mock_apps, v1=AsyncMock())
        core._detect_backend = AsyncMock(return_value="kata-qemu-longhorn")

        result = await core.fork_sandbox("src", "dst")
        assert result.success is False

    async def test_source_pvc_not_found(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
        _setup_clients(core, apps=mock_apps, v1=mock_v1)
        core._detect_backend = AsyncMock(return_value="kata-qemu-longhorn")

        result = await core.fork_sandbox("src", "dst")
        assert result.success is False
        assert "not found" in result.error

    async def test_snapshot_creation_fails(self, core: K7Core):
        self._setup_fork(core)

        with patch.object(
            core,
            "_create_volume_snapshot",
            new_callable=AsyncMock,
            return_value=OperationResult(success=False, error="snap fail"),
        ):
            result = await core.fork_sandbox("src", "dst")

        assert result.success is False
        assert "snap fail" in result.error

    async def test_snapshot_not_ready(self, core: K7Core):
        self._setup_fork(core)

        with (
            patch.object(
                core,
                "_create_volume_snapshot",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True),
            ),
            patch.object(
                core,
                "_wait_for_snapshot_ready",
                new_callable=AsyncMock,
                return_value=OperationResult(success=False, error="timeout"),
            ),
        ):
            result = await core.fork_sandbox("src", "dst")

        assert result.success is False

    async def test_target_already_exists(self, core: K7Core):
        mock_apps, mock_v1 = self._setup_fork(core)
        mock_apps.create_namespaced_deployment.side_effect = ApiException(status=409)

        with (
            patch.object(
                core,
                "_create_volume_snapshot",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True),
            ),
            patch.object(
                core,
                "_wait_for_snapshot_ready",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True),
            ),
            patch.object(
                core,
                "_wait_for_pvc_bound",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True),
            ),
        ):
            result = await core.fork_sandbox("src", "dst")

        assert result.success is False
        assert "already exists" in result.error

    async def test_happy_path(self, core: K7Core):
        mock_apps, _ = self._setup_fork(core)

        with (
            patch.object(
                core,
                "_create_volume_snapshot",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True),
            ),
            patch.object(
                core,
                "_wait_for_snapshot_ready",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True),
            ),
            patch.object(
                core,
                "_wait_for_pvc_bound",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True),
            ),
        ):
            result = await core.fork_sandbox("src", "dst")

        assert result.success is True
        assert "Forked" in result.message
        mock_apps.create_namespaced_deployment.assert_called_once()

    def _patch_fork_storage(self, core: K7Core):
        return (
            patch.object(core, "_create_volume_snapshot", new_callable=AsyncMock, return_value=OperationResult(True)),
            patch.object(core, "_wait_for_snapshot_ready", new_callable=AsyncMock, return_value=OperationResult(True)),
            patch.object(core, "_wait_for_pvc_bound", new_callable=AsyncMock, return_value=OperationResult(True)),
        )

    async def test_fork_applies_network_policies(self, core: K7Core):
        """a fork used to come up with no policy at all."""
        self._setup_fork(core)
        core._custom_objects_client.get_namespaced_custom_object.side_effect = None
        core._custom_objects_client.get_namespaced_custom_object.return_value = {
            "spec": {"egress": [{"toFQDNs": [{"matchName": "example.com"}]}, {"toCIDR": ["10.0.0.0/8"]}]}
        }
        snap, ready, bound = self._patch_fork_storage(core)

        with (
            snap,
            ready,
            bound,
            patch.object(
                core,
                "_apply_sandbox_network_policies",
                new_callable=AsyncMock,
                return_value=OperationResult(True),
            ) as apply_policies,
        ):
            result = await core.fork_sandbox("src", "dst")

        assert result.success is True
        assert apply_policies.call_args.kwargs["name"] == "dst"
        assert apply_policies.call_args.kwargs["egress_whitelist"] == ["example.com", "10.0.0.0/8"]

    async def test_fork_of_open_egress_source_inherits_open_egress(self, core: K7Core):
        self._setup_fork(core)
        snap, ready, bound = self._patch_fork_storage(core)

        with (
            snap,
            ready,
            bound,
            patch.object(
                core,
                "_apply_sandbox_network_policies",
                new_callable=AsyncMock,
                return_value=OperationResult(True),
            ) as apply_policies,
        ):
            result = await core.fork_sandbox("src", "dst")

        assert result.success is True
        assert apply_policies.call_args.kwargs["egress_whitelist"] is None

    async def test_fork_policy_failure_rolls_back_deployment(self, core: K7Core):
        self._setup_fork(core)
        snap, ready, bound = self._patch_fork_storage(core)

        with (
            snap,
            ready,
            bound,
            patch.object(
                core,
                "_apply_sandbox_network_policies",
                new_callable=AsyncMock,
                return_value=OperationResult(False, error="boom"),
            ),
            patch.object(
                core,
                "_delete_sandbox_resources",
                new_callable=AsyncMock,
                return_value=OperationResult(True),
            ) as rollback,
        ):
            result = await core.fork_sandbox("src", "dst")

        assert result.success is False
        assert "rolled back" in result.error and "boom" in result.error
        rollback.assert_awaited_once_with("dst", "default")


class TestApplySandboxNetworkPolicies:
    """opt-in ingress, deny-all by default."""

    def _net(self, core: K7Core):
        mock_net = AsyncMock()
        _setup_clients(core, net=mock_net)
        return mock_net

    def _ingress_body(self, mock_net):
        for call in mock_net.create_namespaced_network_policy.call_args_list:
            body = call.kwargs["body"]
            if body.metadata.name.endswith("-deny-ingress"):
                return body
        raise AssertionError("no deny-ingress policy created")

    async def test_default_is_deny_all(self, core: K7Core):
        mock_net = self._net(core)
        result = await core._apply_sandbox_network_policies(name="sb", namespace="default", egress_whitelist=None)

        assert result.success is True
        body = self._ingress_body(mock_net)
        assert body.spec.policy_types == ["Ingress"]
        assert body.spec.ingress == []
        assert body.spec.pod_selector.match_labels == {"katakate.org/sandbox": "sb"}

    async def test_ports_without_sources_allow_same_namespace_sandboxes(self, core: K7Core):
        mock_net = self._net(core)
        await core._apply_sandbox_network_policies(
            name="sb", namespace="default", egress_whitelist=None, ingress_ports=[8000, 8001]
        )

        rule = self._ingress_body(mock_net).spec.ingress[0]
        assert [(p.protocol, p.port) for p in rule.ports] == [("TCP", 8000), ("TCP", 8001)]
        expr = rule._from[0].pod_selector.match_expressions[0]
        assert (expr.key, expr.operator) == ("katakate.org/sandbox", "Exists")

    async def test_explicit_sources_are_scoped(self, core: K7Core):
        mock_net = self._net(core)
        await core._apply_sandbox_network_policies(
            name="sb",
            namespace="default",
            egress_whitelist=None,
            ingress_ports=[8000],
            ingress_from=["sandbox:alice", "cidr:203.0.113.0/24"],
        )

        peers = self._ingress_body(mock_net).spec.ingress[0]._from
        assert peers[0].pod_selector.match_labels == {"katakate.org/sandbox": "alice"}
        assert peers[1].ip_block.cidr == "203.0.113.0/24"

    async def test_bad_source_fails_without_creating_the_policy(self, core: K7Core):
        mock_net = self._net(core)
        result = await core._apply_sandbox_network_policies(
            name="sb", namespace="default", egress_whitelist=None, ingress_ports=[8000], ingress_from=["nonsense"]
        )

        assert result.success is False
        assert "nonsense" in result.error
        mock_net.create_namespaced_network_policy.assert_not_called()

    async def test_policy_name_is_unchanged_when_it_carries_allow_rules(self, core: K7Core):
        """Renaming it would strand policies created by older k7 versions,
        which `_delete_sandbox_resources` deletes by name."""
        mock_net = self._net(core)
        await core._apply_sandbox_network_policies(
            name="sb", namespace="default", egress_whitelist=None, ingress_ports=[8000]
        )

        assert self._ingress_body(mock_net).metadata.name == "sb-deny-ingress"


class TestExposeSandboxPorts:
    """NodePort Service in front of matching ingress rules."""

    async def test_service_uses_local_external_traffic_policy(self, core: K7Core):
        mock_v1 = AsyncMock()
        created = MagicMock()
        port = MagicMock()
        port.port = 8000
        port.node_port = 31234
        created.spec.ports = [port]
        mock_v1.create_namespaced_service.return_value = created
        _setup_clients(core, v1=mock_v1)

        result = await core._apply_sandbox_expose_service("sb", "default", [8000])

        assert result.success is True
        assert result.data == {"node_ports": {8000: 31234}}
        body = mock_v1.create_namespaced_service.call_args.kwargs["body"]
        assert body.metadata.name == "sb-expose"
        assert body.spec.type == "NodePort"
        # Without Local the client's source IP is SNAT'd and cidr: rules are theatre.
        assert body.spec.external_traffic_policy == "Local"
        assert body.spec.selector == {"app": "sb"}

    async def test_unallocated_node_port_is_a_loud_failure(self, core: K7Core):
        mock_v1 = AsyncMock()
        created = MagicMock()
        port = MagicMock()
        port.port = 8000
        port.node_port = None
        created.spec.ports = [port]
        mock_v1.create_namespaced_service.return_value = created
        _setup_clients(core, v1=mock_v1)

        result = await core._apply_sandbox_expose_service("sb", "default", [8000])
        assert result.success is False
        assert "no node port" in result.error

    async def test_expose_without_matching_ingress_port_is_rejected(self, core: K7Core):
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=AsyncMock(), net=AsyncMock())

        cfg = SandboxConfig(name="sb1", image="alpine:3.20", ingress_ports=[8000], expose_ports=[9000])
        result = await core.create_sandbox(cfg)

        assert result.success is False
        assert "[9000]" in result.error
        mock_apps.create_namespaced_deployment.assert_not_called()


class TestReadSandboxIngressRules:
    async def test_deny_all_reads_back_as_none(self, core: K7Core):
        np = MagicMock()
        np.spec.ingress = []
        mock_net = AsyncMock()
        mock_net.read_namespaced_network_policy.return_value = np
        _setup_clients(core, net=mock_net)

        assert await core._read_sandbox_ingress_rules("sb", "default") == (None, None)

    async def test_round_trips_ports_and_sources(self, core: K7Core):
        """A fork must re-derive the source's policy from what it reads back."""
        core_net = AsyncMock()
        _setup_clients(core, net=core_net)
        await core._apply_sandbox_network_policies(
            name="sb",
            namespace="default",
            egress_whitelist=None,
            ingress_ports=[8000],
            ingress_from=["sandbox:alice", "namespace:team-a", "cidr:203.0.113.0/24"],
        )
        applied = [
            c.kwargs["body"]
            for c in core_net.create_namespaced_network_policy.call_args_list
            if c.kwargs["body"].metadata.name == "sb-deny-ingress"
        ][0]
        core_net.read_namespaced_network_policy.return_value = applied

        ports, sources = await core._read_sandbox_ingress_rules("sb", "default")
        assert ports == [8000]
        assert sources == ["sandbox:alice", "namespace:team-a", "cidr:203.0.113.0/24"]

    async def test_default_source_reads_back_as_empty_list(self, core: K7Core):
        core_net = AsyncMock()
        _setup_clients(core, net=core_net)
        await core._apply_sandbox_network_policies(
            name="sb", namespace="default", egress_whitelist=None, ingress_ports=[8000]
        )
        applied = [
            c.kwargs["body"]
            for c in core_net.create_namespaced_network_policy.call_args_list
            if c.kwargs["body"].metadata.name == "sb-deny-ingress"
        ][0]
        core_net.read_namespaced_network_policy.return_value = applied

        assert await core._read_sandbox_ingress_rules("sb", "default") == ([8000], [])


class TestReadSandboxEgressWhitelist:
    async def test_cilium_wildcard_round_trips(self, core: K7Core):
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.return_value = {
            "spec": {"egress": [{"toFQDNs": [{"matchPattern": "**.docker.com"}, {"matchName": "docker.io"}]}]}
        }
        _setup_clients(core, custom=mock_custom)

        assert await core._read_sandbox_egress_whitelist("sb", "default") == ["*.docker.com", "docker.io"]

    async def test_v1_netpol_cidrs(self, core: K7Core):
        np = MagicMock()
        peer = MagicMock()
        peer.ip_block.cidr = "10.0.0.0/8"
        rule = MagicMock()
        rule.to = [peer]
        np.spec.egress = [rule]
        mock_net = AsyncMock()
        mock_net.read_namespaced_network_policy.return_value = np
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        _setup_clients(core, net=mock_net, custom=mock_custom)

        assert await core._read_sandbox_egress_whitelist("sb", "default") == ["10.0.0.0/8"]

    async def test_block_all_egress_round_trips_as_empty_list(self, core: K7Core):
        """`egress_whitelist=[]` (block everything) must not degrade to None
        (open) when a fork inherits it."""
        np = MagicMock()
        np.spec.egress = []
        mock_net = AsyncMock()
        mock_net.read_namespaced_network_policy.return_value = np
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        _setup_clients(core, net=mock_net, custom=mock_custom)

        assert await core._read_sandbox_egress_whitelist("sb", "default") == []

    async def test_no_policy_means_open_egress(self, core: K7Core):
        mock_net = AsyncMock()
        mock_net.read_namespaced_network_policy.side_effect = ApiException(status=404)
        mock_custom = AsyncMock()
        mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
        _setup_clients(core, net=mock_net, custom=mock_custom)

        assert await core._read_sandbox_egress_whitelist("sb", "default") is None


# --- per-node agent forwarding ---


class TestK7dAgentForwarding:
    def _remote_pod(self, node: str = "node-remote", name: str = "sb1-pod"):
        pod = mock_pod(name=name, phase="Running")
        pod.spec.node_name = node
        return pod

    async def test_pause_forwards_to_remote_agent(self, core: K7Core):
        """k7d pause of a sandbox on another node is forwarded, not failed."""
        dep = mock_deployment(annotations={"k7.katakate.org/backend": "k7d"})
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[self._remote_pod()])
        _setup_clients(core, apps=mock_apps, v1=mock_v1)

        with (
            patch.dict("os.environ", {"K7_NODE_NAME": "node-local"}),
            patch.object(
                core,
                "_k7d_forward_vm_op",
                new_callable=AsyncMock,
                return_value=OperationResult(success=True, message="forwarded"),
            ) as fwd,
        ):
            result = await core.pause_sandbox("sb1")

        assert result.success is True
        fwd.assert_called_once()
        assert fwd.call_args.args[0] == "node-remote"
        assert fwd.call_args.args[1] == "pause"
        assert fwd.call_args.args[2]["pod_name"] == "sb1-pod"
        mock_apps.patch_namespaced_deployment.assert_awaited()

    async def test_fork_looks_up_on_remote_creates_locally(self, core: K7Core):
        """k7d fork Kubernetes writes stay on k7-api; agent only looks up the VM."""
        dep = mock_deployment(annotations={"k7.katakate.org/backend": "k7d"})
        dep.spec.template.metadata.labels = {"app": "sb1"}
        dep.metadata.labels = {"app": "sb1"}
        dep.spec.selector.match_labels = {"app": "sb1"}
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[self._remote_pod()])
        _setup_clients(core, apps=mock_apps, v1=mock_v1)
        vm = {"vm_id": "vm-1-0", "sandbox_id": "cri-src", "cluster_id": "cri-src"}

        with (
            patch.dict("os.environ", {"K7_NODE_NAME": "node-local"}),
            patch.object(core, "lookup_k7d_vm", new=AsyncMock(return_value=vm)),
            patch.object(core, "_wait_for_pod_container_started", new=AsyncMock(return_value="fork-pod")),
            patch.object(core, "_read_sandbox_egress_whitelist", new=AsyncMock(return_value=[])),
            patch.object(core, "_read_sandbox_ingress_rules", new=AsyncMock(return_value=([], []))),
            patch.object(core, "_apply_sandbox_network_policies", new=AsyncMock(return_value=OperationResult(True))),
            patch.object(core, "_k7d_forward_vm_op", new_callable=AsyncMock) as fwd,
        ):
            result = await core.fork_sandbox("sb1", "sb1-fork")

        assert result.success, result.error
        fwd.assert_not_called()
        mock_apps.create_namespaced_deployment.assert_awaited()
        new_dep = mock_apps.create_namespaced_deployment.call_args.kwargs["body"]
        assert new_dep.spec.template.spec.node_name == "node-remote"

    async def test_agent_refuses_to_reforward(self, core: K7Core):
        """An agent that resolves a sandbox to yet another node must fail
        loudly instead of forwarding again (no proxy loops)."""
        with patch.dict("os.environ", {"K7_AGENT": "1", "K7_NODE_NAME": "node-a"}):
            try:
                await core._k7d_forward_vm_op("node-b", "pause", {}, timeout=5)
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "refusing to re-forward" in str(e)

    async def test_missing_agent_pod_fails_loudly(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        _setup_clients(core, v1=mock_v1)

        try:
            await core._k7d_agent_base_url("node-remote")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "k7-agent" in str(e)

    async def test_missing_token_fails_loudly(self, core: K7Core, tmp_path):
        with patch.dict("os.environ", {"K7_AGENT_TOKEN_FILE": str(tmp_path / "absent")}):
            try:
                core._k7d_agent_token()
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "agent token" in str(e)

    async def test_nodes_storage_reports_unreachable_agent(self, core: K7Core, tmp_path):
        """A node with no Ready agent gets a loud error entry, not omission."""
        token_file = tmp_path / "agent_token"
        token_file.write_text("tok\n")
        mock_v1 = AsyncMock()
        mock_v1.list_node.return_value = SimpleNamespace(items=[_node_named("n1")])
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        _setup_clients(core, v1=mock_v1)

        with patch.dict("os.environ", {"K7_AGENT_TOKEN_FILE": str(token_file)}):
            result = await core.nodes_storage()

        assert "n1" in result
        assert "k7-agent" in result["n1"]["error"]


def _node_named(name: str):
    n = MagicMock()
    n.metadata.name = name
    return n


# --- exec_command ---


class TestExecCommand:
    async def test_sandbox_not_found(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.side_effect = ApiException(status=404)
        _setup_clients(core, apps=mock_apps, v1=AsyncMock())

        result = await core.exec_command("missing", "echo hi")
        assert result.exit_code == 1
        assert "not found" in result.stderr

    async def test_no_pods(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        _setup_clients(core, apps=mock_apps, v1=mock_v1)

        result = await core.exec_command("sb1", "echo hi")
        assert result.exit_code == 1
        assert "No pods" in result.stderr

    async def test_pod_not_running(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        pod = mock_pod(phase="Pending")
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        _setup_clients(core, apps=mock_apps, v1=mock_v1)

        result = await core.exec_command("sb1", "echo hi")
        assert result.exit_code == 1
        assert "not running" in result.stderr

    async def test_successful_exec(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        pod = mock_pod(phase="Running")
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        _setup_clients(core, apps=mock_apps, v1=mock_v1)

        with patch("k7.core.core.WsApiClient") as mock_ws_cls:
            mock_ws_ctx = AsyncMock()
            mock_ws_cls.return_value.__aenter__ = AsyncMock(return_value=mock_ws_ctx)
            mock_ws_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_v1_ws = AsyncMock()
            mock_v1_ws.connect_get_namespaced_pod_exec.return_value = "hello\n"

            with patch("k7.core.core.client.CoreV1Api", return_value=mock_v1_ws):
                result = await core.exec_command("sb1", "echo hello")

        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert result.duration_ms >= 0


# --- get_sandbox_metrics ---


class TestGetSandboxMetrics:
    async def test_running_pod_with_metrics(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_metrics = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=mock_v1, metrics=mock_metrics)

        dep = mock_deployment(name="sb1")
        pod = mock_pod(name="sb1-abc", phase="Running")
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        mock_metrics.get_namespaced_custom_object.return_value = {
            "containers": [{"usage": {"cpu": "100m", "memory": "128Mi"}}]
        }

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=[dep]):
            result = await core.get_sandbox_metrics(namespace="default")

        assert len(result) == 1
        assert result[0]["cpu_usage"] == "100m"
        assert result[0]["memory_usage"] == "128Mi"

    async def test_no_pods_skipped(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        mock_metrics = AsyncMock()
        _setup_clients(core, v1=mock_v1, metrics=mock_metrics)

        dep = mock_deployment(name="sb1")
        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=[dep]):
            result = await core.get_sandbox_metrics(namespace="default")

        assert result == []

    async def test_pod_not_running_skipped(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(phase="Pending")
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        mock_metrics = AsyncMock()
        _setup_clients(core, v1=mock_v1, metrics=mock_metrics)

        dep = mock_deployment(name="sb1")
        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, return_value=[dep]):
            result = await core.get_sandbox_metrics(namespace="default")

        assert result == []

    async def test_metrics_api_error(self, core: K7Core):
        _setup_clients(core)

        with patch.object(core, "_get_kata_sandboxes", new_callable=AsyncMock, side_effect=Exception("boom")):
            result = await core.get_sandbox_metrics(namespace="default")

        assert result == []


# --- _wait_for_pvc_bound ---


class TestWaitForPvcBound:
    async def test_bound_immediately(self, core: K7Core):
        mock_v1 = AsyncMock()
        pvc = MagicMock()
        pvc.status.phase = "Bound"
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pvc
        _setup_clients(core, v1=mock_v1)

        result = await core._wait_for_pvc_bound("pvc1", timeout=5)
        assert result.success is True

    async def test_bound_after_retry(self, core: K7Core):
        mock_v1 = AsyncMock()
        pending = MagicMock()
        pending.status.phase = "Pending"
        bound = MagicMock()
        bound.status.phase = "Bound"
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = [pending, bound]
        _setup_clients(core, v1=mock_v1)

        with patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock):
            result = await core._wait_for_pvc_bound("pvc1", timeout=10)

        assert result.success is True

    async def test_404_then_bound(self, core: K7Core):
        mock_v1 = AsyncMock()
        bound = MagicMock()
        bound.status.phase = "Bound"
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = [
            ApiException(status=404),
            bound,
        ]
        _setup_clients(core, v1=mock_v1)

        with patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock):
            result = await core._wait_for_pvc_bound("pvc1", timeout=10)

        assert result.success is True

    async def test_non_404_error(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=500)
        _setup_clients(core, v1=mock_v1)

        result = await core._wait_for_pvc_bound("pvc1", timeout=5)
        assert result.success is False
        assert "check error" in result.error

    async def test_timeout(self, core: K7Core):
        mock_v1 = AsyncMock()
        pending = MagicMock()
        pending.status.phase = "Pending"
        mock_v1.read_namespaced_persistent_volume_claim.return_value = pending
        _setup_clients(core, v1=mock_v1)

        elapsed = 0.0

        def advancing_time():
            nonlocal elapsed
            elapsed += 1.0
            return elapsed

        with (
            patch("k7.core.core.time.time", side_effect=advancing_time),
            patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await core._wait_for_pvc_bound("pvc1", timeout=2)

        assert result.success is False
        assert "not Bound" in result.error


# --- _wait_for_job ---


class TestWaitForJob:
    async def test_job_succeeds(self, core: K7Core):
        mock_batch = AsyncMock()
        job = MagicMock()
        job.status.succeeded = 1
        job.status.failed = 0
        mock_batch.read_namespaced_job.return_value = job
        _setup_clients(core, batch=mock_batch)

        result = await core._wait_for_job("j1", timeout=5)

        assert result.success is True

    async def test_job_fails(self, core: K7Core):
        mock_batch = AsyncMock()
        job = MagicMock()
        job.status.succeeded = 0
        job.status.failed = 1
        mock_batch.read_namespaced_job.return_value = job
        _setup_clients(core, batch=mock_batch)

        result = await core._wait_for_job("j1", timeout=5)

        assert result.success is False
        assert "failed" in result.error

    async def test_404_then_succeeds(self, core: K7Core):
        mock_batch = AsyncMock()
        job_ok = MagicMock()
        job_ok.status.succeeded = 1
        job_ok.status.failed = 0
        mock_batch.read_namespaced_job.side_effect = [ApiException(status=404), job_ok]
        _setup_clients(core, batch=mock_batch)

        with patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock):
            result = await core._wait_for_job("j1", timeout=10)

        assert result.success is True

    async def test_non_404_error(self, core: K7Core):
        mock_batch = AsyncMock()
        mock_batch.read_namespaced_job.side_effect = ApiException(status=500)
        _setup_clients(core, batch=mock_batch)

        result = await core._wait_for_job("j1", timeout=5)

        assert result.success is False
        assert "wait error" in result.error

    async def test_timeout(self, core: K7Core):
        mock_batch = AsyncMock()
        job = MagicMock()
        job.status.succeeded = 0
        job.status.failed = 0
        mock_batch.read_namespaced_job.return_value = job
        _setup_clients(core, batch=mock_batch)

        elapsed = 0.0

        def advancing_time():
            nonlocal elapsed
            elapsed += 1.0
            return elapsed

        with (
            patch("k7.core.core.time.time", side_effect=advancing_time),
            patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await core._wait_for_job("j1", timeout=2)

        assert result.success is False
        assert "did not complete" in result.error


# --- create_sandbox validation ---


class TestCreateSandboxValidation:
    async def test_invalid_limits_rejected(self, core: K7Core):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net)

        cfg = SandboxConfig(name="sb1", image="alpine:3.18", limits={"cpu": "0m"})
        result = await core.create_sandbox(cfg)
        assert result.success is False
        assert "Invalid resource limits" in result.error

    async def test_empty_env_file_rejected(self, core: K7Core, tmp_path):
        mock_apps = AsyncMock()
        mock_v1 = AsyncMock()
        mock_net = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=mock_v1, net=mock_net)

        env_file = tmp_path / ".env"
        env_file.write_text("# only comments\n\n")

        cfg = SandboxConfig(name="sb1", image="alpine:3.18", env_file=str(env_file))
        result = await core.create_sandbox(cfg)
        assert result.success is False
        assert "empty or invalid" in result.error

    async def test_ingress_from_without_ports_rejected(self, core: K7Core):
        _setup_clients(core, apps=AsyncMock(), v1=AsyncMock(), net=AsyncMock())

        cfg = SandboxConfig(name="sb1", image="alpine:3.18", ingress_from=["sandbox:alice"])
        result = await core.create_sandbox(cfg)
        assert result.success is False
        assert "ingress_from requires ingress_ports" in result.error

    async def test_unparseable_ingress_source_rejected_before_provisioning(self, core: K7Core):
        mock_apps = AsyncMock()
        _setup_clients(core, apps=mock_apps, v1=AsyncMock(), net=AsyncMock())

        cfg = SandboxConfig(name="sb1", image="alpine:3.18", ingress_ports=[8000], ingress_from=["nonsense"])
        result = await core.create_sandbox(cfg)
        assert result.success is False
        assert "nonsense" in result.error
        mock_apps.create_namespaced_deployment.assert_not_called()


# --- _wait_for_pod_container_started ---


class TestWaitForPodContainerStarted:
    async def test_returns_pod_name_when_running(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(name="sb1-abc", phase="Running")
        cs = MagicMock()
        cs.name = "sandbox"
        cs.state.running = True
        pod.status.container_statuses = [cs]
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        _setup_clients(core, v1=mock_v1)

        result = await core._wait_for_pod_container_started("sb1", "default", timeout_seconds=5)
        assert result == "sb1-abc"

    async def test_returns_none_on_timeout_no_pods(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        _setup_clients(core, v1=mock_v1)

        elapsed = 0.0

        def advancing_time():
            nonlocal elapsed
            elapsed += 1.0
            return elapsed

        with (
            patch("k7.core.core.time.time", side_effect=advancing_time),
            patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await core._wait_for_pod_container_started("sb1", "default", timeout_seconds=2)

        assert result is None

    async def test_waits_for_running_phase(self, core: K7Core):
        mock_v1 = AsyncMock()
        pending_pod = mock_pod(name="sb1-abc", phase="Pending")

        running_pod = mock_pod(name="sb1-abc", phase="Running")
        cs = MagicMock()
        cs.name = "sandbox"
        cs.state.running = True
        running_pod.status.container_statuses = [cs]

        mock_v1.list_namespaced_pod.side_effect = [
            MagicMock(items=[pending_pod]),
            MagicMock(items=[running_pod]),
        ]
        _setup_clients(core, v1=mock_v1)

        with patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock):
            result = await core._wait_for_pod_container_started("sb1", "default", timeout_seconds=60)

        assert result == "sb1-abc"

    async def test_returns_none_when_container_not_started(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(name="sb1-abc", phase="Running")
        cs = MagicMock()
        cs.name = "sandbox"
        cs.state.running = None
        cs.state.waiting = MagicMock()
        pod.status.container_statuses = [cs]
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        _setup_clients(core, v1=mock_v1)

        elapsed = 0.0

        def advancing_time():
            nonlocal elapsed
            elapsed += 1.0
            return elapsed

        with (
            patch("k7.core.core.time.time", side_effect=advancing_time),
            patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await core._wait_for_pod_container_started("sb1", "default", timeout_seconds=2)

        assert result is None


# --- _list_backends_per_node ---


def _node(name: str, labels: dict[str, str], taints: list | None = None):
    n = MagicMock()
    n.metadata.name = name
    n.metadata.labels = labels
    n.spec.taints = taints or []
    return n


class TestListBackendsPerNode:
    async def test_extracts_backend_labels(self, core: K7Core):
        mock_v1 = AsyncMock()
        nodes = [
            _node(
                "node1",
                {
                    "k7.katakate.org/backend-kata-qemu-longhorn": "true",
                    "k7.katakate.org/backend-kata-firecracker-devmapper": "true",
                    "kubernetes.io/hostname": "node1",
                },
            ),
            _node("node2", {"k7.katakate.org/backend-kata-qemu-longhorn": "true"}),
            _node("node3", {}),
        ]
        mock_v1.list_node.return_value = SimpleNamespace(items=nodes)
        _setup_clients(core, v1=mock_v1)

        result = await core._list_backends_per_node()
        assert result["node1"] == ["kata-firecracker-devmapper", "kata-qemu-longhorn"]
        assert result["node2"] == ["kata-qemu-longhorn"]
        assert result["node3"] == []

    async def test_skips_label_with_false_value(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.list_node.return_value = SimpleNamespace(
            items=[_node("n", {"k7.katakate.org/backend-kata-qemu-longhorn": "false"})]
        )
        _setup_clients(core, v1=mock_v1)

        result = await core._list_backends_per_node()
        assert result["n"] == []


class TestListClusterNodes:
    async def test_hostname_backends_and_tenant(self, core: K7Core):
        taint = SimpleNamespace(key=K7_TENANT_LABEL, value="acme", effect="NoSchedule")
        mock_v1 = AsyncMock()
        mock_v1.list_node.return_value = SimpleNamespace(
            items=[
                _node(
                    "k7-node-01",
                    {
                        "kubernetes.io/hostname": "k7-node-01",
                        "k7.katakate.org/backend-k7d": "true",
                        K7_TENANT_LABEL: "acme",
                    },
                    taints=[taint],
                ),
                _node("k7-node-02", {"kubernetes.io/hostname": "k7-node-02"}),
            ]
        )
        _setup_clients(core, v1=mock_v1)
        rows = await core.list_cluster_nodes()
        by_name = {r["name"]: r for r in rows}
        assert by_name["k7-node-01"]["hostname"] == "k7-node-01"
        assert by_name["k7-node-01"]["backends"] == ["k7d"]
        assert by_name["k7-node-01"]["tenant"] == "acme"
        assert by_name["k7-node-02"]["hostname"] == "k7-node-02"
        assert by_name["k7-node-02"]["backends"] == []
        assert by_name["k7-node-02"]["tenant"] == ""


# --- _check_scheduling ---


def _event(reason: str, message: str):
    e = MagicMock()
    e.reason = reason
    e.message = message
    return e


class TestCheckScheduling:
    async def test_running_pod_returns_success(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(name="sb1-x", phase="Running")
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        _setup_clients(core, v1=mock_v1)

        result = await core._check_scheduling("sb1", "default", "kata-qemu-longhorn", timeout_seconds=1)
        assert result.success is True

    async def test_failed_scheduling_returns_explanatory_error(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(name="sb1-x", phase="Pending")
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        events = SimpleNamespace(
            items=[
                _event(
                    "FailedScheduling",
                    "0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector.",
                )
            ]
        )
        mock_v1.list_namespaced_event.return_value = events
        # Two nodes labeled with FD only — no QL anywhere
        mock_v1.list_node.return_value = SimpleNamespace(
            items=[
                _node("node1", {"k7.katakate.org/backend-kata-firecracker-devmapper": "true"}),
                _node("node2", {"k7.katakate.org/backend-kata-firecracker-devmapper": "true"}),
            ]
        )
        _setup_clients(core, v1=mock_v1)

        result = await core._check_scheduling("sb1", "default", "kata-qemu-longhorn", timeout_seconds=1)
        assert result.success is False
        assert "kata-qemu-longhorn" in result.error
        assert "kata-firecracker-devmapper" in result.error
        assert "node1" in result.error
        assert "node2" in result.error

    async def test_no_pod_yet_eventually_returns_success(self, core: K7Core):
        mock_v1 = AsyncMock()
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        _setup_clients(core, v1=mock_v1)

        elapsed = 0.0

        def advancing_time():
            nonlocal elapsed
            elapsed += 1.0
            return elapsed

        with (
            patch("k7.core.core.time.time", side_effect=advancing_time),
            patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await core._check_scheduling("sb1", "default", "kata-qemu-longhorn", timeout_seconds=2)

        assert result.success is True

    async def test_unrelated_event_does_not_trigger(self, core: K7Core):
        mock_v1 = AsyncMock()
        pod = mock_pod(name="sb1-x", phase="Pending")
        mock_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        mock_v1.list_namespaced_event.return_value = SimpleNamespace(
            items=[_event("Scheduled", "Successfully assigned default/sb1-x to node1")]
        )
        _setup_clients(core, v1=mock_v1)

        elapsed = 0.0

        def advancing_time():
            nonlocal elapsed
            elapsed += 1.0
            return elapsed

        with (
            patch("k7.core.core.time.time", side_effect=advancing_time),
            patch("k7.core.core.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await core._check_scheduling("sb1", "default", "kata-qemu-longhorn", timeout_seconds=2)

        assert result.success is True


# --- _cilium_available / _apply_cilium_egress_policy ---


class TestCiliumAvailable:
    async def test_returns_true_when_crd_exists(self, core: K7Core):
        fake_api_ext = AsyncMock()
        fake_api_ext.read_custom_resource_definition.return_value = MagicMock()
        _setup_clients(core, apiext=fake_api_ext)
        assert await core._cilium_available() is True

    async def test_returns_false_when_crd_missing(self, core: K7Core):
        fake_api_ext = AsyncMock()
        fake_api_ext.read_custom_resource_definition.side_effect = ApiException(status=404)
        _setup_clients(core, apiext=fake_api_ext)
        assert await core._cilium_available() is False


class TestApplyCiliumEgressPolicy:
    async def test_builds_policy_with_fqdns_and_cidrs(self, core: K7Core):
        mock_custom = AsyncMock()
        _setup_clients(core, custom=mock_custom)

        result = await core._apply_cilium_egress_policy(
            sandbox_name="sb1",
            namespace="default",
            cidrs=["10.0.0.0/8"],
            fqdns=["api.openai.com", "*.huggingface.co"],
        )
        assert result.success is True
        args, kwargs = mock_custom.create_namespaced_custom_object.call_args
        body = kwargs["body"]
        assert body["kind"] == "CiliumNetworkPolicy"
        assert body["metadata"]["name"] == "sb1-egress"
        assert body["spec"]["endpointSelector"]["matchLabels"]["katakate.org/sandbox"] == "sb1"
        egress = body["spec"]["egress"]
        # DNS rule first, then FQDN rule, then CIDR rule
        assert any("toFQDNs" in r for r in egress)
        fqdn_rule = next(r for r in egress if "toFQDNs" in r)
        assert {"matchName": "api.openai.com"} in fqdn_rule["toFQDNs"]
        # Leading `*.` must be translated to Cilium's multi-label wildcard
        # `**.` — a single `*` never crosses label boundaries, silently
        # breaking nested subdomains like production.cloudfront.docker.com.
        assert {"matchPattern": "**.huggingface.co"} in fqdn_rule["toFQDNs"]
        assert any("toCIDR" in r and r["toCIDR"] == ["10.0.0.0/8"] for r in egress)
        # DNS proxy allowance is present
        assert any("toPorts" in r and any("rules" in p and "dns" in p["rules"] for p in r["toPorts"]) for r in egress)

    async def test_leading_wildcard_becomes_multilabel_pattern(self, core: K7Core):
        mock_custom = AsyncMock()
        _setup_clients(core, custom=mock_custom)
        result = await core._apply_cilium_egress_policy(
            sandbox_name="sb1",
            namespace="default",
            cidrs=[],
            fqdns=["*.docker.com", "**.already.com", "api-*.foo.com"],
        )
        assert result.success is True
        body = mock_custom.create_namespaced_custom_object.call_args.kwargs["body"]
        fqdn_rule = next(r for r in body["spec"]["egress"] if "toFQDNs" in r)
        assert {"matchPattern": "**.docker.com"} in fqdn_rule["toFQDNs"]
        # Explicit `**.` is passed through untouched; mid-label wildcards too.
        assert {"matchPattern": "**.already.com"} in fqdn_rule["toFQDNs"]
        assert {"matchPattern": "api-*.foo.com"} in fqdn_rule["toFQDNs"]

    async def test_replaces_on_409(self, core: K7Core):
        mock_custom = AsyncMock()
        mock_custom.create_namespaced_custom_object.side_effect = ApiException(status=409)
        _setup_clients(core, custom=mock_custom)

        result = await core._apply_cilium_egress_policy("sb1", "default", [], ["api.openai.com"])
        assert result.success is True
        mock_custom.replace_namespaced_custom_object.assert_called_once()

    async def test_non_conflict_error_propagates(self, core: K7Core):
        mock_custom = AsyncMock()
        mock_custom.create_namespaced_custom_object.side_effect = ApiException(status=500)
        _setup_clients(core, custom=mock_custom)

        result = await core._apply_cilium_egress_policy("sb1", "default", [], ["api.openai.com"])
        assert result.success is False
        assert "CiliumNetworkPolicy" in result.error


# --- kubernetes_asyncio ApiClient lifecycle ---


class TestApiClientLifecycle:
    async def test_typed_clients_share_one_api_client(self, core: K7Core):
        api = MagicMock()
        api.close = AsyncMock()
        with (
            patch("k7.core.core.client.ApiClient", return_value=api) as api_cls,
            patch("k7.core.core.client.CoreV1Api") as core_cls,
            patch("k7.core.core.client.AppsV1Api") as apps_cls,
        ):
            await core._get_core_v1_client()
            await core._get_apps_v1_client()
            api_cls.assert_called_once()
            core_cls.assert_called_once_with(api)
            apps_cls.assert_called_once_with(api)
            await core.aclose()
        api.close.assert_awaited_once()
        assert core._api_client is None
        assert core._core_v1_client is None
        assert core._apps_v1_client is None

    async def test_aclose_is_idempotent(self, core: K7Core):
        await core.aclose()
        await core.aclose()

    async def test_aclose_allows_a_new_session(self, core: K7Core):
        first = MagicMock()
        first.close = AsyncMock()
        second = MagicMock()
        second.close = AsyncMock()
        with (
            patch("k7.core.core.client.ApiClient", side_effect=[first, second]),
            patch("k7.core.core.client.CoreV1Api"),
        ):
            await core._get_core_v1_client()
            await core.aclose()
            first.close.assert_awaited_once()
            await core._get_core_v1_client()
            await core.aclose()
            second.close.assert_awaited_once()


class TestCreateSnapshotMessage:
    async def test_named_snapshot_keeps_create_message(self, core: K7Core):
        """Regression: quiesced success used to drop the VolumeSnapshot name.

        ``k7 snapshot create`` echoes ``result.message``; empty stdout failed
        ``TestSnapshotCrud`` with ``assert snap in cp.stdout``.
        """
        dep = MagicMock()
        dep.metadata.annotations = {}
        dep.status.ready_replicas = 1
        mock_apps = AsyncMock()
        mock_apps.read_namespaced_deployment.return_value = dep
        _setup_clients(core, apps=mock_apps)

        with (
            patch.object(core, "_detect_backend", new=AsyncMock(return_value="kata-qemu-longhorn")),
            patch.object(
                core,
                "_create_volume_snapshot",
                new=AsyncMock(
                    return_value=OperationResult(success=True, message="Snapshot snap1 created for PVC sb-root-lh")
                ),
            ),
            patch.object(
                core,
                "exec_command",
                new=AsyncMock(return_value=ExecResult(exit_code=0, stdout="", stderr="", duration_ms=0)),
            ),
        ):
            result = await core.create_snapshot("sb", "snap1")
        assert result.success, result.error
        assert "snap1" in result.message
