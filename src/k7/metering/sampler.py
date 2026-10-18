"""Node usage sampler, run by k7-agent on every node.

Every ``sampleInterval`` it lists the node's ready sandbox pods with crictl
and closes one window ``[t_prev, t_now)`` per pod. Seconds come from the
monotonic clock, timestamps from the wall clock. A state change between two
samples splits the window at the recorded pause/resume time (``pauses``), or
at the midpoint flagged ``approximate``. A pod that vanished is charged up
to the midpoint, also ``approximate``. Time no sampler saw (agent restart,
metering start) becomes a ``gap`` event, never usage.

Egress is the pod netns ``txBytes`` from CRI ``PodSandboxStats``: a pod whose
stats come back empty marks its backend ``egress_unavailable`` (never 0).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..core.tenancy import BACKEND_ANN, BACKENDS, KEY_ID_LABEL, SANDBOX_LABEL, TENANT_LABEL
from . import MEMORY_ANN, SANDBOX_UID_ANN, VCPU_ANN
from .events import EGRESS, GAP, GIB, SANDBOX, envelope, event_id, iso, size_class, subject
from .spool import write_atomic

log = logging.getLogger("k7.metering.sampler")
STATE_FILE = "sampler-state.json"
RUNTIME_BACKENDS = {"kata": BACKENDS[0], "kata-qemu": "kata-qemu-longhorn", "k7": "k7d", "k7-fc": "k7d-fc"}
Transitions = dict[str, list[tuple[float, bool]]]


@dataclass(frozen=True)
class Pod:
    cri_id: str
    uid: str
    sandbox: str
    namespace: str
    tenant: str
    api_key_id: str | None
    backend: str
    vcpu: str | None
    memory: str | None
    created: float

    def who(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "namespace": self.namespace,
            "sandbox": self.sandbox,
            "sandbox_uid": self.uid,
            "api_key_id": self.api_key_id,
            "backend": self.backend,
        }


@dataclass
class Tracked:
    pod: Pod
    t_wall: float
    t_mono: float
    state: str
    tx: int | None


def parse_pods(listing: dict) -> tuple[dict[str, Pod], list[str]]:
    """Sandbox pods from ``crictl pods -o json``; pods K7 cannot attribute are returned as errors."""
    pods: dict[str, Pod] = {}
    errors: list[str] = []
    for item in listing.get("items") or []:
        labels, anns, meta = item.get("labels") or {}, item.get("annotations") or {}, item["metadata"]
        if SANDBOX_LABEL not in labels:
            continue
        backend = anns.get(BACKEND_ANN) or RUNTIME_BACKENDS.get(item.get("runtimeHandler", ""))
        if backend not in BACKENDS:
            errors.append(f"{meta['namespace']}/{meta['name']}: unknown backend {backend!r} (no {BACKEND_ANN})")
            continue
        pods[item["id"]] = Pod(
            cri_id=item["id"],
            uid=anns.get(SANDBOX_UID_ANN) or meta["uid"],
            sandbox=labels[SANDBOX_LABEL],
            namespace=meta["namespace"],
            tenant=labels.get(TENANT_LABEL, ""),
            api_key_id=labels.get(KEY_ID_LABEL),
            backend=backend,
            vcpu=anns.get(VCPU_ANN),
            memory=anns.get(MEMORY_ANN),
            created=int(item["createdAt"]) / 1e9,
        )
    return pods, errors


def _uint(v: Any) -> int | None:
    if isinstance(v, dict):
        v = v.get("value")
    return None if v is None else int(v)


def parse_tx(stats: dict) -> dict[str, int | None]:
    """Pod sandbox id → tx bytes of the netns default interface (``None`` when the runtime filled none).

    Only the default interface (eth0) counts: a Kata netns also holds the VM's
    tap device, whose traffic crosses eth0 too.
    """
    out: dict[str, int | None] = {}
    for s in stats.get("stats") or []:
        default = (((s.get("linux") or {}).get("network") or {}).get("defaultInterface")) or {}
        out[s["attributes"]["id"]] = _uint(default.get("txBytes"))
    return out


def state_at(trans: list[tuple[float, bool]] | None, t: float) -> str:
    last = [p for tt, p in sorted(trans or ()) if tt <= t]
    return "paused" if last and last[-1] else "running"


def split(
    s0: str, s1: str, t0: float, t1: float, dur: float, trans: list[tuple[float, bool]] | None
) -> list[tuple[float, float, str, float, bool]]:
    """``[t0, t1)`` from state ``s0`` to ``s1`` as ``(start, end, state, seconds, approximate)``."""
    if t1 <= t0:
        return []
    cuts: list[tuple[float, str]] = []
    cur = s0
    for t, paused in sorted(trans or ()):
        st = "paused" if paused else "running"
        if t0 < t <= t1 and st != cur:
            if t < t1:
                cuts.append((t, st))
            cur = st
    approx = cur != s1
    if approx:
        cuts.append((((cuts[-1][0] if cuts else t0) + t1) / 2, s1))
    spans, a, st = [], t0, s0
    for t, nst in cuts:
        spans.append((a, t, st))
        a, st = t, nst
    spans.append((a, t1, st))
    scale = dur / (t1 - t0)
    return [(a, b, st, (b - a) * scale, approx) for a, b, st in spans if b > a]


class NodeSampler:
    def __init__(self, cluster_id: str, node: str, root: Path):
        self.cluster_id = cluster_id
        self.source = f"k7://{cluster_id}/node/{node}"
        self.state_path = root / STATE_FILE
        self.tracked: dict[str, Tracked] = {}
        self.t_prev: float | None = None
        self.egress_unavailable: dict[str, bool] = {}

    def _usage(self, pod: Pod, span: tuple[float, float, str, float, bool], approx: bool = False) -> dict:
        a, b, state, seconds, split_approx = span
        vcpu = Decimal(pod.vcpu) if pod.vcpu else None
        mem = Decimal(pod.memory) if pod.memory else None
        ws = iso(a)
        data = {
            **pod.who(),
            "state": state,
            "size_class": size_class(vcpu, mem),
            "window_start": ws,
            "window_end": iso(b),
            "seconds": round(seconds, 3),
            "vcpu": 0.0 if state == "paused" or vcpu is None else float(vcpu),
            "memory_gib": float(mem / GIB) if mem is not None else 0.0,
            "approximate": approx or split_approx,
        }
        eid = event_id(self.cluster_id, pod.uid, "sandbox", ws)
        return envelope(SANDBOX, self.source, subject(pod.tenant, pod.namespace, pod.sandbox), eid, iso(b), data)

    def _egress(self, pod: Pod, a: float, b: float, nbytes: int, reset: bool) -> dict:
        ws = iso(a)
        data = {
            **pod.who(),
            "scope": "total",
            "window_start": ws,
            "window_end": iso(b),
            "bytes": nbytes,
            "counter_reset": reset,
        }
        eid = event_id(self.cluster_id, pod.uid, "egress", ws)
        return envelope(EGRESS, self.source, subject(pod.tenant, pod.namespace, pod.sandbox), eid, iso(b), data)

    def _gap(self, pod: Pod, meter: str, a: float, b: float, reason: str) -> dict:
        gs = iso(a)
        data = {**pod.who(), "meter": meter, "gap_start": gs, "gap_end": iso(b), "reason": reason}
        eid = event_id(self.cluster_id, pod.uid, f"gap-{meter}", gs)
        return envelope(GAP, self.source, subject(pod.tenant, pod.namespace, pod.sandbox), eid, iso(b), data)

    def step(
        self, pods: dict[str, Pod], tx: dict[str, int | None], trans: Transitions, t_wall: float, t_mono: float
    ) -> list[dict]:
        """One sample. Call ``save`` once the returned events are in the spool."""
        for backend in {p.backend for p in pods.values()}:
            self.egress_unavailable[backend] = any(tx.get(i) is None for i, p in pods.items() if p.backend == backend)
        events = (
            self._advance(pods, tx, trans, t_wall, t_mono) if self.t_prev is not None else self._restore(pods, t_wall)
        )
        self.tracked = {
            i: Tracked(p, t_wall, t_mono, state_at(trans.get(i), t_wall), tx.get(i)) for i, p in pods.items()
        }
        self.t_prev = t_wall
        return events

    def _restore(self, pods: dict[str, Pod], t_wall: float) -> list[dict]:
        if not self.state_path.exists():
            return [
                self._gap(p, "sandbox", p.created, t_wall, "metering-start")
                for p in pods.values()
                if p.created < t_wall
            ]
        saved = json.loads(self.state_path.read_text())
        t_last = float(saved["t_wall"])
        events = []
        for raw in saved["pods"].values():
            pod = Pod(**raw["pod"])
            events.append(self._gap(pod, "sandbox", t_last, t_wall, "agent-restart"))
            if raw["tx"] is not None:
                events.append(self._gap(pod, "egress", t_last, t_wall, "agent-restart"))
        for i, pod in pods.items():
            start = max(pod.created, t_last)
            if i not in saved["pods"] and start < t_wall:
                events.append(self._gap(pod, "sandbox", start, t_wall, "agent-restart"))
        return events

    def _advance(
        self, pods: dict[str, Pod], tx: dict[str, int | None], trans: Transitions, t_wall: float, t_mono: float
    ) -> list[dict]:
        assert self.t_prev is not None
        events = []
        for i, pod in pods.items():
            prev, now_state = self.tracked.get(i), state_at(trans.get(i), t_wall)
            if prev:
                start, spans = (
                    prev.t_wall,
                    split(prev.state, now_state, prev.t_wall, t_wall, t_mono - prev.t_mono, trans.get(i)),
                )
            else:
                start = max(pod.created, self.t_prev)
                spans = split("running", now_state, start, t_wall, t_wall - start, trans.get(i))
            events += [self._usage(pod, s) for s in spans]
            cur = tx.get(i)
            if cur is None or (prev is not None and prev.tx is None):
                continue
            reset = prev is not None and cur < (prev.tx or 0)
            delta = cur if prev is None or reset else cur - (prev.tx or 0)
            if reset:
                log.warning("egress counter of %s/%s went backwards: counting from zero", pod.namespace, pod.sandbox)
            if delta > 0 and t_wall > start:
                events.append(self._egress(pod, start, t_wall, delta, reset))
        for i, prev in self.tracked.items():
            if i in pods:
                continue
            mid = (prev.t_wall + t_wall) / 2
            spans = split(
                prev.state, state_at(trans.get(i), mid), prev.t_wall, mid, (t_mono - prev.t_mono) / 2, trans.get(i)
            )
            events += [self._usage(prev.pod, s, approx=True) for s in spans]
        return events

    def save(self) -> None:
        assert self.t_prev is not None
        pods = {i: {"pod": asdict(t.pod), "state": t.state, "tx": t.tx} for i, t in self.tracked.items()}
        write_atomic(self.state_path, json.dumps({"t_wall": self.t_prev, "pods": pods}).encode())


def crictl(*args: str) -> dict:
    exe = shutil.which("crictl") or "/usr/local/bin/crictl"
    cmd = [exe, "--runtime-endpoint", f"unix://{os.environ['K7_CONTAINERD_SOCKET']}", *args, "-o", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"crictl {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)
