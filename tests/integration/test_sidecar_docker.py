"""Integration tests for --sidecar docker on both backends."""

import asyncio
import json
import subprocess
import time

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = pytest.mark.integration


def _wait_all_containers_ready(sandbox_name: str, namespace: str, timeout: int = 300) -> None:
    """Block until all containers in the pod are ready."""
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
        if result.returncode != 0:
            time.sleep(2)
            continue
        try:
            data = json.loads(result.stdout)
            items = data.get("items", [])
            if not items:
                time.sleep(2)
                continue
            pod = items[0]
            statuses = pod.get("status", {}).get("containerStatuses", [])
            if statuses and all(s.get("ready") for s in statuses):
                return
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
        time.sleep(2)
    raise TimeoutError(f"Not all containers ready for {sandbox_name} within {timeout}s")


async def _wait_docker_ready(k7_core: K7Core, name: str, namespace: str, timeout: int = 120) -> None:
    """Wait for the docker daemon inside the sidecar to respond to 'docker info'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await k7_core.exec_command(name, "docker info >/dev/null 2>&1 && echo ok", namespace=namespace)
        if result.exit_code == 0 and "ok" in result.stdout:
            return
        await asyncio.sleep(3)
    raise TimeoutError(f"Docker daemon not ready in {name} within {timeout}s")


async def _docker_has_image(k7_core: K7Core, name: str, namespace: str, image: str) -> bool:
    """Check if a specific docker image exists in the sandbox."""
    result = await k7_core.exec_command(
        name, f"docker image inspect {image} >/dev/null 2>&1 && echo yes", namespace=namespace
    )
    return result.exit_code == 0 and "yes" in result.stdout


# ===========================================================================
# kata-qemu-longhorn tests
# ===========================================================================


@pytest.mark.qemu
class TestSidecarDockerQL:
    async def test_sidecar_docker_run_ql(self, k7_core: K7Core, test_namespace: str):
        name = "sc-docker-run-ql"
        cfg = SandboxConfig(
            name=name,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            sidecar="docker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            exec_result = await k7_core.exec_command(name, "docker run --rm hello-world", namespace=test_namespace)
            assert exec_result.exit_code == 0, f"exec failed: {exec_result.stderr}"
            assert "Hello from Docker!" in exec_result.stdout
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_sidecar_docker_build_ql(self, k7_core: K7Core, test_namespace: str):
        name = "sc-docker-build-ql"
        cfg = SandboxConfig(
            name=name,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            sidecar="docker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            await k7_core.exec_command(
                name,
                "mkdir -p /tmp/build && printf 'FROM alpine:3.21\\nCMD echo built-ok' > /tmp/build/Dockerfile",
                namespace=test_namespace,
            )
            build_result = await k7_core.exec_command(
                name, "cd /tmp/build && docker build -t myapp .", namespace=test_namespace
            )
            assert build_result.exit_code == 0, f"build failed: {build_result.stderr}"
            run_result = await k7_core.exec_command(name, "docker run --rm myapp", namespace=test_namespace)
            assert run_result.exit_code == 0, f"run failed: {run_result.stderr}"
            assert "built-ok" in run_result.stdout
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_sidecar_docker_pause_resume_ql(self, k7_core: K7Core, test_namespace: str):
        name = "sc-docker-pr-ql"
        cfg = SandboxConfig(
            name=name,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            sidecar="docker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            pull_result = await k7_core.exec_command(name, "docker pull alpine:3.21", namespace=test_namespace)
            assert pull_result.exit_code == 0, f"pull failed: {pull_result.stderr}"

            pause_result = await k7_core.pause_sandbox(name, namespace=test_namespace)
            assert pause_result.success, f"pause failed: {pause_result.error}"

            await asyncio.sleep(5)

            resume_result = await k7_core.resume_sandbox(name, namespace=test_namespace)
            assert resume_result.success, f"resume failed: {resume_result.error}"

            _wait_all_containers_ready(name, test_namespace, timeout=120)
            await _wait_docker_ready(k7_core, name, test_namespace)

            assert await _docker_has_image(k7_core, name, test_namespace, "alpine:3.21"), (
                "alpine:3.21 not found after resume"
            )

            run_result = await k7_core.exec_command(
                name, "docker run --rm alpine:3.21 echo survived-pause", namespace=test_namespace
            )
            assert run_result.exit_code == 0
            assert "survived-pause" in run_result.stdout
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_sidecar_docker_fork_ql(self, k7_core: K7Core, test_namespace: str):
        source = "sc-docker-fork-src"
        fork_name = "sc-docker-fork-dst"
        cfg = SandboxConfig(
            name=source,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            sidecar="docker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(source, test_namespace)
            await _wait_docker_ready(k7_core, source, test_namespace)
            pull_result = await k7_core.exec_command(source, "docker pull alpine:3.21", namespace=test_namespace)
            assert pull_result.exit_code == 0, f"pull failed: {pull_result.stderr}"

            fork_result = await k7_core.fork_sandbox(source, fork_name, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            _wait_all_containers_ready(fork_name, test_namespace, timeout=180)
            await _wait_docker_ready(k7_core, fork_name, test_namespace)

            assert await _docker_has_image(k7_core, fork_name, test_namespace, "alpine:3.21"), (
                "alpine:3.21 not found in fork"
            )

            run_result = await k7_core.exec_command(
                fork_name, "docker run --rm alpine:3.21 echo fork-works", namespace=test_namespace
            )
            assert run_result.exit_code == 0
            assert "fork-works" in run_result.stdout
        finally:
            await k7_core.delete_sandbox(fork_name, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_sidecar_docker_fork_independence_ql(self, k7_core: K7Core, test_namespace: str):
        """Verify docker images pulled in the fork don't leak back to the source."""
        source = "sc-docker-fi-src"
        fork_name = "sc-docker-fi-dst"
        cfg = SandboxConfig(
            name=source,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            sidecar="docker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(source, test_namespace)
            await _wait_docker_ready(k7_core, source, test_namespace)

            pull_result = await k7_core.exec_command(source, "docker pull alpine:3.21", namespace=test_namespace)
            assert pull_result.exit_code == 0, f"pull failed: {pull_result.stderr}"

            fork_result = await k7_core.fork_sandbox(source, fork_name, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            _wait_all_containers_ready(fork_name, test_namespace, timeout=180)
            await _wait_docker_ready(k7_core, fork_name, test_namespace)

            fork_pull = await k7_core.exec_command(fork_name, "docker pull busybox:1.36", namespace=test_namespace)
            assert fork_pull.exit_code == 0, f"fork pull failed: {fork_pull.stderr}"

            assert await _docker_has_image(k7_core, fork_name, test_namespace, "busybox:1.36"), (
                "busybox:1.36 not found in fork"
            )
            assert await _docker_has_image(k7_core, fork_name, test_namespace, "alpine:3.21"), (
                "alpine:3.21 not found in fork (should be inherited)"
            )

            assert not await _docker_has_image(k7_core, source, test_namespace, "busybox:1.36"), (
                "busybox:1.36 leaked from fork to source"
            )
            assert await _docker_has_image(k7_core, source, test_namespace, "alpine:3.21"), (
                "alpine:3.21 missing from source"
            )
        finally:
            await k7_core.delete_sandbox(fork_name, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_sidecar_docker_cleanup_ql(self, k7_core: K7Core, test_namespace: str):
        name = "sc-docker-clean-ql"
        cfg = SandboxConfig(
            name=name,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            sidecar="docker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        _wait_all_containers_ready(name, test_namespace)
        await _wait_docker_ready(k7_core, name, test_namespace)
        await k7_core.exec_command(name, "docker pull alpine:3.21", namespace=test_namespace)

        await k7_core.delete_sandbox(name, namespace=test_namespace)
        await asyncio.sleep(5)

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
        assert not remaining_pvcs, f"leaked PVCs: {remaining_pvcs}"

        dep_check = subprocess.run(
            [
                "k3s",
                "kubectl",
                "get",
                "deployment",
                "-n",
                test_namespace,
                "-o",
                "jsonpath={.items[*].metadata.name}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        remaining_deps = [d for d in dep_check.stdout.strip().split() if d.startswith(name)]
        assert not remaining_deps, f"leaked deployments: {remaining_deps}"


# ===========================================================================
# kata-firecracker-devmapper tests
# ===========================================================================
# An earlier note here claimed that Docker DinD couldn't run on the
# Firecracker backend because Kata-firecracker "doesn't support privileged
# containers". That note was wrong — verified during spec 10b's bench work
# by spinning up ``--backend kfd --sidecar docker`` and running
# ``docker run hello-world`` successfully in ~3s. The sidecar wiring in
# core.py is symmetric across backends; the only difference is that the
# docker daemon's ``/var/lib/docker`` lives on an emptyDir for FD vs a
# Longhorn PVC sub_path for kata-qemu-longhorn (so FD's docker state is
# ephemeral by design — gone with the sandbox).
#
# FD sidecar integration tests of the same shape as the QL ones above
# would be a worthwhile follow-up (TestSidecarDockerFD class). Skipped
# in this commit only to keep the bench PR scope tight; the
# tests/integration/bench_docker_perf.py module already exercises the
# FD-docker path end-to-end against a real cluster.
# ===========================================================================
