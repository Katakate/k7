"""CLI and playbook: `k7 usage`, `install --metering-*`, the spool column, the k7-metering tasks."""

import json
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from k7.cli.k7 import MONITORING_PROMETHEUS_URL, _print_nodes_storage_table, _usage_time, app
from k7.core.models import OperationResult
from k7_sdk.client import Client
from tests.unit.conftest import DEPLOY_DIR, install_tree_text

runner = CliRunner()
NOW = 1_767_312_000.0
ROWS = {
    "from": NOW - 86400,
    "to": NOW,
    "tenant": None,
    "group_by": ["tenant", "state"],
    "rows": [
        {
            "tenant": "acme",
            "state": "paused",
            "sandbox_seconds": 300.0,
            "vcpu_seconds": 0.0,
            "memory_gib_seconds": 1200.0,
            "egress_bytes": 0.0,
            "disk_gib_seconds": 0.0,
            "snapshot_gib_seconds": 0.0,
        }
    ],
}


@pytest.mark.parametrize(
    ("raw", "want"),
    [("now", NOW), ("24h", NOW - 86400), ("7d", NOW - 7 * 86400), ("90m", NOW - 5400), ("1700000000", 1.7e9)],
)
def test_usage_time(raw, want):
    assert _usage_time(raw, NOW) == want
    assert _usage_time("2026-01-02T00:00:00Z", NOW) == NOW


@pytest.mark.parametrize("raw", ["yesterday", "2026-01-02T00:00:00", "-1h"])
def test_usage_time_rejects(raw):
    with pytest.raises(Exception, match="duration like 24h"):
        _usage_time(raw, NOW)


def _usage(args):
    client = MagicMock()
    client.usage.return_value = ROWS
    with patch("k7.cli._client.resolve_client", return_value=client), patch("k7.cli.k7.time.time", return_value=NOW):
        result = runner.invoke(app, ["--api-url", "http://api", "--api-key", "k", "usage", *args])
    return result, client


def test_k7_usage_table_and_json():
    with patch("k7.cli.k7.Console") as console:
        result, client = _usage(["--from", "1d", "--group-by", "tenant,state"])
    assert result.exit_code == 0, result.output
    client.usage.assert_called_once_with(NOW - 86400, NOW, tenant=None, group_by="tenant,state")
    table = console.return_value.print.call_args.args[0]
    cols = {c.header: list(c.cells) for c in table.columns}
    assert cols["STATE"] == ["paused"] and cols["MEMORY_GIB_SECONDS"] == ["1,200"] and cols["VCPU_SECONDS"] == ["0"]
    result, client = _usage(["--tenant", "acme", "--json"])
    assert json.loads(result.output) == ROWS
    assert client.usage.call_args.kwargs["tenant"] == "acme"


def test_k7_usage_refuses_core():
    result = runner.invoke(app, ["--core", "usage"])
    assert result.exit_code == 1 and "drop --core" in result.output


def test_sdk_usage_drops_unset_params():
    c = Client(endpoint="http://api.test", api_key="k")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": ROWS}
    with patch.object(c.session, "request", return_value=resp) as req:
        assert c.usage(1.0, 2.0, group_by="state") == ROWS
    assert req.call_args.args == ("GET", "http://api.test/api/v1/usage")
    assert req.call_args.kwargs["params"] == {"from": 1.0, "to": 2.0, "group_by": "state"}


def test_nodes_storage_shows_the_spool():
    spool = {"bytes": 900 * 2**20, "max_bytes": 2**30, "above_high_water": True, "dropped_events": 7}
    entry = {"kata_thinpool": {}, "k7d_disks": {}}
    with patch("k7.cli.k7.Console") as console:
        _print_nodes_storage_table({"n1": {**entry, "usage_spool": spool}, "n2": entry})
    table = console.return_value.print.call_args.args[0]
    spool_col = next(c for c in table.columns if c.header == "USAGE SPOOL")
    err_col = next(c for c in table.columns if c.header == "ERROR")
    assert "(above high-water)" in list(spool_col.cells)[0] and "k7-agent predates metering" in list(spool_col.cells)[1]
    assert list(err_col.cells)[0] == "7 usage events dropped"


# --- install flags -----------------------------------------------------------


def _install(args):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = runner.invoke(app, ["--core", "install", "--backend", "kfd", *args])
    return result, install


def test_install_renders_the_metering_document(tmp_path):
    sinks = tmp_path / "sinks.yaml"
    sinks.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "om",
                    "type": "http",
                    "url": "http://om:8888",
                    "mode": "batch",
                    "batchSize": 100,
                    "preset": "openmeter",
                },
                {"name": "log", "type": "stdout"},
            ]
        )
    )
    args = ["--api-mode", "production", "--metering-sample-interval", "5", "--metering-spool-size", "256Mi"]
    args += ["--metering-require-delivery", "--metering-sinks-file", str(sinks), "--monitoring"]
    result, install = _install(args)
    assert result.exit_code == 0, result.output
    m = install.call_args.kwargs["extra_vars"]["k7_metering"]
    assert m == {
        "sampleInterval": 5,
        "storageInterval": 60,
        "spool": {"maxBytes": 256 * 2**20, "highWaterPercent": 80},
        "requireDelivery": True,
        "prometheusUrl": MONITORING_PROMETHEUS_URL,
        "sinks": yaml.safe_load(sinks.read_text()),
    }


def test_install_development_defaults_to_15s_without_prometheus():
    result, install = _install(["--api-mode", "development"])
    assert result.exit_code == 0, result.output
    m = install.call_args.kwargs["extra_vars"]["k7_metering"]
    assert m["sampleInterval"] == 15 and m["prometheusUrl"] is None and m["sinks"] == []


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--api-mode", "production"], "explicit --metering-sample-interval"),
        (["--api-mode", "development", "--metering-sample-interval", "2"], "sampleInterval must be 5-60"),
        (["--api-mode", "development", "--metering-spool-size", "lots"], "--metering-spool-size"),
        (
            ["--api-mode", "development", "--monitoring", "--metering-prometheus-url", "http://p:9090"],
            "--monitoring sets metering.prometheusUrl",
        ),
    ],
)
def test_install_refuses_bad_metering(args, needle):
    result, install = _install(args)
    assert result.exit_code != 0 and needle in " ".join(result.output.replace("│", " ").split()), result.output
    install.assert_not_called()


def test_install_refuses_a_bad_sinks_file(tmp_path):
    sinks = tmp_path / "s.yaml"
    sinks.write_text("- {name: om, type: kafka}\n")
    result, install = _install(["--api-mode", "development", "--metering-sinks-file", str(sinks)])
    assert result.exit_code != 0 and "type must be one of http, stdout" in result.output
    install.assert_not_called()


# --- playbook ----------------------------------------------------------------


def _api_tasks() -> dict:
    tasks = yaml.safe_load((DEPLOY_DIR / "roles/k7_cluster/tasks/api.yml").read_text())
    return {t["name"]: t for t in tasks}


def test_playbook_creates_the_cluster_id_once_and_publishes_the_document():
    tasks = _api_tasks()
    cid = next(t for n, t in tasks.items() if "k7-cluster-id" in n)
    script = cid["ansible.builtin.shell"]
    assert script.index("get configmap k7-cluster-id") < script.index("create configmap k7-cluster-id")
    assert "/proc/sys/kernel/random/uuid" in script and "delete" not in script
    assert "k7-metering" in next(n for n in tasks if "metering settings" in n)
    text = install_tree_text()
    assert "k7_metering | to_json(sort_keys=True) | hash('sha256')" in text
    assert "__K7_METERING_CONFIG_HASH__" in text


def test_playbook_validates_the_metering_keys():
    text = (DEPLOY_DIR / "roles/k7_cluster/tasks/validate.yml").read_text()
    for key in ("prometheusUrl", "requireDelivery", "sampleInterval", "sinks", "spool", "storageInterval"):
        assert key in text
