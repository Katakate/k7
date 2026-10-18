"""Snapshot garbage-collection entrypoint (Option C backstop).

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
- Reaps orphan ``VolumeSnapshotContent`` objects (and stuck
  ``pvc-as-source-protection`` PVC finalizers) whose VolumeSnapshot is
  already gone. That is what otherwise pins ``k7-test-*`` namespaces in
  Terminating after snapshot-lifecycle tests.
- Environment overrides: ``K7_GC_KEEP_FORK_FOR_MINUTES`` (default ``10``),
  ``K7_GC_DRY_RUN`` (``true``/``false``, default ``false``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from k7 import logging as k7_logging
from k7.core.core import K7Core

log = logging.getLogger("k7.snapshot_gc")


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("⚠️  Invalid integer for %s=%r; using default %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "y", "on")


async def _main() -> int:
    k7_logging.configure_from_env()
    k7_logging.new_context(request_id=k7_logging.uuid7(), op="snapshot-gc")
    keep_minutes = _env_int("K7_GC_KEEP_FORK_FOR_MINUTES", 10)
    dry_run = _env_bool("K7_GC_DRY_RUN", False)
    core = K7Core()
    try:
        result = await core.gc_snapshots(
            all_namespaces=True,
            keep_fork_for=timedelta(minutes=keep_minutes),
            dry_run=dry_run,
        )
        if not result.success:
            log.error("❌ snapshot-gc failed: %s", result.error)
            return 1
        log.info("%s", result.message)
        for record in result.data or []:
            marker = "would-delete" if dry_run else ("deleted" if record.get("deleted") else "failed")
            log.info("  [%s] %s/%s (age=%s)", marker, record["namespace"], record["name"], record["age"])
        return 0
    finally:
        await core.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
