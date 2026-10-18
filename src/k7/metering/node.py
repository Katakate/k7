"""k7-agent side of metering: sampler loop, spool, sinks and health."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from .. import metrics
from . import pauses
from .config import Config
from .events import DISK, GAP, SNAPSHOT, validate
from .sampler import NodeSampler, crictl, parse_pods, parse_tx
from .sinks import SinkRunner, observe_prometheus
from .spool import Spool

log = logging.getLogger("k7.metering")
DROP_DEGRADED_S = 600
# What k7-api's storage sampler may post into a node spool.
STORAGE_TYPES = frozenset({DISK, SNAPSHOT, GAP})


class NodeMetering:
    def __init__(self, cfg: Config, cluster_id: str, node: str, root: Path):
        self.cfg, self.root, self.cluster_id = cfg, root, cluster_id
        self.spool = Spool(
            root,
            cfg.spool_max_bytes,
            cfg.spool_high_water_percent,
            [s.name for s in cfg.sinks],
            on_drop=metrics.USAGE_EVENTS_DROPPED.inc,
        )
        self.sampler = NodeSampler(cluster_id, node, root)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        self.runners = [SinkRunner(s, self.spool, self.client) for s in cfg.sinks]
        self.last_sample: float | None = None
        self.sample_error: str | None = None
        self.unmetered: list[str] = []

    def ingest(self, events: list[dict], *, prometheus: bool = True) -> None:
        for e in events:
            validate(e)
        self.spool.append(events)
        metrics.USAGE_SPOOL_BYTES.set(self.spool.size())
        for e in events:
            if e["type"] == GAP:
                metrics.USAGE_GAPS.labels(e["data"]["meter"], e["data"]["reason"]).inc()
        if prometheus:
            observe_prometheus(events)

    def ingest_storage(self, events: list[dict]) -> None:
        """Events k7-api's storage sampler hands to this node's spool (and sinks)."""
        source = f"k7://{self.cluster_id}/storage"
        for e in events:
            if not isinstance(e, dict) or e.get("type") not in STORAGE_TYPES or e.get("source") != source:
                raise ValueError(f"only {sorted(STORAGE_TYPES)} events from {source} are accepted here")
        self.ingest(events, prometheus=False)

    def _commit(self, events: list[dict]) -> None:
        # One thread call: a shutdown cancelling sample_once cannot land between the spool
        # append and the state save, which would make the restart gap overlap spooled windows.
        self.ingest(events)
        self.sampler.save()

    async def sample_once(self) -> None:
        pods, self.unmetered = parse_pods(await asyncio.to_thread(crictl, "pods", "--state", "ready"))
        try:
            tx = parse_tx(await asyncio.to_thread(crictl, "statsp"))
        except RuntimeError as e:
            log.error("CRI PodSandboxStats unavailable, egress is unavailable for every backend: %s", e)
            tx = {}
        t_wall, t_mono = time.time(), time.monotonic()
        events = self.sampler.step(pods, tx, pauses.load(self.root), t_wall, t_mono)
        await asyncio.to_thread(self._commit, events)
        for backend, unavailable in self.sampler.egress_unavailable.items():
            metrics.USAGE_EGRESS_UNAVAILABLE.labels(backend).set(int(unavailable))
        for line in self.unmetered:
            log.error("sandbox pod not metered: %s", line)
        self.last_sample = t_wall

    async def run(self) -> None:
        tasks = [asyncio.create_task(r.run()) for r in self.runners]
        try:
            while True:
                start = time.monotonic()
                try:
                    await self.sample_once()
                    self.sample_error = None
                except Exception as e:
                    self.sample_error = str(e)
                    metrics.USAGE_SAMPLE_FAILURES.inc()
                    log.exception("usage sample failed")
                await asyncio.sleep(max(self.cfg.sample_interval - (time.monotonic() - start), 0))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.client.aclose()

    def health(self) -> dict:
        spool = self.spool.status()
        recent_drop = spool["last_drop"] is not None and time.time() - spool["last_drop"] < DROP_DEGRADED_S
        sinks = {r.sink.name: {"stopped": r.stopped, "last_error": r.last_error} for r in self.runners}
        reasons = [
            *(["spool overflow dropped events in the last 10 minutes"] if recent_drop else []),
            *(f"sink {n} stopped: {s['stopped']}" for n, s in sinks.items() if s["stopped"]),
            *([f"last sample failed: {self.sample_error}"] if self.sample_error else []),
            *(f"not metered: {u}" for u in self.unmetered),
        ]
        return {
            "status": "degraded" if reasons else "healthy",
            "degraded": reasons,
            "last_sample": self.last_sample,
            "sample_interval": self.cfg.sample_interval,
            "spool": spool,
            "sinks": sinks,
            "egress_unavailable": sorted(b for b, v in self.sampler.egress_unavailable.items() if v),
        }
