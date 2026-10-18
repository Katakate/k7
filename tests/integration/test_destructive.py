"""Destructive: the stack is reinstalled, upgraded or removed. Opt-in only:
``K7_DESTRUCTIVE=1 pytest -m destructive`` (the hardware CI's ``destructive`` phase,
on freshly reset nodes).

K3s playbook install: ``k7 install`` killed mid-run (SIGKILL, SIGINT) converges
when re-run, and the stack then serves a sandbox.

Chart install (RKE2, ``deploy/helm/k7/ci/rke2-node.sh``): ``helm upgrade`` with
unchanged values restarts nothing; the networkPolicy provider switch needs its
acknowledgement and is reversible; ``helm uninstall`` with ``forceCleanup`` over a
live sandbox goes through the k7-node-cleanup admission policy (probed while the
cleanup pods run) and leaves nothing behind. The uninstall runs last.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

from . import cluster
from .cluster import as_token, bound_token_kubeconfig, kubectl, server_version, system_ns

pytestmark = [pytest.mark.integration, pytest.mark.destructive]

REPO = Path(__file__).resolve().parents[2]
DEV_SH = REPO / "src/k7/cli/dev.sh"
CHART = REPO / "deploy/helm/k7"
RELEASE = "k7"
SANDBOX_IMAGE = "alpine:3.20"
LABEL = "k7.katakate.org"
RUNTIME_CLASSES = ("kata", "kata-qemu", "k7", "k7-fc")
HOOK_SECRETS = ("k7-agent-token", "k7-api-keys", "k7-api-keys-usage")
RKE2_CONTAINERD = "containerd.*/var/lib/rancher/rke2/agent/etc/containerd/config.toml"
# What the cleanup playbook (k7_node_prep cleanup.yml, rke2 adapter) removes from the host.
HOST_LEFTOVERS = (
    "/var/lib/k7/installer-state.json",
    "/etc/systemd/system/k7d.service",
    "/usr/local/bin/containerd-shim-kata-v2",
    "/var/lib/rancher/rke2/agent/etc/containerd/config-v3.toml.tmpl",
)
ANSI = re.compile(r"\x1b\[[0-9;]*[mK]")
INSTALL_TIMEOUT_S = 5400
# Ctrl-C must stop an install promptly; SIGKILL is immediate.
SIGNAL_EXIT_TIMEOUT_S = 300


def _json(*args: str) -> dict:
    return json.loads(kubectl(*args, "-o", "json", check=True).stdout)


def _names(*args: str) -> list[str]:
    return [i["metadata"]["name"] for i in _json("get", *args)["items"]]


def _tail(path: Path, n: int = 60) -> str:
    return "".join(path.read_text().splitlines(keepends=True)[-n:])


async def _wait_ready(core: K7Core, ns: str, name: str, timeout_s: float = 600) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(s.name == name and s.ready == "True" for s in await core.list_sandboxes(namespace=ns)):
            return
        await asyncio.sleep(2)
    pytest.fail(f"sandbox {ns}/{name} not ready within {timeout_s:.0f}s")


async def _sandbox_round_trip(core: K7Core, ns: str, name: str) -> None:
    r = await core.create_sandbox(SandboxConfig(name=name, image=SANDBOX_IMAGE, namespace=ns))
    assert r.success, f"create {ns}/{name}: {r.error}"
    try:
        await _wait_ready(core, ns, name)
        out = await core.exec_command(name, "echo ok", namespace=ns)
        assert out.exit_code == 0 and "ok" in out.stdout, out
    finally:
        await core.delete_sandbox(name, namespace=ns)


# ── K3s playbook install: killed mid-run ────────────────────────────


def _killpg(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, sig)


def _install(args: list[str], log: Path, kill_at: str | None = None, sig: signal.Signals = signal.SIGKILL) -> int:
    """Run ``k7 install -v args`` in its own process group, output to ``log``.

    With ``kill_at``, send ``sig`` to the whole group 3 s after the first output
    line matching it and return the exit code; fail if it is never reached.
    """
    p = subprocess.Popen(
        [str(DEV_SH), "install", "-v", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    watchdog = threading.Timer(INSTALL_TIMEOUT_S, _killpg, (p.pid, signal.SIGKILL))
    watchdog.start()
    try:
        killed = False
        with log.open("w") as f:
            assert p.stdout is not None
            for line in p.stdout:
                f.write(line)
                if kill_at and re.search(kill_at, ANSI.sub("", line)):
                    f.flush()
                    time.sleep(3)
                    _killpg(p.pid, sig)
                    killed = True
                    break
        if kill_at and not killed:
            pytest.fail(f"`k7 install` ended (rc={p.wait()}) before {kill_at!r}:\n{_tail(log)}")
        try:
            return p.wait(timeout=SIGNAL_EXIT_TIMEOUT_S if killed else None)
        except subprocess.TimeoutExpired:
            pytest.fail(f"`k7 install` still running {SIGNAL_EXIT_TIMEOUT_S}s after {sig.name}:\n{_tail(log)}")
    finally:
        watchdog.cancel()
        if p.poll() is None:
            _killpg(p.pid, signal.SIGKILL)
            p.wait()


@pytest.fixture()
def install_args() -> list[str]:
    """The `k7 install` args that built this cluster (utils/ci/k7-e2e.sh passes them)."""
    if os.environ.get("K7_KUBECONFIG") or not shutil.which("k3s"):
        pytest.skip("not a K3s playbook install")
    raw = os.environ.get("K7_ITEST_INSTALL_ARGS")
    if not raw:
        pytest.fail("set K7_ITEST_INSTALL_ARGS to the `k7 install` args that built this cluster")
    return shlex.split(raw)


@pytest.mark.parametrize(
    ("sig", "kill_at"),
    [
        pytest.param(signal.SIGKILL, r"^TASK \[k7_node_prep : Download Firecracker & Jailer binaries", id="kill-prep"),
        pytest.param(signal.SIGKILL, r"^TASK \[k7_k3s : Install K3s on first master", id="kill-k3s"),
        pytest.param(signal.SIGINT, r"^TASK \[k7_cluster : Install OpenEBS LVM LocalPV chart", id="ctrl-c-cluster"),
    ],
)
async def test_install_killed_mid_run_converges(
    install_args: list[str], test_namespace: str, tmp_path: Path, sig: signal.Signals, kill_at: str
):
    rc = _install(install_args, tmp_path / "killed.log", kill_at, sig)
    assert rc != 0, f"`k7 install` exited 0 after {sig.name}"

    rerun = tmp_path / "rerun.log"
    rc = _install(install_args, rerun)
    assert rc == 0, f"re-run after {sig.name} at {kill_at!r} failed (rc={rc}):\n{_tail(rerun)}"
    kubectl("wait", "--for=condition=Ready", "node", "--all", "--timeout=600s", check=True)
    for obj in ("deployment/k7-api", "daemonset/k7-agent"):
        kubectl("-n", system_ns(), "rollout", "status", obj, "--timeout=600s", check=True)
    core = K7Core()
    try:
        await _sandbox_round_trip(core, test_namespace, f"killed-{sig.name.lower()}")
    finally:
        await core.aclose()


# ── Chart install: upgrade, provider switch, uninstall ──────────────


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", *args, "-n", system_ns()],
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": cluster.KUBECONFIG},
    )


def _revision() -> int:
    r = _helm("status", RELEASE, "-o", "json")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["version"]


def _upgrade(*args: str) -> subprocess.CompletedProcess[str]:
    return _helm("upgrade", RELEASE, str(CHART), "--reuse-values", "--wait", "--timeout", "30m", *args)


@pytest.fixture()
def chart() -> None:
    if not shutil.which("helm") or _helm("status", RELEASE).returncode != 0:
        pytest.skip(f"no helm release {RELEASE!r} (K3s playbook install)")


def _containerd_pid() -> str:
    r = subprocess.run(["pgrep", "-of", RKE2_CONTAINERD], capture_output=True, text=True)
    assert r.returncode == 0, f"no process matches {RKE2_CONTAINERD!r}"
    return r.stdout.strip()


def _node_state() -> dict:
    """What a node re-prep would change: containerd, the installer pods, node-ready."""
    ns = system_ns()
    pods = _json("-n", ns, "get", "pods", "-l", "app=k7-node-installer")["items"]
    return {
        "containerd": _containerd_pid(),
        "installer generation": _json("-n", ns, "get", "daemonset", "k7-node-installer")["metadata"]["generation"],
        "installer pods": sorted(p["metadata"]["uid"] for p in pods),
        "node-ready": {
            n["metadata"]["name"]: n["metadata"].get("labels", {}).get(f"{LABEL}/node-ready")
            for n in _json("get", "nodes")["items"]
        },
    }


def _cilium_policies() -> list[str]:
    return sorted(
        f"{kind}/{name}"
        for kind in ("ciliumnetworkpolicies", "ciliumclusterwidenetworkpolicies")
        for name in _names(kind, "-A")
        if name.startswith("k7-")
    )


def _egress_mode() -> str:
    spec = _json("-n", system_ns(), "get", "deployment", "k7-api")["spec"]["template"]["spec"]
    modes = {e.get("value") for c in spec["containers"] for e in c.get("env", []) if e["name"] == "K7_EGRESS_MODE"}
    assert len(modes) == 1, f"k7-api K7_EGRESS_MODE values: {modes}"
    return modes.pop()


def test_upgrade_with_unchanged_config_restarts_nothing(chart):
    before, rev = _node_state(), _revision()
    r = _upgrade()
    assert r.returncode == 0, r.stderr
    assert _revision() == rev + 1
    assert _node_state() == before
    t = _helm("test", RELEASE, "--timeout", "30m")
    assert t.returncode == 0, t.stdout + t.stderr


def test_provider_switch_needs_ack_drops_cilium_policies_and_restores(chart):
    rev, pid, policies = _revision(), _containerd_pid(), _cilium_policies()
    assert policies and _egress_mode() == "full", (policies, _egress_mode())

    r = _upgrade("--set", "networkPolicy.provider=kubernetes")
    assert r.returncode != 0 and "acknowledgeNoPlatformDeny" in r.stderr, r.stderr
    assert _revision() == rev
    try:
        r = _upgrade("-f", str(CHART / "ci/rke2-kubernetes-values.yaml"))
        assert r.returncode == 0, r.stderr
        assert _cilium_policies() == []
        assert _egress_mode() == "cidr-only"
        assert _containerd_pid() == pid, "the provider is API config; the nodes must not be re-prepared"
    finally:
        restore = _upgrade(
            "--set", "networkPolicy.provider=cilium", "--set", "networkPolicy.acknowledgeNoPlatformDeny=false"
        )
    assert restore.returncode == 0, restore.stderr
    assert _cilium_policies() == policies
    assert _egress_mode() == "full"


def _probe_cleanup_policy(helm: subprocess.Popen, tmp: Path) -> list[str]:
    """While a k7-node-cleanup pod runs, act as it (pod-bound token) and check what the
    policy denies. Returns the problems; the uninstall is left to finish either way."""
    ns, deadline = system_ns(), time.monotonic() + 900
    while helm.poll() is None and time.monotonic() < deadline:
        got = kubectl("-n", ns, "get", "pods", "-l", "app=k7-node-cleanup", "-o", "json")
        running = [
            p
            for p in (json.loads(got.stdout)["items"] if got.returncode == 0 else [])
            if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")
        ]
        if running:
            break
        time.sleep(1)
    else:
        return [f"no Running k7-node-cleanup pod seen (helm rc={helm.poll()})"]
    pod = running[0]
    node, name = pod["spec"]["nodeName"], pod["metadata"]["name"]
    cfg = bound_token_kubeconfig(pod, tmp / "cleanup-kubeconfig")
    problems = []
    r = as_token(cfg, "-n", ns, "delete", "pod", name)
    if r.returncode == 0 or "may only delete K7 sandbox pods" not in r.stderr:
        problems.append(f"deleting a non-sandbox pod on its own node: rc={r.returncode} {r.stderr.strip()}")
    elsewhere = [
        p
        for p in _json("get", "pods", "-A", f"--field-selector=spec.nodeName!={node}")["items"]
        if p["spec"].get("nodeName")
    ]
    if elsewhere:
        o = elsewhere[0]["metadata"]
        r = as_token(cfg, "-n", o["namespace"], "delete", "pod", o["name"])
        if r.returncode == 0 or "may only delete pods on the node its pod runs on" not in r.stderr:
            problems.append(f"deleting a pod on another node: rc={r.returncode} {r.stderr.strip()}")
    return problems


def _leftovers(node_names: list[str]) -> list[str]:
    ns = system_ns()
    left = [f"helm release {RELEASE}"] if _helm("status", RELEASE).returncode == 0 else []
    left += [f"runtimeclass/{n}" for n in _names("runtimeclass") if n in RUNTIME_CLASSES]
    for kind in ("clusterrole", "clusterrolebinding", "validatingadmissionpolicy", "validatingadmissionpolicybinding"):
        left += [f"{kind}/{n}" for n in _names(kind) if n.startswith("k7-")]
    for kind in ("daemonset", "deployment", "job"):
        left += [f"{kind}/{n}" for n in _names(kind, "-n", ns)]
    left += [f"secret/{n}" for n in _names("secret", "-n", ns) if n in HOOK_SECRETS]
    for n in _json("get", "nodes")["items"]:
        left += [
            f"node/{n['metadata']['name']} label {k}"
            for k in n["metadata"].get("labels", {})
            if k.startswith(f"{LABEL}/") and k != f"{LABEL}/node-pool"
        ]
    left += [f"host file {p}" for p in HOST_LEFTOVERS if Path(p).exists()]
    events = _json("-n", "default", "get", "events", "--field-selector=reason=K7NodeCleaned")["items"]
    cleaned = {e["involvedObject"]["name"] for e in events}
    left += [f"node/{n}: no K7NodeCleaned event" for n in node_names if n not in cleaned]
    return left


async def test_uninstall_force_cleanup_policy_and_no_leftovers(
    chart, k7_core: K7Core, test_namespace: str, tmp_path: Path
):
    assert server_version() >= (1, 32), "the cleanup policy compares node names from bound tokens (Kubernetes 1.32+)"
    r = _upgrade("--set", "nodeInstaller.forceCleanup=true")
    assert r.returncode == 0, r.stderr
    name = "uninstall-victim"
    res = await k7_core.create_sandbox(SandboxConfig(name=name, image=SANDBOX_IMAGE, namespace=test_namespace))
    assert res.success, res.error
    await _wait_ready(k7_core, test_namespace, name)
    (victim,) = _json("-n", test_namespace, "get", "pods", "-l", f"app={name}")["items"]
    pool = _names("nodes", "-l", f"{LABEL}/node-pool=true")

    log = tmp_path / "uninstall.log"
    with log.open("w") as f:
        helm = subprocess.Popen(
            ["helm", "uninstall", RELEASE, "-n", system_ns(), "--wait", "--timeout", "35m"],
            stdout=f,
            stderr=subprocess.STDOUT,
            env={**os.environ, "KUBECONFIG": cluster.KUBECONFIG},
        )
        try:
            problems = _probe_cleanup_policy(helm, tmp_path)
            rc = helm.wait(timeout=40 * 60)
        finally:
            if helm.poll() is None:
                helm.kill()
                helm.wait()
    assert rc == 0, f"helm uninstall rc={rc}:\n{_tail(log)}"
    assert not problems, problems
    # The cleanup pod deleted the live sandbox pod through the policy (its allow path).
    uids = [p["metadata"]["uid"] for p in _json("-n", test_namespace, "get", "pods")["items"]]
    assert victim["metadata"]["uid"] not in uids
    left = _leftovers(pool)
    assert not left, left
