"""`k7 install --monitoring` (pinned kube-prometheus-stack + K7 rules, monitors, dashboards)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from typer.testing import CliRunner

from k7.cli import k7 as cli
from k7.core.models import OperationResult

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "src/k7/deploy"
OBS = DEPLOY / "observability"
TASKS = DEPLOY / "roles/k7_cluster/tasks"
PLAY_VARS = yaml.safe_load((DEPLOY / "k7-install-node.yaml").read_text())[0]["vars"]
MONITORING = yaml.safe_load((TASKS / "monitoring.yml").read_text())


def _install(args: list[str]):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = CliRunner().invoke(
            cli.app, ["--core", "install", "--backend", "kql", "--api-mode", "development", *args]
        )
    return result, install


def test_monitoring_sets_its_scraper():
    result, install = _install(["--monitoring"])
    assert result.exit_code == 0, result.output
    ev = install.call_args.kwargs["extra_vars"]
    assert (ev["k7_monitoring"], ev["k7_metrics_scraper"]) == ("true", cli.MONITORING_SCRAPER)
    assert PLAY_VARS["k7_monitoring_scraper"] == cli.MONITORING_SCRAPER
    _, install = _install([])
    assert install.call_args.kwargs["extra_vars"]["k7_monitoring"] == "false"


def test_monitoring_refuses_conflicting_flags():
    for extra, error in ((["--metrics-scraper", "obs:app=x"], "drop --metrics-scraper"), (["--no-api"], "--no-api")):
        result, install = _install(["--monitoring", *extra])
        assert result.exit_code != 0 and error in re.sub(r"[\s│]+", " ", result.output), result.output
        install.assert_not_called()


def test_playbook_pins_the_stack():
    assert PLAY_VARS["k7_monitoring"] is False and PLAY_VARS["k7_metrics_scraper"] == ""
    assert PLAY_VARS["kube_prometheus_stack_version"] == "92.3.0"
    assert re.fullmatch(r"[0-9a-f]{64}", PLAY_VARS["kube_prometheus_stack_sha256"])
    (stage,) = [t for t in MONITORING if "ansible.builtin.get_url" in t]
    assert stage["ansible.builtin.get_url"]["checksum"] == "sha256:{{ kube_prometheus_stack_sha256 }}"
    (helm,) = [t for t in MONITORING if "helm" in str(t.get("ansible.builtin.command", ""))]
    argv = helm["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["helm", "upgrade", "--install", "kube-prometheus-stack"] and "--wait" in argv
    assert argv[argv.index("--namespace") + 1] == "monitoring"


def test_playbook_runs_monitoring_only_on_request():
    resources = yaml.safe_load((TASKS / "resources.yml").read_text())
    (imp,) = [t for t in resources if t.get("ansible.builtin.import_tasks") == "monitoring.yml"]
    assert imp["when"] == "k7_monitoring | bool"
    api = yaml.safe_load((TASKS / "api.yml").read_text())
    (helm,) = [t for t in api if t["name"].startswith("Install helm (pinned)")]
    assert "k7_monitoring | bool" in helm["when"]
    validate = yaml.safe_load((TASKS / "validate.yml").read_text())
    (guard,) = [t for t in validate if t["name"] == "Fail if --monitoring cannot scrape K7"]
    assert "k7_monitoring_scraper" in guard["when"][1]


def test_stack_values_select_k7_objects():
    values = yaml.safe_load((OBS / "kube-prometheus-stack-values.yaml").read_text())
    spec = values["prometheus"]["prometheusSpec"]
    for key in ("serviceMonitor", "podMonitor", "rule"):
        assert spec[f"{key}SelectorNilUsesHelmValues"] is False
    assert all(
        values[c]["enabled"] is False for c in ("kubeControllerManager", "kubeScheduler", "kubeProxy", "kubeEtcd")
    )


def test_prometheus_rule_wraps_the_rules_file_verbatim():
    (apply,) = [t for t in MONITORING if "PrometheusRule" in t["name"]]
    script = apply["ansible.builtin.shell"]
    wrap = re.search(r"(\{ printf .*?; \})", script, re.S)
    assert wrap
    out = subprocess.run(["bash", "-c", wrap.group(1)], cwd=OBS, capture_output=True, text=True, check=True).stdout
    doc = yaml.safe_load(out)
    assert (doc["kind"], doc["metadata"]) == ("PrometheusRule", {"name": "k7", "namespace": "kube-system"})
    assert doc["spec"] == yaml.safe_load((OBS / "rules/k7.rules.yaml").read_text())
    assert "grafana_dashboard=1" in script and "-n monitoring create configmap" in script


def test_monitoring_files_are_copied():
    (copy,) = [t for t in MONITORING if "ansible.builtin.copy" in t]
    for item in copy["loop"]:
        assert (OBS / item).exists(), item
