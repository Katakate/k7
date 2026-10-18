"""Helm chart hooks and ``helm test``, run in the k7-api image.

install         pre-install/pre-upgrade: refuse a cluster that cannot enforce what
                the values ask for, then create the agent-token and key-store
                Secrets when missing (never in values, never rotated by an upgrade)
                and ConfigMap k7-cluster-id (kept by uninstall too).
                With api.remoteInstall off, undo what POST /api/v1/install left: its
                node marker and operation records (remote_install.converge_off). The
                nodes stay in the pool, so k7-agent keeps serving them.
uninstall-stop  pre-delete, before the cleanup DaemonSet: delete k7-node-installer.
uninstall-wait  pre-delete, after it: wait until every node is cleaned, then delete
                the Secrets ``install`` created.
test            ``helm test``: create, exec, fork (where the backend can) and delete
                one sandbox per enabled backend.

Settings come from env (the chart sets them): K7_HOOK_PROVIDER, K7_HOOK_BACKENDS,
K7_KQL_STORAGE_CLASS / K7_KQL_SNAPSHOT_CLASS, K7_HOOK_TIMEOUT_S, K7_SYSTEM_NAMESPACE,
K7_HOOK_REMOTE_INSTALL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import time
import uuid
from collections.abc import Awaitable
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

from k7 import logging as k7_logging
from k7.api import remote_install
from k7.core import cluster_config
from k7.deploy.k7_api_keys import KEYS_SECRET, USAGE_SECRET

log = logging.getLogger("k7.hooks")

AGENT_TOKEN_SECRET = "k7-agent-token"
CLUSTER_ID_CM = "k7-cluster-id"
HOOK_SECRETS = (AGENT_TOKEN_SECRET, KEYS_SECRET, USAGE_SECRET)
INSTALLER_DS = "k7-node-installer"
CLEANUP_DS = "k7-node-cleanup"
READY_LABEL = "k7.katakate.org/node-ready"
POOL_SELECTOR = "k7.katakate.org/node-pool=true"
CILIUM_CRDS = ("ciliumnetworkpolicies.cilium.io", "ciliumclusterwidenetworkpolicies.cilium.io")
SNAPSHOT_CRDS = (
    "volumesnapshots.snapshot.storage.k8s.io",
    "volumesnapshotcontents.snapshot.storage.k8s.io",
    "volumesnapshotclasses.snapshot.storage.k8s.io",
)
# DaemonSet label selector → v1 NetworkPolicy engine. aws-node counts only
# with its network policy agent enabled (_aws_policy_agent).
POLICY_ENGINES = {
    "k8s-app=cilium": "Cilium",
    "k8s-app=calico-node": "Calico",
    "k8s-app=canal": "Canal",
    "component=antrea-agent": "Antrea",
    "k8s-app=kube-router": "kube-router",
}
FORKABLE = {"kql", "k7d", "k7d-fc"}
CANONICAL = {"kfd": "kata-firecracker-devmapper", "kql": "kata-qemu-longhorn", "k7d": "k7d", "k7d-fc": "k7d-fc"}
TEST_IMAGE = "alpine:3.20"


class HookError(RuntimeError):
    pass


def _env(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise HookError(f"{key} is not set (the chart's hook Job sets it)")
    return value


async def _exists(call: Awaitable[Any]) -> bool:
    try:
        await call
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def _ds_ready(ds: Any) -> bool:
    s = ds.status
    return (s.desired_number_scheduled or 0) > 0 and s.number_ready == s.desired_number_scheduled


# ── install ─────────────────────────────────────────────────────────


async def check_cilium(api: client.ApiClient) -> list[str]:
    ext, apps, core = client.ApiextensionsV1Api(api), client.AppsV1Api(api), client.CoreV1Api(api)
    problems: list[str] = [
        f"CRD {crd} is missing: networkPolicy.provider=cilium needs Cilium installed"
        for crd in CILIUM_CRDS
        if not await _exists(ext.read_custom_resource_definition(crd))
    ]
    daemonsets = (await apps.list_daemon_set_for_all_namespaces(label_selector="k8s-app=cilium")).items
    if not daemonsets:
        return [*problems, "no Cilium agent DaemonSet (label k8s-app=cilium) in any namespace"]
    for ds in daemonsets:
        if not _ds_ready(ds):
            problems.append(
                f"Cilium DaemonSet {ds.metadata.namespace}/{ds.metadata.name} is not Ready "
                f"({ds.status.number_ready or 0}/{ds.status.desired_number_scheduled or 0})"
            )
    ns = daemonsets[0].metadata.namespace
    try:
        cm = await core.read_namespaced_config_map("cilium-config", ns)
    except ApiException as e:
        if e.status != 404:
            raise
        return [*problems, f"ConfigMap {ns}/cilium-config is missing: cannot confirm Cilium's DNS proxy"]
    if (cm.data or {}).get("enable-l7-proxy") != "true":
        problems.append(
            f"{ns}/cilium-config has enable-l7-proxy != true: FQDN egress needs Cilium's DNS proxy "
            "(Cilium Helm value l7Proxy=true)"
        )
    if (cm.data or {}).get("tofqdns-dns-reject-response-code") != "nameError":
        problems.append(
            f"{ns}/cilium-config has tofqdns-dns-reject-response-code != nameError: sandboxes resolve only "
            "allowlisted names, and musl (Alpine) gives up on the search list at the default REFUSED "
            "(Cilium Helm value dnsProxy.dnsRejectResponseCode=nameError, then restart the cilium DaemonSet)"
        )
    return problems


def _aws_policy_agent(ds: Any) -> bool:
    for c in ds.spec.template.spec.containers:
        if c.name == "aws-network-policy-agent" and "--enable-network-policy=true" in (c.args or []):
            return True
    return False


async def check_policy_engine(api: client.ApiClient) -> list[str]:
    apps = client.AppsV1Api(api)
    for selector in POLICY_ENGINES:
        if (await apps.list_daemon_set_for_all_namespaces(label_selector=selector)).items:
            return []
    aws = (await apps.list_daemon_set_for_all_namespaces(label_selector="k8s-app=aws-node")).items
    if any(_aws_policy_agent(ds) for ds in aws):
        return []
    hint = (
        " On EKS enable it in the VPC CNI add-on (enableNetworkPolicy=true)."
        if aws
        else f" Looked for DaemonSets {', '.join(POLICY_ENGINES)} and aws-node with its policy agent."
    )
    return [f"networkPolicy.provider=kubernetes but no NetworkPolicy engine runs: the policies would be inert.{hint}"]


async def check_kql_storage(api: client.ApiClient, storage_class: str, snapshot_class: str) -> list[str]:
    ext, storage = client.ApiextensionsV1Api(api), client.StorageV1Api(api)
    custom = client.CustomObjectsApi(api)
    problems: list[str] = [
        f"CRD {crd} is missing: kql needs the CSI snapshot CRDs and snapshot-controller"
        for crd in SNAPSHOT_CRDS
        if not await _exists(ext.read_custom_resource_definition(crd))
    ]
    if not await _exists(storage.read_storage_class(storage_class)):
        problems.append(f"StorageClass {storage_class!r} (backends.kql.storageClass) does not exist")
    if not problems and not await _exists(
        custom.get_cluster_custom_object("snapshot.storage.k8s.io", "v1", "volumesnapshotclasses", snapshot_class)
    ):
        problems.append(f"VolumeSnapshotClass {snapshot_class!r} (backends.kql.volumeSnapshotClass) does not exist")
    return problems


async def check_namespace(api: client.ApiClient, namespace: str) -> list[str]:
    ns = await client.CoreV1Api(api).read_namespace(namespace)
    level = (ns.metadata.labels or {}).get("pod-security.kubernetes.io/enforce")
    if level != "privileged":
        return [
            f"namespace {namespace} has pod-security.kubernetes.io/enforce={level or '<unset>'}: k7-node-installer "
            f"is privileged + hostPID. Run: kubectl label namespace {namespace} "
            "pod-security.kubernetes.io/enforce=privileged"
        ]
    return []


async def preflight(api: client.ApiClient, namespace: str, provider: str, backends: list[str]) -> None:
    problems = await check_namespace(api, namespace)
    if provider == "cilium":
        problems += await check_cilium(api)
    elif provider == "kubernetes":
        problems += await check_policy_engine(api)
    if "kql" in backends:
        problems += await check_kql_storage(
            api, cluster_config.kql_storage_class(), cluster_config.kql_snapshot_class()
        )
    if problems:
        raise HookError("refusing to install k7:\n  - " + "\n  - ".join(problems))
    log.info(f"preflight ok: provider={provider} backends={','.join(backends)}")


async def ensure_secrets(api: client.ApiClient, namespace: str) -> None:
    core = client.CoreV1Api(api)
    wanted = {
        AGENT_TOKEN_SECRET: {"agent_token": secrets.token_hex(32)},
        KEYS_SECRET: {},
        USAGE_SECRET: {},
    }
    for name, data in wanted.items():
        try:
            existing = await core.read_namespaced_secret(name, namespace)
        except ApiException as e:
            if e.status != 404:
                raise
            body = client.V1Secret(metadata=client.V1ObjectMeta(name=name), string_data=data or None)
            await core.create_namespaced_secret(namespace, body)
            log.info(f"created Secret {namespace}/{name}")
            continue
        if name == AGENT_TOKEN_SECRET and not (existing.data or {}).get("agent_token"):
            raise HookError(f"Secret {namespace}/{name} exists without an agent_token key; delete it to regenerate")
        log.info(f"kept Secret {namespace}/{name}")


async def ensure_cluster_id(api: client.ApiClient, namespace: str) -> None:
    """The usage-event cluster id: created once, never regenerated (a new id is a new cluster downstream)."""
    core = client.CoreV1Api(api)
    try:
        cm = await core.read_namespaced_config_map(CLUSTER_ID_CM, namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=CLUSTER_ID_CM), data={"id": str(uuid.uuid4())})
        await core.create_namespaced_config_map(namespace, body)
        log.info(f"created ConfigMap {namespace}/{CLUSTER_ID_CM}")
        return
    try:
        uuid.UUID((cm.data or {}).get("id", ""))
    except ValueError:
        raise HookError(f"ConfigMap {namespace}/{CLUSTER_ID_CM} has no UUID under key id: {cm.data!r}") from None
    log.info(f"kept ConfigMap {namespace}/{CLUSTER_ID_CM}")


async def converge_remote_install(api: client.ApiClient, namespace: str, remote_install_on: str) -> None:
    if remote_install_on not in ("0", "1"):
        raise HookError(f"K7_HOOK_REMOTE_INSTALL={remote_install_on!r}, expected 0 or 1")
    if remote_install_on == "1":
        return
    left = await remote_install.converge_off(client.CoreV1Api(api), namespace)
    log.info(f"remote install off: removed {remote_install.ADDED_BY_LABEL} from {left or 'no nodes'}")


# ── uninstall ───────────────────────────────────────────────────────


async def uninstall_stop(api: client.ApiClient, namespace: str, timeout_s: float) -> None:
    apps, core = client.AppsV1Api(api), client.CoreV1Api(api)
    try:
        await apps.delete_namespaced_daemon_set(INSTALLER_DS, namespace, propagation_policy="Foreground")
    except ApiException as e:
        if e.status != 404:
            raise
    deadline = time.monotonic() + timeout_s
    while True:
        pods = (await core.list_namespaced_pod(namespace, label_selector=f"app={INSTALLER_DS}")).items
        if not pods and not await _exists(apps.read_namespaced_daemon_set(INSTALLER_DS, namespace)):
            log.info(f"{INSTALLER_DS} deleted")
            return
        if time.monotonic() > deadline:
            raise HookError(f"{INSTALLER_DS} still has {len(pods)} pod(s) after {timeout_s:.0f}s")
        await asyncio.sleep(3)


async def _cleanup_report(core: client.CoreV1Api, namespace: str) -> str:
    lines = []
    for p in (await core.list_namespaced_pod(namespace, label_selector=f"app={CLEANUP_DS}")).items:
        reasons = [
            (cs.state.waiting.reason if cs.state.waiting else "running" if cs.state.running else "terminated")
            for cs in p.status.container_statuses or []
        ]
        lines.append(f"{p.metadata.name} on {p.spec.node_name}: {p.status.phase} {','.join(reasons)}")
    return "; ".join(lines) or "no cleanup pods"


async def uninstall_wait(api: client.ApiClient, namespace: str, timeout_s: float) -> None:
    apps, core = client.AppsV1Api(api), client.CoreV1Api(api)
    deadline = time.monotonic() + timeout_s
    while True:
        pool = len((await core.list_node(label_selector=POOL_SELECTOR)).items)
        ds = await apps.read_namespaced_daemon_set(CLEANUP_DS, namespace)
        synced = (ds.status.observed_generation or 0) >= (ds.metadata.generation or 0)
        if synced and (ds.status.desired_number_scheduled or 0) == pool and (pool == 0 or _ds_ready(ds)):
            break
        if time.monotonic() > deadline:
            raise HookError(
                f"node cleanup not finished after {timeout_s:.0f}s ({await _cleanup_report(core, namespace)}). "
                f"`kubectl -n {namespace} logs ds/{CLEANUP_DS}` says why; a cleanup pod refuses while K7 "
                "sandboxes run on its node (nodeInstaller.forceCleanup=true deletes them)."
            )
        await asyncio.sleep(5)
    still = [n.metadata.name for n in (await core.list_node(label_selector=READY_LABEL)).items]
    if still:
        raise HookError(f"cleanup finished but nodes still carry {READY_LABEL}: {', '.join(still)}")
    for name in HOOK_SECRETS:
        try:
            await core.delete_namespaced_secret(name, namespace)
            log.info(f"deleted Secret {namespace}/{name}")
        except ApiException as e:
            if e.status != 404:
                raise
    log.info(f"node cleanup done on {pool} node(s)")


# ── helm test ───────────────────────────────────────────────────────


async def _wait_exec(core: Any, name: str, namespace: str, timeout_s: float) -> str:
    deadline, last = time.monotonic() + timeout_s, ""
    while time.monotonic() < deadline:
        try:
            r = await core.exec_command(name, "echo k7-helm-test", namespace=namespace)
            if r.exit_code == 0 and "k7-helm-test" in r.stdout:
                return r.stdout
            last = f"exit {r.exit_code}: {r.stderr.strip()[-300:]}"
        except Exception as e:
            last = str(e)[-300:]
        await asyncio.sleep(3)
    raise HookError(f"exec in {namespace}/{name} never succeeded within {timeout_s:.0f}s: {last}")


def _ok(result: Any, what: str) -> None:
    if not result.success:
        raise HookError(f"{what}: {result.error or result.message}")


async def smoke(backends: list[str], timeout_s: float) -> None:
    from k7.core.core import K7Core
    from k7.core.models import SandboxConfig

    ns = f"k7-helm-test-{uuid.uuid4().hex[:6]}"
    k7 = K7Core()
    config.load_incluster_config()
    api = client.ApiClient()
    v1 = client.CoreV1Api(api)
    await v1.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=ns)))
    try:
        for b in backends:
            name = f"t-{b}"
            start = time.monotonic()
            cfg = SandboxConfig(name=name, image=TEST_IMAGE, namespace=ns, backend=CANONICAL[b])
            _ok(await k7.create_sandbox(cfg), f"create {b}")
            await _wait_exec(k7, name, ns, timeout_s)
            log.info(f"{b}: create+exec ok ({time.monotonic() - start:.0f}s)")
            if b in FORKABLE:
                _ok(await k7.fork_sandbox(name, f"{name}-fork", namespace=ns), f"fork {b}")
                await _wait_exec(k7, f"{name}-fork", ns, timeout_s)
                _ok(await k7.delete_sandbox(f"{name}-fork", ns), f"delete {b} fork")
                log.info(f"{b}: fork ok")
            _ok(await k7.delete_sandbox(name, ns), f"delete {b}")
            log.info(f"{b}: delete ok")
    finally:
        try:
            await v1.delete_namespace(ns)
        finally:
            await k7.aclose()
            await api.close()


# ── entrypoint ──────────────────────────────────────────────────────


async def run(command: str) -> None:
    timeout_s = float(_env("K7_HOOK_TIMEOUT_S"))
    if command == "test":
        await smoke(_env("K7_HOOK_BACKENDS").split(","), timeout_s)
        return
    namespace = cluster_config.system_namespace()
    config.load_incluster_config()
    async with client.ApiClient() as api:
        if command == "install":
            await preflight(api, namespace, _env("K7_HOOK_PROVIDER"), _env("K7_HOOK_BACKENDS").split(","))
            await ensure_secrets(api, namespace)
            await ensure_cluster_id(api, namespace)
            await converge_remote_install(api, namespace, _env("K7_HOOK_REMOTE_INSTALL"))
        elif command == "uninstall-stop":
            await uninstall_stop(api, namespace, timeout_s)
        else:
            await uninstall_wait(api, namespace, timeout_s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="k7 Helm chart hooks")
    p.add_argument("command", choices=["install", "uninstall-stop", "uninstall-wait", "test"])
    args = p.parse_args(argv)
    k7_logging.configure("human", "info")
    try:
        asyncio.run(run(args.command))
    except (HookError, RuntimeError) as e:
        log.error(f"k7 chart hook {args.command}: FAILED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
