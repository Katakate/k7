"""k7-api side of metering: GET /api/v1/usage, requireDelivery, create-time stamps."""

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from kubernetes_asyncio import client as k

from k7.core import tenancy
from k7.core.core import _stamp_metering
from k7.metering import MEMORY_ANN, SANDBOX_UID_ANN, VCPU_ANN, config, delivery, showback
from tests.unit.auth_fixtures import api
from tests.unit.conftest import METERING

T0 = 1_767_225_600


def _cfg(**over) -> config.Config:
    return dataclasses.replace(config.parse(METERING), **over)


def _prometheus(status: int = 200):
    def handler(req: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="prometheus down")
        metric = req.url.params["query"].split("{")[0].split("[")[0]
        result = []
        if metric == "k7_usage_sandbox_seconds_total":
            labels = {"tenant": "acme", "namespace": "default", "backend": "k7d", "state": "running"}
            result = [{"metric": labels, "values": [[T0 + 10, "15"], [T0 + 25, "30"]]}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": result}})

    real = showback.usage

    async def usage(url, tenant, start, end, groups, _http):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await real(url, tenant, start, end, groups, http)

    return patch("k7.api.main.showback.usage", usage)


@pytest.fixture()
def usage_api():
    with api() as a, patch("k7.api.main.METERING", _cfg(prometheus_url="http://prom:9090")), _prometheus():
        a.add_key("acme-user", role="user", tenant="acme")
        a.add_key("operator", role="operator")
        a.add_key("ns-operator", role="operator", namespaces=["default"])
        yield a


PARAMS = {"from": str(T0), "to": "2026-01-01T00:01:00Z"}


async def test_tenant_key_sees_only_its_own_tenant(usage_api):
    r = await usage_api.call("GET", "/api/v1/usage", "acme-user", params={**PARAMS, "tenant": "acme"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["rows"][0]["tenant"] == "acme" and data["rows"][0]["sandbox_seconds"] == 30
    assert (data["from"], data["to"], data["group_by"]) == (T0, T0 + 60, ["tenant"])
    for params in ({**PARAMS, "tenant": "globex"}, PARAMS):
        r = await usage_api.call("GET", "/api/v1/usage", "acme-user", params=params)
        assert r.status_code == 403, r.text


async def test_cluster_wide_needs_an_unscoped_operator(usage_api):
    assert (await usage_api.call("GET", "/api/v1/usage", "operator", params=PARAMS)).status_code == 200
    assert (await usage_api.call("GET", "/api/v1/usage", "ns-operator", params=PARAMS)).status_code == 403
    assert (await usage_api.call("GET", "/api/v1/usage", None, params=PARAMS)).status_code == 401


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"from": "yesterday", "to": "60"}, "'from' must be RFC 3339"),
        ({"from": "2026-01-01T00:00:00", "to": "60"}, "UTC offset"),
        ({"from": "60", "to": "60"}, "after"),
        ({**PARAMS, "group_by": "sandbox"}, "group_by"),
        ({**PARAMS, "tenant": "Bad_Tenant"}, "valid tenant"),
    ],
)
async def test_bad_queries_are_400(usage_api, params, needle):
    r = await usage_api.call("GET", "/api/v1/usage", "operator", params=params)
    assert r.status_code == 400 and needle in r.text, r.text


async def test_unset_prometheus_url_names_the_key():
    with api() as a, patch("k7.api.main.METERING", _cfg()):
        a.add_key("operator", role="operator")
        r = await a.call("GET", "/api/v1/usage", "operator", params=PARAMS)
    assert r.status_code == 400 and "metering.prometheusUrl is unset" in r.text


async def test_prometheus_failure_is_502():
    with api() as a, patch("k7.api.main.METERING", _cfg(prometheus_url="http://prom:9090")), _prometheus(503):
        a.add_key("operator", role="operator")
        r = await a.call("GET", "/api/v1/usage", "operator", params=PARAMS)
    assert r.status_code == 502 and r.json()["error"]["code"] == "PrometheusUnavailable" and "prometheus down" in r.text


# --- requireDelivery ---------------------------------------------------------


def _dep(node_selector=None):
    pod = k.V1PodSpec(containers=[k.V1Container(name="sandbox")], node_selector=node_selector)
    return k.V1Deployment(spec=k.V1DeploymentSpec(selector=k.V1LabelSelector(), template=k.V1PodTemplateSpec(spec=pod)))


def _status(over: bool) -> dict:
    return {"spool": {"above_high_water": over, "bytes": 900, "max_bytes": 1000, "high_water_percent": 80}}


def _core(spools: dict[str, bool | None]):
    async def agent(node, method, path, timeout):
        assert (method, path) == ("GET", "/agent/v1/usage/status")
        if spools[node] is None:
            raise RuntimeError("agent unreachable")
        return _status(bool(spools[node]))

    v1 = AsyncMock()
    v1.list_node.return_value = SimpleNamespace(
        items=[SimpleNamespace(metadata=SimpleNamespace(name=n)) for n in spools]
    )
    return SimpleNamespace(_agent_request=agent, _get_core_v1_client=AsyncMock(return_value=v1))


@pytest.fixture()
def require_delivery(metering_env):
    metering_env.write_text(json.dumps({**METERING, "requireDelivery": True}))


async def test_off_by_default_checks_nothing():
    core = SimpleNamespace(_agent_request=AsyncMock(side_effect=AssertionError("called")))
    await delivery.require(core, _dep(), None)


async def test_pinned_create_on_an_overflowing_node_is_503(require_delivery):
    with pytest.raises(tenancy.AdmissionError) as e:
        await delivery.require(_core({"n1": True, "n2": False}), _dep({delivery.HOSTNAME: "n1"}), None)
    assert (e.value.status, e.value.code) == (503, "UsageSpoolFull")
    assert "node n1" in e.value.message and "above its 80% high-water mark" in e.value.message


async def test_unpinned_create_is_steered_off_overflowing_and_unknown_nodes(require_delivery):
    dep = _dep()
    await delivery.require(_core({"n1": True, "n2": False, "n3": None}), dep, None)
    (term,) = (
        dep.spec.template.spec.affinity.node_affinity.required_during_scheduling_ignored_during_execution.node_selector_terms
    )
    (expr,) = term.match_expressions
    assert (expr.key, expr.operator, expr.values) == (delivery.HOSTNAME, "NotIn", ["n1", "n3"])


async def test_tenant_nodes_bound_the_candidates_and_all_over_is_503(require_delivery):
    tenant = {"spec": {"nodes": ["n1"]}}
    with pytest.raises(tenancy.AdmissionError, match="usage spool state unknown"):
        await delivery.require(_core({"n1": None, "n2": False}), _dep(), tenant)


async def test_existing_affinity_terms_gain_the_exclusion(require_delivery):
    dep = _dep()
    req = k.V1NodeSelectorRequirement(key="k7.katakate.org/backend-k7d", operator="Exists")
    dep.spec.template.spec.affinity = k.V1Affinity(
        node_affinity=k.V1NodeAffinity(
            required_during_scheduling_ignored_during_execution=k.V1NodeSelector(
                node_selector_terms=[k.V1NodeSelectorTerm(match_expressions=[req])]
            )
        )
    )
    await delivery.require(_core({"n1": True, "n2": False}), dep, None)
    (term,) = (
        dep.spec.template.spec.affinity.node_affinity.required_during_scheduling_ignored_during_execution.node_selector_terms
    )
    assert [e.operator for e in term.match_expressions] == ["Exists", "NotIn"]


# --- create-time stamps ------------------------------------------------------


def test_stamp_metering_sets_a_fresh_uid_and_the_reserved_size():
    res = k.V1ResourceRequirements(limits={"cpu": "1500m", "memory": "2Gi"})
    tmpl = k.V1PodTemplateSpec(
        metadata=k.V1ObjectMeta(annotations={VCPU_ANN: "9", SANDBOX_UID_ANN: "copied-from-source"}),
        spec=k.V1PodSpec(containers=[k.V1Container(name="sidecar"), k.V1Container(name="sandbox", resources=res)]),
    )
    dep = k.V1Deployment(
        metadata=k.V1ObjectMeta(name="sb"), spec=k.V1DeploymentSpec(selector=k.V1LabelSelector(), template=tmpl)
    )
    _stamp_metering(dep)
    for meta in (dep.metadata, dep.spec.template.metadata):
        assert meta.annotations[VCPU_ANN] == "1.5" and meta.annotations[MEMORY_ANN] == str(2 * 2**30)
        assert meta.annotations[SANDBOX_UID_ANN] != "copied-from-source"
    assert dep.metadata.annotations[SANDBOX_UID_ANN] == dep.spec.template.metadata.annotations[SANDBOX_UID_ANN]
    unsized = k.V1Deployment(
        metadata=k.V1ObjectMeta(annotations={MEMORY_ANN: "1"}),
        spec=k.V1DeploymentSpec(
            selector=k.V1LabelSelector(),
            template=k.V1PodTemplateSpec(
                metadata=k.V1ObjectMeta(), spec=k.V1PodSpec(containers=[k.V1Container(name="sandbox")])
            ),
        ),
    )
    _stamp_metering(unsized)
    assert set(unsized.metadata.annotations) == {SANDBOX_UID_ANN}


async def test_every_create_path_is_stamped_and_gated(require_delivery):
    from k7.core.core import K7Core

    core = K7Core()
    apps = AsyncMock()
    core._get_apps_v1_client = AsyncMock(return_value=apps)
    dep = k.V1Deployment(
        metadata=k.V1ObjectMeta(name="sb"),
        spec=k.V1DeploymentSpec(
            selector=k.V1LabelSelector(),
            template=k.V1PodTemplateSpec(
                metadata=k.V1ObjectMeta(), spec=k.V1PodSpec(containers=[k.V1Container(name="sandbox")])
            ),
        ),
    )
    await core._create_sandbox_deployment("default", dep)
    assert SANDBOX_UID_ANN in dep.metadata.annotations and apps.create_namespaced_deployment.await_count == 1
    core.admission = SimpleNamespace(tenant=None)
    full = AsyncMock(side_effect=tenancy.AdmissionError(503, "UsageSpoolFull", "full", {}))
    with patch("k7.core.core.delivery.require", full), pytest.raises(tenancy.AdmissionError):
        await core._create_sandbox_deployment("default", dep)
    assert apps.create_namespaced_deployment.await_count == 1
