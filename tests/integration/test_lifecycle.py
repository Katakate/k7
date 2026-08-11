"""Backend-agnostic integration tests for sandbox lifecycle."""

import asyncio

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = pytest.mark.integration


class TestSandboxLifecycle:
    async def test_create_exec_delete(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-basic",
            image="alpine:latest",
            namespace=test_namespace,
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            for _ in range(60):
                sandboxes = await k7_core.list_sandboxes(namespace=test_namespace)
                if any(s.name == "integ-basic" and s.ready == "True" for s in sandboxes):
                    break
                await asyncio.sleep(2)

            exec_result = await k7_core.exec_command("integ-basic", "echo hello", namespace=test_namespace)
            assert exec_result.exit_code == 0
            assert "hello" in exec_result.stdout
        finally:
            await k7_core.delete_sandbox("integ-basic", namespace=test_namespace)

    async def test_before_script(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-before",
            image="alpine:latest",
            namespace=test_namespace,
            before_script="touch /tmp/marker",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            for _ in range(60):
                sandboxes = await k7_core.list_sandboxes(namespace=test_namespace)
                if any(s.name == "integ-before" and s.ready == "True" for s in sandboxes):
                    break
                await asyncio.sleep(2)

            exec_result = await k7_core.exec_command(
                "integ-before", "test -f /tmp/marker && echo ok", namespace=test_namespace
            )
            assert exec_result.exit_code == 0
            assert "ok" in exec_result.stdout
        finally:
            await k7_core.delete_sandbox("integ-before", namespace=test_namespace)

    async def test_resource_limits(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-limits",
            image="alpine:latest",
            namespace=test_namespace,
            limits={"cpu": "250m", "memory": "256Mi"},
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            v1 = await k7_core._get_core_v1_client()
            pods = None
            for _ in range(60):
                pods = await v1.list_namespaced_pod(namespace=test_namespace, label_selector="app=integ-limits")
                if pods.items:
                    break
                await asyncio.sleep(2)
            assert pods and len(pods.items) > 0, "Pod never appeared"
            container = pods.items[0].spec.containers[0]
            assert container.resources.limits["cpu"] == "250m"
            assert container.resources.limits["memory"] == "256Mi"
        finally:
            await k7_core.delete_sandbox("integ-limits", namespace=test_namespace)
