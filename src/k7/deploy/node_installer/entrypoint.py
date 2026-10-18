"""k7-node-installer entrypoint: install | cleanup | bake.

Stdlib only: the image carries ansible-core and this file, not the k7 package deps.

install  run k7_node_prep on this pod's node, then label it node-ready (last);
         any failure removes the readiness labels and exits non-zero.
cleanup  refuse while K7 sandboxes run on the node (unless --force), remove the
         labels, then undo node prep (k7-node-cleanup.yaml).
bake     Packer image build: node prep minus first-boot disk state, no API calls.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from k7 import __version__

LABEL = "k7.katakate.org"
READY_LABEL = f"{LABEL}/node-ready"
VIRT_LABEL = f"{LABEL}/virt"
BACKENDS = {
    "kfd": "kata-firecracker-devmapper",
    "kql": "kata-qemu-longhorn",
    "k7d": "k7d",
    "k7d-fc": "k7d-fc",
}
BACKEND_LABELS = [f"{LABEL}/backend-{b}" for b in BACKENDS.values()]
SANDBOX_RUNTIME_CLASSES = {"kata", "kata-qemu", "k7", "k7-fc"}
DISTROS = ("k3s", "rke2", "kubeadm", "eks-al2023")
# containerd is the distro's whole kubelet unit here: restarting it on a server
# restarts the apiserver.
KUBELET_IS_CONTAINERD = ("k3s", "rke2")
CONFIG_KEYS = {
    "distro",
    "backends",
    "diskSource",
    "k7dDisksImageSize",
    "kataThinpoolPvSize",
    "allowServerRestart",
    "wipeDisks",
}
SIZE_RE = re.compile(r"^[0-9]+[KMGT]$")
LABEL_PATCH_DEADLINE_S = 300

HOST_ROOT = Path("/host")
STATE_FILE = HOST_ROOT / "var/lib/k7/installer-state.json"
INSTALLER_DIR = Path("/opt/k7")
DEPLOY_DIR = INSTALLER_DIR / "src/k7/deploy"
ARTIFACT_DIR = Path("/opt/k7-artifacts")
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
READY_FILE = Path("/tmp/k7-node-installer-ready")  # readinessProbe
CONFIG_POLL_S = 30


class InstallerError(RuntimeError):
    pass


# ── config ──────────────────────────────────────────────────────────


def validate_config(cfg: Any) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise InstallerError("installer config must be a JSON object")
    unknown = set(cfg) - CONFIG_KEYS
    if unknown:
        raise InstallerError(f"unknown installer config key(s) {sorted(unknown)}; allowed: {sorted(CONFIG_KEYS)}")
    if cfg.get("distro") not in DISTROS:
        raise InstallerError(f"distro={cfg.get('distro')!r}; allowed: {list(DISTROS)}")
    backends = cfg.get("backends")
    if not isinstance(backends, list) or not backends:
        raise InstallerError(f"backends must be a non-empty list of {list(BACKENDS)}")
    bad = [b for b in backends if b not in BACKENDS]
    if bad or len(set(backends)) != len(backends):
        raise InstallerError(f"backends={backends}: unknown or duplicate; allowed: {list(BACKENDS)}")
    disk = cfg.get("diskSource")
    if not (disk in ("spare-disk", "loopback") or (isinstance(disk, str) and re.match(r"^device:/dev/\S+$", disk))):
        raise InstallerError(f"diskSource={disk!r}; allowed: spare-disk, loopback, device:/dev/<disk>")
    if disk == "loopback" and "kfd" in backends:
        raise InstallerError("diskSource=loopback provisions no LVM thin pool, and kfd needs one")
    for key in ("k7dDisksImageSize", "kataThinpoolPvSize"):
        if key in cfg and not (isinstance(cfg[key], str) and SIZE_RE.match(cfg[key])):
            raise InstallerError(f"{key}={cfg[key]!r}; expected e.g. 64G")
    for key in ("allowServerRestart", "wipeDisks"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise InstallerError(f"{key} must be true or false")
    return cfg


def config_sha(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def extra_vars(cfg: dict[str, Any]) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "k7_distro": cfg["distro"],
        "k7_backends": ",".join(cfg["backends"]),
        "k7_disk_source": cfg["diskSource"],
        "k7_node_prep_only": True,
        "k7_node_installer": True,
        "k7_artifact_dir": str(ARTIFACT_DIR),
        "k7_host_root": str(HOST_ROOT),
        "k7_cleanup_wipe_disks": cfg.get("wipeDisks", False),
    }
    if "k7dDisksImageSize" in cfg:
        ev["k7d_disks_image_size"] = cfg["k7dDisksImageSize"]
    if "kataThinpoolPvSize" in cfg:
        ev["kata_thinpool_pv_size"] = cfg["kataThinpoolPvSize"]
    return ev


# ── labels ──────────────────────────────────────────────────────────


def unready_labels() -> dict[str, str | None]:
    return dict.fromkeys([READY_LABEL, *BACKEND_LABELS])


def ready_labels(cfg: dict[str, Any], nested: bool) -> dict[str, str | None]:
    labels: dict[str, str | None] = dict.fromkeys(BACKEND_LABELS)
    for b in cfg["backends"]:
        labels[f"{LABEL}/backend-{BACKENDS[b]}"] = "true"
    if "k7d-fc" in cfg["backends"]:
        labels[f"{LABEL}/backend-k7d"] = "true"  # same daemon; matches roles/k7_cluster nodes.yml
    labels[VIRT_LABEL] = "nested" if nested else None
    labels[READY_LABEL] = __version__
    return labels


def is_nested(cpuinfo: str) -> bool:
    """The node is itself a guest (heuristic; label only, never a gate)."""
    return any(line.startswith("flags") and " hypervisor" in line for line in cpuinfo.splitlines())


# ── Kubernetes API (in-cluster, stdlib) ─────────────────────────────


class Kube:
    def __init__(self) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT")
        if not host or not port:
            raise InstallerError("KUBERNETES_SERVICE_HOST/PORT unset: install and cleanup run in a pod")
        self.base = f"https://{host}:{port}"
        self.ctx = ssl.create_default_context(cafile=str(SA_DIR / "ca.crt"))

    def request(self, method: str, path: str, body: Any = None, content_type: str = "application/json") -> Any:
        token = (SA_DIR / "token").read_text().strip()  # re-read: projected tokens rotate
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
        with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")


def retry(
    fn: Callable[[], Any],
    what: str,
    deadline_s: float = LABEL_PATCH_DEADLINE_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Retry across an apiserver restart (K3s/RKE2 containerd restart), bounded."""
    start, delay = clock(), 1.0
    while True:
        try:
            return fn()
        except (urllib.error.URLError, OSError) as e:
            # 401/403 too: a restarting apiserver serves before its authn/RBAC caches sync.
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code not in (401, 403, 429):
                raise InstallerError(f"{what}: HTTP {e.code} {e.read().decode(errors='replace')}")
            err = (
                f"HTTP {e.code} {e.read().decode(errors='replace')[:300]}"
                if isinstance(e, urllib.error.HTTPError)
                else e
            )
            if clock() - start + delay > deadline_s:
                raise InstallerError(f"{what}: API unreachable for {deadline_s:.0f}s: {err}")
            print(f"k7-node-installer: {what} failed ({err}); retrying in {delay:.0f}s", flush=True)
            sleep(delay)
            delay = min(delay * 2, 30.0)


def patch_labels(kube: Kube, node: str, labels: dict[str, str | None]) -> None:
    body = {"metadata": {"labels": labels}}
    retry(
        lambda: kube.request("PATCH", f"/api/v1/nodes/{node}", body, "application/merge-patch+json"),
        f"label node {node}",
    )


def node_event(kube: Kube, node: str, reason: str, message: str, warning: bool) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "metadata": {"generateName": f"{node}.k7-node-installer."},
        # kubectl describe node matches node events by uid == node name, like the kubelet's.
        "involvedObject": {"kind": "Node", "name": node, "uid": node, "apiVersion": "v1"},
        "reason": reason,
        "message": message[:1024],
        "type": "Warning" if warning else "Normal",
        "source": {"component": "k7-node-installer", "host": node},
        "firstTimestamp": now,
        "lastTimestamp": now,
        "count": 1,
    }
    try:
        retry(lambda: kube.request("POST", "/api/v1/namespaces/default/events", event), "post node event", 60)
    except InstallerError as e:
        print(f"k7-node-installer: {e}", file=sys.stderr, flush=True)  # the pod log already has the cause


def is_server(node_obj: dict[str, Any]) -> bool:
    labels = node_obj.get("metadata", {}).get("labels", {})
    return "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels


def sandbox_pods(kube: Kube, node: str) -> list[str]:
    pods = retry(
        lambda: kube.request("GET", f"/api/v1/pods?fieldSelector=spec.nodeName%3D{node}"), f"list pods on {node}"
    )
    return [
        f"{p['metadata']['namespace']}/{p['metadata']['name']}"
        for p in pods.get("items", [])
        if p["spec"].get("runtimeClassName") in SANDBOX_RUNTIME_CLASSES
        and p.get("status", {}).get("phase") not in ("Succeeded", "Failed")
    ]


# ── Ansible ─────────────────────────────────────────────────────────


def inventory(node: str, server: bool) -> str:
    host = f"{node} ansible_connection=k7_nsenter ansible_become=false ansible_python_interpreter=/usr/bin/python3"
    servers, agents = (host, "") if server else ("", host)
    return f"[k7_servers]\n{servers}\n[k7_agents]\n{agents}\n[k7_cluster:children]\nk7_servers\nk7_agents\n"


def run_playbook(playbook: str, node: str, server: bool, ev: dict[str, Any], skip_tags: str = "") -> None:
    """Stream ansible-playbook to the pod log; raise naming the failed task."""
    with tempfile.TemporaryDirectory() as tmp:
        inv, evf = Path(tmp, "inventory.ini"), Path(tmp, "extra-vars.json")
        inv.write_text(inventory(node, server))
        evf.write_text(json.dumps(ev))
        cmd = ["ansible-playbook", "-i", str(inv), "-e", f"@{evf}", str(DEPLOY_DIR / playbook)]
        if skip_tags:
            cmd += ["--skip-tags", skip_tags]
        env = {
            **os.environ,
            "ANSIBLE_CONNECTION_PLUGINS": str(DEPLOY_DIR / "node_installer/connection_plugins"),
            "ANSIBLE_PIPELINING": "True",
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
        }
        print(f"k7-node-installer: {' '.join(cmd)}", flush=True)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        task, failed = "", ""
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            if line.startswith("TASK ["):
                task = line[len("TASK [") :].split("]", 1)[0]
            elif line.startswith(("fatal:", "failed:")) and not failed:
                failed = task
        if proc.wait() != 0:
            raise InstallerError(f"{playbook} failed at task {failed or task or '<before first task>'!r}")


# ── modes ───────────────────────────────────────────────────────────


def read_state() -> dict[str, Any] | None:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None


def write_state(cfg: dict[str, Any], sha: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "version": __version__,
                "config_sha": sha,
                "backends": cfg["backends"],
                "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            }
        )
        + "\n"
    )
    tmp.replace(STATE_FILE)


def refuse_server_restart(cfg: dict[str, Any], node: str, node_obj: dict[str, Any]) -> bool:
    server = is_server(node_obj)
    if server and cfg["distro"] in KUBELET_IS_CONTAINERD and not cfg.get("allowServerRestart", False):
        raise InstallerError(
            f"{node} is a {cfg['distro']} server: restarting its containerd restarts the apiserver. "
            "The playbook (`k7 install`) owns server nodes; set allowServerRestart=true to override."
        )
    return server


def install(cfg: dict[str, Any], kube: Kube, node: str, playbook: Callable[..., None] = run_playbook) -> None:
    sha = config_sha(cfg)
    node_obj = retry(lambda: kube.request("GET", f"/api/v1/nodes/{node}"), f"get node {node}")
    server = refuse_server_restart(cfg, node, node_obj)
    state = read_state()
    same = state is not None and state.get("version") == __version__ and state.get("config_sha") == sha
    print(f"k7-node-installer {__version__}: node={node} config_sha={sha[:12]} state={'same' if same else 'changed'}")
    if not same:
        patch_labels(kube, node, unready_labels())
    try:
        playbook("k7-install-node.yaml", node, server, extra_vars(cfg))
        write_state(cfg, sha)
        nested = is_nested((HOST_ROOT / "proc/cpuinfo").read_text())
        patch_labels(kube, node, ready_labels(cfg, nested))
    except BaseException as e:
        print(f"k7-node-installer: install failed: {e}", file=sys.stderr, flush=True)
        try:
            patch_labels(kube, node, unready_labels())
        finally:
            node_event(kube, node, "K7NodeInstallFailed", str(e), warning=True)
        raise
    node_event(kube, node, "K7NodeReady", f"{READY_LABEL}={__version__} backends={cfg['backends']}", warning=False)


def cleanup(
    cfg: dict[str, Any], kube: Kube, node: str, force: bool, playbook: Callable[..., None] = run_playbook
) -> None:
    node_obj = retry(lambda: kube.request("GET", f"/api/v1/nodes/{node}"), f"get node {node}")
    server = refuse_server_restart(cfg, node, node_obj)
    pods = sandbox_pods(kube, node)
    if pods and not force:
        raise InstallerError(f"refusing cleanup: {len(pods)} K7 sandbox pod(s) on {node}: {', '.join(pods)}")
    patch_labels(kube, node, {**unready_labels(), VIRT_LABEL: None})
    for p in pods:
        ns, name = p.split("/", 1)
        retry(lambda ns=ns, name=name: kube.request("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}"), f"delete {p}")
    deadline = time.monotonic() + LABEL_PATCH_DEADLINE_S
    while pods and sandbox_pods(kube, node):
        if time.monotonic() > deadline:
            raise InstallerError(f"K7 sandbox pods still on {node} after {LABEL_PATCH_DEADLINE_S}s")
        time.sleep(5)
    playbook("k7-node-cleanup.yaml", node, server, extra_vars(cfg))
    STATE_FILE.unlink(missing_ok=True)
    node_event(kube, node, "K7NodeCleaned", f"wipeDisks={cfg.get('wipeDisks', False)}", warning=False)


def bake(cfg: dict[str, Any], playbook: Callable[..., None] = run_playbook) -> None:
    playbook("k7-install-node.yaml", "localhost", False, extra_vars(cfg), skip_tags="first-boot")


def load_config(path: Path) -> dict[str, Any]:
    return validate_config(json.loads(path.read_text()))


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--force"]
    mode = args[0] if args else os.environ.get("K7_INSTALLER_MODE", "")
    if mode not in ("install", "cleanup", "bake"):
        print("usage: entrypoint.py install|cleanup|bake [--force]  (or K7_INSTALLER_MODE)", file=sys.stderr)
        return 2
    config_path = Path(os.environ.get("K7_INSTALLER_CONFIG", "/etc/k7-node-installer/config.json"))
    try:
        cfg = load_config(config_path)
        if mode == "bake":
            bake(cfg)
            return 0
        node = os.environ.get("K7_NODE_NAME", "")
        if not node:
            raise InstallerError("K7_NODE_NAME unset (DaemonSet sets it from spec.nodeName)")
        kube = Kube()
        if mode == "install":
            install(cfg, kube, node)
        else:
            cleanup(cfg, kube, node, force="--force" in argv)
    except InstallerError as e:
        print(f"k7-node-installer: FAILED: {e}", file=sys.stderr, flush=True)
        return 1
    READY_FILE.write_text(f"{mode} {__version__}\n")
    print(f"k7-node-installer: {mode} complete; watching {config_path}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(CONFIG_POLL_S)
        # A ConfigMap volume refreshes in place; re-run so a changed config re-applies and relabels.
        if mode == "install" and config_sha(json.loads(config_path.read_text())) != config_sha(cfg):
            print("k7-node-installer: config changed; re-running install", flush=True)
            READY_FILE.unlink(missing_ok=True)
            os.execv(sys.executable, [sys.executable, "-m", "k7.deploy.node_installer.entrypoint", *argv])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
