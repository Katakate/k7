"""Integration tests specific to kata-qemu-longhorn backend."""

import asyncio
import json
import subprocess
import time

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = [pytest.mark.integration, pytest.mark.qemu]


def _kubectl_json(args: list[str]) -> str:
    """Run kubectl with JSON output and return stdout."""
    result = subprocess.run(
        ["k3s", "kubectl", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _wait_pod_ready(sandbox_name: str, namespace: str, timeout: int = 300) -> None:
    """Block until a non-terminating Running pod for the sandbox is Ready.

    After pause→resume, a terminating pod can still report Ready=True while
    the replacement is Pending; ``.items[0]`` alone races (spec 18h flake).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            [
                "k3s",
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                f"app={sandbox_name}",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for pod in json.loads(result.stdout).get("items", []):
                meta = pod.get("metadata") or {}
                status = pod.get("status") or {}
                if meta.get("deletionTimestamp"):
                    continue
                if status.get("phase") != "Running":
                    continue
                if any(
                    c.get("type") == "Ready" and c.get("status") == "True" for c in (status.get("conditions") or [])
                ):
                    return
        time.sleep(2)
    raise TimeoutError(f"Pod for {sandbox_name} not Ready within {timeout}s")


def _delete_snapshot(name: str, namespace: str) -> None:
    """Delete a VolumeSnapshot without hanging on CSI finalizers."""
    subprocess.run(
        [
            "k3s",
            "kubectl",
            "patch",
            "volumesnapshot",
            name,
            "-n",
            namespace,
            "--type=json",
            "-p",
            '[{"op":"remove","path":"/metadata/finalizers"}]',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        [
            "k3s",
            "kubectl",
            "delete",
            "volumesnapshot",
            name,
            "-n",
            namespace,
            "--ignore-not-found",
            "--wait=false",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_snapshot_gone(name: str, namespace: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            ["k3s", "kubectl", "get", "volumesnapshot", name, "-n", namespace, "-o", "name"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return
        _delete_snapshot(name, namespace)
        time.sleep(2)
    raise TimeoutError(f"VolumeSnapshot {name} still present after {timeout}s")


class TestQemuLonghornBackend:
    async def test_root_pvc_created(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-ql",
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            v1 = await k7_core._get_core_v1_client()
            pvc_name = "integ-ql-root-lh"
            pvc = await v1.read_namespaced_persistent_volume_claim(pvc_name, test_namespace)
            assert pvc is not None
            assert pvc.metadata.name == pvc_name
        finally:
            await k7_core.delete_sandbox("integ-ql", namespace=test_namespace)

    async def test_runtime_class_kata_qemu(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-ql-rt",
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            apps_v1 = await k7_core._get_apps_v1_client()
            dep = await apps_v1.read_namespaced_deployment("integ-ql-rt", test_namespace)
            runtime_class = dep.spec.template.spec.runtime_class_name
            assert runtime_class == "kata-qemu", f"expected kata-qemu, got {runtime_class}"
        finally:
            await k7_core.delete_sandbox("integ-ql-rt", namespace=test_namespace)

    async def test_memory_limit_sizes_vm(self, k7_core: K7Core, test_namespace: str):
        """Spec 18g regression: a kql sandbox with a memory limit stamps the
        ``default_memory`` hypervisor annotation. Before the install allowed
        it in configuration-qemu.toml, kata rejected the WHOLE pod with
        "annotation ... is not enabled" — it could never start. Assert the
        pod runs and the guest VM actually got the larger memory size."""
        cfg = SandboxConfig(
            name="integ-ql-mem",
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            limits={"memory": "3Gi", "cpu": "1"},
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready("integ-ql-mem", test_namespace)
            r = await k7_core.exec_command(
                "integ-ql-mem",
                "grep MemTotal /proc/meminfo",
                namespace=test_namespace,
            )
            assert r.exit_code == 0, r.stderr
            mem_total_kb = int(r.stdout.split()[1])
            # Kata's default VM is 2048 MiB; 3Gi via the annotation must be
            # visibly larger (leave headroom for kernel-reserved memory).
            assert mem_total_kb > 2_500_000, f"guest MemTotal {mem_total_kb} kB — annotation not applied?"
        finally:
            await k7_core.delete_sandbox("integ-ql-mem", namespace=test_namespace)


class TestSnapshotPauseResumeFork:
    """Validate snapshot, pause/resume, and fork operations for kata-qemu-longhorn."""

    async def test_create_to_ready_latency(self, k7_core: K7Core, test_namespace: str):
        """Measure cold sandbox creation to pod-ready latency."""
        name = "integ-cold"
        cfg = SandboxConfig(
            name=name,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        t0 = time.time()
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(name, test_namespace)
            elapsed = time.time() - t0
            print(f"[timing] cold create to pod ready in {elapsed:.2f}s")
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_snapshot_create_and_ready(self, k7_core: K7Core, test_namespace: str):
        """Validates snapshot creation + readyToUse, prints latency."""
        name = "integ-snap"
        cfg = SandboxConfig(
            name=name,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        pvc_name = k7_core._root_pvc_name(name)
        snapshot_name = f"{name}-test-snap"

        try:
            _wait_pod_ready(name, test_namespace)

            await k7_core.exec_command(name, "echo snapshot-marker > /mnt/state/marker", namespace=test_namespace)

            t0 = time.time()
            snap = await k7_core._create_volume_snapshot(
                pvc_name=pvc_name,
                snapshot_name=snapshot_name,
                namespace=test_namespace,
            )
            assert snap.success, f"snapshot creation failed: {snap.error}"

            ready = await k7_core._wait_for_snapshot_ready(snapshot_name, namespace=test_namespace, timeout=60)
            elapsed = time.time() - t0
            print(f"[timing] snapshot ready in {elapsed:.2f}s")
            assert ready.success, f"snapshot not ready: {ready.error}"
            assert elapsed < 60, f"snapshot took {elapsed:.2f}s (>60s)"
        finally:
            _delete_snapshot(snapshot_name, test_namespace)
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_pause_resume_preserves_state(self, k7_core: K7Core, test_namespace: str):
        """Validates pause (scale=0 + snapshot) then resume, data survives."""
        name = "integ-pr"
        cfg = SandboxConfig(
            name=name,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        pvc_name = k7_core._root_pvc_name(name)
        snapshot_name = f"{name}-pause-snap"

        try:
            _wait_pod_ready(name, test_namespace)

            await k7_core.exec_command(name, "echo persist-data > /mnt/state/persist-test", namespace=test_namespace)

            t_pause = time.time()
            pause = await k7_core.pause_sandbox(
                name,
                namespace=test_namespace,
                pvc_name=pvc_name,
                snapshot_name=snapshot_name,
            )
            assert pause.success, f"pause failed: {pause.error}"

            deadline = time.time() + 60
            while time.time() < deadline:
                dep_json = _kubectl_json(
                    [
                        "get",
                        "deployment",
                        name,
                        "-n",
                        test_namespace,
                        "-o",
                        "jsonpath={.spec.replicas}",
                    ]
                )
                if dep_json.strip() == "0":
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError("replicas did not reach 0 within 60s")

            pause_elapsed = time.time() - t_pause
            print(f"[timing] pause (scale down) in {pause_elapsed:.2f}s")

            t_resume = time.time()
            resume = await k7_core.resume_sandbox(name, namespace=test_namespace)
            assert resume.success, f"resume failed: {resume.error}"

            _wait_pod_ready(name, test_namespace, timeout=120)
            resume_elapsed = time.time() - t_resume
            print(f"[timing] resume (scale up + pod ready) in {resume_elapsed:.2f}s")
            assert resume_elapsed < 60, f"resume took {resume_elapsed:.2f}s (>60s)"

            exec_result = await k7_core.exec_command(
                name,
                "cat /mnt/state/persist-test",
                namespace=test_namespace,
            )
            assert exec_result.exit_code == 0, f"exec failed: {exec_result.stderr}"
            assert "persist-data" in exec_result.stdout, f"data not preserved after resume: {exec_result.stdout!r}"
        finally:
            _delete_snapshot(snapshot_name, test_namespace)
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_fork_clones_data(self, k7_core: K7Core, test_namespace: str):
        """Validates fork creates clone with source data."""
        source = "integ-fork-src"
        fork_name = "integ-fork-dst"
        cfg = SandboxConfig(
            name=source,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create source failed: {result.error}"

        try:
            _wait_pod_ready(source, test_namespace)

            await k7_core.exec_command(source, "echo fork-marker > /mnt/state/fork-file", namespace=test_namespace)

            t0 = time.time()
            fork_result = await k7_core.fork_sandbox(source, fork_name, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            _wait_pod_ready(fork_name, test_namespace, timeout=600)
            fork_elapsed = time.time() - t0
            print(f"[timing] fork total in {fork_elapsed:.2f}s")
            # 600s bound (spec 18e): on a 3-node Longhorn r=3 cluster the
            # fork's cloned volume hydrates replicas on all nodes, and the
            # very FIRST fork after a fresh install additionally pulls the
            # Longhorn engine image on every node plus the sandbox image on
            # the fork's node (>240s observed cold; ~150s warm). The guard
            # still catches a pathological full-byte-copy restore path,
            # which takes far longer on real volumes.
            assert fork_elapsed < 600, f"fork took {fork_elapsed:.2f}s (>600s)"

            exec_result = await k7_core.exec_command(
                fork_name,
                "cat /mnt/state/fork-file",
                namespace=test_namespace,
            )
            assert exec_result.exit_code == 0, f"exec on fork failed: {exec_result.stderr}"
            assert "fork-marker" in exec_result.stdout, f"data not cloned to fork: {exec_result.stdout!r}"
        finally:
            await k7_core.delete_sandbox(fork_name, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_fork_independence(self, k7_core: K7Core, test_namespace: str):
        """Validates writes to fork don't affect source."""
        source = "integ-indep-src"
        fork_name = "integ-indep-dst"
        cfg = SandboxConfig(
            name=source,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create source failed: {result.error}"

        try:
            _wait_pod_ready(source, test_namespace)

            await k7_core.exec_command(source, "echo original > /mnt/state/shared-file", namespace=test_namespace)

            fork_result = await k7_core.fork_sandbox(source, fork_name, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            _wait_pod_ready(fork_name, test_namespace, timeout=180)

            await k7_core.exec_command(
                fork_name,
                "echo modified-by-fork > /mnt/state/shared-file",
                namespace=test_namespace,
            )
            await k7_core.exec_command(
                fork_name,
                "echo fork-only > /mnt/state/fork-exclusive",
                namespace=test_namespace,
            )

            src_read = await k7_core.exec_command(
                source,
                "cat /mnt/state/shared-file",
                namespace=test_namespace,
            )
            assert src_read.exit_code == 0
            assert "original" in src_read.stdout, f"source data was modified by fork write: {src_read.stdout!r}"

            src_excl = await k7_core.exec_command(
                source,
                "cat /mnt/state/fork-exclusive 2>&1 || true",
                namespace=test_namespace,
            )
            assert "fork-only" not in src_excl.stdout, "fork-exclusive file leaked to source"
        finally:
            await k7_core.delete_sandbox(fork_name, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_cleanup_no_leak(self, k7_core: K7Core, test_namespace: str):
        """Validates delete removes PVCs once referencing snapshots are gone.

        Spec 10e keeps the root PVC while pause/named snapshots still exist, so
        the snapshot must be deleted before the sandbox for a full cleanup.
        """
        name = "integ-leak"
        cfg = SandboxConfig(
            name=name,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        pvc_name = k7_core._root_pvc_name(name)
        snapshot_name = f"{name}-leak-snap"

        try:
            _wait_pod_ready(name, test_namespace)

            snap = await k7_core._create_volume_snapshot(
                pvc_name=pvc_name,
                snapshot_name=snapshot_name,
                namespace=test_namespace,
            )
            assert snap.success, f"snapshot creation failed: {snap.error}"
            await k7_core._wait_for_snapshot_ready(snapshot_name, namespace=test_namespace, timeout=60)
        finally:
            _delete_snapshot(snapshot_name, test_namespace)
            _wait_snapshot_gone(snapshot_name, test_namespace)
            await k7_core.delete_sandbox(name, namespace=test_namespace)

        deadline = time.time() + 60
        remaining_pvcs: list[str] = [name]
        remaining_snaps: list[str] = [name]
        while time.time() < deadline:
            pvc_check = subprocess.run(
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "pvc",
                    "-n",
                    test_namespace,
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            remaining_pvcs = [p for p in pvc_check.stdout.strip().split() if p.startswith(name)]
            snap_check = subprocess.run(
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "volumesnapshot",
                    "-n",
                    test_namespace,
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            remaining_snaps = [s for s in snap_check.stdout.strip().split() if s.startswith(name)]
            if not remaining_pvcs and not remaining_snaps:
                break
            time.sleep(2)
        assert not remaining_pvcs, f"leaked PVCs: {remaining_pvcs}"
        assert not remaining_snaps, f"leaked snapshots: {remaining_snaps}"

    async def test_longhorn_no_dangling_resources(self, k7_core: K7Core, test_namespace: str):
        """Validates no orphaned Longhorn volumes/replicas or stuck PVCs/snapshots after a full cycle."""
        names = [f"integ-dangle-{i}" for i in range(2)]
        created = []

        try:
            for name in names:
                cfg = SandboxConfig(
                    name=name,
                    image="alpine:latest",
                    namespace=test_namespace,
                    backend="kata-qemu-longhorn",
                )
                result = await k7_core.create_sandbox(cfg)
                assert result.success, f"create {name} failed: {result.error}"
                created.append(name)

            for name in names:
                _wait_pod_ready(name, test_namespace)
        finally:
            for name in created:
                await k7_core.delete_sandbox(name, namespace=test_namespace)

        # Poll for up to 90s for Longhorn cleanup. With dataLocality=best-effort
        # and replica-auto-balance enabled, replica reconciliation can extend
        # the time between PVC delete and PV/volume removal beyond 10s.
        deadline = time.time() + 90
        last_orphans: list[str] = []
        last_stuck_pvcs: list[str] = []
        last_orphan_snaps: list[str] = []
        while time.time() < deadline:
            pvc_result = subprocess.run(
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "pvc",
                    "-n",
                    test_namespace,
                    "-o",
                    "jsonpath={range .items[*]}{.metadata.name}={.status.phase} {end}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            last_stuck_pvcs = []
            if pvc_result.stdout.strip():
                for entry in pvc_result.stdout.strip().split():
                    if "=" not in entry:
                        continue
                    pvc_name, phase = entry.rsplit("=", 1)
                    if any(pvc_name.startswith(n) for n in names) and phase in ("Pending", "Terminating"):
                        last_stuck_pvcs.append(f"{pvc_name}={phase}")

            snap_result = subprocess.run(
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "volumesnapshot",
                    "-n",
                    test_namespace,
                    "-o",
                    "jsonpath={range .items[*]}{.metadata.name}={.status.readyToUse} {end}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            last_orphan_snaps = []
            if snap_result.stdout.strip():
                for entry in snap_result.stdout.strip().split():
                    if "=" not in entry:
                        continue
                    snap_name, ready = entry.rsplit("=", 1)
                    if any(snap_name.startswith(n) for n in names):
                        last_orphan_snaps.append(f"{snap_name}={ready}")

            lh_vol_result = subprocess.run(
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "volumes.longhorn.io",
                    "-n",
                    "longhorn-system",
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            last_orphans = []
            if lh_vol_result.returncode == 0 and lh_vol_result.stdout.strip():
                for vol in lh_vol_result.stdout.strip().split():
                    pv_check = subprocess.run(
                        [
                            "k3s",
                            "kubectl",
                            "get",
                            "pv",
                            vol,
                            "-o",
                            "jsonpath={.spec.claimRef.name}",
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if pv_check.returncode == 0:
                        claim = pv_check.stdout.strip()
                        if any(claim.startswith(n) for n in names):
                            last_orphans.append(f"{vol} (claim={claim})")

            if not last_stuck_pvcs and not last_orphan_snaps and not last_orphans:
                print("[timing] dangling resource check passed")
                return

            await asyncio.sleep(3)

        if last_stuck_pvcs:
            pytest.fail(f"PVCs stuck after 90s: {last_stuck_pvcs}")
        if last_orphan_snaps:
            pytest.fail(f"Orphaned snapshots after 90s: {last_orphan_snaps}")
        assert not last_orphans, f"Orphaned Longhorn volumes after 90s: {last_orphans}"
