"""Usage sinks: Prometheus aggregation, stdout, CloudEvents batch HTTP."""

import io
import json
import sys

import httpx
import pytest
from prometheus_client import generate_latest

from k7 import metrics
from k7.metering import config
from k7.metering.sinks import BATCH_CONTENT_TYPE, SinkRunner, observe_prometheus, usage_log
from k7.metering.spool import Spool
from tests.unit.test_metering_events import sandbox_event

OM = config.parse(
    {
        "sampleInterval": 15,
        "storageInterval": 60,
        "spool": {"maxBytes": 2**30, "highWaterPercent": 80},
        "requireDelivery": False,
        "prometheusUrl": None,
        "sinks": [
            {
                "name": "om",
                "type": "http",
                "url": "http://om:8888",
                "mode": "batch",
                "batchSize": 2,
                "preset": "openmeter",
            },
            {"name": "log", "type": "stdout"},
        ],
    }
).sinks


def _spool(tmp_path, n: int) -> Spool:
    s = Spool(tmp_path, 2**30, 80, [x.name for x in OM], on_drop=lambda _: None)
    s.append([sandbox_event(sandbox_uid=f"u-{i}") for i in range(n)])
    return s


def _runner(tmp_path, handler, n=3, sink=OM[0]):
    sleeps: list[float] = []

    async def sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) > 20:
            raise RuntimeError("runner did not stop")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SinkRunner(sink, _spool(tmp_path, n), client, sleep=sleep), sleeps


def _sample(name: str, labels: dict) -> float | None:
    return metrics.AGENT.get_sample_value(name, labels)


def test_prometheus_counters_aggregate_without_a_sandbox_label():
    labels = {"tenant": "t-prom", "namespace": "acme-dev", "backend": "k7d", "state": "paused"}
    before = _sample("k7_usage_sandbox_seconds_total", labels) or 0.0
    gib_before = _sample("k7_usage_memory_gib_seconds_total", labels) or 0.0
    observe_prometheus([sandbox_event(tenant="t-prom", sandbox="a"), sandbox_event(tenant="t-prom", sandbox="b")])
    assert _sample("k7_usage_sandbox_seconds_total", labels) == before + 30
    assert _sample("k7_usage_memory_gib_seconds_total", labels) == gib_before + 2 * 4 * 15
    exposed = generate_latest(metrics.AGENT).decode()
    assert all("sandbox=" not in line for line in exposed.splitlines() if line.startswith("k7_usage_"))


async def test_openmeter_preset_posts_batches_and_acks(tmp_path):
    posts: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        posts.append(req)
        return httpx.Response(204)

    runner, _ = _runner(tmp_path, handler)
    assert await runner.drain_once() == 2 and await runner.drain_once() == 1 and await runner.drain_once() == 0
    assert [str(p.url) for p in posts] == ["http://om:8888/api/v1/events"] * 2
    assert posts[0].headers["content-type"] == BATCH_CONTENT_TYPE
    batch = json.loads(posts[0].content)
    assert [e["data"]["sandbox_uid"] for e in batch] == ["u-0", "u-1"] and batch[0]["specversion"] == "1.0"
    assert runner.delivered == 3 and runner.spool.pending_bytes("om") == 0
    # OpenMeter answers 400 "Value is not nullable" to a null data value: the preset leaves the key out.
    assert "api_key_id" not in batch[0]["data"]
    assert runner.spool.read("log", 1)[0][0]["data"]["api_key_id"] is None


async def test_a_plain_http_sink_keeps_null_data_values(tmp_path):
    posts: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        posts.append(req)
        return httpx.Response(200)

    plain = config.Sink(name="om", type="http", url="http://c:1/e", batch_size=5)
    runner, _ = _runner(tmp_path, handler, n=1, sink=plain)
    assert await runner.drain_once() == 1
    assert json.loads(posts[0].content)[0]["data"]["api_key_id"] is None


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_failures_back_off_and_keep_events(tmp_path, status):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(status if len(calls) <= 4 else 200)

    runner, sleeps = _runner(tmp_path, handler, n=1)
    with pytest.raises(RuntimeError, match="did not stop"):
        await runner.run()
    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
    assert runner.delivered == 1 and runner.stopped is None


async def test_transport_errors_are_retried(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    runner, sleeps = _runner(tmp_path, handler, n=1)
    with pytest.raises(RuntimeError, match="did not stop"):
        await runner.run()
    assert sleeps[:3] == [1.0, 2.0, 4.0] and "ConnectError" in (runner.last_error or "")
    assert runner.spool.pending_bytes("om") > 0


async def test_a_4xx_stops_the_sink_loudly(tmp_path, caplog):
    runner, sleeps = _runner(tmp_path, lambda req: httpx.Response(400, text="bad event: id"))
    await runner.run()
    assert sleeps == [] and runner.stopped and "HTTP 400" in runner.stopped and "bad event: id" in runner.stopped
    assert metrics.AGENT.get_sample_value("k7_usage_sink_stopped", {"sink": "om"}) == 1
    assert "usage sink om STOPPED" in caplog.text and "bad event: id" in caplog.text
    assert runner.spool.pending_bytes("om") > 0


async def test_unreadable_headers_secret_stops_the_sink(tmp_path, monkeypatch):
    monkeypatch.setenv(config.HEADERS_DIR_ENV, str(tmp_path / "none"))
    sink = config.Sink(name="om", type="http", url="http://om/api/v1/events", batch_size=1, headers_key="om")
    runner, _ = _runner(tmp_path, lambda req: httpx.Response(200), sink=sink)
    await runner.run()
    assert "k7-metering-headers" in (runner.stopped or "")


async def test_headers_come_from_the_secret(tmp_path, monkeypatch):
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "om").write_text('{"Authorization": "Bearer t"}')
    monkeypatch.setenv(config.HEADERS_DIR_ENV, str(tmp_path / "h"))
    seen = []
    sink = config.Sink(name="om", type="http", url="http://om/api/v1/events", batch_size=5, headers_key="om")
    runner, _ = _runner(tmp_path / "s", lambda req: seen.append(req) or httpx.Response(200), sink=sink)
    await runner.drain_once()
    assert seen[0].headers["authorization"] == "Bearer t"


async def test_stdout_sink_writes_one_json_event_per_line(tmp_path, monkeypatch):
    out = io.StringIO()
    (handler,) = usage_log.handlers
    assert handler.stream is sys.stdout and not usage_log.propagate
    monkeypatch.setattr(handler, "stream", out)
    runner, _ = _runner(tmp_path, lambda req: httpx.Response(500), sink=OM[1])
    assert await runner.drain_once() == 3
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [e["data"]["sandbox_uid"] for e in lines] == ["u-0", "u-1", "u-2"]
