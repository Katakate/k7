"""Snapshot garbage-collection entrypoint (Spec 10e, Option C backstop).

Run as ``python -m k7.api.snapshot_gc`` inside the ``k7-api`` container.
The accompanying CronJob (``snapshot-gc-cronjob.yaml``) invokes this
every 10 minutes to clean up stale ``kind=fork`` VolumeSnapshots that
the inline cleanup in ``fork_sandbox`` missed (e.g. due to an API pod
crash mid-fork).

Behaviour:

- Lists snapshots cluster-wide (``all_namespaces=True``).
- Honours the same ``keep_fork_for`` window as :meth:`K7Core.gc_snapshots`.
- Skips anything that isn't ``kind=fork`` — pause and named snapshots are
  never touched.
- Environment overrides: ``K7_GC_KEEP_FORK_FOR_MINUTES`` (default ``10``),
  ``K7_GC_DRY_RUN`` (``true``/``false``, default ``false``).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

from k7.core.core import K7Core


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️  Invalid integer for {key}={raw!r}; using default {default}", file=sys.stderr)
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "y", "on")


async def _main() -> int:
    keep_minutes = _env_int("K7_GC_KEEP_FORK_FOR_MINUTES", 10)
    dry_run = _env_bool("K7_GC_DRY_RUN", False)
    core = K7Core()
    result = await core.gc_snapshots(
        all_namespaces=True,
        keep_fork_for=timedelta(minutes=keep_minutes),
        dry_run=dry_run,
    )
    if not result.success:
        print(f"❌ snapshot-gc failed: {result.error}", file=sys.stderr)
        return 1
    print(result.message)
    for record in result.data or []:
        marker = "would-delete" if dry_run else ("deleted" if record.get("deleted") else "failed")
        print(f"  [{marker}] {record['namespace']}/{record['name']} (age={record['age']})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
