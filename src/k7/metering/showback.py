"""``k7 usage`` / ``GET /api/v1/usage``: showback from Prometheus.

K7 keeps no usage database. Counters are read as raw samples over
``[from - lookback, to]`` and their increase after ``from`` is summed
exactly, counter resets (agent restarts) included, so the result matches
the sum of the events up to one scrape interval. Gauges (disk, snapshot
bytes) are integrated over time into GiB·s. A meter without one of the
``group_by`` labels (egress and disk have no ``state``, snapshots no
``backend``) reports ``*`` for it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import httpx

GROUPS = ("tenant", "namespace", "backend", "state")
COUNTERS = {
    "sandbox_seconds": "k7_usage_sandbox_seconds_total",
    "vcpu_seconds": "k7_usage_vcpu_seconds_total",
    "memory_gib_seconds": "k7_usage_memory_gib_seconds_total",
    "egress_bytes": "k7_usage_egress_bytes_total",
}
GAUGES = {"disk_gib_seconds": "k7_usage_disk_bytes", "snapshot_gib_seconds": "k7_usage_snapshot_bytes"}
LOOKBACK_S = 300
_TENANT = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def parse_group_by(raw: str | None) -> list[str]:
    groups = [g.strip() for g in ("tenant" if raw is None else raw).split(",") if g.strip()]
    bad = [g for g in groups if g not in GROUPS]
    if not groups or bad or len(set(groups)) != len(groups):
        raise ValueError(f"group_by must be a comma-separated subset of {', '.join(GROUPS)}, got {raw!r}")
    return groups


def increase(values: list[tuple[float, float]], start: float) -> float:
    """Counter increase after ``start``; a series first seen after ``start`` counts from 0."""
    total, prev = 0.0, None
    for ts, v in sorted(values):
        if ts <= start:
            prev = v
            continue
        total += v if prev is None or v < prev else v - prev
        prev = v
    return total


def integrate(values: list[tuple[float, float]], start: float, end: float) -> float:
    """∫ gauge dt over ``[start, end]``, each sample held until the next.

    The last sample is held for one scrape step (the smallest gap between
    samples), not until ``end``: a series that ended (deleted sandbox) stops
    accruing there.
    """
    pts = sorted(values)
    step = min((b[0] - a[0] for a, b in zip(pts, pts[1:], strict=False) if b[0] > a[0]), default=0.0)
    total = 0.0
    for i, (ts, v) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else min(ts + step, end)
        a, b = max(ts, start), min(nxt, end)
        if b > a:
            total += v * (b - a)
    return total


async def _matrix(http: httpx.AsyncClient, url: str, selector: str, start: float, end: float) -> list[dict]:
    rng = int(end - start) + LOOKBACK_S
    r = await http.get(f"{url.rstrip('/')}/api/v1/query", params={"query": f"{selector}[{rng}s]", "time": end})
    if r.status_code != 200:
        raise RuntimeError(f"Prometheus {url} answered HTTP {r.status_code}: {r.text[:500]}")
    body = r.json()
    if body.get("status") != "success" or body["data"]["resultType"] != "matrix":
        raise RuntimeError(f"Prometheus {url} returned {body}")
    return body["data"]["result"]


async def usage(
    url: str, tenant: str | None, start: float, end: float, group_by: list[str], http: httpx.AsyncClient
) -> list[dict[str, Any]]:
    if end <= start:
        raise ValueError("'to' must be after 'from'")
    if tenant is not None and not _TENANT.match(tenant):
        raise ValueError(f"tenant {tenant!r} is not a valid tenant name")
    match = f'{{tenant="{tenant}"}}' if tenant is not None else ""
    rows: dict[tuple[str, ...], dict[str, Any]] = defaultdict(dict)
    meters = [(k, m, False) for k, m in COUNTERS.items()] + [(k, m, True) for k, m in GAUGES.items()]
    for key, metric, gauge in meters:
        for series in await _matrix(http, url, metric + match, start, end):
            labels = series["metric"]
            values = [(float(t), float(v)) for t, v in series["values"]]
            value = integrate(values, start, end) / 2**30 if gauge else increase(values, start)
            row = rows[tuple(labels.get(g, "*") for g in group_by)]
            row[key] = row.get(key, 0.0) + value
    return [
        {**dict(zip(group_by, k, strict=True)), **{m: round(row.get(m, 0.0), 3) for m in (*COUNTERS, *GAUGES)}}
        for k, row in sorted(rows.items())
    ]
