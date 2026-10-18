"""Structured logs.

k7-api, k7-agent and the in-cluster jobs write one JSON object per line
(``K7_LOG_FORMAT=json``); the CLI keeps its human messages. Request-scoped
fields ride a ContextVar, so core logs them without signature changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

FIELDS = ("request_id", "tenant", "namespace", "sandbox", "backend", "op", "duration_ms", "error_code")
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# API key tokens (k7_<id>_<secret>) and JWTs (OIDC). Never in a log line.
_SECRET_RE = re.compile(r"k7_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
# uvicorn's color_message repeats msg with ANSI escapes.
_RECORD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName", "color_message"}
_CTX: ContextVar[dict[str, Any] | None] = ContextVar("k7_log_context", default=None)


def scrub(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def uuid7() -> str:
    """RFC 9562 UUIDv7: 48-bit unix ms, version 7, variant 10, random tail."""
    rand = int.from_bytes(os.urandom(10), "big")
    value = (time.time_ns() // 1_000_000) << 80 | 0x7 << 76 | (rand >> 68) << 64 | 0b10 << 62 | rand & (1 << 62) - 1
    return str(uuid.UUID(int=value))


def context() -> dict[str, Any] | None:
    return _CTX.get()


def new_context(**fields: Any) -> dict[str, Any]:
    """Start a fresh field set for this task (one request, one job run)."""
    ctx = {k: v for k, v in fields.items() if v is not None}
    _CTX.set(ctx)
    return ctx


def bind(**fields: Any) -> None:
    """Add fields to the current context; no-op outside one (the CLI)."""
    ctx = _CTX.get()
    if ctx is not None:
        ctx.update({k: v for k, v in fields.items() if v is not None})


def start_request(header_value: str | None, **fields: Any) -> str:
    """Accept a well-formed incoming request id, else mint a UUIDv7."""
    request_id = header_value if header_value and _REQUEST_ID_RE.match(header_value) else uuid7()
    new_context(request_id=request_id, **fields)
    return request_id


def request_id() -> str | None:
    ctx = _CTX.get()
    return ctx.get("request_id") if ctx else None


def access(logger: logging.Logger, method: str, route: str, status: int, elapsed_s: float, **fields: Any) -> None:
    """One line per request; kubelet's /health probes only at DEBUG."""
    extra = {"method": method, "route": route, "status": status, "duration_ms": round(elapsed_s * 1000, 1), **fields}
    level = logging.DEBUG if route == "/health" else logging.INFO
    logger.log(level, "%s %s %d", method, route, status, extra=extra)


class _Scrubbing(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class HumanFormatter(_Scrubbing):
    """The CLI's legacy ``print(..., file=sys.stderr)`` lines, verbatim."""

    def __init__(self) -> None:
        super().__init__("%(message)s")


class JsonFormatter(_Scrubbing):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **dict.fromkeys(FIELDS),
            **(_CTX.get() or {}),
            **{k: v for k, v in vars(record).items() if k not in _RECORD_ATTRS},
        }
        if record.exc_info and record.levelno >= logging.ERROR:
            line["exc"] = self.formatException(record.exc_info)
        return scrub(json.dumps(line, default=str, ensure_ascii=False))


class ConsoleHandler(logging.StreamHandler):
    """Resolves ``sys.stdout``/``sys.stderr`` per record, so redirected or captured streams work."""

    def __init__(self, stream_name: str = "stderr") -> None:
        self.stream_name = stream_name
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return getattr(sys, self.stream_name)

    @stream.setter
    def stream(self, _value) -> None:
        pass


def _handler(formatter: logging.Formatter, level: int = logging.NOTSET) -> ConsoleHandler:
    handler = ConsoleHandler()
    handler.setFormatter(formatter)
    handler.setLevel(level)
    return handler


def configure(fmt: str, level: str) -> None:
    """Install the one k7 handler on the root logger (idempotent)."""
    if fmt not in ("json", "human"):
        raise RuntimeError(f"K7_LOG_FORMAT must be 'json' or 'human', got {fmt!r}")
    if level not in LEVELS:
        raise RuntimeError(f"K7_LOG_LEVEL must be one of {sorted(LEVELS)}, got {level!r}")
    root, k7 = logging.getLogger(), logging.getLogger("k7")
    for lg in (root, k7):
        for h in [h for h in lg.handlers if isinstance(h, ConsoleHandler)]:
            lg.removeHandler(h)
    if fmt == "json":
        root.addHandler(_handler(JsonFormatter()))
        root.setLevel(LEVELS[level])
        k7.setLevel(logging.NOTSET)
        k7.propagate = True
    else:
        # The CLI shows k7's own lines at the chosen level. Other libraries
        # keep what the stdlib last resort showed (bare WARNING+), scrubbed.
        k7.addHandler(_handler(HumanFormatter()))
        k7.setLevel(LEVELS[level])
        k7.propagate = False
        root.addHandler(_handler(HumanFormatter(), logging.WARNING))
    # uvicorn's own handlers would bypass the formatter; the request middleware
    # logs each request (with its id), so the access log is redundant.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.setLevel(logging.NOTSET)
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # Per-op summary lines are for collectors; the CLI prints its own result.
    logging.getLogger("k7.op").setLevel(logging.WARNING if fmt == "human" else logging.NOTSET)


def configure_from_env() -> None:
    """Pods must say how to log: the manifests set both, there is no default."""
    missing = [v for v in ("K7_LOG_FORMAT", "K7_LOG_LEVEL") if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"{' and '.join(missing)} must be set (the k7 manifests set K7_LOG_FORMAT=json)")
    configure(os.environ["K7_LOG_FORMAT"], os.environ["K7_LOG_LEVEL"])
