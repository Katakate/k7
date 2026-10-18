"""Showback from Prometheus: exact counter increase and gauge integration."""

import httpx
import pytest

from k7.metering import showback
from k7.metering.showback import increase, integrate, parse_group_by

T0 = 1_767_225_600.0


def test_increase_counts_only_after_start_and_survives_resets():
    assert increase([(T0 - 30, 100), (T0 + 15, 130), (T0 + 30, 160)], T0) == 60
    assert increase([(T0 + 15, 20), (T0 + 30, 50)], T0) == 50  # series born after start
    assert increase([(T0 - 15, 100), (T0 + 15, 130), (T0 + 30, 10), (T0 + 45, 40)], T0) == 30 + 10 + 30


def test_integrate_holds_each_sample_until_the_next_and_the_last_one_step():
    gib = 2**30
    assert integrate([(T0 - 30, gib), (T0 + 30, gib), (T0 + 60, gib)], T0, T0 + 600) == gib * 90
    assert integrate([(T0, 2.0), (T0 + 30, 4.0)], T0 + 15, T0 + 60) == 2.0 * 15 + 4.0 * 30
    assert integrate([(T0, 1.0)], T0, T0 + 60) == 0.0


@pytest.mark.parametrize("raw", ["", "pod", "tenant,tenant", "tenant,,sandbox"])
def test_group_by_rejects(raw):
    with pytest.raises(ValueError, match="group_by"):
        parse_group_by(raw)


def test_group_by_default_is_tenant():
    assert parse_group_by(None) == ["tenant"] and parse_group_by("tenant, state") == ["tenant", "state"]


def _prometheus(series: dict[str, list[dict]], seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params["query"]
        seen.append((q, req.url.params["time"]))
        metric = q.split("{")[0].split("[")[0]
        result = [{"metric": m, "values": [[t, str(v)] for t, v in vals]} for m, vals in series.get(metric, [])]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": result}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_usage_rows_group_and_star_missing_labels():
    acme = {"tenant": "acme", "namespace": "a", "backend": "k7d", "state": "running"}
    series = {
        "k7_usage_sandbox_seconds_total": [
            (acme, [(T0 - 15, 0), (T0 + 15, 15), (T0 + 30, 30)]),
            ({**acme, "state": "paused"}, [(T0 + 15, 15)]),
        ],
        "k7_usage_egress_bytes_total": [
            ({**{k: acme[k] for k in ("tenant", "namespace", "backend")}, "scope": "total"}, [(T0 + 1, 500)])
        ],
        "k7_usage_disk_bytes": [
            ({"tenant": "acme", "namespace": "a", "backend": "k7d"}, [(T0, 2**30), (T0 + 30, 2**30)])
        ],
    }
    seen: list = []
    async with _prometheus(series, seen) as http:
        rows = await showback.usage("http://prom:9090", "acme", T0, T0 + 60, ["tenant", "state"], http)
    assert all('{tenant="acme"}' in q and float(t) == T0 + 60 for q, t in seen) and len(seen) == 6
    assert all(q.endswith(f"[{60 + showback.LOOKBACK_S}s]") for q, _ in seen)
    by = {(r["tenant"], r["state"]): r for r in rows}
    assert by[("acme", "running")]["sandbox_seconds"] == 30 and by[("acme", "paused")]["sandbox_seconds"] == 15
    assert by[("acme", "*")]["egress_bytes"] == 500 and by[("acme", "*")]["disk_gib_seconds"] == 60
    assert set(by[("acme", "running")]) == {"tenant", "state", *showback.COUNTERS, *showback.GAUGES}


async def test_usage_validates_and_surfaces_prometheus_errors():
    async with _prometheus({}, []) as http:
        with pytest.raises(ValueError, match="after"):
            await showback.usage("http://p", None, T0, T0, ["tenant"], http)
        with pytest.raises(ValueError, match="not a valid tenant"):
            await showback.usage("http://p", 'a"}or{x="', T0, T0 + 1, ["tenant"], http)
    bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")))
    async with bad:
        with pytest.raises(RuntimeError, match="HTTP 503: down"):
            await showback.usage("http://p", None, T0, T0 + 1, ["tenant"], bad)
