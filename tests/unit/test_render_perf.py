"""Deterministic-Markdown test for bench/docker-perf/render.py.

The point of this test is the bit-stable rendered output, not the
benchmark numbers themselves — when someone re-runs the renderer on
the same CSV, the markdown that lands in ``PERFORMANCE.md`` should be
byte-identical, so PR diffs only ever show real number changes (never
formatting drift).

We exercise:
- a 1-node CSV (only ``host`` + ``k7-fd`` + ``k7-ql-r1``) — the
  ``k7-ql-r2`` column disappears cleanly,
- the ``(max - min) / median > 0.30`` flag,
- and the ratio-vs-host table.
"""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parents[2] / "bench" / "docker-perf"


def _import_render():
    """Import ``bench/docker-perf/render.py`` without installing it as a package."""
    spec = importlib.util.spec_from_file_location("_render_perf", BENCH_DIR / "render.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def render():
    return _import_render().render


def _write_csv(path: Path, rows: list[tuple[str, str, int, float]]) -> Path:
    p = path / "in.csv"
    lines = ["label,operation,run,seconds"]
    for label, op, run, secs in rows:
        lines.append(f"{label},{op},{run},{secs}")
    p.write_text("\n".join(lines) + "\n")
    return p


# A tight, deterministic dataset. Numbers chosen so:
#  - host is fast,
#  - k7-fd is ~2× host on build,
#  - k7-ql-r1 is ~4× host on build,
#  - k7-ql-r2 is absent (1-node fixture).
# All ranges stay below the 30% gate so the ⚠ marker is exercised by
# the second fixture (test_range_gate_flags_noisy_cell).
SMOOTH_ROWS: list[tuple[str, str, int, float]] = []
for run, secs in enumerate([3.0, 3.05, 3.1, 3.02, 3.08], start=1):
    SMOOTH_ROWS.append(("host", "pull", run, secs))
for run, secs in enumerate([60.0, 60.5, 61.0, 60.2, 60.8], start=1):
    SMOOTH_ROWS.append(("host", "build_nocache", run, secs))
for run, secs in enumerate([0.5, 0.5, 0.5, 0.5, 0.5], start=1):
    SMOOTH_ROWS.append(("host", "build_cached", run, secs))
for run, secs in enumerate([10.0, 10.0, 10.0, 10.0, 10.0], start=1):
    SMOOTH_ROWS.append(("host", "run_cpu", run, secs))
for run, secs in enumerate([8.0, 8.2, 8.0, 8.1, 8.0], start=1):
    SMOOTH_ROWS.append(("host", "run_io", run, secs))
for run, secs in enumerate([2.0, 2.1, 2.0, 2.0, 2.05], start=1):
    SMOOTH_ROWS.append(("host", "run_read", run, secs))

for run, secs in enumerate([3.5, 3.55, 3.6, 3.55, 3.5], start=1):
    SMOOTH_ROWS.append(("k7-fd", "pull", run, secs))
for run, secs in enumerate([120.0, 121.0, 120.5, 120.8, 121.2], start=1):
    SMOOTH_ROWS.append(("k7-fd", "build_nocache", run, secs))
for run, secs in enumerate([0.6, 0.6, 0.6, 0.6, 0.6], start=1):
    SMOOTH_ROWS.append(("k7-fd", "build_cached", run, secs))
for run, secs in enumerate([10.5, 10.5, 10.4, 10.6, 10.5], start=1):
    SMOOTH_ROWS.append(("k7-fd", "run_cpu", run, secs))
for run, secs in enumerate([16.0, 16.2, 16.1, 16.0, 16.05], start=1):
    SMOOTH_ROWS.append(("k7-fd", "run_io", run, secs))
for run, secs in enumerate([2.5, 2.55, 2.5, 2.6, 2.55], start=1):
    SMOOTH_ROWS.append(("k7-fd", "run_read", run, secs))

for run, secs in enumerate([3.6, 3.7, 3.6, 3.65, 3.6], start=1):
    SMOOTH_ROWS.append(("k7-ql-r1", "pull", run, secs))
for run, secs in enumerate([240.0, 241.0, 240.5, 240.8, 241.2], start=1):
    SMOOTH_ROWS.append(("k7-ql-r1", "build_nocache", run, secs))
for run, secs in enumerate([0.7, 0.7, 0.7, 0.7, 0.7], start=1):
    SMOOTH_ROWS.append(("k7-ql-r1", "build_cached", run, secs))
for run, secs in enumerate([10.6, 10.6, 10.7, 10.6, 10.5], start=1):
    SMOOTH_ROWS.append(("k7-ql-r1", "run_cpu", run, secs))
for run, secs in enumerate([32.0, 32.2, 32.1, 32.0, 32.05], start=1):
    SMOOTH_ROWS.append(("k7-ql-r1", "run_io", run, secs))
for run, secs in enumerate([3.0, 3.05, 3.0, 3.1, 3.0], start=1):
    SMOOTH_ROWS.append(("k7-ql-r1", "run_read", run, secs))


def test_render_one_node_csv_is_byte_stable(tmp_path: Path, render):
    csv = _write_csv(tmp_path, SMOOTH_ROWS)
    out = render(csv, title="Docker workloads — fixture", hardware_note="Hardware: fixture.")
    # The ``k7-ql-r2`` column must NOT appear (CSV has no rows for it).
    assert "k7-ql-r2" not in out
    # Headings + the two-table layout are present.
    assert "## Docker workloads — fixture" in out
    assert "5 runs per cell, median (range in parens)" in out
    assert "Hardware: fixture." in out
    # Both tables present.
    assert out.count("| Operation                 |") == 1
    assert out.count("| Cell                      |") == 1
    # Median is selected, not mean: host.build_nocache median is 60.5
    # (values 60.0 60.2 60.5 60.8 61.0 → median 60.5).
    assert "60.5 s" in out
    # Ratio rows use × on the second table — k7-ql-r1 vs host on build_nocache
    # is 240.5 / 60.5 ≈ 3.98×.
    assert "3.98×" in out
    # And build_cached (excluded from the ratio table) does NOT appear there.
    cells_idx = out.index("| Cell")
    ratio_section = out[cells_idx:]
    assert "build (cached)" not in ratio_section


def test_render_flags_noisy_cell(tmp_path: Path, render):
    rows = [
        ("host", "run_io", 1, 1.0),
        ("host", "run_io", 2, 1.0),
        ("host", "run_io", 3, 1.0),
        ("host", "run_io", 4, 1.0),
        ("host", "run_io", 5, 1.0),
        # 1.0 .. 2.0 → range/median = 1.0 > 0.30, must trip the ⚠ flag.
        ("k7-fd", "run_io", 1, 1.0),
        ("k7-fd", "run_io", 2, 1.4),
        ("k7-fd", "run_io", 3, 1.5),
        ("k7-fd", "run_io", 4, 1.7),
        ("k7-fd", "run_io", 5, 2.0),
    ]
    csv = _write_csv(tmp_path, rows)
    out = render(csv, title="noisy")
    # The k7-fd row of run_io must carry the ⚠ flag in its cell.
    io_row = next(line for line in out.splitlines() if line.startswith("| run io"))
    assert "⚠" in io_row, io_row


def test_render_output_is_deterministic(tmp_path: Path, render):
    csv = _write_csv(tmp_path, SMOOTH_ROWS)
    a = render(csv, title="t", hardware_note="hw")
    b = render(csv, title="t", hardware_note="hw")
    assert a == b


def test_render_handles_missing_envs_gracefully(tmp_path: Path, render):
    rows = [
        ("host", "pull", 1, 1.0),
        ("host", "pull", 2, 1.0),
        ("k7-fd", "pull", 1, 2.0),
        ("k7-fd", "pull", 2, 2.0),
    ]
    csv = _write_csv(tmp_path, rows)
    out = render(csv, title="partial")
    assert "k7-ql-r1" not in out
    assert "k7-ql-r2" not in out
    # The single-cell rows that are missing render as ``—`` for the absent ops.
    assert "—" in out


def test_render_known_byte_stable_snapshot(tmp_path: Path, render):
    """Lock down the rendered output for a small fixture.

    If you change render.py's formatting deliberately, update this string.
    The intent is to fail loudly when an unintended whitespace / column-
    width drift sneaks in.
    """
    rows = [
        ("host", "pull", 1, 3.0),
        ("host", "pull", 2, 3.0),
        ("host", "build_nocache", 1, 60.0),
        ("host", "build_nocache", 2, 60.0),
        ("k7-fd", "pull", 1, 3.5),
        ("k7-fd", "pull", 2, 3.5),
        ("k7-fd", "build_nocache", 1, 120.0),
        ("k7-fd", "build_nocache", 2, 120.0),
    ]
    csv = _write_csv(tmp_path, rows)
    out = render(csv, title="snapshot", hardware_note="hw note")
    expected = textwrap.dedent(
        """\
        ## snapshot

        hw note

        5 runs per cell, median (range in parens). ⚠ marks cells whose (max−min)/median > 0.30.

        | Operation                 | host | k7-fd |
        |---------------------------|------|-------|
        | pull debian:12-slim       | 3.00 s (3.00 s–3.00 s) | 3.50 s (3.50 s–3.50 s) |
        | build (no-cache)          | 60.0 s (60.0 s–60.0 s) | 120.0 s (120.0 s–120.0 s) |
        | build (cached)            | — | — |
        | run cpu (10s budget)      | — | — |
        | run io (2k small + 512 MB)| — | — |
        | run read (venv tree cat)  | — | — |

        | Cell                      | k7-fd / host |
        |---------------------------|--------------|
        | build (no-cache) ratio    | 2.00× |
        | run io ratio              | — |
        | run cpu ratio             | — |
        """
    )
    assert out == expected, f"--- expected ---\n{expected}\n--- got ---\n{out}"
