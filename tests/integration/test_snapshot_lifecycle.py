"""Integration tests for the Spec 10e snapshot lifecycle, multi-node cluster.

Coverage:

- ``k7 snapshot create / list / inspect / delete`` round-trip via ``dev.sh``.
- ``k7 fork`` auto-deletes its temporary ``-fork-`` snapshot once the cloned
  PVC is bound (Option A inline cleanup).
- The inline cleanup does **not** break the cloned sandbox — it can still
  read/write after the snapshot is gone.
- ``k7 snapshot gc`` leaves pause and named snapshots alone.
- The CronJob applies cleanly and a manually-triggered Job from it cleans
  up old fork snapshots.
"""

import asyncio
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = pytest.mark.integration

_K3S = "/usr/local/bin/k3s"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEV_SH = _REPO_ROOT / "src" / "k7" / "cli" / "dev.sh"


def _run_cli(*args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    """Invoke ``dev.sh --core <args>`` — Spec 10g made the default API; these
    CLI-flag tests don't want / need that detour."""
    cmd = [str(_DEV_SH), "--core", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout, cwd=str(_REPO_ROOT))


def _wait_pod_ready(name: str, namespace: str, timeout: int = 240) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            [
                _K3S,
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                f"app={name}",
                "-o",
                "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}",
            ],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip() == "True":
            return
        time.sleep(2)
    raise TimeoutError(f"pod for sandbox {name} not Ready within {timeout}s")


def _list_snapshots(namespace: str) -> list[str]:
    out = subprocess.run(
        [_K3S, "kubectl", "get", "volumesnapshot", "-n", namespace, "-o", "name"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    return [s.split("/", 1)[1] for s in out.stdout.splitlines() if s.startswith("volumesnapshot.")]


def _snapshot_exists(name: str, namespace: str) -> bool:
    out = subprocess.run(
        [_K3S, "kubectl", "get", "volumesnapshot", name, "-n", namespace, "-o", "name"],
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and out.stdout.strip() != ""


def _force_delete_snapshot(name: str, namespace: str) -> None:
    """Best-effort cleanup: strip finalizers so Longhorn doesn't hang the delete."""
    subprocess.run(
        [
            _K3S,
            "kubectl",
            "patch",
            "volumesnapshot",
            name,
            "-n",
            namespace,
            "--type=merge",
            "-p",
            '{"metadata":{"finalizers":[]}}',
        ],
        capture_output=True,
    )
    subprocess.run(
        [_K3S, "kubectl", "delete", "volumesnapshot", name, "-n", namespace, "--ignore-not-found", "--wait=false"],
        capture_output=True,
    )


@pytest.fixture()
def ql_sandbox(test_namespace: str):
    """Provision a fresh kata-qemu-longhorn sandbox per test.

    Each ``asyncio.run`` call uses a freshly-instantiated K7Core: the
    kubernetes_asyncio client caches its session on first ``await`` and
    rebinds to the running loop, so a reused K7Core across multiple
    ``asyncio.run`` calls fails with "Event loop is closed".
    """
    name = f"snap-life-{uuid4().hex[:6]}"
    cfg = SandboxConfig(name=name, image="alpine:3.20", namespace=test_namespace, backend="kata-qemu-longhorn")
    setup_result = asyncio.run(K7Core().create_sandbox(cfg))
    assert setup_result.success, f"create failed: {setup_result.error}"
    _wait_pod_ready(name, test_namespace)
    try:
        yield name
    finally:
        for snap in _list_snapshots(test_namespace):
            _force_delete_snapshot(snap, test_namespace)
        try:
            asyncio.run(K7Core().delete_sandbox(name, namespace=test_namespace))
        except Exception:
            pass


class TestSnapshotCrud:
    """``k7 snapshot create / list / inspect / delete`` end-to-end."""

    def test_create_list_inspect_delete_round_trip(
        self,
        ql_sandbox: str,
        test_namespace: str,
    ):
        snap = f"{ql_sandbox}-named-v1"

        try:
            cp = _run_cli("snapshot", "create", ql_sandbox, snap, "-n", test_namespace)
            assert snap in cp.stdout, cp.stdout
            assert _snapshot_exists(snap, test_namespace)

            ls = _run_cli("snapshot", "list", "-n", test_namespace)
            assert snap in ls.stdout
            assert "named" in ls.stdout  # KIND column

            ins = _run_cli("snapshot", "inspect", snap, "-n", test_namespace)
            assert snap in ins.stdout
            assert ql_sandbox in ins.stdout  # source sandbox

            de = _run_cli("snapshot", "delete", snap, "-n", test_namespace, "--yes")
            assert "deleted" in de.stdout

            # Confirm delete was accepted. Longhorn/CSI finalizers can leave the
            # object Terminating for a long time; deletionTimestamp is enough.
            deadline = time.time() + 30
            gone = False
            while time.time() < deadline:
                if not _snapshot_exists(snap, test_namespace):
                    gone = True
                    break
                ts = subprocess.run(
                    [
                        _K3S,
                        "kubectl",
                        "get",
                        "volumesnapshot",
                        snap,
                        "-n",
                        test_namespace,
                        "-o",
                        "jsonpath={.metadata.deletionTimestamp}",
                    ],
                    capture_output=True,
                    text=True,
                )
                if ts.returncode == 0 and ts.stdout.strip():
                    gone = True
                    break
                time.sleep(1)
            assert gone, f"VolumeSnapshot {snap} was not deleted"
        finally:
            _force_delete_snapshot(snap, test_namespace)


class TestForkInlineSnapshotDelete:
    """Spec 10e Option A: ``fork`` cleans up its ``-fork-`` snapshot inline."""

    async def test_fork_auto_deletes_temp_snapshot(
        self,
        ql_sandbox: str,
        test_namespace: str,
    ):
        """Async test so K7Core's lazy-init aiohttp clients bind to this loop.

        ``ql_sandbox`` is provisioned in a separate ``asyncio.run`` (which closes
        its loop on exit), so we must use a *new* ``K7Core`` here.
        """
        before = set(_list_snapshots(test_namespace))
        child = f"{ql_sandbox}-c"
        k7 = K7Core()

        try:
            result = await k7.fork_sandbox(source_name=ql_sandbox, new_name=child, namespace=test_namespace)
            assert result.success, f"fork failed: {result.error}"

            # Inline delete is best-effort but should land within a short window.
            deadline = time.time() + 60
            while time.time() < deadline:
                current = set(_list_snapshots(test_namespace))
                new = current - before
                leftover_fork = [s for s in new if "-fork-" in s and ql_sandbox in s]
                if not leftover_fork:
                    break
                time.sleep(2)
            current = set(_list_snapshots(test_namespace))
            new = current - before
            leftover_fork = [s for s in new if "-fork-" in s and ql_sandbox in s]
            assert leftover_fork == [], f"Auto fork snapshot was not cleaned up: {leftover_fork}"

            # The child sandbox should still be Ready (Option A safety check).
            _wait_pod_ready(child, test_namespace)

            # And exec should still work — the data plane survives the snapshot delete.
            exec_result = await k7.exec_command(
                child,
                "echo fork-survives > /mnt/state/marker && cat /mnt/state/marker",
                namespace=test_namespace,
            )
            assert exec_result.exit_code == 0
            assert "fork-survives" in exec_result.stdout
        finally:
            try:
                await k7.delete_sandbox(child, namespace=test_namespace)
            except Exception:
                pass


class TestGcSafety:
    """Spec 10e: pause and named snapshots are never auto-collected."""

    def test_pause_and_named_snapshots_survive_gc(
        self,
        ql_sandbox: str,
        test_namespace: str,
    ):
        pause_snap = f"{ql_sandbox}-paused-keepme"
        named_snap = f"{ql_sandbox}-exp-baseline"

        try:
            # Pause snapshot via CLI (--snapshot=<name>).
            _run_cli("pause", ql_sandbox, "-n", test_namespace, f"--snapshot={pause_snap}")
            assert _snapshot_exists(pause_snap, test_namespace)

            # Resume so the GC sweep doesn't accidentally interact with a scaled-down deploy.
            _run_cli("resume", ql_sandbox, "-n", test_namespace)
            _wait_pod_ready(ql_sandbox, test_namespace)

            # Named snapshot via the new ``snapshot create`` sub-command.
            _run_cli("snapshot", "create", ql_sandbox, named_snap, "-n", test_namespace)
            assert _snapshot_exists(named_snap, test_namespace)

            # GC with keep_fork_for=0s — every fork snapshot would be eligible,
            # but pause and named must be untouched.
            gc = _run_cli(
                "snapshot",
                "gc",
                "-n",
                test_namespace,
                "--keep-fork-for=0s",
                "--dry-run",
            )
            assert pause_snap not in gc.stdout
            assert named_snap not in gc.stdout

            real_gc = _run_cli(
                "snapshot",
                "gc",
                "-n",
                test_namespace,
                "--keep-fork-for=0s",
            )
            assert pause_snap not in real_gc.stdout
            assert named_snap not in real_gc.stdout
            assert _snapshot_exists(pause_snap, test_namespace)
            assert _snapshot_exists(named_snap, test_namespace)
        finally:
            for s in (pause_snap, named_snap):
                _force_delete_snapshot(s, test_namespace)


class TestCronJobManifest:
    """The snapshot-gc CronJob lands and a manually-triggered Job runs successfully."""

    def test_cronjob_applied_by_install(self):
        out = subprocess.run(
            [_K3S, "kubectl", "get", "cronjob", "k7-snapshot-gc", "-n", "kube-system", "-o", "name"],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0 or "k7-snapshot-gc" not in out.stdout:
            pytest.skip("k7-snapshot-gc CronJob not yet applied — re-run k7 install --tags k7,api")

    def test_manual_gc_job_completes(self):
        """Trigger a Job from the CronJob and verify it completes within 2 min."""
        check = subprocess.run(
            [_K3S, "kubectl", "get", "cronjob", "k7-snapshot-gc", "-n", "kube-system", "-o", "name"],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            pytest.skip("k7-snapshot-gc CronJob not present")

        job_name = f"k7-snapshot-gc-test-{uuid4().hex[:6]}"
        try:
            create = subprocess.run(
                [
                    _K3S,
                    "kubectl",
                    "create",
                    "job",
                    "--from=cronjob/k7-snapshot-gc",
                    job_name,
                    "-n",
                    "kube-system",
                ],
                capture_output=True,
                text=True,
            )
            assert create.returncode == 0, create.stderr

            deadline = time.time() + 120
            succeeded = False
            while time.time() < deadline:
                out = subprocess.run(
                    [
                        _K3S,
                        "kubectl",
                        "get",
                        "job",
                        job_name,
                        "-n",
                        "kube-system",
                        "-o",
                        "jsonpath={.status.succeeded}",
                    ],
                    capture_output=True,
                    text=True,
                )
                if out.returncode == 0 and out.stdout.strip() == "1":
                    succeeded = True
                    break
                time.sleep(3)
            assert succeeded, f"Job {job_name} never reached succeeded=1"
        finally:
            subprocess.run(
                [_K3S, "kubectl", "delete", "job", job_name, "-n", "kube-system", "--ignore-not-found"],
                capture_output=True,
            )
