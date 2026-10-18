"""k7-api gauges refreshed in the background: ``k7_sandboxes`` and TLS expiry.

``k7_sandboxes`` is a cluster-wide count, so only one replica exports it:
the Ready k7-api pod with the smallest name. The others export none, so
``sum()`` never double-counts and no lease object (or RBAC for one) is needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import ssl
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .. import metrics
from ..core import cluster_config
from ..core.core import K7_TENANT_LABEL, K7D_ANN_PAUSED, K7Core, aclose_k7_core

log = logging.getLogger("k7.api.gauges")
INTERVAL_S = 30
CADDY_TLS_PORT = 8443


def sandbox_state(dep: Any) -> str:
    if (dep.metadata.annotations or {}).get(K7D_ANN_PAUSED) == "true" or dep.spec.replicas == 0:
        return "paused"
    return "running" if (dep.status.ready_replicas or 0) > 0 else "pending"


def _ready(pod: Any) -> bool:
    conditions = pod.status.conditions or []
    return pod.status.phase == "Running" and any(c.type == "Ready" and c.status == "True" for c in conditions)


async def refresh_sandboxes() -> None:
    core = K7Core()
    try:
        v1 = await core._get_core_v1_client()
        pods = await v1.list_namespaced_pod(cluster_config.system_namespace(), label_selector="app=k7-api")
        ready = sorted(p.metadata.name for p in pods.items if _ready(p))
        if not ready or ready[0] != socket.gethostname():
            metrics.SANDBOXES.clear()
            return
        counts: Counter[tuple[str, str, str]] = Counter()
        for dep in await core._get_kata_sandboxes(None):
            backend = core._canonicalize_backend((dep.metadata.annotations or {}).get("k7.katakate.org/backend"))
            tenant = (dep.metadata.labels or {}).get(K7_TENANT_LABEL, "")
            counts[(tenant, backend or "unknown", sandbox_state(dep))] += 1
    finally:
        await aclose_k7_core(core)
    metrics.SANDBOXES.clear()
    for labels, n in counts.items():
        metrics.SANDBOXES.labels(*labels).set(n)


def der_not_after(der: bytes) -> float:
    """``notAfter`` of a DER X.509 certificate (RFC 5280 TBSCertificate.validity)."""

    def tlv(i: int) -> tuple[int, int, int]:
        tag, length, i = der[i], der[i + 1], i + 2
        if length & 0x80:
            n = length & 0x7F
            length, i = int.from_bytes(der[i : i + n], "big"), i + n
        return tag, i, i + length

    i = tlv(tlv(0)[1])[1]  # Certificate → TBSCertificate contents
    tag, _, end = tlv(i)
    if tag == 0xA0:  # explicit [0] version
        i = end
    for _ in range(3):  # serialNumber, signature, issuer
        i = tlv(i)[2]
    not_before_end = tlv(tlv(i)[1])[2]
    tag, start, end = tlv(not_before_end)
    fmt = "%y%m%d%H%M%SZ" if tag == 0x17 else "%Y%m%d%H%M%SZ"  # UTCTime / GeneralizedTime
    return datetime.strptime(der[start:end].decode(), fmt).replace(tzinfo=timezone.utc).timestamp()


async def served_cert_not_after(sni: str) -> float:
    """notAfter of the certificate the Caddy sidecar serves (``sni`` "" sends none)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", CADDY_TLS_PORT, ssl=ctx, server_hostname=sni), timeout=5
    )
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
        await writer.wait_closed()
    return der_not_after(der)


def tls_config() -> tuple[str, str] | None:
    """``(source, sni)`` from the TLS overlay's env; None when k7-api serves plain HTTP."""
    source = os.environ.get("K7_TLS_CERT_SOURCE")
    if source is None:
        return None
    if "K7_TLS_SNI" not in os.environ:
        raise RuntimeError("K7_TLS_CERT_SOURCE is set but K7_TLS_SNI is not (empty for operator certificates)")
    return source, os.environ["K7_TLS_SNI"]


async def run(tls: tuple[str, str] | None) -> None:
    async def refresh_tls() -> None:
        if tls:
            metrics.TLS_EXPIRY.labels(tls[0]).set(await served_cert_not_after(tls[1]))

    while True:
        for name, refresh in (("k7_sandboxes", refresh_sandboxes), ("tls expiry", refresh_tls)):
            try:
                await refresh()
            except Exception:
                log.exception("refreshing %s failed", name)
        await asyncio.sleep(INTERVAL_S)
