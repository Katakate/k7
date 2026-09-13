"""Integration tests for the k7d-fc backend.

Same daemon socket as ``k7d``; pods use ``runtimeClassName: k7-fc``.
Skipped when RuntimeClass ``k7-fc`` is not registered.

Daemon-socket verbs: create / fork / pause / resume, plus
hostPath refused before the pod exists. CRI exec / kube Ready
work the same as native k7d (CHALLENGES #15).
"""

import asyncio
import json
import os
import subprocess
import time

import pytest

from k7.core.core import K7D_FC_VIRTIOFS_REFUSED, K7Core
from k7.core.models import SandboxConfig

pytestmark = [pytest.mark.integration, pytest.mark.k7d]

_LOCAL_NODE = os.uname().nodename


def _runtimeclass_present(name: str) -> bool:
    result = subprocess.run(
        ["k3s", "kubectl", "get", "runtimeclass", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_k7_fc():
    if not _runtimeclass_present("k7-fc"):
        pytest.skip("RuntimeClass k7-fc not registered")


def _wait_pod_ready(sandbox_name: str, namespace: str, timeout: int = 300) -> None:
    """Block until the sandbox pod is Ready.

    Ready is the exec readiness probe. That probe succeeds on k7-fc
    the same way it does on native k7.
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
        if result.returncode == 0:
            try:
                items = json.loads(result.stdout).get("items", [])
                for pod in items:
                    status = pod.get("status", {})
                    if status.get("phase") != "Running":
                        continue
                    statuses = status.get("containerStatuses") or []
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


class TestK7dFcLifecycle:
    async def test_k7d_fc_create_pause_resume(self, k7_core: K7Core, test_namespace: str):
        name = "test-k7d-fc-lifecycle"
        cfg = SandboxConfig(
            name=name,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="k7d-fc",
            node_name=_LOCAL_NODE,
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(name, test_namespace)
            assert _pod_runtime_class(name, test_namespace) == "k7-fc"

            pause_result = await k7_core.pause_sandbox(name, namespace=test_namespace)
            assert pause_result.success, f"pause failed: {pause_result.error}"
            assert "frozen" in pause_result.message

            await asyncio.sleep(3)

            resume_result = await k7_core.resume_sandbox(name, namespace=test_namespace)
            assert resume_result.success, f"resume failed: {resume_result.error}"
        finally:
            delete_result = await k7_core.delete_sandbox(name, namespace=test_namespace)
            assert delete_result.success, f"delete failed: {delete_result.error}"

    async def test_k7d_fc_fork_inherits_state(self, k7_core: K7Core, test_namespace: str):
        source = "test-k7d-fc-fork-src"
        fork = "test-k7d-fc-fork-dst"
        cfg = SandboxConfig(
            name=source,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="k7d-fc",
            node_name=_LOCAL_NODE,
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(source, test_namespace)
            assert _pod_runtime_class(source, test_namespace) == "k7-fc"

            fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"
            assert "warm fork" in fork_result.message

            _wait_pod_ready(fork, test_namespace, timeout=120)
            assert _pod_runtime_class(fork, test_namespace) == "k7-fc"
        finally:
            await k7_core.delete_sandbox(fork, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_k7d_fc_exec_echo_hello(self, k7_core: K7Core, test_namespace: str):
        """CRI exec into a k7-fc sandbox returns; kube Ready is a valid signal."""
        name = "test-k7d-fc-exec"
        cfg = SandboxConfig(
            name=name,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="k7d-fc",
            node_name=_LOCAL_NODE,
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(name, test_namespace)
            assert _pod_runtime_class(name, test_namespace) == "k7-fc"
            exec_result = await k7_core.exec_command(name, "echo hello", namespace=test_namespace)
            assert "hello" in (exec_result.stdout or ""), exec_result.stdout
        finally:
            delete_result = await k7_core.delete_sandbox(name, namespace=test_namespace)
            assert delete_result.success, f"delete failed: {delete_result.error}"

    async def test_k7d_fc_hostpath_refused_up_front(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="test-k7d-fc-hostpath",
            image="alpine:3.20",
            namespace=test_namespace,
            backend="k7d-fc",
            volumes=[{"name": "hp", "hostPath": {"path": "/var/log"}}],
        )
        result = await k7_core.create_sandbox(cfg)
        assert not result.success
        assert result.error == K7D_FC_VIRTIOFS_REFUSED
        listed = subprocess.run(
            ["k3s", "kubectl", "get", "deploy", "test-k7d-fc-hostpath", "-n", test_namespace],
            capture_output=True,
            text=True,
            check=False,
        )
        assert listed.returncode != 0, "hostPath k7d-fc create must not leave a Deployment"
