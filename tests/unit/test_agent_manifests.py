"""Agent must not inherit k7-api ClusterRole or accept remote-node ingress."""

from pathlib import Path

MANIFESTS = Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api"


def test_agent_daemonset_has_no_api_rbac():
    ds = (MANIFESTS / "agent-daemonset.yaml").read_text()
    assert "serviceAccountName: k7-agent" in ds
    assert "automountServiceAccountToken: false" in ds
    assert "serviceAccountName: k7-api" not in ds


def test_agent_sa_has_no_clusterrolebinding():
    binding = (MANIFESTS / "clusterrolebinding.yaml").read_text()
    assert "name: k7-api" in binding
    assert "k7-agent" not in binding
    sa = (MANIFESTS / "serviceaccount.yaml").read_text()
    assert "name: k7-agent" in sa
    assert "automountServiceAccountToken: false" in sa


def test_agent_cnp_denies_remote_node():
    cnp = (MANIFESTS / "cilium/agent-networkpolicy.yaml").read_text()
    spec = cnp.split("spec:", 1)[1]
    assert "app: k7-api" in spec
    assert "- host" in spec
    assert "remote-node" not in spec
