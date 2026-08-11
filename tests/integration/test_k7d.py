"""Integration tests specific to the k7d backend (spec 9a M11).

k7d sandboxes are pods with ``runtimeClassName: k7`` — each pod is one
k7d microVM. Fork is a **warm CoW disk+memory fork** of the live source
VM (driven by the ``k7d.katakate.org/fork-source-*`` pod annotations);
pause/resume freeze/unfreeze the VM in place through the k7d daemon
socket. Named Longhorn snapshots intentionally do not apply.
"""

import asyncio
import json
import os
import subprocess
import time

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = [pytest.mark.integration, pytest.mark.k7d]

# k7d VM operations (pause/resume/fork) go through the node-local
# /run/k7d/k7d.sock — cross-node ops are out of scope (k7d spec 9a M12)
# and core fails loudly on a node mismatch. On a multi-node cluster the
# scheduler may place the sandbox anywhere, so tests that exercise VM
# ops pin their sandboxes to the node this pytest process runs on.
_LOCAL_NODE = os.uname().nodename


def _wait_pod_ready(sandbox_name: str, namespace: str, timeout: int = 300) -> None:
    """Block until at least one pod for the sandbox is Ready."""
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
        if result.returncode == 0:
            try:
                items = json.loads(result.stdout).get("items", [])
                for pod in items:
                    statuses = pod.get("status", {}).get("containerStatuses", [])
                    if statuses and all(s.get("ready") for s in statuses):
                        return
            except (json.JSONDecodeError, KeyError):
                pass
        time.sleep(2)
    raise TimeoutError(f"Pod for {sandbox_name} not Ready within {timeout}s")


def _pod_runtime_class(sandbox_name: str, namespace: str) -> str:
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
            "jsonpath={.items[0].spec.runtimeClassName}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class TestK7dLifecycle:
    async def test_k7d_full_lifecycle(self, k7_core: K7Core, test_namespace: str):
        """Create → exec → pause → resume → delete on the k7d backend."""
        name = "test-k7d-lifecycle"
        cfg = SandboxConfig(
            name=name,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="k7d",
            node_name=_LOCAL_NODE,
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(name, test_namespace)
            assert _pod_runtime_class(name, test_namespace) == "k7"

            exec_result = await k7_core.exec_command(name, "echo hello-k7d", namespace=test_namespace)
            assert exec_result.exit_code == 0, f"exec failed: {exec_result.stderr}"
            assert "hello-k7d" in exec_result.stdout

            # tmpfs marker: survives pause/resume (memory retained) but not
            # a VM reboot — this is what proves resume didn't recreate the VM.
            marker = await k7_core.exec_command(
                name, "echo alive > /tmp/pause-marker && cat /tmp/pause-marker", namespace=test_namespace
            )
            assert marker.exit_code == 0

            pause_result = await k7_core.pause_sandbox(name, namespace=test_namespace)
            assert pause_result.success, f"pause failed: {pause_result.error}"
            assert "frozen" in pause_result.message

            await asyncio.sleep(3)

            resume_result = await k7_core.resume_sandbox(name, namespace=test_namespace)
            assert resume_result.success, f"resume failed: {resume_result.error}"

            after = await k7_core.exec_command(name, "cat /tmp/pause-marker", namespace=test_namespace)
            assert after.exit_code == 0, f"exec after resume failed: {after.stderr}"
            assert "alive" in after.stdout, "tmpfs marker lost across pause/resume — VM was recreated, not frozen"
        finally:
            delete_result = await k7_core.delete_sandbox(name, namespace=test_namespace)
            assert delete_result.success, f"delete failed: {delete_result.error}"

    async def test_k7d_snapshot_rejected_loudly(self, k7_core: K7Core, test_namespace: str):
        """Named Longhorn snapshots must fail loudly on k7d, pointing at fork/k7d."""
        name = "test-k7d-nosnap"
        cfg = SandboxConfig(name=name, image="alpine:3.20", namespace=test_namespace, backend="k7d")
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"
        try:
            _wait_pod_ready(name, test_namespace)
            snap = await k7_core.create_snapshot(name, "no-such-snap", namespace=test_namespace)
            assert not snap.success
            assert "k7d" in snap.error
            assert "fork" in snap.error
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)


class TestK7dFork:
    async def test_k7d_fork_inherits_state_and_diverges(self, k7_core: K7Core, test_namespace: str):
        """The fork is a warm CoW copy: it inherits the source's in-memory
        state (tmpfs marker) and diverges independently afterwards."""
        source = "test-k7d-fork-src"
        fork = "test-k7d-fork-dst"
        cfg = SandboxConfig(
            name=source, image="alpine:3.20", namespace=test_namespace, backend="k7d", node_name=_LOCAL_NODE
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(source, test_namespace)
            pre = await k7_core.exec_command(
                source, "echo from-source > /tmp/fork-marker && cat /tmp/fork-marker", namespace=test_namespace
            )
            assert pre.exit_code == 0

            fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"
            assert "warm fork" in fork_result.message

            _wait_pod_ready(fork, test_namespace, timeout=120)

            # Inheritance: the fork carries the source's pre-fork memory state.
            inherited = await k7_core.exec_command(fork, "cat /tmp/fork-marker", namespace=test_namespace)
            assert inherited.exit_code == 0, f"exec in fork failed: {inherited.stderr}"
            assert "from-source" in inherited.stdout, "fork did not inherit the source's tmpfs state"

            # Independence: post-fork writes do not leak in either direction.
            # Content-based checks — k7's exec API doesn't surface exit codes
            # (`exec_command` returns 0 on any transport success), so we look
            # at what `cat` actually printed.
            await k7_core.exec_command(fork, "echo forked > /tmp/divergence", namespace=test_namespace)
            source_view = await k7_core.exec_command(source, "cat /tmp/divergence 2>&1", namespace=test_namespace)
            assert "forked" not in source_view.stdout, "fork write leaked back into the source VM"

            await k7_core.exec_command(source, "echo original > /tmp/source-only", namespace=test_namespace)
            fork_view = await k7_core.exec_command(fork, "cat /tmp/source-only 2>&1", namespace=test_namespace)
            assert "original" not in fork_view.stdout, "source write leaked into the fork VM"

            # The source must stay fully usable after being fork-prepared.
            still_alive = await k7_core.exec_command(source, "echo src-alive", namespace=test_namespace)
            assert still_alive.exit_code == 0
            assert "src-alive" in still_alive.stdout
        finally:
            await k7_core.delete_sandbox(fork, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_k7d_fork_latency(self, k7_core: K7Core, test_namespace: str):
        """Measure end-to-end `k7 fork` latency (includes pod scheduling)."""
        source = "test-k7d-bench-src"
        fork = "test-k7d-bench-dst"
        cfg = SandboxConfig(
            name=source, image="alpine:3.20", namespace=test_namespace, backend="k7d", node_name=_LOCAL_NODE
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(source, test_namespace)
            start = time.monotonic()
            fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
            elapsed = time.monotonic() - start
            assert fork_result.success, f"fork failed: {fork_result.error}"
            print(f"k7d fork latency (end-to-end incl. k8s): {elapsed * 1000:.0f}ms")
            # Generous ceiling — the VM-level fork is ~ms; kubelet pod
            # admission dominates. Guards against silent cold-boot regressions
            # (a cold boot + image pull would blow way past this).
            assert elapsed < 60, f"fork took {elapsed:.1f}s — warm-fork path is likely broken"
        finally:
            await k7_core.delete_sandbox(fork, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)


class TestK7dSidecarDocker:
    async def test_k7d_sidecar_docker_run(self, k7_core: K7Core, test_namespace: str):
        """docker-in-VM: dind sidecar + workload container in ONE k7d microVM."""
        name = "sc-docker-run-k7d"
        cfg = SandboxConfig(
            name=name,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="k7d",
            sidecar="docker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(name, test_namespace)
            deadline = time.time() + 120
            while time.time() < deadline:
                probe = await k7_core.exec_command(
                    name, "docker info >/dev/null 2>&1 && echo ok", namespace=test_namespace
                )
                if probe.exit_code == 0 and "ok" in probe.stdout:
                    break
                await asyncio.sleep(3)
            else:
                raise TimeoutError(f"Docker daemon not ready in {name} within 120s")

            run_result = await k7_core.exec_command(name, "docker run --rm hello-world", namespace=test_namespace)
            assert run_result.exit_code == 0, f"docker run failed: {run_result.stderr}"
            assert "Hello from Docker!" in run_result.stdout
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_k7d_sidecar_fork_rejected_loudly(self, k7_core: K7Core, test_namespace: str):
        """Forking a sidecar sandbox is a documented k7d limitation — loud error."""
        name = "sc-docker-nofork-k7d"
        cfg = SandboxConfig(
            name=name,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="k7d",
            sidecar="docker",
            node_name=_LOCAL_NODE,
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"
        try:
            _wait_pod_ready(name, test_namespace)
            fork_result = await k7_core.fork_sandbox(name, f"{name}-fork", namespace=test_namespace)
            assert not fork_result.success
            assert "sidecar" in fork_result.error
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)
