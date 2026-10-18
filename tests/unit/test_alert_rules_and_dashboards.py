"""The alert rules pass `promtool check/test rules`, and the
dashboards are valid Grafana JSON whose queries parse and name real metrics."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from k7 import metrics

REPO = Path(__file__).resolve().parents[2]
OBS = REPO / "src/k7/deploy/observability"
RULES = OBS / "rules/k7.rules.yaml"
RULE_TESTS = OBS / "tests/k7.rules.test.yaml"
DASHBOARDS = sorted((OBS / "dashboards").glob("*.json"))
PROMTOOL_VERSION = "3.15.0"  # .github/workflows/ci.yml PROMTOOL_VERSION
# Emitted by the k7-agent NodeCollector (tests/unit/test_metrics.py) rather than declared.
NODE_METRICS = {"k7_storage_pool_bytes", "k7_storage_report_age_seconds", "k7d_daemon_up", "k7_vm_count"}
# The scrape and kube-state-metrics series.
EXTERNAL = {
    "up",
    "kube_job_status_failed",
}
_METRIC = re.compile(r"\b(k7d?_[a-z0-9_]+|up|kube_[a-z0-9_]+)\b(?=\s*[{\[)]|\s*$|\s+[-/<>=!*+]|\s+(?:or|and|unless)\b)")


def _promtool_or_skip() -> str:
    tool = shutil.which("promtool")
    found = subprocess.run([tool, "--version"], capture_output=True, text=True).stdout if tool else ""
    if tool and f"version {PROMTOOL_VERSION} " in found:
        return tool
    reason = f"promtool {PROMTOOL_VERSION} not installed (found {found.splitlines()[0] if found else 'none'})"
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


def _promtool(*args: str, cwd: Path = OBS) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_promtool_or_skip(), *args], capture_output=True, text=True, cwd=cwd)


def _alerts() -> list[dict]:
    groups = yaml.safe_load(RULES.read_text())["groups"]
    return [r for g in groups for r in g["rules"] if "alert" in r]


def _names(expr: str) -> set[str]:
    return set(_METRIC.findall(re.sub(r"k7:[a-z0-9_:]+", "", expr)))


def _known_metrics() -> set[str]:
    out = set(NODE_METRICS) | EXTERNAL
    for registry in (metrics.API, metrics.AGENT):
        for fam in registry.collect():
            out.add(fam.name + ("_total" if fam.type == "counter" else ""))
            if fam.type == "histogram":
                out |= {fam.name + s for s in ("_bucket", "_count", "_sum")}
    return out


def test_promtool_check_rules():
    r = _promtool("check", "rules", str(RULES))
    assert r.returncode == 0, r.stdout + r.stderr


def test_promtool_test_rules():
    r = _promtool("test", "rules", RULE_TESTS.name, cwd=RULE_TESTS.parent)
    assert r.returncode == 0, r.stdout + r.stderr


def test_every_alert_has_a_firing_and_a_quiet_case():
    cases: dict[str, set[bool]] = {}
    for t in yaml.safe_load(RULE_TESTS.read_text())["tests"]:
        for a in t.get("alert_rule_test") or []:
            cases.setdefault(a["alertname"], set()).add(bool(a["exp_alerts"]))
    assert {a["alert"]: {True, False} for a in _alerts()} == cases


def test_spec_alert_set():
    assert {a["alert"] for a in _alerts()} == {
        "K7APIDown",
        "K7APIHighErrorRate",
        "K7OperationSlow",
        "K7KeyStoreStale",
        "K7AgentDown",
        "K7dDaemonDown",
        "K7StoragePoolFilling",
        "K7StorageReportStale",
        "K7SnapshotGCFailing",
        "K7UsageSpoolOverflow",
        "K7UsageSinkStopped",
        "K7TLSCertExpiringSoon",
    }


def test_every_alert_links_its_runbook_with_three_commands():
    base = "https://github.com/Katakate/k7/blob/main/"
    runbooks = REPO / "docs/runbooks"
    for a in _alerts():
        url = a["annotations"]["runbook_url"]
        assert url == f"{base}docs/runbooks/{a['alert']}.md", url
        text = (REPO / url.removeprefix(base)).read_text()
        first = text.split("## First three commands", 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]
        assert len(first.strip().splitlines()) == 3, (a["alert"], first)
    assert {p.stem for p in runbooks.glob("*.md")} == {a["alert"] for a in _alerts()}


def test_alerts_name_real_metrics():
    known = _known_metrics()
    for a in _alerts():
        names = _names(a["expr"])
        assert names and names <= known, (a["alert"], names - known)


def test_dashboards_are_valid_grafana_json():
    assert [d.name for d in DASHBOARDS] == [
        "k7-api-overview.json",
        "k7-nodes-storage.json",
        "k7-sandbox-lifecycle.json",
        "k7-usage-by-tenant.json",
    ]
    uids = set()
    for path in DASHBOARDS:
        d = json.loads(path.read_text())
        assert d["uid"] == path.stem and d["uid"] not in uids and d["title"].startswith("K7 / ")
        uids.add(d["uid"])
        variables = {v["name"]: v for v in d["templating"]["list"]}
        assert variables["datasource"]["type"] == "datasource" and variables["datasource"]["query"] == "prometheus"
        assert isinstance(d["schemaVersion"], int) and d["panels"]
        assert len({p["id"] for p in d["panels"]}) == len(d["panels"])
        for p in d["panels"]:
            assert p["datasource"] == {"type": "prometheus", "uid": "${datasource}"}, (path.name, p["title"])
            assert set(p["gridPos"]) == {"x", "y", "w", "h"} and p["targets"]
            assert len({t["refId"] for t in p["targets"]}) == len(p["targets"])
            for t in p["targets"]:
                used = set(re.findall(r"\$(\w+)", t["expr"]))
                assert used <= set(variables), (path.name, p["title"], used)


def _exprs() -> list[tuple[str, str]]:
    return [
        (f"{path.stem}: {p['title']}", t["expr"])
        for path in DASHBOARDS
        for p in json.loads(path.read_text())["panels"]
        for t in p["targets"]
    ]


def test_dashboard_queries_parse(tmp_path: Path):
    rules = {
        "groups": [
            {
                "name": "dashboards",
                "rules": [
                    {"record": f"k7:dashboard:{i}", "expr": re.sub(r"\$\w+", ".*", expr)}
                    for i, (_, expr) in enumerate(_exprs())
                ],
            }
        ]
    }
    (tmp_path / "dashboards.rules.yaml").write_text(yaml.safe_dump(rules))
    r = _promtool("check", "rules", "dashboards.rules.yaml", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_dashboard_queries_name_real_metrics():
    known = _known_metrics()
    for where, expr in _exprs():
        names = _names(expr)
        assert names and names <= known, (where, names - known)


def test_dashboards_cover_the_spec_panels():
    titles = {path.stem: {p["title"] for p in json.loads(path.read_text())["panels"]} for path in DASHBOARDS}
    blob = {
        stem: " ".join(
            t["expr"] for p in json.loads((OBS / f"dashboards/{stem}.json").read_text())["panels"] for t in p["targets"]
        )
        for stem in titles
    }
    for stem, needles in {
        "k7-api-overview": [
            "k7_http_requests_total",
            "k7_http_request_duration_seconds_bucket",
            "k7_auth_failures_total",
            "k7_key_store_last_sync_timestamp_seconds",
        ],
        "k7-sandbox-lifecycle": [
            "k7_operation_duration_seconds_bucket",
            "k7_operation_errors_total",
            "k7_sandboxes",
            "k7_quota_rejections_total",
            "k7_rate_limited_total",
        ],
        "k7-nodes-storage": [
            "thinpool_data",
            "thinpool_metadata",
            "k7d_disks",
            "k7_storage_report_age_seconds",
            "k7d_daemon_up",
            "k7d_rpc_duration_seconds_bucket",
            "k7_vm_count",
        ],
        "k7-usage-by-tenant": [
            "k7_usage_vcpu_seconds_total",
            "k7_usage_memory_gib_seconds_total",
            "k7_usage_egress_bytes_total",
            "k7_usage_snapshot_bytes",
        ],
    }.items():
        for needle in needles:
            assert needle in blob[stem], (stem, needle)
    for stem in ("k7-sandbox-lifecycle", "k7-usage-by-tenant"):
        assert "topk(" in blob[stem], f"{stem}: tenant panels use topk (cardinality)"
