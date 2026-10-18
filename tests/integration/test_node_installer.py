"""Node installer on a live cluster: RuntimeClass node selectors, the node-ready
label, and the own-node admission policy evaluated with a real pod-bound token.

The installer parts skip on a K3s playbook install (no k7-node-installer
DaemonSet, no ValidatingAdmissionPolicy). Policy probes are ``--dry-run=server``:
admission runs, nothing is persisted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .cluster import as_token, bound_token_kubeconfig, kubectl, server_version

pytestmark = pytest.mark.integration

LABEL = "k7.katakate.org"
RUNTIME_CLASSES = ("kata", "kata-qemu", "k7", "k7-fc")
NODE_POLICY = "k7-node-installer-own-node"


def _json(*args: str) -> dict:
    return json.loads(kubectl(*args, "-o", "json", check=True).stdout)


def _installer_pods() -> list[dict]:
    pods = _json("get", "pods", "-A", "-l", "app=k7-node-installer")["items"]
    return [p for p in pods if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]


def test_runtime_classes_select_backend_labelled_nodes():
    """A pod that omits core's nodeSelector still lands only on a node marked for that backend."""
    rcs = {rc["metadata"]["name"]: rc for rc in _json("get", "runtimeclass")["items"]}
    present = [n for n in RUNTIME_CLASSES if n in rcs]
    assert present, f"no K7 RuntimeClass among {sorted(rcs)}"
    for name in present:
        sel = (rcs[name].get("scheduling") or {}).get("nodeSelector") or {}
        assert sel and all(k.startswith(f"{LABEL}/backend-") for k in sel), (name, sel)


def test_installed_nodes_carry_node_ready():
    pods = _installer_pods()
    if not pods:
        pytest.skip("k7-node-installer DaemonSet not deployed (K3s playbook install)")
    nodes = {n["metadata"]["name"]: n["metadata"].get("labels") or {} for n in _json("get", "nodes")["items"]}
    for pod in pods:
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
        if ready:
            node = pod["spec"]["nodeName"]
            assert nodes[node].get(f"{LABEL}/node-ready"), f"{node} runs a Ready installer but has no node-ready"


@pytest.fixture(scope="module")
def installer_kubeconfig(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str, list[str]]:
    """A kubeconfig authenticating as the installer ServiceAccount with a token bound
    to one installer pod; returns it, that pod's node, and the other node names."""
    if kubectl("get", "validatingadmissionpolicy", NODE_POLICY).returncode != 0:
        pytest.skip(f"ValidatingAdmissionPolicy {NODE_POLICY} not installed")
    if server_version() < (1, 32):
        pytest.skip(f"bound tokens carry the node name from Kubernetes 1.32 on, cluster is {server_version()}")
    pods = _installer_pods()
    if not pods:
        pytest.skip("no running k7-node-installer pod to bind a token to")
    node = pods[0]["spec"]["nodeName"]
    path = bound_token_kubeconfig(pods[0], tmp_path_factory.mktemp("installer") / "kubeconfig")
    others = [n["metadata"]["name"] for n in _json("get", "nodes")["items"] if n["metadata"]["name"] != node]
    return path, node, others


def test_installer_may_set_k7_labels_on_its_own_node(installer_kubeconfig):
    cfg, node, _ = installer_kubeconfig
    r = as_token(cfg, "label", "node", node, f"{LABEL}/policy-probe=1", "--overwrite")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["label", "node", "{node}", "probe.example.com/x=1"], "may only change k7.katakate.org/ labels"),
        (["annotate", "node", "{node}", f"{LABEL}/probe=1"], "may not change node annotations"),
        (["taint", "node", "{node}", f"{LABEL}/probe=1:NoSchedule"], "may not change the node spec"),
    ],
)
def test_installer_is_denied_outside_k7_labels(installer_kubeconfig, args: list[str], message: str):
    cfg, node, _ = installer_kubeconfig
    r = as_token(cfg, *[a.format(node=node) for a in args])
    assert r.returncode != 0 and message in r.stderr, r.stderr


def test_installer_is_denied_on_another_node(installer_kubeconfig):
    cfg, _, others = installer_kubeconfig
    if not others:
        pytest.skip("single-node cluster")
    r = as_token(cfg, "label", "node", others[0], f"{LABEL}/policy-probe=1", "--overwrite")
    assert r.returncode != 0 and "may only patch the node its pod runs on" in r.stderr, r.stderr
