"""Prometheus metrics: registry contents, cardinality rule, buckets, listener, collectors."""

import asyncio
import bisect
import datetime as dt
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from prometheus_client import CollectorRegistry

from k7 import logging as k7_logging
from k7 import metrics
from k7.api import agent as agent_mod
from k7.api import gauges
from k7.core.models import OperationResult

API_METRICS = {
    "k7_http_requests_total": {"route", "method", "status"},
    "k7_http_request_duration_seconds": {"route", "method"},
    "k7_operation_duration_seconds": {"op", "backend", "outcome"},
    "k7_operation_errors_total": {"op", "backend", "error_code"},
    "k7_auth_failures_total": {"reason"},
    "k7_key_store_last_sync_timestamp_seconds": set(),
    "k7_kube_api_errors_total": {"verb", "resource", "status"},
    "k7_quota_rejections_total": {"tenant", "dimension"},
    "k7_rate_limited_total": {"tenant", "class"},
    "k7_sandboxes": {"tenant", "backend", "state"},
    "k7_api_info": {"version", "mode"},
    "k7_tls_cert_expiry_timestamp_seconds": {"source"},
    "k7_usage_disk_bytes": {"tenant", "namespace", "backend"},
    "k7_usage_snapshot_bytes": {"tenant", "namespace", "kind", "location"},
}
AGENT_METRICS = {
    "k7d_rpc_duration_seconds": {"op"},
    "k7d_rpc_errors_total": {"op"},
    "k7_usage_sandbox_seconds_total": {"tenant", "namespace", "backend", "state"},
    "k7_usage_vcpu_seconds_total": {"tenant", "namespace", "backend", "state"},
    "k7_usage_memory_gib_seconds_total": {"tenant", "namespace", "backend", "state"},
    "k7_usage_egress_bytes_total": {"tenant", "namespace", "backend", "scope"},
    "k7_usage_egress_unavailable": {"backend"},
    "k7_usage_events_dropped_total": set(),
    "k7_usage_spool_bytes": set(),
    "k7_usage_sink_stopped": {"sink"},
    "k7_usage_sink_pending_bytes": {"sink"},
    "k7_usage_gaps_total": {"meter", "reason"},
    "k7_usage_sample_failures_total": set(),
}


def _declared(registry: CollectorRegistry) -> dict[str, set[str]]:
    collectors = registry._collector_to_names  # the label schema lives on each collector
    return {c._name: set(c._labelnames) for c in collectors if hasattr(c, "_labelnames")}


def _sample(registry: CollectorRegistry, name: str, labels: dict[str, str]) -> float:
    value = registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_registries_declare_the_spec_metrics():
    assert _declared(metrics.API) == {n.removesuffix("_total"): labels for n, labels in API_METRICS.items()}
    assert _declared(metrics.AGENT) == {n.removesuffix("_total"): labels for n, labels in AGENT_METRICS.items()}


def test_no_sandbox_labels_anywhere(tmp_path: Path, monkeypatch):
    """Cardinality rule: neither the declared schema nor any emitted sample names a sandbox."""
    _fake_node(tmp_path, monkeypatch)
    with patch("subprocess.run", return_value=_crictl([("kata", "SANDBOX_READY")])):
        samples = [s for r in (metrics.API, metrics.AGENT) for fam in r.collect() for s in fam.samples]
    for labels in [*_declared(metrics.API).values(), *_declared(metrics.AGENT).values()]:
        assert not labels & metrics.FORBIDDEN_LABELS
    for s in samples:
        assert not set(s.labels) & metrics.FORBIDDEN_LABELS, s


def test_op_buckets_separate_k7d_and_kql_fork():
    hist = metrics.OP_DURATION.labels("fork", "k7d", "success")
    assert tuple(hist._upper_bounds[:-1]) == metrics.OP_BUCKETS
    # PERFORMANCE.md: k7d fork ~2 s through the API, kql fork 8-47 s; deletes ~40 ms.
    k7d, kql, delete = (bisect.bisect_left(metrics.OP_BUCKETS, s) for s in (2.0, 7.8, 0.04))
    assert len({k7d, kql, delete}) == 3
    assert metrics.OP_BUCKETS[0] <= 0.005 and metrics.OP_BUCKETS[-1] >= 600


def test_observed_counts_outcomes_once():
    labels = {"op": "restore", "backend": "kata-qemu-longhorn", "outcome": "error"}
    before = _sample(metrics.API, "k7_operation_duration_seconds_count", labels)
    nested_before = _sample(metrics.API, "k7_operation_duration_seconds_count", {**labels, "op": "create"})

    @metrics.observed("create")
    async def create():
        return OperationResult(success=False, error="PVC timed out")

    @metrics.observed("restore")
    async def restore():
        k7_logging.bind(backend="kata-qemu-longhorn")
        return await create()

    k7_logging.new_context()
    asyncio.run(restore())
    assert _sample(metrics.API, "k7_operation_duration_seconds_count", labels) == before + 1
    assert _sample(metrics.API, "k7_operation_duration_seconds_count", {**labels, "op": "create"}) == nested_before
    errors = {"op": "restore", "backend": "kata-qemu-longhorn", "error_code": "Timeout"}
    assert _sample(metrics.API, "k7_operation_errors_total", errors) >= 1


def test_observed_exception_and_unknown_backend():
    @metrics.observed("delete")
    async def boom():
        raise RuntimeError("kube down")

    k7_logging.new_context()
    errors = {"op": "delete", "backend": "unknown", "error_code": "Exception"}
    before = _sample(metrics.API, "k7_operation_errors_total", errors)
    with pytest.raises(RuntimeError):
        asyncio.run(boom())
    assert _sample(metrics.API, "k7_operation_errors_total", errors) == before + 1


@pytest.mark.parametrize(
    ("url", "resource"),
    [
        ("https://10.43.0.1/api/v1/namespaces/team-a/pods/sb-1/exec?command=ls", "pods"),
        ("https://10.43.0.1/apis/apps/v1/namespaces/team-a/deployments", "deployments"),
        ("https://10.43.0.1/apis/snapshot.storage.k8s.io/v1/volumesnapshots", "volumesnapshots"),
        ("https://10.43.0.1/api/v1/nodes/n1", "nodes"),
        ("https://10.43.0.1/version", "other"),
    ],
)
def test_kube_api_error_labels_are_bounded(url: str, resource: str):
    labels = {"verb": "get", "resource": resource, "status": "404"}
    before = _sample(metrics.API, "k7_kube_api_errors_total", labels)
    metrics.kube_api_error("GET", url, 404)
    assert _sample(metrics.API, "k7_kube_api_errors_total", labels) == before + 1


def test_listener_serves_only_metrics(monkeypatch):
    monkeypatch.setenv("K7_METRICS_PORT", "0")
    server = metrics.serve(metrics.API)
    assert server is not None
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        r = httpx.get(f"{base}/metrics")
        assert r.status_code == 200 and "k7_auth_failures_total" in r.text
        assert "process_cpu_seconds_total" in r.text and "_created" not in r.text
        assert httpx.get(f"{base}/").status_code == 404
        assert httpx.get(f"{base}/health").status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_listener_port_is_required(monkeypatch):
    monkeypatch.setenv("K7_METRICS_PORT", "off")
    assert metrics.serve(metrics.API) is None
    monkeypatch.delenv("K7_METRICS_PORT")
    with pytest.raises(RuntimeError, match="K7_METRICS_PORT"):
        metrics.serve(metrics.API)


async def test_api_counts_requests_and_auth_failures():
    transport = httpx.ASGITransport(app=__import__("k7.api.main", fromlist=["app"]).app)
    req = {"route": "/api/v1/sandboxes", "method": "GET", "status": "401"}
    before = (
        _sample(metrics.API, "k7_http_requests_total", req),
        _sample(metrics.API, "k7_auth_failures_total", {"reason": "missing"}),
    )
    unmatched = {"route": "<unmatched>", "method": "OTHER", "status": "404"}
    before_unmatched = _sample(metrics.API, "k7_http_requests_total", unmatched)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/sandboxes")).status_code == 401
        assert (await client.request("BREW", "/no/such/route")).status_code == 404
    assert _sample(metrics.API, "k7_http_requests_total", req) == before[0] + 1
    assert _sample(metrics.API, "k7_auth_failures_total", {"reason": "missing"}) == before[1] + 1
    assert _sample(metrics.API, "k7_http_requests_total", unmatched) == before_unmatched + 1


# --- k7-agent node collector ---------------------------------------------------


def _fake_node(tmp_path: Path, monkeypatch) -> socket.socket:
    report = tmp_path / "storage.json"
    lv = {
        "lv_name": "thin-pool",
        "lv_attr": "twi-aotz--",
        "lv_size": "1000",
        "lv_metadata_size": "100",
        "data_percent": "25.00",
        "metadata_percent": "50.00",
    }
    report.write_text(json.dumps({"report": [{"lv": [lv]}]}))
    disks = tmp_path / "disks"
    disks.mkdir()
    sock_path = tmp_path / "k7d.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)
    monkeypatch.setattr(agent_mod, "STORAGE_REPORT", str(report))
    monkeypatch.setattr(agent_mod, "K7D_DISKS_DIR", str(disks))
    monkeypatch.setenv("K7D_SOCKET", str(sock_path))
    monkeypatch.setenv("K7_CONTAINERD_SOCKET", "/run/containerd/containerd.sock")
    return listener


def _crictl(pods: list[tuple[str, str]]) -> subprocess.CompletedProcess:
    items = [{"id": f"p{i}", "runtimeHandler": h, "state": s} for i, (h, s) in enumerate(pods)]
    return subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": items}), stderr="")


def _scrape() -> dict[tuple[str, tuple], float]:
    out = {}
    for fam in agent_mod.NodeCollector().collect():
        for s in fam.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def test_node_collector(tmp_path: Path, monkeypatch):
    listener = _fake_node(tmp_path, monkeypatch)
    pods = [("k7", "SANDBOX_READY"), ("k7", "SANDBOX_NOTREADY"), ("kata-qemu", "SANDBOX_READY"), ("runc", "x")]
    try:
        with patch("subprocess.run", return_value=_crictl(pods)) as run:
            got = _scrape()
    finally:
        listener.close()
    assert run.call_args.args[0][-3:] == ["pods", "-o", "json"]

    def pool(name: str, kind: str) -> float:
        return got[("k7_storage_pool_bytes", (("kind", kind), ("pool", name)))]

    assert (pool("thinpool_data", "size"), pool("thinpool_data", "used")) == (1000, 250)
    assert (pool("thinpool_metadata", "size"), pool("thinpool_metadata", "used")) == (100, 50)
    st = os.statvfs(tmp_path / "disks")
    assert pool("k7d_disks", "size") == st.f_blocks * st.f_frsize
    assert 0 <= got[("k7_storage_report_age_seconds", ())] < 60
    assert got[("k7d_daemon_up", ())] == 1
    vm = {k[1]: v for k, v in got.items() if k[0] == "k7_vm_count"}
    assert vm[(("backend", "k7d"), ("state", "ready"))] == 1
    assert vm[(("backend", "k7d"), ("state", "notready"))] == 1
    assert vm[(("backend", "kata-qemu-longhorn"), ("state", "ready"))] == 1
    assert vm[(("backend", "kata-firecracker-devmapper"), ("state", "ready"))] == 0
    assert len(vm) == 8


def test_node_collector_daemon_down_and_failing_source(tmp_path: Path, monkeypatch, caplog):
    _fake_node(tmp_path, monkeypatch).close()
    (tmp_path / "k7d.sock").unlink()
    with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "crictl")):
        got = _scrape()
    assert got[("k7d_daemon_up", ())] == 0
    assert not any(k[0] == "k7_vm_count" for k in got)
    assert ("k7_storage_pool_bytes", (("kind", "size"), ("pool", "thinpool_data"))) in got
    assert "collecting vm metrics failed" in caplog.text


def test_node_collector_without_kfd_or_k7d(tmp_path: Path, monkeypatch):
    _fake_node(tmp_path, monkeypatch).close()
    monkeypatch.setattr(agent_mod, "STORAGE_REPORT", str(tmp_path / "absent.json"))
    monkeypatch.setattr(agent_mod, "K7D_DISKS_DIR", str(tmp_path / "absent"))
    with patch("subprocess.run", return_value=_crictl([])):
        names = {k[0] for k in _scrape()}
    assert names == {"k7_vm_count"}


# --- k7-api background gauges ---------------------------------------------------


def _cert(not_after: dt.datetime, key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    key = key or ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "k7.example")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        .not_valid_after(not_after)
    )
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


@pytest.mark.parametrize(
    "not_after",
    [dt.datetime(2026, 12, 31, 23, 59, 58, tzinfo=dt.timezone.utc), dt.datetime(2051, 6, 1, tzinfo=dt.timezone.utc)],
)
def test_der_not_after(not_after: dt.datetime):
    # 2051 is past UTCTime's range, so the certificate carries a GeneralizedTime.
    assert gauges.der_not_after(_cert(not_after)) == not_after.timestamp()


def test_tls_config(monkeypatch):
    monkeypatch.delenv("K7_TLS_CERT_SOURCE", raising=False)
    assert gauges.tls_config() is None
    monkeypatch.setenv("K7_TLS_CERT_SOURCE", "acme")
    monkeypatch.delenv("K7_TLS_SNI", raising=False)
    with pytest.raises(RuntimeError, match="K7_TLS_SNI"):
        gauges.tls_config()
    monkeypatch.setenv("K7_TLS_SNI", "k7.example")
    assert gauges.tls_config() == ("acme", "k7.example")


def _dep(backend: str, *, tenant: str | None = None, replicas: int = 1, ready: int = 1, paused: bool = False):
    annotations = {"k7.katakate.org/backend": backend, **({"k7.katakate.org/k7d-paused": "true"} if paused else {})}
    labels = {"k7.katakate.org/tenant": tenant} if tenant else {}
    return SimpleNamespace(
        metadata=SimpleNamespace(annotations=annotations, labels=labels),
        spec=SimpleNamespace(replicas=replicas),
        status=SimpleNamespace(ready_replicas=ready),
    )


def _pod(name: str, ready: bool):
    cond = SimpleNamespace(type="Ready", status="True" if ready else "False")
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name), status=SimpleNamespace(phase="Running", conditions=[cond])
    )


@pytest.mark.parametrize(("hostname", "leader"), [("k7-api-a", True), ("k7-api-b", False)])
def test_sandboxes_gauge_only_on_leader(hostname: str, leader: bool):
    deps = [
        _dep("k7d", tenant="acme"),
        _dep("k7", tenant="acme"),
        _dep("kql", replicas=0, ready=0),
        _dep("k7d", paused=True),
        _dep("kata-qemu-longhorn", ready=0),
    ]
    pods = SimpleNamespace(items=[_pod("k7-api-b", True), _pod("k7-api-a", True), _pod("k7-api-0", False)])
    core = MagicMock()
    core._get_core_v1_client = AsyncMock(return_value=MagicMock(list_namespaced_pod=AsyncMock(return_value=pods)))
    core._get_kata_sandboxes = AsyncMock(return_value=deps)
    from k7.core.core import K7Core

    core._canonicalize_backend = K7Core._canonicalize_backend
    with patch.object(gauges, "K7Core", return_value=core), patch("socket.gethostname", return_value=hostname):
        asyncio.run(gauges.refresh_sandboxes())
    got = {tuple(s.labels.values()): s.value for fam in metrics.SANDBOXES.collect() for s in fam.samples}
    if not leader:
        assert got == {}
        return
    assert got == {
        ("acme", "k7d", "running"): 2,
        ("", "kata-qemu-longhorn", "paused"): 1,
        ("", "k7d", "paused"): 1,
        ("", "kata-qemu-longhorn", "pending"): 1,
    }


async def test_tls_expiry_from_the_served_cert_and_loop_logs_failures(tmp_path: Path, monkeypatch, caplog):
    not_after = dt.datetime(2027, 3, 1, tzinfo=dt.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    der = _cert(not_after, key)
    (tmp_path / "c.pem").write_bytes(x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    snis: list[str | None] = []
    ctx.sni_callback = lambda sock, name, _ctx: snis.append(name)

    async def handle(_reader, writer):
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    monkeypatch.setattr(gauges, "CADDY_TLS_PORT", server.sockets[0].getsockname()[1])
    monkeypatch.setattr(gauges, "refresh_sandboxes", AsyncMock(side_effect=RuntimeError("kube down")))
    task = asyncio.create_task(gauges.run(("acme", "k7.example")))
    try:
        for _ in range(200):
            if metrics.API.get_sample_value("k7_tls_cert_expiry_timestamp_seconds", {"source": "acme"}):
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        server.close()
    assert metrics.API.get_sample_value("k7_tls_cert_expiry_timestamp_seconds", {"source": "acme"}) == (
        not_after.timestamp()
    )
    assert snis == ["k7.example"]
    assert "refreshing k7_sandboxes failed" in caplog.text and "kube down" in caplog.text


async def test_tls_probe_failure_is_logged_not_fatal(monkeypatch, caplog):
    monkeypatch.setattr(gauges, "refresh_sandboxes", AsyncMock())
    monkeypatch.setattr(gauges, "served_cert_not_after", AsyncMock(side_effect=ConnectionRefusedError("caddy")))
    task = asyncio.create_task(gauges.run(("files", "")))
    for _ in range(200):
        if "refreshing tls expiry failed" in caplog.text:
            break
        await asyncio.sleep(0.01)
    assert not task.done(), "a failed probe must not end the refresh loop"
    task.cancel()
    assert "refreshing tls expiry failed" in caplog.text and "caddy" in caplog.text


async def test_kube_api_client_counts_failures():
    from kubernetes_asyncio.client.exceptions import ApiException

    from k7.core import core as core_mod

    outcomes: list[object] = [ApiException(status=409), OSError("connection reset"), "ok"]

    async def request(method, url, *args, **kwargs):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    api = core_mod._count_kube_api_errors(SimpleNamespace(request=request))
    url = "https://10.43.0.1/api/v1/namespaces/team-a/secrets/k7-api-keys"
    conflict, transport, ok = ({"verb": "put", "resource": "secrets", "status": s} for s in ("409", "0", "200"))
    before = [_sample(metrics.API, "k7_kube_api_errors_total", lbl) for lbl in (conflict, transport)]
    with pytest.raises(ApiException):
        await api.request("PUT", url)
    with pytest.raises(OSError):
        await api.request("PUT", url)
    assert await api.request("PUT", url) == "ok"
    after = [_sample(metrics.API, "k7_kube_api_errors_total", lbl) for lbl in (conflict, transport)]
    assert after == [before[0] + 1, before[1] + 1]
    assert _sample(metrics.API, "k7_kube_api_errors_total", ok) == 0

    core = core_mod.K7Core()
    with (
        patch.object(core, "_load_k3s_config", AsyncMock()),
        patch.object(core_mod.client, "ApiClient", return_value=SimpleNamespace(request=request)),
    ):
        assert (await core._get_api_client()).request.__name__ == "counted"


def test_k7d_rpc_metrics(tmp_path: Path, monkeypatch):
    from k7.core.core import K7Core

    sock_path = tmp_path / "k7d.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(2)

    def daemon():
        for reply in (b'{"status": "ok"}\n', b'{"status": "error", "message": "no such vm"}\n'):
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(reply)
            conn.close()

    threading.Thread(target=daemon, daemon=True).start()
    monkeypatch.setenv("K7D_SOCKET", str(sock_path))
    core = K7Core()

    def counts() -> tuple[float, float, float]:
        return (
            _sample(metrics.AGENT, "k7d_rpc_duration_seconds_count", {"op": "fork"}),
            _sample(metrics.AGENT, "k7d_rpc_errors_total", {"op": "fork"}),
            _sample(metrics.AGENT, "k7d_rpc_errors_total", {"op": "pause"}),
        )

    before = counts()
    assert asyncio.run(core._k7d_request({"op": "fork"})) == {"status": "ok"}
    with pytest.raises(RuntimeError, match="no such vm"):
        asyncio.run(core._k7d_request({"op": "fork"}))
    listener.close()
    sock_path.unlink()
    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(core._k7d_request({"op": "pause"}))
    assert counts() == (before[0] + 2, before[1] + 1, before[2] + 1)


async def test_key_store_refresh_sets_sync_timestamp():
    from k7.api.keystore import SecretKeyStore

    store = SecretKeyStore("kube-system")
    core = MagicMock(read_secret_data=AsyncMock(return_value={}))
    start = time.time()
    await store.refresh(core)
    assert start <= metrics.API.get_sample_value("k7_key_store_last_sync_timestamp_seconds") <= time.time()
    core.read_secret_data = AsyncMock(return_value=None)
    synced = metrics.API.get_sample_value("k7_key_store_last_sync_timestamp_seconds")
    with pytest.raises(RuntimeError, match="not found"):
        await store.refresh(core)
    assert metrics.API.get_sample_value("k7_key_store_last_sync_timestamp_seconds") == synced


def _serving(monkeypatch) -> list:
    servers: list = []
    serve = metrics.serve

    def capture(registry):
        servers.append(serve(registry))
        return servers[-1]

    monkeypatch.setenv("K7_METRICS_PORT", "0")
    monkeypatch.setattr(metrics, "serve", capture)
    return servers


async def test_api_lifespan_serves_metrics_and_info(tmp_path: Path, monkeypatch):
    from k7 import __version__
    from k7.api import main

    servers = _serving(monkeypatch)
    monkeypatch.setenv("K7_API_KEY_STORE", "file")
    monkeypatch.delenv("K7_TLS_CERT_SOURCE", raising=False)
    started: list = []

    async def fake_run(tls):
        started.append(tls)
        await asyncio.Event().wait()

    monkeypatch.setattr(main.gauges, "run", fake_run)
    monkeypatch.setattr(main, "reconcile_sandbox_egress", AsyncMock())
    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        body = httpx.get(f"http://127.0.0.1:{servers[0].server_port}/metrics").text
    assert f'k7_api_info{{mode="development",version="{__version__}"}} 1.0' in body
    assert "# TYPE k7_sandboxes gauge" in body and "k7d_daemon_up" not in body
    assert started == [None]
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{servers[0].server_port}/metrics", timeout=1)


async def test_agent_lifespan_serves_agent_registry(tmp_path: Path, monkeypatch):
    servers = _serving(monkeypatch)
    _fake_node(tmp_path, monkeypatch).close()
    with patch("subprocess.run", return_value=_crictl([("k7", "SANDBOX_READY")])):
        async with agent_mod.lifespan(agent_mod.app):
            body = httpx.get(f"http://127.0.0.1:{servers[0].server_port}/metrics").text
    assert 'k7_vm_count{backend="k7d",state="ready"} 1.0' in body
    assert "# TYPE k7d_rpc_duration_seconds histogram" in body and "k7_http_requests_total" not in body


async def test_api_counts_invalid_expired_and_forbidden(tmp_path: Path):
    import hashlib

    from k7.api import main
    from k7.api.keystore import FileKeyStore

    keys = {
        "k7-metrics-test-scoped-0123456789": {"name": "scoped", "role": "user", "namespaces": ["team-a"]},
        "k7-metrics-test-expired-012345678": {"name": "old", "expires": 1},
    }
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({hashlib.sha256(k.encode()).hexdigest(): v for k, v in keys.items()}))
    scoped, expired = keys
    reasons = ("invalid", "expired", "forbidden")
    before = [_sample(metrics.API, "k7_auth_failures_total", {"reason": r}) for r in reasons]
    with patch.object(main, "STORE", FileKeyStore(path)):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/v1/sandboxes", headers={"X-API-Key": "nope"})).status_code == 401
            assert (await client.get("/api/v1/sandboxes", headers={"X-API-Key": expired})).status_code == 401
            r = await client.get("/api/v1/sandboxes", params={"namespace": "team-b"}, headers={"X-API-Key": scoped})
            assert r.status_code == 403
    after = [_sample(metrics.API, "k7_auth_failures_total", {"reason": r}) for r in reasons]
    assert after == [b + 1 for b in before]
    forbidden = {"route": "/api/v1/sandboxes", "method": "GET", "status": "403"}
    assert (
        _sample(metrics.API, "k7_http_request_duration_seconds_count", {"route": "/api/v1/sandboxes", "method": "GET"})
        >= 3
    )
    assert _sample(metrics.API, "k7_http_requests_total", forbidden) >= 1


K7CORE_OPS = {
    "create": "create_sandbox",
    "delete": "delete_sandbox",
    "fork": "fork_sandbox",
    "pause": "pause_sandbox",
    "resume": "resume_sandbox",
    "snapshot": "create_snapshot",
    "restore": "restore_sandbox",
}


def test_every_lifecycle_op_is_observed():
    from k7.core.core import K7Core

    # rollback and export have no K7Core method yet; their label values are reserved.
    assert set(metrics.OPS) - set(K7CORE_OPS) == {"rollback", "export"}
    for op, name in K7CORE_OPS.items():
        fn = getattr(K7Core, name)
        assert op in [c.cell_contents for c in fn.__closure__ or ()], f"{name} is not @observed({op!r})"


async def test_delete_labels_backend_from_the_annotation():
    from kubernetes_asyncio.client.exceptions import ApiException

    from k7.core.core import K7Core

    dep = SimpleNamespace(metadata=SimpleNamespace(annotations={"k7.katakate.org/backend": "k7"}))
    apps = MagicMock(read_namespaced_deployment=AsyncMock(side_effect=[dep, ApiException(status=404)]))
    core = K7Core()
    core._get_apps_v1_client = AsyncMock(return_value=apps)
    core._delete_sandbox_resources = AsyncMock(return_value=OperationResult(success=True))
    k7d, unknown = ({"op": "delete", "backend": b, "outcome": "success"} for b in ("k7d", "unknown"))
    before = [_sample(metrics.API, "k7_operation_duration_seconds_count", lbl) for lbl in (k7d, unknown)]
    for name in ("sb", "gone"):
        k7_logging.new_context()
        assert (await core.delete_sandbox(name, "team-a")).success
    after = [_sample(metrics.API, "k7_operation_duration_seconds_count", lbl) for lbl in (k7d, unknown)]
    assert after == [before[0] + 1, before[1] + 1]
    assert core._delete_sandbox_resources.await_count == 2
