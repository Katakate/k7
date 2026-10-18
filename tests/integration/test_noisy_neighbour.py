"""Per-VM network caps hold under load, and unenforceable limits are refused.

kfd and kql cap guest traffic with Kata's hypervisor rate limiters
(Firecracker token buckets on kfd, tc on the tap for kata-qemu), set from the
tenant's ``limits.network`` as pod annotations. ``iperf3`` runs inside the
sandbox against a runc server pod the test owns; each direction is averaged
over 30 s, as the server reports it, and must sit within 10% of the cap. ``fio`` is not run: no backend can
enforce ``diskIO`` yet, so every backend refuses it with 400 (asserted here).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from uuid import uuid4

import pytest

from k7.core.tenancy import kata_rate

from .cluster import KUBECTL, kubectl
from .test_tenancy import (  # noqa: F401  (admin, base, make_tenant: fixtures)
    IMAGE,
    KFD,
    KQL,
    Tenant,
    admin,
    base,
    error,
    kjson,
    make_tenant,
    sandboxes_in,
)

pytestmark = pytest.mark.integration

IPERF3 = "networkstatic/iperf3@sha256:bc267517f534d9f7cced2669f325799a1fd7fe1d5953c2ca2c6ea1db61affa08"
CAP = 50_000_000  # bits/s
SECONDS = 30
EGRESS_ANN = "io.katacontainers.config.hypervisor.tx_rate_limiter_max_rate"
INGRESS_ANN = "io.katacontainers.config.hypervisor.rx_rate_limiter_max_rate"


@dataclass(frozen=True)
class IperfServer:
    ip: str
    ns: str

    def results(self) -> list[dict]:
        """Every finished test so far: ``iperf3 -s -J`` logs one JSON document per test."""
        text = kubectl("-n", self.ns, "logs", "iperf3", check=True).stdout
        docs, i, decoder = [], text.find("{"), json.JSONDecoder()
        while i != -1:
            doc, i = decoder.raw_decode(text, i)
            docs.append(doc)
            i = text.find("{", i)
        return docs


@pytest.fixture(scope="module")
def iperf_server():
    """A runc iperf3 server pod in its own namespace."""
    ns = f"it41l-iperf-{uuid4().hex[:6]}"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "iperf3", "namespace": ns, "labels": {"app": "it41l-iperf3"}},
        "spec": {"containers": [{"name": "iperf3", "image": IPERF3, "args": ["-s", "-J"]}]},
    }
    kubectl("create", "namespace", ns, check=True)
    try:
        subprocess.run([*KUBECTL, "create", "-f", "-"], input=json.dumps(pod), text=True, check=True)
        kubectl("-n", ns, "wait", "--for=condition=Ready", "pod/iperf3", "--timeout=180s", check=True)
        yield IperfServer(kjson("-n", ns, "get", "pod", "iperf3")["status"]["podIP"], ns)
    finally:
        kubectl("delete", "namespace", ns, "--wait=true", "--timeout=180s", check=True)


def _iperf(t: Tenant, sandbox: str, server: IperfServer, *, reverse: bool, seconds: int = SECONDS) -> float:
    # Read from the runc server, not exec stdout: k7 exec on Kata backends can lose the tail of stdout
    # (the exit-status frame still arrives), which would truncate the client's JSON.
    before = len(server.results())
    cmd = f"iperf3 -c {server.ip} -t {seconds}{' -R' if reverse else ''} > /dev/null"
    r = t("POST", f"/api/v1/sandboxes/{sandbox}/exec", params={"namespace": t.ns}, json={"command": cmd})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["exit_code"] == 0, r.text
    docs = server.results()
    assert len(docs) == before + 1, f"expected one new iperf3 result on the server, got {len(docs) - before}"
    # The server only has its own side: received for egress, sent (flow-controlled by the guest's
    # rx limiter, off by at most the bytes in flight over 30 s) for ingress.
    return float(docs[-1]["end"]["sum_sent" if reverse else "sum_received"]["bits_per_second"])


def _sandbox(t: Tenant, name: str, backend: str, **extra) -> None:
    payload = {
        "name": name,
        "image": IPERF3,
        "namespace": t.ns,
        "backend": backend,
        "entrypoint": ["sleep", "infinity"],
        "limits": {"cpu": "1", "memory": "1Gi"},
        **extra,
    }
    r = t("POST", "/api/v1/sandboxes", json=payload)
    assert r.status_code == 201, r.text
    t.wait_ready(name)


@pytest.mark.parametrize("backend", [KFD, KQL], ids=["kfd", "kql"])
def test_network_caps_hold_for_30s(make_tenant, iperf_server, backend):  # noqa: F811
    t = make_tenant(limits={"network": {"egressBps": str(CAP), "ingressBps": str(CAP)}})
    _sandbox(t, "capped", backend)
    pod = kjson("-n", t.ns, "get", "pod", "-l", "katakate.org/sandbox=capped")["items"][0]
    anns, want = pod["metadata"]["annotations"], str(kata_rate(backend, CAP))
    assert anns.get(EGRESS_ANN) == want and anns.get(INGRESS_ANN) == want, anns

    egress = _iperf(t, "capped", iperf_server, reverse=False)
    ingress = _iperf(t, "capped", iperf_server, reverse=True)
    print(f"[noisy-neighbour] {backend}: egress {egress / 1e6:.1f} Mbit/s, ingress {ingress / 1e6:.1f} Mbit/s, cap 50")
    for what, rate in (("egress", egress), ("ingress", ingress)):
        # Spec bound: at most the cap + 10%. The floor only proves traffic flowed through the cap.
        assert CAP * 0.5 <= rate <= CAP * 1.1, f"{backend} {what} {rate / 1e6:.1f} Mbit/s vs cap 50 Mbit/s (+10%)"

    _sandbox(t, "uncapped-request", backend, network_limits={"egressBps": "20M"})
    pod = kjson("-n", t.ns, "get", "pod", "-l", "katakate.org/sandbox=uncapped-request")["items"][0]
    lowered = str(kata_rate(backend, 20_000_000))
    assert pod["metadata"]["annotations"].get(EGRESS_ANN) == lowered, "a request may lower the tenant cap"


@pytest.mark.parametrize("backend", [KFD, KQL], ids=["kfd", "kql"])
def test_uncapped_baseline_is_well_above_the_cap(make_tenant, iperf_server, backend):  # noqa: F811
    """Without a cap the same path runs far faster, so the capped result above is the limiter's doing."""
    t = make_tenant()
    _sandbox(t, "free", backend)
    rate = _iperf(t, "free", iperf_server, reverse=False, seconds=10)
    print(f"[noisy-neighbour] {backend}: uncapped egress {rate / 1e6:.1f} Mbit/s")
    assert rate > 4 * CAP, f"uncapped {backend} egress {rate / 1e6:.1f} Mbit/s; the cap test would prove nothing"


ALL = [KFD, KQL, "k7d", "k7d-fc"]
IDS = ["kfd", "kql", "native-vmm", "native-fc"]


def _refused(t: Tenant, backend: str, limit: str, **extra) -> None:
    payload = {
        "name": f"r-{uuid4().hex[:4]}",
        "image": IMAGE,
        "namespace": t.ns,
        "backend": backend,
        "limits": {"cpu": "250m", "memory": "256Mi"},
        **extra,
    }
    r = t("POST", "/api/v1/sandboxes", json=payload)
    assert r.status_code == 400, r.text
    err = error(r)
    assert err["code"] == "LimitNotEnforceable" and err["details"] == {"backend": backend, "limit": limit}, r.text
    assert sandboxes_in(t.ns) == []


@pytest.mark.parametrize("backend", ALL, ids=IDS)
def test_disk_io_is_refused_on_every_backend(make_tenant, backend):  # noqa: F811
    _refused(make_tenant(limits={"diskIO": {"writeBps": "100M"}}), backend, "diskIO")


@pytest.mark.parametrize("backend", ALL[2:], ids=IDS[2:])
def test_network_and_disk_refused_on_native_backends(make_tenant, backend):  # noqa: F811
    _refused(make_tenant(limits={"network": {"egressBps": "50M"}}), backend, "network")
    _refused(make_tenant(quotas={"disk": "50G"}), backend, "disk")


def test_request_cannot_raise_the_tenant_cap(make_tenant):  # noqa: F811
    t = make_tenant(limits={"network": {"egressBps": "50M"}})
    body = {"name": "greedy", "image": IMAGE, "namespace": t.ns, "backend": KFD, "network_limits": {"egressBps": "1G"}}
    r = t("POST", "/api/v1/sandboxes", json={**body, "limits": {"cpu": "250m", "memory": "256Mi"}})
    assert r.status_code == 400 and "cap" in r.text, r.text
    assert sandboxes_in(t.ns) == []
