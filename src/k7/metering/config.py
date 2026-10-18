"""Metering settings, read from the ``k7-metering`` ConfigMap.

The playbook and the Helm chart render the same JSON document and mount it
at ``K7_METERING_CONFIG`` in k7-api and k7-agent. Every key is explicit:
an unknown or missing key is a startup error, never a default.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_ENV = "K7_METERING_CONFIG"
CLUSTER_ID_ENV = "K7_CLUSTER_ID"
SPOOL_DIR_ENV = "K7_USAGE_SPOOL_DIR"
DEFAULT_SPOOL_DIR = "/var/lib/k7/usage-spool"
# Only this Secret is mounted into k7-agent (it has no API token to read others).
HEADERS_SECRET = "k7-metering-headers"
HEADERS_DIR_ENV = "K7_METERING_HEADERS_DIR"
OPENMETER_PATH = "/api/v1/events"
SINK_TYPES = ("http", "stdout")
_KEYS = {"sampleInterval", "storageInterval", "spool", "requireDelivery", "prometheusUrl", "sinks"}
_SINK_KEYS = {
    "http": {"name", "type", "url", "mode", "preset", "batchSize", "headersSecretRef"},
    "stdout": {"name", "type"},
}
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$")


@dataclass(frozen=True)
class Sink:
    name: str
    type: str
    url: str = ""
    batch_size: int = 0
    headers_key: str | None = None
    preset: str | None = None


@dataclass(frozen=True)
class Config:
    sample_interval: int
    storage_interval: int
    spool_max_bytes: int
    spool_high_water_percent: int
    require_delivery: bool
    prometheus_url: str | None
    sinks: tuple[Sink, ...]


def _seconds(value: object, what: str, lo: int, hi: int) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+s", value):
        value = int(value[:-1])
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"metering.{what} must be {lo}-{hi} seconds (an integer or '<n>s'), got {value!r}")
    return value


def _int(value: object, what: str, lo: int, hi: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < lo or (hi is not None and value > hi):
        raise ValueError(f"metering.{what} must be an integer in [{lo}, {hi or '∞'}], got {value!r}")
    return value


def _sink(raw: Any, i: int) -> Sink:
    where = f"metering.sinks[{i}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a mapping")
    stype = raw.get("type")
    if stype not in SINK_TYPES:
        raise ValueError(f"{where}.type must be one of {', '.join(SINK_TYPES)}, got {stype!r}")
    unknown = set(raw) - _SINK_KEYS[stype]
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))} for a {stype} sink")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"{where}.name must be a DNS label of at most 32 characters, got {name!r}")
    if stype == "stdout":
        return Sink(name=name, type=stype)
    url, preset = raw.get("url"), raw.get("preset")
    if not isinstance(url, str) or not re.match(r"^https?://[^/]+", url):
        raise ValueError(f"{where}.url must be an http(s) URL, got {url!r}")
    if raw.get("mode") != "batch":
        raise ValueError(f"{where}.mode must be 'batch' (CloudEvents batched content mode), got {raw.get('mode')!r}")
    if preset not in (None, "openmeter"):
        raise ValueError(f"{where}.preset must be 'openmeter' or absent, got {preset!r}")
    if preset == "openmeter":
        if re.sub(r"^https?://[^/]+", "", url) not in ("", "/"):
            raise ValueError(f"{where}: the openmeter preset appends {OPENMETER_PATH}; give the base URL, got {url!r}")
        url = url.rstrip("/") + OPENMETER_PATH
    ref = raw.get("headersSecretRef")
    key = None
    if ref is not None:
        if not isinstance(ref, dict) or set(ref) != {"name", "key"} or ref["name"] != HEADERS_SECRET:
            raise ValueError(
                f"{where}.headersSecretRef must be {{name: {HEADERS_SECRET}, key: <key>}} "
                f"(k7-agent mounts only that Secret), got {ref!r}"
            )
        key = str(ref["key"])
    return Sink(
        name=name,
        type=stype,
        url=url,
        batch_size=_int(raw.get("batchSize"), "batchSize", 1, 1000),
        headers_key=key,
        preset=preset,
    )


def parse(raw: Any) -> Config:
    if not isinstance(raw, dict):
        raise ValueError("metering config must be a mapping")
    if set(raw) != _KEYS:
        raise ValueError(
            f"metering config: missing keys {sorted(_KEYS - set(raw))}, unknown {sorted(set(raw) - _KEYS)}"
        )
    spool = raw["spool"]
    if not isinstance(spool, dict) or set(spool) != {"maxBytes", "highWaterPercent"}:
        raise ValueError(f"metering.spool must have exactly maxBytes and highWaterPercent, got {spool!r}")
    if not isinstance(raw["requireDelivery"], bool):
        raise ValueError(f"metering.requireDelivery must be a boolean, got {raw['requireDelivery']!r}")
    url = raw["prometheusUrl"]
    if url not in (None, "") and (not isinstance(url, str) or not re.match(r"^https?://[^/]+", url)):
        raise ValueError(f"metering.prometheusUrl must be an http(s) URL or empty, got {url!r}")
    sinks = raw["sinks"]
    if not isinstance(sinks, list):
        raise ValueError("metering.sinks must be a list")
    parsed = tuple(_sink(s, i) for i, s in enumerate(sinks))
    names = [s.name for s in parsed]
    if len(set(names)) != len(names):
        raise ValueError(f"metering.sinks names must be unique, got {names}")
    return Config(
        sample_interval=_seconds(raw["sampleInterval"], "sampleInterval", 5, 60),
        storage_interval=_seconds(raw["storageInterval"], "storageInterval", 30, 600),
        spool_max_bytes=_int(spool["maxBytes"], "spool.maxBytes", 16 * 2**20),
        spool_high_water_percent=_int(spool["highWaterPercent"], "spool.highWaterPercent", 1, 99),
        require_delivery=raw["requireDelivery"],
        prometheus_url=url or None,
        sinks=parsed,
    )


def load() -> Config:
    path = os.environ.get(CONFIG_ENV)
    if not path:
        raise RuntimeError(f"{CONFIG_ENV} is not set: the k7 manifests and chart mount the k7-metering ConfigMap")
    try:
        return parse(json.loads(Path(path).read_text()))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"metering config {path}: {e}") from e


def cluster_id() -> str:
    """The UUID in ConfigMap ``k7-cluster-id`` (written once at install, never regenerated)."""
    raw = os.environ.get(CLUSTER_ID_ENV, "")
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise RuntimeError(f"{CLUSTER_ID_ENV}={raw!r} is not a UUID: it comes from ConfigMap k7-cluster-id (key id)")


def spool_dir() -> Path:
    return Path(os.environ.get(SPOOL_DIR_ENV) or DEFAULT_SPOOL_DIR)


def sink_headers(sink: Sink) -> dict[str, str]:
    """Re-read on every request, so a rotated Secret applies without a restart."""
    if sink.headers_key is None:
        return {}
    path = Path(os.environ.get(HEADERS_DIR_ENV, "/etc/k7-metering/headers")) / sink.headers_key
    try:
        headers = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise RuntimeError(f"sink {sink.name}: headers {path} (Secret {HEADERS_SECRET}) unreadable: {e}") from e
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise RuntimeError(f"sink {sink.name}: {path} must be a JSON object of header name to string value")
    return headers
