"""CloudEvents 1.0 usage events: types, idempotent ids, validation.

The JSON schemas in ``schemas/`` are the contract. ``validate`` checks an
event against its type's schema with the small subset of JSON Schema those
files use, so the API image needs no ``jsonschema`` dependency.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMAS = Path(__file__).parent / "schemas"
SANDBOX = "io.katakate.k7.sandbox.usage.v1"
GAP = "io.katakate.k7.sandbox.usage.gap.v1"
EGRESS = "io.katakate.k7.egress.usage.v1"
DISK = "io.katakate.k7.disk.usage.v1"
SNAPSHOT = "io.katakate.k7.snapshot.usage.v1"
TYPES = (SANDBOX, GAP, EGRESS, DISK, SNAPSHOT)
GIB = 2**30


def iso(t: float) -> str:
    """UTC RFC 3339 with millisecond precision: the form ids are computed from."""
    return datetime.fromtimestamp(round(t, 3), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def event_id(cluster_id: str, uid: str, meter: str, window_start: str) -> str:
    """``sha256(cluster-id, sandbox_uid, meter, window_start)``: the downstream dedup key."""
    return hashlib.sha256("\x1f".join((cluster_id, uid, meter, window_start)).encode()).hexdigest()


def _num(v: Decimal) -> str:
    return format(v.normalize(), "f")


def size_class(vcpu: Decimal | None, memory: Decimal | None) -> str:
    """``<cpu>c-<memGi>g`` from the sandbox limits (cores, bytes); ``unsized`` without both."""
    if vcpu is None or memory is None:
        return "unsized"
    return f"{_num(vcpu)}c-{_num(memory / GIB)}g"


def subject(*parts: str | None) -> str:
    """``<tenant>/<namespace>/<name>``; a sandbox outside every tenant is ``-``."""
    return "/".join(p or "-" for p in parts)


def envelope(etype: str, source: str, subj: str, eid: str, time: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "specversion": "1.0",
        "type": etype,
        "source": source,
        "subject": subj,
        "id": eid,
        "time": time,
        "datacontenttype": "application/json",
        "data": data,
    }


@functools.cache
def schema(etype: str) -> dict:
    if etype not in TYPES:
        raise ValueError(f"unknown usage event type {etype!r}; known: {', '.join(TYPES)}")
    return json.loads((SCHEMAS / f"{etype}.json").read_text())


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "null": (type(None),),
    "integer": (int,),
    "number": (int, float),
}


def _is(value: Any, t: str) -> bool:
    if t in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _JSON_TYPES[t])


def _check(value: Any, s: dict, path: str) -> None:
    if "const" in s and value != s["const"]:
        raise ValueError(f"{path}: expected {s['const']!r}, got {value!r}")
    if "enum" in s and value not in s["enum"]:
        raise ValueError(f"{path}: {value!r} is not one of {s['enum']}")
    types = s.get("type")
    if types is not None and not any(_is(value, t) for t in ([types] if isinstance(types, str) else types)):
        raise ValueError(f"{path}: expected {types}, got {type(value).__name__}")
    if isinstance(value, str):
        if len(value) < s.get("minLength", 0):
            raise ValueError(f"{path}: empty")
        if "pattern" in s and not re.search(s["pattern"], value):
            raise ValueError(f"{path}: {value!r} does not match {s['pattern']}")
        if s.get("format") == "date-time" and not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", value):
            raise ValueError(f"{path}: {value!r} is not an RFC 3339 UTC timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < s.get("minimum", value):
        raise ValueError(f"{path}: {value} is below {s['minimum']}")
    if isinstance(value, dict):
        missing = set(s.get("required", ())) - value.keys()
        if missing:
            raise ValueError(f"{path}: missing {sorted(missing)}")
        props = s.get("properties", {})
        extra = value.keys() - props.keys()
        if s.get("additionalProperties") is False and extra:
            raise ValueError(f"{path}: unexpected {sorted(extra)}")
        for key, sub in props.items():
            if key in value:
                _check(value[key], sub, f"{path}.{key}")


def validate(event: Any) -> None:
    """Raise ``ValueError`` naming the first field that breaks the event's schema."""
    if not isinstance(event, dict):
        raise ValueError(f"event must be an object, got {type(event).__name__}")
    _check(event, schema(str(event.get("type"))), "event")
