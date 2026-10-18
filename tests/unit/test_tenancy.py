"""Tenants, production mode, quotas under the per-tenant Lease, per-VM limits.

The fake cluster below keeps Deployments, PVCs, VolumeSnapshots and Leases in
memory with real resourceVersion / 409 semantics and an ``await`` at every
call, so concurrent admissions interleave the way two API replicas do.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from k7 import metrics
from k7.api import main as api_main
from k7.api.keystore import FileKeyStore
from k7.core import tenancy
from k7.core.models import OperationResult, SandboxConfig
from k7.core.tenancy import KFD, Admission, AdmissionError

GI = Decimal(2**30)
SYS_NS = "kube-system"


def tenant(name: str = "acme", namespaces=("acme-dev",), **spec) -> dict:
    return {"metadata": {"name": name, "generation": 3}, "spec": {"namespaces": list(namespaces), **spec}}


def deployment(
    name: str = "sb",
    backend: str = KFD,
    cpu: str | None = "1",
    memory: str | None = "1Gi",
    pvcs: tuple[str, ...] = (),
    ephemeral: str | None = None,
    ephemeral_claim: str | None = None,
    node: str | None = None,
    tolerations: list | None = None,
    annotations: dict | None = None,
) -> client.V1Deployment:
    limits = {k: v for k, v in (("cpu", cpu), ("memory", memory), ("ephemeral-storage", ephemeral)) if v}
    vols = [
        client.V1Volume(name=p, persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=p))
        for p in pvcs
    ]
    if ephemeral_claim:
        tmpl = client.V1PersistentVolumeClaimTemplate(
            spec=client.V1PersistentVolumeClaimSpec(
                resources=client.V1VolumeResourceRequirements(requests={"storage": ephemeral_claim})
            )
        )
        vols.append(
            client.V1Volume(name="scratch", ephemeral=client.V1EphemeralVolumeSource(volume_claim_template=tmpl))
        )
    pod = client.V1PodSpec(
        containers=[client.V1Container(name="sandbox", resources=client.V1ResourceRequirements(limits=limits))],
        volumes=vols or None,
        node_selector={"kubernetes.io/hostname": node} if node else None,
        tolerations=tolerations,
    )
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=name, labels={"katakate.org/sandbox": name}, annotations={tenancy.BACKEND_ANN: backend}
        ),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"katakate.org/sandbox": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"katakate.org/sandbox": name}, annotations=annotations),
                spec=pod,
            ),
        ),
    )


def adm(t: dict | None, key: str = "key-a", mode: str = "development", admin: bool = False) -> Admission:
    return Admission(key_id=key, admin=admin, tenant=t, mode=mode)


# ── fake cluster ────────────────────────────────────────────────────


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


class FakeLeases:
    """coordination.k8s.io/v1 Leases with optimistic concurrency."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], SimpleNamespace] = {}
        self.rv = 0
        self.fail_replace = False

    def _obj(self, body: dict) -> SimpleNamespace:
        self.rv += 1
        s = body["spec"]
        return SimpleNamespace(
            metadata=SimpleNamespace(name=body["metadata"]["name"], resource_version=str(self.rv)),
            spec=SimpleNamespace(
                holder_identity=s["holderIdentity"],
                lease_duration_seconds=s["leaseDurationSeconds"],
                acquire_time=_parse(s["acquireTime"]),
                renew_time=_parse(s["renewTime"]),
            ),
        )

    def hold(self, name: str, holder: str, ttl: int = 10, age: float = 0.0) -> None:
        self.rv += 1
        when = datetime.now(timezone.utc) - timedelta(seconds=age)
        self.store[(SYS_NS, name)] = SimpleNamespace(
            metadata=SimpleNamespace(name=name, resource_version=str(self.rv)),
            spec=SimpleNamespace(
                holder_identity=holder, lease_duration_seconds=ttl, acquire_time=when, renew_time=when
            ),
        )

    async def create_namespaced_lease(self, namespace: str, body: dict):
        await asyncio.sleep(0)
        key = (namespace, body["metadata"]["name"])
        if key in self.store:
            raise ApiException(status=409)
        self.store[key] = self._obj(body)
        return self.store[key]

    async def read_namespaced_lease(self, name: str, namespace: str):
        await asyncio.sleep(0)
        if (namespace, name) not in self.store:
            raise ApiException(status=404)
        return self.store[(namespace, name)]

    async def replace_namespaced_lease(self, name: str, namespace: str, body: dict):
        await asyncio.sleep(0)
        cur = self.store.get((namespace, name))
        if cur is None:
            raise ApiException(status=404)
        if self.fail_replace or body["metadata"].get("resourceVersion") != cur.metadata.resource_version:
            raise ApiException(status=409)
        self.store[(namespace, name)] = self._obj(body)
        return self.store[(namespace, name)]

    async def delete_namespaced_lease(self, name: str, namespace: str, body=None):
        await asyncio.sleep(0)
        cur = self.store.get((namespace, name))
        if cur is None:
            raise ApiException(status=404)
        rv = body.preconditions.resource_version if body is not None else None
        if rv and rv != cur.metadata.resource_version:
            raise ApiException(status=409)
        del self.store[(namespace, name)]


class FakeCluster:
    """Stands in for K7Core's clients: apps, core and custom objects are this object."""

    def __init__(self) -> None:
        self.deps: dict[str, list] = {}
        self.pvcs: dict[str, dict[str, str]] = {}
        self.snaps: dict[str, list[dict]] = {}
        self.leases = FakeLeases()

    async def _get_api_client(self):
        return None

    async def _get_apps_v1_client(self):
        return self

    _get_core_v1_client = _get_custom_objects_client = _get_apps_v1_client

    async def list_namespaced_deployment(self, namespace: str, label_selector: str | None = None):
        await asyncio.sleep(0)
        return SimpleNamespace(items=list(self.deps.get(namespace, [])))

    async def list_namespaced_persistent_volume_claim(self, namespace: str):
        await asyncio.sleep(0)
        items = [
            SimpleNamespace(
                metadata=SimpleNamespace(name=n, owner_references=None),
                spec=SimpleNamespace(resources=SimpleNamespace(requests={"storage": size})),
            )
            for n, size in self.pvcs.get(namespace, {}).items()
        ]
        return SimpleNamespace(items=items)

    async def list_namespaced_custom_object(self, group: str, version: str, namespace: str, plural: str):
        await asyncio.sleep(0)
        return {"items": list(self.snaps.get(namespace, []))}


@pytest.fixture()
def cluster(monkeypatch) -> FakeCluster:
    c = FakeCluster()
    monkeypatch.setattr(tenancy.client, "CoordinationV1Api", lambda _api: c.leases)
    return c


def creator(cluster: FakeCluster, namespace: str, dep, state: dict):
    """create() for admit_deployment: records the Deployment and checks mutual exclusion."""

    async def create():
        state["inside"] = state.get("inside", 0) + 1
        state["max"] = max(state.get("max", 0), state["inside"])
        await asyncio.sleep(0.001)
        cluster.deps.setdefault(namespace, []).append(dep)
        state["inside"] -= 1
        return dep

    return create


# ── quantities and spec validation ──────────────────────────────────


@pytest.mark.parametrize(
    ("value", "want"),
    [("500m", Decimal("0.5")), ("2", Decimal(2)), (3, Decimal(3)), ("4Gi", 4 * GI), ("500M", Decimal(5 * 10**8))],
)
def test_quantity(value, want):
    assert tenancy.quantity(value) == want


@pytest.mark.parametrize("value", ["", "1.2.3", "4GB", "-1", True, "lots"])
def test_quantity_rejects(value):
    with pytest.raises(ValueError):
        tenancy.quantity(value)


@pytest.mark.parametrize(
    ("dim", "value", "want"),
    [
        ("vcpu", Decimal("1.5"), "1500m"),
        ("vcpu", Decimal(2), "2"),
        ("memory", 3 * GI, "3Gi"),
        ("sandboxes", Decimal(4), "4"),
    ],
)
def test_fmt(dim, value, want):
    assert tenancy.fmt(dim, value) == want


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        ({}, "at least one namespace"),
        ({"namespaces": []}, "at least one namespace"),
        ({"namespaces": ["a", "a"]}, "unique"),
        ({"namespaces": ["Bad_NS"]}, "not a valid namespace"),
        ({"namespaces": ["a"], "bogus": 1}, "unknown Tenant spec"),
        ({"namespaces": ["a"], "quotas": {"gpus": 1}}, "unknown key"),
        ({"namespaces": ["a"], "quotas": {"sandboxes": -1}}, "non-negative integer"),
        ({"namespaces": ["a"], "quotas": {"sandboxes": "10"}}, "non-negative integer"),
        ({"namespaces": ["a"], "quotas": {"memory": "lots"}}, "spec.quotas.memory"),
        ({"namespaces": ["a"], "limits": {"network": {"egress": "1M"}}}, "unknown key"),
        ({"namespaces": ["a"], "limits": {"cpuShares": {}}}, "unknown key"),
        (
            {"namespaces": ["a"], "defaultSize": {"cpu": "4"}, "limits": {"maxSandboxSize": {"cpu": "2"}}},
            "above spec.limits.maxSandboxSize.cpu",
        ),
    ],
)
def test_validate_spec_refuses(spec, error):
    with pytest.raises(ValueError, match=error):
        tenancy.validate_spec("acme", spec)


def test_validate_spec_rejects_bad_name_and_accepts_full_spec():
    with pytest.raises(ValueError, match="DNS-1123"):
        tenancy.validate_spec("Acme", {"namespaces": ["a"]})
    tenancy.validate_spec(
        "acme",
        {
            "namespaces": ["acme-dev", "acme-prod"],
            "nodes": ["n1"],
            "defaultSize": {"cpu": "1", "memory": "1Gi", "disk": "10Gi"},
            "quotas": {"sandboxes": 50, "vcpu": "64", "memory": "128Gi", "disk": "2Ti", "snapshotStorage": "500Gi"},
            "limits": {
                "maxSandboxSize": {"cpu": "8", "memory": "32Gi", "disk": "200Gi"},
                "diskIO": {"readBps": "200Mi", "writeBps": "200Mi", "readIops": 5000, "writeIops": 5000},
                "network": {"egressBps": "500M", "ingressBps": "500M"},
            },
        },
    )


def test_crd_schema_matches_validate_spec_fields():
    import yaml

    crd = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/crd-tenants.yaml").read_text()
    )
    spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    assert set(spec) == {"namespaces", "nodes", "defaultSize", "quotas", "limits"}
    assert set(spec["quotas"]["properties"]) == set(tenancy.DIMENSIONS)
    assert set(spec["limits"]["properties"]["diskIO"]["properties"]) == set(tenancy.DISK_IO_KEYS)
    assert set(spec["limits"]["properties"]["network"]["properties"]) == set(tenancy.NETWORK_KEYS)
    assert crd["spec"]["scope"] == "Cluster" and "status" in crd["spec"]["versions"][0]["subresources"]


# ── tenant lookup and key scope ─────────────────────────────────────

A = tenant("acme", ("acme-dev", "acme-prod"))
B = tenant("globex", ("globex",))


def test_namespace_owner():
    assert tenancy.name_of(tenancy.tenant_of_namespace("acme-prod", [A, B])) == "acme"
    assert tenancy.tenant_of_namespace("default", [A, B]) is None


def test_double_claimed_namespace_is_500_naming_both():
    rogue = tenant("initech", ("acme-dev",))
    with pytest.raises(AdmissionError) as e:
        tenancy.tenant_of_namespace("acme-dev", [A, B, rogue])
    assert e.value.status == 500 and e.value.code == "TenantConflict"
    assert "acme and initech" in e.value.message
    assert e.value.details["tenants"] == ["acme", "initech"]


@pytest.mark.parametrize(
    ("key", "want"),
    [
        ({}, None),
        ({"namespaces": ["x"]}, ["x"]),
        ({"tenant": "acme"}, ["acme-dev", "acme-prod"]),
        ({"tenant": "acme", "namespaces": ["acme-prod"]}, ["acme-prod"]),
        ({"tenant": "acme", "namespaces": ["globex"]}, []),
        ({"tenant": "acme", "namespaces": ["acme-dev", "globex"]}, ["acme-dev"]),
    ],
)
def test_scope_is_tenant_intersect_key(key, want):
    assert tenancy.key_namespaces(key, [A, B]) == want


def test_new_tenant_namespace_reaches_existing_key():
    grown = tenant("acme", ("acme-dev", "acme-prod", "acme-ci"))
    assert "acme-ci" in tenancy.key_namespaces({"tenant": "acme"}, [grown, B])


def test_key_bound_to_missing_tenant_is_403():
    with pytest.raises(AdmissionError) as e:
        tenancy.key_namespaces({"tenant": "gone"}, [A])
    assert e.value.status == 403


KEY_SCOPES = {
    "ci": {},  # from before roles, never migrated
    "old-admin-flag": {"admin": True},  # from before roles, never migrated
    "legacy-unscoped": {"role": "admin", "unscoped_legacy": True},
    "ns-only": {"role": "user", "namespaces": ["a"]},
    "bad-role": {"role": "user"},
    "ops": {"role": "operator"},
    "root": {"role": "admin"},
    "acme-bot": {"role": "user", "tenant": "acme"},
    "acme-ro": {"role": "read-only", "tenant": "acme", "namespaces": ["acme-dev"]},
}
REFUSED_IN_PRODUCTION = {"ci", "old-admin-flag", "legacy-unscoped", "ns-only", "bad-role"}


def test_unscoped_keys():
    from k7.deploy.k7_api_keys import unscoped_keys

    assert set(unscoped_keys(KEY_SCOPES)) == REFUSED_IN_PRODUCTION


async def test_missing_crd_is_loud():
    custom = MagicMock(list_cluster_custom_object=AsyncMock(side_effect=ApiException(status=404)))
    core = SimpleNamespace(_get_custom_objects_client=AsyncMock(return_value=custom))
    with pytest.raises(RuntimeError, match="Tenant CRD"):
        await tenancy.list_tenants(core)


# ── production mode startup ─────────────────────────────────────────


def _store(tmp_path: Path, keys: dict) -> FileKeyStore:
    import json

    path = tmp_path / "keys.json"
    path.write_text(json.dumps(keys))
    return FileKeyStore(path)


async def test_production_refuses_unscoped_keys_and_names_them(tmp_path, monkeypatch):
    monkeypatch.setenv("K7_API_TRANSPORT", "https")
    keys = {f"{i:08x}": {"name": name, "hash": f"h{i}", **scope} for i, (name, scope) in enumerate(KEY_SCOPES.items())}
    with pytest.raises(RuntimeError) as e:
        await api_main.production_preflight(_store(tmp_path, keys))
    msg = str(e.value)
    assert {name for name in KEY_SCOPES if f"({name})" in msg} == REFUSED_IN_PRODUCTION
    assert "unscoped_legacy" in msg and "k7 tenant adopt" in msg and "--role user --tenant T" in msg


async def test_production_starts_with_only_scoped_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("K7_API_TRANSPORT", "https")
    await api_main.production_preflight(_store(tmp_path, {"cccccccc": {"name": "b", "hash": "h", "tenant": "acme"}}))


@pytest.mark.parametrize(
    ("env", "error"),
    [
        ({"K7_API_TRANSPORT": "insecure-http"}, "K7_API_TRANSPORT"),
        ({"K7_API_TRANSPORT": "https", "K7_API_ENABLE_REMOTE_INSTALL": "1"}, "REMOTE_INSTALL"),
    ],
)
async def test_production_refuses_insecure_http_and_remote_install(tmp_path, monkeypatch, env, error):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match=error):
        await api_main.production_preflight(_store(tmp_path, {}))


async def test_lifespan_runs_preflight_only_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("K7_API_KEY_STORE", "file")
    monkeypatch.setattr(api_main, "API_KEYS_FILE", tmp_path / "none.json")
    preflight = AsyncMock()
    monkeypatch.setattr(api_main, "production_preflight", preflight)
    reconcile = AsyncMock()
    monkeypatch.setattr(api_main, "reconcile_sandbox_egress", reconcile)
    async with api_main.lifespan(api_main.app):
        pass
    preflight.assert_not_awaited()
    reconcile.assert_awaited_once()
    monkeypatch.setenv("K7_API_MODE", "production")
    async with api_main.lifespan(api_main.app):
        pass
    preflight.assert_awaited_once()
    monkeypatch.setenv("K7_API_MODE", "staging")
    with pytest.raises(RuntimeError, match="K7_API_MODE"):
        async with api_main.lifespan(api_main.app):
            pass


# ── per-sandbox policy: defaultSize, production, limits merge ───────


def cfg(**kw) -> SandboxConfig:
    return SandboxConfig(name="sb", image="alpine:3.20", namespace="acme-dev", **kw)


def test_default_size_fills_only_what_the_request_omits():
    t = tenant(defaultSize={"cpu": "2", "memory": "4Gi", "disk": "20Gi"})
    c = cfg(limits={"cpu": "500m"})
    tenancy.apply_policy(c, KFD, t, "production")
    assert c.limits == {"cpu": "500m", "memory": "4Gi"} and c.root_disk_size == "20Gi"
    c = cfg(root_disk_size="5Gi")
    tenancy.apply_policy(c, KFD, t, "development")
    assert c.root_disk_size == "5Gi" and c.limits == {"cpu": "2", "memory": "4Gi"}


@pytest.mark.parametrize("t", [None, tenant(), tenant(defaultSize={"cpu": "1"})])
def test_production_without_cpu_memory_or_default_is_400(t):
    with pytest.raises(AdmissionError) as e:
        tenancy.apply_policy(cfg(), KFD, t, "production")
    assert e.value.status == 400 and "defaultSize" in e.value.message


def test_development_without_limits_is_today():
    c = cfg()
    tenancy.apply_policy(c, KFD, tenant(), "development")
    assert c.limits == {}


def test_request_may_lower_network_cap_never_raise():
    t = tenant(limits={"network": {"egressBps": "100M"}})
    tenancy.apply_policy(cfg(network_limits={"egressBps": "50M"}), KFD, t, "development")
    with pytest.raises(AdmissionError, match="above tenant acme's cap 100M"):
        tenancy.apply_policy(cfg(network_limits={"egressBps": "200M"}), KFD, t, "development")
    with pytest.raises(AdmissionError, match="unknown key"):
        tenancy.apply_policy(cfg(network_limits={"egress": "1M"}), KFD, t, "development")


def test_network_annotations_are_bits_per_second():
    assert tenancy.network_annotations({"egressBps": "500M", "ingressBps": "1Gi"}, "kata-qemu-longhorn") == {
        tenancy.NETWORK_ANNOTATIONS["egressBps"]: "500000000",
        tenancy.NETWORK_ANNOTATIONS["ingressBps"]: str(2**30),
    }
    assert tenancy.network_annotations({"egressBps": "50M"}, KFD) == {
        tenancy.NETWORK_ANNOTATIONS["egressBps"]: str(tenancy.kata_rate(KFD, 50_000_000))
    }
    with pytest.raises(ValueError):
        tenancy.network_annotations({"rx": "1"}, KFD)


def _kata_revert_bytes(num: int) -> int:
    """Kata 3.24 virtcontainers/utils.RevertBytes, which fc.go applies to rate / 8."""
    a, b = divmod(num, 1000)
    return num if a == 0 else 1024 * _kata_revert_bytes(a) + b


@pytest.mark.parametrize("bps", [8_000, 1_000_000, 50_000_000, 100_000_000, 123_456_789, 10**9, 8 * 1024**3, 10**11])
def test_kfd_rate_survives_katas_firecracker_conversion(bps):
    """What Firecracker's bucket ends up with is the cap (never above, at most 3% below)."""
    assert tenancy.kata_rate("kata-qemu-longhorn", bps) == bps
    got = _kata_revert_bytes(tenancy.kata_rate(KFD, bps) // 8)
    assert bps // 8 * 0.97 <= got <= bps // 8
    assert _kata_revert_bytes(50_000_000 // 8) * 8 > 52_000_000, "unencoded, Kata would run 4.8% fast"


def test_kata_allowlists_the_rate_limiter_annotations():
    path = Path(__file__).resolve().parents[2] / "src/k7/deploy/roles/k7_node_prep/tasks/runtimes.yml"
    edits = [t.get("ansible.builtin.lineinfile") or {} for t in yaml.safe_load(path.read_text())]
    lines = [e["line"] for e in edits if str(e.get("line", "")).startswith("enable_annotations = [")]
    assert len(lines) == 2
    for ann in tenancy.NETWORK_ANNOTATIONS.values():
        short = ann.rsplit(".", 1)[1]
        assert all(f'"{short}"' in line for line in lines), short


@pytest.mark.parametrize("vm_tenant", [tenant(limits={"network": {"egressBps": "100M", "ingressBps": "80M"}})])
def test_tenant_network_cap_is_stamped_and_lowered_request_kept(vm_tenant):
    egress, ingress = tenancy.NETWORK_ANNOTATIONS["egressBps"], tenancy.NETWORK_ANNOTATIONS["ingressBps"]
    dep = deployment(annotations={egress: str(tenancy.kata_rate(KFD, 300_000_000)), ingress: "1000"})
    tenancy.enforce_vm_limits(dep, vm_tenant)
    assert dep.spec.template.metadata.annotations == {egress: str(tenancy.kata_rate(KFD, 100_000_000)), ingress: "1000"}
    dep = deployment(backend="kata-qemu-longhorn")
    tenancy.enforce_vm_limits(dep, vm_tenant)
    assert dep.spec.template.metadata.annotations == {egress: "100000000", ingress: "80000000"}


ENFORCEABLE = {
    # (backend, limit): enforced?
    **{(b, "diskIO"): False for b in tenancy.BACKENDS},
    (KFD, "network"): True,
    ("kata-qemu-longhorn", "network"): True,
    ("k7d", "network"): False,
    ("k7d-fc", "network"): False,
    (KFD, "disk"): True,
    ("kata-qemu-longhorn", "disk"): True,
    ("k7d", "disk"): False,
    ("k7d-fc", "disk"): False,
}


@pytest.mark.parametrize(("backend", "limit"), sorted(ENFORCEABLE))
def test_enforceability_matrix(backend, limit):
    msg = tenancy.unenforceable(backend, limit)
    assert (msg is None) == ENFORCEABLE[(backend, limit)]
    if msg:
        assert backend in msg and limit in msg


@pytest.mark.parametrize("backend", tenancy.BACKENDS)
@pytest.mark.parametrize(
    "spec",
    [
        {"limits": {"diskIO": {"writeBps": "100Mi"}}},
        {"limits": {"network": {"egressBps": "100M"}}},
        {"quotas": {"disk": "100Gi"}},
        {"limits": {"maxSandboxSize": {"disk": "100Gi"}}},
    ],
)
async def test_unenforceable_tenant_limit_fails_create_with_400(cluster, backend, spec):
    limit = (
        "diskIO" if "diskIO" in spec.get("limits", {}) else "network" if "network" in spec.get("limits", {}) else "disk"
    )
    t = tenant(**spec)
    create = AsyncMock()
    dep = deployment(backend=backend)
    if ENFORCEABLE[(backend, limit)]:
        await tenancy.admit_deployment(cluster, adm(t), "acme-dev", dep, create)
        create.assert_awaited_once()
        return
    with pytest.raises(AdmissionError) as e:
        await tenancy.admit_deployment(cluster, adm(t), "acme-dev", dep, create)
    assert e.value.status == 400 and e.value.code == "LimitNotEnforceable"
    assert e.value.details == {"backend": backend, "limit": limit}
    create.assert_not_awaited()


@pytest.mark.parametrize(
    ("dep", "error"),
    [
        (deployment(cpu="9"), "maxSandboxSize.cpu"),
        (deployment(memory="33Gi"), "maxSandboxSize.memory"),
        (deployment(backend="kata-qemu-longhorn", pvcs=("root",)), "maxSandboxSize.disk"),
    ],
)
async def test_above_max_sandbox_size_is_400(cluster, dep, error):
    cluster.pvcs["acme-dev"] = {"root": "300Gi"}
    t = tenant(limits={"maxSandboxSize": {"cpu": "8", "memory": "32Gi", "disk": "200Gi"}})
    with pytest.raises(AdmissionError, match=error) as e:
        await tenancy.admit_deployment(cluster, adm(t), "acme-dev", dep, AsyncMock())
    assert e.value.status == 400


async def test_at_max_sandbox_size_passes(cluster):
    t = tenant(limits={"maxSandboxSize": {"cpu": "8", "memory": "32Gi"}})
    create = AsyncMock()
    await tenancy.admit_deployment(cluster, adm(t), "acme-dev", deployment(cpu="8", memory="32Gi"), create)
    create.assert_awaited_once()


@pytest.mark.parametrize(("dim", "flag"), [("vcpu", "cpu"), ("memory", "memory")])
async def test_cpu_or_memory_quota_requires_that_limit(cluster, dim, flag):
    t = tenant(quotas={dim: "10" if dim == "vcpu" else "10Gi"})
    dep = deployment(cpu=None if flag == "cpu" else "1", memory=None if flag == "memory" else "1Gi")
    with pytest.raises(AdmissionError, match=f"needs a {flag} limit"):
        await tenancy.admit_deployment(cluster, adm(t), "acme-dev", dep, AsyncMock())


async def test_production_admission_requires_limits_even_without_tenant(cluster):
    with pytest.raises(AdmissionError, match="production mode requires"):
        await tenancy.admit_deployment(
            cluster, adm(None, mode="production"), "default", deployment(cpu=None), AsyncMock()
        )


# ── identity labels and dedicated nodes ─────────────────────────────


def test_stamp_sets_identity_labels_on_deployment_and_pods():
    dep = deployment()
    dep.metadata.labels[tenancy.TENANT_LABEL] = "spoofed"
    tenancy.stamp(dep, adm(A, key="abcd1234"))
    for meta in (dep.metadata, dep.spec.template.metadata):
        assert meta.labels[tenancy.TENANT_LABEL] == "acme" and meta.labels[tenancy.KEY_ID_LABEL] == "abcd1234"


def test_stamp_without_tenant_drops_spoofed_tenant_label():
    dep = deployment()
    dep.spec.template.metadata.labels[tenancy.TENANT_LABEL] = "acme"
    tenancy.stamp(dep, adm(None, key="k"))
    assert tenancy.TENANT_LABEL not in dep.spec.template.metadata.labels


def test_tenant_nodes_get_selector_and_toleration_without_a_pin():
    t = tenant(nodes=["n1", "n2"])
    dep = deployment()
    tenancy.stamp(dep, adm(t))
    pod = dep.spec.template.spec
    assert pod.node_selector == {tenancy.TENANT_LABEL: "acme"}
    assert [(x.key, x.value, x.effect) for x in pod.tolerations] == [(tenancy.TENANT_LABEL, "acme", "NoSchedule")]


def test_pin_outside_tenant_nodes_is_400():
    with pytest.raises(AdmissionError, match="not one of tenant acme's nodes") as e:
        tenancy.stamp(deployment(node="n9"), adm(tenant(nodes=["n1"])))
    assert e.value.status == 400


def test_pin_inside_tenant_nodes_keeps_pin():
    dep = deployment(node="n1")
    tenancy.stamp(dep, adm(tenant(nodes=["n1"])))
    assert dep.spec.template.spec.node_selector == {"kubernetes.io/hostname": "n1", tenancy.TENANT_LABEL: "acme"}


def test_other_tenants_dedicated_node_is_403_unless_admin():
    tol = [client.V1Toleration(key=tenancy.TENANT_LABEL, operator="Equal", value="globex", effect="NoSchedule")]
    with pytest.raises(AdmissionError) as e:
        tenancy.stamp(deployment(node="g1", tolerations=tol), adm(A))
    assert e.value.status == 403 and "globex" in e.value.message
    with pytest.raises(AdmissionError):
        tenancy.stamp(deployment(node="g1", tolerations=list(tol)), adm(None))
    tenancy.stamp(deployment(node="g1", tolerations=list(tol)), adm(None, admin=True))


# ── quota arithmetic ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("dep", "pvcs", "want"),
    [
        (deployment(backend=KFD), {}, {"vcpu": 1, "memory": GI, "disk": tenancy.KFD_ROOT_DISK}),
        (
            deployment(backend=KFD, pvcs=("sb-docker",)),
            {"sb-docker": "20Gi"},
            {"disk": tenancy.KFD_ROOT_DISK + 20 * GI},
        ),
        (
            deployment(backend="kata-qemu-longhorn", cpu="500m", pvcs=("sb-root-lh", "sb-docker")),
            {"sb-root-lh": "10Gi", "sb-docker": "20Gi"},
            {"vcpu": Decimal("0.5"), "disk": 30 * GI},
        ),
        (deployment(backend="k7d", memory="2Gi"), {}, {"memory": 2 * GI, "disk": 0}),
        (deployment(backend="k7d-fc", ephemeral="5Gi"), {}, {"disk": 5 * GI}),
        (deployment(backend="kata-qemu-longhorn", ephemeral_claim="3Gi"), {}, {"disk": 3 * GI}),
    ],
)
def test_deployment_usage_per_backend(dep, pvcs, want):
    got = tenancy.deployment_usage(dep, {k: tenancy.quantity(v) for k, v in pvcs.items()})
    assert got["sandboxes"] == 1
    for k, v in want.items():
        assert got[k] == v, k


def _rejections(dim: str) -> float:
    return metrics.API.get_sample_value("k7_quota_rejections_total", {"tenant": "acme", "dimension": dim}) or 0


@pytest.mark.parametrize(
    ("dim", "limit", "usage", "requested"),
    [
        ("sandboxes", 10, Decimal(9), Decimal(1)),
        ("vcpu", "8", Decimal(7), Decimal("1")),
        ("memory", "16Gi", 15 * GI, GI),
        ("disk", "100Gi", 90 * GI, 10 * GI),
        ("snapshotStorage", "50Gi", 40 * GI, 10 * GI),
    ],
)
def test_quota_boundary_per_dimension(dim, limit, usage, requested):
    t = tenant(quotas={dim: limit})
    before = _rejections(dim)
    tenancy.check_quota(t, {dim: usage}, {dim: requested})  # exactly at the limit
    assert _rejections(dim) == before, "an admitted create is not a rejection"
    with pytest.raises(AdmissionError) as e:
        tenancy.check_quota(t, {dim: usage + (Decimal("0.001") if dim == "vcpu" else 1)}, {dim: requested})
    assert e.value.status == 403 and e.value.code == "QuotaExceeded"
    assert _rejections(dim) == before + 1
    d = e.value.details
    assert d["tenant"] == "acme" and d["dimension"] == dim and d["limit"] == tenancy.fmt(dim, tenancy.quantity(limit))
    assert d["requested"] == tenancy.fmt(dim, requested)
    assert all(str(v) in e.value.message for v in (d["limit"], d["usage"], d["requested"]))


def test_quota_absent_dimension_is_unlimited_and_zero_quota_refuses():
    tenancy.check_quota(tenant(quotas={"vcpu": "1"}), {"memory": 10**15}, {"memory": Decimal(10**15)})
    with pytest.raises(AdmissionError):
        tenancy.check_quota(tenant(quotas={"sandboxes": 0}), {"sandboxes": Decimal(0)}, {"sandboxes": Decimal(1)})


async def test_usage_counts_every_tenant_namespace_and_only_referenced_pvcs(cluster):
    t = tenant(namespaces=("a1", "a2"))
    cluster.pvcs = {"a1": {"x-root-lh": "10Gi", "racing-root-lh": "50Gi"}, "a2": {}}
    cluster.deps = {
        "a1": [deployment("x", backend="kata-qemu-longhorn", cpu="2", pvcs=("x-root-lh",))],
        "a2": [deployment("y", backend=KFD, memory="3Gi"), deployment("z", backend="k7d", cpu="500m")],
        "other": [deployment("foreign")],
    }
    u = await tenancy.tenant_usage(cluster, t)
    assert u == {
        "sandboxes": 3,
        "vcpu": Decimal("3.5"),
        "memory": 5 * GI,
        "disk": 10 * GI + tenancy.KFD_ROOT_DISK,
    }


@pytest.mark.parametrize(
    ("dim", "limit", "dep", "existing"),
    [
        ("sandboxes", 2, deployment(), 2),
        ("vcpu", "3", deployment(cpu="2"), 2),
        ("memory", "2Gi", deployment(memory="1536Mi"), 1),
        ("disk", "25G", deployment(), 2),
    ],
)
async def test_admission_rejects_at_boundary_and_never_creates(cluster, dim, limit, dep, existing):
    cluster.deps["acme-dev"] = [deployment(f"old-{i}") for i in range(existing)]
    create = AsyncMock()
    with pytest.raises(AdmissionError) as e:
        await tenancy.admit_deployment(cluster, adm(tenant(quotas={dim: limit})), "acme-dev", dep, create)
    assert e.value.code == "QuotaExceeded" and e.value.details["dimension"] == dim
    create.assert_not_awaited()
    assert cluster.leases.store == {}, "lease released after a rejection"


async def test_admission_counts_own_pvcs_once(cluster):
    cluster.pvcs["acme-dev"] = {"sb-root-lh": "10Gi"}
    t = tenant(quotas={"disk": "10Gi"})
    create = AsyncMock()
    await tenancy.admit_deployment(
        cluster, adm(t), "acme-dev", deployment(backend="kata-qemu-longhorn", pvcs=("sb-root-lh",)), create
    )
    create.assert_awaited_once()


# ── concurrent creates racing the quota (two "replicas" = distinct holders) ──


RACES = [
    ("sandboxes", 10, lambda i: deployment(f"sb-{i}"), 10),
    ("vcpu", "4", lambda i: deployment(f"sb-{i}", cpu="1"), 4),
    ("vcpu", "2500m", lambda i: deployment(f"sb-{i}", cpu="500m"), 5),
    ("memory", "6Gi", lambda i: deployment(f"sb-{i}", memory="1Gi"), 6),
    ("disk", "50G", lambda i: deployment(f"sb-{i}", backend=KFD), 5),
    ("disk", "30Gi", lambda i: deployment(f"sb-{i}", backend="kata-qemu-longhorn", pvcs=(f"sb-{i}-root-lh",)), 3),
]


@pytest.mark.parametrize(("dim", "limit", "make", "want"), RACES)
async def test_concurrent_creates_never_overshoot(cluster, dim, limit, make, want):
    t = tenant(namespaces=("a1", "a2"), quotas={dim: limit})
    cluster.pvcs = {
        "a1": {f"sb-{i}-root-lh": "10Gi" for i in range(20)},
        "a2": {f"sb-{i}-root-lh": "10Gi" for i in range(20)},
    }
    state: dict = {}

    async def attempt(i: int):
        ns = ("a1", "a2")[i % 2]
        dep = make(i)
        return await tenancy.admit_deployment(
            cluster, adm(t, key=f"replica-{i % 2}"), ns, dep, creator(cluster, ns, dep, state)
        )

    results = await asyncio.gather(*(attempt(i) for i in range(20)), return_exceptions=True)
    created = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(created) == want
    assert all(isinstance(e, AdmissionError) and e.code == "QuotaExceeded" for e in errors), errors
    assert sum(len(v) for v in cluster.deps.values()) == want
    assert state["max"] == 1, "two creates were inside the lease at once"
    assert cluster.leases.store == {}


async def test_tenants_do_not_share_a_lease(cluster):
    cluster.leases.hold(tenancy.quota_lease_name("acme"), "someone-else")
    create = AsyncMock()
    await tenancy.admit_deployment(
        cluster, adm(tenant("globex", ("globex",), quotas={"sandboxes": 1})), "globex", deployment(), create
    )
    create.assert_awaited_once()


async def test_concurrent_snapshots_never_overshoot(cluster):
    t = tenant(quotas={"snapshotStorage": "25Gi"})
    cluster.pvcs["acme-dev"] = {"sb-root-lh": "10Gi"}
    inside = {"n": 0, "max": 0}

    async def attempt(i: int):
        async def create():
            inside["n"] += 1
            inside["max"] = max(inside["max"], inside["n"])
            await asyncio.sleep(0.001)
            cluster.snaps.setdefault("acme-dev", []).append(
                {"metadata": {"name": f"s{i}"}, "spec": {"source": {"persistentVolumeClaimName": "sb-root-lh"}}}
            )
            inside["n"] -= 1

        return await tenancy.admit_snapshot(cluster, adm(t, key=f"replica-{i % 2}"), "acme-dev", "sb-root-lh", create)

    results = await asyncio.gather(*(attempt(i) for i in range(10)), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(cluster.snaps["acme-dev"]) == 2 and len(errors) == 8
    assert all(isinstance(e, AdmissionError) and e.details["dimension"] == "snapshotStorage" for e in errors)
    assert inside["max"] == 1


async def test_snapshot_usage_prefers_restore_size(cluster):
    cluster.pvcs["acme-dev"] = {"p": "10Gi"}
    cluster.snaps["acme-dev"] = [
        {
            "metadata": {"name": "cut"},
            "status": {"restoreSize": "4Gi"},
            "spec": {"source": {"persistentVolumeClaimName": "p"}},
        },
        {"metadata": {"name": "pending"}, "spec": {"source": {"persistentVolumeClaimName": "p"}}},
    ]
    assert await tenancy.snapshot_usage(cluster, tenant()) == 14 * GI
    cluster.snaps["acme-dev"].append(
        {"metadata": {"name": "orphan"}, "spec": {"source": {"persistentVolumeClaimName": "gone"}}}
    )
    with pytest.raises(AdmissionError, match="orphan"):
        await tenancy.snapshot_usage(cluster, tenant())


async def test_snapshot_without_quota_skips_the_lease(cluster):
    create = AsyncMock()
    await tenancy.admit_snapshot(cluster, adm(tenant()), "acme-dev", "missing", create)
    create.assert_awaited_once()
    with pytest.raises(AdmissionError) as e:
        await tenancy.admit_snapshot(
            cluster, adm(tenant(quotas={"snapshotStorage": "1Gi"})), "acme-dev", "missing", create
        )
    assert e.value.status == 404


# ── the quota Lease ─────────────────────────────────────────────────


async def test_lease_acquire_and_release(cluster):
    name = tenancy.quota_lease_name("acme")
    async with tenancy.QuotaLease(cluster, name) as lease:
        held = cluster.leases.store[(SYS_NS, name)]
        assert held.spec.holder_identity == lease.holder and held.spec.lease_duration_seconds == tenancy.LEASE_TTL
        lease.assert_held()
    assert cluster.leases.store == {}


async def test_lease_timeout_is_503_and_nothing_proceeds(cluster, monkeypatch):
    name = tenancy.quota_lease_name("acme")
    cluster.leases.hold(name, "other-replica")
    with pytest.raises(AdmissionError) as e:
        async with tenancy.QuotaLease(cluster, name, timeout=0.2):
            pytest.fail("entered without the lease")
    assert e.value.status == 503 and e.value.code == "QuotaLockTimeout"
    monkeypatch.setattr(tenancy, "QuotaLease", functools.partial(tenancy.QuotaLease, timeout=0.2))
    create = AsyncMock()
    with pytest.raises(AdmissionError, match="could not acquire quota lease"):
        await tenancy.admit_deployment(cluster, adm(tenant(quotas={"sandboxes": 5})), "acme-dev", deployment(), create)
    create.assert_not_awaited()
    assert cluster.leases.store[(SYS_NS, name)].spec.holder_identity == "other-replica"


async def test_expired_lease_is_taken_over(cluster):
    name = tenancy.quota_lease_name("acme")
    cluster.leases.hold(name, "crashed-replica", ttl=10, age=11)
    async with tenancy.QuotaLease(cluster, name, timeout=0.5) as lease:
        assert cluster.leases.store[(SYS_NS, name)].spec.holder_identity == lease.holder


async def test_lost_lease_is_503_before_create(cluster):
    name = tenancy.quota_lease_name("acme")
    async with tenancy.QuotaLease(cluster, name, ttl=1) as lease:
        cluster.leases.fail_replace = True
        await asyncio.sleep(0.5)
        with pytest.raises(AdmissionError) as e:
            lease.assert_held()
    assert e.value.code == "QuotaLockLost" and e.value.status == 503


async def test_release_never_deletes_another_holders_lease(cluster):
    name = tenancy.quota_lease_name("acme")
    async with tenancy.QuotaLease(cluster, name):
        cluster.leases.hold(name, "thief")
    assert cluster.leases.store[(SYS_NS, name)].spec.holder_identity == "thief"


async def test_lease_latency_is_logged(cluster, caplog):
    with caplog.at_level("INFO", logger="k7.tenancy"):
        async with tenancy.QuotaLease(cluster, "k7-quota-acme"):
            pass
    assert re.search(r"Quota lease k7-quota-acme: acquired in [0-9.]+ ms, held [0-9.]+ ms", caplog.text)


async def test_status_leader_election(cluster):
    assert await tenancy.try_lead(cluster, tenancy.STATUS_LEASE, "pod-a", 90)
    assert not await tenancy.try_lead(cluster, tenancy.STATUS_LEASE, "pod-b", 90)
    assert await tenancy.try_lead(cluster, tenancy.STATUS_LEASE, "pod-a", 90)


# ── status and ResourceQuota backstop ───────────────────────────────


def _status_core(cluster: FakeCluster):
    v1 = MagicMock(
        replace_namespaced_resource_quota=AsyncMock(side_effect=ApiException(status=404)),
        create_namespaced_resource_quota=AsyncMock(),
        delete_namespaced_resource_quota=AsyncMock(side_effect=ApiException(status=404)),
        list_namespaced_persistent_volume_claim=cluster.list_namespaced_persistent_volume_claim,
    )
    custom = MagicMock(
        patch_cluster_custom_object_status=AsyncMock(),
        list_namespaced_custom_object=cluster.list_namespaced_custom_object,
    )
    return (
        SimpleNamespace(
            _get_apps_v1_client=AsyncMock(return_value=cluster),
            _get_core_v1_client=AsyncMock(return_value=v1),
            _get_custom_objects_client=AsyncMock(return_value=custom),
        ),
        v1,
        custom,
    )


async def test_reconcile_writes_status_and_single_namespace_resource_quota(cluster):
    cluster.deps["acme-dev"] = [deployment("a"), deployment("b", cpu="500m")]
    core, v1, custom = _status_core(cluster)
    await tenancy.reconcile_tenant(core, tenant(quotas={"sandboxes": 5, "disk": "100Gi", "vcpu": "8"}))
    status = custom.patch_cluster_custom_object_status.await_args.args[-1]["status"]
    assert custom.patch_cluster_custom_object_status.await_args.kwargs == {"_content_type": tenancy.MERGE_PATCH}
    assert status["usage"] == {
        "sandboxes": "2",
        "vcpu": "1500m",
        "memory": "2Gi",
        "disk": "20G",
        "snapshotStorage": "0",
    }
    assert status["observedGeneration"] == 3
    body = v1.create_namespaced_resource_quota.await_args.args[1]
    assert body["spec"]["hard"] == {"count/deployments.apps": "5", "requests.storage": "100Gi"}


async def test_reconcile_multi_namespace_tenant_has_no_resource_quota(cluster):
    core, v1, _ = _status_core(cluster)
    await tenancy.reconcile_tenant(core, tenant(namespaces=("a1", "a2"), quotas={"sandboxes": 5}))
    v1.create_namespaced_resource_quota.assert_not_awaited()
    assert v1.delete_namespaced_resource_quota.await_count == 2


# ── lifecycle: create / update / delete / dedicate / adopt ──────────


def lifecycle_core(tenants: list[dict], namespaces: dict[str, dict] | None = None):
    namespaces = namespaces if namespaces is not None else {}

    async def read_namespace(ns):
        if ns not in namespaces:
            raise ApiException(status=404)
        return SimpleNamespace(metadata=SimpleNamespace(labels=namespaces[ns]))

    async def get_cr(_g, _v, _p, name):
        hit = next((t for t in tenants if tenancy.name_of(t) == name), None)
        if hit is None:
            raise ApiException(status=404)
        return hit

    v1 = MagicMock(
        read_namespace=read_namespace,
        create_namespace=AsyncMock(),
        patch_namespace=AsyncMock(),
        delete_namespace=AsyncMock(),
        delete_namespaced_resource_quota=AsyncMock(side_effect=ApiException(status=404)),
    )
    custom = MagicMock(
        list_cluster_custom_object=AsyncMock(return_value={"items": tenants}),
        get_cluster_custom_object=get_cr,
        create_cluster_custom_object=AsyncMock(side_effect=lambda _g, _v, _p, body: body),
        replace_cluster_custom_object=AsyncMock(side_effect=lambda _g, _v, _p, _n, body: body),
        patch_cluster_custom_object=AsyncMock(),
        delete_cluster_custom_object=AsyncMock(),
    )
    apps = MagicMock(list_namespaced_deployment=AsyncMock(return_value=SimpleNamespace(items=[])))
    core = MagicMock(
        _get_core_v1_client=AsyncMock(return_value=v1),
        _get_custom_objects_client=AsyncMock(return_value=custom),
        _get_apps_v1_client=AsyncMock(return_value=apps),
        _get_api_client=AsyncMock(),
        dedicate_node=AsyncMock(return_value=OperationResult(success=True)),
        undedicate_node=AsyncMock(return_value=OperationResult(success=True)),
        list_snapshots=AsyncMock(return_value=[]),
        delete_snapshot=AsyncMock(return_value=OperationResult(success=True)),
        delete_sandbox=AsyncMock(return_value=OperationResult(success=True)),
    )
    return core, v1, custom, apps


async def test_create_refuses_namespace_of_another_tenant():
    core, *_ = lifecycle_core([B])
    with pytest.raises(ValueError, match="already belong to tenant globex"):
        await tenancy.create_tenant(core, "acme", {"namespaces": ["acme-dev", "globex"]})


async def test_create_labels_creates_missing_namespaces_and_dedicates():
    core, v1, custom, _ = lifecycle_core([], {"acme-dev": {}})
    await tenancy.create_tenant(core, "acme", {"namespaces": ["acme-dev", "acme-new"], "nodes": ["n1"]})
    v1.patch_namespace.assert_awaited_once_with("acme-dev", {"metadata": {"labels": {tenancy.TENANT_LABEL: "acme"}}})
    v1.create_namespace.assert_awaited_once()
    core.dedicate_node.assert_awaited_once_with("n1", "acme")


async def test_create_rolls_back_on_failure():
    core, v1, custom, _ = lifecycle_core([], {"acme-dev": {}})
    core.dedicate_node = AsyncMock(return_value=OperationResult(success=False, error="no such node"))
    with pytest.raises(RuntimeError, match="no such node"):
        await tenancy.create_tenant(core, "acme", {"namespaces": ["acme-dev", "acme-new"], "nodes": ["n1"]})
    v1.delete_namespace.assert_awaited_once_with("acme-new")
    assert v1.patch_namespace.await_args_list[-1].args == (
        "acme-dev",
        {"metadata": {"labels": {tenancy.TENANT_LABEL: None}}},
    )
    custom.delete_cluster_custom_object.assert_awaited_once()


async def test_create_refuses_namespace_labelled_for_existing_tenant():
    core, *_ = lifecycle_core([B], {"stolen": {tenancy.TENANT_LABEL: "globex"}})
    with pytest.raises(ValueError, match="labelled for tenant globex"):
        await tenancy.create_tenant(core, "acme", {"namespaces": ["stolen"]})


async def test_update_relabels_and_drops_resource_quota_of_removed_namespace():
    t = tenant("acme", ("acme-dev", "acme-old"), quotas={"sandboxes": 3})
    core, v1, custom, _ = lifecycle_core([t], {"acme-dev": {}, "acme-old": {}, "acme-ci": {}})
    out = await tenancy.update_tenant(core, "acme", {"namespaces": ["acme-dev", "acme-ci"], "quotas": None})
    assert out["spec"] == {"namespaces": ["acme-dev", "acme-ci"]}
    patched = {
        c.args[0]: c.args[1]["metadata"]["labels"][tenancy.TENANT_LABEL] for c in v1.patch_namespace.await_args_list
    }
    assert patched == {"acme-ci": "acme", "acme-old": None}
    v1.delete_namespaced_resource_quota.assert_awaited_once_with(tenancy.RESOURCE_QUOTA, "acme-old")


async def test_update_refuses_taking_another_tenants_namespace():
    core, *_ = lifecycle_core([tenant("acme"), B])
    with pytest.raises(ValueError, match="globex"):
        await tenancy.update_tenant(core, "acme", {"namespaces": ["acme-dev", "globex"]})


async def test_delete_refuses_with_objects_unless_cascade():
    t = tenant("acme", ("acme-dev",), nodes=["n1"])
    core, v1, custom, apps = lifecycle_core([t], {"acme-dev": {tenancy.TENANT_LABEL: "acme"}})
    apps.list_namespaced_deployment = AsyncMock(return_value=SimpleNamespace(items=[deployment("sb1")]))
    core.list_snapshots = AsyncMock(return_value=[SimpleNamespace(name="snap1")])
    with pytest.raises(ValueError, match="sandbox acme-dev/sb1, snapshot acme-dev/snap1"):
        await tenancy.delete_tenant(core, "acme", cascade=False)
    core.delete_sandbox.assert_not_awaited()
    order: list[str] = []

    async def delete_snapshot(*_a, **_k):
        order.append("snapshot")
        return OperationResult(success=True)

    async def delete_sandbox(*_a, **_k):
        order.append("sandbox")
        return OperationResult(success=True)

    core.delete_snapshot.side_effect = delete_snapshot
    core.delete_sandbox.side_effect = delete_sandbox
    deleted = await tenancy.delete_tenant(core, "acme", cascade=True)
    assert order == ["snapshot", "sandbox"]
    assert deleted[:2] == ["snapshot acme-dev/snap1", "sandbox acme-dev/sb1"] and deleted[-1] == "tenant acme"
    core.undedicate_node.assert_awaited_once_with("n1")
    custom.delete_cluster_custom_object.assert_awaited_once()


async def test_cascade_stops_loudly_on_a_failed_delete():
    core, *_rest = lifecycle_core([tenant()], {"acme-dev": {}})
    _rest[2].list_namespaced_deployment = AsyncMock(return_value=SimpleNamespace(items=[deployment("sb1")]))
    core.delete_sandbox = AsyncMock(return_value=OperationResult(success=False, error="stuck"))
    with pytest.raises(RuntimeError, match="cascade stopped.*sb1.*stuck"):
        await tenancy.delete_tenant(core, "acme", cascade=True)
    _rest[1].delete_cluster_custom_object.assert_not_awaited()


async def test_dedicate_needs_existing_tenant_and_moves_node_between_tenants():
    core, _, custom, _ = lifecycle_core([tenant("acme", nodes=[]), tenant("globex", ("g",), nodes=["n1"])])
    with pytest.raises(LookupError, match="k7 tenant create"):
        await tenancy.dedicate(core, "n1", "initech")
    core.dedicate_node.assert_not_awaited()
    await tenancy.dedicate(core, "n1", "acme")
    patches = {c.args[3]: c.args[4]["spec"]["nodes"] for c in custom.patch_cluster_custom_object.await_args_list}
    assert patches == {"acme": ["n1"], "globex": []}
    assert all(
        c.kwargs == {"_content_type": tenancy.MERGE_PATCH} for c in custom.patch_cluster_custom_object.await_args_list
    )


def test_every_custom_object_patch_is_a_merge_patch():
    """A dict sent without a content type goes out as JSON Patch, which the API server rejects (400)."""
    src = inspect.getsource(tenancy)
    calls = re.findall(r"\.patch_cluster_custom_object(?:_status)?\((.*?)\)\n", src, re.S)
    assert len(calls) == 3
    assert all("_content_type=MERGE_PATCH" in c for c in calls), calls


def test_adopt_plan_merges_overlapping_key_scopes():
    keys = {
        "k1": {"namespaces": ["a", "b"]},
        "k2": {"namespaces": ["b", "c"]},
        "k3": {"namespaces": ["z"]},
        "k4": {},
        "k5": {"role": "admin"},
        "k6": {"role": "user", "tenant": "acme", "namespaces": ["q"]},
        "k7": {"role": "admin", "unscoped_legacy": True},
        "k8": {"role": "operator"},
    }
    plan, unscoped = tenancy.adopt_plan(keys, [tenant("z", ("other",))])
    assert plan == [
        {"tenant": "a", "namespaces": ["a", "b", "c"], "keys": ["k1", "k2"]},
        {"tenant": "z-2", "namespaces": ["z"], "keys": ["k3"]},
    ]
    assert unscoped == ["k4", "k7"]
    with pytest.raises(ValueError, match="overlap existing tenant"):
        tenancy.adopt_plan({"k1": {"namespaces": ["acme-dev"]}}, [A])


# ── K7Core choke points ─────────────────────────────────────────────

CORE = (Path(__file__).resolve().parents[2] / "src/k7/core/core.py").read_text()


def _function_of(line_no: int) -> str:
    for line in reversed(CORE.splitlines()[:line_no]):
        m = re.match(r"\s*(?:async )?def (\w+)", line)
        if m:
            return m.group(1)
    raise AssertionError


def test_every_deployment_and_snapshot_create_goes_through_admission():
    lines = CORE.splitlines()
    deps = [i for i, line in enumerate(lines, 1) if "create_namespaced_deployment(" in line]
    assert [_function_of(i) for i in deps] == ["create"]
    assert "async def _create_sandbox_deployment" in "\n".join(lines[deps[0] - 8 : deps[0]])
    snaps = [
        i
        for i, line in enumerate(lines, 1)
        if "create_namespaced_custom_object(" in line and 'plural="volumesnapshots"' in "\n".join(lines[i : i + 5])
    ]
    assert len(snaps) == 1 and "async def _create_volume_snapshot" in "\n".join(lines[snaps[0] - 60 : snaps[0]])


@pytest.fixture()
def k7core():
    from unittest.mock import patch

    from k7.core.core import K7Core

    with patch.object(K7Core, "_load_k3s_config", new_callable=AsyncMock):
        c = K7Core()
    c._config_loaded = True
    return c


async def test_core_deployment_create_uses_admission(k7core, monkeypatch):
    apps = MagicMock(create_namespaced_deployment=AsyncMock(return_value="made"))
    monkeypatch.setattr(k7core, "_get_apps_v1_client", AsyncMock(return_value=apps))
    body = client.V1Deployment(
        metadata=client.V1ObjectMeta(name="sb"),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(), spec=client.V1PodSpec(containers=[client.V1Container(name="sandbox")])
            ),
        ),
    )
    assert await k7core._create_sandbox_deployment("ns", body) == "made"

    async def passthrough(core, a, ns, body, create):
        return await create()

    admit = AsyncMock(side_effect=passthrough)
    monkeypatch.setattr(tenancy, "admit_deployment", admit)
    k7core.admission = adm(A)
    assert await k7core._create_sandbox_deployment("acme-dev", body) == "made"
    assert admit.await_args.args[1:4] == (k7core.admission, "acme-dev", body)


@pytest.mark.parametrize(("kind", "admitted"), [("named", True), ("pause", True), ("fork", False)])
async def test_core_snapshot_create_uses_admission_except_fork_temp(k7core, monkeypatch, kind, admitted):
    custom = MagicMock(create_namespaced_custom_object=AsyncMock())
    monkeypatch.setattr(k7core, "_get_custom_objects_client", AsyncMock(return_value=custom))

    async def passthrough(core, a, ns, pvc, create):
        return await create()

    admit = AsyncMock(side_effect=passthrough)
    monkeypatch.setattr(tenancy, "admit_snapshot", admit)
    k7core.admission = adm(A, key="kid")
    r = await k7core._create_volume_snapshot("p", "s", namespace="acme-dev", kind=kind)
    assert r.success and admit.await_count == int(admitted)
    body = custom.create_namespaced_custom_object.await_args.kwargs["body"]
    assert body["metadata"]["labels"] == {tenancy.KEY_ID_LABEL: "kid", tenancy.TENANT_LABEL: "acme"}


async def test_core_snapshot_quota_refusal_propagates(k7core, monkeypatch):
    monkeypatch.setattr(k7core, "_get_custom_objects_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(tenancy, "admit_snapshot", AsyncMock(side_effect=AdmissionError(403, "QuotaExceeded", "full")))
    k7core.admission = adm(A)
    with pytest.raises(AdmissionError):
        await k7core._create_volume_snapshot("p", "s", namespace="acme-dev")


async def test_pause_snapshot_refused_restores_replicas(k7core, monkeypatch):
    apps = MagicMock(
        read_namespaced_deployment_scale=AsyncMock(return_value=SimpleNamespace(spec=SimpleNamespace(replicas=1))),
        patch_namespaced_deployment_scale=AsyncMock(),
    )
    monkeypatch.setattr(k7core, "_get_apps_v1_client", AsyncMock(return_value=apps))
    monkeypatch.setattr(k7core, "_existing_backend", AsyncMock(return_value="kata-qemu-longhorn"))
    monkeypatch.setattr(
        k7core, "_create_volume_snapshot", AsyncMock(side_effect=AdmissionError(403, "QuotaExceeded", "full"))
    )
    with pytest.raises(AdmissionError):
        await k7core.pause_sandbox("sb", "acme-dev", snapshot_name="s1")
    replicas = [c.kwargs["body"]["spec"]["replicas"] for c in apps.patch_namespaced_deployment_scale.await_args_list]
    assert replicas == [0, 1]


# ── create_sandbox under admission (labels, limits, rollback) ───────


async def _create(k7core, monkeypatch, cfg_obj, admission=None, leases=None):
    from unittest.mock import patch

    apps = AsyncMock()
    apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[])
    v1 = AsyncMock()
    v1.read_namespaced_persistent_volume_claim.side_effect = ApiException(status=404)
    v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    v1.list_namespaced_persistent_volume_claim.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(name=f"{cfg_obj.name}-root-lh"),
                spec=SimpleNamespace(resources=SimpleNamespace(requests={"storage": "10Gi"})),
            )
        ]
    )
    net = AsyncMock()
    k7core._apps_v1_client, k7core._core_v1_client, k7core._networking_v1_client = apps, v1, net
    k7core.admission = admission
    if leases is not None:
        monkeypatch.setattr(tenancy.client, "CoordinationV1Api", lambda _api: leases)
    ok = OperationResult(success=True, data={"created": True})
    with (
        patch.object(k7core, "_check_scheduling", new=AsyncMock(return_value=ok)),
        patch.object(k7core, "_load_persist_bind_script", return_value='#!/bin/sh\nexec "$@"'),
        patch.object(k7core, "_get_image_entrypoint_cmd", new=AsyncMock(return_value=([], ["/bin/sh"]))),
        patch.object(k7core, "_apply_sandbox_network_policies", new=AsyncMock(return_value=ok)),
        patch.object(k7core, "_wait_for_ready", new=AsyncMock(return_value=ok), create=True),
    ):
        try:
            return await k7core.create_sandbox(cfg_obj), apps, v1
        except AdmissionError as e:
            return e, apps, v1


def _sb(**kw) -> SandboxConfig:
    return SandboxConfig(name="sb", image="alpine:3.20", namespace="acme-dev", **kw)


async def test_create_stamps_identity_labels_on_deployment_pods_and_pvc(k7core, monkeypatch):
    t = tenant(defaultSize={"cpu": "1", "memory": "1Gi"})
    result, apps, v1 = await _create(k7core, monkeypatch, _sb(backend="kql"), adm(t, key="kid12345"))
    dep = apps.create_namespaced_deployment.call_args.kwargs["body"]
    want = {tenancy.TENANT_LABEL: "acme", tenancy.KEY_ID_LABEL: "kid12345"}
    assert want.items() <= dep.metadata.labels.items() and want.items() <= dep.spec.template.metadata.labels.items()
    pvc = v1.create_namespaced_persistent_volume_claim.call_args.kwargs["body"]
    assert want.items() <= (pvc.metadata.labels or {}).items()
    sandbox = dep.spec.template.spec.containers[0]
    assert sandbox.resources.limits["cpu"] == "1" and sandbox.resources.limits["memory"] == "1Gi"


async def test_create_over_quota_is_refused_and_rolls_back_pvc(k7core, monkeypatch):
    t = tenant(quotas={"sandboxes": 0}, defaultSize={"cpu": "1", "memory": "1Gi"})
    result, apps, v1 = await _create(k7core, monkeypatch, _sb(backend="kql"), adm(t), leases=FakeLeases())
    assert isinstance(result, AdmissionError) and result.code == "QuotaExceeded"
    apps.create_namespaced_deployment.assert_not_called()
    assert v1.delete_namespaced_persistent_volume_claim.call_args.kwargs["name"] == "sb-root-lh"


@pytest.mark.parametrize("backend", ["kfd", "kql"])
async def test_create_network_limits_become_kata_annotations(k7core, monkeypatch, backend):
    result, apps, _ = await _create(k7core, monkeypatch, _sb(backend=backend, network_limits={"egressBps": "100M"}))
    anns = apps.create_namespaced_deployment.call_args.kwargs["body"].spec.template.metadata.annotations
    full = {"kfd": KFD, "kql": "kata-qemu-longhorn"}[backend]
    assert anns[tenancy.NETWORK_ANNOTATIONS["egressBps"]] == str(tenancy.kata_rate(full, 100_000_000))


@pytest.mark.parametrize("backend", ["k7d", "k7d-fc"], ids=["native-vmm", "native-fc"])
async def test_create_network_limits_on_k7d_is_refused(k7core, monkeypatch, backend):
    result, apps, _ = await _create(k7core, monkeypatch, _sb(backend=backend, network_limits={"egressBps": "100M"}))
    assert not result.success and "cannot enforce network" in result.error
    apps.create_namespaced_deployment.assert_not_called()


async def test_create_production_without_size_is_400_before_any_object(k7core, monkeypatch):
    result, apps, v1 = await _create(k7core, monkeypatch, _sb(backend="kql"), adm(tenant(), mode="production"))
    assert isinstance(result, AdmissionError) and result.status == 400
    v1.create_namespaced_persistent_volume_claim.assert_not_called()
    apps.create_namespaced_deployment.assert_not_called()
