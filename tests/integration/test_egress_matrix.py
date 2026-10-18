"""Egress x backend x fork traffic matrix.

Every cell proves with real packets that a sandbox — and, where the backend
forks, its fork and then the source again — gets exactly the egress asked
for. k7d / k7d-fc ``exec`` rides vsock, so a fork with a dead NIC looks healthy
to everything but a probe; restricted modes probe a blocked *raw IP* so a fork
whose traffic skips its pod's Cilium endpoint shows up as an unexpected OK.
"""

import asyncio
import os
import subprocess

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from k7.core.core import KFD_FORK_REJECT, K7Core
from k7.core.models import SandboxConfig

from .cluster import KUBECTL
from .netprobe import reachable, wget
from .test_cilium import _wait_ready, requires_cilium
from .test_sandbox_ingress import _read_cnp, _read_netpol

pytestmark = [pytest.mark.integration, pytest.mark.egress_matrix, requires_cilium]

_LOCAL_NODE = os.uname().nodename
_KFD = "kata-firecracker-devmapper"
_BACKENDS = [_KFD, "kata-qemu-longhorn", "k7d", "k7d-fc"]

# Serves 200 with no redirect: a CIDR-only policy has no DNS rule, so a probe
# that follows http://1.1.1.1/'s 301 to one.one.one.one reads as BLOCKED.
_ONE = "https://1.1.1.1/cdn-cgi/trace"
# mode -> (egress_whitelist, allowed URLs, blocked URLs)
_MODES: dict[str, tuple[list[str] | None, list[str], list[str]]] = {
    "open": (None, [_ONE, "https://example.com/"], []),
    "block-all": ([], [], [_ONE, "https://example.com/"]),
    "fqdn": (["example.com"], ["https://example.com/"], ["https://www.iana.org/", _ONE]),
    "cidr": (["1.1.1.1/32"], [_ONE], ["http://8.8.8.8/"]),
}


def _node_has_backend(backend: str) -> bool:
    out = subprocess.run(
        [
            *KUBECTL,
            "get",
            "node",
            _LOCAL_NODE,
            "-o",
            f"jsonpath={{.metadata.labels.k7\\.katakate\\.org/backend-{backend}}}",
        ],
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and out.stdout.strip() == "true"


async def _assert_probes(k7_core: K7Core, name: str, namespace: str, where: str, backend: str, mode: str) -> None:
    _, allowed, blocked = _MODES[mode]
    for url in allowed:
        ok = False
        for _ in range(3):
            ok = await reachable(k7_core, name, wget(url, 10), namespace)
            if ok:
                break
            await asyncio.sleep(3)
        assert ok, f"[{backend}/{mode}/{where}] {url} must be reachable"
    for url in blocked:
        assert not await reachable(k7_core, name, wget(url, 10), namespace), (
            f"[{backend}/{mode}/{where}] {url} must be blocked"
        )


async def _absent(read, *args) -> bool:
    try:
        await read(*args)
    except ApiException as e:
        assert e.status == 404, e
        return True
    return False


async def _assert_policy(k7_core: K7Core, name: str, namespace: str, mode: str) -> None:
    whitelist, _, _ = _MODES[mode]
    cnp, netpol = f"{name}-egress", f"{name}-netpol"
    if mode == "open":
        assert await _absent(_read_cnp, k7_core, cnp, namespace), f"open sandbox {name} has {cnp}"
        assert await _absent(_read_netpol, k7_core, netpol, namespace), f"open sandbox {name} has {netpol}"
    elif mode == "fqdn":
        spec = (await _read_cnp(k7_core, cnp, namespace))["spec"]
        assert spec["endpointSelector"]["matchLabels"]["katakate.org/sandbox"] == name
        assert any(
            "toFQDNs" in r and any(m.get("matchName") == whitelist[0] for m in r["toFQDNs"]) for r in spec["egress"]
        ), f"{cnp} does not allow {whitelist[0]}: {spec['egress']!r}"
    else:
        np = await _read_netpol(k7_core, netpol, namespace)
        assert np.spec.pod_selector.match_labels == {"katakate.org/sandbox": name}
        assert np.spec.policy_types == ["Egress"]
        cidrs = [p.ip_block.cidr for r in (np.spec.egress or []) for p in (r.to or []) if p.ip_block]
        if mode == "block-all":
            assert not np.spec.egress, f"block-all {netpol} has egress rules: {np.spec.egress!r}"
        else:
            assert whitelist[0] in cidrs, f"{netpol} does not allow {whitelist[0]}: {cidrs!r}"
    deny = await _read_netpol(k7_core, f"{name}-deny-ingress", namespace)
    assert deny.spec.ingress in (None, []), f"{name}-deny-ingress admits ingress: {deny.spec.ingress!r}"


@pytest.mark.parametrize("mode", list(_MODES))
@pytest.mark.parametrize("backend", _BACKENDS)
async def test_egress_matrix(k7_core: K7Core, test_namespace: str, backend: str, mode: str):
    if not _node_has_backend(backend):
        pytest.skip(f"node {_LOCAL_NODE} lacks label k7.katakate.org/backend-{backend}=true")
    short = {"kata-firecracker-devmapper": "kfd", "kata-qemu-longhorn": "kql"}.get(backend, backend)
    source, fork = f"em-{short}-{mode}", f"em-{short}-{mode}-f"
    cfg = SandboxConfig(
        name=source,
        image="alpine:3.20",
        namespace=test_namespace,
        backend=backend,
        egress_whitelist=_MODES[mode][0],
        # Every cell stays on the node the suite runs on: k7d VM ops are
        # node-local, and a cell must not land on a host another suite owns.
        node_name=_LOCAL_NODE,
    )
    forked = False
    try:
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create {source} failed: {result.error}"
        await _wait_ready(k7_core, source, test_namespace, timeout=300)
        await _assert_policy(k7_core, source, test_namespace, mode)
        await _assert_probes(k7_core, source, test_namespace, "source", backend, mode)

        fork_result = await k7_core.fork_sandbox(source, fork, namespace=test_namespace)
        if backend == _KFD:
            assert not fork_result.success and fork_result.error == KFD_FORK_REJECT, (
                f"kfd fork must be refused loudly: {fork_result!r}"
            )
            return
        forked = True
        assert fork_result.success, f"fork {source} -> {fork} failed: {fork_result.error}"
        await _assert_policy(k7_core, fork, test_namespace, mode)
        # Longhorn clone + kata-qemu boot of a fork can pass 300s on a busy node.
        await _wait_ready(k7_core, fork, test_namespace, timeout=600)
        await _assert_probes(k7_core, fork, test_namespace, "fork", backend, mode)
        await _assert_probes(k7_core, source, test_namespace, "source-after-fork", backend, mode)
    finally:
        deletes = ([fork] if forked else []) + [source]
        results = [await k7_core.delete_sandbox(n, namespace=test_namespace) for n in deletes]
        for n, r in zip(deletes, results, strict=True):
            assert r.success, f"delete {n} failed: {r.error}"
