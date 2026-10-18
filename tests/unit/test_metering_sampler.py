"""Node sampler: windows, state attribution, gaps, egress."""

from collections import defaultdict

import pytest

from k7.core.tenancy import BACKEND_ANN, KEY_ID_LABEL, SANDBOX_LABEL, TENANT_LABEL
from k7.metering import MEMORY_ANN, SANDBOX_UID_ANN, VCPU_ANN, pauses
from k7.metering.events import EGRESS, GAP, GIB, SANDBOX, validate
from k7.metering.sampler import NodeSampler, Pod, parse_pods, parse_tx, split, state_at
from tests.unit.conftest import CLUSTER_ID

T0 = 1_767_225_600.0  # 2026-01-01T00:00:00Z
STEP = 15.0


def pod(cri="cri-1", uid="uid-1", backend="k7d", created=T0 - 100) -> Pod:
    return Pod(
        cri_id=cri,
        uid=uid,
        sandbox="sb",
        namespace="acme-dev",
        tenant="acme",
        api_key_id="key-1",
        backend=backend,
        vcpu="2",
        memory=str(4 * GIB),
        created=created,
    )


def sampler(tmp_path) -> NodeSampler:
    return NodeSampler(CLUSTER_ID, "node-a", tmp_path)


def run(s: NodeSampler, steps, *, start=T0, pods=None, tx=None, trans=None, mono_skew=0.0) -> list[dict]:
    """``steps`` samples STEP apart; the first one only primes the sampler (metering-start gap)."""
    pods = pods if pods is not None else {"cri-1": pod()}
    out = []
    for i in range(steps):
        t = start + i * STEP
        out += s.step(pods, tx(i) if tx else {k: None for k in pods}, trans or {}, t, t + mono_skew)
        s.save()
    for e in out:
        validate(e)
    return out


def totals(events) -> dict:
    out: dict = defaultdict(float)
    for e in events:
        if e["type"] == SANDBOX:
            d = e["data"]
            out[d["state"]] += d["seconds"]
            out[d["state"] + "_vcpu"] += d["vcpu"] * d["seconds"]
            out[d["state"] + "_gib"] += d["memory_gib"] * d["seconds"]
    return dict(out)


# --- split -------------------------------------------------------------------


def test_split_same_state_is_one_exact_span():
    assert split("running", "running", 0, 15, 15, None) == [(0, 15, "running", 15, False)]


def test_split_at_the_recorded_transition():
    assert split("running", "paused", 0, 15, 15, [(5.0, True)]) == [
        (0, 5.0, "running", 5.0, False),
        (5.0, 15, "paused", 10.0, False),
    ]


def test_split_without_a_recorded_transition_is_midpoint_and_approximate():
    assert split("running", "paused", 0, 10, 10, None) == [
        (0, 5.0, "running", 5.0, True),
        (5.0, 10, "paused", 5.0, True),
    ]


def test_split_seconds_follow_the_monotonic_clock():
    (span,) = split("running", "running", 0, 10, 9.0, None)
    assert span[3] == 9.0  # a wall-clock step (NTP) shifts stamps, not seconds


def test_state_at_uses_the_last_transition_before_t():
    trans = [(10.0, True), (20.0, False)]
    assert [state_at(trans, t) for t in (5, 10, 15, 25)] == ["running", "paused", "paused", "running"]
    assert state_at(None, 1) == "running"


# --- acceptance A ------------------------------------------------------------


def test_ten_minutes_running_five_paused_then_deleted(tmp_path):
    s = sampler(tmp_path)
    p = pod(created=T0 - 1)
    trans = {"cri-1": [(T0 + 600.0, True)]}
    steps = int(900 / STEP) + 1
    ev = run(s, steps, pods={"cri-1": p}, trans=trans)
    ev += s.step({}, {}, trans, T0 + 900 + STEP, T0 + 900 + STEP)  # deleted
    tot = totals(ev)
    assert tot["running"] == pytest.approx(600, abs=STEP)
    assert tot["paused"] == pytest.approx(300, abs=STEP)
    assert tot["paused_vcpu"] == 0 and tot["paused_gib"] == pytest.approx(4 * tot["paused"])
    assert tot["running_vcpu"] == pytest.approx(2 * tot["running"])
    assert [e["data"]["reason"] for e in ev if e["type"] == GAP] == ["metering-start"]
    assert all(not e["data"]["approximate"] for e in ev if e["type"] == SANDBOX and e["time"] <= "2026-01-01T00:15:00")


def test_vanished_pod_is_charged_to_the_midpoint_approximately(tmp_path):
    s = sampler(tmp_path)
    run(s, 2)
    (last,) = s.step({}, {}, {}, T0 + 2 * STEP, T0 + 2 * STEP)
    assert last["data"]["seconds"] == STEP / 2 and last["data"]["approximate"] is True


def test_stopped_sandbox_has_no_pod_and_no_node_seconds(tmp_path):
    s = sampler(tmp_path)
    run(s, 2)
    s.step({}, {}, {}, T0 + 2 * STEP, T0 + 2 * STEP)
    assert run(s, 4, start=T0 + 3 * STEP, pods={}) == []


def test_new_pod_starts_at_its_creation(tmp_path):
    s = sampler(tmp_path)
    run(s, 1, pods={})
    (e,) = s.step({"cri-1": pod(created=T0 + 10)}, {"cri-1": None}, {}, T0 + STEP, T0 + STEP)
    assert e["data"]["seconds"] == 5.0 and e["data"]["window_start"] == "2026-01-01T00:00:10.000Z"


# --- gaps, restart, replay ---------------------------------------------------


def test_first_sample_emits_metering_start_gap_not_usage(tmp_path):
    (gap,) = sampler(tmp_path).step({"cri-1": pod()}, {"cri-1": None}, {}, T0, T0)
    assert gap["type"] == GAP
    assert (gap["data"]["gap_start"], gap["data"]["gap_end"]) == (
        "2025-12-31T23:58:20.000Z",
        "2026-01-01T00:00:00.000Z",
    )


def test_agent_restart_is_a_gap_and_never_double_counted(tmp_path):
    a = sampler(tmp_path)
    first = run(a, 3, tx=lambda i: {"cri-1": 100 * (i + 1)})
    restarted = sampler(tmp_path)
    t = T0 + 10 * STEP  # down for 7 intervals
    gaps = restarted.step({"cri-1": pod()}, {"cri-1": 5000}, {}, t, 0.0)
    restarted.save()
    assert {(g["data"]["meter"], g["data"]["reason"]) for g in gaps} == {
        ("sandbox", "agent-restart"),
        ("egress", "agent-restart"),
    }
    assert {g["data"]["gap_start"] for g in gaps} == {"2026-01-01T00:00:30.000Z"}
    after = restarted.step({"cri-1": pod()}, {"cri-1": 5100}, {}, t + STEP, STEP)
    covered = [(e["data"]["window_start"], e["data"]["window_end"]) for e in first + after if e["type"] == SANDBOX]
    assert len(covered) == len(set(covered))
    assert totals(first + after)["running"] == 3 * STEP  # two windows before the restart, one after
    assert [e["data"]["bytes"] for e in after if e["type"] == EGRESS] == [100]


def test_split_transition_at_the_window_end_is_exact():
    assert split("running", "paused", 0, 15, 15, [(15.0, True)]) == [(0, 15, "running", 15, False)]


def test_replay_produces_identical_ids(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = run(sampler(tmp_path / "a"), 5, tx=lambda i: {"cri-1": 1000 * i})
    b = run(sampler(tmp_path / "b"), 5, tx=lambda i: {"cri-1": 1000 * i})
    assert [e["id"] for e in a] == [e["id"] for e in b] and len({e["id"] for e in a}) == len(a)


# --- egress ------------------------------------------------------------------


def test_egress_delta_and_counter_reset(tmp_path):
    tx = [100, 600, 50, 80]
    ev = [e for e in run(sampler(tmp_path), 4, tx=lambda i: {"cri-1": tx[i]}) if e["type"] == EGRESS]
    assert [(e["data"]["bytes"], e["data"]["counter_reset"]) for e in ev] == [(500, False), (50, True), (30, False)]
    assert {e["data"]["scope"] for e in ev} == {"total"}


def test_new_pod_sandbox_id_counts_from_zero(tmp_path):
    s = sampler(tmp_path)
    run(s, 2, tx=lambda i: {"cri-1": 900})
    ev = s.step({"cri-2": pod(cri="cri-2", uid="uid-2")}, {"cri-2": 40}, {}, T0 + 2 * STEP, T0 + 2 * STEP)
    assert [e["data"]["bytes"] for e in ev if e["type"] == EGRESS] == [40]


def test_empty_stats_mark_the_backend_unavailable_never_zero(tmp_path):
    s = sampler(tmp_path)
    pods = {"cri-1": pod(), "cri-2": pod(cri="cri-2", uid="uid-2", backend="kata-qemu-longhorn")}
    ev = run(s, 3, pods=pods, tx=lambda i: {"cri-1": None, "cri-2": 10 * i})
    assert s.egress_unavailable == {"k7d": True, "kata-qemu-longhorn": False}
    assert {e["data"]["sandbox_uid"] for e in ev if e["type"] == EGRESS} == {"uid-2"}


# --- CRI parsing -------------------------------------------------------------


def _item(**over) -> dict:
    item = {
        "id": "cri-1",
        "metadata": {"name": "sb-abc", "uid": "pod-uid", "namespace": "acme-dev"},
        "labels": {SANDBOX_LABEL: "sb", TENANT_LABEL: "acme", KEY_ID_LABEL: "key-1"},
        "annotations": {BACKEND_ANN: "k7d", SANDBOX_UID_ANN: "uid-1", VCPU_ANN: "2", MEMORY_ANN: str(4 * GIB)},
        "createdAt": str(int((T0 - 100) * 1e9)),
        "runtimeHandler": "k7",
    }
    item.update(over)
    return item


def test_parse_pods():
    plain = _item(id="cri-2", labels={"app": "x"})
    by_handler = _item(id="cri-3", annotations={}, runtimeHandler="kata")
    unknown = _item(id="cri-4", annotations={}, runtimeHandler="runc")
    pods, errors = parse_pods({"items": [_item(), plain, by_handler, unknown]})
    assert pods["cri-1"] == pod()
    assert pods["cri-3"].backend == "kata-firecracker-devmapper" and pods["cri-3"].uid == "pod-uid"
    assert set(pods) == {"cri-1", "cri-3"}
    assert errors == ["acme-dev/sb-abc: unknown backend None (no k7.katakate.org/backend)"]


def test_parse_tx_reads_the_default_interface_only():
    stats = {
        "stats": [
            {
                "attributes": {"id": "cri-1"},
                "linux": {
                    "network": {
                        "defaultInterface": {"name": "eth0", "txBytes": {"value": "1234"}},
                        "interfaces": [{"name": "tap0_kata", "txBytes": {"value": "99999"}}],
                    }
                },
            },
            {"attributes": {"id": "cri-2"}, "linux": {"network": None}},
            {"attributes": {"id": "cri-3"}},
        ]
    }
    assert parse_tx(stats) == {"cri-1": 1234, "cri-2": None, "cri-3": None}


# --- pause transitions -------------------------------------------------------


def test_pause_transitions_are_recorded_and_bounded(tmp_path):
    for i in range(pauses.KEEP + 5):
        pauses.record("cri-1", i % 2 == 0, t=T0 + i, root=tmp_path)
    pauses.record("old", True, t=T0 - pauses.MAX_AGE_S - 1, root=tmp_path)
    pauses.record("cri-2", False, t=T0 + 100, root=tmp_path)
    got = pauses.load(tmp_path)
    assert len(got["cri-1"]) == pauses.KEEP and got["cri-1"][-1] == (T0 + pauses.KEEP + 4, True)
    assert "old" not in got and got["cri-2"] == [(T0 + 100, False)]
