"""Per-node k7 agent.

Runs as a DaemonSet (``k7-agent``, kube-system) on every node, reusing the
``k7-api`` image with an overridden command
(``uvicorn k7.api.agent:app``). It exposes ONLY node-local operations
that need the k7d socket / crictl — never Kubernetes writes:

- ``POST /agent/v1/vm/{pause,resume,lookup}`` — crictl + k7d daemon.
- ``GET /agent/v1/storage`` — kfd thin-pool (from the host timer's
  ``/run/k7/storage.json``) / k7d disks utilization, and the usage spool.
- Usage metering: the node sampler loop, the hostPath spool
  ``/var/lib/k7/usage-spool`` and its sinks. ``POST /agent/v1/usage/events``
  takes k7-api's storage events; ``GET /agent/v1/usage/status`` reports the
  spool for ``requireDelivery``.

Unprivileged: no ``/dev``, all capabilities dropped. It keeps uid 0 only
because the containerd and k7d sockets are root-owned (owner access
needs no capability). ``lvs`` runs on the host (``k7-storage-report.timer``).

Fork, pause annotations, and NetworkPolicy stay on ``k7-api`` (ClusterRole).
The agent ServiceAccount has no token (``automountServiceAccountToken:
false``), so root on an agent cannot steal cluster-admin-equivalent RBAC.

Auth: ``X-K7-Agent-Token`` from ``/etc/k7/agent_token`` (Secret ``k7-agent-token``). A CiliumNetworkPolicy
allows ingress from the k7-api pod and the local ``host`` (kubelet probes).
``remote-node`` is denied so a compromised agent cannot call other agents.
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from prometheus_client.core import GaugeMetricFamily

from .. import __version__, metrics
from .. import logging as k7_logging
from ..core.core import K7Core
from ..core.models import OperationResult
from ..metering import config as metering_config
from ..metering import pauses
from ..metering.node import NodeMetering

k7_logging.configure_from_env()
log = logging.getLogger("k7.agent")

AGENT_TOKEN_FILE = os.getenv("K7_AGENT_TOKEN_FILE", "/etc/k7/agent_token")
KATA_VG = os.getenv("K7_KATA_VG", "kata-vg")
STORAGE_REPORT = os.getenv("K7_STORAGE_REPORT", "/run/k7/storage.json")
STORAGE_REPORT_MAX_AGE_S = 90
K7D_DISKS_DIR = os.getenv("K7D_DISKS_DIR", "/var/lib/k7d/disks")
# RuntimeClass handler (roles/k7_cluster/tasks/nodes.yml) → k7 backend.
VM_BACKENDS = {"kata": "kata-firecracker-devmapper", "kata-qemu": "kata-qemu-longhorn", "k7": "k7d", "k7-fc": "k7d-fc"}


def _thin_pool(report: dict) -> dict:
    lvs = report["report"][0]["lv"]
    pools = [lv for lv in lvs if lv["lv_attr"].startswith("t")]
    if not pools:
        raise RuntimeError(f"no thin pool LV found in VG {KATA_VG} (lvs returned {lvs})")
    return pools[0]


class NodeCollector:
    """Per-node gauges, read at scrape time. A failing source is logged and
    left out of the scrape; it never fails the other gauges."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        for name, source in (("storage", self._storage), ("k7d", self._k7d), ("vm", self._vms)):
            try:
                yield from source()
            except Exception:
                log.exception("collecting %s metrics failed", name)

    def _storage(self) -> Iterator[GaugeMetricFamily]:
        pool = GaugeMetricFamily("k7_storage_pool_bytes", "Storage pool size and use", labels=["pool", "kind"])
        if os.path.exists(STORAGE_REPORT):
            age = time.time() - os.stat(STORAGE_REPORT).st_mtime
            yield GaugeMetricFamily("k7_storage_report_age_seconds", "Age of /run/k7/storage.json", value=age)
            with open(STORAGE_REPORT) as f:
                lv = _thin_pool(json.load(f))
            for name, size, pct in (
                ("thinpool_data", lv["lv_size"], lv["data_percent"]),
                ("thinpool_metadata", lv["lv_metadata_size"], lv["metadata_percent"]),
            ):
                pool.add_metric([name, "size"], float(size))
                pool.add_metric([name, "used"], float(size) * float(pct) / 100)
        if os.path.isdir(K7D_DISKS_DIR):
            st = os.statvfs(K7D_DISKS_DIR)
            pool.add_metric(["k7d_disks", "size"], st.f_blocks * st.f_frsize)
            pool.add_metric(["k7d_disks", "used"], (st.f_blocks - st.f_bfree) * st.f_frsize)
        yield pool

    def _k7d(self) -> Iterator[GaugeMetricFamily]:
        if not os.path.isdir(K7D_DISKS_DIR):
            return
        up = 1
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            try:
                sock.connect(K7Core._k7d_socket_path())
            except OSError as e:
                log.warning("k7d control socket unreachable: %s", e)
                up = 0
        yield GaugeMetricFamily("k7d_daemon_up", "1 if the k7d control socket accepts connections", value=up)

    def _vms(self) -> Iterator[GaugeMetricFamily]:
        crictl = shutil.which("crictl") or "/usr/local/bin/crictl"
        endpoint = f"unix://{os.environ['K7_CONTAINERD_SOCKET']}"
        out = subprocess.run(
            [crictl, "--runtime-endpoint", endpoint, "pods", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        counts = Counter(
            (VM_BACKENDS[p["runtimeHandler"]], "ready" if p["state"] == "SANDBOX_READY" else "notready")
            for p in json.loads(out)["items"]
            if p.get("runtimeHandler") in VM_BACKENDS
        )
        family = GaugeMetricFamily("k7_vm_count", "Sandbox VMs on this node", labels=["backend", "state"])
        for backend in VM_BACKENDS.values():
            for state in ("ready", "notready"):
                family.add_metric([backend, state], counts[(backend, state)])
        yield family


metrics.AGENT.register(NodeCollector())
METERING: NodeMetering | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global METERING
    node = os.environ.get("K7_NODE_NAME")
    if not node:
        raise RuntimeError("K7_NODE_NAME is not set (the k7-agent DaemonSet sets it from spec.nodeName)")
    METERING = NodeMetering(metering_config.load(), metering_config.cluster_id(), node, metering_config.spool_dir())
    server = metrics.serve(metrics.AGENT)
    task = asyncio.create_task(METERING.run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if server:
            server.shutdown()
            server.server_close()


def _metering() -> NodeMetering:
    if METERING is None:
        raise HTTPException(status_code=503, detail="usage metering has not started on this agent")
    return METERING


app = FastAPI(title="K7 Node Agent", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def request_log(request: Request, call_next):
    """Logs each request under the caller's X-Request-ID (k7-api forwards its own)."""
    k7_logging.start_request(request.headers.get(k7_logging.REQUEST_ID_HEADER))
    start = time.monotonic()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", None) or "<unmatched>"
        k7_logging.access(log, request.method, route, status_code, time.monotonic() - start)


def _load_agent_token() -> str:
    """Read the agent token — unreadable/empty is a deployment bug
    and must fail loudly (a silent 401 would be misdiagnosed as a bad
    caller token)."""
    try:
        with open(AGENT_TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"agent token {AGENT_TOKEN_FILE} is unreadable ({e}) — the install playbook "
                "provisions it on every node; re-run `k7 install`"
            ),
        )
    if not token:
        raise HTTPException(status_code=500, detail=f"agent token {AGENT_TOKEN_FILE} is empty — re-run `k7 install`")
    return token


async def verify_agent_token(x_k7_agent_token: str | None = Header(None)):
    if not x_k7_agent_token or not secrets.compare_digest(x_k7_agent_token.strip(), _load_agent_token()):
        raise HTTPException(status_code=401, detail="Invalid or missing agent token")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):  # type: ignore[override]
    return JSONResponse(
        content={"error": {"code": "InternalServerError", "message": str(exc)}},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


@app.get("/health")
async def health():
    """Always 200 (probes must not restart the agent over metering); ``status`` turns ``degraded``."""
    usage = METERING.health() if METERING else None
    status_text = usage["status"] if usage else "healthy"
    return {"status": status_text, "node": os.environ.get("K7_NODE_NAME", ""), "usage": usage}


def _required_name(body: dict | None) -> tuple[str, str]:
    body = body or {}
    name = body.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="name is required")
    namespace = body.get("namespace", "default")
    k7_logging.bind(sandbox=name, namespace=namespace)
    return name, namespace


def _required_pod_name(body: dict | None) -> str:
    pod_name = (body or {}).get("pod_name")
    if not pod_name or not isinstance(pod_name, str):
        raise HTTPException(status_code=400, detail="pod_name is required")
    return pod_name


async def _lookup_local_vm(pod_name: str, namespace: str) -> dict:
    """crictl + k7d only — no Kubernetes client."""
    core = K7Core()
    cri_sandbox_id = await core._k7d_cri_sandbox_id(pod_name, namespace)
    resp = await core._k7d_request({"op": "lookup_sandbox", "sandbox_id": cri_sandbox_id})
    if resp.get("status") != "sandbox_found":
        raise RuntimeError(f"unexpected k7d lookup_sandbox response: {resp}")
    resp["sandbox_id"] = cri_sandbox_id
    return resp


@app.post("/agent/v1/vm/pause", dependencies=[Depends(verify_agent_token)])
async def vm_pause(body: dict | None = None):
    _name, namespace = _required_name(body)
    pod_name = _required_pod_name(body)
    vm = await _lookup_local_vm(pod_name, namespace)
    await K7Core()._k7d_request({"op": "pause_vm", "vm_id": vm["vm_id"]})
    await asyncio.to_thread(pauses.record, vm["sandbox_id"], True)
    return OperationResult(
        success=True,
        message=f"k7d VM {vm['vm_id']} frozen in place; memory retained, vCPUs stopped.",
    ).to_dict()


@app.post("/agent/v1/vm/resume", dependencies=[Depends(verify_agent_token)])
async def vm_resume(body: dict | None = None):
    _name, namespace = _required_name(body)
    pod_name = _required_pod_name(body)
    vm = await _lookup_local_vm(pod_name, namespace)
    await K7Core()._k7d_request({"op": "resume_vm", "vm_id": vm["vm_id"]})
    await asyncio.to_thread(pauses.record, vm["sandbox_id"], False)
    return OperationResult(
        success=True,
        message=f"k7d VM {vm['vm_id']} vCPUs restarted.",
    ).to_dict()


@app.post("/agent/v1/vm/fork", dependencies=[Depends(verify_agent_token)])
async def vm_fork(_body: dict | None = None):
    raise HTTPException(
        status_code=400,
        detail=(
            "k7d fork is a control-plane operation (k7-api ClusterRole creates the "
            "Deployment). The agent only runs lookup/pause/resume/storage so a "
            "compromised worker cannot create sandboxes on other nodes."
        ),
    )


@app.post("/agent/v1/vm/lookup", dependencies=[Depends(verify_agent_token)])
async def vm_lookup(body: dict | None = None):
    _name, namespace = _required_name(body)
    pod_name = _required_pod_name(body)
    return await _lookup_local_vm(pod_name, namespace)


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"{' '.join(cmd)} failed on node {os.environ.get('K7_NODE_NAME', '')}: {result.stderr.strip()}",
        )
    return result.stdout


def _kata_thinpool() -> dict:
    """kfd thin-pool utilization from the host timer's ``lvs`` JSON. Missing
    or older than 90 s is a loud 500 — never a fallback to running ``lvs``."""
    node = os.environ.get("K7_NODE_NAME", "")
    try:
        age = time.time() - os.stat(STORAGE_REPORT).st_mtime
        with open(STORAGE_REPORT) as f:
            report = json.load(f)
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=f"{STORAGE_REPORT} missing on node {node} — is the kfd backend installed "
            "(k7-storage-report.timer writes it)?",
        )
    if age > STORAGE_REPORT_MAX_AGE_S:
        raise HTTPException(
            status_code=500,
            detail=f"{STORAGE_REPORT} on node {node} is {int(age)}s old (max {STORAGE_REPORT_MAX_AGE_S}s) — "
            "check `systemctl status k7-storage-report.timer`",
        )
    try:
        lv = _thin_pool(report)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "vg": KATA_VG,
        "lv": lv["lv_name"],
        "size_bytes": int(float(lv["lv_size"])),
        "data_percent": float(lv["data_percent"]),
        "metadata_percent": float(lv["metadata_percent"]),
    }


def _k7d_disks() -> dict:
    """k7d disks-pool utilization via ``df`` on the hostPath-mounted
    /var/lib/k7d/disks XFS loopback mount."""
    if not os.path.isdir(K7D_DISKS_DIR):
        raise HTTPException(status_code=500, detail=f"{K7D_DISKS_DIR} not found — is the k7d backend installed?")
    out = _run(["df", "--output=size,used,avail", "--block-size=1", K7D_DISKS_DIR])
    size, used, avail = out.splitlines()[1].split()
    return {
        "path": K7D_DISKS_DIR,
        "size_bytes": int(size),
        "used_bytes": int(used),
        "avail_bytes": int(avail),
        "used_percent": round(100.0 * int(used) / int(size), 2) if int(size) else 0.0,
    }


@app.get("/agent/v1/storage", dependencies=[Depends(verify_agent_token)])
async def storage():
    return {"kata_thinpool": _kata_thinpool(), "k7d_disks": _k7d_disks(), "usage_spool": _metering().spool.status()}


@app.post("/agent/v1/usage/events", dependencies=[Depends(verify_agent_token)])
async def usage_events(body: dict | None = None):
    events = (body or {}).get("events")
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=400, detail="events must be a non-empty list of CloudEvents")
    try:
        await asyncio.to_thread(_metering().ingest_storage, events)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"accepted": len(events)}


@app.get("/agent/v1/usage/status", dependencies=[Depends(verify_agent_token)])
async def usage_status():
    return _metering().health()
