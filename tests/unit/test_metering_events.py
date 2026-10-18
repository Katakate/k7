"""Usage event ids, schemas and the frozen v1 contract."""

import hashlib
import json
from decimal import Decimal

import pytest

from k7.metering import events
from k7.metering.events import GIB, envelope, event_id, iso, size_class, subject, validate
from tests.unit.conftest import CLUSTER_ID

# v1 is never mutated: a changed field is a new vN type and a new file.
FROZEN = {
    events.DISK: "45b8b749599ba1f8f77b9bfad9d9a379aafc9843812fb6a28ef332ad0aa31b3a",
    events.EGRESS: "540a4c8f12c303c9579a750d08876d6c4fb8a1ddb8568323ce294a5f605ab945",
    events.GAP: "9f72e83fc7188e65f151de47039b7cbe5f8da60240eda3f8ffbb9db82ba69ca2",
    events.SANDBOX: "5031e05c78d084499fc3690e7faa30b4b29c54dfd78c5dffe795f48f4d91e892",
    events.SNAPSHOT: "70354d3df32bbd387e16c531a293774d9e52e0306163c053c9492c9019c4a4ca",
}
SOURCE = f"k7://{CLUSTER_ID}/node/node-a"


def sandbox_event(**data) -> dict:
    d = {
        "tenant": "acme",
        "namespace": "acme-dev",
        "sandbox": "sb",
        "sandbox_uid": "u-1",
        "api_key_id": None,
        "backend": "k7d",
        "state": "paused",
        "size_class": "2c-4g",
        "window_start": "2026-01-01T00:00:00.000Z",
        "window_end": "2026-01-01T00:00:15.000Z",
        "seconds": 15.0,
        "vcpu": 0.0,
        "memory_gib": 4.0,
        "approximate": False,
        **data,
    }
    eid = event_id(CLUSTER_ID, d["sandbox_uid"], "sandbox", d["window_start"])
    return envelope(events.SANDBOX, SOURCE, "acme/acme-dev/sb", eid, d["window_end"], d)


def test_v1_schemas_are_frozen():
    assert {p.name for p in events.SCHEMAS.glob("*.json")} == {f"{t}.json" for t in events.TYPES}
    for etype, digest in FROZEN.items():
        assert hashlib.sha256((events.SCHEMAS / f"{etype}.json").read_bytes()).hexdigest() == digest, etype
        assert events.schema(etype)["properties"]["type"] == {"const": etype}


def test_event_id_is_deterministic_and_separates_every_part():
    a = event_id(CLUSTER_ID, "u-1", "sandbox", "2026-01-01T00:00:00.000Z")
    assert a == event_id(CLUSTER_ID, "u-1", "sandbox", "2026-01-01T00:00:00.000Z")
    assert len(a) == 64 and int(a, 16) >= 0
    others = {
        event_id("other", "u-1", "sandbox", "2026-01-01T00:00:00.000Z"),
        event_id(CLUSTER_ID, "u-2", "sandbox", "2026-01-01T00:00:00.000Z"),
        event_id(CLUSTER_ID, "u-1", "egress", "2026-01-01T00:00:00.000Z"),
        event_id(CLUSTER_ID, "u-1", "sandbox", "2026-01-01T00:00:15.000Z"),
        event_id(CLUSTER_ID + "u", "-1", "sandbox", "2026-01-01T00:00:00.000Z"),
    }
    assert a not in others and len(others) == 5


def test_iso_is_utc_millisecond_rfc3339():
    assert iso(0) == "1970-01-01T00:00:00.000Z"
    assert iso(1767225600.1234) == "2026-01-01T00:00:00.123Z"


@pytest.mark.parametrize(
    ("vcpu", "mem", "want"),
    [
        (Decimal(2), Decimal(4 * GIB), "2c-4g"),
        (Decimal("0.5"), Decimal(GIB // 2), "0.5c-0.5g"),
        (None, None, "unsized"),
    ],
)
def test_size_class(vcpu, mem, want):
    assert size_class(vcpu, mem) == want


def test_subject_marks_untenanted_sandboxes():
    assert subject("", "default", "sb") == "-/default/sb"


def test_a_valid_event_passes_and_round_trips_json():
    e = sandbox_event()
    validate(e)
    validate(json.loads(json.dumps(e)))


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda e: e.update(specversion="0.3"), "specversion"),
        (lambda e: e.update(source="k7://not-a-cluster/node/a"), "source"),
        (lambda e: e.update(id="abc"), "event.id"),
        (lambda e: e.update(time="2026-01-01 00:00:00"), "RFC 3339"),
        (lambda e: e.update(extra=1), "unexpected"),
        (lambda e: e["data"].pop("approximate"), "missing"),
        (lambda e: e["data"].update(state="stopped"), "state"),
        (lambda e: e["data"].update(seconds=-1), "below"),
        (lambda e: e["data"].update(approximate=1), "approximate"),
        (lambda e: e["data"].update(vcpu=True), "vcpu"),
        (lambda e: e["data"].update(backend="docker"), "backend"),
        (lambda e: e["data"].update(size_class="big"), "size_class"),
        (lambda e: e["data"].update(namespace=""), "empty"),
    ],
)
def test_schema_rejects(mutate, error):
    e = sandbox_event()
    mutate(e)
    with pytest.raises(ValueError, match=error):
        validate(e)


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError, match="unknown usage event type"):
        validate({**sandbox_event(), "type": "io.katakate.k7.sandbox.usage.v2"})
    with pytest.raises(ValueError, match="must be an object"):
        validate([])
