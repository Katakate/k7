"""Observability on the live cluster: metrics, JSON logs and request ids.

- Port 9464 on k7-api and k7-agent answers only the pods named by the
  install's ``--metrics-scraper`` (nobody by default, Prometheus with
  ``--monitoring``); the API port stays open.
- Both registries expose the metric families, real ops move them, and no
  live series carries a per-sandbox label.
- k7-api and k7-agent log one JSON object per line, and one request id
  follows a k7d VM op from the API into the k7-agent on the sandbox's node
  (k7-api is remote-only, so that is the agent hop on every cluster size).
- ``k7 install --monitoring``: Prometheus loaded every K7 rule, scrapes every
  K7 pod, and Grafana's sidecar loaded the K7 dashboards.

Metric values are read over each pod's loopback (``kubectl exec``), which no
network policy covers; the probe pods test the policy itself.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx as _httpx
import pytest
import yaml
from prometheus_client.parser import text_string_to_metric_families

from k7 import logging as k7_logging
from k7.deploy import k7_metrics_policy
from k7.metrics import FORBIDDEN_LABELS

from .cluster import HAS_CLUSTER, KUBECTL, kubectl, system_ns
from .test_api import _api_pod_ready, _generate_test_api_key, _get_api_base_url, _revoke_test_api_key, httpx
from .test_k7d_api import requires_k7d

pytestmark = pytest.mark.integration

PORT = k7_metrics_policy.METRICS_PORT
PROBE_IMAGE = "alpine:3.20"
OBSERVABILITY = Path(__file__).resolve().parents[2] / "src" / "k7" / "deploy" / "observability"
PROMETHEUS = "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:http-web/proxy"
_SCRAPE = (
    "import sys, urllib.request; "
    f"sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:{PORT}/metrics', timeout=10).read().decode())"
)
CONTAINER = {"k7-api": "k7-api", "k7-agent": "k7-agent"}
_GRAFANA_UIDS = """
import base64, json, os, urllib.request
auth = base64.b64encode(f"{os.environ['REQ_USERNAME']}:{os.environ['REQ_PASSWORD']}".encode()).decode()
req = urllib.request.Request("http://127.0.0.1:3000/api/search?type=dash-db", headers={"Authorization": f"Basic {auth}"})
print(json.dumps([d["uid"] for d in json.load(urllib.request.urlopen(req, timeout=10))]))
"""


def _json(*args: str) -> dict:
    return json.loads(kubectl(*args, "-o", "json", check=True).stdout)


def _pods(app: str) -> list[dict]:
    pods = _json("-n", system_ns(), "get", "pods", "-l", f"app={app}", "--field-selector", "status.phase=Running")
    assert pods["items"], f"no Running {app} pod in {system_ns()}"
    return pods["items"]


def _scrape(pod: dict) -> str:
    app = pod["metadata"]["labels"]["app"]
    out = kubectl(
        "-n", system_ns(), "exec", pod["metadata"]["name"], "-c", CONTAINER[app], "--", "python3", "-c", _SCRAPE
    )
    assert out.returncode == 0, f"{pod['metadata']['name']} :{PORT}/metrics over loopback failed: {out.stderr}"
    return out.stdout


def _families(text: str) -> set[str]:
    return {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")}


def _samples(texts: list[str]) -> list[tuple[str, dict[str, str], float]]:
    return [(s.name, s.labels, s.value) for t in texts for f in text_string_to_metric_families(t) for s in f.samples]


def _sum(texts: list[str], name: str, **labels: str) -> float:
    return sum(v for n, lab, v in _samples(texts) if n == name and labels.items() <= lab.items())


def _scrape_app(app: str) -> list[str]:
    return [_scrape(p) for p in _pods(app)]


def _logs(pod: dict, since: str = "10m") -> list[dict]:
    app = pod["metadata"]["labels"]["app"]
    out = kubectl("-n", system_ns(), "logs", pod["metadata"]["name"], "-c", CONTAINER[app], f"--since={since}")
    assert out.returncode == 0, out.stderr
    lines = []
    for line in out.stdout.splitlines():
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            raise AssertionError(f"{pod['metadata']['name']} wrote a non-JSON log line: {line!r}") from None
    return lines


def _installed_policy() -> tuple[str, dict]:
    """The /metrics policy as installed: ``("cilium", CNP)`` or ``("flannel", List)``."""
    cnp = kubectl("-n", system_ns(), "get", "ciliumnetworkpolicy", "k7-metrics", "-o", "json")
    if cnp.returncode == 0:
        return "cilium", json.loads(cnp.stdout)
    nps = [kubectl("-n", system_ns(), "get", "networkpolicy", f"{a}-metrics", "-o", "json") for a in CONTAINER]
    assert all(np.returncode == 0 for np in nps), (
        f"neither CiliumNetworkPolicy k7-metrics nor NetworkPolicies k7-api-metrics/k7-agent-metrics in "
        f"{system_ns()}: `k7 install` renders one of them ({[np.stderr for np in nps]})"
    )
    return "flannel", {"items": [json.loads(np.stdout) for np in nps]}


def _scraper_of(cni: str, policy: dict) -> k7_metrics_policy.Scraper | None:
    if cni == "cilium":
        allow = policy["spec"].get("ingress")
        if not allow:
            return None
        labels = dict(allow[0]["fromEndpoints"][0]["matchLabels"])
        return labels.pop(k7_metrics_policy.NS_LABEL), labels
    rules = policy["items"][0]["spec"]["ingress"][1:]
    if not rules:
        return None
    source = rules[0]["from"][0]
    return source["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], source["podSelector"][
        "matchLabels"
    ]


def _probe_pod(name: str, namespace: str, labels: dict[str, str]) -> None:
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "terminationGracePeriodSeconds": 0,
            "containers": [{"name": "probe", "image": PROBE_IMAGE, "command": ["sleep", "3600"]}],
        },
    }
    subprocess.run([*KUBECTL, "apply", "-f", "-"], input=json.dumps(pod), text=True, capture_output=True, check=True)
    kubectl("-n", namespace, "wait", "--for=condition=Ready", f"pod/{name}", "--timeout=180s", check=True)


def _reaches(pod: str, namespace: str, url: str) -> bool:
    out = kubectl("-n", namespace, "exec", pod, "--", "wget", "-q", "-T", "5", "-O", "/dev/null", url)
    return out.returncode == 0


@pytest.fixture(scope="module")
def api_base_url() -> str:
    if not _api_pod_ready():
        pytest.skip("k7-api deployment not ready")
    return _get_api_base_url()


@pytest.fixture(scope="module")
def api_key() -> Iterator[str]:
    name = f"obs-{uuid4().hex[:8]}"
    token = _generate_test_api_key(name=name)
    try:
        yield token
    finally:
        _revoke_test_api_key(name)


@pytest.fixture(scope="module")
def api_headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


class TestMetricsPolicy:
    def test_installed_policy_is_the_rendered_one(self):
        """The cluster holds exactly what k7_metrics_policy renders for its own scraper."""
        cni, policy = _installed_policy()
        scraper = _scraper_of(cni, policy)
        if cni == "cilium":
            want = k7_metrics_policy.cilium_policy(system_ns(), scraper)
            assert policy["spec"] == want["spec"]
        else:
            acme = 80 in [p["port"] for p in policy["items"][0]["spec"]["ingress"][0]["ports"]]
            want_items = k7_metrics_policy.network_policies(system_ns(), scraper, acme=acme)
            assert [np["spec"] for np in policy["items"]] == [np["spec"] for np in want_items]

    def test_only_the_scraper_reaches_9464(self, test_namespace: str):
        """A pod in another namespace and an unlabelled pod in the scraper's
        namespace are refused on 9464 but reach the API port; the scraper reaches 9464."""
        cni, policy = _installed_policy()
        scraper = _scraper_of(cni, policy)
        targets = [p["status"]["podIP"] for app in CONTAINER for p in _pods(app)]
        api_health = f"http://{_pods('k7-api')[0]['status']['podIP']}:8000/health"
        created_ns = None
        probes = [("obs-outsider", test_namespace, {})]
        if scraper is not None:
            scraper_ns, labels = scraper
            if kubectl("get", "namespace", scraper_ns).returncode != 0:
                kubectl("create", "namespace", scraper_ns, check=True)
                created_ns = scraper_ns
            suffix = uuid4().hex[:6]
            probes += [(f"obs-stranger-{suffix}", scraper_ns, {}), (f"obs-scraper-{suffix}", scraper_ns, labels)]
        try:
            for name, ns, labels in probes:
                _probe_pod(name, ns, labels)
            for name, ns, labels in probes:
                assert _reaches(name, ns, api_health), f"{ns}/{name} cannot reach {api_health}: the probe is broken"
                for ip in targets:
                    reached = _reaches(name, ns, f"http://{ip}:{PORT}/metrics")
                    assert reached == bool(labels), f"{ns}/{name} labels={labels} -> {ip}:{PORT}: reached={reached}"
            if cni == "cilium":
                with pytest.raises((_httpx.ConnectError, _httpx.TimeoutException)):
                    _httpx.get(f"http://{targets[0]}:{PORT}/metrics", timeout=5)
        finally:
            for name, ns, _labels in probes:
                kubectl("-n", ns, "delete", "pod", name, "--wait=false")
            if created_ns:
                kubectl("delete", "namespace", created_ns, "--wait=false")


class TestMetricsContent:
    API_FAMILIES = {
        "k7_http_requests_total",
        "k7_http_request_duration_seconds",
        "k7_operation_duration_seconds",
        "k7_operation_errors_total",
        "k7_auth_failures_total",
        "k7_key_store_last_sync_timestamp_seconds",
        "k7_kube_api_errors_total",
        "k7_quota_rejections_total",
        "k7_rate_limited_total",
        "k7_sandboxes",
        "k7_api_info",
        "k7_tls_cert_expiry_timestamp_seconds",
        "process_resident_memory_bytes",
    }
    AGENT_FAMILIES = {
        "k7d_rpc_duration_seconds",
        "k7d_rpc_errors_total",
        "k7_storage_pool_bytes",
        "k7_vm_count",
        "process_resident_memory_bytes",
    }

    def test_api_families_and_gauges(self):
        texts = _scrape_app("k7-api")
        for text in texts:
            missing = self.API_FAMILIES - _families(text)
            assert not missing, f"k7-api /metrics lacks {sorted(missing)}"
            assert "k7_http_requests_created" not in text, "_created series are disabled"
        assert all(_sum([t], "k7_api_info") == 1 for t in texts)
        for reason in ("missing", "invalid", "expired", "forbidden", "oidc_invalid"):
            assert any(n == "k7_auth_failures_total" and lab == {"reason": reason} for n, lab, _ in _samples(texts))
        now = time.time()
        for t in texts:
            synced = _sum([t], "k7_key_store_last_sync_timestamp_seconds")
            assert now - 60 < synced <= now + 5, f"key store last synced {now - synced:.0f}s ago"
        if Path("/etc/k7/api_endpoint").read_text().startswith("https://"):
            for t in texts:
                expiry = [v for n, _lab, v in _samples([t]) if n == "k7_tls_cert_expiry_timestamp_seconds"]
                assert len(expiry) == 1 and expiry[0] > now, f"TLS expiry gauge: {expiry}"

    def test_agent_families_and_gauges(self):
        pods = _pods("k7-agent")
        texts = [_scrape(p) for p in pods]
        k7d = kubectl("get", "runtimeclass", "k7").returncode == 0
        for pod, text in zip(pods, texts, strict=True):
            want = self.AGENT_FAMILIES | ({"k7d_daemon_up"} if k7d else set())
            missing = want - _families(text)
            assert not missing, f"{pod['metadata']['name']} /metrics lacks {sorted(missing)}"
            if k7d:
                assert _sum([text], "k7d_daemon_up") == 1, f"k7d down on {pod['spec']['nodeName']}"
                assert _sum([text], "k7_storage_pool_bytes", pool="k7d_disks", kind="size") > 0

    def test_no_series_carries_a_sandbox_label(self):
        texts = _scrape_app("k7-api") + _scrape_app("k7-agent")
        leaked = {(n, k) for n, lab, _ in _samples(texts) for k in lab if k in FORBIDDEN_LABELS}
        assert not leaked, f"per-sandbox labels on live series: {sorted(leaked)}"

    def test_requests_move_http_and_auth_counters(self, api_base_url: str, api_headers: dict):
        before = _scrape_app("k7-api")
        assert httpx.get(f"{api_base_url}/api/v1/sandboxes", timeout=10).status_code == 401
        assert (
            httpx.get(f"{api_base_url}/api/v1/sandboxes", headers={"X-API-Key": "bogus"}, timeout=10).status_code == 401
        )
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes/obs-missing/pause",
            json={"namespace": "default"},
            headers=api_headers,
            timeout=30,
        )
        assert r.status_code == 404, r.text
        after = _scrape_app("k7-api")

        def delta(name: str, **labels: str) -> float:
            return _sum(after, name, **labels) - _sum(before, name, **labels)

        assert delta("k7_auth_failures_total", reason="missing") >= 1
        assert delta("k7_auth_failures_total", reason="invalid") >= 1
        route = "/api/v1/sandboxes/{name}/pause"
        assert delta("k7_http_requests_total", route=route, method="POST", status="404") == 1
        assert delta("k7_http_request_duration_seconds_count", route=route, method="POST") == 1
        assert delta("k7_http_requests_total", route="/api/v1/sandboxes", method="GET", status="401") >= 2
        assert delta("k7_operation_errors_total", op="pause", backend="unknown", error_code="NotFound") == 1
        assert delta("k7_operation_duration_seconds_count", op="pause", backend="unknown", outcome="error") == 1


@requires_k7d
class TestK7dOpsAndTrace:
    def test_ops_move_metrics_and_one_request_id_reaches_the_agent(
        self, api_base_url: str, api_headers: dict, test_namespace: str
    ):
        name, fork = "obs-k7d", "obs-k7d-fork"
        api_before = _scrape_app("k7-api")
        agent_before = _scrape_app("k7-agent")
        try:
            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes",
                json={"name": name, "image": "alpine:3.20", "namespace": test_namespace, "backend": "k7d"},
                headers=api_headers,
                timeout=300,
            )
            assert r.status_code == 201, r.text
            node = _wait_ready(api_base_url, api_headers, name, test_namespace)["node"]

            request_id = k7_logging.uuid7()
            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{name}/pause",
                json={"namespace": test_namespace},
                headers={**api_headers, "X-Request-ID": request_id},
                timeout=120,
            )
            assert r.status_code == 200, r.text
            assert r.headers["X-Request-ID"] == request_id
            for op in ("resume", "fork"):
                body = {"namespace": test_namespace, **({"new_name": fork} if op == "fork" else {})}
                r = httpx.post(
                    f"{api_base_url}/api/v1/sandboxes/{name}/{op}", json=body, headers=api_headers, timeout=300
                )
                assert r.status_code in (200, 201), f"{op}: {r.text}"
            for sandbox in (fork, name):
                r = httpx.delete(
                    f"{api_base_url}/api/v1/sandboxes/{sandbox}",
                    params={"namespace": test_namespace},
                    headers=api_headers,
                    timeout=120,
                )
                assert r.status_code == 200, r.text

            api_after = _scrape_app("k7-api")
            for op in ("create", "pause", "resume", "fork", "delete"):
                count = "k7_operation_duration_seconds_count"
                moved = _sum(api_after, count, op=op, backend="k7d", outcome="success") - _sum(
                    api_before, count, op=op, backend="k7d", outcome="success"
                )
                assert moved >= 1, f"k7_operation_duration_seconds{{op={op},backend=k7d}} did not move"
            agent_after = _scrape_app("k7-agent")
            for rpc in ("lookup_sandbox", "pause_vm", "resume_vm"):
                moved = _sum(agent_after, "k7d_rpc_duration_seconds_count", op=rpc) - _sum(
                    agent_before, "k7d_rpc_duration_seconds_count", op=rpc
                )
                assert moved >= 1, f"k7d_rpc_duration_seconds{{op={rpc}}} did not move on the agent"

            api_lines = [line for pod in _pods("k7-api") for line in _logs(pod) if line["request_id"] == request_id]
            audit = [line for line in api_lines if line["logger"] == "k7.audit"]
            assert len(audit) == 1, api_lines
            assert audit[0]["route"] == "/api/v1/sandboxes/{name}/pause" and audit[0]["status"] == 200
            op_lines = [line for line in api_lines if line["logger"] == "k7.op"]
            assert len(op_lines) == 1, api_lines
            assert op_lines[0]["op"] == "pause" and op_lines[0]["backend"] == "k7d"
            assert op_lines[0]["error_code"] is None and op_lines[0]["duration_ms"] > 0

            agent = [p for p in _pods("k7-agent") if p["spec"]["nodeName"] == node]
            assert len(agent) == 1, f"one k7-agent on {node}, got {[p['metadata']['name'] for p in agent]}"
            agent_lines = [line for line in _logs(agent[0]) if line["request_id"] == request_id]
            assert [line["route"] for line in agent_lines] == ["/agent/v1/vm/pause"], agent_lines
            assert agent_lines[0]["sandbox"] == name and agent_lines[0]["namespace"] == test_namespace
        finally:
            for sandbox in (fork, name):
                httpx.delete(
                    f"{api_base_url}/api/v1/sandboxes/{sandbox}",
                    params={"namespace": test_namespace},
                    headers=api_headers,
                    timeout=120,
                )

    def test_sandboxes_gauge_counts_once(self, api_base_url: str, api_headers: dict, test_namespace: str):
        """Only the leader replica exports k7_sandboxes, so sum() is the cluster count."""
        name = "obs-gauge"
        r = httpx.post(
            f"{api_base_url}/api/v1/sandboxes",
            json={"name": name, "image": "alpine:3.20", "namespace": test_namespace, "backend": "k7d"},
            headers=api_headers,
            timeout=300,
        )
        assert r.status_code == 201, r.text
        try:
            _wait_ready(api_base_url, api_headers, name, test_namespace)
            want = sum(
                1
                for d in _json("get", "deployments", "-A", "-l", "katakate.org/sandbox")["items"]
                if (d["metadata"].get("annotations") or {}).get("k7.katakate.org/backend") == "k7d"
            )
            deadline = time.time() + 75
            while True:
                texts = _scrape_app("k7-api")
                exporters = [t for t in texts if any(n == "k7_sandboxes" for n, _l, _v in _samples([t]))]
                got = _sum(texts, "k7_sandboxes", backend="k7d")
                if len(exporters) == 1 and got == want:
                    break
                assert time.time() < deadline, f"k7_sandboxes: {len(exporters)} exporters, k7d={got}, want {want}"
                time.sleep(5)
        finally:
            httpx.delete(
                f"{api_base_url}/api/v1/sandboxes/{name}",
                params={"namespace": test_namespace},
                headers=api_headers,
                timeout=120,
            )


def _fork_buckets(before: list[str], after: list[str], backend: str) -> list[tuple[float, float]]:
    """``(le, new observations <= le)`` of successful forks on ``backend`` between two scrapes."""
    name = "k7_operation_duration_seconds_bucket"
    labels = {"op": "fork", "backend": backend, "outcome": "success"}
    les = sorted({float(lab["le"]) for n, lab, _ in _samples(after) if n == name and labels.items() <= lab.items()})
    return [(le, _sum(after, name, le=_le(le), **labels) - _sum(before, name, le=_le(le), **labels)) for le in les]


def _le(le: float) -> str:
    return "+Inf" if le == float("inf") else str(le)


@pytest.mark.qemu
@requires_k7d
def test_k7d_and_kql_forks_land_in_distinct_buckets(api_base_url: str, api_headers: dict, test_namespace: str):
    """A k7d warm fork (~2 s through the API) and a kql fork (8-47 s) resolve apart."""
    before = _scrape_app("k7-api")
    sandboxes = []
    try:
        for backend in ("k7d", "kata-qemu-longhorn"):
            source = f"obs-{backend[:3]}-src"
            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes",
                json={"name": source, "image": "alpine:3.20", "namespace": test_namespace, "backend": backend},
                headers=api_headers,
                timeout=300,
            )
            assert r.status_code == 201, r.text
            sandboxes.append(source)
            _wait_ready(api_base_url, api_headers, source, test_namespace)
            r = httpx.post(
                f"{api_base_url}/api/v1/sandboxes/{source}/fork",
                json={"new_name": f"{source}-fork", "namespace": test_namespace},
                headers=api_headers,
                timeout=600,
            )
            assert r.status_code == 201, r.text
            sandboxes.append(f"{source}-fork")
        after = _scrape_app("k7-api")
        k7d, kql = _fork_buckets(before, after, "k7d"), _fork_buckets(before, after, "kata-qemu-longhorn")
        k7d_top = next(le for le, n in k7d if n == k7d[-1][1])
        kql_bottom = next(le for le, n in kql if n > 0)
        assert k7d[-1][1] == 1 and kql[-1][1] == 1, (k7d, kql)
        assert k7d_top < kql_bottom, f"k7d fork <= {k7d_top}s and kql fork <= {kql_bottom}s share a bucket"
    finally:
        for name in reversed(sandboxes):
            httpx.delete(
                f"{api_base_url}/api/v1/sandboxes/{name}",
                params={"namespace": test_namespace},
                headers=api_headers,
                timeout=120,
            )


def _wait_ready(url: str, headers: dict, name: str, namespace: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{url}/api/v1/sandboxes/{name}", params={"namespace": namespace}, headers=headers, timeout=10)
        if r.status_code == 200 and r.json()["data"]["ready"] == "True":
            return r.json()["data"]
        time.sleep(3)
    raise TimeoutError(f"sandbox {name} not Ready via API within {timeout}s")


class TestJsonLogs:
    def test_every_line_is_json_with_the_spec_fields(self, api_base_url: str):
        assert httpx.get(f"{api_base_url}/api/v1/sandboxes", timeout=10).status_code == 401
        for app in CONTAINER:
            for pod in _pods(app):
                lines = _logs(pod, since="30m")
                assert lines, f"{pod['metadata']['name']} logged nothing in 30m"
                for line in lines:
                    missing = {"ts", "level", "logger", "msg", *k7_logging.FIELDS} - line.keys()
                    assert not missing, f"{pod['metadata']['name']} line lacks {sorted(missing)}: {line}"

    def test_snapshot_gc_job_logs_json(self):
        """One run of the snapshot-gc CronJob logs JSON lines under one request id."""
        job = f"obs-gc-{uuid4().hex[:6]}"
        ns = system_ns()
        kubectl("-n", ns, "create", "job", job, "--from=cronjob/k7-snapshot-gc", check=True)
        try:
            kubectl("-n", ns, "wait", "--for=condition=Complete", f"job/{job}", "--timeout=300s", check=True)
            out = kubectl("-n", ns, "logs", f"job/{job}", check=True).stdout
            lines = [json.loads(line) for line in out.splitlines()]
            assert lines and all(line["op"] == "snapshot-gc" and line["request_id"] for line in lines), lines
            assert len({line["request_id"] for line in lines}) == 1
        finally:
            kubectl("-n", ns, "delete", "job", job, "--wait=false")


def _monitoring_installed() -> bool:
    return HAS_CLUSTER and kubectl("-n", "monitoring", "get", "prometheus").stdout.strip() != ""


def _prom(path: str) -> dict:
    out = kubectl("get", "--raw", f"{PROMETHEUS}{path}", check=True)
    body = json.loads(out.stdout)
    assert body["status"] == "success", body
    return body["data"]


@pytest.mark.skipif(not _monitoring_installed(), reason="kube-prometheus-stack not installed (k7 install --monitoring)")
class TestMonitoringStack:
    def test_prometheus_loaded_every_k7_rule(self):
        want = yaml.safe_load((OBSERVABILITY / "rules" / "k7.rules.yaml").read_text())
        want_names = {r.get("alert") or r["record"] for g in want["groups"] for r in g["rules"]}
        groups = [g for g in _prom("/api/v1/rules")["groups"] if "kube-system-k7-" in g["file"]]
        assert {g["name"] for g in groups} == {g["name"] for g in want["groups"]}
        rules = [r for g in groups for r in g["rules"]]
        assert {r["name"] for r in rules} == want_names
        bad = [(r["name"], r.get("lastError")) for r in rules if r["health"] != "ok"]
        assert not bad, f"rules failing to evaluate: {bad}"
        firing = [r["name"] for r in rules if r.get("state") == "firing"]
        assert not firing, f"K7 alerts firing on a healthy cluster: {firing}"
        assert _prom("/api/v1/query?query=k7%3Athreshold%3Aapi_error_ratio")["result"], "threshold record empty"

    def test_every_k7_pod_is_an_up_target(self):
        want = {app: {p["status"]["podIP"] for p in _pods(app)} for app in CONTAINER}
        deadline = time.time() + 120
        while True:
            targets = _prom("/api/v1/targets?state=active")["activeTargets"]
            got = {
                app: {
                    t["labels"]["instance"].rsplit(":", 1)[0]
                    for t in targets
                    if t["labels"]["job"] == app and t["health"] == "up"
                }
                for app in CONTAINER
            }
            if got == want:
                break
            assert time.time() < deadline, f"up K7 targets {got}, want {want}"
            time.sleep(10)
        names = set(_prom("/api/v1/label/__name__/values"))
        emitted = {n for t in _scrape_app("k7-api") + _scrape_app("k7-agent") for n, _l, _v in _samples([t])}
        missing = {n for n in emitted if n.startswith(("k7_", "k7d_"))} - names
        assert not missing, f"emitted but not in Prometheus: {sorted(missing)}"

    def test_grafana_loaded_the_k7_dashboards(self):
        want = sorted(p.name for p in (OBSERVABILITY / "dashboards").glob("*.json"))
        cms = _json("-n", "monitoring", "get", "configmaps", "-l", "grafana_dashboard=1")["items"]
        shipped = sorted(k for cm in cms for k in cm.get("data", {}) if k in want)
        assert shipped == want
        grafana = _json("-n", "monitoring", "get", "pods", "-l", "app.kubernetes.io/name=grafana")["items"]
        assert grafana, "no Grafana pod in monitoring"
        uids = {json.loads((OBSERVABILITY / "dashboards" / f).read_text())["uid"] for f in want}
        pod = grafana[0]["metadata"]["name"]
        deadline = time.time() + 120
        while True:
            # The Grafana image is distroless; its dashboard sidecar has Python and the admin credentials.
            out = kubectl(
                "-n", "monitoring", "exec", pod, "-c", "grafana-sc-dashboard", "--", "python3", "-c", _GRAFANA_UIDS
            )
            assert out.returncode == 0, out.stderr
            if uids <= set(json.loads(out.stdout)):
                break
            assert time.time() < deadline, f"Grafana has {out.stdout}, want {sorted(uids)}"
            time.sleep(10)
