"""Storage sampler: provisioned disk and snapshot GiB·s, aligned windows, leader gaps."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from kubernetes_asyncio import client as k
from kubernetes_asyncio.client.exceptions import ApiException

from k7 import metrics
from k7.core.core import K7D_ANN_PAUSED
from k7.core.tenancy import BACKEND_ANN, KFD_ROOT_DISK, TENANT_LABEL
from k7.metering import SANDBOX_UID_ANN
from k7.metering.events import DISK, GAP, GIB, SNAPSHOT, validate
from k7.metering.storage import StorageSampler, disk_bytes, sandbox_state
from tests.unit.conftest import CLUSTER_ID

T0 = 1_767_225_600.0
NS = "acme-dev"


def dep(name, backend, *, replicas=1, pvc=None, eph=None, limit=None, created=T0 - 1000, paused=False):
    anns = {BACKEND_ANN: backend, SANDBOX_UID_ANN: f"uid-{name}", **({K7D_ANN_PAUSED: "true"} if paused else {})}
    vols = []
    if pvc:
        vols.append(k.V1Volume(name="d", persistent_volume_claim=k.V1PersistentVolumeClaimVolumeSource(claim_name=pvc)))
    if eph:
        spec = k.V1PersistentVolumeClaimSpec(resources=k.V1VolumeResourceRequirements(requests={"storage": eph}))
        tmpl = k.V1PersistentVolumeClaimTemplate(spec=spec)
        vols.append(k.V1Volume(name="e", ephemeral=k.V1EphemeralVolumeSource(volume_claim_template=tmpl)))
    res = k.V1ResourceRequirements(limits={"ephemeral-storage": limit} if limit else {})
    return k.V1Deployment(
        metadata=k.V1ObjectMeta(
            name=name,
            namespace=NS,
            uid=f"dep-{name}",
            labels={TENANT_LABEL: "acme"},
            annotations=anns,
            creation_timestamp=datetime.fromtimestamp(created, timezone.utc),
        ),
        spec=k.V1DeploymentSpec(
            replicas=replicas,
            selector=k.V1LabelSelector(),
            template=k.V1PodTemplateSpec(
                spec=k.V1PodSpec(containers=[k.V1Container(name="sandbox", resources=res)], volumes=vols)
            ),
        ),
    )


def snap(name, size="5Gi", created="2025-12-31T00:00:00Z", source=None):
    return {
        "metadata": {
            "namespace": NS,
            "name": name,
            "uid": f"snap-{name}",
            "creationTimestamp": created,
            "labels": {TENANT_LABEL: "acme"},
        },
        "spec": {"source": {"persistentVolumeClaimName": source}},
        "status": {"restoreSize": size} if size else {},
    }


def sampler() -> StorageSampler:
    return StorageSampler(CLUSTER_ID, 60, "node-a")


PVCS = {(NS, "data-kql"): Decimal(20 * GIB)}


def test_provisioned_disk_per_backend():
    assert disk_bytes(dep("q", "kata-qemu-longhorn", pvc="data-kql"), "kata-qemu-longhorn", PVCS) == 20 * GIB
    assert disk_bytes(dep("f", "kata-firecracker-devmapper", limit="2Gi"), "kata-firecracker-devmapper", {}) == (
        KFD_ROOT_DISK + 2 * GIB
    )
    stopped = dep("f", "kata-firecracker-devmapper", replicas=0, eph="1Gi")
    assert disk_bytes(stopped, "kata-firecracker-devmapper", {}) == GIB
    with pytest.raises(LookupError, match="missing PVC gone"):
        disk_bytes(dep("q", "kata-qemu-longhorn", pvc="gone"), "kata-qemu-longhorn", PVCS)


def test_sandbox_state():
    assert sandbox_state(dep("a", "k7d")) == "running"
    assert sandbox_state(dep("a", "k7d", paused=True)) == "paused"
    assert sandbox_state(dep("a", "kata-qemu-longhorn", replicas=0)) == "stopped"


def test_build_window_matches_provisioned_sizes():
    s = sampler()
    deps = [
        dep("q", "kata-qemu-longhorn", pvc="data-kql", replicas=0),
        dep("new", "kata-qemu-longhorn", pvc="data-kql", created=T0 + 30),
        dep("v", "k7d"),
    ]
    snaps = [snap("s1"), snap("s2", size=None, source="data-kql"), snap("future", created="2026-01-01T00:02:00Z")]
    events, disk, snap_g, errors = s.build(deps, snaps, PVCS, T0, T0 + 60)
    for e in events:
        validate(e)
    by = {(e["type"], e["subject"]): e["data"] for e in events}
    q = by[(DISK, f"acme/{NS}/q")]
    assert (q["state"], q["seconds"], q["bytes"], q["gib_seconds"]) == ("stopped", 60.0, 20 * GIB, 20 * 60.0)
    assert by[(DISK, f"acme/{NS}/new")]["seconds"] == 30.0
    assert by[(SNAPSHOT, f"acme/{NS}/s1")]["gib_seconds"] == 5 * 60.0
    assert by[(SNAPSHOT, f"acme/{NS}/s2")]["bytes"] == 20 * GIB
    assert (SNAPSHOT, f"acme/{NS}/future") not in by
    assert disk == {("acme", NS, "kata-qemu-longhorn"): 40 * GIB}
    assert snap_g == {("acme", NS, "volume", "local"): 25 * GIB + 5 * GIB}
    assert errors == [] and s.unavailable == ["k7d"]


def test_one_unsizable_object_is_an_error_not_a_lost_window():
    deps = [dep("ok", "kata-qemu-longhorn", pvc="data-kql"), dep("bad", "kata-qemu-longhorn", pvc="gone")]
    events, _, _, errors = sampler().build(deps, [snap("x", size=None, source="nope")], PVCS, T0, T0 + 60)
    assert [e["subject"] for e in events] == [f"acme/{NS}/ok"]
    assert len(errors) == 2 and "gone" in errors[0] and "cannot size VolumeSnapshot" in errors[1]


def test_any_leader_computes_the_same_ids():
    a = sampler().build([dep("q", "kata-qemu-longhorn", pvc="data-kql")], [snap("s1")], PVCS, T0, T0 + 60)[0]
    b = sampler().build([dep("q", "kata-qemu-longhorn", pvc="data-kql")], [snap("s1")], PVCS, T0, T0 + 60)[0]
    assert [e["id"] for e in a] == [e["id"] for e in b]


def _core(state=None, deps=(), snaps=()):
    v1 = AsyncMock()
    if state is None:
        v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    else:
        v1.read_namespaced_config_map.return_value = SimpleNamespace(data={"storageWindowEnd": str(state)})
    v1.patch_namespaced_config_map.side_effect = ApiException(status=404)
    v1.list_persistent_volume_claim_for_all_namespaces.return_value = SimpleNamespace(items=[])
    custom = AsyncMock()
    custom.list_cluster_custom_object.return_value = {"items": list(snaps)}
    core = AsyncMock()
    core._get_core_v1_client.return_value = v1
    core._get_custom_objects_client.return_value = custom
    core._get_kata_sandboxes.return_value = list(deps)
    return core, v1


async def test_tick_as_leader_posts_aligned_window_and_records_it():
    core, v1 = _core(deps=[dep("f", "kata-firecracker-devmapper")], snaps=[snap("s1")])
    s = sampler()
    with patch("k7.metering.storage.tenancy.try_lead", AsyncMock(return_value=True)):
        await s.tick(core, T0 + 61.5)
    (((node, method, path, body), _),) = [(c.args, c.kwargs) for c in core._agent_request.await_args_list]
    assert (node, method, path) == ("node-a", "POST", "/agent/v1/usage/events")
    assert {e["data"]["window_end"] for e in body["events"]} == {"2026-01-01T00:01:00.000Z"}
    v1.create_namespaced_config_map.assert_awaited_once()
    assert v1.create_namespaced_config_map.await_args.args[1]["data"] == {"storageWindowEnd": str(int(T0) + 60)}
    assert metrics.API.get_sample_value(
        "k7_usage_disk_bytes", {"tenant": "acme", "namespace": NS, "backend": "kata-firecracker-devmapper"}
    ) == float(KFD_ROOT_DISK)


async def test_tick_after_a_leader_change_emits_gaps_first():
    core, _ = _core(state=T0 - 180, snaps=[snap("s1")])
    with patch("k7.metering.storage.tenancy.try_lead", AsyncMock(return_value=True)):
        await sampler().tick(core, T0 + 61)
    events = core._agent_request.await_args.args[3]["events"]
    gaps = [e for e in events if e["type"] == GAP]
    assert [g["data"]["meter"] for g in gaps] == ["disk", "snapshot"] and events[:2] == gaps
    assert {(g["data"]["gap_start"], g["data"]["gap_end"]) for g in gaps} == {
        ("2025-12-31T23:57:00.000Z", "2026-01-01T00:00:00.000Z")
    }
    for e in events:
        validate(e)


async def test_tick_skips_a_window_already_emitted_and_followers_do_nothing():
    core, _ = _core(state=T0 + 60)
    with patch("k7.metering.storage.tenancy.try_lead", AsyncMock(return_value=True)):
        await sampler().tick(core, T0 + 70)
    core._agent_request.assert_not_awaited()
    with patch("k7.metering.storage.tenancy.try_lead", AsyncMock(return_value=False)):
        s = sampler()
        await s.tick(core, T0 + 130)
    assert not s.leader and core._get_kata_sandboxes.await_count == 0


async def test_tick_reports_unsizable_objects_after_posting_the_rest():
    core, _ = _core(deps=[dep("ok", "kata-firecracker-devmapper"), dep("bad", "kata-qemu-longhorn", pvc="gone")])
    lead = patch("k7.metering.storage.tenancy.try_lead", AsyncMock(return_value=True))
    with lead, pytest.raises(RuntimeError, match="missing PVC gone"):
        await sampler().tick(core, T0 + 61)
    assert len(core._agent_request.await_args.args[3]["events"]) == 1
