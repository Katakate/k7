"""Integration tests for ``k7 pause`` after Spec 10c.

These tests live alongside ``tests/integration/test_qemu.py`` (which already
covers pause/resume/fork via K7Core directly) and exercise the **CLI surface**:
that the snapshot-trigger bug is fixed (snapshot fires on ``--snapshot`` alone,
without ``--pvc``) and that the removed flags ``--pvc`` / ``--snapshot-class``
fail fast.

We invoke ``k7`` via ``src/k7/cli/dev.sh`` so we don't depend on a packaged
binary being installed on the node.
"""

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


def _run_cli(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke ``dev.sh <args>``.

    Forces the ``--core`` global flag so these CLI-flag-handling tests don't
    require ``K7_API_URL`` / ``K7_API_KEY`` (Spec 10g made the default route
    the API). Tests that need the API-routed path go through the SDK / HTTP
    fixtures in ``test_api.py``.
    """
    cmd = [str(_DEV_SH), "--core", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout, cwd=str(_REPO_ROOT))


def _wait_replicas(name: str, namespace: str, expected: int, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            [_K3S, "kubectl", "get", "deployment", name, "-n", namespace, "-o", "jsonpath={.spec.replicas}"],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip() == str(expected):
            return
        time.sleep(1)
    raise TimeoutError(f"deployment {name} did not reach replicas={expected}")


def _snapshot_exists(name: str, namespace: str) -> bool:
    out = subprocess.run(
        [_K3S, "kubectl", "get", "volumesnapshot", name, "-n", namespace, "-o", "name"],
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and out.stdout.strip() != ""


def _list_snapshots(namespace: str) -> list[str]:
    out = subprocess.run(
        [_K3S, "kubectl", "get", "volumesnapshot", "-n", namespace, "-o", "name"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


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


@pytest.fixture()
def k7_core() -> K7Core:
    return K7Core()


@pytest.fixture()
def ql_sandbox(k7_core: K7Core, test_namespace: str):
    """Provision a fresh kata-qemu-longhorn sandbox; yield its name; tear down."""
    name = f"pause-spec10c-{uuid4().hex[:6]}"
    import asyncio

    cfg = SandboxConfig(
        name=name,
        image="alpine:3.20",
        namespace=test_namespace,
        backend="kata-qemu-longhorn",
    )
    result = asyncio.run(k7_core.create_sandbox(cfg))
    assert result.success, f"create failed: {result.error}"
    _wait_pod_ready(name, test_namespace)
    try:
        yield name
    finally:
        try:
            asyncio.run(k7_core.delete_sandbox(name, namespace=test_namespace))
        except Exception:
            pass


class TestPauseSnapshotBugRegression:
    """Spec 10c: ``k7 pause foo --snapshot=bar`` (without ``--pvc``) **must**
    create a real ``VolumeSnapshot``. Pre-10c this silently did nothing.
    """

    def test_pause_with_snapshot_actually_creates_one(
        self,
        ql_sandbox: str,
        test_namespace: str,
    ):
        snap = f"{ql_sandbox}-v1"
        try:
            _run_cli("pause", ql_sandbox, "-n", test_namespace, f"--snapshot={snap}")
            _wait_replicas(ql_sandbox, test_namespace, 0)
            # Wait briefly for the VolumeSnapshot object to settle.
            deadline = time.time() + 60
            while time.time() < deadline and not _snapshot_exists(snap, test_namespace):
                time.sleep(2)
            assert _snapshot_exists(snap, test_namespace), (
                f"VolumeSnapshot {snap} was never created — the spec 10c bug regressed"
            )
        finally:
            subprocess.run(
                [
                    _K3S,
                    "kubectl",
                    "delete",
                    "volumesnapshot",
                    snap,
                    "-n",
                    test_namespace,
                    "--ignore-not-found",
                    "--wait=false",
                ],
                capture_output=True,
            )

    def test_pause_with_bare_flag_auto_names_snapshot(
        self,
        ql_sandbox: str,
        test_namespace: str,
    ):
        """``--snapshot`` as a bare flag → snapshot named ``{sandbox}-paused-<unix>``."""
        before = set(_list_snapshots(test_namespace))
        try:
            _run_cli("pause", ql_sandbox, "-n", test_namespace, "--snapshot")
            _wait_replicas(ql_sandbox, test_namespace, 0)

            deadline = time.time() + 60
            auto: str | None = None
            while time.time() < deadline:
                current = set(_list_snapshots(test_namespace))
                new = current - before
                # Spec auto-name format is ``{name}-paused-<ts>``.
                for s in new:
                    if "/" in s and "paused" in s and ql_sandbox in s:
                        auto = s.split("/", 1)[1]
                        break
                if auto:
                    break
                time.sleep(2)
            assert auto, "auto-named snapshot was never created"
            assert auto.startswith(f"{ql_sandbox}-paused-"), auto
        finally:
            for s in _list_snapshots(test_namespace):
                if "paused" in s and ql_sandbox in s:
                    snap_name = s.split("/", 1)[1] if "/" in s else s
                    subprocess.run(
                        [
                            _K3S,
                            "kubectl",
                            "delete",
                            "volumesnapshot",
                            snap_name,
                            "-n",
                            test_namespace,
                            "--ignore-not-found",
                            "--wait=false",
                        ],
                        capture_output=True,
                    )

    def test_pause_no_snapshot_creates_none(
        self,
        ql_sandbox: str,
        test_namespace: str,
    ):
        before = set(_list_snapshots(test_namespace))
        _run_cli("pause", ql_sandbox, "-n", test_namespace)
        _wait_replicas(ql_sandbox, test_namespace, 0)
        # Give k8s a moment in case anything is going to appear.
        time.sleep(5)
        after = set(_list_snapshots(test_namespace))
        new = after - before
        sandbox_snaps = [s for s in new if ql_sandbox in s]
        assert sandbox_snaps == [], f"unexpected snapshot(s) created for {ql_sandbox}: {sandbox_snaps}"


class TestRemovedPauseFlags:
    """Spec 10c: ``--pvc`` and ``--snapshot-class`` were removed from ``k7 pause``."""

    def test_pvc_flag_rejected(self, test_namespace: str):
        result = subprocess.run(
            [str(_DEV_SH), "--core", "pause", "any-name", "-n", test_namespace, "--pvc=foo"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode != 0
        combined = (result.stdout or "") + (result.stderr or "")
        assert "no such option" in combined.lower()

    def test_snapshot_class_flag_rejected(self, test_namespace: str):
        result = subprocess.run(
            [str(_DEV_SH), "--core", "pause", "any-name", "-n", test_namespace, "--snapshot-class=longhorn"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        assert result.returncode != 0
        combined = (result.stdout or "") + (result.stderr or "")
        assert "no such option" in combined.lower()
