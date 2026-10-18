"""Integration tests for the cluster-wide sandbox platform deny.

The CCNP ``k7-sandbox-platform-deny`` must make the node, the control plane
and cloud metadata unreachable from a sandbox in EVERY egress mode,
including ``--egress-open``, without flipping open sandboxes to
default-deny. Skipped when the Cilium CRD is absent (Flannel clusters get
no platform isolation — see SECURITY.md).

On the playbook cluster the repo manifest is applied by the test itself, and
on a chart install the chart's policy must exist, so a missing policy is a
loud failure, never a silently green run.
"""

import json
import subprocess
from pathlib import Path

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

from .cluster import KUBECTL, system_ns
from .netprobe import verdict
from .test_cilium import _wait_ready, requires_cilium

pytestmark = pytest.mark.integration

_MANIFEST = Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/cilium/sandbox-platform-deny.yaml"
_POLICY = "k7-sandbox-platform-deny"


def _kubectl(*args: str) -> str:
    return subprocess.run([*KUBECTL, *args], check=True, capture_output=True, text=True).stdout


def _node_ipv4s() -> list[str]:
    out = _kubectl("get", "nodes", "-o", "jsonpath={.items[*].status.addresses[?(@.type=='InternalIP')].address}")
    ips = [ip for ip in out.split() if ":" not in ip]
    assert ips, "no IPv4 node InternalIP found"
    return ips


def _probe(url: str, timeout: int = 6) -> str:
    return f"curl -sk -o /dev/null --max-time {timeout} {url}"


@pytest.fixture(scope="module")
def platform_deny_applied() -> None:
    # A chart install owns the policy (its release namespace is a platform
    # namespace too): check it, never overwrite it with the playbook copy.
    if system_ns() == "kube-system":
        assert _MANIFEST.is_file(), f"manifest missing: {_MANIFEST}"
        _kubectl("apply", "-f", str(_MANIFEST))
    spec = json.loads(_kubectl("get", "ciliumclusterwidenetworkpolicy", _POLICY, "-o", "jsonpath={.spec}"))
    denied_ns = {
        ns
        for rule in spec.get("egressDeny", [])
        for ep in rule.get("toEndpoints", [])
        for expr in ep.get("matchExpressions", [])
        if expr["key"] == "k8s:io.kubernetes.pod.namespace" and expr["operator"] == "In"
        for ns in expr["values"]
    }
    assert system_ns() in denied_ns, f"{_POLICY} does not deny sandbox egress to {system_ns()}: {sorted(denied_ns)}"
    assert "egressDeny" in spec and "egress" not in spec and "ingress" not in spec, (
        f"{_POLICY} must be deny-only: an allow section flips --egress-open sandboxes to default-deny"
    )
    assert spec.get("enableDefaultDeny") == {"egress": False, "ingress": False}, (
        f"{_POLICY} must set enableDefaultDeny false: on Cilium 1.19 egressDeny alone enables default-deny"
    )


@requires_cilium
@pytest.mark.usefixtures("platform_deny_applied")
class TestSandboxPlatformDeny:
    async def _open_sandbox(self, k7_core: K7Core, namespace: str, name: str) -> None:
        cfg = SandboxConfig(
            name=name,
            image="alpine:3.20",
            namespace=namespace,
            egress_whitelist=None,  # --egress-open
            before_script="apk add --no-cache curl >/dev/null",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"
        await _wait_ready(k7_core, name, namespace)

    async def _expect(self, k7_core: K7Core, name: str, namespace: str, cmd: str, want: str, what: str) -> None:
        got = await verdict(k7_core, name, cmd, namespace)
        assert got == want, f"{what}: expected {want}, got {got}"

    async def test_open_sandbox_still_reaches_internet(self, k7_core: K7Core, test_namespace: str):
        """egressDeny + enableDefaultDeny=false must not flip an --egress-open
        sandbox to default-deny (the semantics check, encoded)."""
        name = "integ-pd-open"
        await self._open_sandbox(k7_core, test_namespace, name)
        try:
            await self._expect(
                k7_core, name, test_namespace, _probe("https://example.com/", 15), "OK", "--egress-open → example.com"
            )
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_open_sandbox_cannot_reach_platform(self, k7_core: K7Core, test_namespace: str):
        name = "integ-pd-plat"
        await self._open_sandbox(k7_core, test_namespace, name)
        try:
            api_svc = _kubectl("get", "svc", "k7-api", "-n", system_ns(), "-o", "jsonpath={.spec.clusterIP}")
            api_pod = _kubectl(
                "get", "pod", "-n", system_ns(), "-l", "app=k7-api", "-o", "jsonpath={.items[0].status.podIP}"
            )
            lh_svc = _kubectl(
                "get", "svc", "longhorn-backend", "-n", "longhorn-system", "-o", "jsonpath={.spec.clusterIP}"
            )
            checks = {
                "apiserver ClusterIP 10.43.0.1:443": _probe("https://10.43.0.1/"),
                "metadata 169.254.169.254": _probe("http://169.254.169.254/", 4),
                # M2: platform pods by ClusterIP and by pod IP
                f"k7-api ClusterIP {api_svc}:8000": _probe(f"http://{api_svc}:8000/health"),
                f"k7-api pod {api_pod}:8000": _probe(f"http://{api_pod}:8000/health"),
                f"longhorn-backend {lh_svc}:9500": _probe(f"http://{lh_svc}:9500/v1"),
            }
            # Every node: the sandbox's own node is the `host` entity, the
            # others are `remote-node` (multi-node clusters).
            for ip in _node_ipv4s():
                checks[f"node {ip}:6443"] = _probe(f"https://{ip}:6443/")
                checks[f"node {ip}:22"] = f"nc -z -w 4 {ip} 22"
            for what, cmd in checks.items():
                await self._expect(k7_core, name, test_namespace, cmd, "BLOCKED", what)
            # Sanity: the deny is surgical — CoreDNS (in kube-system, the M2
            # namespace) still answers, both via ClusterIP and pod IP.
            dns_pod = _kubectl(
                "get", "pod", "-n", "kube-system", "-l", "k8s-app=kube-dns", "-o", "jsonpath={.items[0].status.podIP}"
            )
            for what, server in (("CoreDNS ClusterIP", "10.43.0.10"), ("CoreDNS pod", dns_pod)):
                await self._expect(
                    k7_core,
                    name,
                    test_namespace,
                    f"nslookup kubernetes.default.svc.cluster.local {server}",
                    "OK",
                    f"{what} {server}:53",
                )
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_fqdn_sandbox_unaffected(self, k7_core: K7Core, test_namespace: str):
        """The deny must compose with the per-sandbox FQDN allow: whitelisted
        domain reachable, everything else (incl. the node) still blocked."""
        name = "integ-pd-fqdn"
        cfg = SandboxConfig(
            name=name,
            image="alpine:3.20",
            namespace=test_namespace,
            egress_whitelist=["example.com"],
            before_script="apk add --no-cache curl >/dev/null",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"
        try:
            await _wait_ready(k7_core, name, test_namespace)
            await self._expect(k7_core, name, test_namespace, _probe("https://example.com/", 15), "OK", "whitelisted")
            await self._expect(
                k7_core, name, test_namespace, _probe("https://www.iana.org/"), "BLOCKED", "non-whitelisted"
            )
            await self._expect(k7_core, name, test_namespace, _probe("https://10.43.0.1/"), "BLOCKED", "apiserver")
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)
