"""Integration tests for Cilium CNI + FQDN egress policy.

Skipped automatically when the Cilium CRD is absent (e.g. a cluster still on
Flannel), so the file is safe to run on any k7 node.
"""

import asyncio
import subprocess
import time

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = pytest.mark.integration

_K3S = "/usr/local/bin/k3s"


def _cilium_crd_present() -> bool:
    try:
        result = subprocess.run(
            [_K3S, "kubectl", "get", "crd", "ciliumnetworkpolicies.cilium.io"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


requires_cilium = pytest.mark.skipif(
    not _cilium_crd_present(),
    reason="Cilium CRD not installed (cluster likely running Flannel)",
)


async def _wait_ready(k7_core: K7Core, name: str, namespace: str, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sandboxes = await k7_core.list_sandboxes(namespace=namespace)
        if any(s.name == name and s.ready == "True" for s in sandboxes):
            return
        await asyncio.sleep(3)
    raise AssertionError(f"Sandbox {name} never became Ready within {timeout}s")


@requires_cilium
class TestCiliumFqdnEgress:
    async def test_fqdn_egress_creates_ciliumnetworkpolicy(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-cnp",
            image="alpine:3.20",
            namespace=test_namespace,
            egress_whitelist=["api.openai.com", "10.0.0.0/8"],
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            custom = await k7_core._get_custom_objects_client()
            cnp = await custom.get_namespaced_custom_object(
                group="cilium.io",
                version="v2",
                namespace=test_namespace,
                plural="ciliumnetworkpolicies",
                name="integ-cnp-egress",
            )
            egress = cnp["spec"]["egress"]
            # Must contain a toFQDNs rule naming api.openai.com
            assert any(
                "toFQDNs" in r and any(m.get("matchName") == "api.openai.com" for m in r["toFQDNs"]) for r in egress
            )
            assert any("toCIDR" in r and r["toCIDR"] == ["10.0.0.0/8"] for r in egress)
            # Standard v1 NetworkPolicy must NOT have been created for FQDN flow
            net = await k7_core._get_networking_v1_client()
            from kubernetes_asyncio.client.exceptions import ApiException

            try:
                await net.read_namespaced_network_policy(name="integ-cnp-netpol", namespace=test_namespace)
                pytest.fail("Legacy NetworkPolicy unexpectedly created alongside CiliumNetworkPolicy")
            except ApiException as e:
                assert e.status == 404
        finally:
            await k7_core.delete_sandbox("integ-cnp", namespace=test_namespace)
            # Ensure CNP is deleted along with the sandbox
            from kubernetes_asyncio.client.exceptions import ApiException

            try:
                custom = await k7_core._get_custom_objects_client()
                await custom.get_namespaced_custom_object(
                    group="cilium.io",
                    version="v2",
                    namespace=test_namespace,
                    plural="ciliumnetworkpolicies",
                    name="integ-cnp-egress",
                )
                pytest.fail("CiliumNetworkPolicy was not cleaned up on delete")
            except ApiException as e:
                assert e.status == 404

    async def test_cidr_only_egress_still_uses_v1_networkpolicy(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-cidr",
            image="alpine:3.20",
            namespace=test_namespace,
            egress_whitelist=["10.0.0.0/8"],
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            net = await k7_core._get_networking_v1_client()
            np = await net.read_namespaced_network_policy(name="integ-cidr-netpol", namespace=test_namespace)
            assert np.spec.policy_types == ["Egress"]
            # No CiliumNetworkPolicy for CIDR-only case
            custom = await k7_core._get_custom_objects_client()
            from kubernetes_asyncio.client.exceptions import ApiException

            try:
                await custom.get_namespaced_custom_object(
                    group="cilium.io",
                    version="v2",
                    namespace=test_namespace,
                    plural="ciliumnetworkpolicies",
                    name="integ-cidr-egress",
                )
                pytest.fail("Unexpected CiliumNetworkPolicy for CIDR-only egress")
            except ApiException as e:
                assert e.status == 404
        finally:
            await k7_core.delete_sandbox("integ-cidr", namespace=test_namespace)

    async def test_fqdn_egress_allows_whitelisted_domain(self, k7_core: K7Core, test_namespace: str):
        cfg = SandboxConfig(
            name="integ-fqdn-net",
            image="alpine:3.20",
            namespace=test_namespace,
            egress_whitelist=["example.com"],
            before_script="apk add --no-cache curl >/dev/null",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            await _wait_ready(k7_core, "integ-fqdn-net", test_namespace)

            allowed = await k7_core.exec_command(
                "integ-fqdn-net",
                "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 https://example.com/",
                namespace=test_namespace,
            )
            assert allowed.exit_code == 0
            assert allowed.stdout.strip().startswith("2") or allowed.stdout.strip().startswith("3")

            blocked = await k7_core.exec_command(
                "integ-fqdn-net",
                "curl -sS -o /dev/null --max-time 8 https://www.iana.org/ && echo OK || echo BLOCKED",
                namespace=test_namespace,
            )
            assert "BLOCKED" in blocked.stdout, (
                f"Expected non-whitelisted domain to be blocked, got stdout={blocked.stdout!r} "
                f"stderr={blocked.stderr!r}"
            )
        finally:
            await k7_core.delete_sandbox("integ-fqdn-net", namespace=test_namespace)

    async def test_docker_pull_through_fqdn_whitelist(self, k7_core: K7Core, test_namespace: str):
        """`docker pull` from Docker Hub through an FQDN egress whitelist
        (spec 18f issue 3). Hub blobs are served from CDN hosts with nested
        subdomains (production.cloudfront.docker.com) and 30-60s DNS TTLs —
        this exercises the `*.` → `**.` matchPattern translation and the
        dnsProxy.minTtl tuning end-to-end."""
        name = "integ-fqdn-pull"
        cfg = SandboxConfig(
            name=name,
            image="docker:27.5-cli",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
            sidecar="docker",
            egress_whitelist=["docker.io", "*.docker.io", "*.docker.com", "*.cloudfront.net"],
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            await _wait_ready(k7_core, name, test_namespace, timeout=300)

            deadline = time.time() + 180
            while time.time() < deadline:
                probe = await k7_core.exec_command(
                    name, "docker info >/dev/null 2>&1 && echo ok", namespace=test_namespace
                )
                if probe.exit_code == 0 and "ok" in probe.stdout:
                    break
                await asyncio.sleep(3)
            else:
                raise TimeoutError(f"Docker daemon not ready in {name} within 180s")

            pull = await k7_core.exec_command(name, "docker pull alpine:3.20 2>&1 | tail -3", namespace=test_namespace)
            assert "Downloaded newer image" in pull.stdout or "Image is up to date" in pull.stdout, (
                f"docker pull through FQDN whitelist failed — CDN egress is broken again "
                f"(spec 18f issue 3): stdout={pull.stdout!r} stderr={pull.stderr!r}"
            )
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)
