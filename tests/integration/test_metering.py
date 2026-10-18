"""Usage metering against the live cluster.

The module swaps ConfigMap ``k7-metering`` for a test document
(``sampleInterval: 5``, ``storageInterval: 30``, an OpenMeter preset sink and
a stdout sink, the installed ``prometheusUrl`` kept) and rolls k7-agent and
k7-api onto it. In ``finally`` it puts the installed document and pod
annotations back, which rolls both again: the stdout sink's CloudEvent lines
leave the agents' logs with the old pods. The installed sinks stay in every
test document, so their spool offsets survive; they also receive the test's
events, including the synthetic ``it41m-overflow`` disk events that fill the
spool.

OpenMeter is a pinned single pod owned by the test (Kafka, ClickHouse and
Postgres as native sidecars on localhost, then the API and the sink worker),
in its own namespace, deleted in ``finally``.

Ground truth is independent of the sampler: CRI pod-sandbox ``createdAt`` and
the first poll that no longer sees the sandbox ready (crictl through the
agent on the sandbox's node), the wall-clock bounds of the pause and resume
calls, the bytes the iperf3 client sent, PVC capacity and VolumeSnapshot
``restoreSize``. Every sandbox is pinned to this node, whose agent the test
reaches directly (host to local pod) with the shared agent token.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import pytest

from k7.core import tenancy
from k7.metering import SANDBOX_UID_ANN
from k7.metering import config as metering_config
from k7.metering.events import DISK, EGRESS, GAP, GIB, SANDBOX, SNAPSHOT, envelope, iso
from k7.metering.sinks import openmeter_event

from .cluster import KUBECTL, agent_token, kubectl, system_ns
from .conftest import _DEV_SH
from .test_noisy_neighbour import IPERF3, IperfServer, iperf_server  # noqa: F401  (fixture)
from .test_observability import _monitoring_installed, _prom, _scrape, _sum
from .test_tenancy import KFD, KQL, NODE, SMALL, Tenant, admin, base, error, kjson, make_tenant, tenant  # noqa: F401

pytestmark = pytest.mark.integration

INTERVAL = 5
STORAGE_INTERVAL = 30
RUN_S = 20
PAUSE_S = 35
LIMITS = {"cpu": "1", "memory": "1Gi"}
EGRESS_N = "256M"
MIB = 2**20
PREFIX = "it41m-"
CM = "k7-metering"
CONFIG_ANN = "k7.katakate.org/metering-config"
WORKLOADS = (("daemonset", "k7-agent"), ("deployment", "k7-api"))
RUNTIME_CLASS = {KFD: "kata", KQL: "kata-qemu", "k7d": "k7", "k7d-fc": "k7-fc"}
SHORT = {KFD: "kfd", KQL: "kql", "k7d": "k7d", "k7d-fc": "k7dfc"}
BATCH = "application/cloudevents-batch+json"

OPENMETER = (
    "ghcr.io/openmeterio/openmeter:v1.0.0-beta.236"
    "@sha256:7d6982a0c12cce366593740f61d9f53960e2ac9af20be2ce011b8c154ee5ebb7"
)
KAFKA = "confluentinc/cp-kafka:8.0.3@sha256:db5eac24a1d15a1689fa89642ef97a7e3bc4f55ad5159a07b213c4dc7d0114e3"
CLICKHOUSE = (
    "clickhouse/clickhouse-server:25.12.3-alpine"
    "@sha256:74da41cd61db84f652c6364fd30d59e19b7276d34f7c82515f5f0e70d6f325da"
)
POSTGRES = "postgres:14.20-alpine3.23@sha256:14f02666642586a64d6fae8ef42d479fd76456a77c73ae8a626b8fe323b76d22"
OM_APP = f"{PREFIX}openmeter"


def _meter(slug: str, etype: str, value: str, group: dict[str, str]) -> dict:
    return {"slug": slug, "eventType": etype, "aggregation": "SUM", "valueProperty": value, "groupBy": group}


OM_CONFIG = {
    "ingest": {"kafka": {"broker": "127.0.0.1:9092"}},
    "aggregation": {"clickhouse": {"address": "127.0.0.1:9000"}},
    "sink": {
        "namespaceRefetch": "10s",
        "namespaceRefetchTimeout": "10s",
        "meterRefetchInterval": "1s",
        "logDroppedEvents": True,
        "kafka": {"brokers": "127.0.0.1:9092"},
        "dedupe": {"enabled": True, "driver": "memory", "config": {"size": 1_000_000}},
    },
    "postgres": {"url": "postgres://postgres:postgres@127.0.0.1:5432/postgres?sslmode=disable"},
    "meters": [
        _meter("k7_sandbox_seconds", SANDBOX, "$.seconds", {"tenant": "$.tenant", "state": "$.state"}),
        _meter("k7_egress_bytes", EGRESS, "$.bytes", {"tenant": "$.tenant", "backend": "$.backend"}),
        _meter("k7_disk_gib_seconds", DISK, "$.gib_seconds", {"tenant": "$.tenant"}),
        _meter("k7_snapshot_gib_seconds", SNAPSHOT, "$.gib_seconds", {"tenant": "$.tenant"}),
    ],
}
KAFKA_ENV = {
    "CLUSTER_ID": "ca497efe-9f82-4b84-890b-d9969a9a2e1c",
    "KAFKA_BROKER_ID": "0",
    "KAFKA_PROCESS_ROLES": "broker,controller",
    "KAFKA_CONTROLLER_QUORUM_VOTERS": "0@127.0.0.1:9093",
    "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
    "KAFKA_INTER_BROKER_LISTENER_NAME": "INTERNAL",
    "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT",
    "KAFKA_ADVERTISED_LISTENERS": "INTERNAL://127.0.0.1:9092",
    # The kubelet's startup probe dials the pod IP, so the client listener cannot be loopback-only.
    "KAFKA_LISTENERS": "INTERNAL://0.0.0.0:9092,CONTROLLER://127.0.0.1:9093",
    "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
    "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
    "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
    "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
    "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
    "KAFKA_LOG_MESSAGE_TIMESTAMP_AFTER_MAX_MS": "9223372036854775807",
}
_CRICTL = 'exec crictl --runtime-endpoint "unix://$K7_CONTAINERD_SOCKET" pods --namespace "$1" -o json'
# One probe at a time: the probes share k7-agent's 256Mi memory limit with the agent.
_CRI_LOCK = threading.Lock()
# The installed CLI after `make install` (utils/run-integration-tests.sh forwards it).
_CLI = os.environ.get("K7_ITEST_CLI") or str(_DEV_SH)


def _env(values: dict[str, str]) -> list[dict]:
    return [{"name": k, "value": v} for k, v in values.items()]


def _sidecar(name: str, image: str, port: int, env: dict[str, str]) -> dict:
    """A native sidecar: the next container starts only once this one accepts on ``port``."""
    return {
        "name": name,
        "image": image,
        "restartPolicy": "Always",
        "env": _env(env),
        "startupProbe": {"tcpSocket": {"port": port}, "periodSeconds": 2, "failureThreshold": 150},
    }


def _openmeter_objects(ns: str) -> list[dict]:
    cfg = "/etc/openmeter/config.yaml"
    mount = [{"name": "config", "mountPath": "/etc/openmeter"}]
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "openmeter", "namespace": ns},
            "data": {"config.yaml": json.dumps(OM_CONFIG)},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "openmeter", "namespace": ns, "labels": {"app": OM_APP}},
            "spec": {
                "enableServiceLinks": False,
                "terminationGracePeriodSeconds": 5,
                "initContainers": [
                    _sidecar("kafka", KAFKA, 9092, KAFKA_ENV),
                    _sidecar(
                        "clickhouse",
                        CLICKHOUSE,
                        9000,
                        {"CLICKHOUSE_USER": "default", "CLICKHOUSE_PASSWORD": "default", "CLICKHOUSE_DB": "openmeter"},
                    ),
                    _sidecar(
                        "postgres",
                        POSTGRES,
                        5432,
                        {"POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": "postgres", "POSTGRES_DB": "postgres"},
                    ),
                    # The sink worker needs the API's Postgres migrations and the om_default_events topic.
                    {
                        **_sidecar("openmeter", OPENMETER, 8888, {}),
                        # Two OpenMeter processes share the pod's netns: the API's telemetry moves off :10000.
                        "command": ["openmeter", "--address", "0.0.0.0:8888", "--telemetry-address=127.0.0.1:10001"],
                        "args": ["--config", cfg],
                        "volumeMounts": mount,
                        "readinessProbe": {"httpGet": {"path": "/api/v1/debug/metrics", "port": 8888}},
                    },
                ],
                "containers": [
                    {
                        "name": "sink-worker",
                        "image": OPENMETER,
                        "command": ["openmeter-sink-worker", "--telemetry-address=0.0.0.0:10000"],
                        "args": ["--config", cfg],
                        "volumeMounts": mount,
                        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 10000}},
                    },
                ],
                "volumes": [{"name": "config", "configMap": {"name": "openmeter"}}],
            },
        },
    ]


def _ts(raw: str) -> float:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()


_METERED = {m["eventType"] for m in OM_CONFIG["meters"]}


def _validation_errors(item: dict) -> list[str]:
    """OpenMeter's validation lines for a stored event, minus subjects without a customer and unmetered types."""
    return [
        line
        for line in (item.get("validationError") or "").splitlines()
        if not line.startswith("no customer found for event subject:")
        and not (line.startswith("no meter found for event type:") and item["event"]["type"] not in _METERED)
    ]


_SINK_GROUP = [
    "kafka-consumer-groups",
    "--bootstrap-server",
    "127.0.0.1:9092",
    "--describe",
    "--group",
    "openmeter-sink-worker",
]


def _sink_group(ns: str) -> subprocess.CompletedProcess[str]:
    return kubectl("-n", ns, "exec", "openmeter", "-c", "kafka", "--", *_SINK_GROUP)


@dataclass(frozen=True)
class OpenMeter:
    url: str
    ns: str

    def sink_state(self) -> str:
        """The sink worker's consumer lag and last log lines, for a failure message."""
        logs = kubectl("-n", self.ns, "logs", "openmeter", "-c", "sink-worker", "--tail=20").stdout
        return f"{_sink_group(self.ns).stdout}\n{logs}"

    def post(self, events: list[dict]) -> None:
        r = httpx.post(
            f"{self.url}/api/v1/events", content=json.dumps(events), headers={"Content-Type": BATCH}, timeout=30
        )
        assert r.status_code in (200, 202, 204), f"OpenMeter refused a batch: {r.status_code} {r.text}"

    def _list(self, **params: str | int) -> list[dict]:
        r = httpx.get(f"{self.url}/api/v1/events", params={"limit": 100, **params}, timeout=30)
        assert r.status_code == 200, f"OpenMeter GET /api/v1/events {params}: {r.status_code} {r.text}"
        items = r.json()
        bad = [(i["event"]["id"], e) for i in items if (e := _validation_errors(i))]
        assert not bad, f"OpenMeter could not validate k7 events: {bad}"
        return items

    def events(self, prefix: str, start: float, end: float) -> list[dict]:
        """Every stored event whose subject starts with ``prefix``, in whole seconds ``[floor(start), ceil(end))``.

        OpenMeter matches ``subject`` exactly, compares times in whole seconds and pages at 100 events.
        """
        lo, hi = math.floor(start), math.ceil(end)
        items = self._list(**{"from": iso(lo), "to": iso(hi)})
        if len(items) == 100:
            assert hi - lo > 1, f"OpenMeter holds 100+ events within second {lo}: they cannot be paged by time"
            mid = (lo + hi) // 2
            return self.events(prefix, lo, mid) + self.events(prefix, mid, hi)
        return [i["event"] for i in items if i["event"]["subject"].startswith(prefix)]

    def count(self, event_id: str) -> int:
        return len(self._list(id=event_id))

    def barrier(self) -> None:
        """Wait until a fresh event is stored: the single ingest partition then processed everything before it."""
        eid = uuid.uuid4().hex
        now = time.time()
        self.post([envelope("it41m.canary", "it41m-test", f"{PREFIX}canary", eid, iso(now), {})])
        deadline = time.time() + 180
        while self.count(eid) != 1:
            assert time.time() < deadline, f"OpenMeter did not store canary {eid} within 180s:\n{self.sink_state()}"
            time.sleep(1)


def _wait_sink_assigned(ns: str) -> None:
    """The sink worker consumes om_default_events: it starts at the latest offset, so earlier events are never stored."""
    deadline = time.time() + 180
    while True:
        out = _sink_group(ns)
        rows = [line.split() for line in out.stdout.splitlines() if line.startswith("openmeter-sink-worker ")]
        if any(r[1] == "om_default_events" and r[6] != "-" for r in rows):
            return
        assert time.time() < deadline, f"the OpenMeter sink worker took no om_default_events partition: {out}"
        time.sleep(2)


@pytest.fixture(scope="module")
def openmeter() -> Iterator[OpenMeter]:
    ns = f"{PREFIX}om-{uuid.uuid4().hex[:6]}"
    kubectl("create", "namespace", ns, check=True)
    try:
        for obj in _openmeter_objects(ns):
            subprocess.run([*KUBECTL, "create", "-f", "-"], input=json.dumps(obj), text=True, check=True)
        out = kubectl("-n", ns, "wait", "--for=condition=Ready", "pod/openmeter", "--timeout=600s")
        if out.returncode != 0:
            desc = kubectl("-n", ns, "describe", "pod", "openmeter").stdout
            logs = {
                c: kubectl("-n", ns, "logs", "openmeter", "-c", c, "--tail=30").stdout
                for c in ("openmeter", "sink-worker")
            }
            pytest.fail(f"OpenMeter pod not Ready: {out.stderr}\n{desc}\n{logs}", pytrace=False)
        om = OpenMeter(f"http://{kjson('-n', ns, 'get', 'pod', 'openmeter')['status']['podIP']}:8888", ns)
        _wait_sink_assigned(ns)
        om.barrier()
        yield om
    finally:
        kubectl("delete", "namespace", ns, "--wait=true", "--timeout=300s", check=True)


# ── the k7-metering swap ──


def _agent_pod() -> dict:
    """The Ready k7-agent pod on this node."""
    deadline = time.time() + 300
    while time.time() < deadline:
        for p in kjson("-n", system_ns(), "get", "pods", "-l", "app=k7-agent")["items"]:
            ready = {c["type"]: c["status"] for c in p["status"].get("conditions") or []}.get("Ready")
            if p["spec"].get("nodeName") == NODE and ready == "True" and not p["metadata"].get("deletionTimestamp"):
                return p
        time.sleep(2)
    raise AssertionError(f"no Ready k7-agent pod on {NODE} within 300s")


def _agent(method: str, path: str, **kw) -> httpx.Response:
    url = f"http://{_agent_pod()['status']['podIP']}:8000{path}"
    return httpx.request(method, url, headers={"X-K7-Agent-Token": agent_token()}, timeout=60, **kw)


def _status() -> dict:
    r = _agent("GET", "/agent/v1/usage/status")
    assert r.status_code == 200, r.text
    return r.json()


def _template_annotation(kind: str, name: str) -> str | None:
    meta = kjson("-n", system_ns(), "get", kind, name)["spec"]["template"]["metadata"]
    return (meta.get("annotations") or {}).get(CONFIG_ANN)


def _publish(data: str, annotations: dict[str, str | None]) -> None:
    ns = system_ns()
    kubectl(
        "-n",
        ns,
        "patch",
        "configmap",
        CM,
        "--type=merge",
        "-p",
        json.dumps({"data": {"metering.json": data}}),
        check=True,
    )
    for kind, name in WORKLOADS:
        patch = {"spec": {"template": {"metadata": {"annotations": {CONFIG_ANN: annotations[kind]}}}}}
        kubectl("-n", ns, "patch", kind, name, "--type=merge", "-p", json.dumps(patch), check=True)
    for kind, name in WORKLOADS:
        out = kubectl("-n", ns, "rollout", "status", f"{kind}/{name}", "--timeout=600s")
        assert out.returncode == 0, f"{kind}/{name} did not roll onto the metering config: {out.stderr}"


@dataclass
class Metering:
    original: str
    annotations: dict[str, str | None]
    doc: dict
    current: str = ""

    def publish(self, doc: dict) -> None:
        data = json.dumps(doc, sort_keys=True)
        if data == self.current:
            return
        self.current = data  # before the roll, so a failed roll is still restored
        _publish(data, {kind: hashlib.sha256(data.encode()).hexdigest()[:16] for kind, _ in WORKLOADS})
        _wait_agent_config(doc)

    def restore(self) -> None:
        if self.current != self.original:
            _publish(self.original, self.annotations)
            self.current = self.original
            _wait_agent_config(self.doc)

    def with_sinks(self, sinks: list[dict], **over) -> dict:
        return {
            **self.doc,
            "sampleInterval": INTERVAL,
            "storageInterval": STORAGE_INTERVAL,
            "spool": {"maxBytes": 64 * MIB, "highWaterPercent": 90},
            "requireDelivery": False,
            "sinks": [*self.doc["sinks"], *sinks],
            **over,
        }


def _wait_agent_config(doc: dict) -> None:
    want = metering_config.parse(doc)
    deadline = time.time() + 120
    while True:
        st = _status()
        if set(st["sinks"]) == {s.name for s in want.sinks} and st["sample_interval"] == want.sample_interval:
            return
        assert time.time() < deadline, f"agent on {NODE} still runs sinks {sorted(st['sinks'])}"
        time.sleep(2)


def _wait_storage_leader(timeout: float) -> None:
    """A current k7-api pod holds Lease k7-metering-leader at the test storageInterval."""
    deadline = time.time() + timeout
    while True:
        pods = {p["metadata"]["name"] for p in kjson("-n", system_ns(), "get", "pods", "-l", "app=k7-api")["items"]}
        out = kubectl("-n", system_ns(), "get", "lease", "k7-metering-leader", "-o", "json")
        spec = json.loads(out.stdout)["spec"] if out.returncode == 0 else {}
        holder = (spec.get("holderIdentity") or "").rsplit("-", 1)[0]
        if holder in pods and spec.get("leaseDurationSeconds") == 2 * STORAGE_INTERVAL:
            return
        assert time.time() < deadline, f"no k7-api pod took the storage-sampler lease within {timeout:.0f}s: {spec}"
        time.sleep(3)


@pytest.fixture(scope="module")
def metering(openmeter: OpenMeter) -> Iterator[Metering]:
    data = kjson("-n", system_ns(), "get", "configmap", CM)["data"]["metering.json"]
    env = Metering(data, {kind: _template_annotation(kind, name) for kind, name in WORKLOADS}, json.loads(data), data)
    old_storage = metering_config.parse(env.doc).storage_interval
    try:
        env.publish(env.with_sinks(_sinks(openmeter)))
        _wait_storage_leader(2 * old_storage + 120)
        yield env
    finally:
        env.restore()


def _sinks(om: OpenMeter, suffix: str = "") -> list[dict]:
    return [
        {
            "name": f"{PREFIX}openmeter{suffix}",
            "type": "http",
            "url": om.url,
            "mode": "batch",
            "preset": "openmeter",
            "batchSize": 100,
        },
        {"name": f"{PREFIX}stdout{suffix}", "type": "stdout"},
    ]


def _settle(om: OpenMeter) -> None:
    """This node's test sinks drained, then everything they sent is stored in OpenMeter."""
    deadline = time.time() + 180
    while True:
        st = _status()
        stopped = {n: s["stopped"] for n, s in st["sinks"].items() if s["stopped"]}
        assert not stopped, f"usage sinks stopped: {stopped}"
        pending = {s: b for s, b in st["spool"]["pending_bytes"].items() if s.startswith(PREFIX) and b}
        if not pending:
            break
        assert time.time() < deadline, f"usage sinks did not drain within 180s: {pending}"
        time.sleep(1)
    om.barrier()


def _stdout_events(prefix: str, pods: list[dict] | None = None) -> dict[str, dict]:
    """CloudEvents the stdout sink logged, by id, for subjects starting with ``prefix`` (every agent pod)."""
    pods = pods or kjson("-n", system_ns(), "get", "pods", "-l", "app=k7-agent")["items"]
    out: dict[str, dict] = {}
    for p in pods:
        logs = kubectl("-n", system_ns(), "logs", p["metadata"]["name"], "-c", "k7-agent", check=True).stdout
        for line in logs.splitlines():
            if not line.startswith('{"data":'):
                continue
            e = json.loads(line)
            if e.get("specversion") == "1.0" and e["subject"].startswith(prefix):
                assert out.get(e["id"], e) == e, f"two different events share id {e['id']}"
                out[e["id"]] = e
    return out


# ── ground truth from CRI ──


def _cri(ns: str) -> list[dict]:
    pod = _agent_pod()["metadata"]["name"]
    with _CRI_LOCK:
        out = kubectl("-n", system_ns(), "exec", pod, "-c", "k7-agent", "--", "sh", "-c", _CRICTL, "crictl", ns)
    assert out.returncode == 0, f"crictl through {pod}: {out.stderr}"
    return json.loads(out.stdout)["items"]


def _ready_cri(ns: str, name: str, exclude: tuple[str, ...] = ()) -> dict:
    deadline = time.time() + 120
    while True:
        items = [
            i
            for i in _cri(ns)
            if (i.get("labels") or {}).get(tenancy.SANDBOX_LABEL) == name
            and i["state"] == "SANDBOX_READY"
            and i["id"] not in exclude
        ]
        if len(items) == 1:
            return items[0]
        assert time.time() < deadline, f"{ns}/{name}: expected one ready CRI sandbox, got {items}"
        time.sleep(1)


def _wait_gone(ns: str, cri_id: str, since: float) -> tuple[float, float]:
    """Bounds on when CRI sandbox ``cri_id`` stopped being ready: the last poll that saw it, the first that did not."""
    last_ready, deadline = since, time.time() + 300
    while time.time() < deadline:
        t0 = time.time()
        states = {i["id"]: i["state"] for i in _cri(ns)}
        t1 = time.time()
        if states.get(cri_id) != "SANDBOX_READY":
            return last_ready, t1
        last_ready = t0
        time.sleep(0.5)
    raise AssertionError(f"CRI sandbox {cri_id} in {ns} still ready 300s after its stop")


def _provisioned(dep: dict, ns: str) -> int:
    """What the spec calls provisioned disk: PVC capacity, ephemeral claims and ephemeral-storage limits."""
    pod = dep["spec"]["template"]["spec"]
    total = sum(
        tenancy.quantity(c["resources"]["limits"]["ephemeral-storage"])
        for c in pod["containers"]
        if "ephemeral-storage" in ((c.get("resources") or {}).get("limits") or {})
    )
    for v in pod.get("volumes") or []:
        if "persistentVolumeClaim" in v:
            pvc = kjson("-n", ns, "get", "pvc", v["persistentVolumeClaim"]["claimName"])
            total += tenancy.quantity(pvc["status"]["capacity"]["storage"])
        elif "ephemeral" in v:
            total += tenancy.quantity(v["ephemeral"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"])
    return int(total)


# ── the lifecycle: create → run → pause → resume → delete, every installed backend at once ──


@dataclass
class Run:
    backend: str
    name: str
    uid: str = ""
    created: float = 0.0  # the Deployment
    deleted: tuple[float, float] = (0.0, 0.0)  # the DELETE call
    pods: list[tuple[float, float, float]] = field(default_factory=list)  # CRI createdAt, end bounds
    pause: tuple[float, float] = (0.0, 0.0)
    resume: tuple[float, float] = (0.0, 0.0)
    egress: int = 0
    egress_unavailable: bool = False
    disk_bytes: int = 0
    snapshot_uid: str = ""
    snapshot_bytes: int = 0


@dataclass
class Lifecycle:
    tenant: Tenant
    runs: list[Run]
    start: float
    end: float
    events: list[dict]  # from OpenMeter
    stdout: dict[str, dict]  # from the agents' stdout sinks
    counters: str  # this node's agent /metrics before any restart

    def of(self, run: Run, etype: str) -> list[dict]:
        return sorted(
            (e for e in self.events if e["type"] == etype and e["data"].get("sandbox_uid") == run.uid),
            key=lambda e: e["data"]["window_start"],
        )


def _installed() -> list[str]:
    return [b for b, rc in RUNTIME_CLASS.items() if kubectl("get", "runtimeclass", rc).returncode == 0]


def _iperf_bytes(t: Tenant, name: str, server: IperfServer) -> int:
    """Payload bytes the sandbox's iperf3 client sent (the server's own total can miss the last blocks)."""
    before = len(server.results())
    cmd = f"iperf3 -c {server.ip} -n {EGRESS_N} -J"
    r = t("POST", f"/api/v1/sandboxes/{name}/exec", params={"namespace": t.ns}, json={"command": cmd})
    assert r.status_code == 200 and r.json()["data"]["exit_code"] == 0, r.text
    docs = server.results()
    assert len(docs) == before + 1, f"expected one new iperf3 result on the server, got {len(docs) - before}"
    return int(json.loads(r.json()["data"]["stdout"])["end"]["sum_sent"]["bytes"])


def _call(t: Tenant, verb: str, name: str) -> tuple[float, float]:
    t0 = time.time()
    r = t("POST", f"/api/v1/sandboxes/{name}/{verb}", json={"namespace": t.ns})
    t1 = time.time()
    assert r.status_code == 200, f"{verb} {name}: {r.status_code} {r.text}"
    return t0, t1


def _drive(t: Tenant, backend: str, server: IperfServer, iperf_lock: threading.Lock) -> Run:
    run = Run(backend, f"lc-{SHORT[backend]}")
    extra = {"root_disk_size": "2Gi"} if backend == KQL else {}
    r = t.create(
        run.name,
        backend=backend,
        image=IPERF3,
        entrypoint=["sleep", "infinity"],
        limits=LIMITS,
        node_name=NODE,
        **extra,
    )
    assert r.status_code == 201, f"{backend}: {r.text}"
    t.wait_ready(run.name, timeout=600)
    dep = kjson("-n", t.ns, "get", "deployment", run.name)
    run.uid, run.created = dep["metadata"]["annotations"][SANDBOX_UID_ANN], _ts(dep["metadata"]["creationTimestamp"])
    pod = _ready_cri(t.ns, run.name)
    if backend == KQL:
        run.disk_bytes = _provisioned(dep, t.ns)
        snap = f"{run.name}-snap"
        assert t.snapshot(run.name, snap).status_code == 201
        t.wait_snapshot_ready(snap, timeout=600)
        vs = kjson("-n", t.ns, "get", "volumesnapshot", snap)
        run.snapshot_uid, run.snapshot_bytes = vs["metadata"]["uid"], int(tenancy.quantity(vs["status"]["restoreSize"]))
    with iperf_lock:
        run.egress = _iperf_bytes(t, run.name, server)
    time.sleep(INTERVAL + 2)
    run.egress_unavailable = backend in _status()["egress_unavailable"]
    time.sleep(RUN_S)
    run.pause = _call(t, "pause", run.name)
    k7d = backend in tenancy.K7D_FAMILY
    if not k7d:
        run.pods.append((int(pod["createdAt"]) / 1e9, *_wait_gone(t.ns, pod["id"], run.pause[0])))
    time.sleep(PAUSE_S)
    run.resume = _call(t, "resume", run.name)
    if not k7d:
        t.wait_ready(run.name, timeout=600)
        pod = _ready_cri(t.ns, run.name, exclude=(pod["id"],))
    time.sleep(RUN_S)
    d0 = time.time()
    r = t("DELETE", f"/api/v1/sandboxes/{run.name}", params={"namespace": t.ns})
    run.deleted = (d0, time.time())
    assert r.status_code == 200, r.text
    run.pods.append((int(pod["createdAt"]) / 1e9, *_wait_gone(t.ns, pod["id"], d0)))
    return run


@pytest.fixture(scope="module")
def lifecycle(metering: Metering, openmeter: OpenMeter, base: str, admin: str, iperf_server: IperfServer):  # noqa: F811
    backends = _installed()
    assert backends, f"no sandbox backend installed (none of RuntimeClasses {sorted(RUNTIME_CLASS.values())})"
    with tenant(base, admin) as t:
        start, lock = time.time(), threading.Lock()
        with ThreadPoolExecutor(len(backends)) as pool:
            runs = [f.result() for f in [pool.submit(_drive, t, b, iperf_server, lock) for b in backends]]
        # The storage sampler emits a window after it closes; then let both sinks drain.
        time.sleep(STORAGE_INTERVAL + 5)
        _settle(openmeter)
        counters = _scrape(_agent_pod())
        end = time.time()
        stdout = _stdout_events(f"{t.name}/")
        deadline = time.time() + 120
        while True:
            events = openmeter.events(f"{t.name}/", start - 60, time.time() + 60)
            missing = set(stdout) - {e["id"] for e in events}
            if not missing:
                break
            assert time.time() < deadline, (
                f"OpenMeter lacks {len(missing)} event(s) the stdout sink logged: {sorted(missing)[:5]}"
            )
            time.sleep(3)
        yield Lifecycle(t, runs, start, end, events, stdout, counters)


def _seconds(events: list[dict], state: str) -> float:
    return sum(e["data"]["seconds"] for e in events if e["data"]["state"] == state)


def _window(e: dict) -> tuple[float, float]:
    return _ts(e["data"]["window_start"]), _ts(e["data"]["window_end"])


class TestNodeSampler:
    def test_running_and_paused_seconds_match_wall_clock_within_one_interval(self, lifecycle: Lifecycle):
        failures = []
        for run in lifecycle.runs:
            events = lifecycle.of(run, SANDBOX)
            life_lo = sum(lo - c for c, lo, _ in run.pods)
            life_hi = sum(hi - c for c, _, hi in run.pods)
            if run.backend in tenancy.K7D_FAMILY:
                paused = (run.resume[0] - run.pause[1], run.resume[1] - run.pause[0])
            else:
                paused = (0.0, 0.0)
            running = (life_lo - paused[1], life_hi - paused[0])
            got = {"running": _seconds(events, "running"), "paused": _seconds(events, "paused")}
            # Each vanished pod is charged to a sample midpoint (≤ half an interval off), plus sampler jitter.
            tol = len(run.pods) * INTERVAL / 2 + 1
            for state, (lo, hi) in (("running", running), ("paused", paused)):
                if not lo - tol <= got[state] <= hi + tol:
                    failures.append(f"{run.backend}: {state} {got[state]:.3f}s, truth [{lo:.3f}, {hi:.3f}] ± {tol}s")
            print(
                f"[metering] {run.backend}: running {got['running']:.1f}s in {running}, paused {got['paused']:.1f}s in {paused}"
            )
        assert not failures, failures

    def test_paused_windows_carry_memory_and_no_vcpu(self, lifecycle: Lifecycle):
        k7d = [r for r in lifecycle.runs if r.backend in tenancy.K7D_FAMILY]
        for run in lifecycle.runs:
            events = lifecycle.of(run, SANDBOX)
            assert {
                (e["data"]["vcpu"], e["data"]["memory_gib"]) for e in events if e["data"]["state"] == "running"
            } == {(1.0, 1.0)}
            paused = {(e["data"]["vcpu"], e["data"]["memory_gib"]) for e in events if e["data"]["state"] == "paused"}
            assert paused == ({(0.0, 1.0)} if run in k7d else set()), f"{run.backend}: paused windows {paused}"
            assert {e["data"]["size_class"] for e in events} == {"1c-1g"}

    def test_stopped_sandboxes_have_no_node_seconds_and_windows_never_overlap(self, lifecycle: Lifecycle):
        for run in lifecycle.runs:
            windows = sorted(_window(e) for e in lifecycle.of(run, SANDBOX))
            assert windows, f"{run.backend}: no sandbox usage events"
            for (_, a_end), (b_start, _) in zip(windows, windows[1:], strict=False):
                assert b_start >= a_end - 0.0015, f"{run.backend}: overlapping windows {windows}"
            # A vanished pod is charged at most to the midpoint of the sample that missed it.
            outside = [
                w
                for w in windows
                if not any(c - 0.0015 <= w[0] and w[1] <= hi + INTERVAL / 2 + 1 for c, _, hi in run.pods)
            ]
            assert not outside, f"{run.backend}: usage outside the pod lifetimes {run.pods}: {outside}"

    def test_no_gap_without_an_agent_restart_and_ids_unique(self, lifecycle: Lifecycle):
        ids = [e["id"] for e in lifecycle.events]
        assert len(ids) == len(set(ids)), "OpenMeter stored an id twice"
        gaps = [e for e in lifecycle.events if e["type"] == GAP]
        assert gaps == [], f"gap events with no agent restart: {gaps}"

    def test_egress_matches_iperf3_within_5_percent_or_is_unavailable(self, lifecycle: Lifecycle):
        failures = []
        for run in lifecycle.runs:
            sent = sum(e["data"]["bytes"] for e in lifecycle.of(run, EGRESS))
            print(f"[metering] {run.backend}: egress {sent} B, iperf3 client sent {run.egress} B")
            if run.egress_unavailable:
                if sent:
                    failures.append(f"{run.backend}: egress_unavailable yet {sent} B metered")
            elif not abs(sent - run.egress) <= 0.05 * run.egress:
                failures.append(f"{run.backend}: egress {sent} B vs {run.egress} B sent (> 5%)")
        assert not failures, failures

    def test_prometheus_counters_equal_the_events_and_carry_no_sandbox_label(self, lifecycle: Lifecycle):
        t = lifecycle.tenant.name
        for run in lifecycle.runs:
            for state in ("running", "paused"):
                labels = {"tenant": t, "namespace": lifecycle.tenant.ns, "backend": run.backend, "state": state}
                got = _sum([lifecycle.counters], "k7_usage_sandbox_seconds_total", **labels)
                assert got == pytest.approx(_seconds(lifecycle.of(run, SANDBOX), state), abs=0.01), (run.backend, state)
        for line in lifecycle.counters.splitlines():
            if line.startswith("k7_usage_") and "{" in line:
                assert "sandbox" not in line.split("{", 1)[1].split("}", 1)[0], line


class TestStorage:
    def _kql(self, lifecycle: Lifecycle) -> Run:
        runs = [r for r in lifecycle.runs if r.backend == KQL]
        assert runs, "the storage meters are checked on kata-qemu-longhorn, which is not installed"
        return runs[0]

    def test_kql_disk_bytes_match_the_pvc_and_cover_its_lifetime(self, lifecycle: Lifecycle):
        run = self._kql(lifecycle)
        disk = lifecycle.of(run, DISK)
        assert disk, "no disk usage events for the kql sandbox"
        assert {e["data"]["bytes"] for e in disk} == {run.disk_bytes}
        for e in disk:
            assert e["data"]["gib_seconds"] == pytest.approx(run.disk_bytes / GIB * e["data"]["seconds"], rel=1e-6)
        assert "stopped" in {e["data"]["state"] for e in disk}, "no disk window while the kql sandbox was scaled to 0"
        total, life = sum(e["data"]["seconds"] for e in disk), run.deleted[1] - run.created
        assert life - 2 * STORAGE_INTERVAL - 2 <= total <= life + 2, (total, life)

    def test_kql_snapshot_bytes_match_restore_size(self, lifecycle: Lifecycle):
        run = self._kql(lifecycle)
        snaps = [e for e in lifecycle.events if e["type"] == SNAPSHOT and e["data"]["snapshot_uid"] == run.snapshot_uid]
        assert snaps, "no snapshot usage events for the kql VolumeSnapshot"
        assert {e["data"]["bytes"] for e in snaps} == {run.snapshot_bytes}
        assert {(e["data"]["source_sandbox"], e["data"]["kind"]) for e in snaps} == {(run.name, "volume")}


class TestDelivery:
    def test_openmeter_received_every_event_the_spool_delivered(self, lifecycle: Lifecycle, openmeter: OpenMeter):
        assert lifecycle.stdout, "the stdout sink logged no event for the lifecycle tenant"
        stored = {e["id"]: e for e in lifecycle.events}
        for eid, e in lifecycle.stdout.items():
            assert stored[eid]["data"] == openmeter_event(e)["data"] and stored[eid]["type"] == e["type"], eid
        later = _stdout_events(f"{lifecycle.tenant.name}/")
        assert set(stored) <= set(later), "OpenMeter holds k7 events the stdout sink never logged"

    def test_k7_usage_matches_the_sum_of_events(self, lifecycle: Lifecycle, metering: Metering, base: str):  # noqa: F811
        t = lifecycle.tenant

        def usage() -> subprocess.CompletedProcess[str]:
            args = ["--from", str(int(lifecycle.start) - 60), "--to", str(int(time.time()))]
            cmd = [_CLI, "--api-url", base, "--api-key", t.token, "usage", "--tenant", t.name, *args]
            return subprocess.run(
                [*cmd, "--group-by", "backend,state", "--json"], capture_output=True, text=True, timeout=120
            )

        if not metering.doc["prometheusUrl"]:
            out = usage()
            assert out.returncode != 0 and "metering.prometheusUrl" in out.stdout + out.stderr, out
            return
        want: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for e in lifecycle.events:
            d = e["data"]
            if e["type"] == SANDBOX:
                row = want[(d["backend"], d["state"])]
                row["sandbox_seconds"] += d["seconds"]
                row["vcpu_seconds"] += d["vcpu"] * d["seconds"]
                row["memory_gib_seconds"] += d["memory_gib"] * d["seconds"]
            elif e["type"] == EGRESS:
                want[(d["backend"], "*")]["egress_bytes"] += d["bytes"]
        deadline, diff = time.time() + 240, {}
        while time.time() < deadline:
            out = usage()
            assert out.returncode == 0, out.stderr
            rows = {(r["backend"], r["state"]): r for r in json.loads(out.stdout)["rows"]}
            diff = {
                (k, m): (rows.get(k, {}).get(m, 0.0), v)
                for k, ms in want.items()
                for m, v in ms.items()
                if abs(rows.get(k, {}).get(m, 0.0) - v) > 0.01 + 1e-6 * v
            }
            if not diff:
                return
            time.sleep(10)
        pytest.fail(f"k7 usage (got, events) differ after Prometheus caught up: {diff}", pytrace=False)


# ── agent restart: a gap with bounds, no duplicate seconds ──


def test_agent_restart_emits_a_bounded_gap_and_no_duplicate_seconds(
    lifecycle: Lifecycle,
    openmeter: OpenMeter,
    base: str,  # noqa: F811
    admin: str,  # noqa: F811
):
    with tenant(base, admin) as t:
        backend = _installed()[0]
        r = t.create("gap", backend=backend, limits=dict(SMALL), node_name=NODE)
        assert r.status_code == 201, r.text
        t.wait_ready("gap", timeout=600)
        uid = kjson("-n", t.ns, "get", "deployment", "gap")["metadata"]["annotations"][SANDBOX_UID_ANN]
        pod = _ready_cri(t.ns, "gap")
        time.sleep(3 * INTERVAL)
        old = _agent_pod()["metadata"]["name"]
        kubectl("-n", system_ns(), "delete", "pod", old, "--wait=true", "--timeout=120s", check=True)
        assert _agent_pod()["metadata"]["name"] != old
        time.sleep(3 * INTERVAL)
        d0 = time.time()
        assert t("DELETE", "/api/v1/sandboxes/gap", params={"namespace": t.ns}).status_code == 200
        _wait_gone(t.ns, pod["id"], d0)
        time.sleep(2 * INTERVAL)
        _settle(openmeter)
        events = [
            e
            for e in openmeter.events(f"{t.name}/", d0 - 3600, time.time() + 60)
            if e["data"].get("sandbox_uid") == uid
        ]
    gaps = [e for e in events if e["type"] == GAP and e["data"]["meter"] == "sandbox"]
    assert len(gaps) == 1 and gaps[0]["data"]["reason"] == "agent-restart", gaps
    g = (_ts(gaps[0]["data"]["gap_start"]), _ts(gaps[0]["data"]["gap_end"]))
    assert g[0] < g[1], g
    windows = sorted(_window(e) for e in events if e["type"] == SANDBOX)
    assert any(w[1] <= g[0] for w in windows) and any(w[0] >= g[1] for w in windows), (windows, g)
    spans = sorted([*windows, g])
    for (_, a_end), (b_start, _) in zip(spans, spans[1:], strict=False):
        assert abs(b_start - a_end) <= 0.0015, f"windows and the gap must tile the lifetime exactly: {spans}"
    ids = [e["id"] for e in events]
    assert len(ids) == len(set(ids))


# ── spool replay: identical ids, deduplicated by OpenMeter ──


def test_spool_replay_resends_identical_events_that_openmeter_deduplicates(
    lifecycle: Lifecycle, metering: Metering, openmeter: OpenMeter
):
    # New sink names start at the oldest spool segment: the agent re-delivers what it already delivered.
    metering.publish(metering.with_sinks(_sinks(openmeter, "-replay")))
    _settle(openmeter)
    replayed = _stdout_events(f"{lifecycle.tenant.name}/", [_agent_pod()])
    common = set(replayed) & set(lifecycle.stdout)
    assert common, "the replay re-sent none of the lifecycle events (spool segments gone?)"
    assert all(replayed[i] == lifecycle.stdout[i] for i in common), "a replayed event differs from the original"
    dupes = {i: n for i in sorted(common) if (n := openmeter.count(i)) != 1}
    assert not dupes, f"OpenMeter stored replayed ids more than once: {dupes}"


# ── spool overflow, the drop counter, its alert, and requireDelivery ──


def _disk_events(cluster: str, n: int) -> list[dict]:
    now = time.time()
    out = []
    for _ in range(n):
        uid = uuid.uuid4().hex
        data = {
            "tenant": f"{PREFIX}overflow",
            "namespace": f"{PREFIX}overflow",
            "sandbox": uid[:12],
            "sandbox_uid": uid,
            "api_key_id": None,
            "backend": KFD,
            "state": "running",
            "window_start": iso(now - 60),
            "window_end": iso(now),
            "seconds": 60.0,
            "bytes": GIB,
            "gib_seconds": 60.0,
        }
        out.append(
            envelope(
                DISK, f"k7://{cluster}/storage", f"{PREFIX}overflow/x/{uid[:12]}", uuid.uuid4().hex * 2, iso(now), data
            )
        )
    return out


def _alerts(names: set[str]) -> list[dict]:
    return [a for a in _prom("/api/v1/alerts")["alerts"] if a["labels"]["alertname"] in names]


def test_overflow_drops_counts_alerts_and_require_delivery_refuses_with_503(
    lifecycle: Lifecycle,
    metering: Metering,
    openmeter: OpenMeter,
    make_tenant,  # noqa: F811
):
    blackhole = {
        "name": f"{PREFIX}blackhole",
        "type": "http",
        "url": f"{openmeter.url}/{PREFIX}no-such-path",
        "mode": "batch",
        "batchSize": 500,
    }
    doc = metering.with_sinks([blackhole], spool={"maxBytes": 16 * MIB, "highWaterPercent": 1}, requireDelivery=True)
    monitoring = _monitoring_installed()
    with ExitStack() as stack:
        stack.callback(_wait_alerts_clear, monitoring)
        stack.callback(metering.restore)
        metering.publish(doc)
        if monitoring:
            _wait_scraped(_agent_pod()["status"]["podIP"])
        cluster = kjson("-n", system_ns(), "get", "configmap", "k7-cluster-id")["data"]["id"]
        # Storage events through the agent's own hand-off endpoint: the path k7-api's sampler takes.
        for _ in range(120):
            r = _agent("POST", "/agent/v1/usage/events", json={"events": _disk_events(cluster, 500)})
            assert r.status_code == 200, r.text
            if _status()["spool"]["dropped_events"]:
                break
        st = _status()
        assert st["spool"]["dropped_events"] > 0 and st["spool"]["above_high_water"], st["spool"]
        assert st["spool"]["bytes"] <= 16 * MIB, st["spool"]
        assert "HTTP 4" in (st["sinks"][blackhole["name"]]["stopped"] or ""), st["sinks"]
        assert st["status"] == "degraded" and any("spool overflow" in d for d in st["degraded"]), st
        text = _scrape(_agent_pod())
        assert _sum([text], "k7_usage_events_dropped_total") >= st["spool"]["dropped_events"]
        assert _sum([text], "k7_usage_sink_stopped", sink=blackhole["name"]) == 1

        t, backend = make_tenant(), _installed()[0]
        r = t.create("pinned", backend=backend, node_name=NODE)
        assert r.status_code == 503, r.text
        err = error(r)
        assert err["code"] == "UsageSpoolFull" and err["details"]["nodes"] == [NODE], err
        assert "high-water" in err["message"], err
        r = t.create("unpinned", backend=backend)
        if r.status_code == 503:
            assert error(r)["code"] == "UsageSpoolFull" and NODE in error(r)["details"]["nodes"], r.text
        else:
            assert r.status_code == 201, r.text
            affinity = kjson("-n", t.ns, "get", "deployment", "unpinned")["spec"]["template"]["spec"]["affinity"]
            terms = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
            assert all(
                any(x["operator"] == "NotIn" and NODE in x["values"] for x in term["matchExpressions"])
                for term in terms
            ), terms

        if monitoring:
            want, deadline = {"K7UsageSpoolOverflow", "K7UsageSinkStopped"}, time.time() + 300
            while (firing := {a["labels"]["alertname"] for a in _alerts(want) if a["state"] == "firing"}) != want:
                assert time.time() < deadline, f"only {sorted(firing)} of {sorted(want)} fired within 300s of the drop"
                time.sleep(10)


def _wait_scraped(ip: str) -> None:
    """Prometheus holds a pre-drop sample of the new agent's counter, so increase() can see the drop."""
    deadline = time.time() + 180
    query = "/api/v1/query?query=k7_usage_events_dropped_total"
    while not any(r["metric"].get("instance", "").startswith(f"{ip}:") for r in _prom(query)["result"]):
        assert time.time() < deadline, f"Prometheus did not scrape the k7-agent at {ip} within 180s"
        time.sleep(5)


def _wait_alerts_clear(monitoring: bool) -> None:
    """The overflow alert looks back 10 minutes: wait it out so later suites see a healthy cluster."""
    if not monitoring:
        return
    deadline = time.time() + 1200
    while left := _alerts({"K7UsageSpoolOverflow", "K7UsageSinkStopped"}):
        assert time.time() < deadline, f"metering alerts still active 20 minutes after the restore: {left}"
        time.sleep(15)
