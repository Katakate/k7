"""Storage sampler, run by the k7-api replica holding Lease ``k7-metering-leader``.

Windows are aligned to ``storageInterval`` boundaries, so any leader computes
the same ids for the same window. Each tick emits the window that just
closed for every sandbox (disk) and VolumeSnapshot (snapshot) that exists,
starting at the object's creation when that falls inside the window. The
last emitted boundary is kept in ConfigMap ``k7-metering-state``; windows a
leader change skipped become ``gap`` events.

The events go to the spool of the k7-agent on this replica's node, which
delivers them with its own sinks. Gauges ``k7_usage_disk_bytes`` and
``k7_usage_snapshot_bytes`` are exported by the leader only.

Not metered yet (their sources are not on main): k7d writable disks (agent fan-out), k7d snapshots and exported bundles. k7d disk
is reported unavailable in ``/health``, never as 0.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import socket
import time
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from kubernetes_asyncio.client.exceptions import ApiException

from .. import metrics
from ..core import cluster_config, tenancy
from ..core.core import K7D_ANN_PAUSED, K7Core, aclose_k7_core
from . import SANDBOX_UID_ANN
from .config import cluster_id
from .events import DISK, GAP, GIB, SNAPSHOT, envelope, event_id, iso, subject

log = logging.getLogger("k7.metering.storage")
LEASE = "k7-metering-leader"
STATE_CM = "k7-metering-state"
SOURCE_SANDBOX_ANN = "k7.io/source-sandbox"
POST_CHUNK = 500


def _ts(dt: Any) -> float:
    return dt.timestamp() if dt is not None else 0.0


def _pvc_sizes(pvcs: Any) -> dict[tuple[str, str], Decimal]:
    out = {}
    for p in pvcs.items:
        cap = (p.status.capacity or {}).get("storage") if p.status else None
        out[(p.metadata.namespace, p.metadata.name)] = tenancy.quantity(cap or p.spec.resources.requests["storage"])
    return out


def sandbox_state(dep: Any) -> str:
    if dep.spec.replicas == 0:
        return "stopped"
    return "paused" if (dep.metadata.annotations or {}).get(K7D_ANN_PAUSED) == "true" else "running"


def disk_bytes(dep: Any, backend: str, pvc_sizes: dict[tuple[str, str], Decimal]) -> Decimal:
    """Provisioned disk: PVCs (capacity), ephemeral claims, ephemeral-storage limits, kfd root while it runs."""
    ns, pod = dep.metadata.namespace, dep.spec.template.spec
    total = Decimal(0)
    for c in pod.containers:
        lim = (c.resources.limits or {}) if c.resources else {}
        if "ephemeral-storage" in lim:
            total += tenancy.quantity(lim["ephemeral-storage"])
    for v in pod.volumes or []:
        if v.persistent_volume_claim:
            key = (ns, v.persistent_volume_claim.claim_name)
            if key not in pvc_sizes:
                raise LookupError(f"sandbox {ns}/{dep.metadata.name} references missing PVC {key[1]}")
            total += pvc_sizes[key]
        elif v.ephemeral:
            total += tenancy.quantity(v.ephemeral.volume_claim_template.spec.resources.requests["storage"])
    if backend == tenancy.KFD and dep.spec.replicas != 0:
        total += tenancy.KFD_ROOT_DISK
    return total


class StorageSampler:
    def __init__(self, cluster_id: str, interval: int, node: str):
        self.cluster_id, self.interval, self.node = cluster_id, interval, node
        self.source = f"k7://{cluster_id}/storage"
        self.holder = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.leader = False
        self.last_window_end: float | None = None
        self.error: str | None = None
        self.unavailable: list[str] = []

    def health(self) -> dict:
        return {
            "leader": self.leader,
            "last_window_end": self.last_window_end,
            "error": self.error,
            "disk_unavailable": self.unavailable,
        }

    def _event(self, etype: str, meter: str, uid: str, subj: str, ws: str, we: str, data: dict) -> dict:
        return envelope(etype, self.source, subj, event_id(self.cluster_id, uid, meter, ws), we, data)

    async def _read_state(self, core: K7Core) -> float | None:
        v1 = await core._get_core_v1_client()
        try:
            cm = await v1.read_namespaced_config_map(STATE_CM, cluster_config.system_namespace())
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        raw = (cm.data or {}).get("storageWindowEnd")
        return float(raw) if raw else None

    async def _write_state(self, core: K7Core, end: float) -> None:
        v1, ns = await core._get_core_v1_client(), cluster_config.system_namespace()
        body = {"metadata": {"name": STATE_CM, "labels": {"app": "k7-api"}}, "data": {"storageWindowEnd": str(end)}}
        try:
            await v1.patch_namespaced_config_map(STATE_CM, ns, body)
        except ApiException as e:
            if e.status != 404:
                raise
            await v1.create_namespaced_config_map(ns, body)

    async def _snapshots(self, core: K7Core) -> list[dict]:
        custom = await core._get_custom_objects_client()
        try:
            listed = await custom.list_cluster_custom_object("snapshot.storage.k8s.io", "v1", "volumesnapshots")
        except ApiException as e:
            if e.status == 404:  # no snapshot CRDs: no VolumeSnapshots to meter
                return []
            raise
        return listed["items"]

    def _disk(self, dep: Any, pvc_sizes: dict, start: float, end: float) -> tuple[tuple, Decimal, dict | None]:
        backend = K7Core._canonicalize_backend((dep.metadata.annotations or {}).get(tenancy.BACKEND_ANN)) or ""
        if backend not in tenancy.BACKENDS:
            raise ValueError(f"sandbox {dep.metadata.namespace}/{dep.metadata.name} has unknown backend {backend!r}")
        labels, anns = dep.metadata.labels or {}, dep.metadata.annotations or {}
        tenant, ns, name = labels.get(tenancy.TENANT_LABEL, ""), dep.metadata.namespace, dep.metadata.name
        size = disk_bytes(dep, backend, pvc_sizes)
        a = max(start, _ts(dep.metadata.creation_timestamp))
        if a >= end or not size:
            return (tenant, ns, backend), size, None
        ws, we, uid, seconds = iso(a), iso(end), anns.get(SANDBOX_UID_ANN) or dep.metadata.uid, round(end - a, 3)
        data = {
            "tenant": tenant,
            "namespace": ns,
            "sandbox": name,
            "sandbox_uid": uid,
            "api_key_id": labels.get(tenancy.KEY_ID_LABEL),
            "backend": backend,
            "state": sandbox_state(dep),
            "window_start": ws,
            "window_end": we,
            "seconds": seconds,
            "bytes": int(size),
            "gib_seconds": float(size / GIB) * seconds,
        }
        return (tenant, ns, backend), size, self._event(DISK, "disk", uid, subject(tenant, ns, name), ws, we, data)

    def _snapshot(self, s: dict, pvc_sizes: dict, start: float, end: float) -> tuple[tuple, Decimal, dict | None]:
        meta = s["metadata"]
        ns, name = meta["namespace"], meta["name"]
        size_raw = (s.get("status") or {}).get("restoreSize")
        if size_raw is None:
            src = ((s.get("spec") or {}).get("source") or {}).get("persistentVolumeClaimName")
            if (ns, src) not in pvc_sizes:
                raise LookupError(f"cannot size VolumeSnapshot {ns}/{name}: no restoreSize and no source PVC")
            size = pvc_sizes[(ns, src)]
        else:
            size = tenancy.quantity(size_raw)
        tenant = (meta.get("labels") or {}).get(tenancy.TENANT_LABEL, "")
        key = (tenant, ns, "volume", "local")
        a = max(start, _parse_ts(meta.get("creationTimestamp")))
        if a >= end or not size:
            return key, size, None
        ws, we, seconds = iso(a), iso(end), round(end - a, 3)
        data = {
            "tenant": tenant,
            "namespace": ns,
            "snapshot": name,
            "snapshot_uid": meta["uid"],
            "source_sandbox": (meta.get("annotations") or {}).get(SOURCE_SANDBOX_ANN),
            "kind": "volume",
            "location": "local",
            "window_start": ws,
            "window_end": we,
            "seconds": seconds,
            "bytes": int(size),
            "gib_seconds": float(size / GIB) * seconds,
        }
        return key, size, self._event(SNAPSHOT, "snapshot", meta["uid"], subject(tenant, ns, name), ws, we, data)

    def build(
        self, deps: list, snaps: list[dict], pvc_sizes: dict[tuple[str, str], Decimal], start: float, end: float
    ) -> tuple[list[dict], dict, dict, list[str]]:
        """Events for window ``[start, end)``, the disk and snapshot gauges, and per-object errors.

        One object that cannot be sized is an error (logged, in ``/health``), not a reason to drop
        every other object's window.
        """
        events: list[dict] = []
        gauges: tuple[dict, dict] = (defaultdict(Decimal), defaultdict(Decimal))
        errors: list[str] = []
        unavailable: set[str] = set()
        work: list[tuple[int, Any, Any]] = []
        for d in deps:
            backend = K7Core._canonicalize_backend((d.metadata.annotations or {}).get(tenancy.BACKEND_ANN))
            if backend in tenancy.K7D_FAMILY:
                unavailable.add(backend)
            else:
                work.append((0, self._disk, d))
        work += [(1, self._snapshot, s) for s in snaps]
        for which, fn, obj in work:
            try:
                key, size, event = fn(obj, pvc_sizes, start, end)
            except (LookupError, ValueError, KeyError) as e:
                errors.append(str(e))
                continue
            gauges[which][key] += size
            if event:
                events.append(event)
        self.unavailable = sorted(unavailable)
        return events, gauges[0], gauges[1], errors

    def gaps(self, last: float, start: float) -> list[dict]:
        out = []
        for meter in ("disk", "snapshot"):
            gs = iso(last)
            data = {
                "meter": meter,
                "tenant": None,
                "namespace": None,
                "sandbox": None,
                "sandbox_uid": None,
                "api_key_id": None,
                "backend": None,
                "gap_start": gs,
                "gap_end": iso(start),
                "reason": "leader-change",
            }
            out.append(self._event(GAP, f"gap-{meter}", "cluster", "cluster/storage", gs, iso(start), data))
        return out

    async def tick(self, core: K7Core, now: float) -> None:
        self.leader = await tenancy.try_lead(core, LEASE, self.holder, self.interval * 2)
        if not self.leader:
            metrics.USAGE_DISK_BYTES.clear()
            metrics.USAGE_SNAPSHOT_BYTES.clear()
            return
        end = math.floor(now / self.interval) * self.interval
        start = end - self.interval
        last = await self._read_state(core)
        if last is not None and last >= end:
            self.last_window_end = last
            return
        v1 = await core._get_core_v1_client()
        deps, pvcs, snaps = await asyncio.gather(
            core._get_kata_sandboxes(None), v1.list_persistent_volume_claim_for_all_namespaces(), self._snapshots(core)
        )
        events, disk, snap_g, errors = self.build(deps, snaps, _pvc_sizes(pvcs), start, end)
        if last is not None and last < start:
            events = self.gaps(last, start) + events
        for i in range(0, len(events), POST_CHUNK):
            await core._agent_request(
                self.node, "POST", "/agent/v1/usage/events", {"events": events[i : i + POST_CHUNK]}
            )
        await self._write_state(core, end)
        self.last_window_end = end
        metrics.USAGE_DISK_BYTES.clear()
        for labels, value in disk.items():
            metrics.USAGE_DISK_BYTES.labels(*labels).set(float(value))
        metrics.USAGE_SNAPSHOT_BYTES.clear()
        for labels, value in snap_g.items():
            metrics.USAGE_SNAPSHOT_BYTES.labels(*labels).set(float(value))
        if errors:
            raise RuntimeError("storage usage: " + "; ".join(errors))

    async def run(self) -> None:
        while True:
            now = time.time()
            await asyncio.sleep(self.interval - now % self.interval + 2)
            core = K7Core()
            try:
                await self.tick(core, time.time())
                self.error = None
            except Exception as e:
                self.error = str(e)
                log.exception("storage usage sample failed")
            finally:
                await aclose_k7_core(core)


def _parse_ts(raw: str | None) -> float:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() if raw else 0.0


def from_env(interval: int) -> StorageSampler:
    node = os.environ.get("K7_NODE_NAME")
    if not node:
        raise RuntimeError("K7_NODE_NAME is not set: k7-api hands storage usage events to the k7-agent on its node")
    return StorageSampler(cluster_id(), interval, node)
