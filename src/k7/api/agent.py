"""Per-node k7 agent.

Runs as a DaemonSet (``k7-agent``, kube-system) on every node, reusing the
``k7-api:local`` image with an overridden command
(``uvicorn k7.api.agent:app``). It exposes ONLY node-local operations
that need the k7d socket / crictl / ``lvs`` — never Kubernetes writes:

- ``POST /agent/v1/vm/{pause,resume,lookup}`` — crictl + k7d daemon.
- ``GET /agent/v1/storage`` — kfd thin-pool / k7d disks utilization.

Fork, pause annotations, and NetworkPolicy stay on ``k7-api`` (ClusterRole).
The agent ServiceAccount has no token (``automountServiceAccountToken:
false``), so root on an agent cannot steal cluster-admin-equivalent RBAC.

Auth: ``X-K7-Agent-Token`` from ``/etc/k7/agent_token``. A CiliumNetworkPolicy
allows ingress from the k7-api pod and the local ``host`` (kubelet probes).
``remote-node`` is denied so a compromised agent cannot call other agents.
"""

import json
import os
import secrets
import subprocess

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .. import __version__
from ..core.core import K7Core
from ..core.models import OperationResult

app = FastAPI(title="K7 Node Agent", version=__version__)

AGENT_TOKEN_FILE = os.getenv("K7_AGENT_TOKEN_FILE", "/etc/k7/agent_token")
KATA_VG = os.getenv("K7_KATA_VG", "kata-vg")
K7D_DISKS_DIR = os.getenv("K7D_DISKS_DIR", "/var/lib/k7d/disks")


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
    return {"status": "healthy", "node": os.environ.get("K7_NODE_NAME", "")}


def _required_name(body: dict | None) -> tuple[str, str]:
    body = body or {}
    name = body.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="name is required")
    return name, body.get("namespace", "default")


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
    """kfd thin-pool utilization via ``lvs`` (needs a privileged container
    with /dev hostPath-mounted — the DaemonSet provides both)."""
    out = _run(["lvs", "--reportformat", "json", "--units", "b", "--nosuffix", KATA_VG])
    lvs = json.loads(out)["report"][0]["lv"]
    pools = [lv for lv in lvs if lv["lv_attr"].startswith("t")]
    if not pools:
        raise HTTPException(status_code=500, detail=f"no thin pool LV found in VG {KATA_VG} (lvs returned {lvs})")
    lv = pools[0]
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
    return {"kata_thinpool": _kata_thinpool(), "k7d_disks": _k7d_disks()}
