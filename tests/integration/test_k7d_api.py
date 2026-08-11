"""k7d VM ops through the centralized HTTP API (specs 18f issue 2 + 18g).

The k7-api pod handles sandboxes co-located with it directly (hostPath
sockets + crictl). For a sandbox on any OTHER node, core forwards the VM
op to the k7-agent DaemonSet pod on that node (spec 18g) — so
pause/resume/fork work through the API for any node. The agent requires
the shared token from /etc/k7/agent_token and rejects everything else.

Runs on the first master (like all API integration tests: the key store
and NodePort are local).
"""

import json
import subprocess
import time

import httpx
import pytest

from .test_api import _api_pod_ready, _generate_test_api_key, _get_api_base_url

pytestmark = [pytest.mark.integration, pytest.mark.k7d]


def _kubectl_json(*args: str) -> dict:
    result = subprocess.run(
        ["k3s", "kubectl", *args, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _k7d_runtimeclass_present() -> bool:
    try:
        result = subprocess.run(
            ["k3s", "kubectl", "get", "runtimeclass", "k7"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


requires_k7d = pytest.mark.skipif(
    not _k7d_runtimeclass_present(),
    reason="k7d backend not installed (RuntimeClass k7 absent)",
)


@pytest.fixture(scope="module")
def api_base_url() -> str:
    if not _api_pod_ready():
        pytest.skip("k7-api deployment not ready")
    url = _get_api_base_url()
    for _ in range(30):
        try:
            if httpx.get(f"{url}/health", timeout=3).status_code == 200:
                return url
        except httpx.ConnectError:
            pass
        time.sleep(2)
    pytest.skip("k7-api health endpoint not reachable")


@pytest.fixture(scope="module")
def api_key() -> str:
    return _generate_test_api_key()


@pytest.fixture(scope="module")
def api_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


@pytest.fixture(scope="module")
def api_node() -> str:
    """The Kubernetes node hosting the k7-api pod (the first master)."""
    pods = _kubectl_json("get", "pods", "-n", "kube-system", "-l", "app=k7-api")
    nodes = [p["spec"]["nodeName"] for p in pods["items"] if p["status"]["phase"] == "Running"]
    assert nodes, "no Running k7-api pod found"
    return nodes[0]


def _wait_sandbox_ready(url: str, headers: dict, name: str, namespace: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{url}/api/v1/sandboxes/{name}", params={"namespace": namespace}, headers=headers, timeout=10)
        if r.status_code == 200 and r.json()["data"]["ready"] == "True":
            return r.json()["data"]
        time.sleep(3)
    raise TimeoutError(f"sandbox {name} not Ready via API within {timeout}s")


def _delete_sandbox(url: str, headers: dict, name: str, namespace: str) -> None:
    httpx.delete(f"{url}/api/v1/sandboxes/{name}", params={"namespace": namespace}, headers=headers, timeout=120)


@requires_k7d
class TestK7dViaApi:
    def test_k7d_pause_resume_fork_via_api_colocated(
        self, api_base_url: str, api_headers: dict, api_node: str, test_namespace: str
    ):
        """Full k7d VM-op round-trip over HTTP for a sandbox pinned to the
        API pod's node: pause → resume → fork. Also asserts the new
        ``node`` field (spec 18f issue 6) so clients can see placement."""
        name = "api-k7d-vmops"
        fork = "api-k7d-vmops-fork"
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={
                "name": name,
                "image": "alpine:3.20",
                "namespace": test_namespace,
                "backend": "k7d",
                "node_name": api_node,
            },
            headers=api_headers,
            timeout=300,
        )
        assert r.status_code == 201, f"create failed: {r.text}"

        try:
            info = _wait_sandbox_ready(api_base_url, api_headers, name, test_namespace)
            assert info["backend"] == "k7d"
            assert info["node"] == api_node, f"SandboxInfo.node missing/wrong: {info}"

            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/pause",
                json={"namespace": test_namespace},
                headers=api_headers,
                timeout=120,
            )
            assert r.status_code == 200, f"pause via API failed: {r.text}"
            assert "frozen" in r.json()["data"]["message"]

            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/resume",
                json={"namespace": test_namespace},
                headers=api_headers,
                timeout=120,
            )
            assert r.status_code == 200, f"resume via API failed: {r.text}"

            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/fork",
                json={"new_name": fork, "namespace": test_namespace},
                headers=api_headers,
                timeout=300,
            )
            assert r.status_code == 201, f"fork via API failed: {r.text}"
            assert "warm fork" in r.json()["data"]["message"]
        finally:
            _delete_sandbox(api_base_url, api_headers, fork, test_namespace)
            _delete_sandbox(api_base_url, api_headers, name, test_namespace)

    def test_k7d_vm_ops_on_remote_node_via_agent(
        self, api_base_url: str, api_headers: dict, api_node: str, test_namespace: str
    ):
        """Spec 18g: a k7d sandbox on a DIFFERENT node than the API pod is
        pausable/resumable/forkable through the API — core forwards the op
        to the k7-agent on the sandbox's node. The fork must land on the
        source's node (cross-node fork is k7d 9a M12, not built)."""
        nodes = _kubectl_json("get", "nodes")
        other = [n["metadata"]["name"] for n in nodes["items"] if n["metadata"]["name"] != api_node]
        if not other:
            pytest.skip("single-node cluster — no remote node to test against")

        name = "api-k7d-remote"
        fork = "api-k7d-remote-fork"
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={
                "name": name,
                "image": "alpine:3.20",
                "namespace": test_namespace,
                "backend": "k7d",
                "node_name": other[0],
            },
            headers=api_headers,
            timeout=300,
        )
        assert r.status_code == 201, f"create failed: {r.text}"

        try:
            info = _wait_sandbox_ready(api_base_url, api_headers, name, test_namespace)
            assert info["node"] == other[0]

            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/pause",
                json={"namespace": test_namespace},
                headers=api_headers,
                timeout=120,
            )
            assert r.status_code == 200, f"remote pause via agent failed: {r.text}"
            assert "frozen" in r.json()["data"]["message"]

            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/resume",
                json={"namespace": test_namespace},
                headers=api_headers,
                timeout=120,
            )
            assert r.status_code == 200, f"remote resume via agent failed: {r.text}"

            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/fork",
                json={"new_name": fork, "namespace": test_namespace},
                headers=api_headers,
                timeout=300,
            )
            assert r.status_code == 201, f"remote fork via agent failed: {r.text}"
            assert "warm fork" in r.json()["data"]["message"]
            fork_info = _wait_sandbox_ready(api_base_url, api_headers, fork, test_namespace)
            assert fork_info["node"] == other[0], f"fork must land on the source's node: {fork_info}"
        finally:
            _delete_sandbox(api_base_url, api_headers, fork, test_namespace)
            _delete_sandbox(api_base_url, api_headers, name, test_namespace)


def _local_agent_pod_ip(api_node: str) -> str:
    """Pod IP of the k7-agent on the test-runner's node (the first master —
    host→local-pod traffic is allowed by Cilium, so token auth is what's
    being exercised)."""
    pods = _kubectl_json(
        "get", "pods", "-n", "kube-system", "-l", "app=k7-agent", "--field-selector", "status.phase=Running"
    )
    ips = [
        p["status"]["podIP"] for p in pods["items"] if p["spec"]["nodeName"] == api_node and p["status"].get("podIP")
    ]
    assert ips, f"no Running k7-agent pod on {api_node}"
    return ips[0]


@requires_k7d
class TestK7Agent:
    def test_agent_rejects_unauthenticated_requests(self, api_node: str):
        """Spec 18g: the agent must reject requests without the shared token
        (missing header and wrong token both yield 401)."""
        base = f"http://{_local_agent_pod_ip(api_node)}:8000"
        r = httpx.post(f"{base}/agent/v1/vm/pause", json={"name": "x"}, timeout=10)
        assert r.status_code == 401, f"expected 401 without token, got {r.status_code}: {r.text}"
        r = httpx.get(f"{base}/agent/v1/storage", headers={"X-K7-Agent-Token": "wrong-token"}, timeout=10)
        assert r.status_code == 401, f"expected 401 with wrong token, got {r.status_code}: {r.text}"

    def test_agent_accepts_shared_token_storage(self, api_node: str):
        """With the real token from /etc/k7/agent_token, the storage endpoint
        reports both node-local pools."""
        with open("/etc/k7/agent_token") as f:
            token = f.read().strip()
        base = f"http://{_local_agent_pod_ip(api_node)}:8000"
        r = httpx.get(f"{base}/agent/v1/storage", headers={"X-K7-Agent-Token": token}, timeout=30)
        assert r.status_code == 200, f"storage query failed: {r.text}"
        data = r.json()
        assert data["kata_thinpool"]["size_bytes"] > 0, data
        assert 0 <= data["kata_thinpool"]["data_percent"] <= 100, data
        assert data["k7d_disks"]["size_bytes"] > 0, data

    def test_nodes_storage_aggregation(self, api_base_url: str, api_headers: dict):
        """GET /api/v1/nodes/storage returns both pools for EVERY cluster
        node (spec 18g success criterion)."""
        nodes = [n["metadata"]["name"] for n in _kubectl_json("get", "nodes")["items"]]
        r = httpx.get(f"{api_base_url}/api/v1/nodes/storage", headers=api_headers, timeout=120)
        assert r.status_code == 200, f"nodes/storage failed: {r.text}"
        data = r.json()["data"]
        for node in nodes:
            assert node in data, f"node {node} missing from storage report: {data}"
            entry = data[node]
            assert "error" not in entry, f"agent on {node} unreachable/failed: {entry}"
            assert entry["kata_thinpool"]["size_bytes"] > 0, entry
            assert entry["k7d_disks"]["size_bytes"] > 0, entry

    def test_nodes_storage_sdk(self, api_base_url: str, api_key: str):
        """Spec 18h: SDK ``Client.nodes_storage()`` hits the same endpoint."""
        from k7_sdk import Client

        nodes = [n["metadata"]["name"] for n in _kubectl_json("get", "nodes")["items"]]
        data = Client(endpoint=api_base_url, api_key=api_key, verify_ssl=False).nodes_storage()
        for node in nodes:
            assert node in data, f"node {node} missing from SDK storage report: {data}"
            assert "error" not in data[node], data[node]
            assert data[node]["kata_thinpool"]["size_bytes"] > 0, data[node]
