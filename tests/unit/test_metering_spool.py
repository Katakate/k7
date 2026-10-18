"""Durable usage spool: fsync, per-sink offsets, overflow accounting."""

import os
from pathlib import Path
from unittest.mock import patch

from k7.metering.spool import Spool

MAX = 16 * 2**20


def ev(i: int, pad: int = 0) -> dict:
    return {"id": f"{i:064x}", "pad": "x" * pad}


def spool(root: Path, sinks=("a", "b"), max_bytes=MAX, drops=None, **kw) -> Spool:
    return Spool(root, max_bytes, 80, sinks, on_drop=(drops if drops is not None else []).append, **kw)


def test_append_fsyncs_before_returning(tmp_path):
    s = spool(tmp_path)
    with patch("k7.metering.spool.os.fsync", wraps=os.fsync) as fsync:
        s.append([ev(1), ev(2)])
    assert fsync.call_count == 2  # the segment, and the directory that gained it
    s.append([ev(3)])
    assert [e["id"] for e in s.read("a", 10)[0]] == [ev(i)["id"] for i in (1, 2, 3)]


def test_offsets_are_per_sink_and_survive_restart(tmp_path):
    s = spool(tmp_path)
    s.append([ev(i) for i in range(5)])
    got, cur = s.read("a", 3)
    assert [e["id"] for e in got] == [ev(i)["id"] for i in range(3)]
    s.ack("a", cur)
    s2 = spool(tmp_path)
    assert [e["id"] for e in s2.read("a", 10)[0]] == [ev(i)["id"] for i in (3, 4)]
    assert len(s2.read("b", 10)[0]) == 5
    assert s2.pending_bytes("a") < s2.pending_bytes("b")


def test_unacked_read_is_redelivered(tmp_path):
    s = spool(tmp_path)
    s.append([ev(1)])
    s.read("a", 10)
    assert spool(tmp_path).read("a", 10)[0] == [ev(1)]


def test_segments_roll_and_are_deleted_once_every_sink_read_them(tmp_path):
    s = spool(tmp_path, segment_bytes=200)
    for i in range(6):
        s.append([ev(i)])
    assert len(s._seqs()) > 1
    for sink in ("a", "b"):
        got, cur = s.read(sink, 100)
        assert len(got) == 6
        s.ack(sink, cur)
    assert s._seqs() == [s.active]


def test_partial_tail_after_crash_is_cut(tmp_path):
    s = spool(tmp_path)
    s.append([ev(1)])
    with open(s._path(s.active), "ab") as f:
        f.write(b'{"id": "half')
    assert spool(tmp_path).read("a", 10)[0] == [ev(1)]


def test_overflow_drops_oldest_and_counts_undelivered(tmp_path):
    drops: list[int] = []
    s = spool(tmp_path, max_bytes=MAX, drops=drops)
    pad = 2**20
    for i in range(2):
        s.append([ev(i, pad)])  # 2 segments of 1 event each (segment_bytes = MAX/8)
    got, cur = s.read("a", 1)  # sink a has read event 0, b nothing
    s.ack("a", cur)
    for i in range(2, 17):
        s.append([ev(i, pad)])
    assert sum(drops) == s.dropped and s.dropped >= 1
    assert s.size() <= MAX
    st = s.status()
    assert st["dropped_events"] == s.dropped and st["last_drop"] is not None
    remaining = [e["id"] for e in s.read("b", 100)[0]]
    assert ev(0)["id"] not in remaining and remaining[-1] == ev(16)["id"]


def test_drop_counts_only_what_the_most_behind_sink_lost(tmp_path):
    drops: list[int] = []
    s = spool(tmp_path, drops=drops, segment_bytes=10**9)
    s.segment_bytes = 200
    s.append([ev(0), ev(1)])  # segment 1: two events
    s.append([ev(2)])  # segment 2
    a, cur_a = s.read("a", 10)
    s.ack("a", cur_a)  # a is past segment 1
    _, cur_b = s.read("b", 1)
    s.ack("b", cur_b)  # b read one of segment 1's two events
    s.max_bytes = s.size() - 1
    s._enforce_cap()
    assert drops == [1]


def test_overflow_without_sinks_loses_nothing_undelivered(tmp_path):
    drops: list[int] = []
    s = spool(tmp_path, sinks=(), drops=drops)
    s.append([ev(1)])
    assert drops == [] and s.status()["pending_bytes"] == {}


def test_high_water_mark(tmp_path):
    s = spool(tmp_path, sinks=("a",))
    assert not s.above_high_water()
    s.append([ev(0, int(MAX * 0.81))])
    assert s.above_high_water() and s.status()["above_high_water"]


def test_removed_sink_offset_is_dropped(tmp_path):
    s = spool(tmp_path)
    s.append([ev(1)])
    s.ack("b", s.read("b", 10)[1])
    assert (tmp_path / "offsets" / "b.json").exists()
    spool(tmp_path, sinks=("a",))
    assert not (tmp_path / "offsets" / "b.json").exists()
