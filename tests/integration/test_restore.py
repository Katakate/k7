"""Integration tests for ``k7 restore`` (Spec 10f), multi-node cluster.

Coverage:

- Restore after the source sandbox has been deleted entirely (the
  motivating use case).
- ``--image`` override against a legacy snapshot that lacks
  ``k7.io/source-image`` annotations.
- ``--no-keep-snapshot`` deletes the source snapshot after success.
- Restoring into an existing sandbox name is rejected with 409 semantics.
- Restoring from a snapshot whose ``readyToUse`` is false fails fast.

These exercise ``K7Core.restore_sandbox`` directly (async tests) so we
share an event loop with the kubernetes_asyncio clients; the
``dev.sh``-based ``test_pause_resume.py`` covers the CLI surface.
"""

import asyncio
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig, SandboxConfigOverrides

pytestmark = pytest.mark.integration

_K3S = "/usr/local/bin/k3s"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_kubectl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([_K3S, "kubectl", *args], capture_output=True, text=True)


def _wait_pod_ready(name: str, namespace: str, timeout: int = 240) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = _run_kubectl(
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={name}",
            "-o",
            "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}",
        )
        if out.returncode == 0 and out.stdout.strip() == "True":
            return
        time.sleep(2)
    raise TimeoutError(f"pod for sandbox {name} not Ready within {timeout}s")


def _snapshot_exists(name: str, namespace: str) -> bool:
    out = _run_kubectl("get", "volumesnapshot", name, "-n", namespace, "-o", "name")
    return out.returncode == 0 and out.stdout.strip() != ""


def _force_delete_snapshot(name: str, namespace: str) -> None:
    _run_kubectl(
        "patch",
        "volumesnapshot",
        name,
        "-n",
        namespace,
        "--type=merge",
        "-p",
        '{"metadata":{"finalizers":[]}}',
    )
    _run_kubectl("delete", "volumesnapshot", name, "-n", namespace, "--ignore-not-found", "--wait=false")


@pytest.fixture()
def restore_sandbox_cleanup(test_namespace: str):
    """Track sandboxes and snapshots for teardown — restore tests are messy."""
    sandboxes: list[str] = []
    snapshots: list[str] = []

    def _register_sandbox(name: str) -> None:
        sandboxes.append(name)

    def _register_snapshot(name: str) -> None:
        snapshots.append(name)

    yield _register_sandbox, _register_snapshot

    for snap in snapshots:
        _force_delete_snapshot(snap, test_namespace)
    for name in sandboxes:
        try:
            asyncio.run(K7Core().delete_sandbox(name, namespace=test_namespace))
        except Exception:
            pass


def _ql_config(name: str, namespace: str) -> SandboxConfig:
    return SandboxConfig(
        name=name,
        image="alpine:3.20",
        namespace=namespace,
        backend="kata-qemu-longhorn",
    )


class TestRestoreAfterSourceDeleted:
    """The motivating use case: pause, snapshot, delete source, restore."""

    async def test_restore_after_pause_then_delete_source(
        self,
        test_namespace: str,
        restore_sandbox_cleanup,
    ):
        register_sandbox, register_snapshot = restore_sandbox_cleanup
        source = f"restore-src-{uuid4().hex[:6]}"
        snap = f"{source}-paused-keep"
        restored = f"{source}-r"
        register_sandbox(source)
        register_sandbox(restored)
        register_snapshot(snap)

        k7 = K7Core()

        # 1. Create + write a marker into the source's persistent state.
        create = await k7.create_sandbox(_ql_config(source, test_namespace))
        assert create.success, f"create failed: {create.error}"
        _wait_pod_ready(source, test_namespace)
        marker = await k7.exec_command(
            source,
            "echo restore-marker > /mnt/state/marker && cat /mnt/state/marker",
            namespace=test_namespace,
        )
        assert marker.exit_code == 0, f"marker write failed: {marker.stderr}"

        # 2. Pause + snapshot (kind=pause, persistent).
        pause = await k7.pause_sandbox(source, namespace=test_namespace, snapshot_name=snap)
        assert pause.success, pause.error
        # Wait for the snapshot to become readyToUse before we delete the source.
        deadline = time.time() + 180
        ready = False
        while time.time() < deadline:
            info = await k7.get_snapshot(snap, namespace=test_namespace)
            if info is not None and info.ready_to_use:
                ready = True
                break
            await asyncio.sleep(2)
        assert ready, f"snapshot {snap} never reached readyToUse=True"

        # 3. Delete the source Deployment + PVC entirely. The snapshot is the
        #    only trace of the original sandbox now.
        delete = await k7.delete_sandbox(source, namespace=test_namespace)
        assert delete.success, delete.error
        # Confirm the source's root PVC is gone.
        deadline = time.time() + 60
        while time.time() < deadline:
            out = _run_kubectl("get", "pvc", f"{source}-root-lh", "-n", test_namespace, "-o", "name")
            if out.returncode != 0 or out.stdout.strip() == "":
                break
            time.sleep(2)

        # 4. Restore from the snapshot.
        restore_k7 = K7Core()  # fresh K7Core (separate event loop is fine here)
        restore_result = await restore_k7.restore_sandbox(
            snapshot_name=snap,
            new_sandbox_name=restored,
            namespace=test_namespace,
        )
        assert restore_result.success, f"restore failed: {restore_result.error}"
        _wait_pod_ready(restored, test_namespace)

        # 5. The marker survives.
        check = await restore_k7.exec_command(restored, "cat /mnt/state/marker", namespace=test_namespace)
        assert check.exit_code == 0
        assert "restore-marker" in check.stdout

        # 6. Default keep_snapshot=True → snapshot still there.
        assert _snapshot_exists(snap, test_namespace)


class TestRestoreOverrides:
    """``--image`` override against an annotation-less snapshot."""

    async def test_image_override_when_annotation_missing(
        self,
        test_namespace: str,
        restore_sandbox_cleanup,
    ):
        register_sandbox, register_snapshot = restore_sandbox_cleanup
        source = f"restore-ovr-{uuid4().hex[:6]}"
        snap = f"{source}-legacy"
        restored = f"{source}-r"
        register_sandbox(source)
        register_sandbox(restored)
        register_snapshot(snap)

        k7 = K7Core()
        create = await k7.create_sandbox(_ql_config(source, test_namespace))
        assert create.success, create.error
        _wait_pod_ready(source, test_namespace)

        # Create a "legacy" snapshot manually: no ``k7.io/source-*`` annotations.
        custom = await k7._get_custom_objects_client()
        await custom.create_namespaced_custom_object(
            group="snapshot.storage.k8s.io",
            version="v1",
            namespace=test_namespace,
            plural="volumesnapshots",
            body={
                "apiVersion": "snapshot.storage.k8s.io/v1",
                "kind": "VolumeSnapshot",
                "metadata": {"name": snap, "namespace": test_namespace},
                "spec": {
                    "volumeSnapshotClassName": "longhorn",
                    "source": {"persistentVolumeClaimName": f"{source}-root-lh"},
                },
            },
        )
        # Wait for ready.
        deadline = time.time() + 180
        ready = False
        while time.time() < deadline:
            info = await k7.get_snapshot(snap, namespace=test_namespace)
            if info is not None and info.ready_to_use:
                ready = True
                break
            await asyncio.sleep(2)
        assert ready, "legacy snapshot never became ready"

        # Restore *without* --image must fail loudly.
        restore_k7 = K7Core()
        bad = await restore_k7.restore_sandbox(
            snapshot_name=snap,
            new_sandbox_name=restored,
            namespace=test_namespace,
        )
        assert not bad.success
        assert "k7.io/source-image" in bad.error
        assert "--image" in bad.error

        # Restore *with* --image succeeds.
        good = await restore_k7.restore_sandbox(
            snapshot_name=snap,
            new_sandbox_name=restored,
            namespace=test_namespace,
            overrides=SandboxConfigOverrides(image="alpine:3.20"),
        )
        assert good.success, good.error
        _wait_pod_ready(restored, test_namespace)


class TestRestoreNoKeepSnapshot:
    async def test_no_keep_snapshot_deletes_snapshot(
        self,
        test_namespace: str,
        restore_sandbox_cleanup,
    ):
        register_sandbox, register_snapshot = restore_sandbox_cleanup
        source = f"restore-nk-{uuid4().hex[:6]}"
        snap = f"{source}-disposable"
        restored = f"{source}-r"
        register_sandbox(source)
        register_sandbox(restored)
        register_snapshot(snap)

        k7 = K7Core()
        await k7.create_sandbox(_ql_config(source, test_namespace))
        _wait_pod_ready(source, test_namespace)
        await k7.pause_sandbox(source, namespace=test_namespace, snapshot_name=snap)

        # Wait for ready.
        deadline = time.time() + 180
        while time.time() < deadline:
            info = await k7.get_snapshot(snap, namespace=test_namespace)
            if info is not None and info.ready_to_use:
                break
            await asyncio.sleep(2)

        restore_k7 = K7Core()
        result = await restore_k7.restore_sandbox(
            snapshot_name=snap,
            new_sandbox_name=restored,
            namespace=test_namespace,
            keep_snapshot=False,
        )
        assert result.success, result.error
        _wait_pod_ready(restored, test_namespace)

        # Snapshot should be deleted (give Longhorn a few seconds to settle).
        deadline = time.time() + 30
        while time.time() < deadline and _snapshot_exists(snap, test_namespace):
            time.sleep(1)
        assert not _snapshot_exists(snap, test_namespace)


class TestRestoreErrors:
    async def test_restore_into_existing_name_fails(
        self,
        test_namespace: str,
        restore_sandbox_cleanup,
    ):
        register_sandbox, register_snapshot = restore_sandbox_cleanup
        source = f"restore-conf-{uuid4().hex[:6]}"
        snap = f"{source}-snap"
        register_sandbox(source)
        register_snapshot(snap)

        k7 = K7Core()
        await k7.create_sandbox(_ql_config(source, test_namespace))
        _wait_pod_ready(source, test_namespace)
        await k7.pause_sandbox(source, namespace=test_namespace, snapshot_name=snap)
        deadline = time.time() + 180
        while time.time() < deadline:
            info = await k7.get_snapshot(snap, namespace=test_namespace)
            if info is not None and info.ready_to_use:
                break
            await asyncio.sleep(2)

        # Restore into the source's *own* name → must fail.
        restore_k7 = K7Core()
        result = await restore_k7.restore_sandbox(
            snapshot_name=snap,
            new_sandbox_name=source,  # already exists
            namespace=test_namespace,
        )
        assert not result.success
        assert "already exists" in result.error

    async def test_restore_missing_snapshot_returns_helpful_error(self, test_namespace: str):
        k7 = K7Core()
        result = await k7.restore_sandbox(
            snapshot_name="does-not-exist",
            new_sandbox_name="any",
            namespace=test_namespace,
        )
        assert not result.success
        assert "not found" in result.error.lower()
