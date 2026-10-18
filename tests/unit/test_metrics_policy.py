"""Port 9464 is scraper-only (k7_metrics_policy + `k7 install --metrics-scraper`)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.models import OperationResult
from k7.deploy import k7_metrics_policy as mp
from tests.unit.conftest import install_tree_text

PROM = "monitoring:app.kubernetes.io/name=prometheus"
NS = "k8s:io.kubernetes.pod.namespace"
PORT = [{"ports": [{"port": "9464", "protocol": "TCP"}]}]


@pytest.mark.parametrize(
    ("spec", "want"),
    [
        ("", None),
        (PROM, ("monitoring", {"app.kubernetes.io/name": "prometheus"})),
        ("obs:app=prom,team=sre", ("obs", {"app": "prom", "team": "sre"})),
        ("obs:empty=", ("obs", {"empty": ""})),
    ],
)
def test_parse_scraper(spec: str, want):
    assert mp.parse_scraper(spec) == want


@pytest.mark.parametrize(
    "spec",
    ["monitoring", "monitoring:", ":app=prom", "Mon:app=prom", "obs:app", "obs:app=a,app=b", "obs:-bad=x", "obs:a=b c"],
)
def test_parse_scraper_rejects(spec: str):
    with pytest.raises(ValueError, match="--metrics-scraper"):
        mp.parse_scraper(spec)


def test_cilium_without_scraper_denies_every_source():
    spec = mp.cilium_policy("kube-system", None)["spec"]
    assert spec["ingressDeny"] == [{"fromEntities": ["all"], "toPorts": PORT}]
    assert "ingress" not in spec and spec["enableDefaultDeny"] == {"ingress": False}


def test_cilium_admits_only_the_scraper():
    pol = mp.cilium_policy("kube-system", ("obs", {"team": "sre", "app": "prom"}))
    assert pol["metadata"] == {"name": "k7-metrics", "namespace": "kube-system"}
    spec = pol["spec"]
    assert spec["endpointSelector"]["matchExpressions"][0]["values"] == ["k7-api", "k7-agent"]
    assert spec["ingress"] == [
        {"fromEndpoints": [{"matchLabels": {NS: "obs", "app": "prom", "team": "sre"}}], "toPorts": PORT}
    ]
    entities, endpoints = spec["ingressDeny"]
    assert set(entities["fromEntities"]) >= {"world", "host", "remote-node", "kube-apiserver"}
    assert endpoints["fromEndpoints"] == [
        {"matchExpressions": [{"key": NS, "operator": "NotIn", "values": ["obs"]}]},
        {"matchLabels": {NS: "obs"}, "matchExpressions": [{"key": "app", "operator": "NotIn", "values": ["prom"]}]},
        {"matchLabels": {NS: "obs"}, "matchExpressions": [{"key": "team", "operator": "NotIn", "values": ["sre"]}]},
    ]
    assert all(rule["toPorts"] == PORT for rule in spec["ingressDeny"])


@pytest.mark.parametrize(("acme", "api_ports"), [(False, [8000, 8443]), (True, [8000, 8443, 80])])
def test_network_policies_keep_service_ports_open(acme: bool, api_ports: list[int]):
    api, agent = mp.network_policies("kube-system", mp.parse_scraper(PROM), acme=acme)
    assert [p["port"] for p in api["spec"]["ingress"][0]["ports"]] == api_ports
    assert [p["port"] for p in agent["spec"]["ingress"][0]["ports"]] == [8000]
    for pol in (api, agent):
        assert "from" not in pol["spec"]["ingress"][0]
        assert pol["spec"]["ingress"][1] == {
            "from": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
                }
            ],
            "ports": [{"port": 9464, "protocol": "TCP"}],
        }


def test_network_policies_without_scraper_admit_no_metrics_source():
    for pol in mp.network_policies("kube-system", None, acme=False):
        assert len(pol["spec"]["ingress"]) == 1 and pol["spec"]["policyTypes"] == ["Ingress"]


def test_write_keeps_only_this_cnis_file(tmp_path: Path):
    assert (
        mp.main(
            ["write", "--manifest-dir", str(tmp_path), "--cni", "flannel", "--scraper", PROM]
            + ["--namespace", "kube-system", "--acme", "true"]
        )
        == 0
    )
    doc = yaml.safe_load((tmp_path / mp.NETPOL_FILE).read_text())
    assert doc["kind"] == "List" and [i["metadata"]["name"] for i in doc["items"]] == [
        "k7-api-metrics",
        "k7-agent-metrics",
    ]
    assert [p["port"] for p in doc["items"][0]["spec"]["ingress"][0]["ports"]] == [8000, 8443, 80]
    mp.write(tmp_path, cilium=True, scraper=None, namespace="kube-system", acme=False)
    assert not (tmp_path / mp.NETPOL_FILE).exists()
    assert json.loads((tmp_path / mp.CILIUM_FILE).read_text()) == mp.cilium_policy("kube-system", None)
    mp.write(tmp_path, cilium=False, scraper=None, namespace="kube-system", acme=False)
    assert not (tmp_path / mp.CILIUM_FILE).exists()


def test_main_rejects_a_bad_scraper(tmp_path: Path):
    with pytest.raises(ValueError, match="--metrics-scraper"):
        mp.main(
            ["write", "--manifest-dir", str(tmp_path), "--cni", "cilium", "--scraper", "nope"]
            + ["--namespace", "kube-system", "--acme", "false"]
        )
    assert not list(tmp_path.iterdir())


def _install(args: list[str]):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = CliRunner().invoke(app, ["--core", "install", "--backend", "kql", "--api-mode", "development", *args])
    return result, install


def test_install_forwards_the_scraper():
    result, install = _install(["--metrics-scraper", f" {PROM} "])
    assert result.exit_code == 0, result.output
    assert install.call_args.kwargs["extra_vars"]["k7_metrics_scraper"] == PROM
    result, install = _install([])
    assert install.call_args.kwargs["extra_vars"]["k7_metrics_scraper"] == ""


def test_install_rejects_a_bad_scraper_before_ansible():
    result, install = _install(["--metrics-scraper", "monitoring"])
    assert result.exit_code != 0 and "--metrics-scraper" in result.output
    install.assert_not_called()


def test_playbook_renders_the_policy_before_the_apply():
    text = install_tree_text()
    render = text.index("/etc/k7/k7_metrics_policy.py\n      - write")
    assert render < text.index("apply --recursive=false -f /etc/k7/manifests/k7-api/")
    assert "{{ k7_metrics_scraper | default('') }}" in text
    assert "delete networkpolicy k7-api-metrics k7-agent-metrics --ignore-not-found" in text
