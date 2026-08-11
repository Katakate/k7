"""CSV → Markdown renderer for the Spec 10b Docker benchmark.

Input CSV (produced by ``run_all.sh`` from ``/tmp/bench-<label>-*.log``):

    label,operation,run,seconds
    host,pull,1,3.142
    host,pull,2,3.118
    ...
    k7-ql-r2,run_read,5,7.214

Output: two Markdown tables.

  1) Raw timings — one row per operation, one column per environment.
     Cell format: ``<median> s (<min>–<max>)``.
  2) Ratio table — for the three "interesting" rows (``build_nocache``,
     ``run_io``, ``run_cpu``), each sandbox env's median divided by host
     median. The number that lands on Hacker News.

The renderer gates publishability on the spec-10b rule "(max - min) /
median > 0.30 → investigate". Failing cells are tagged with ``⚠``.

Deterministic: ``--input`` and stable column ordering mean the same CSV
always produces byte-identical Markdown (see test_render_perf.py).
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from statistics import median

# Stable column ordering for the rendered tables.
ENVS: list[str] = ["host", "k7-fd", "k7-ql-r1", "k7-ql-r2", "k7-ql-r3"]

# Stable row ordering, plus the human-readable column label for each op.
OP_DISPLAY: list[tuple[str, str]] = [
    ("pull", "pull debian:12-slim"),
    ("build_nocache", "build (no-cache)"),
    ("build_cached", "build (cached)"),
    ("run_cpu", "run cpu (10s budget)"),
    ("run_io", "run io (2k small + 512 MB)"),
    ("run_read", "run read (venv tree cat)"),
]

# Operations included in the ratio-vs-host table — the ones that genuinely
# isolate the storage/VM tax. ``pull`` is network-dominated, ``build_cached``
# is sub-second everywhere, ``run_read`` is page-cache-dominated; reporting
# ratios on those would just amplify measurement noise.
RATIO_OPS: list[tuple[str, str]] = [
    ("build_nocache", "build (no-cache) ratio"),
    ("run_io", "run io ratio"),
    ("run_cpu", "run cpu ratio"),
]

RANGE_GATE = 0.30  # spec 10b: (max - min) / median > 0.30 → ⚠


def _load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open() as f:
        return list(csv.DictReader(f))


def _aggregate(rows: Iterable[dict[str, str]]) -> dict[tuple[str, str], dict[str, float]]:
    """Group seconds by (label, operation) and compute median / min / max.

    Returns a ``{(label, op): {"median": …, "min": …, "max": …, "n": …}}`` dict.
    Cells with fewer than 1 sample are omitted (caller renders ``—``).
    """
    by_cell: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        try:
            value = float(row["seconds"])
        except (KeyError, ValueError):
            continue
        # Skip NaN sentinels — these are recorded by the bench when an op
        # (typically docker daemon wedging on vfs after a long no-cache
        # build) fails; aggregating them would poison median/min/max.
        if math.isnan(value):
            continue
        by_cell[(row["label"], row["operation"])].append(value)
    out: dict[tuple[str, str], dict[str, float]] = {}
    for key, values in by_cell.items():
        if not values:
            continue
        m = median(values)
        out[key] = {
            "median": m,
            "min": min(values),
            "max": max(values),
            "n": float(len(values)),
            "flag": 1.0 if m > 0 and (max(values) - min(values)) / m > RANGE_GATE else 0.0,
        }
    return out


def _fmt_secs(value: float) -> str:
    """Friendly seconds display — ms when sub-second, s otherwise."""
    if value < 1.0:
        return f"{value * 1000:.0f} ms"
    if value < 10.0:
        return f"{value:.2f} s"
    return f"{value:.1f} s"


def _fmt_cell(stats: dict[str, float] | None) -> str:
    if stats is None:
        return "—"
    flag = "⚠ " if stats["flag"] >= 0.5 else ""
    return f"{flag}{_fmt_secs(stats['median'])} ({_fmt_secs(stats['min'])}–{_fmt_secs(stats['max'])})"


def _fmt_ratio(stats: dict[str, float] | None, host_stats: dict[str, float] | None) -> str:
    if stats is None or host_stats is None or host_stats["median"] == 0:
        return "—"
    ratio = stats["median"] / host_stats["median"]
    if ratio < 10:
        return f"{ratio:.2f}×"
    return f"{ratio:.1f}×"


def _envs_present(agg: dict[tuple[str, str], dict[str, float]]) -> list[str]:
    """Return the envs from ``ENVS`` that have ≥1 measurement in the CSV.

    Lets the renderer skip the ``k7-ql-r2`` column cleanly when only a
    1-node cluster was available.
    """
    seen = {label for label, _ in agg}
    return [e for e in ENVS if e in seen]


def render(
    csv_path: Path,
    *,
    title: str,
    hardware_note: str = "",
) -> str:
    rows = _load_rows(csv_path)
    agg = _aggregate(rows)
    envs = _envs_present(agg) or ENVS

    lines: list[str] = []
    lines.append(f"## {title}")
    lines.append("")
    if hardware_note:
        lines.append(hardware_note)
        lines.append("")
    lines.append("5 runs per cell, median (range in parens). ⚠ marks cells whose (max−min)/median > 0.30.")
    lines.append("")

    # Raw table.
    header_envs = "|".join(f" {e} " for e in envs)
    sep = "|".join("-" * (len(e) + 2) for e in envs)
    lines.append(f"| Operation                 |{header_envs}|")
    lines.append(f"|---------------------------|{sep}|")
    for op_key, op_display in OP_DISPLAY:
        cells = "|".join(f" {_fmt_cell(agg.get((env, op_key)))} " for env in envs)
        lines.append(f"| {op_display:<26}|{cells}|")
    lines.append("")

    # Ratio table — only the sandbox envs (i.e. drop the host column itself).
    sandbox_envs = [e for e in envs if e != "host"]
    if sandbox_envs and ("host" in envs):
        ratio_labels = [f"{e} / host" for e in sandbox_envs]
        header_envs = "|".join(f" {label} " for label in ratio_labels)
        sep = "|".join("-" * (len(label) + 2) for label in ratio_labels)
        lines.append(f"| Cell                      |{header_envs}|")
        lines.append(f"|---------------------------|{sep}|")
        for op_key, op_display in RATIO_OPS:
            host_stats = agg.get(("host", op_key))
            cells = "|".join(f" {_fmt_ratio(agg.get((env, op_key)), host_stats)} " for env in sandbox_envs)
            lines.append(f"| {op_display:<26}|{cells}|")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render bench CSV to Markdown")
    parser.add_argument("--input", "-i", type=Path, required=True, help="Path to the aggregated CSV")
    parser.add_argument(
        "--title",
        "-t",
        required=True,
        help="Markdown section heading text (e.g. 'Docker workloads inside sandboxes — 2026-05-29 (k7 v<sha>)')",
    )
    parser.add_argument(
        "--hardware",
        default="",
        help="One-line hardware / kernel / Docker / Longhorn / FS note",
    )
    parser.add_argument("--output", "-o", type=Path, default=None, help="Write to this file instead of stdout")
    args = parser.parse_args(argv)

    md = render(args.input, title=args.title, hardware_note=args.hardware)
    if args.output is not None:
        args.output.write_text(md)
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
