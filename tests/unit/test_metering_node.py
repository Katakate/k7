"""k7-agent metering: spool ingest, storage hand-off, health and the usage endpoints."""

import asyncio
import json
import threading
import time
from datetime import datetime
from unittest.mock import patch

import httpx
import pytest

from k7 import metrics
from k7.metering import config
from k7.metering.events import DISK, GAP, SANDBOX, envelope, event_id
from k7.metering.node import NodeMetering
from k7.metering.sampler import parse_pods
from tests.unit.conftest import CLUSTER_ID
from tests.unit.test_agent import TOKEN, _client
from tests.unit.test_metering_events import sandbox_event
from tests.unit.test_metering_sampler import _item

H = {"X-K7-Agent-Token": TOKEN}
STORAGE = f"k7://{CLUSTER_ID}/storage"


def disk_event(source: str = STORAGE) -> dict:
    data = {
        "tenant": "acme",
        "namespace": "acme-dev",
        "sandbox": "sb",
        "sandbox_uid": "u-1",
        "api_key_id": None,
        "backend": "kata-qemu-longhorn",
        "state": "stopped",
        "window_start": "2026-01-01T00:00:00.000Z",
        "window_end": "2026-01-01T00:01:00.000Z",
        "seconds": 60.0,
        "bytes": 2**30,
        "gib_seconds": 60.0,
    }
    eid = event_id(CLUSTER_ID, "u-1", "disk", data["window_start"])
    return envelope(DISK, source, "acme/acme-dev/sb", eid, data["window_end"], data)


def node(tmp_path) -> NodeMetering:
    return NodeMetering(config.load(), CLUSTER_ID, "node-a", tmp_path)


def _all(m: NodeMetering) -> list[dict]:
    m.spool.cursors["probe"] = (m.spool._seqs()[0], 0)
    return m.spool.read("probe", 1000)[0]


def test_ingest_validates_before_spooling(tmp_path):
    m = node(tmp_path)
    with pytest.raises(ValueError, match="state"):
        m.ingest([sandbox_event(), sandbox_event(state="stopped")])
    assert m.spool.size() == 0


def test_storage_hand_off_accepts_only_storage_events(tmp_path):
    m = node(tmp_path)
    m.ingest_storage([disk_event()])
    for wrong in (sandbox_event(), disk_event(source=f"k7://{CLUSTER_ID}/node/node-a")):
        with pytest.raises(ValueError, match="only"):
            m.ingest_storage([wrong])
    assert [e["type"] for e in _all(m)] == [DISK]


async def test_sample_once_spools_gap_then_windows(tmp_path):
    m = node(tmp_path)
    crictl = {"pods": {"items": [_item()]}, "statsp": {"stats": []}}
    with patch("k7.metering.node.crictl", side_effect=lambda *a: crictl[a[0]]):
        await m.sample_once()
        await m.sample_once()
    assert [e["type"] for e in _all(m)] == [GAP, SANDBOX]
    assert m.last_sample is not None and m.sampler.egress_unavailable == {"k7d": True}
    assert metrics.AGENT.get_sample_value("k7_usage_egress_unavailable", {"backend": "k7d"}) == 1
    assert (tmp_path / "sampler-state.json").exists()


async def test_cancel_during_spool_append_still_saves_the_sampler_state(tmp_path):
    """A shutdown mid-sample must not leave spooled windows behind a stale state file."""
    m = node(tmp_path)
    crictl = {"pods": {"items": [_item()]}, "statsp": {"stats": []}}
    entered, release = threading.Event(), threading.Event()
    ingest = m.ingest

    def slow_ingest(events):
        entered.set()
        release.wait(5)
        ingest(events)

    with patch("k7.metering.node.crictl", side_effect=lambda *a: crictl[a[0]]):
        await m.sample_once()
        m.ingest = slow_ingest
        task = asyncio.create_task(m.sample_once())
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    await asyncio.sleep(0.2)
    saved = json.loads((tmp_path / "sampler-state.json").read_text())["t_wall"]
    windows = [e for e in _all(m) if e["type"] == SANDBOX]
    assert len(windows) == 1
    assert abs(saved - datetime.fromisoformat(windows[-1]["data"]["window_end"][:-1] + "+00:00").timestamp()) < 0.002


async def test_statsp_failure_is_egress_unavailable_not_a_failed_sample(tmp_path):
    m = node(tmp_path)

    def crictl(*args):
        if args[0] == "statsp":
            raise RuntimeError("statsp: not implemented")
        return {"items": [_item()]}

    with patch("k7.metering.node.crictl", side_effect=crictl):
        await m.sample_once()
    assert m.sampler.egress_unavailable == {"k7d": True}


def test_gap_events_are_counted(tmp_path):
    m = node(tmp_path)
    labels = {"meter": "sandbox", "reason": "metering-start"}
    before = metrics.AGENT.get_sample_value("k7_usage_gaps_total", labels) or 0
    pods = parse_pods({"items": [_item()]})[0]
    m.ingest(m.sampler.step(pods, {"cri-1": None}, {}, 1_767_225_600.0, 0.0))
    assert metrics.AGENT.get_sample_value("k7_usage_gaps_total", labels) == before + 1


def test_health_turns_degraded_with_reasons(tmp_path):
    m = node(tmp_path)
    assert m.health()["status"] == "healthy"
    m.sample_error = "crictl failed"
    m.unmetered = ["ns/pod: unknown backend"]
    m.spool.last_drop = time.time()
    m.sampler.egress_unavailable = {"k7d": True, "kata-qemu-longhorn": False}
    h = m.health()
    assert h["status"] == "degraded" and h["egress_unavailable"] == ["k7d"]
    assert len(h["degraded"]) == 3 and any("spool overflow" in r for r in h["degraded"])


async def test_usage_endpoints(tmp_path, monkeypatch):
    transport = _client(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as c:
        assert (await c.post("/agent/v1/usage/events", json={"events": [disk_event()]})).status_code == 401
        r = await c.post("/agent/v1/usage/events", json={"events": [disk_event()]}, headers=H)
        assert r.status_code == 200 and r.json() == {"accepted": 1}
        r = await c.post("/agent/v1/usage/events", json={"events": [sandbox_event()]}, headers=H)
        assert r.status_code == 400 and "only" in r.json()["detail"]
        assert (await c.post("/agent/v1/usage/events", json={"events": []}, headers=H)).status_code == 400
        status = (await c.get("/agent/v1/usage/status", headers=H)).json()
        assert status["spool"]["bytes"] > 0 and status["spool"]["above_high_water"] is False
        health = (await c.get("/health")).json()
        assert health["status"] == "healthy" and health["usage"]["sample_interval"] == 15


async def test_pause_and_resume_record_transitions(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from k7.metering import pauses

    transport = _client(tmp_path, monkeypatch)
    vm = {"vm_id": "vm-1", "status": "sandbox_found", "sandbox_id": "cri-9"}
    body = {"name": "sb", "namespace": "ns", "pod_name": "sb-pod"}
    with (
        patch("k7.api.agent._lookup_local_vm", new=AsyncMock(return_value=vm)),
        patch("k7.api.agent.K7Core") as core_cls,
    ):
        core_cls.return_value._k7d_request = AsyncMock(return_value={"status": "ok"})
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as c:
            assert (await c.post("/agent/v1/vm/pause", json=body, headers=H)).status_code == 200
            assert (await c.post("/agent/v1/vm/resume", json=body, headers=H)).status_code == 200
    assert [p for _, p in pauses.load()["cri-9"]] == [True, False]
