"""Cluster-wide VMM process leak detection (spec 18f issue 1b).

Under parallel kata-fc pod churn the kata shim intermittently fails to
kill the Firecracker microVM on pod deletion (shim logs "Agent did not
stop sandbox: Dead agent" / "getting vm status failed: ...
firecracker.socket: no such file or directory"), leaving orphaned
``firecracker`` processes spinning at 100% CPU. Spec 18e observed 14
orphans across the cluster with zero live Kata pods.

This module is named ``test_zz_*`` so pytest runs it LAST: after the
whole suite (and its sandbox churn) it asserts that every node's VMM
process count exactly matches its live Kata pod count — zero orphans.
Host processes are counted through short-lived hostPID scan pods pinned
to each node (runc runtime, so they never perturb the counts).
"""

import json
import subprocess
import time
import uuid

import pytest

pytestmark = pytest.mark.integration

_K3S = "/usr/local/bin/k3s"
# RuntimeClass → VMM process comm prefix expected per pod (one VMM each).
# k7d pods intentionally absent: their VMs live inside the single k7d
# daemon process, not in per-pod hypervisor processes.
_VMM_BY_RUNTIME_CLASS = {"kata": "firecracker", "kata-qemu": "qemu"}
_SCAN_TIMEOUT = 60
# Shims reap VMMs asynchronously after pod deletion — give the cluster a
# grace window to converge before declaring a leak.
_CONVERGE_TIMEOUT = 180


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([_K3S, "kubectl", *args], capture_output=True, text=True, check=check)


def _node_names() -> list[str]:
    out = _kubectl("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}").stdout
    return out.split()


def _live_kata_pods_per_node() -> dict[str, dict[str, int]]:
    """{node: {vmm_comm: live pod count}} for kata-backed pods. A pod is
    "live" (expected to have a VMM) unless it already finished; pods being
    created/deleted are counted too — convergence is handled by retrying."""
    data = json.loads(_kubectl("get", "pods", "-A", "-o", "json").stdout)
    counts: dict[str, dict[str, int]] = {}
    for pod in data["items"]:
        rc = pod["spec"].get("runtimeClassName")
        vmm = _VMM_BY_RUNTIME_CLASS.get(rc or "")
        node = pod["spec"].get("nodeName")
        if not vmm or not node:
            continue
        if pod["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        counts.setdefault(node, {}).setdefault(vmm, 0)
        counts[node][vmm] += 1
    return counts


def _vmm_processes_on_node(node: str) -> dict[str, int]:
    """Count firecracker/qemu processes on a node via a hostPID scan pod."""
    pod_name = f"leak-scan-{uuid.uuid4().hex[:8]}"
    overrides = {
        "spec": {
            "nodeName": node,
            "hostPID": True,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "scan",
                    "image": "alpine:3.20",
                    "command": ["ps", "-o", "comm"],
                }
            ],
        }
    }
    _kubectl(
        "run",
        pod_name,
        "--image=alpine:3.20",
        "--restart=Never",
        f"--overrides={json.dumps(overrides)}",
    )
    try:
        deadline = time.time() + _SCAN_TIMEOUT
        while time.time() < deadline:
            phase = _kubectl("get", "pod", pod_name, "-o", "jsonpath={.status.phase}", check=False).stdout.strip()
            if phase == "Succeeded":
                break
            if phase == "Failed":
                raise RuntimeError(f"leak scan pod on {node} failed")
            time.sleep(2)
        else:
            raise TimeoutError(f"leak scan pod on {node} did not complete within {_SCAN_TIMEOUT}s")
        comms = _kubectl("logs", pod_name).stdout.splitlines()
    finally:
        _kubectl("delete", "pod", pod_name, "--ignore-not-found", "--wait=false", check=False)
    return {
        "firecracker": sum(1 for c in comms if c.strip() == "firecracker"),
        "qemu": sum(1 for c in comms if c.strip().startswith("qemu")),
    }


class TestVmmProcessLeaks:
    def test_no_orphaned_vmm_processes_cluster_wide(self):
        """Every node's firecracker/qemu process count must equal its live
        Kata pod count. More processes than pods == orphaned VMMs (the
        kata-fc shim leak, CHALLENGES.md #6); fewer == a pod without its
        VMM. Both are loud failures."""
        nodes = _node_names()
        assert nodes, "no Kubernetes nodes found"

        deadline = time.time() + _CONVERGE_TIMEOUT
        mismatches: list[str] = []
        while time.time() < deadline:
            expected = _live_kata_pods_per_node()
            actual = {node: _vmm_processes_on_node(node) for node in nodes}
            mismatches = []
            for node in nodes:
                for vmm in ("firecracker", "qemu"):
                    want = expected.get(node, {}).get(vmm, 0)
                    got = actual[node][vmm]
                    if got != want:
                        mismatches.append(f"{node}: {got} {vmm} process(es) but {want} live kata pod(s) expecting one")
            if not mismatches:
                return
            time.sleep(10)

        pytest.fail(
            "VMM process leak detected (did not converge within "
            f"{_CONVERGE_TIMEOUT}s):\n  " + "\n  ".join(mismatches) + "\n"
            "Orphaned firecracker processes spin at 100% CPU and starve the node "
            "(spec 18f issue 1b / CHALLENGES.md #6). Inspect with "
            "`ps -eo pid,ppid,comm,%cpu | grep -E 'firecracker|qemu'` on the node "
            "and check `journalctl -t kata` for 'Agent did not stop sandbox'."
        )
