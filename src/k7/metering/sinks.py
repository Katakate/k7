"""Usage sinks.

- Prometheus: always on. Counters at tenant × namespace × backend × state
  (egress: × scope), never per sandbox; per-sandbox detail lives in events.
- ``stdout``: one CloudEvent JSON per line on the ``k7.usage`` logger.
- ``http``: CloudEvents batched content mode. 429/5xx/transport errors back
  off exponentially while the spool keeps the events; any other 4xx stops
  that sink loudly (error log with status and body, ``k7_usage_sink_stopped``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable

import httpx

from .. import metrics
from .config import Sink, sink_headers
from .events import EGRESS, SANDBOX
from .spool import Spool

log = logging.getLogger("k7.metering.sinks")
BATCH_CONTENT_TYPE = "application/cloudevents-batch+json"
STDOUT_BATCH = 500
POLL_S = 1.0
MAX_BACKOFF_S = 300.0

usage_log = logging.getLogger("k7.usage")
if not usage_log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    usage_log.addHandler(_handler)
    usage_log.setLevel(logging.INFO)
    usage_log.propagate = False


def observe_prometheus(events: list[dict]) -> None:
    for e in events:
        d = e["data"]
        if e["type"] == SANDBOX:
            labels = (d["tenant"], d["namespace"], d["backend"], d["state"])
            metrics.USAGE_SANDBOX_SECONDS.labels(*labels).inc(d["seconds"])
            metrics.USAGE_VCPU_SECONDS.labels(*labels).inc(d["vcpu"] * d["seconds"])
            metrics.USAGE_MEMORY_GIB_SECONDS.labels(*labels).inc(d["memory_gib"] * d["seconds"])
        elif e["type"] == EGRESS:
            metrics.USAGE_EGRESS_BYTES.labels(d["tenant"], d["namespace"], d["backend"], d["scope"]).inc(d["bytes"])


class Retry(Exception):
    pass


class Stop(Exception):
    pass


def openmeter_event(e: dict) -> dict:
    """OpenMeter's ingest schema refuses null ``data`` values (400); an absent key carries the same meaning."""
    return {**e, "data": {k: v for k, v in e["data"].items() if v is not None}}


async def post_batch(client: httpx.AsyncClient, sink: Sink, events: list[dict]) -> None:
    try:
        headers = {**sink_headers(sink), "Content-Type": BATCH_CONTENT_TYPE}
    except RuntimeError as e:
        raise Stop(str(e))
    if sink.preset == "openmeter":
        events = [openmeter_event(e) for e in events]
    try:
        r = await client.post(sink.url, content=json.dumps(events, separators=(",", ":")), headers=headers)
    except httpx.TransportError as e:
        raise Retry(f"{type(e).__name__}: {e}")
    if r.status_code == 429 or r.status_code >= 500:
        raise Retry(f"HTTP {r.status_code}: {r.text[:500]}")
    if r.status_code >= 400:
        raise Stop(f"HTTP {r.status_code} from {sink.url}: {r.text[:2000]}")


class SinkRunner:
    """Drains one sink's spool offset. ``stopped`` is set (and stays set) on a non-retryable refusal."""

    def __init__(
        self,
        sink: Sink,
        spool: Spool,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.sink, self.spool, self.client, self.sleep = sink, spool, client, sleep
        self.stopped: str | None = None
        self.last_error: str | None = None
        self.delivered = 0
        metrics.USAGE_SINK_STOPPED.labels(sink.name).set(0)

    async def send(self, events: list[dict]) -> None:
        if self.sink.type == "stdout":
            for e in events:
                usage_log.info(json.dumps(e, separators=(",", ":"), sort_keys=True))
            return
        await post_batch(self.client, self.sink, events)

    async def drain_once(self) -> int:
        """Deliver one batch; returns how many events were acknowledged (0 when idle)."""
        size = self.sink.batch_size or STDOUT_BATCH
        events, cursor = await asyncio.to_thread(self.spool.read, self.sink.name, size)
        if not events:
            return 0
        await self.send(events)
        await asyncio.to_thread(self.spool.ack, self.sink.name, cursor)
        self.delivered += len(events)
        self.last_error = None
        return len(events)

    async def run(self) -> None:
        delay = 1.0
        while True:
            metrics.USAGE_SINK_PENDING_BYTES.labels(self.sink.name).set(self.spool.pending_bytes(self.sink.name))
            try:
                sent = await self.drain_once()
            except Stop as e:
                self.stopped = str(e)
                metrics.USAGE_SINK_STOPPED.labels(self.sink.name).set(1)
                log.error(
                    "usage sink %s STOPPED: %s. Its events accumulate in the spool until it is fixed and "
                    "k7-agent restarts; the spool cap then drops the oldest (k7_usage_events_dropped_total).",
                    self.sink.name,
                    e,
                )
                return
            except Exception as e:
                self.last_error = str(e)
                log.warning("usage sink %s: %s; retrying in %.0fs (events stay in the spool)", self.sink.name, e, delay)
                await self.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_S)
                continue
            delay = 1.0
            if not sent:
                await self.sleep(POLL_S)
