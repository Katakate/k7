"""``POST /api/v1/install``: prepare existing cluster nodes through the k7-node-installer.

The API never runs Ansible and never creates workloads. The installer stack (DaemonSet,
RBAC, admission policies, config) and its pinned image are deployed at install time
(``k7 install --api-remote-install`` or the chart's ``api.remoteInstall``), which also sets
``K7_NODE_INSTALLER_IMAGE``. A request checks that the live DaemonSet runs exactly that
image, records an operation, and labels the nodes into the installer's pool; status is
computed from the cluster each time it is polled. Turning it off converges: the playbook
deletes its stack and pool labels, the chart's install hook calls ``converge_off``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from .. import __version__
from ..deploy.images import DIGEST_REF, LOCAL_NODE_INSTALLER_IMAGE_REF
from ..deploy.node_installer import entrypoint as installer

ENABLE_ENV = "K7_API_ENABLE_REMOTE_INSTALL"
IMAGE_ENV = "K7_NODE_INSTALLER_IMAGE"
NAMESPACE_ENV = "K7_NODE_INSTALLER_NAMESPACE"
DAEMONSET = "k7-node-installer"
CONTAINER = "installer"
POD_SELECTOR = "app=k7-node-installer"
POOL_LABEL = f"{installer.LABEL}/node-pool"
# Marks the nodes this endpoint added to the pool; the chart's off path removes only this label.
ADDED_BY_LABEL = f"{installer.LABEL}/node-pool-added-by"
ADDED_BY = "remote-install"
RECORD_PREFIX = "k7-install-"
RECORD_LABELS = {"app.kubernetes.io/component": "k7-remote-install"}
RECORD_SELECTOR = ",".join(f"{k}={v}" for k, v in RECORD_LABELS.items())
RECORD_TTL = timedelta(days=30)
OPERATION_RE = re.compile(r"^[0-9a-f]{16}$")
NODE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
MAX_NODES = 100
# Waiting reasons that will not resolve without someone fixing something.
FAILED_WAITING = {
    "CrashLoopBackOff",
    "ErrImagePull",
    "ImagePullBackOff",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
}


class RemoteInstallError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1"


def parse_nodes(body: Any) -> list[str]:
    """The body is exactly ``{"nodes": [...]}``: no playbook, inventory, image or config."""
    if not isinstance(body, dict) or set(body) != {"nodes"}:
        extra = sorted(set(body) - {"nodes"}) if isinstance(body, dict) else []
        raise RemoteInstallError(
            400,
            f'body must be exactly {{"nodes": [<node name>, ...]}}; not accepted: {extra or "missing nodes"}. '
            "The playbook, installer image and installer config are fixed at deploy time.",
        )
    nodes = body["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise RemoteInstallError(400, f"nodes must be a list of 1 to {MAX_NODES} node names")
    bad = [n for n in nodes if not isinstance(n, str) or not NODE_NAME_RE.match(n)]
    if bad or len(set(nodes)) != len(nodes):
        raise RemoteInstallError(400, f"nodes must be unique Kubernetes node names; invalid: {bad or 'duplicates'}")
    return nodes


def pinned_image() -> str:
    image = os.environ.get(IMAGE_ENV, "")
    if not (DIGEST_REF.match(image) or image == LOCAL_NODE_INSTALLER_IMAGE_REF):
        raise RemoteInstallError(
            500,
            f"{IMAGE_ENV}={image!r} is neither name@sha256:<digest> nor {LOCAL_NODE_INSTALLER_IMAGE_REF}; "
            "redeploy with `k7 install --api-remote-install` (or the chart's api.remoteInstall)",
        )
    return image


def namespace() -> str:
    ns = os.environ.get(NAMESPACE_ENV, "")
    if not ns:
        raise RemoteInstallError(500, f"{NAMESPACE_ENV} is unset; redeploy with remote install enabled")
    return ns


async def check_daemonset(apps: client.AppsV1Api, ns: str, image: str) -> None:
    """The live DaemonSet must run the deploy-time image and select the pool label."""
    try:
        ds = await apps.read_namespaced_daemon_set(DAEMONSET, ns)
    except ApiException as e:
        if e.status == 404:
            raise RemoteInstallError(500, f"DaemonSet {ns}/{DAEMONSET} not found; remote install drives it")
        raise
    spec = ds.spec.template.spec
    images = [c.image for c in spec.containers if c.name == CONTAINER]
    if images != [image]:
        raise RemoteInstallError(500, f"DaemonSet {ns}/{DAEMONSET} runs {images}, not the deploy-time {image}")
    if (spec.node_selector or {}).get(POOL_LABEL) != "true":
        raise RemoteInstallError(500, f"DaemonSet {ns}/{DAEMONSET} does not select {POOL_LABEL}=true")


async def installer_config(v1: client.CoreV1Api, ns: str) -> dict[str, Any]:
    try:
        cm = await v1.read_namespaced_config_map(DAEMONSET, ns)
    except ApiException as e:
        if e.status == 404:
            raise RemoteInstallError(500, f"ConfigMap {ns}/{DAEMONSET} not found; the installer has no config")
        raise
    try:
        return installer.validate_config(json.loads((cm.data or {}).get("config.json", "")))
    except (ValueError, installer.InstallerError) as e:
        raise RemoteInstallError(500, f"ConfigMap {ns}/{DAEMONSET} config.json is invalid: {e}")


async def read_nodes(v1: client.CoreV1Api, names: list[str]) -> list[client.V1Node]:
    found, missing = [], []
    for name in names:
        try:
            found.append(await v1.read_node(name))
        except ApiException as e:
            if e.status != 404:
                raise
            missing.append(name)
    if missing:
        raise RemoteInstallError(404, f"node(s) not found: {', '.join(missing)}")
    return found


def refuse_servers(cfg: dict[str, Any], nodes: list[client.V1Node]) -> None:
    """Fail now where the installer would refuse (entrypoint.refuse_server_restart)."""
    if cfg["distro"] not in installer.KUBELET_IS_CONTAINERD or cfg.get("allowServerRestart", False):
        return
    servers = [n.metadata.name for n in nodes if installer.is_server({"metadata": {"labels": n.metadata.labels or {}}})]
    if servers:
        raise RemoteInstallError(
            409,
            f"{', '.join(servers)}: {cfg['distro']} server node(s); the installer restarts containerd, which "
            "restarts the apiserver there, and its config has no allowServerRestart. `k7 install` owns servers.",
        )


async def prune_records(v1: client.CoreV1Api, ns: str, now: datetime) -> None:
    for cm in (await v1.list_namespaced_config_map(ns, label_selector=RECORD_SELECTOR)).items:
        if cm.metadata.creation_timestamp < now - RECORD_TTL:
            await v1.delete_namespaced_config_map(cm.metadata.name, ns)


async def start(v1: client.CoreV1Api, apps: client.AppsV1Api, op: str, nodes: list[str], subject: str) -> dict:
    image, ns = pinned_image(), namespace()
    await check_daemonset(apps, ns, image)
    cfg = await installer_config(v1, ns)
    found = await read_nodes(v1, nodes)
    refuse_servers(cfg, found)
    await prune_records(v1, ns, datetime.now(timezone.utc))
    record = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=RECORD_PREFIX + op, labels=RECORD_LABELS),
        data={"nodes": json.dumps(nodes), "image": image, "subject": subject},
    )
    await v1.create_namespaced_config_map(ns, record)
    # A node already in the pool stays unmarked: the marker means "added by remote install".
    for node in found:
        if (node.metadata.labels or {}).get(POOL_LABEL) == "true":
            continue
        name = node.metadata.name
        try:
            await v1.patch_node(name, {"metadata": {"labels": {POOL_LABEL: "true", ADDED_BY_LABEL: ADDED_BY}}})
        except ApiException as e:
            raise RemoteInstallError(500, f"operation {op}: labelling {name} {POOL_LABEL}=true failed: {e.reason}")
    return {"operation": op, "nodes": nodes, "status": f"/api/v1/install/{op}"}


async def converge_off(v1: client.CoreV1Api, ns: str) -> list[str]:
    """Remote install is off on the chart: unmark the nodes this endpoint added, drop its records.

    They stay in the pool: on the chart the installer is core and ``k7-agent`` (pause, resume,
    storage) selects the same label, so unpooling would strand their sandboxes."""
    marked = [n.metadata.name for n in (await v1.list_node(label_selector=f"{ADDED_BY_LABEL}={ADDED_BY}")).items]
    for name in marked:
        await v1.patch_node(name, {"metadata": {"labels": {ADDED_BY_LABEL: None}}})
    for cm in (await v1.list_namespaced_config_map(ns, label_selector=RECORD_SELECTOR)).items:
        await v1.delete_namespaced_config_map(cm.metadata.name, ns)
    return marked


async def failure_line(v1: client.CoreV1Api, ns: str, pod: str, previous: bool) -> str | None:
    """The installer's own ``FAILED:``/``install failed:`` line, if its log has one."""
    try:
        log = await v1.read_namespaced_pod_log(pod, ns, container=CONTAINER, previous=previous, tail_lines=200)
    except ApiException:
        return None
    lines = [ln for ln in log.splitlines() if "k7-node-installer: FAILED:" in ln or "install failed:" in ln]
    return lines[-1].strip() if lines else None


async def node_state(v1: client.CoreV1Api, ns: str, name: str) -> dict[str, Any]:
    try:
        node = await v1.read_node(name)
    except ApiException as e:
        if e.status != 404:
            raise
        return {"node": name, "state": "failed", "error": f"node {name} no longer exists"}
    labels = node.metadata.labels or {}
    if labels.get(POOL_LABEL) != "true":
        return {"node": name, "state": "failed", "error": f"{POOL_LABEL} was removed; the installer left {name}"}
    pods = (await v1.list_namespaced_pod(ns, label_selector=POD_SELECTOR, field_selector=f"spec.nodeName={name}")).items
    if not pods:
        return {"node": name, "state": "pending"}
    pod = max(pods, key=lambda p: p.metadata.creation_timestamp)
    cs = next((c for c in (pod.status.container_statuses or []) if c.name == CONTAINER), None)
    if cs is None:
        return {"node": name, "state": "pending", "pod": pod.metadata.name}
    out = {"node": name, "pod": pod.metadata.name, "restarts": cs.restart_count}
    if cs.ready:
        ready = labels.get(installer.READY_LABEL)
        if ready == __version__:
            return {**out, "state": "succeeded"}
        return {**out, "state": "failed", "error": f"{installer.READY_LABEL}={ready!r}, expected {__version__!r}"}
    waiting = cs.state.waiting if cs.state else None
    terminated = cs.state.terminated if cs.state else None
    if cs.restart_count or (waiting and waiting.reason in FAILED_WAITING) or (terminated and terminated.exit_code):
        why = await failure_line(v1, ns, pod.metadata.name, previous=bool(cs.restart_count))
        reason = f"{waiting.reason}: {waiting.message or ''}".strip() if waiting else "installer exited non-zero"
        return {**out, "state": "failed", "error": why or reason}
    return {**out, "state": "running" if cs.state and cs.state.running else "pending"}


async def status(v1: client.CoreV1Api, op: str) -> dict:
    ns = namespace()
    if not OPERATION_RE.match(op):
        raise RemoteInstallError(404, f"operation {op!r} not found")
    try:
        record = await v1.read_namespaced_config_map(RECORD_PREFIX + op, ns)
    except ApiException as e:
        if e.status == 404:
            raise RemoteInstallError(404, f"operation {op!r} not found")
        raise
    states = [await node_state(v1, ns, n) for n in json.loads(record.data["nodes"])]
    overall = (
        "failed"
        if any(s["state"] == "failed" for s in states)
        else "succeeded"
        if all(s["state"] == "succeeded" for s in states)
        else "running"
    )
    return {
        "operation": op,
        "state": overall,
        "nodes": states,
        "image": record.data["image"],
        "created_at": record.metadata.creation_timestamp.isoformat(),
    }
