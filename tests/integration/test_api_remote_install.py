"""POST /api/v1/install end to end (k7.api.remote_install): the API labels a node into the
k7-node-installer pool, the DaemonSet prepares it, GET /api/v1/install/{id} polls it to
succeeded. Refusals change nothing. The enabled tests need a cluster installed with
`k7 install --api-remote-install` (on a single K3s server node, also
`--node-installer-allow-server-restart`); the default install only gets the 404 check.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from k7 import __version__
from k7.api import remote_install as ri

from .cluster import kubectl, system_ns
from .test_api import _api_pod_ready, _generate_test_api_key, _get_api_base_url, _revoke_test_api_key, httpx

pytestmark = pytest.mark.integration

INSTALL_TIMEOUT_S = 1200
# Leave the installed node in the pool and marked, for turning remote install off to converge
# (then TestRemoteInstallOff checks it: playbook unpools it, chart unmarks it and keeps k7-agent).
KEEP_NODE = os.environ.get("K7_TEST_REMOTE_INSTALL_KEEP_NODE") == "1"


def _api_env() -> dict[str, str]:
    dep = json.loads(kubectl("-n", system_ns(), "get", "deployment", "k7-api", "-o", "json", check=True).stdout)
    (api, *_) = dep["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value", "") for e in api["env"]}


def _node(name: str) -> dict:
    return json.loads(kubectl("get", "node", name, "-o", "json", check=True).stdout)


def _pool_nodes() -> list[str]:
    out = kubectl("get", "nodes", "-l", f"{ri.POOL_LABEL}=true", "-o", "jsonpath={.items[*].metadata.name}", check=True)
    return out.stdout.split()


def _nodes_with(label: str) -> list[str]:
    return kubectl("get", "nodes", "-l", label, "-o", "name", check=True).stdout.split()


def _records(ns: str) -> list[str]:
    return kubectl("-n", ns, "get", "configmap", "-l", ri.RECORD_SELECTOR, "-o", "name", check=True).stdout.split()


def _playbook_stack() -> list[tuple[str, str | None]]:
    """(kind/name, namespace) of every object `k7 install --api-remote-install` applies."""
    root = Path(ri.__file__).parents[1] / "deploy/manifests/k7-node-installer"
    docs = [
        d for f in [*root.glob("*.yaml"), *root.glob("k7-api-access/*.yaml")] for d in yaml.safe_load_all(f.read_text())
    ]
    return [(f"{d['kind']}/{d['metadata']['name']}", d["metadata"].get("namespace")) for d in docs if d]


def _installer_cfg(ns: str) -> dict:
    out = kubectl("-n", ns, "get", "configmap", ri.DAEMONSET, "-o", "jsonpath={.data.config\\.json}", check=True)
    return json.loads(out.stdout)


def _set_installer_cfg(ns: str, cfg: dict) -> None:
    patch = json.dumps({"data": {"config.json": json.dumps(cfg)}})
    kubectl("-n", ns, "patch", "configmap", ri.DAEMONSET, "--type=merge", "-p", patch, check=True)


def _wait_no_installer_pod(ns: str, node: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        out = kubectl(
            "-n", ns, "get", "pods", "-l", ri.POD_SELECTOR, "--field-selector", f"spec.nodeName={node}", "-o", "name"
        )
        if not out.stdout.strip():
            return
        time.sleep(3)
    raise AssertionError(f"installer pod on {node} still present 180s after leaving the pool")


def _wait_agent_ready_on_pool() -> None:
    pool, status = len(_pool_nodes()), {}
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        out = kubectl("-n", system_ns(), "get", "daemonset", "k7-agent", "-o", "jsonpath={.status}", check=True)
        status = json.loads(out.stdout)
        if status.get("desiredNumberScheduled") == pool == status.get("numberReady"):
            return
        time.sleep(5)
    raise AssertionError(f"k7-agent not Ready on all {pool} pool node(s) after remote install off: {status}")


def _leave_pool(ns: str, node: str) -> None:
    kubectl("label", "node", node, f"{ri.POOL_LABEL}-", f"{ri.ADDED_BY_LABEL}-", check=True)
    _wait_no_installer_pod(ns, node)


@pytest.fixture(scope="module")
def base_url() -> str:
    if not _api_pod_ready():
        pytest.skip("k7-api deployment not ready")
    return _get_api_base_url()


@pytest.fixture(scope="module")
def env() -> dict[str, str]:
    return _api_env()


class TestRemoteInstallOff:
    def test_post_and_get_are_404(self, base_url: str, env: dict[str, str]):
        if env.get(ri.ENABLE_ENV) == "1":
            pytest.skip("remote install is enabled on this cluster")
        name = f"ri-off-{int(time.time())}"
        token = _generate_test_api_key(name=name, role="admin")
        try:
            headers = {"X-API-Key": token}
            r = httpx.post(f"{base_url}/api/v1/install", headers=headers, json={"nodes": ["x"]}, timeout=10)
            assert r.status_code == 404
            r = httpx.get(f"{base_url}/api/v1/install/0123456789abcdef", headers=headers, timeout=10)
            assert r.status_code == 404
        finally:
            _revoke_test_api_key(name)

    def test_off_leaves_no_installer_stack_or_pool_labels(self, env: dict[str, str]):
        """Re-installing without remote install converges: run it after a cluster had it on."""
        if env.get(ri.ENABLE_ENV) == "1":
            pytest.skip("remote install is enabled on this cluster")
        assert _nodes_with(ri.ADDED_BY_LABEL) == []
        dep = json.loads(kubectl("-n", system_ns(), "get", "deployment", "k7-api", "-o", "json", check=True).stdout)
        if "meta.helm.sh/release-name" in (dep["metadata"].get("annotations") or {}):
            # The chart keeps its installer stack (core) and the nodes the API added stay pooled,
            # so k7-agent keeps serving them; only the API's access, the marker and the records go.
            out = kubectl("-n", system_ns(), "get", "role,rolebinding", "-o", "name", check=True).stdout
            assert "k7-api-remote-install" not in out
            assert _records(system_ns()) == []
            _wait_agent_ready_on_pool()
            return
        assert _nodes_with(ri.POOL_LABEL) == []
        left = []
        for obj, ns in _playbook_stack():
            out = kubectl(*(["-n", ns] if ns else []), "get", obj, "--ignore-not-found", "-o", "name", check=True)
            left += out.stdout.split()
        assert left == [], f"left behind by a re-install without --api-remote-install: {left}"


class TestRemoteInstall:
    @pytest.fixture(scope="class")
    def ctx(self, base_url: str, env: dict[str, str]):
        if env.get(ri.ENABLE_ENV) != "1":
            pytest.skip("remote install is off: install with `k7 install --api-remote-install`")
        ns = env[ri.NAMESPACE_ENV]
        admin_name, user_name = f"ri-admin-{int(time.time())}", f"ri-user-{int(time.time())}"
        admin = _generate_test_api_key(name=admin_name, role="admin")
        try:
            user = _generate_test_api_key(name=user_name, role="operator")
            try:
                node = kubectl("get", "nodes", "-o", "jsonpath={.items[0].metadata.name}", check=True).stdout
                yield {
                    "url": base_url,
                    "ns": ns,
                    "image": env[ri.IMAGE_ENV],
                    "node": node,
                    "admin": {"X-API-Key": admin},
                    "user": {"X-API-Key": user},
                }
            finally:
                _revoke_test_api_key(user_name)
        finally:
            _revoke_test_api_key(admin_name)

    def _post(self, ctx: dict, body, headers: str = "admin"):
        return httpx.post(f"{ctx['url']}/api/v1/install", headers=ctx[headers], json=body, timeout=30)

    def test_daemonset_runs_the_deploy_time_image(self, ctx: dict):
        out = kubectl("-n", ctx["ns"], "get", "daemonset", ri.DAEMONSET, "-o", "json", check=True).stdout
        spec = json.loads(out)["spec"]["template"]["spec"]
        assert [c["image"] for c in spec["containers"]] == [ctx["image"]]
        assert spec["nodeSelector"] == {ri.POOL_LABEL: "true"}

    def test_non_admin_is_403_and_labels_nothing(self, ctx: dict):
        before = _pool_nodes()
        r = self._post(ctx, {"nodes": [ctx["node"]]}, headers="user")
        assert r.status_code == 403
        r = httpx.get(f"{ctx['url']}/api/v1/install/0123456789abcdef", headers=ctx["user"], timeout=10)
        assert r.status_code == 403
        assert _pool_nodes() == before

    @pytest.mark.parametrize(
        "extra",
        [
            {"playbook": "- hosts: all\n  tasks: []"},
            {"image": "ghcr.io/evil/k7-node-installer@sha256:" + "e" * 64},
            {"inventory": "[k7_servers]\nevil ansible_host=203.0.113.1"},
            {"config": {"allowServerRestart": True}},
        ],
    )
    def test_caller_cannot_choose_playbook_image_or_config(self, ctx: dict, extra: dict):
        before = _pool_nodes()
        r = self._post(ctx, {"nodes": [ctx["node"]], **extra})
        assert r.status_code == 400 and "fixed at deploy time" in r.text, r.text
        assert _pool_nodes() == before

    def test_unknown_node_is_404_and_records_nothing(self, ctx: dict):
        before = (_pool_nodes(), _records(ctx["ns"]))
        r = self._post(ctx, {"nodes": [ctx["node"], "k7-no-such-node"]})
        assert r.status_code == 404 and "k7-no-such-node" in r.text
        assert (_pool_nodes(), _records(ctx["ns"])) == before

    def test_unknown_operation_is_404(self, ctx: dict):
        for op in ("ffffffffffffffff", "not-an-id"):
            r = httpx.get(f"{ctx['url']}/api/v1/install/{op}", headers=ctx["admin"], timeout=10)
            assert r.status_code == 404, op

    def test_server_without_allow_server_restart_is_409(self, ctx: dict):
        labels = _node(ctx["node"])["metadata"]["labels"]
        if "node-role.kubernetes.io/control-plane" not in labels:
            pytest.skip(f"{ctx['node']} is not a server node")
        if _pool_nodes():
            pytest.skip("nodes are in the installer pool; a config change would re-run the installer there")
        cfg = _installer_cfg(ctx["ns"])
        _set_installer_cfg(ctx["ns"], {**cfg, "allowServerRestart": False})
        try:
            r = self._post(ctx, {"nodes": [ctx["node"]]})
            assert r.status_code == 409 and "server" in r.text, r.text
            assert ctx["node"] not in _pool_nodes()
        finally:
            _set_installer_cfg(ctx["ns"], cfg)

    def test_install_a_node_and_poll_to_succeeded(self, ctx: dict):
        node, ns = ctx["node"], ctx["ns"]
        cfg = _installer_cfg(ns)
        if "node-role.kubernetes.io/control-plane" in _node(node)["metadata"]["labels"] and not cfg.get(
            "allowServerRestart"
        ):
            pytest.skip(f"{node} is a server; install with --node-installer-allow-server-restart to cover it")
        was_in_pool = node in _pool_nodes()
        if was_in_pool:
            _leave_pool(ns, node)
        op = None
        try:
            r = self._post(ctx, {"nodes": [node]})
            assert r.status_code == 202, r.text
            started = r.json()["data"]
            op = started["operation"]
            assert started == {"operation": op, "nodes": [node], "status": f"/api/v1/install/{op}"}
            assert node in _pool_nodes()
            assert _node(node)["metadata"]["labels"][ri.ADDED_BY_LABEL] == ri.ADDED_BY
            seen, status = set(), {}
            deadline = time.monotonic() + INSTALL_TIMEOUT_S
            while time.monotonic() < deadline:
                r = httpx.get(f"{ctx['url']}{started['status']}", headers=ctx["admin"], timeout=30)
                assert r.status_code == 200, r.text
                status = r.json()["data"]
                seen.add(status["state"])
                if status["state"] in ("succeeded", "failed"):
                    break
                time.sleep(10)
            assert status.get("state") == "succeeded", json.dumps(status)
            assert "running" in seen, f"never observed the operation in progress: {seen}"
            assert status["image"] == ctx["image"] and status["nodes"][0]["node"] == node
            assert _node(node)["metadata"]["labels"][ri.installer.READY_LABEL] == __version__
            record = kubectl("-n", ns, "get", "configmap", ri.RECORD_PREFIX + op, "-o", "json", check=True).stdout
            assert json.loads(json.loads(record)["data"]["nodes"]) == [node]
            logs = kubectl("-n", system_ns(), "logs", "-l", "app=k7-api", "-c", "k7-api", "--tail=-1").stdout
            mentions = [json.loads(ln) for ln in logs.splitlines() if f'"operation": "{op}"' in ln]
            audit = [ln for ln in mentions if ln["logger"] == "k7.audit"]
            assert len(audit) == 1 and audit[0]["nodes"] == [node] and audit[0]["status"] == 202
        finally:
            if op:
                kubectl("-n", ns, "delete", "configmap", ri.RECORD_PREFIX + op, "--ignore-not-found")
            if was_in_pool:
                kubectl(
                    "label", "node", node, f"{ri.POOL_LABEL}=true", f"{ri.ADDED_BY_LABEL}-", "--overwrite", check=True
                )
            elif node in _pool_nodes() and not KEEP_NODE:
                _leave_pool(ns, node)
