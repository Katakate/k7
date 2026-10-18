"""Tenancy and quotas against the live k7-api.

Every tenant here is created by the test through the admin API and deleted
with ``k7 tenant delete --cascade`` in a ``finally``; its namespaces are then
torn down and waited on like ``test_namespace``. Tenant keys are real
``--tenant`` keys in the k7-api-keys Secret, so every request takes the same
path as a customer's: auth → tenant scope → core → admission → quota lease.
Races go straight to each k7-api pod IP so both replicas contend for the
tenant's quota Lease.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from uuid import uuid4

import httpx as _httpx
import pytest

from k7.deploy.k7_api_keys import KEYS_SECRET, generate, mint_token

from .cluster import KUBECTL, kubectl, system_ns
from .conftest import _DEV_SH, _teardown_namespace
from .test_api import (
    _api_pod_ready,
    _generate_test_api_key,
    _get_api_base_url,
    _keys_kubectl,
    _revoke_test_api_key,
    _tls_verify,
    _wait_key_live,
)
from .test_observability import _scrape_app, _sum

pytestmark = pytest.mark.integration

KFD, KQL = "kata-firecracker-devmapper", "kata-qemu-longhorn"
IMAGE = "alpine:3.21"
SMALL = {"cpu": "250m", "memory": "256Mi"}
TIMEOUT = 300
NODE = os.uname().nodename
TENANT_PREFIX = "it41l-"


def call(method: str, url: str, token: str, *, verify: bool | str | None = None, **kw) -> _httpx.Response:
    kw.setdefault("timeout", TIMEOUT)
    verify = _tls_verify() if verify is None else verify
    return _httpx.request(method, url, headers={"X-API-Key": token}, verify=verify, **kw)


def body(name: str, ns: str, backend: str = KFD, **extra) -> dict:
    return {"name": name, "image": IMAGE, "namespace": ns, "backend": backend, "limits": dict(SMALL), **extra}


def error(r: _httpx.Response) -> dict:
    return r.json()["error"]


def assert_quota_exceeded(r: _httpx.Response, dimension: str, limit: str, usage: str, requested: str) -> None:
    assert r.status_code == 403, r.text
    err = error(r)
    assert err["code"] == "QuotaExceeded", r.text
    assert {k: err["details"][k] for k in ("dimension", "limit", "usage", "requested")} == {
        "dimension": dimension,
        "limit": limit,
        "usage": usage,
        "requested": requested,
    }, r.text


def kjson(*args: str) -> dict:
    return json.loads(kubectl(*args, "-o", "json", check=True).stdout)


def sandboxes_in(ns: str) -> list[str]:
    items = kjson("-n", ns, "get", "deployment", "-l", "katakate.org/sandbox")["items"]
    return sorted(d["metadata"]["name"] for d in items)


def pvcs_in(ns: str) -> list[str]:
    return sorted(p["metadata"]["name"] for p in kjson("-n", ns, "get", "pvc")["items"])


def snapshots_in(ns: str) -> list[str]:
    return sorted(s["metadata"]["name"] for s in kjson("-n", ns, "get", "volumesnapshot")["items"])


def quota_leases() -> list[str]:
    items = kjson("-n", system_ns(), "get", "lease")["items"]
    return [n for n in (i["metadata"]["name"] for i in items) if n.startswith("k7-quota-")]


@dataclass
class Tenant:
    name: str
    namespaces: list[str]
    token: str
    base: str

    @property
    def ns(self) -> str:
        return self.namespaces[0]

    def __call__(self, method: str, path: str, **kw) -> _httpx.Response:
        return call(method, f"{self.base}{path}", self.token, **kw)

    def create(self, name: str, ns: str | None = None, backend: str = KFD, **extra) -> _httpx.Response:
        return self("POST", "/api/v1/sandboxes", json=body(name, ns or self.ns, backend, **extra))

    def wait_ready(self, name: str, ns: str | None = None, timeout: int = 300) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self("GET", f"/api/v1/sandboxes/{name}", params={"namespace": ns or self.ns}, timeout=10)
            if r.status_code == 200 and r.json()["data"].get("ready") == "True":
                return
            time.sleep(2)
        raise TimeoutError(f"sandbox {ns or self.ns}/{name} not ready within {timeout}s")

    def snapshot(self, sandbox: str, snap: str, ns: str | None = None) -> _httpx.Response:
        return self(
            "POST", f"/api/v1/sandboxes/{sandbox}/snapshot", json={"snapshot_name": snap, "namespace": ns or self.ns}
        )

    def wait_snapshot_ready(self, snap: str, ns: str | None = None, timeout: int = 300) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self("GET", f"/api/v1/snapshots/{snap}", params={"namespace": ns or self.ns}, timeout=10)
            if r.status_code == 200 and r.json()["data"].get("ready_to_use"):
                return
            time.sleep(2)
        raise TimeoutError(f"snapshot {ns or self.ns}/{snap} not ready within {timeout}s")


def _wait_api(url: str) -> None:
    for _ in range(30):
        try:
            if _httpx.get(f"{url}/health", verify=_tls_verify(), timeout=3).status_code == 200:
                return
        except _httpx.HTTPError:
            pass
        time.sleep(2)
    pytest.fail(f"k7-api {url}/health not reachable", pytrace=False)


@pytest.fixture(scope="module")
def base() -> str:
    if not _api_pod_ready():
        pytest.fail("k7-api deployment not ready; tenancy tests need the API", pytrace=False)
    url = _get_api_base_url()
    _wait_api(url)
    return url


@pytest.fixture(scope="module")
def admin(base: str) -> Iterator[str]:
    name = f"{TENANT_PREFIX}admin-{uuid4().hex[:6]}"
    try:
        yield _generate_test_api_key(name=name, role="admin")
    finally:
        _revoke_test_api_key(name)


@contextmanager
def tenant(base: str, admin_token: str, *, namespaces: int = 1, **spec) -> Iterator[Tenant]:
    """A test-owned Tenant + ``--tenant`` key; ``tenant delete --cascade`` and namespace teardown in ``finally``."""
    name = f"{TENANT_PREFIX}{uuid4().hex[:6]}"
    nss = [f"{name}-{i}" for i in range(namespaces)]
    created = False
    try:
        r = call(
            "POST", f"{base}/api/v1/tenants", admin_token, json={"name": name, "spec": {"namespaces": nss, **spec}}
        )
        assert r.status_code == 201, r.text
        created = True
        token, _id, _entry = generate(
            _keys_kubectl(), f"{name}-key", role="user", tenant=name, expires_days=1, namespaces=[], nodes=[]
        )
        _wait_key_live(token)
        yield Tenant(name, nss, token, base)
    finally:
        failures = []
        if created and kubectl("get", "tenant", name).returncode == 0:
            out = subprocess.run(
                [str(_DEV_SH), "tenant", "delete", name, "--cascade"], capture_output=True, text=True, timeout=900
            )
            if out.returncode != 0:
                failures.append(f"tenant delete {name} --cascade: {out.stderr or out.stdout}")
        _revoke_test_api_key(f"{name}-key")
        for ns in nss:
            if kubectl("get", "namespace", ns).returncode == 0:
                _teardown_namespace(ns)
        assert not failures, failures


@pytest.fixture()
def make_tenant(base: str, admin: str):
    with ExitStack() as stack:
        yield lambda **kw: stack.enter_context(tenant(base, admin, **kw))


def api_pod_urls() -> tuple[list[str], bool | str]:
    """Each k7-api replica's own URL, and ``verify``: Caddy's TLS port when the sidecar runs, else :8000."""
    pods = kjson("-n", system_ns(), "get", "pods", "-l", "app=k7-api")["items"]
    urls = []
    for p in pods:
        if p["status"].get("phase") != "Running" or not p["status"].get("podIP"):
            continue
        caddy = next((c for c in p["spec"]["containers"] if c["name"] == "caddy"), None)
        ip = p["status"]["podIP"]
        urls.append(f"https://{ip}:{caddy['ports'][0]['containerPort']}" if caddy else f"http://{ip}:8000")
    assert len(urls) >= 2, f"the race needs both k7-api replicas, found {urls}"
    # Pod IPs are not in the API certificate's SANs: TLS to Caddy, unverified.
    return urls, False


def race(t: Tenant, n: int, request) -> Counter:
    """``n`` requests released together, alternating between the k7-api pods; status code counts."""
    urls, verify = api_pod_urls()
    gate = threading.Barrier(n)

    def one(i: int) -> tuple[int, str]:
        method, path, payload = request(i)
        gate.wait()
        r = call(method, f"{urls[i % len(urls)]}{path}", t.token, verify=verify, json=payload)
        return r.status_code, r.text

    with ThreadPoolExecutor(n) as pool:
        results = list(pool.map(one, range(n)))
    codes = Counter(code for code, _ in results)
    unexpected = [text for code, text in results if code not in (201, 403)]
    assert not unexpected, f"race returned non-201/403: {unexpected[:3]}"
    for code, text in results:
        if code == 403:
            assert json.loads(text)["error"]["code"] == "QuotaExceeded", text
    assert quota_leases() == [], "quota leases left behind after the race"
    return codes


# ── A: scope isolation across sandboxes, snapshots, forks and restore ──


@pytest.fixture(scope="module")
def pair(base: str, admin: str) -> Iterator[tuple[Tenant, Tenant]]:
    """Tenant A (two namespaces, a running kql sandbox ``src`` and a snapshot ``src-snap``) and tenant B."""
    with tenant(base, admin, namespaces=2) as a, tenant(base, admin) as b:
        r = a.create("src", backend=KQL, root_disk_size="2Gi")
        assert r.status_code == 201, r.text
        a.wait_ready("src")
        r = a.snapshot("src", "src-snap")
        assert r.status_code == 201, r.text
        a.wait_snapshot_ready("src-snap")
        yield a, b


def _routes(ns: str) -> list[tuple[str, str, str, dict]]:
    """(label, method, path, kwargs) for every namespaced route, aimed at A's ``src`` / ``src-snap`` in ``ns``."""
    q = {"params": {"namespace": ns}}
    return [
        ("list sandboxes", "GET", "/api/v1/sandboxes", q),
        ("get sandbox", "GET", "/api/v1/sandboxes/src", q),
        ("logs", "GET", "/api/v1/sandboxes/src/logs", q),
        ("exec", "POST", "/api/v1/sandboxes/src/exec", {**q, "json": {"command": "id"}}),
        ("create sandbox", "POST", "/api/v1/sandboxes", {"json": body("intruder", ns)}),
        ("pause", "POST", "/api/v1/sandboxes/src/pause", {"json": {"namespace": ns}}),
        ("pause+snapshot", "POST", "/api/v1/sandboxes/src/pause", {"json": {"namespace": ns, "snapshot": "x"}}),
        ("resume", "POST", "/api/v1/sandboxes/src/resume", {"json": {"namespace": ns}}),
        ("fork", "POST", "/api/v1/sandboxes/src/fork", {"json": {"new_name": "intruder", "namespace": ns}}),
        (
            "snapshot create",
            "POST",
            "/api/v1/sandboxes/src/snapshot",
            {"json": {"snapshot_name": "x", "namespace": ns}},
        ),
        ("snapshot list", "GET", "/api/v1/snapshots", q),
        ("snapshot get", "GET", "/api/v1/snapshots/src-snap", q),
        (
            "restore",
            "POST",
            "/api/v1/snapshots/src-snap/restore",
            {"json": {"new_sandbox_name": "intruder", "namespace": ns}},
        ),
        ("snapshot gc", "POST", "/api/v1/snapshots/gc", {"json": {"namespace": ns, "keep_fork_for": "0s"}}),
        ("snapshot delete", "DELETE", "/api/v1/snapshots/src-snap", q),
        ("delete sandbox", "DELETE", "/api/v1/sandboxes/src", q),
        ("delete all sandboxes", "DELETE", "/api/v1/sandboxes", q),
    ]


class TestTenantIsolation:
    def test_cross_tenant_read_write_delete_refused(self, pair):
        """B's key gets 403 on every route in A's namespaces, and A's objects are untouched."""
        a, b = pair
        before = (sandboxes_in(a.ns), pvcs_in(a.ns), snapshots_in(a.ns))
        for ns in a.namespaces:
            for label, method, path, kw in _routes(ns):
                r = b(method, path, **kw)
                assert r.status_code == 403, f"{label} in {ns} with tenant B's key: {r.status_code} {r.text}"
        assert (sandboxes_in(a.ns), pvcs_in(a.ns), snapshots_in(a.ns)) == before
        assert sandboxes_in(a.namespaces[1]) == []
        a.wait_ready("src")  # never paused

    def test_reverse_direction_refused(self, pair):
        a, b = pair
        for label, method, path, kw in _routes(b.ns):
            r = a(method, path, **kw)
            assert r.status_code == 403, f"{label} in {b.ns} with tenant A's key: {r.status_code} {r.text}"
        assert sandboxes_in(b.ns) == []

    def test_fork_and_restore_cannot_cross_into_another_tenant(self, pair):
        """Fork and restore are namespace-local: B naming A's source from B's own namespace finds nothing."""
        a, b = pair
        r = b("POST", "/api/v1/sandboxes/src/fork", json={"new_name": "stolen", "namespace": b.ns})
        assert r.status_code == 404, r.text
        r = b("POST", "/api/v1/snapshots/src-snap/restore", json={"new_sandbox_name": "stolen", "namespace": b.ns})
        assert r.status_code == 404, r.text
        r = b("GET", "/api/v1/snapshots/src-snap", params={"namespace": b.ns})
        assert r.status_code == 404, r.text
        assert sandboxes_in(b.ns) == [] and pvcs_in(b.ns) == []

    def test_cluster_wide_routes_refused_for_tenant_keys(self, pair):
        a, _b = pair
        for method, path, kw in (
            ("GET", "/api/v1/sandboxes", {}),
            ("GET", "/api/v1/snapshots", {"params": {"all_namespaces": "true"}}),
            ("POST", "/api/v1/snapshots/gc", {"json": {"all_namespaces": True}}),
            ("GET", "/api/v1/nodes/storage", {}),
            ("GET", "/api/v1/tenants", {}),
            ("DELETE", f"/api/v1/tenants/{a.name}", {}),
        ):
            r = a(method, path, **kw)
            assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"

    def test_tenant_key_reaches_every_tenant_namespace(self, pair):
        a, _b = pair
        for ns in a.namespaces:
            for path in ("/api/v1/sandboxes", "/api/v1/snapshots"):
                r = a("GET", path, params={"namespace": ns})
                assert r.status_code == 200, f"{path} in {ns}: {r.text}"

    def test_fork_and_restore_within_tenant_carry_identity(self, pair):
        a, _b = pair
        r = a("POST", "/api/v1/sandboxes/src/fork", json={"new_name": "src-fork", "namespace": a.ns})
        assert r.status_code == 201, r.text
        r = a("POST", "/api/v1/snapshots/src-snap/restore", json={"new_sandbox_name": "src-rest", "namespace": a.ns})
        assert r.status_code == 201, r.text
        try:
            for name in ("src", "src-fork", "src-rest"):
                a.wait_ready(name)
            key_ids = set()
            for kind, selector in (
                ("deployment", "katakate.org/sandbox"),
                ("pod", "katakate.org/sandbox"),
                ("pvc", "k7.katakate.org/tenant"),
                ("volumesnapshot", "k7.katakate.org/tenant"),
            ):
                items = kjson("-n", a.ns, "get", kind, "-l", selector)["items"]
                assert items, f"no {kind} in {a.ns}"
                for obj in items:
                    labels = obj["metadata"].get("labels") or {}
                    assert labels.get("k7.katakate.org/tenant") == a.name, f"{kind} {obj['metadata']['name']}: {labels}"
                    assert labels.get("k7.katakate.org/api-key-id"), f"{kind} {obj['metadata']['name']}: {labels}"
                    key_ids.add(labels["k7.katakate.org/api-key-id"])
            assert len(key_ids) == 1, f"objects carry different key ids: {key_ids}"
            assert {"src-fork", "src-rest"} <= set(sandboxes_in(a.ns))
        finally:
            for name in ("src-fork", "src-rest"):
                a("DELETE", f"/api/v1/sandboxes/{name}", params={"namespace": a.ns})

    def test_new_namespace_reaches_existing_key_and_removal_revokes(self, pair, admin):
        a, _b = pair
        extra = f"{a.name}-added"
        try:
            r = call("PATCH", f"{a.base}/api/v1/tenants/{a.name}", admin, json={"namespaces": [*a.namespaces, extra]})
            assert r.status_code == 200, r.text
            r = a("GET", "/api/v1/sandboxes", params={"namespace": extra})
            assert r.status_code == 200, f"same key, newly added namespace: {r.text}"
            r = call("PATCH", f"{a.base}/api/v1/tenants/{a.name}", admin, json={"namespaces": a.namespaces})
            assert r.status_code == 200, r.text
            r = a("GET", "/api/v1/sandboxes", params={"namespace": extra})
            assert r.status_code == 403, f"removed namespace must be refused at once: {r.text}"
        finally:
            if kubectl("get", "namespace", extra).returncode == 0:
                _teardown_namespace(extra)

    def test_double_claimed_namespace_is_500_naming_both(self, pair, admin):
        a, b = pair
        r = call(
            "POST", f"{a.base}/api/v1/tenants", admin, json={"name": f"{a.name}-x", "spec": {"namespaces": [a.ns]}}
        )
        assert r.status_code == 400 and a.name in r.text, f"the API refuses a second claim: {r.text}"
        # A hand-written CR (kubectl apply) bypasses that check; every request there then fails loudly.
        rogue = f"{TENANT_PREFIX}rogue-{uuid4().hex[:6]}"
        cr = {
            "apiVersion": "k7.katakate.org/v1alpha1",
            "kind": "Tenant",
            "metadata": {"name": rogue},
            "spec": {"namespaces": [a.ns]},
        }
        subprocess.run([*KUBECTL, "apply", "-f", "-"], input=json.dumps(cr), text=True, check=True)
        try:
            for who in (a, b):
                r = who("GET", "/api/v1/sandboxes", params={"namespace": a.ns})
                assert r.status_code in (403, 500), r.text
            r = a("GET", "/api/v1/sandboxes", params={"namespace": a.ns})
            assert r.status_code == 500, r.text
            err = error(r)
            assert err["code"] == "TenantConflict" and sorted(err["details"]["tenants"]) == sorted([a.name, rogue])
            r = a("POST", "/api/v1/sandboxes", json=body("blocked", a.ns))
            assert r.status_code == 500, r.text
        finally:
            kubectl("delete", "tenant", rogue, "--ignore-not-found", check=True)
        assert a("GET", "/api/v1/sandboxes", params={"namespace": a.ns}).status_code == 200

    def test_orphan_tenant_key_is_refused(self, base, admin):
        """A key whose tenant was deleted reaches nothing, not everything."""
        with tenant(base, admin) as t:
            assert call("DELETE", f"{base}/api/v1/tenants/{t.name}", admin).status_code == 200
            for params in ({"namespace": t.ns}, {"namespace": "default"}, {}):
                r = t("GET", "/api/v1/sandboxes", params=params)
                assert r.status_code == 403 and "does not exist" in r.text, f"{params}: {r.text}"


class TestTenantNodes:
    def test_sandboxes_land_on_tenant_nodes_without_a_pin(self, make_tenant):
        t = make_tenant(nodes=[NODE])
        node = kjson("get", "node", NODE)
        assert node["metadata"]["labels"].get("k7.katakate.org/tenant") == t.name
        assert any(x.get("key") == "k7.katakate.org/tenant" for x in node["spec"].get("taints") or [])
        r = t.create("placed")
        assert r.status_code == 201, r.text
        t.wait_ready("placed")
        pod = kjson("-n", t.ns, "get", "pod", "-l", "katakate.org/sandbox=placed")["items"][0]
        assert pod["spec"]["nodeSelector"].get("k7.katakate.org/tenant") == t.name
        assert pod["spec"]["nodeName"] == NODE

    def test_tenant_delete_releases_its_nodes(self, base, admin):
        with tenant(base, admin, nodes=[NODE]):
            pass
        node = kjson("get", "node", NODE)
        assert "k7.katakate.org/tenant" not in (node["metadata"].get("labels") or {})
        assert not any(x.get("key") == "k7.katakate.org/tenant" for x in node["spec"].get("taints") or [])


# ── C: quota boundaries, one dimension at a time ──


class TestQuotaBoundaries:
    def test_sandboxes(self, make_tenant):
        t = make_tenant(quotas={"sandboxes": 2})
        for i in range(2):
            assert t.create(f"s{i}").status_code == 201
        assert_quota_exceeded(t.create("s2"), "sandboxes", "2", "2", "1")
        assert sandboxes_in(t.ns) == ["s0", "s1"]
        rejected = _sum(_scrape_app("k7-api"), "k7_quota_rejections_total", tenant=t.name, dimension="sandboxes")
        assert rejected == 1, "the refusal is counted once, on the replica that served it"

    def test_sandboxes_across_namespaces(self, make_tenant):
        t = make_tenant(namespaces=2, quotas={"sandboxes": 2})
        assert t.create("s0", t.namespaces[0]).status_code == 201
        assert t.create("s1", t.namespaces[1]).status_code == 201
        assert_quota_exceeded(t.create("s2", t.namespaces[1]), "sandboxes", "2", "2", "1")

    def test_vcpu(self, make_tenant):
        t = make_tenant(quotas={"vcpu": "1"})
        for i in range(2):
            assert t.create(f"c{i}", limits={"cpu": "500m", "memory": "256Mi"}).status_code == 201
        r = t.create("c2", limits={"cpu": "500m", "memory": "256Mi"})
        assert_quota_exceeded(r, "vcpu", "1", "1", "500m")

    def test_vcpu_quota_needs_a_cpu_limit(self, make_tenant):
        t = make_tenant(quotas={"vcpu": "1"})
        r = t("POST", "/api/v1/sandboxes", json={**body("nolimit", t.ns), "limits": {"memory": "256Mi"}})
        assert r.status_code == 400 and "cpu" in r.text, r.text
        assert sandboxes_in(t.ns) == []

    def test_memory(self, make_tenant):
        t = make_tenant(quotas={"memory": "1Gi"})
        for i in range(2):
            assert t.create(f"m{i}", limits={"cpu": "250m", "memory": "512Mi"}).status_code == 201
        r = t.create("m2", limits={"cpu": "250m", "memory": "512Mi"})
        assert_quota_exceeded(r, "memory", "1Gi", "1Gi", "512Mi")

    def test_disk_kfd(self, make_tenant):
        """kfd reserves its 10 GB devmapper root per sandbox."""
        t = make_tenant(quotas={"disk": "25G"})
        for i in range(2):
            assert t.create(f"d{i}").status_code == 201
        assert_quota_exceeded(t.create("d2"), "disk", "25G", "20G", "10G")

    def test_disk_kql_rolls_back_the_refused_pvc(self, make_tenant):
        t = make_tenant(quotas={"disk": "5Gi"})
        for i in range(2):
            assert t.create(f"q{i}", backend=KQL, root_disk_size="2Gi").status_code == 201
        before = pvcs_in(t.ns)
        r = t.create("q2", backend=KQL, root_disk_size="2Gi")
        assert_quota_exceeded(r, "disk", "5Gi", "4Gi", "2Gi")
        assert pvcs_in(t.ns) == before and sandboxes_in(t.ns) == ["q0", "q1"]

    def test_snapshot_storage(self, make_tenant):
        t = make_tenant(quotas={"snapshotStorage": "3Gi"})
        assert t.create("src", backend=KQL, root_disk_size="2Gi").status_code == 201
        t.wait_ready("src")
        r = t.snapshot("src", "snap0")
        assert r.status_code == 201, r.text
        r = t.snapshot("src", "snap1")
        assert_quota_exceeded(r, "snapshotStorage", "3Gi", "2Gi", "2Gi")
        r = t("POST", "/api/v1/sandboxes/src/pause", json={"namespace": t.ns, "snapshot": "snap2"})
        assert_quota_exceeded(r, "snapshotStorage", "3Gi", "2Gi", "2Gi")
        assert snapshots_in(t.ns) == ["snap0"]
        t.wait_ready("src")  # the refused pause+snapshot left it running

    def test_fork_over_quota_leaves_nothing_behind(self, make_tenant):
        t = make_tenant(quotas={"sandboxes": 1})
        assert t.create("src", backend=KQL, root_disk_size="2Gi").status_code == 201
        t.wait_ready("src")
        before = (pvcs_in(t.ns), snapshots_in(t.ns))
        r = t("POST", "/api/v1/sandboxes/src/fork", json={"new_name": "f", "namespace": t.ns})
        assert_quota_exceeded(r, "sandboxes", "1", "1", "1")
        assert (pvcs_in(t.ns), snapshots_in(t.ns)) == before and sandboxes_in(t.ns) == ["src"]

    def test_restore_over_quota_leaves_nothing_behind(self, make_tenant):
        t = make_tenant(quotas={"sandboxes": 1})
        assert t.create("src", backend=KQL, root_disk_size="2Gi").status_code == 201
        t.wait_ready("src")
        assert t.snapshot("src", "snap").status_code == 201
        t.wait_snapshot_ready("snap")
        before = (pvcs_in(t.ns), snapshots_in(t.ns))
        r = t("POST", "/api/v1/snapshots/snap/restore", json={"new_sandbox_name": "r", "namespace": t.ns})
        assert_quota_exceeded(r, "sandboxes", "1", "1", "1")
        assert (pvcs_in(t.ns), snapshots_in(t.ns)) == before and sandboxes_in(t.ns) == ["src"]

    def test_resume_is_not_a_create(self, make_tenant):
        """Pause keeps the reservation (reserved-not-used), so resume never trips the quota."""
        t = make_tenant(quotas={"sandboxes": 1})
        assert t.create("p").status_code == 201
        assert t("POST", "/api/v1/sandboxes/p/pause", json={"namespace": t.ns}).status_code == 200
        assert_quota_exceeded(t.create("p2"), "sandboxes", "1", "1", "1")
        assert t("POST", "/api/v1/sandboxes/p/resume", json={"namespace": t.ns}).status_code == 200

    def test_freeing_quota_admits_again(self, make_tenant):
        t = make_tenant(quotas={"sandboxes": 1})
        assert t.create("a").status_code == 201
        assert t.create("b").status_code == 403
        assert t("DELETE", "/api/v1/sandboxes/a", params={"namespace": t.ns}).status_code == 200
        assert t.create("b").status_code == 201, "a deleted sandbox frees its quota"

    def test_max_sandbox_size_and_default_size(self, make_tenant):
        t = make_tenant(defaultSize={"cpu": "500m", "memory": "512Mi"}, limits={"maxSandboxSize": {"cpu": "1"}})
        r = t.create("big", limits={"cpu": "2", "memory": "256Mi"})
        assert r.status_code == 400 and "maxSandboxSize.cpu" in r.text, r.text
        r = t("POST", "/api/v1/sandboxes", json={"name": "dflt", "image": IMAGE, "namespace": t.ns, "backend": KFD})
        assert r.status_code == 201, r.text
        dep = kjson("-n", t.ns, "get", "deployment", "dflt")
        lim = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "sandbox")["resources"]
        assert lim["limits"]["cpu"] == "500m" and lim["limits"]["memory"] == "512Mi", lim

    def test_lease_timeout_is_503_and_nothing_created(self, make_tenant):
        t = make_tenant(quotas={"sandboxes": 5})
        lease = f"k7-quota-{t.name}"
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
        held = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": lease, "namespace": system_ns()},
            "spec": {
                "holderIdentity": "it41l-foreign",
                "leaseDurationSeconds": 120,
                "acquireTime": now,
                "renewTime": now,
            },
        }
        subprocess.run([*KUBECTL, "create", "-f", "-"], input=json.dumps(held), text=True, check=True)
        try:
            t0 = time.monotonic()
            r = t.create("blocked")
            assert r.status_code == 503 and error(r)["code"] == "QuotaLockTimeout", r.text
            assert time.monotonic() - t0 >= 5, "gave up before the 5 s acquire timeout"
            assert sandboxes_in(t.ns) == [] and pvcs_in(t.ns) == []
            holder = kjson("-n", system_ns(), "get", "lease", lease)["spec"]["holderIdentity"]
            assert holder == "it41l-foreign", "the API must never take or delete a live foreign lease"
        finally:
            kubectl("-n", system_ns(), "delete", "lease", lease, "--ignore-not-found", check=True)
        assert t.create("after").status_code == 201

    def test_status_usage_converges_and_backstop_resource_quota(self, make_tenant):
        t = make_tenant(quotas={"sandboxes": 3, "disk": "50G"})
        for i in range(2):
            assert t.create(f"u{i}").status_code == 201
        deadline, usage = time.time() + 75, {}
        while time.time() < deadline:
            usage = (kjson("get", "tenant", t.name).get("status") or {}).get("usage") or {}
            if usage.get("sandboxes") == "2":
                break
            time.sleep(3)
        assert usage.get("sandboxes") == "2" and usage.get("disk") == "20G", f"status.usage after 75 s: {usage}"
        rq = kjson("-n", t.ns, "get", "resourcequota", "k7-tenant-quota")
        assert rq["spec"]["hard"] == {"count/deployments.apps": "3", "requests.storage": "50G"}


# ── C: concurrent creates racing the quota through both API replicas ──


class TestQuotaRaces:
    def _create(self, t: Tenant, backend: str = KFD, **extra):
        return lambda i: (
            "POST",
            "/api/v1/sandboxes",
            body(f"r{i}", t.namespaces[i % len(t.namespaces)], backend, **extra),
        )

    def _count(self, t: Tenant) -> int:
        return sum(len(sandboxes_in(ns)) for ns in t.namespaces)

    def test_sandboxes_20_against_10(self, make_tenant):
        t = make_tenant(namespaces=2, quotas={"sandboxes": 10})
        codes = race(t, 20, self._create(t))
        assert codes == Counter({201: 10, 403: 10}) and self._count(t) == 10, codes

    def test_vcpu(self, make_tenant):
        t = make_tenant(namespaces=2, quotas={"vcpu": "2"})
        codes = race(t, 20, self._create(t, limits={"cpu": "500m", "memory": "256Mi"}))
        assert codes == Counter({201: 4, 403: 16}) and self._count(t) == 4, codes

    def test_memory(self, make_tenant):
        t = make_tenant(namespaces=2, quotas={"memory": "1Gi"})
        codes = race(t, 20, self._create(t, limits={"cpu": "250m", "memory": "256Mi"}))
        assert codes == Counter({201: 4, 403: 16}) and self._count(t) == 4, codes

    def test_disk_kfd(self, make_tenant):
        t = make_tenant(namespaces=2, quotas={"disk": "50G"})
        codes = race(t, 20, self._create(t))
        assert codes == Counter({201: 5, 403: 15}) and self._count(t) == 5, codes

    def test_disk_kql(self, make_tenant):
        t = make_tenant(namespaces=2, quotas={"disk": "10Gi"})
        codes = race(t, 20, self._create(t, backend=KQL, root_disk_size="2Gi"))
        assert codes == Counter({201: 5, 403: 15}) and self._count(t) == 5, codes
        for ns in t.namespaces:
            claimed = {
                v["persistentVolumeClaim"]["claimName"]
                for d in kjson("-n", ns, "get", "deployment", "-l", "katakate.org/sandbox")["items"]
                for v in d["spec"]["template"]["spec"].get("volumes") or []
                if "persistentVolumeClaim" in v
            }
            assert set(pvcs_in(ns)) == claimed, f"refused creates left PVCs in {ns}"

    def test_snapshots(self, make_tenant):
        t = make_tenant(quotas={"snapshotStorage": "5Gi"})
        assert t.create("src", backend=KQL, root_disk_size="2Gi").status_code == 201
        t.wait_ready("src")
        codes = race(
            t, 10, lambda i: ("POST", "/api/v1/sandboxes/src/snapshot", {"snapshot_name": f"s{i}", "namespace": t.ns})
        )
        assert codes == Counter({201: 2, 403: 8}) and len(snapshots_in(t.ns)) == 2, codes


# ── B: production mode, on a test-owned copy of the k7-api pod ──


@contextmanager
def _probe_pod(env: dict[str, str]) -> Iterator[str]:
    """A test-owned copy of the k7-api pod template with ``env`` overridden; yields its name."""
    dep = kjson("-n", system_ns(), "get", "deployment", "k7-api")
    spec = dep["spec"]["template"]["spec"]
    spec.pop("affinity", None)
    spec.pop("topologySpreadConstraints", None)
    spec["restartPolicy"] = "Never"
    for c in spec["containers"]:
        if c["name"] == "k7-api":
            c["env"] = [e for e in c.get("env") or [] if e["name"] not in env] + [
                {"name": k, "value": v} for k, v in env.items()
            ]
            for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
                c.pop(probe, None)
    name = f"{TENANT_PREFIX}api-probe-{uuid4().hex[:6]}"
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": system_ns(), "labels": {"app": "k7-api-probe"}},
        "spec": spec,
    }
    subprocess.run([*KUBECTL, "create", "-f", "-"], input=json.dumps(pod), text=True, check=True)
    try:
        yield name
    finally:
        kubectl("-n", system_ns(), "delete", "pod", name, "--ignore-not-found", "--wait=false", check=True)


def _k7_api_terminated(name: str) -> bool:
    st = kjson("-n", system_ns(), "get", "pod", name)["status"]
    return any(
        (s.get("state") or {}).get("terminated") for s in st.get("containerStatuses") or [] if s["name"] == "k7-api"
    )


@contextmanager
def _api_probe(env: dict[str, str]) -> Iterator[str]:
    """Run the probe pod once; yield the k7-api container's logs after it exits (it must refuse to start)."""
    with _probe_pod(env) as name:
        deadline = time.time() + 180
        while not _k7_api_terminated(name):
            if time.time() > deadline:
                pytest.fail(f"production-mode probe {name} is still running after 180 s; it must refuse to start")
            time.sleep(2)
        yield kubectl("-n", system_ns(), "logs", name, "-c", "k7-api", check=True).stdout


@contextmanager
def _running_api_probe(env: dict[str, str]) -> Iterator[str]:
    """Run the probe pod and yield its own URL once ``/health`` answers (TLS to Caddy, unverified)."""
    with _probe_pod(env) as name:
        deadline = time.time() + 180
        while True:
            if _k7_api_terminated(name):
                logs = kubectl("-n", system_ns(), "logs", name, "-c", "k7-api").stdout
                pytest.fail(f"probe {name} exited; it should be serving:\n{logs}")
            pod = kjson("-n", system_ns(), "get", "pod", name)
            caddy = next((c for c in pod["spec"]["containers"] if c["name"] == "caddy"), None)
            ip = pod["status"].get("podIP")
            url = f"https://{ip}:{caddy['ports'][0]['containerPort']}" if caddy else f"http://{ip}:8000"
            try:
                if ip and _httpx.get(f"{url}/health", verify=False, timeout=5).status_code == 200:
                    yield url
                    return
            except _httpx.TransportError:
                pass
            if time.time() > deadline:
                pytest.fail(f"probe {name} did not serve /health within 180 s")
            time.sleep(2)


@contextmanager
def legacy_key(**scope) -> Iterator[tuple[str, str]]:
    """A key written straight to the Secret the way `k7 api keys migrate` leaves a key from before roles
    (``generate`` refuses such scopes); ``(name, token)``, revoked in ``finally``."""
    name = f"{TENANT_PREFIX}legacy-{uuid4().hex[:6]}"
    token, key_id, digest = mint_token()
    now = int(time.time())
    entry = {"name": name, "hash": digest, "created": now, "expires": now + 86400, **scope}
    _keys_kubectl().patch(KEYS_SECRET, {key_id: json.dumps(entry)})
    try:
        yield name, token
    finally:
        _revoke_test_api_key(name)


def wait_key_seen(url: str, token: str) -> None:
    """Until ``url`` (one replica) has polled the key store and knows ``token`` (any answer but 401)."""
    deadline = time.time() + 30
    while call("GET", f"{url}/api/v1/auth/whoami", token, verify=False).status_code == 401:
        assert time.time() < deadline, f"{url} never saw the key"
        time.sleep(1)


UNSCOPED_LEGACY = {"role": "admin", "unscoped_legacy": True}
# What a migrated unscoped key would reach as admin: key minting, role bindings, remote install, sandboxes.
ADMIN_REACH = [
    ("POST", "/api/v1/keys", {"json": {"name": f"{TENANT_PREFIX}minted", "role": "admin", "expires_days": 1}}),
    ("GET", "/api/v1/keys", {}),
    ("GET", "/api/v1/role-bindings", {}),
    ("PUT", "/api/v1/role-bindings", {"json": {"bindings": [], "deny": []}}),
    ("POST", "/api/v1/install", {"json": {"inventory": "x"}}),
    ("POST", "/api/v1/tenants", {"json": {"name": f"{TENANT_PREFIX}minted", "spec": {"namespaces": ["x"]}}}),
    ("GET", "/api/v1/sandboxes", {"params": {"namespace": "default"}}),
    ("GET", "/api/v1/auth/whoami", {}),
]


class TestProductionMode:
    def test_mode_is_reported(self, base):
        r = _httpx.get(f"{base}/health", verify=_tls_verify(), timeout=10)
        assert r.headers["X-K7-Mode"] == "development" and r.json()["mode"] == "development", r.text

    @pytest.mark.parametrize(
        "scope",
        [UNSCOPED_LEGACY, {"role": "user", "namespaces": ["default"]}, {}],
        ids=["unscoped-legacy", "ns-only", "no-role"],
    )
    def test_refuses_to_start_with_an_unscoped_key_and_names_it(self, base, scope):
        prod = {"K7_API_MODE": "production", "K7_API_TRANSPORT": "https"}
        with legacy_key(**scope) as (name, _token), _api_probe(prod) as logs:
            assert "production mode refuses to start: API key(s)" in logs, logs
            assert f"({name})" in logs and "--role user --tenant T" in logs, logs

    def test_development_lets_an_unscoped_legacy_key_act_as_admin(self, base, admin):
        with legacy_key(**UNSCOPED_LEGACY) as (name, token):
            _wait_key_live(token)
            me = call("GET", f"{base}/api/v1/auth/whoami", token)
            assert me.status_code == 200 and me.json()["data"]["grants"] == ["admin"], me.text
            for method, path, kw in ADMIN_REACH[1:3]:
                r = call(method, f"{base}{path}", token, **kw)
                assert r.status_code == 200, (path, r.text)
            listed = call("GET", f"{base}/api/v1/keys", admin).json()["data"]
            assert any(k["name"] == name and k.get("unscoped_legacy") for k in listed), listed

    def test_production_refuses_an_unscoped_legacy_key_on_every_route(self, base, admin):
        env = {"K7_API_MODE": "production", "K7_API_TRANSPORT": "https", "K7_API_ENABLE_REMOTE_INSTALL": "0"}
        with _running_api_probe(env) as url:
            wait_key_seen(url, admin)
            assert call("GET", f"{url}/api/v1/auth/whoami", admin, verify=False).status_code == 200
            with legacy_key(**UNSCOPED_LEGACY) as (name, token):
                wait_key_seen(url, token)
                for method, path, kw in ADMIN_REACH:
                    r = call(method, f"{url}{path}", token, verify=False, **kw)
                    msg = error(r)["message"]
                    assert r.status_code == 403 and r.headers["X-K7-Mode"] == "production", (path, r.text)
                    assert "production mode refuses" in msg and "unscoped_legacy" in msg, (path, msg)
                    assert "--role user --tenant T" in msg and f"k7 revoke-api-key {name}" in msg, (path, msg)
            with legacy_key(role="user", namespaces=["default"]) as (_name, token):
                wait_key_seen(url, token)
                r = call("GET", f"{url}/api/v1/sandboxes", token, verify=False, params={"namespace": "default"})
                assert r.status_code == 403 and "k7 tenant adopt" in error(r)["message"], r.text
        listed = call("GET", f"{base}/api/v1/keys", admin).json()["data"]
        assert not [k for k in listed if k["name"] == f"{TENANT_PREFIX}minted"], "a refused key minted a key"
        assert kubectl("get", "tenant", f"{TENANT_PREFIX}minted").returncode != 0

    def test_refuses_insecure_http(self, base):
        with _api_probe({"K7_API_MODE": "production", "K7_API_TRANSPORT": "insecure-http"}) as logs:
            assert "production mode refuses K7_API_TRANSPORT='insecure-http'" in logs, logs

    def test_refuses_remote_install(self, base):
        with _api_probe({"K7_API_MODE": "production", "K7_API_ENABLE_REMOTE_INSTALL": "1"}) as logs:
            assert "production mode refuses K7_API_ENABLE_REMOTE_INSTALL=1" in logs, logs

    def test_unknown_mode_refuses_to_start(self, base):
        with _api_probe({"K7_API_MODE": "staging"}) as logs:
            assert "K7_API_MODE='staging'; allowed: production, development" in logs, logs
