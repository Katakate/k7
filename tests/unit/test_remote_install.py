"""k7.api.remote_install against fake Kubernetes clients: refusals, the image pin,
the operation record, node labels, and every status state."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from typing import Any

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from k7 import __version__
from k7.api import remote_install as ri

IMAGE = "ghcr.io/katakate/k7-node-installer@sha256:" + "c" * 64
NS_ = "k7-system"
OP = "0123456789abcdef"
NOW = datetime.now(timezone.utc)
SERVER = {"node-role.kubernetes.io/control-plane": "true"}
CFG = {"distro": "k3s", "backends": ["k7d"], "diskSource": "loopback"}


def _cm(name: str, data: dict[str, str], created: datetime = NOW, labels: dict | None = None) -> NS:
    return NS(metadata=NS(name=name, creation_timestamp=created, labels=labels), data=data)


def _cs(ready=False, restarts=0, waiting=None, terminated=None, running=False) -> NS:
    state = NS(
        waiting=NS(reason=waiting[0], message=waiting[1]) if waiting else None,
        terminated=NS(exit_code=terminated) if terminated is not None else None,
        running=NS() if running else None,
    )
    return NS(name=ri.CONTAINER, ready=ready, restart_count=restarts, state=state)


class FakeCore:
    def __init__(self, nodes: dict[str, dict[str, str]], cfg: dict | str | None = CFG):
        self.nodes = {n: NS(metadata=NS(name=n, labels=dict(lbl))) for n, lbl in nodes.items()}
        self.cms: dict[str, NS] = {}
        if cfg is not None:
            self.cms[ri.DAEMONSET] = _cm(
                ri.DAEMONSET, {"config.json": cfg if isinstance(cfg, str) else json.dumps(cfg)}
            )
        self.pods: dict[str, list[NS]] = {}
        self.logs: dict[tuple[str, bool], str] = {}
        self.patched: list[str] = []
        self.patch_error: ApiException | None = None

    async def read_node(self, name):
        if name not in self.nodes:
            raise ApiException(status=404)
        return self.nodes[name]

    async def patch_node(self, name, body):
        if self.patch_error:
            raise self.patch_error
        self.patched.append(name)
        labels = self.nodes[name].metadata.labels
        for k, v in body["metadata"]["labels"].items():
            if v is None:
                labels.pop(k, None)
            else:
                labels[k] = v

    async def list_node(self, label_selector):
        k, v = label_selector.split("=")
        return NS(items=[n for n in self.nodes.values() if n.metadata.labels.get(k) == v])

    async def read_namespaced_config_map(self, name, ns):
        assert ns == NS_
        if name not in self.cms:
            raise ApiException(status=404)
        return self.cms[name]

    async def list_namespaced_config_map(self, ns, label_selector):
        k, v = label_selector.split("=")
        return NS(items=[c for c in self.cms.values() if (c.metadata.labels or {}).get(k) == v])

    async def delete_namespaced_config_map(self, name, ns):
        del self.cms[name]

    async def create_namespaced_config_map(self, ns, body):
        self.cms[body.metadata.name] = _cm(body.metadata.name, body.data, labels=body.metadata.labels)

    async def list_namespaced_pod(self, ns, label_selector, field_selector):
        assert label_selector == ri.POD_SELECTOR
        return NS(items=self.pods.get(field_selector.removeprefix("spec.nodeName="), []))

    async def read_namespaced_pod_log(self, pod, ns, container, previous, tail_lines):
        return self.logs.get((pod, previous), "")


class FakeApps:
    def __init__(self, image: str | None = IMAGE, selector: dict | None = None):
        self.image, self.selector = image, {ri.POOL_LABEL: "true"} if selector is None else selector

    async def read_namespaced_daemon_set(self, name, ns):
        if self.image is None:
            raise ApiException(status=404)
        container = NS(name=ri.CONTAINER, image=self.image)
        return NS(spec=NS(template=NS(spec=NS(containers=[container], node_selector=self.selector))))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(ri.IMAGE_ENV, IMAGE)
    monkeypatch.setenv(ri.NAMESPACE_ENV, NS_)


async def _start(core: FakeCore, apps: FakeApps | None = None, nodes: list[str] | None = None) -> dict[str, Any]:
    return await ri.start(core, apps or FakeApps(), OP, nodes or ["n1"], "key-1")  # type: ignore[arg-type]


# --- request body ---


@pytest.mark.parametrize(
    "body",
    [None, [], {}, {"nodes": []}, {"nodes": "n1"}, {"nodes": ["n1", "n1"]}, {"nodes": ["N1"]}, {"nodes": [1]}],
)
def test_parse_nodes_refuses(body):
    with pytest.raises(ri.RemoteInstallError) as e:
        ri.parse_nodes(body)
    assert e.value.status == 400


def test_parse_nodes_caps_the_batch():
    assert ri.parse_nodes({"nodes": [f"n{i}" for i in range(ri.MAX_NODES)]})
    with pytest.raises(ri.RemoteInstallError, match="1 to 100"):
        ri.parse_nodes({"nodes": [f"n{i}" for i in range(ri.MAX_NODES + 1)]})


def test_parse_nodes_names_the_rejected_keys():
    with pytest.raises(ri.RemoteInstallError, match=r"\['image', 'playbook'\]"):
        ri.parse_nodes({"nodes": ["n1"], "playbook": "x", "image": "y"})


# --- deploy-time pin ---


@pytest.mark.parametrize("image", ["", "ghcr.io/katakate/k7-node-installer:0.7.0", "k7-api:local"])
async def test_unpinned_installer_image_is_a_loud_500(monkeypatch, image):
    monkeypatch.setenv(ri.IMAGE_ENV, image)
    core = FakeCore({"n1": {}})
    with pytest.raises(ri.RemoteInstallError, match=ri.IMAGE_ENV) as e:
        await _start(core)
    assert e.value.status == 500 and not core.patched


async def test_local_installer_image_is_accepted(monkeypatch):
    monkeypatch.setenv(ri.IMAGE_ENV, "k7-node-installer:local")
    assert (await _start(FakeCore({"n1": {}}), FakeApps("k7-node-installer:local")))["operation"] == OP


async def test_unset_namespace_is_a_loud_500(monkeypatch):
    monkeypatch.delenv(ri.NAMESPACE_ENV)
    with pytest.raises(ri.RemoteInstallError, match=ri.NAMESPACE_ENV):
        await _start(FakeCore({"n1": {}}))


@pytest.mark.parametrize(
    "apps,match",
    [
        (FakeApps(None), "not found"),
        (FakeApps("ghcr.io/evil/k7-node-installer@sha256:" + "d" * 64), "not the deploy-time"),
        (FakeApps(selector={}), "does not select"),
    ],
)
async def test_daemonset_drift_is_a_loud_500_before_any_label(apps, match):
    core = FakeCore({"n1": {}})
    with pytest.raises(ri.RemoteInstallError, match=match) as e:
        await _start(core, apps)
    assert e.value.status == 500 and not core.patched and list(core.cms) == [ri.DAEMONSET]


@pytest.mark.parametrize("cfg,match", [(None, "not found"), ("{", "invalid"), ({"distro": "k3s"}, "invalid")])
async def test_missing_or_invalid_installer_config_is_a_loud_500(cfg, match):
    core = FakeCore({"n1": {}}, cfg)
    with pytest.raises(ri.RemoteInstallError, match=match) as e:
        await _start(core)
    assert e.value.status == 500 and not core.patched


# --- refusals ---


async def test_unknown_nodes_are_404_and_nothing_is_labelled():
    core = FakeCore({"n1": {}})
    with pytest.raises(ri.RemoteInstallError, match="ghost") as e:
        await _start(core, nodes=["n1", "ghost"])
    assert e.value.status == 404 and not core.patched and ri.RECORD_PREFIX + OP not in core.cms


async def test_k3s_server_without_allow_server_restart_is_409():
    core = FakeCore({"n1": {}, "cp": SERVER})
    with pytest.raises(ri.RemoteInstallError, match="cp: k3s server") as e:
        await _start(core, nodes=["n1", "cp"])
    assert e.value.status == 409 and not core.patched


@pytest.mark.parametrize("cfg", [{**CFG, "allowServerRestart": True}, {**CFG, "distro": "kubeadm"}])
async def test_server_is_allowed_when_the_installer_would_allow_it(cfg):
    core = FakeCore({"cp": SERVER}, cfg)
    await _start(core, nodes=["cp"])
    assert core.patched == ["cp"]


# --- start ---


async def test_start_records_the_operation_then_labels_the_nodes():
    core = FakeCore({"n1": {}, "n2": {"keep": "me"}})
    out = await _start(core, nodes=["n1", "n2"])
    assert out == {"operation": OP, "nodes": ["n1", "n2"], "status": f"/api/v1/install/{OP}"}
    rec = core.cms[ri.RECORD_PREFIX + OP]
    assert rec.data == {"nodes": '["n1", "n2"]', "image": IMAGE, "subject": "key-1"}
    assert rec.metadata.labels == ri.RECORD_LABELS
    assert core.patched == ["n1", "n2"]
    assert core.nodes["n2"].metadata.labels == {"keep": "me", ri.POOL_LABEL: "true", ri.ADDED_BY_LABEL: ri.ADDED_BY}


async def test_start_leaves_nodes_already_in_the_pool_unmarked():
    core = FakeCore({"n1": {ri.POOL_LABEL: "true"}, "n2": {}})
    await _start(core, nodes=["n1", "n2"])
    assert core.patched == ["n2"]
    assert core.nodes["n1"].metadata.labels == {ri.POOL_LABEL: "true"}


# --- converge_off (remote install turned off) ---


async def test_converge_off_unmarks_added_nodes_keeps_them_pooled_and_drops_records():
    core = FakeCore({"operator": {ri.POOL_LABEL: "true", "keep": "me"}, "added": {}, "other": {"x": "y"}})
    await _start(core, nodes=["operator", "added"])
    core.cms["k7-install-old"] = _cm("k7-install-old", {}, NOW - timedelta(days=1), ri.RECORD_LABELS)
    assert await ri.converge_off(core, NS_) == ["added"]  # type: ignore[arg-type]
    # Still pooled: k7-agent (pause, resume, storage) selects the same label on the chart.
    assert core.nodes["added"].metadata.labels == {ri.POOL_LABEL: "true"}
    assert core.nodes["operator"].metadata.labels == {ri.POOL_LABEL: "true", "keep": "me"}
    assert core.nodes["other"].metadata.labels == {"x": "y"}
    assert set(core.cms) == {ri.DAEMONSET}


async def test_converge_off_is_idempotent():
    core = FakeCore({"n1": {}})
    await _start(core)
    await ri.converge_off(core, NS_)  # type: ignore[arg-type]
    core.patched.clear()
    assert await ri.converge_off(core, NS_) == []  # type: ignore[arg-type]
    assert core.patched == []


async def test_label_failure_is_a_500_naming_the_operation():
    core = FakeCore({"n1": {}})
    core.patch_error = ApiException(status=422, reason="Invalid")
    with pytest.raises(ri.RemoteInstallError, match=f"operation {OP}: labelling n1") as e:
        await _start(core)
    assert e.value.status == 500


async def test_start_prunes_records_past_their_ttl():
    core = FakeCore({"n1": {}})
    core.cms["k7-install-old"] = _cm("k7-install-old", {}, NOW - ri.RECORD_TTL - timedelta(hours=1), ri.RECORD_LABELS)
    core.cms["k7-install-new"] = _cm("k7-install-new", {}, NOW - timedelta(days=1), ri.RECORD_LABELS)
    await _start(core)
    assert "k7-install-old" not in core.cms and "k7-install-new" in core.cms


# --- status ---


def _pod(name: str, cs: NS | None, age_s: int = 0) -> NS:
    return NS(
        metadata=NS(name=name, creation_timestamp=NOW - timedelta(seconds=age_s)),
        status=NS(container_statuses=[cs] if cs else None),
    )


async def _status(core: FakeCore, op: str = OP) -> dict[str, Any]:
    return await ri.status(core, op)  # type: ignore[arg-type]


async def _one(labels: dict[str, str], pods: list[NS], logs: dict | None = None) -> dict[str, Any]:
    core = FakeCore({"n1": labels})
    await _start(core)
    core.nodes["n1"].metadata.labels.update(labels)
    core.pods["n1"] = pods
    core.logs = logs or {}
    out = await _status(core)
    assert out["operation"] == OP and out["image"] == IMAGE and out["created_at"] == NOW.isoformat()
    (node,) = out["nodes"]
    return {"overall": out["state"], **node}


READY = {ri.installer.READY_LABEL: __version__}


@pytest.mark.parametrize(
    "labels,pods,state,error",
    [
        ({}, [], "pending", None),
        ({}, [_pod("p", None)], "pending", None),
        ({}, [_pod("p", _cs())], "pending", None),
        ({}, [_pod("p", _cs(running=True))], "running", None),
        (READY, [_pod("p", _cs(ready=True))], "succeeded", None),
        ({ri.installer.READY_LABEL: "0.0.1"}, [_pod("p", _cs(ready=True))], "failed", "expected"),
        ({}, [_pod("p", _cs(waiting=("ErrImagePull", "not found")))], "failed", "ErrImagePull: not found"),
        ({}, [_pod("p", _cs(terminated=2))], "failed", "installer exited non-zero"),
        # The newest pod wins: an old crashed pod does not fail a fresh rollout.
        ({}, [_pod("old", _cs(restarts=3), age_s=60), _pod("new", _cs(running=True))], "running", None),
    ],
)
async def test_node_states(labels, pods, state, error):
    node = await _one(labels, pods)
    assert node["state"] == state
    assert node.get("error") == error or (error and error in node["error"])
    assert node["overall"] == {"pending": "running"}.get(state, state)


async def test_crashloop_reports_the_installers_own_failure_line():
    log = "k7-node-installer: running node prep\nk7-node-installer: FAILED: n1 is a k3s server\n"
    cs = _cs(restarts=2, waiting=("CrashLoopBackOff", "back-off"))
    node = await _one({}, [_pod("p", cs)], {("p", True): log})
    assert node["state"] == "failed" and node["error"] == "k7-node-installer: FAILED: n1 is a k3s server"


async def test_node_removed_or_unlabelled_is_failed():
    core = FakeCore({"n1": {}, "n2": {}})
    await _start(core, nodes=["n1", "n2"])
    del core.nodes["n1"]
    core.nodes["n2"].metadata.labels.pop(ri.POOL_LABEL)
    states = {n["node"]: n for n in (await _status(core))["nodes"]}
    assert "no longer exists" in states["n1"]["error"] and ri.POOL_LABEL in states["n2"]["error"]


async def test_overall_state_needs_every_node():
    core = FakeCore({"n1": {}, "n2": {}})
    await _start(core, nodes=["n1", "n2"])
    core.nodes["n1"].metadata.labels.update(READY)
    core.pods["n1"] = [_pod("p1", _cs(ready=True))]
    assert (await _status(core))["state"] == "running"
    core.nodes["n2"].metadata.labels.update(READY)
    core.pods["n2"] = [_pod("p2", _cs(ready=True))]
    assert (await _status(core))["state"] == "succeeded"
    core.pods["n2"] = [_pod("p2", _cs(terminated=1))]
    assert (await _status(core))["state"] == "failed"


@pytest.mark.parametrize("op", ["ffffffffffffffff", "../../etc", OP.upper(), ""])
async def test_unknown_or_malformed_operation_is_404(op):
    with pytest.raises(ri.RemoteInstallError, match="not found") as e:
        await _status(FakeCore({}), op)
    assert e.value.status == 404


def test_enabled_only_for_exactly_1(monkeypatch):
    for value, want in (("1", True), ("true", False), ("0", False)):
        monkeypatch.setenv(ri.ENABLE_ENV, value)
        assert ri.enabled() is want
    monkeypatch.delenv(ri.ENABLE_ENV)
    assert ri.enabled() is False
