"""Metering settings: every key explicit, every bad value a startup error."""

import copy
import json

import pytest

from k7.metering import config
from tests.unit.conftest import CLUSTER_ID, METERING


def _raw(**over):
    raw = copy.deepcopy(METERING)
    raw.update(over)
    return raw


def test_defaults_document_parses():
    cfg = config.parse(_raw())
    assert (cfg.sample_interval, cfg.storage_interval, cfg.spool_max_bytes) == (15, 60, 2**30)
    assert cfg.prometheus_url is None and cfg.sinks == () and cfg.require_delivery is False
    assert config.parse(_raw(sampleInterval="5s", prometheusUrl="")).sample_interval == 5


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"sampleInterval": 4}, "sampleInterval must be 5-60"),
        ({"sampleInterval": 61}, "sampleInterval must be 5-60"),
        ({"sampleInterval": None}, "sampleInterval"),
        ({"sampleInterval": True}, "sampleInterval"),
        ({"storageInterval": 10}, "storageInterval must be 30-600"),
        ({"spool": {"maxBytes": 1}}, "exactly maxBytes and highWaterPercent"),
        ({"spool": {"maxBytes": 1024, "highWaterPercent": 80}}, "spool.maxBytes"),
        ({"spool": {"maxBytes": 2**30, "highWaterPercent": 100}}, "highWaterPercent"),
        ({"requireDelivery": "yes"}, "requireDelivery must be a boolean"),
        ({"prometheusUrl": "prometheus:9090"}, "prometheusUrl must be an http"),
        ({"sinks": {}}, "sinks must be a list"),
        ({"sinks": [{"name": "a", "type": "kafka"}]}, "type must be one of http, stdout"),
        ({"sinks": [{"name": "A_b", "type": "stdout"}]}, "DNS label"),
        ({"sinks": [{"name": "a", "type": "stdout", "url": "http://x"}]}, "unknown key"),
        ({"sinks": [{"name": "a", "type": "stdout"}, {"name": "a", "type": "stdout"}]}, "unique"),
        ({"sinks": [{"name": "a", "type": "http", "url": "x", "mode": "batch", "batchSize": 1}]}, "url must be"),
        ({"sinks": [{"name": "a", "type": "http", "url": "http://x", "mode": "binary", "batchSize": 1}]}, "batch"),
        ({"sinks": [{"name": "a", "type": "http", "url": "http://x", "mode": "batch"}]}, "batchSize"),
        (
            {
                "sinks": [
                    {
                        "name": "a",
                        "type": "http",
                        "url": "http://x/e",
                        "mode": "batch",
                        "batchSize": 1,
                        "preset": "openmeter",
                    }
                ]
            },
            "give the base URL",
        ),
        (
            {
                "sinks": [
                    {
                        "name": "a",
                        "type": "http",
                        "url": "http://x",
                        "mode": "batch",
                        "batchSize": 1,
                        "headersSecretRef": {"name": "other", "key": "k"},
                    }
                ]
            },
            "k7-agent mounts only that Secret",
        ),
    ],
)
def test_rejects(over, error):
    with pytest.raises(ValueError, match=error):
        config.parse(_raw(**over))


def test_missing_or_unknown_top_level_keys():
    raw = _raw()
    del raw["sinks"]
    with pytest.raises(ValueError, match=r"missing keys \['sinks'\]"):
        config.parse(raw)
    with pytest.raises(ValueError, match=r"unknown \['extra'\]"):
        config.parse(_raw(extra=1))


def test_openmeter_preset_targets_the_events_api():
    sink = {"name": "om", "type": "http", "url": "http://om:8888/", "mode": "batch", "batchSize": 100}
    (s,) = config.parse(_raw(sinks=[{**sink, "preset": "openmeter"}])).sinks
    assert s.url == "http://om:8888/api/v1/events" and s.batch_size == 100 and s.headers_key is None


def test_load_and_cluster_id_fail_loudly(monkeypatch, tmp_path):
    monkeypatch.delenv(config.CONFIG_ENV)
    with pytest.raises(RuntimeError, match="K7_METERING_CONFIG is not set"):
        config.load()
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps(_raw(sampleInterval=1)))
    monkeypatch.setenv(config.CONFIG_ENV, str(bad))
    with pytest.raises(RuntimeError, match=f"metering config {bad}"):
        config.load()
    assert config.cluster_id() == CLUSTER_ID
    monkeypatch.setenv(config.CLUSTER_ID_ENV, "cluster-1")
    with pytest.raises(RuntimeError, match="not a UUID"):
        config.cluster_id()


def test_sink_headers_are_reread_and_validated(monkeypatch, tmp_path):
    monkeypatch.setenv(config.HEADERS_DIR_ENV, str(tmp_path))
    sink = config.Sink(name="om", type="http", url="http://x", batch_size=1, headers_key="om")
    assert config.sink_headers(config.Sink(name="s", type="stdout")) == {}
    with pytest.raises(RuntimeError, match="unreadable"):
        config.sink_headers(sink)
    (tmp_path / "om").write_text('{"Authorization": "Bearer a"}')
    assert config.sink_headers(sink) == {"Authorization": "Bearer a"}
    (tmp_path / "om").write_text('{"Authorization": "Bearer b"}')
    assert config.sink_headers(sink) == {"Authorization": "Bearer b"}
    (tmp_path / "om").write_text('{"X-N": 1}')
    with pytest.raises(RuntimeError, match="JSON object of header name to string"):
        config.sink_headers(sink)
