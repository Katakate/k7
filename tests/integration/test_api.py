"""Integration tests for the K7 API running as a K3s pod.

These tests exercise the full HTTP path: request → NodePort → k7-api pod
→ FastAPI → K7Core → Kubernetes API.  They verify that the API deployment,
ServiceAccount RBAC, and in-cluster config all work end-to-end.
"""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

K7_API_KEYS_FILE = Path("/etc/k7/api_keys.json")


def _get_api_base_url() -> str:
    """Read the API endpoint from the K3s service."""
    result = subprocess.run(
        ["k3s", "kubectl", "get", "svc", "k7-api", "-n", "kube-system", "-o", "jsonpath={.spec.ports[0].nodePort}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("k7-api service not found — run 'k7 install' or 'k7 api enable' first")
    port = result.stdout.strip()
    return f"http://127.0.0.1:{port}"


def _api_pod_ready() -> bool:
    result = subprocess.run(
        [
            "k3s",
            "kubectl",
            "get",
            "deployment",
            "k7-api",
            "-n",
            "kube-system",
            "-o",
            "jsonpath={.status.readyReplicas}",
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() not in ("", "0")


def _generate_test_api_key(*, name: str = "integration-test", namespaces: list[str] | None = None) -> str:
    """Write a test API key directly into the keys file, return the raw token."""
    import os
    import secrets as _secrets

    token = _secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(token.encode()).hexdigest()

    keys: dict = {}
    if K7_API_KEYS_FILE.exists():
        keys = json.loads(K7_API_KEYS_FILE.read_text())

    entry: dict = {
        "name": name,
        "created": int(time.time()),
        "expires": int(time.time()) + 3600,
        "last_used": None,
    }
    if namespaces:
        entry["namespaces"] = list(namespaces)
    keys[key_hash] = entry
    K7_API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    K7_API_KEYS_FILE.write_text(json.dumps(keys, indent=2))
    # Mirror production permissions (`k7 generate-api-key`): 0600 owned by
    # the API container uid (1000, see Dockerfile.api). The old 0666 here
    # masked a real bug where a root-owned 0600 store locked the pod out.
    os.chmod(K7_API_KEYS_FILE, 0o600)
    os.chown(K7_API_KEYS_FILE, 1000, 1000)
    return token


@pytest.fixture(scope="module")
def api_base_url() -> str:
    if not _api_pod_ready():
        pytest.skip("k7-api deployment not ready")
    url = _get_api_base_url()
    client = httpx.Client()
    for _ in range(30):
        try:
            r = client.get(f"{url}/health", timeout=3)
            if r.status_code == 200:
                client.close()
                return url
        except httpx.ConnectError:
            pass
        time.sleep(2)
    client.close()
    pytest.skip("k7-api health endpoint not reachable")


@pytest.fixture(scope="module")
def api_key() -> str:
    return _generate_test_api_key()


@pytest.fixture(scope="module")
def api_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


class TestApiHealth:
    def test_root_returns_version(self, api_base_url: str):
        r = httpx.get(f"{api_base_url}/", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert data["message"] == "K7 Sandbox API"

    def test_health_returns_healthy(self, api_base_url: str):
        r = httpx.get(f"{api_base_url}/health", timeout=5)
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"


class TestApiAuth:
    def test_list_without_key_returns_401(self, api_base_url: str):
        r = httpx.get(f"{api_base_url}/api/v1/sandboxes", timeout=5)
        assert r.status_code == 401

    def test_list_with_bad_key_returns_401(self, api_base_url: str):
        r = httpx.get(
            f"{api_base_url}/api/v1/sandboxes",
            headers={"X-API-Key": "bogus-key"},
            timeout=5,
        )
        assert r.status_code == 401

    def test_list_with_valid_key_returns_200(self, api_base_url: str, api_headers: dict):
        r = httpx.get(f"{api_base_url}/api/v1/sandboxes", headers=api_headers, timeout=5)
        assert r.status_code == 200
        assert "data" in r.json()


class TestApiSandboxLifecycle:
    """Create → list → get → exec → delete, all through the HTTP API."""

    SANDBOX_NAME = "api-integ-test"

    @pytest.fixture(autouse=True)
    def _cleanup(self, api_base_url: str, api_headers: dict, test_namespace: str):
        yield
        httpx.delete(
            f"{api_base_url}/api/v1/sandboxes/{self.SANDBOX_NAME}",
            params={"namespace": test_namespace},
            headers=api_headers,
            timeout=10,
        )

    def test_create_list_exec_delete(self, api_base_url: str, api_headers: dict, test_namespace: str):
        # Create
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={"name": self.SANDBOX_NAME, "image": "alpine:latest", "namespace": test_namespace},
            headers=api_headers,
            timeout=30,
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        assert r.json()["data"]["name"] == self.SANDBOX_NAME

        # Wait for ready
        ready = False
        for _ in range(90):
            gr = httpx.get(
                f"{api_base_url}/api/v1/sandboxes/{self.SANDBOX_NAME}",
                params={"namespace": test_namespace},
                headers=api_headers,
                timeout=5,
            )
            if gr.status_code == 200 and gr.json()["data"].get("ready") == "True":
                ready = True
                break
            time.sleep(2)
        assert ready, "sandbox never became ready"

        # List should include it
        lr = httpx.get(
            f"{api_base_url}/api/v1/sandboxes",
            params={"namespace": test_namespace},
            headers=api_headers,
            timeout=5,
        )
        assert lr.status_code == 200
        names = [s["name"] for s in lr.json()["data"]]
        assert self.SANDBOX_NAME in names

        # Exec
        er = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{self.SANDBOX_NAME}/exec",
            json={"command": "echo api-works"},
            params={"namespace": test_namespace},
            headers=api_headers,
            timeout=30,
        )
        assert er.status_code == 200
        exec_data = er.json()["data"]
        assert exec_data["exit_code"] == 0
        assert "api-works" in exec_data["stdout"]

        # Delete
        dr = httpx.delete(
            f"{api_base_url}/api/v1/sandboxes/{self.SANDBOX_NAME}",
            params={"namespace": test_namespace},
            headers=api_headers,
            timeout=30,
        )
        assert dr.status_code == 200

        # Confirm gone
        gr2 = httpx.get(
            f"{api_base_url}/api/v1/sandboxes/{self.SANDBOX_NAME}",
            params={"namespace": test_namespace},
            headers=api_headers,
            timeout=5,
        )
        assert gr2.status_code == 404


def _wait_for_sandbox_ready(api_base_url: str, api_headers: dict, name: str, namespace: str, timeout: int = 240):
    """Poll the API until the sandbox reports Ready=True. Raises on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(
            f"{api_base_url}/api/v1/sandboxes/{name}",
            params={"namespace": namespace},
            headers=api_headers,
            timeout=5,
        )
        if r.status_code == 200 and r.json()["data"].get("ready") == "True":
            return
        time.sleep(2)
    raise TimeoutError(f"Sandbox {name} did not become ready within {timeout}s")


def _replicas(name: str, namespace: str) -> int:
    out = subprocess.run(
        [
            "k3s",
            "kubectl",
            "get",
            "deployment",
            name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.replicas}",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return -1
    return int(out.stdout.strip())


def _snapshot_exists(name: str, namespace: str) -> bool:
    out = subprocess.run(
        [
            "k3s",
            "kubectl",
            "get",
            "volumesnapshot",
            name,
            "-n",
            namespace,
            "-o",
            "name",
        ],
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and out.stdout.strip().startswith("volumesnapshot.snapshot.storage.k8s.io/")


@pytest.fixture()
def cleanup_sandbox(api_base_url: str, api_headers: dict):
    """Yield a callable that registers sandbox (name, namespace) pairs for teardown."""
    to_delete: list[tuple[str, str]] = []

    def _register(name: str, namespace: str) -> None:
        to_delete.append((name, namespace))

    yield _register

    for name, namespace in to_delete:
        try:
            httpx.delete(
                f"{api_base_url}/api/v1/sandboxes/{name}",
                params={"namespace": namespace},
                headers=api_headers,
                timeout=30,
            )
        except Exception:
            pass


class TestApiPauseResumeFork:
    """Spec 10a — pause/resume/fork via the HTTP API, multi-node cluster.

    Uses kata-qemu-longhorn (only backend that supports snapshot + fork).
    """

    def test_pause_resume_round_trip(
        self,
        api_base_url: str,
        api_headers: dict,
        test_namespace: str,
        cleanup_sandbox,
    ):
        name = "api-pr"
        cleanup_sandbox(name, test_namespace)
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={
                "name": name,
                "image": "alpine:3.20",
                "namespace": test_namespace,
                "backend": "kata-qemu-longhorn",
            },
            headers=api_headers,
            timeout=60,
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        _wait_for_sandbox_ready(api_base_url, api_headers, name, test_namespace)

        pr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{name}/pause",
            json={"namespace": test_namespace},
            headers=api_headers,
            timeout=60,
        )
        assert pr.status_code == 200, pr.text
        # Wait for the scale change to settle.
        deadline = time.time() + 60
        while time.time() < deadline:
            if _replicas(name, test_namespace) == 0:
                break
            time.sleep(1)
        assert _replicas(name, test_namespace) == 0, "deployment did not scale to 0 after pause"

        rr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{name}/resume",
            json={"namespace": test_namespace},
            headers=api_headers,
            timeout=60,
        )
        assert rr.status_code == 200, rr.text
        assert _replicas(name, test_namespace) == 1, "deployment did not scale back to 1 after resume"

    def test_pause_with_snapshot(
        self,
        api_base_url: str,
        api_headers: dict,
        test_namespace: str,
        cleanup_sandbox,
    ):
        name = "api-pr-snap"
        snap = f"{name}-v1"
        cleanup_sandbox(name, test_namespace)
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={
                "name": name,
                "image": "alpine:3.20",
                "namespace": test_namespace,
                "backend": "kata-qemu-longhorn",
            },
            headers=api_headers,
            timeout=60,
        )
        assert r.status_code == 201, r.text
        _wait_for_sandbox_ready(api_base_url, api_headers, name, test_namespace)

        # Spec 10c: ``snapshot`` alone is enough — pvc/snapshot_class derived server-side.
        pr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{name}/pause",
            json={"namespace": test_namespace, "snapshot": snap},
            headers=api_headers,
            timeout=120,
        )
        assert pr.status_code == 200, pr.text

        deadline = time.time() + 90
        snap_seen = False
        while time.time() < deadline:
            if _snapshot_exists(snap, test_namespace):
                snap_seen = True
                break
            time.sleep(2)
        assert snap_seen, f"VolumeSnapshot {snap} never appeared in {test_namespace}"

        try:
            rr = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/resume",
                json={"namespace": test_namespace},
                headers=api_headers,
                timeout=60,
            )
            assert rr.status_code == 200, rr.text
            assert _snapshot_exists(snap, test_namespace), "snapshot should survive resume"
        finally:
            subprocess.run(
                [
                    "k3s",
                    "kubectl",
                    "delete",
                    "volumesnapshot",
                    snap,
                    "-n",
                    test_namespace,
                    "--ignore-not-found",
                    "--wait=false",
                ],
                capture_output=True,
            )

    def test_fork_basic_via_api(
        self,
        api_base_url: str,
        api_headers: dict,
        test_namespace: str,
        cleanup_sandbox,
    ):
        source = "api-fork-src"
        child = "api-fork-child"
        cleanup_sandbox(source, test_namespace)
        cleanup_sandbox(child, test_namespace)

        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={
                "name": source,
                "image": "alpine:3.20",
                "namespace": test_namespace,
                "backend": "kata-qemu-longhorn",
            },
            headers=api_headers,
            timeout=60,
        )
        assert r.status_code == 201, r.text
        _wait_for_sandbox_ready(api_base_url, api_headers, source, test_namespace)

        # Write a marker into the source's persistent state.
        er = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{source}/exec",
            json={"command": "echo fork-marker > /mnt/state/marker"},
            params={"namespace": test_namespace},
            headers=api_headers,
            timeout=30,
        )
        assert er.status_code == 200, er.text

        fr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{source}/fork",
            json={"new_name": child, "namespace": test_namespace},
            headers=api_headers,
            timeout=600,
        )
        assert fr.status_code == 201, fr.text
        body = fr.json()["data"]
        assert body["name"] == child
        assert body["source"] == source
        assert fr.headers.get("location", "").endswith(f"/api/v1/sandboxes/{child}?namespace={test_namespace}")

        _wait_for_sandbox_ready(api_base_url, api_headers, child, test_namespace)

        # Marker survives the fork.
        cr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{child}/exec",
            json={"command": "cat /mnt/state/marker"},
            params={"namespace": test_namespace},
            headers=api_headers,
            timeout=30,
        )
        assert cr.status_code == 200
        assert "fork-marker" in cr.json()["data"]["stdout"]

    def test_fork_into_existing_name_returns_409(
        self,
        api_base_url: str,
        api_headers: dict,
        test_namespace: str,
        cleanup_sandbox,
    ):
        a = "api-fork-conflict-a"
        b = "api-fork-conflict-b"
        cleanup_sandbox(a, test_namespace)
        cleanup_sandbox(b, test_namespace)

        for n in (a, b):
            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes",
                json={
                    "name": n,
                    "image": "alpine:3.20",
                    "namespace": test_namespace,
                    "backend": "kata-qemu-longhorn",
                },
                headers=api_headers,
                timeout=60,
            )
            assert r.status_code == 201, r.text
            _wait_for_sandbox_ready(api_base_url, api_headers, n, test_namespace)

        fr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/{a}/fork",
            json={"new_name": b, "namespace": test_namespace},
            headers=api_headers,
            timeout=600,
        )
        assert fr.status_code == 409, fr.text

    def test_fork_missing_source_returns_404(
        self,
        api_base_url: str,
        api_headers: dict,
        test_namespace: str,
    ):
        fr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/does-not-exist/fork",
            json={"new_name": "child", "namespace": test_namespace},
            headers=api_headers,
            timeout=30,
        )
        assert fr.status_code == 404, fr.text

    def test_fork_missing_new_name_returns_400(
        self,
        api_base_url: str,
        api_headers: dict,
        test_namespace: str,
    ):
        fr = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/whatever/fork",
            json={},
            headers=api_headers,
            timeout=10,
        )
        assert fr.status_code == 400, fr.text


class TestSdkSandboxProxy:
    """End-to-end SDK exercise: Client.create → proxy.pause/resume/fork."""

    def test_sdk_pause_resume_fork_round_trip(
        self,
        api_base_url: str,
        api_key: str,
        test_namespace: str,
        cleanup_sandbox,
    ):
        from k7_sdk import Client

        c = Client(endpoint=api_base_url, api_key=api_key, verify_ssl=False)
        name = "sdk-prf"
        child = "sdk-prf-child"
        cleanup_sandbox(name, test_namespace)
        cleanup_sandbox(child, test_namespace)

        proxy = c.create(
            {
                "name": name,
                "image": "alpine:3.20",
                "namespace": test_namespace,
                "backend": "kata-qemu-longhorn",
            }
        )
        assert proxy.name == name
        _wait_for_sandbox_ready(api_base_url, {"X-API-Key": api_key}, name, test_namespace)

        proxy.pause()
        deadline = time.time() + 60
        while time.time() < deadline and _replicas(name, test_namespace) != 0:
            time.sleep(1)
        assert _replicas(name, test_namespace) == 0

        proxy.resume()
        deadline = time.time() + 60
        while time.time() < deadline and _replicas(name, test_namespace) != 1:
            time.sleep(1)
        assert _replicas(name, test_namespace) == 1

        # Need the pod Running again before exec.
        _wait_for_sandbox_ready(api_base_url, {"X-API-Key": api_key}, name, test_namespace)
        proxy.exec("echo sdk-fork-marker > /mnt/state/marker")

        forked = proxy.fork(child)
        assert forked.name == child
        assert forked.namespace == test_namespace
        _wait_for_sandbox_ready(api_base_url, {"X-API-Key": api_key}, child, test_namespace)

        out = forked.exec("cat /mnt/state/marker")
        assert "sdk-fork-marker" in out["stdout"]


# ---------------------------------------------------------------------------
# Spec 10h: SSRF guard + API-key namespace authorization
# ---------------------------------------------------------------------------


class TestApiSsrfGuard:
    """Control-plane must not fetch OCI manifests from internal/loopback hosts."""

    def test_loopback_image_rejected_and_listener_untouched(
        self, api_base_url: str, api_headers: dict, test_namespace: str
    ):
        port = 8199
        # Tiny python listener with a hit counter. Bound on all interfaces so a
        # buggy control-plane fetch via the node IP would also be visible; the
        # image uses 127.0.0.1 which the guard must reject before any dial.
        counter = Path("/tmp/k7-ssrf-listener-hits")
        if counter.exists():
            counter.unlink()
        listener = subprocess.Popen(
            [
                "python3",
                "-c",
                (
                    "import socket, pathlib\n"
                    f"p=pathlib.Path({str(counter)!r})\n"
                    "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
                    f"s.bind(('0.0.0.0', {port})); s.listen(5); s.settimeout(8)\n"
                    "try:\n"
                    "  c,_=s.accept(); p.write_text('hit'); c.close()\n"
                    "except Exception:\n"
                    "  pass\n"
                    "finally:\n"
                    "  s.close()\n"
                ),
            ],
        )
        try:
            time.sleep(0.4)
            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes",
                json={
                    "name": "ssrf-loopback",
                    "image": f"127.0.0.1:{port}/x:latest",
                    "namespace": test_namespace,
                },
                headers=api_headers,
                timeout=15,
            )
            assert r.status_code >= 400, f"expected error, got {r.status_code}: {r.text}"
            assert "not allowed" in r.text.lower() or "registry" in r.text.lower()
            time.sleep(0.5)
            assert not counter.exists(), "listener received a connection — SSRF guard failed"
        finally:
            listener.kill()
            try:
                listener.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def test_metadata_image_rejected(self, api_base_url: str, api_headers: dict, test_namespace: str):
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={
                "name": "ssrf-metadata",
                "image": "169.254.169.254/latest/meta-data:latest",
                "namespace": test_namespace,
            },
            headers=api_headers,
            timeout=15,
        )
        assert r.status_code >= 400
        assert "not allowed" in r.text.lower() or "registry" in r.text.lower()

    def test_public_image_create_still_works(
        self, api_base_url: str, api_headers: dict, test_namespace: str, cleanup_sandbox
    ):
        name = "ssrf-public-ok"
        cleanup_sandbox(name, test_namespace)
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={"name": name, "image": "alpine:3.21", "namespace": test_namespace},
            headers=api_headers,
            timeout=30,
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        _wait_for_sandbox_ready(api_base_url, api_headers, name, test_namespace)


class TestApiNamespaceAuthz:
    """Scoped API keys may only operate in their listed namespaces."""

    def test_scoped_key_alpha_ok_beta_forbidden(self, api_base_url: str, test_namespace: str, cleanup_sandbox):
        # Ensure namespaces exist (create via unscoped key / kubectl).
        for ns in ("alpha", "beta"):
            subprocess.run(
                ["k3s", "kubectl", "create", "namespace", ns],
                capture_output=True,
                text=True,
            )

        scoped = _generate_test_api_key(name="integ-scoped-alpha", namespaces=["alpha"])
        scoped_headers = {"X-API-Key": scoped}
        unscoped = _generate_test_api_key(name="integ-unscoped")
        unscoped_headers = {"X-API-Key": unscoped}

        name = "ns-authz-sb"
        cleanup_sandbox(name, "alpha")
        cleanup_sandbox(name, "beta")

        # Scoped key: create in alpha OK
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={"name": name, "image": "alpine:3.21", "namespace": "alpha"},
            headers=scoped_headers,
            timeout=30,
        )
        assert r.status_code == 201, f"scoped create alpha failed: {r.text}"

        # List/delete in alpha OK
        lr = httpx.get(
            f"{api_base_url}/api/v1/sandboxes",
            params={"namespace": "alpha"},
            headers=scoped_headers,
            timeout=10,
        )
        assert lr.status_code == 200

        # beta denied
        br = httpx.get(
            f"{api_base_url}/api/v1/sandboxes",
            params={"namespace": "beta"},
            headers=scoped_headers,
            timeout=10,
        )
        assert br.status_code == 403

        br2 = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={"name": name, "image": "alpine:3.21", "namespace": "beta"},
            headers=scoped_headers,
            timeout=15,
        )
        assert br2.status_code == 403

        # all-namespaces list denied
        ar = httpx.get(
            f"{api_base_url}/api/v1/sandboxes",
            headers=scoped_headers,
            timeout=10,
        )
        assert ar.status_code == 403

        # Unscoped key still works on beta
        ur = httpx.get(
            f"{api_base_url}/api/v1/sandboxes",
            params={"namespace": "beta"},
            headers=unscoped_headers,
            timeout=10,
        )
        assert ur.status_code == 200

        # Cleanup via scoped key in alpha
        dr = httpx.delete(
            f"{api_base_url}/api/v1/sandboxes/{name}",
            params={"namespace": "alpha"},
            headers=scoped_headers,
            timeout=30,
        )
        assert dr.status_code == 200, dr.text
