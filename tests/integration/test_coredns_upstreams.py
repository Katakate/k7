"""CoreDNS forwards to k7_dns_upstreams, or to the host's resolvers when unset.

The k3s Corefile forwards `.` to /etc/resolv.conf, which the kubelet fills
from k3s's --resolv-conf (K3S_RESOLV_CONF in the unit's env file) when the
pod is created. A privileged hostPID pod on each CoreDNS node reads that env
line, the file it names, and the live CoreDNS process's /etc/resolv.conf.

K7_DNS_UPSTREAMS is the value the cluster was installed with (comma-separated;
utils/ci/k7-e2e.sh exports it): set, the resolvers must equal it; set but
empty, the host's resolvers must be in use; unset, only the env file →
kubelet → CoreDNS chain is checked.
"""

import json
import os
import time
import uuid

import pytest

from .cluster import kubectl

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        bool(os.environ.get("K7_KUBECONFIG")),
        reason="k7_dns_upstreams is a playbook (K3s) setting; the Helm chart does not manage CoreDNS",
    ),
]

_K7_RESOLV = "/etc/k7/resolv.conf"
_SCAN_TIMEOUT = 120
_SCAN = r"""
set -eu
path=$(cat /proc/1/root/etc/systemd/system/k3s.service.env /proc/1/root/etc/systemd/system/k3s-agent.service.env \
  2>/dev/null | sed -n "s/^K3S_RESOLV_CONF=//p" | tr -d "'\"" | head -n 1)
[ -n "$path" ] || { echo "no K3S_RESOLV_CONF in the k3s unit env file" >&2; exit 1; }
echo "path $path"
awk '$1 == "nameserver" {print "kubelet", $2}' "/proc/1/root$path"
found=0
for d in /proc/[0-9]*; do
  [ "$(cat "$d/comm" 2>/dev/null)" = coredns ] || continue
  found=1
  awk -v p="${d#/proc/}" '$1 == "nameserver" {print "coredns-" p, $2}' "$d/root/etc/resolv.conf"
done
[ "$found" = 1 ] || { echo "no coredns process on this node" >&2; exit 1; }
"""


def _coredns_nodes() -> set[str]:
    pods = json.loads(
        kubectl("-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns", "-o", "json", check=True).stdout
    )
    nodes = {
        p["spec"]["nodeName"]
        for p in pods["items"]
        if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")
    }
    assert nodes, "no Running CoreDNS pod in kube-system"
    return nodes


def _scan(node: str) -> tuple[str, list[str], dict[str, list[str]]]:
    """(K3S_RESOLV_CONF path, its nameservers, {coredns pid: its nameservers}) on ``node``."""
    pod = f"dns-scan-{uuid.uuid4().hex[:8]}"
    overrides = {
        "spec": {
            "nodeName": node,
            "hostPID": True,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "scan",
                    "image": "alpine:3.20",
                    "command": ["sh", "-c", _SCAN],
                    "securityContext": {"privileged": True},
                }
            ],
        }
    }
    kubectl("run", pod, "--image=alpine:3.20", "--restart=Never", f"--overrides={json.dumps(overrides)}", check=True)
    try:
        deadline = time.time() + _SCAN_TIMEOUT
        while (phase := kubectl("get", "pod", pod, "-o", "jsonpath={.status.phase}").stdout.strip()) not in (
            "Succeeded",
            "Failed",
        ):
            assert time.time() < deadline, f"DNS scan pod on {node} still {phase or 'Pending'} after {_SCAN_TIMEOUT}s"
            time.sleep(2)
        logs = kubectl("logs", pod, check=True).stdout
        assert phase == "Succeeded", f"DNS scan pod on {node} failed:\n{logs}"
    finally:
        kubectl("delete", "pod", pod, "--ignore-not-found", "--wait=false")
    path, kubelet, coredns = "", [], {}
    for key, value in (line.split(" ", 1) for line in logs.splitlines() if line.strip()):
        if key == "path":
            path = value
        elif key == "kubelet":
            kubelet.append(value)
        else:
            coredns.setdefault(key, []).append(value)
    return path, kubelet, coredns


def test_corefile_forwards_to_the_pod_resolv_conf():
    corefile = kubectl(
        "-n", "kube-system", "get", "configmap", "coredns", "-o", "jsonpath={.data.Corefile}", check=True
    )
    assert "forward . /etc/resolv.conf" in corefile.stdout, corefile.stdout


def test_coredns_upstreams_match_k7_dns_upstreams():
    expected = os.environ.get("K7_DNS_UPSTREAMS")
    for node in sorted(_coredns_nodes()):
        path, kubelet, coredns = _scan(node)
        assert kubelet, f"{node}: {path} has no nameserver"
        assert coredns, f"{node}: no CoreDNS process found"
        for pid, servers in coredns.items():
            # The kubelet keeps the first 3 nameservers.
            assert servers == kubelet[:3], f"{node} {pid} forwards to {servers}, but the kubelet's {path} has {kubelet}"
        if expected is None:
            continue
        upstreams = [s.strip() for s in expected.split(",") if s.strip()]
        if upstreams:
            assert (path, kubelet) == (_K7_RESOLV, upstreams), f"{node}: k3s uses {path} = {kubelet}"
        else:
            assert path != _K7_RESOLV, f"{node}: K7_DNS_UPSTREAMS is empty, but k3s still uses {_K7_RESOLV}"
