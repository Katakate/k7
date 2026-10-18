"""Cluster-wide VMM process leak detection.

Kata 3.24.0 launches the Firecracker jailer with ``--daemonize``. The
jailer double-forks, the shim records the parent PID (already exited),
and ``fcEnd`` SIGTERMs that PID. The grandchild ``firecracker-v1.*``
process, reparented to init, keeps running after the sandbox is gone.
Linux truncates ``comm`` to 15 bytes (``firecracker-v1.``), so a check
for ``comm == "firecracker"`` never sees it.

This module is named ``test_zz_*`` so pytest runs it LAST: after the
whole suite (and its sandbox churn) it asserts that every node's VMM
process count exactly matches its live Kata pod count — zero orphans.
Host processes are counted through short-lived hostPID scan pods pinned
to each node (runc runtime, so they never perturb the counts). The
reaper timer is stopped for these tests so a 1-minute watchdog cannot
hide a VM the shim failed to kill.
"""

import json
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

from .cluster import API_KEYS_AT_START, KUBECTL, NAMESPACES_AT_START, api_keys, namespaces, system_ns
from .test_metering import _status

pytestmark = pytest.mark.integration

# RuntimeClass → VMM process comm prefix expected per pod (one VMM each).
# k7d pods intentionally absent: their VMs live inside the single k7d
# daemon process, not in per-pod hypervisor processes.
_VMM_BY_RUNTIME_CLASS = {"kata": "firecracker", "kata-qemu": "qemu"}
_SCAN_TIMEOUT = 60
# Shims reap VMMs asynchronously after pod deletion — give the cluster a
# grace window to converge before declaring a leak.
_CONVERGE_TIMEOUT = 180
_REAPER_TIMER = "k7-vmm-reaper.timer"


def _kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([*KUBECTL, *args], capture_output=True, text=True, check=check)


def _node_names() -> list[str]:
    """Ready nodes only — a NotReady peer cannot run the hostPID scan pod."""
    data = json.loads(_kubectl("get", "nodes", "-o", "json").stdout)
    names: list[str] = []
    for node in data.get("items", []):
        conds = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
        if conds.get("Ready") == "True":
            names.append(node["metadata"]["name"])
    return names


def _live_kata_pods_per_node() -> dict[str, dict[str, int]]:
    """{node: {vmm_comm: live pod count}} for kata-backed pods. A pod is
    "live" (expected to have a VMM) unless it already finished; pods being
    created/deleted are counted too — convergence is handled by retrying."""
    data = json.loads(_kubectl("get", "pods", "-A", "-o", "json").stdout)
    counts: dict[str, dict[str, int]] = {}
    for pod in data["items"]:
        rc = pod["spec"].get("runtimeClassName")
        vmm = _VMM_BY_RUNTIME_CLASS.get(rc or "")
        node = pod["spec"].get("nodeName")
        if not vmm or not node:
            continue
        if pod["status"].get("phase") in ("Succeeded", "Failed"):
            continue
        counts.setdefault(node, {}).setdefault(vmm, 0)
        counts[node][vmm] += 1
    return counts


def _vmm_processes_on_node(node: str) -> dict[str, int]:
    """Count firecracker/qemu processes on a node via a hostPID scan pod."""
    pod_name = f"leak-scan-{uuid.uuid4().hex[:8]}"
    overrides = {
        "spec": {
            "nodeName": node,
            "hostPID": True,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "scan",
                    "image": "alpine:3.20",
                    "command": ["ps", "-o", "comm"],
                }
            ],
        }
    }
    _kubectl(
        "run",
        pod_name,
        "--image=alpine:3.20",
        "--restart=Never",
        f"--overrides={json.dumps(overrides)}",
    )
    try:
        deadline = time.time() + _SCAN_TIMEOUT
        while time.time() < deadline:
            phase = _kubectl("get", "pod", pod_name, "-o", "jsonpath={.status.phase}", check=False).stdout.strip()
            if phase == "Succeeded":
                break
            if phase == "Failed":
                raise RuntimeError(f"leak scan pod on {node} failed")
            time.sleep(2)
        else:
            raise TimeoutError(f"leak scan pod on {node} did not complete within {_SCAN_TIMEOUT}s")
        comms = _kubectl("logs", pod_name).stdout.splitlines()
    finally:
        _kubectl("delete", "pod", pod_name, "--ignore-not-found", "--wait=false", check=False)
    return count_vmms(comms)


def count_vmms(comms: list[str]) -> dict[str, int]:
    """Count VMMs the way the end-of-suite leak check must.

    k7d-fc repoints ``/usr/local/bin/firecracker`` at ``firecracker-v1.16.2``.
    Kata execs that target, and ``comm`` keeps 15 bytes (``firecracker-v1.``).
    An exact ``firecracker`` match sees none of them (PR #87).
    """
    return {
        "firecracker": sum(1 for c in comms if _is_firecracker_comm(c)),
        "qemu": sum(1 for c in comms if c.strip().startswith("qemu")),
    }


def _is_firecracker_comm(comm: str) -> bool:
    """True for the stock name and the versioned binary.

    ``firecracker-v1.16.2`` is truncated to ``firecracker-v1.`` in
    ``comm``. Equality with ``firecracker`` misses every jailed kfd VM
    once ``/usr/local/bin/firecracker`` is that symlink.
    """
    name = comm.strip()
    return name == "firecracker" or name.startswith("firecracker-")


def _firecracker_pids() -> dict[str, str]:
    """{pid: comm} for every Firecracker process on this host."""
    found: dict[str, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                comm = f.read().strip()
        except OSError:
            continue
        if _is_firecracker_comm(comm):
            found[entry] = comm
    return found


def _proc_ppid(pid: str) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[1]


def _proc_state(pid: str) -> str | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[0]


def _local_node_name() -> str:
    short = socket.gethostname().split(".", 1)[0]
    data = json.loads(_kubectl("get", "nodes", "-o", "json").stdout)
    names = [n["metadata"]["name"] for n in data.get("items", [])]
    for name in names:
        if name == short or name.startswith(short + "."):
            return name
    if not names:
        raise RuntimeError("no Kubernetes nodes found")
    return names[0]


@contextmanager
def _reaper_paused() -> Iterator[None]:
    """Stop the VMM reaper so it cannot SIGKILL an orphan the shim left behind."""
    exists = subprocess.run(["systemctl", "cat", _REAPER_TIMER], capture_output=True)
    stopped = False
    if exists.returncode == 0:
        stop = subprocess.run(["systemctl", "stop", _REAPER_TIMER], capture_output=True, text=True)
        if stop.returncode != 0:
            raise RuntimeError(f"systemctl stop {_REAPER_TIMER} failed: {stop.stderr.strip()}")
        stopped = True
    try:
        yield
    finally:
        if stopped:
            subprocess.run(["systemctl", "start", _REAPER_TIMER], check=True)


class TestOidcTestLeaks:
    def test_no_dex_tenants_bindings_or_keys_left_by_test_oidc_login(self):
        """test_oidc_login labels everything it creates and restores k7-oidc,
        k7-role-bindings and the k7-api oidc-config annotation in finally."""
        from k7.deploy.images import OIDC_OFF_HASH
        from k7.deploy.k7_api_keys import KEYS_SECRET, Kubectl, decode_keys

        from .cluster import system_ns

        label = "k7.katakate.org/itest=oidc"
        kinds = "namespaces,deployments,services,configmaps,customresourcedefinitions"
        if _kubectl("get", "crd", "tenants.k7.katakate.org", check=False).returncode == 0:
            kinds += ",tenants.k7.katakate.org"
        left = _kubectl("get", kinds, "-A", "-l", label, "-o", "name").stdout.split()
        assert not left, f"objects labelled {label} outlived test_oidc_login: {left}"

        ns = system_ns()
        keys = decode_keys(Kubectl(KUBECTL, ns).read(KEYS_SECRET) or {})
        stale = sorted(e.get("name") for e in keys.values() if str(e.get("name", "")).startswith("itest-oidc-"))
        assert not stale, f"API keys left by test_oidc_login: {stale}"

        bindings = _kubectl("-n", ns, "get", "cm", "k7-role-bindings", "-o", "json", check=False)
        if bindings.returncode == 0:
            assert "itest-" not in json.dumps(json.loads(bindings.stdout).get("data") or {})

        if _kubectl("-n", ns, "get", "cm", "k7-oidc", check=False).returncode != 0:
            annotations = json.loads(
                _kubectl(
                    "-n", ns, "get", "deploy", "k7-api", "-o", "jsonpath={.spec.template.metadata.annotations}"
                ).stdout
                or "{}"
            )
            assert annotations.get("k7.katakate.org/oidc-config") in (None, OIDC_OFF_HASH), annotations


def test_no_test_api_keys_left():
    """Leaked test keys pile up across runs. Once a role or tenancy migration makes them
    unscoped, production mode refuses to start (test_tenancy's production probes)."""
    from k7.deploy.k7_api_keys import KEYS_SECRET, Kubectl, decode_keys

    from .cluster import system_ns

    keys = decode_keys(Kubectl(KUBECTL, system_ns()).read(KEYS_SECRET) or {})
    prefixes = ("integ", "revoke-", "obs-", "it41l-", "itest-")
    left = sorted(f"{k} ({e.get('name')})" for k, e in keys.items() if str(e.get("name", "")).startswith(prefixes))
    assert not left, f"test API keys left in {KEYS_SECRET}: {left}"


class TestVmmProcessLeaks:
    def test_no_orphaned_vmm_processes_cluster_wide(self):
        """Every node's firecracker/qemu process count must equal its live
        Kata pod count. More processes than pods == orphaned VMMs (the
        kata-fc shim leak); fewer == a pod without its VMM. Both are loud
        failures. Versioned ``firecracker-v1.*`` comms count."""
        nodes = _node_names()
        assert nodes, "no Kubernetes nodes found"

        deadline = time.time() + _CONVERGE_TIMEOUT
        mismatches: list[str] = []
        with _reaper_paused():
            while time.time() < deadline:
                expected = _live_kata_pods_per_node()
                actual = {node: _vmm_processes_on_node(node) for node in nodes}
                mismatches = []
                for node in nodes:
                    for vmm in ("firecracker", "qemu"):
                        want = expected.get(node, {}).get(vmm, 0)
                        got = actual[node][vmm]
                        if got != want:
                            mismatches.append(
                                f"{node}: {got} {vmm} process(es) but {want} live kata pod(s) expecting one"
                            )
                if not mismatches:
                    return
                time.sleep(10)

        pytest.fail(
            "VMM process leak detected (did not converge within "
            f"{_CONVERGE_TIMEOUT}s):\n  " + "\n  ".join(mismatches) + "\n"
            "Orphaned firecracker processes spin at 100% CPU and starve the node "
            "(CHALLENGES.md #6). Inspect with "
            "`ps -eo pid,ppid,comm,%cpu | grep -E 'firecracker|qemu'` on the node. "
            "comm is truncated to 15 bytes (firecracker-v1.), so an exact "
            "match on 'firecracker' hides the orphan."
        )

    @pytest.mark.firecracker
    async def test_kfd_delete_reaps_its_firecracker(self, k7_core: K7Core, test_namespace: str):
        """Deleting one kfd sandbox must kill its Firecracker process.

        The reaper is stopped: a VM that is still running after the pod
        is gone is a failed teardown, not something a timer may clean up.
        While the sandbox is running the VMM's parent must be the shim
        (PPID != 1). ``--daemonize`` reparents it to init immediately.
        """
        name = "integ-fc-leak"
        cfg = SandboxConfig(
            name=name,
            image="alpine:3.20",
            namespace=test_namespace,
            backend="kata-firecracker-devmapper",
            node_name=_local_node_name(),
        )
        with _reaper_paused():
            before = set(_firecracker_pids())
            result = await k7_core.create_sandbox(cfg)
            assert result.success, f"create failed: {result.error}"
            try:
                deadline = time.time() + 120
                while time.time() < deadline:
                    phase = _kubectl(
                        "-n",
                        test_namespace,
                        "get",
                        "pods",
                        "-l",
                        f"app={name}",
                        "-o",
                        "jsonpath={.items[0].status.phase}",
                        check=False,
                    ).stdout.strip()
                    if phase == "Running":
                        break
                    time.sleep(2)
                else:
                    raise AssertionError(f"pod for {name} did not reach Running")
                born = {pid: comm for pid, comm in _firecracker_pids().items() if pid not in before}
                assert born, f"no new firecracker process for {name}; comms={_firecracker_pids()}"
                orphans = {pid: _proc_ppid(pid) for pid in born if _proc_ppid(pid) == "1"}
                assert not orphans, f"firecracker already reparented to init while the sandbox is running: {orphans}"
                pod = _kubectl(
                    "-n",
                    test_namespace,
                    "get",
                    "pod",
                    "-l",
                    f"app={name}",
                    "-o",
                    "jsonpath={.items[0].metadata.name}",
                ).stdout.strip()
                assert pod, "sandbox pod disappeared before delete"
                deleted = await k7_core.delete_sandbox(name, namespace=test_namespace)
                assert deleted.success, deleted.error
                _kubectl("-n", test_namespace, "wait", "--for=delete", "pod", pod, "--timeout=120s")
                leftover = {pid: state for pid in born if (state := _proc_state(pid)) not in (None, "Z")}
                assert not leftover, f"firecracker outlived sandbox {name}: {leftover}"
                reap_deadline = time.time() + 5
                while time.time() < reap_deadline and any(_proc_state(pid) == "Z" for pid in born):
                    time.sleep(0.2)
                zombies = {pid: _proc_state(pid) for pid in born if _proc_state(pid) is not None}
                assert not zombies, f"firecracker zombie outlived sandbox {name}: {zombies}"
            finally:
                await k7_core.delete_sandbox(name, namespace=test_namespace)


class TestTenancyLeaks:
    """Tenancy tests leave no Tenant CRs, quota Leases, ResourceQuotas, labels or probe pods."""

    def test_no_test_tenants_left(self):
        items = json.loads(_kubectl("get", "tenants.k7.katakate.org", "-o", "json").stdout)["items"]
        left = [t["metadata"]["name"] for t in items if t["metadata"]["name"].startswith("it41l-")]
        assert left == [], f"test tenants left behind: {left}"

    def test_no_quota_leases_left(self):
        items = json.loads(_kubectl("get", "lease", "-A", "-o", "json").stdout)["items"]
        left = [f"{i['metadata']['namespace']}/{i['metadata']['name']}" for i in items]
        left = [n for n in left if "/k7-quota-" in n]
        assert left == [], f"quota leases outlive their create: {left}"

    def test_no_orphan_resource_quotas(self):
        tenants = json.loads(_kubectl("get", "tenants.k7.katakate.org", "-o", "json").stdout)["items"]
        claimed = {ns for t in tenants for ns in (t.get("spec") or {}).get("namespaces") or []}
        items = json.loads(_kubectl("get", "resourcequota", "-A", "-o", "json").stdout)["items"]
        left = [
            f"{i['metadata']['namespace']}/{i['metadata']['name']}"
            for i in items
            if i["metadata"]["name"] == "k7-tenant-quota" and i["metadata"]["namespace"] not in claimed
        ]
        assert left == [], f"k7-tenant-quota outside any tenant namespace: {left}"

    def test_no_test_tenant_labels_or_probe_pods(self):
        objs = json.loads(_kubectl("get", "nodes,namespaces", "-o", "json").stdout)["items"]
        labelled = [
            f"{o['kind']}/{o['metadata']['name']}"
            for o in objs
            if (o["metadata"].get("labels") or {}).get("k7.katakate.org/tenant", "").startswith("it41l-")
        ]
        assert labelled == [], f"tenant labels left by tests: {labelled}"
        tainted = [
            n["metadata"]["name"]
            for n in objs
            if n["kind"] == "Node"
            and any(
                t.get("key") == "k7.katakate.org/tenant" and str(t.get("value", "")).startswith("it41l-")
                for t in (n.get("spec") or {}).get("taints") or []
            )
        ]
        assert tainted == [], f"nodes still dedicated to test tenants: {tainted}"
        pods = json.loads(
            _kubectl("get", "pods", "-A", "-l", "app in (k7-api-probe,it41l-iperf3)", "-o", "json").stdout
        )
        assert pods["items"] == [], [p["metadata"]["name"] for p in pods["items"]]


class TestMeteringLeaks:
    """test_metering put k7-metering back, deleted its OpenMeter pod, and every sink drained."""

    def test_no_openmeter_pod_or_test_sink_left(self):
        pods = json.loads(_kubectl("get", "pods", "-A", "-l", "app=it41m-openmeter", "-o", "json").stdout)["items"]
        assert pods == [], [f"{p['metadata']['namespace']}/{p['metadata']['name']}" for p in pods]
        cm = json.loads(_kubectl("-n", system_ns(), "get", "configmap", "k7-metering", "-o", "json").stdout)
        left = [s["name"] for s in json.loads(cm["data"]["metering.json"])["sinks"] if s["name"].startswith("it41m-")]
        assert left == [], f"k7-metering still carries test sinks: {left}"

    def test_usage_spool_drained(self):
        deadline = time.time() + _CONVERGE_TIMEOUT
        while True:
            st = _status()
            undelivered = {n: b for n, b in st["spool"]["pending_bytes"].items() if b}
            stopped = {n: s["stopped"] for n, s in st["sinks"].items() if s["stopped"]}
            if not undelivered and not stopped:
                return
            assert time.time() < deadline, f"usage spool not drained: pending {undelivered}, stopped {stopped}"
            time.sleep(5)


def test_no_namespace_created_by_the_suite_outlives_it():
    """Every namespace a test creates is deleted by its teardown (runs last, like the VMM check)."""
    if not NAMESPACES_AT_START:
        pytest.skip("namespaces were not recorded at session start")
    deadline = time.time() + _CONVERGE_TIMEOUT
    while (left := namespaces() - NAMESPACES_AT_START) and time.time() < deadline:
        time.sleep(5)
    assert not left, f"namespaces created during the run and never deleted: {sorted(left)}"


def test_no_api_key_minted_by_the_suite_outlives_it():
    """Every key a test mints (admin fixtures included) is revoked by its teardown."""
    if not NAMESPACES_AT_START:
        pytest.skip("the key store was not recorded at session start")
    left = {k: n for k, n in api_keys().items() if k not in API_KEYS_AT_START}
    assert not left, f"API keys minted during the run and never revoked: {sorted(left.items())}"
