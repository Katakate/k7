"""Node-local durable usage spool.

Events are appended as JSON lines to numbered segment files and fsynced
before ``append`` returns. Each spool sink keeps a byte offset; a segment
every sink has read past is deleted. Over ``max_bytes`` the oldest segment
is dropped and its unread events are counted, never hidden. Delivery is at
least once: a sink re-reads from its last saved offset after a crash.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger("k7.metering.spool")
Cursor = tuple[int, int]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class Spool:
    def __init__(
        self,
        root: Path,
        max_bytes: int,
        high_water_percent: int,
        sinks: Iterable[str],
        on_drop: Callable[[int], None],
        segment_bytes: int = 4 * 2**20,
    ):
        self.root, self.max_bytes, self.high_water_percent = root, max_bytes, high_water_percent
        self.segment_bytes = min(segment_bytes, max_bytes // 8)
        self.on_drop = on_drop
        self.dropped = 0
        self.last_drop: float | None = None
        self._lock = threading.Lock()
        self.segments, self.offsets = root / "segments", root / "offsets"
        self.segments.mkdir(parents=True, exist_ok=True)
        self.offsets.mkdir(exist_ok=True)
        self.sinks = tuple(sinks)
        for stale in set(self._offset_files()) - set(self.sinks):
            log.warning("usage spool: dropping the offset of removed sink %s", stale)
            (self.offsets / f"{stale}.json").unlink()
        seqs = self._seqs()
        self.active = seqs[-1] if seqs else 1
        self._repair_tail()
        self.cursors = {s: self._load_cursor(s) for s in self.sinks}

    def _offset_files(self) -> list[str]:
        return [p.stem for p in self.offsets.glob("*.json")]

    def _seqs(self) -> list[int]:
        return sorted(int(p.stem.removeprefix("seg-")) for p in self.segments.glob("seg-*.jsonl"))

    def _path(self, seq: int) -> Path:
        return self.segments / f"seg-{seq:020d}.jsonl"

    def _repair_tail(self) -> None:
        """A crash mid-write leaves a partial last line in the active segment: cut it."""
        path = self._path(self.active)
        if not path.exists():
            return
        data = path.read_bytes()
        keep = data.rfind(b"\n") + 1
        if keep != len(data):
            log.warning("usage spool: truncating %d partial bytes at the end of %s", len(data) - keep, path.name)
            with open(path, "r+b") as f:
                f.truncate(keep)
                os.fsync(f.fileno())

    def _load_cursor(self, sink: str) -> Cursor:
        path = self.offsets / f"{sink}.json"
        if not path.exists():
            seqs = self._seqs()
            return (seqs[0] if seqs else self.active, 0)
        raw = json.loads(path.read_text())
        return int(raw["segment"]), int(raw["offset"])

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.segments.glob("seg-*.jsonl"))

    def above_high_water(self) -> bool:
        return self.size() * 100 >= self.max_bytes * self.high_water_percent

    def append(self, events: list[dict]) -> None:
        if not events:
            return
        data = b"".join(json.dumps(e, separators=(",", ":"), sort_keys=True).encode() + b"\n" for e in events)
        with self._lock:
            path = self._path(self.active)
            if path.exists() and path.stat().st_size and path.stat().st_size + len(data) > self.segment_bytes:
                self.active += 1
                path = self._path(self.active)
            new = not path.exists()
            with open(path, "ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if new:
                _fsync_dir(self.segments)
            self._enforce_cap()
            self._gc()

    def _enforce_cap(self) -> None:
        while self.size() > self.max_bytes:
            seqs = self._seqs()
            if len(seqs) == 1:
                self.active += 1
                seqs = self._seqs()
            oldest = seqs[0]
            data = self._path(oldest).read_bytes()
            start = (
                min((off if seq == oldest else len(data)) for seq, off in self.cursors.values()) if self.cursors else 0
            )
            lost = data[start:].count(b"\n") if self.cursors else 0
            self._path(oldest).unlink()
            for sink, (seq, _) in self.cursors.items():
                if seq <= oldest:
                    self._save_cursor(sink, (oldest + 1, 0))
            self.dropped += lost
            self.last_drop = time.time()
            log.error(
                "usage spool over %d bytes: dropped segment %d with %d undelivered event(s)",
                self.max_bytes,
                oldest,
                lost,
            )
            self.on_drop(lost)

    def _save_cursor(self, sink: str, cursor: Cursor) -> None:
        write_atomic(self.offsets / f"{sink}.json", json.dumps({"segment": cursor[0], "offset": cursor[1]}).encode())
        self.cursors[sink] = cursor

    def _gc(self) -> None:
        floor = min([self.active, *(seq for seq, _ in self.cursors.values())])
        for seq in self._seqs():
            if seq < floor:
                self._path(seq).unlink()

    def read(self, sink: str, max_events: int) -> tuple[list[dict], Cursor]:
        """Up to ``max_events`` unread events for ``sink`` and the cursor after them (pass it to ``ack``)."""
        with self._lock:
            seq, off = self.cursors[sink]
            out: list[dict] = []
            while len(out) < max_events and seq <= self.active:
                path = self._path(seq)
                data = path.read_bytes()[off:] if path.exists() else b""
                end = data.rfind(b"\n") + 1
                for line in data[:end].splitlines(keepends=True):
                    if len(out) == max_events:
                        break
                    out.append(json.loads(line))
                    off += len(line)
                if len(out) == max_events or seq == self.active:
                    break
                seq, off = seq + 1, 0
            return out, (seq, off)

    def ack(self, sink: str, cursor: Cursor) -> None:
        with self._lock:
            if cursor[0] < self.cursors[sink][0]:
                return  # the segment was dropped meanwhile; the drop already counted it
            self._save_cursor(sink, cursor)
            self._gc()

    def pending_bytes(self, sink: str) -> int:
        seq, off = self.cursors[sink]
        return sum(
            max(p.stat().st_size - (off if s == seq else 0), 0)
            for s in self._seqs()
            if s >= seq
            for p in [self._path(s)]
        )

    def status(self) -> dict:
        return {
            "path": str(self.root),
            "bytes": self.size(),
            "max_bytes": self.max_bytes,
            "high_water_percent": self.high_water_percent,
            "above_high_water": self.above_high_water(),
            "dropped_events": self.dropped,
            "last_drop": self.last_drop,
            "pending_bytes": {s: self.pending_bytes(s) for s in self.sinks},
        }
