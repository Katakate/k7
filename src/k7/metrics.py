"""Prometheus metrics for k7-api and k7-agent.

Each process has its own registry, served on a separate plain-HTTP port
(``K7_METRICS_PORT``, 9464 in the manifests) that answers ``/metrics`` only.
No metric carries a sandbox name or uid label (tests/unit/test_metrics.py).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server

from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    make_wsgi_app,
)
from prometheus_client.exposition import ThreadingWSGIServer

from . import logging as k7_logging

disable_created_metrics()
API = CollectorRegistry()
AGENT = CollectorRegistry()
for _registry in (API, AGENT):
    for _collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
        _registry.register(_collector)

FORBIDDEN_LABELS = frozenset({"sandbox", "sandbox_name", "name", "uid", "pod", "vm_id", "sandbox_id"})
OPS = ("create", "delete", "fork", "pause", "resume", "snapshot", "restore", "rollback", "export")
# PERFORMANCE.md: k7d warm fork ~5 ms at the VMM, ~2 s through the API; kql
# fork 8-47 s; kql create ~17 s; deletes 40 ms. One set resolves all of them.
OP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600)

HTTP_REQUESTS = Counter("k7_http_requests_total", "k7-api HTTP requests", ["route", "method", "status"], registry=API)
HTTP_DURATION = Histogram(
    "k7_http_request_duration_seconds",
    "k7-api HTTP request latency",
    ["route", "method"],
    buckets=OP_BUCKETS,
    registry=API,
)
OP_DURATION = Histogram(
    "k7_operation_duration_seconds",
    "Sandbox lifecycle operation latency",
    ["op", "backend", "outcome"],
    buckets=OP_BUCKETS,
    registry=API,
)
OP_ERRORS = Counter(
    "k7_operation_errors_total", "Failed sandbox lifecycle operations", ["op", "backend", "error_code"], registry=API
)
AUTH_FAILURES = Counter("k7_auth_failures_total", "Rejected API requests", ["reason"], registry=API)
for _reason in ("missing", "invalid", "expired", "forbidden", "oidc_invalid"):
    AUTH_FAILURES.labels(_reason)
KEY_STORE_SYNC = Gauge(
    "k7_key_store_last_sync_timestamp_seconds", "Last successful read of the API key Secret", registry=API
)
KUBE_API_ERRORS = Counter(
    "k7_kube_api_errors_total", "Kubernetes API calls that failed", ["verb", "resource", "status"], registry=API
)
QUOTA_REJECTIONS = Counter(
    "k7_quota_rejections_total", "Creates refused by a tenant quota", ["tenant", "dimension"], registry=API
)
RATE_LIMITED = Counter("k7_rate_limited_total", "Requests refused by a rate limit", ["tenant", "class"], registry=API)
SANDBOXES = Gauge(
    "k7_sandboxes",
    "Sandboxes by tenant, backend and state (leader replica only)",
    ["tenant", "backend", "state"],
    registry=API,
)
API_INFO = Gauge("k7_api_info", "k7-api build and mode", ["version", "mode"], registry=API)
TLS_EXPIRY = Gauge(
    "k7_tls_cert_expiry_timestamp_seconds", "notAfter of the certificate k7-api serves", ["source"], registry=API
)

# Usage meters. Never a sandbox label: per-sandbox detail is in the events.
_USAGE = ["tenant", "namespace", "backend", "state"]
USAGE_SANDBOX_SECONDS = Counter("k7_usage_sandbox_seconds", "Sandbox seconds (node sampler)", _USAGE, registry=AGENT)
USAGE_VCPU_SECONDS = Counter(
    "k7_usage_vcpu_seconds", "Reserved vCPU seconds (none while paused)", _USAGE, registry=AGENT
)
USAGE_MEMORY_GIB_SECONDS = Counter("k7_usage_memory_gib_seconds", "Reserved memory GiB seconds", _USAGE, registry=AGENT)
USAGE_EGRESS_BYTES = Counter(
    "k7_usage_egress_bytes", "Bytes sent by sandbox pods", ["tenant", "namespace", "backend", "scope"], registry=AGENT
)
USAGE_EGRESS_UNAVAILABLE = Gauge(
    "k7_usage_egress_unavailable",
    "1 if the runtime reports no network stats for this backend",
    ["backend"],
    registry=AGENT,
)
USAGE_EVENTS_DROPPED = Counter("k7_usage_events_dropped", "Usage events lost to the spool size cap", registry=AGENT)
USAGE_SPOOL_BYTES = Gauge("k7_usage_spool_bytes", "Bytes in the node usage spool", registry=AGENT)
USAGE_SINK_STOPPED = Gauge("k7_usage_sink_stopped", "1 once a sink refused events with a 4xx", ["sink"], registry=AGENT)
USAGE_SINK_PENDING_BYTES = Gauge(
    "k7_usage_sink_pending_bytes", "Spool bytes a sink has not delivered yet", ["sink"], registry=AGENT
)
USAGE_GAPS = Counter("k7_usage_gaps", "Usage gap events emitted", ["meter", "reason"], registry=AGENT)
USAGE_SAMPLE_FAILURES = Counter("k7_usage_sample_failures", "Node usage samples that failed", registry=AGENT)
USAGE_DISK_BYTES = Gauge(
    "k7_usage_disk_bytes",
    "Provisioned sandbox disk (metering leader)",
    ["tenant", "namespace", "backend"],
    registry=API,
)
USAGE_SNAPSHOT_BYTES = Gauge(
    "k7_usage_snapshot_bytes",
    "Snapshot bytes (metering leader)",
    ["tenant", "namespace", "kind", "location"],
    registry=API,
)

K7D_RPC_DURATION = Histogram(
    "k7d_rpc_duration_seconds", "k7d daemon control-socket call latency", ["op"], buckets=OP_BUCKETS, registry=AGENT
)
K7D_RPC_ERRORS = Counter("k7d_rpc_errors_total", "Failed k7d daemon calls", ["op"], registry=AGENT)

log = logging.getLogger("k7.op")
_IN_OP: ContextVar[bool] = ContextVar("k7_in_op", default=False)
_ERROR_CODES = (
    ("not found", "NotFound"),
    ("already exists", "Conflict"),
    ("not supported", "Unsupported"),
    ("timed out", "Timeout"),
    ("within", "Timeout"),
    ("kubernetes error", "KubernetesError"),
)
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def error_code(error: str) -> str:
    """Bounded label for an OperationResult error (never the message itself)."""
    text = error.lower()
    return next((code for needle, code in _ERROR_CODES if needle in text), "Failed")


def observed(op: str) -> Callable[[F], F]:
    """Time a K7Core lifecycle op; a nested op (restore → create) counts once."""

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _IN_OP.get():
                return await fn(*args, **kwargs)
            ctx = k7_logging.context()
            if ctx is None:
                ctx = k7_logging.new_context()
            ctx["op"] = op
            token = _IN_OP.set(True)
            start = time.monotonic()
            code: str | None = "Exception"
            try:
                result = await fn(*args, **kwargs)
                code = None if getattr(result, "success", True) else error_code(getattr(result, "error", "") or "")
                return result
            finally:
                _IN_OP.reset(token)
                elapsed = time.monotonic() - start
                backend = ctx.get("backend") or "unknown"
                OP_DURATION.labels(op, backend, "success" if code is None else "error").observe(elapsed)
                if code is not None:
                    OP_ERRORS.labels(op, backend, code).inc()
                outcome = "ok" if code is None else "failed"
                log.info("%s %s", op, outcome, extra={"duration_ms": round(elapsed * 1000, 1), "error_code": code})

        return cast(F, wrapper)

    return deco


_KUBE_RESOURCE_RE = re.compile(r"^/(?:api/v1|apis/[^/]+/[^/]+)(?:/namespaces/[^/]+)?/([^/?]+)")


def kube_api_error(method: str, url: str, status: int | None) -> None:
    m = _KUBE_RESOURCE_RE.match(urlsplit(url).path)
    KUBE_API_ERRORS.labels(method.lower(), m.group(1) if m else "other", str(status or 0)).inc()


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve(registry: CollectorRegistry) -> ThreadingWSGIServer | None:
    """Start the ``/metrics`` listener from ``K7_METRICS_PORT`` (``off`` disables it)."""
    raw = os.environ.get("K7_METRICS_PORT")
    if not raw:
        raise RuntimeError("K7_METRICS_PORT must be set (9464 in the k7 manifests, or 'off')")
    if raw == "off":
        return None
    metrics_app = make_wsgi_app(registry)

    def only_metrics(environ, start_response):
        if environ.get("PATH_INFO") != "/metrics":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"only /metrics is served here\n"]
        return metrics_app(environ, start_response)

    httpd = make_server("0.0.0.0", int(raw), only_metrics, ThreadingWSGIServer, handler_class=_QuietHandler)
    threading.Thread(target=httpd.serve_forever, name="k7-metrics", daemon=True).start()
    return httpd
