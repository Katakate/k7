"""Integration tests specific to kata-firecracker-devmapper backend."""

import os
import socket
import subprocess
import time

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = [pytest.mark.integration, pytest.mark.firecracker]

_K3S = "/usr/local/bin/k3s"


def _local_node_name() -> str:
    """Return the k3s node name corresponding to the current host.

    Tests like test_jailer_active inspect /proc on the local machine, so any
    sandbox they create must be pinned to this node when the cluster is
    multi-node.
    """
    short = socket.gethostname().split(".", 1)[0]
    try:
        result = subprocess.run(
            [_K3S, "kubectl", "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"],
            capture_output=True,
            text=True,
            check=True,
        )
        nodes = result.stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return short
    for n in nodes:
        if n == short or n.startswith(short + "."):
            return n
    return nodes[0] if nodes else short


def _wait_for_pod_running(name: str, namespace: str, timeout: int = 120) -> None:
    """Block until the pod for a sandbox deployment reaches Running phase."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
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
                "jsonpath={.items[0].status.phase}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "Running":
            return
        time.sleep(3)
    raise AssertionError(f"Pod for {name} did not reach Running within {timeout}s")


def _get_live_firecracker_pids() -> list[str]:
    """Return PIDs of firecracker processes whose root fs is still live.

    Scans /proc directly instead of relying on pgrep.  Skips stale
    orphan processes whose chroot has been cleaned up (readlink shows
    ``(deleted)``).
    """
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                if f.read().strip() != "firecracker":
                    continue
            root_link = os.readlink(f"/proc/{entry}/root")
            if "(deleted)" in root_link:
                continue
            pids.append(entry)
        except (OSError, PermissionError):
            continue
    return pids


class TestFirecrackerBackend:
    async def test_devmapper_sandbox_uses_kata_runtime(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-fc",
            image="alpine:3.20",
            namespace=test_namespace,
            backend="kata-firecracker-devmapper",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            apps_v1 = await k7_core._get_apps_v1_client()
            dep = await apps_v1.read_namespaced_deployment("integ-fc", test_namespace)
            runtime_class = dep.spec.template.spec.runtime_class_name
            assert runtime_class == "kata", f"expected kata, got {runtime_class}"
        finally:
            await k7_core.delete_sandbox("integ-fc", namespace=test_namespace)

    async def test_memory_limit_sizes_vm(self, k7_core: K7Core, test_namespace: str):
        """Spec 18h regression: kfd must honour ``default_memory`` (not silently
        ignore it). Before the install forwarded ``pod_annotations`` on
        ``runtimes.kata`` and allowlisted the annotation in
        configuration-fc.toml, ``--memory 3Gi`` still booted a 2048 MiB VM."""
        cfg = SandboxConfig(
            name="integ-fc-mem",
            image="alpine:3.20",
            namespace=test_namespace,
            backend="kata-firecracker-devmapper",
            limits={"memory": "3Gi", "cpu": "1"},
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_for_pod_running("integ-fc-mem", test_namespace)
            r = await k7_core.exec_command(
                "integ-fc-mem",
                "grep MemTotal /proc/meminfo",
                namespace=test_namespace,
            )
            assert r.exit_code == 0, r.stderr
            mem_total_kb = int(r.stdout.split()[1])
            # Kata's default VM is 2048 MiB; 3Gi via the annotation must be
            # visibly larger (leave headroom for kernel-reserved memory).
            assert mem_total_kb > 2_500_000, f"guest MemTotal {mem_total_kb} kB — annotation not applied?"
        finally:
            await k7_core.delete_sandbox("integ-fc-mem", namespace=test_namespace)

    async def test_jailer_active(self, k7_core: K7Core, test_namespace: str):
        """Verify the Firecracker jailer is active for fc sandboxes.

        The jailer chroots the firecracker process into a minimal directory
        containing only the VM assets (kernel, rootfs, drives, config).
        Without the jailer, firecracker runs on the host root filesystem
        and can see /etc/passwd, /etc/shadow, /usr/local/bin/*, etc.

        This test checks the ACTUAL security property: the firecracker
        process's root filesystem must NOT contain host files.
        """
        cfg = SandboxConfig(
            name="test-jailer",
            image="alpine:3.20",
            namespace=test_namespace,
            backend="kata-firecracker-devmapper",
            # Pin to the local node so /proc inspection below sees the
            # firecracker process this test just spawned (multi-node clusters
            # may otherwise schedule the sandbox on a different node).
            node_name=_local_node_name(),
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_for_pod_running("test-jailer", test_namespace)

            pids = _get_live_firecracker_pids()
            assert len(pids) > 0, "No firecracker processes found on the host"

            for pid in pids:
                fc_root = f"/proc/{pid}/root"

                assert not os.path.exists(f"{fc_root}/etc/passwd"), (
                    f"Firecracker PID {pid} can see /etc/passwd — "
                    f"it is running on the host root filesystem, NOT in a "
                    f"jailer chroot. The jailer is not active."
                )
                assert not os.path.exists(f"{fc_root}/etc/shadow"), (
                    f"Firecracker PID {pid} can see /etc/shadow — host root filesystem is exposed. Jailer not active."
                )
                assert not os.path.exists(f"{fc_root}/usr"), (
                    f"Firecracker PID {pid} can see /usr — host root filesystem is exposed. Jailer not active."
                )
                assert not os.path.exists(f"{fc_root}/boot"), (
                    f"Firecracker PID {pid} can see /boot — host root filesystem is exposed. Jailer not active."
                )

                assert os.path.isfile(f"{fc_root}/firecracker"), (
                    f"Firecracker PID {pid} chroot missing firecracker binary"
                )
                assert os.path.isfile(f"{fc_root}/vmlinux"), f"Firecracker PID {pid} chroot missing vmlinux kernel"
                assert os.path.isfile(f"{fc_root}/rootfs"), f"Firecracker PID {pid} chroot missing rootfs image"

                entries = os.listdir(fc_root)
                assert len(entries) < 20, (
                    f"Firecracker PID {pid} root has {len(entries)} entries "
                    f"(expected <20 for jailer chroot, host root has 20+). "
                    f"Entries: {sorted(entries)}"
                )
        finally:
            await k7_core.delete_sandbox("test-jailer", namespace=test_namespace)
