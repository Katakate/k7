"""Read-only API key stores for k7-api.

``SecretKeyStore`` serves authentication from an in-memory snapshot of
the ``k7-api-keys`` Secret, re-read every ``POLL_SECONDS``. Nothing is
written on authenticate. After ``STALE_SECONDS`` without a successful
read, auth fails with 503 — never a stale allow. ``last_used`` is kept
per replica in memory and max-merged into ``k7-api-keys-usage`` every
``FLUSH_SECONDS``; a failed flush is logged and shown in ``/health``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException

from .. import metrics
from ..core.core import K7Core, aclose_k7_core
from ..deploy.k7_api_keys import KEYS_SECRET, USAGE_SECRET, decode_keys, legacy_entries

POLL_SECONDS = 5
STALE_SECONDS = 60
FLUSH_SECONDS = 300

log = logging.getLogger("k7.keystore")


class KeyStore(Protocol):
    def snapshot(self) -> dict[str, dict]: ...
    def touch(self, key_id: str) -> None: ...
    def health(self) -> dict: ...


class FileKeyStore:
    """Deprecated read-only view of ``/etc/k7/api_keys.json`` (``K7_API_KEY_STORE=file``)."""

    def __init__(self, path: Path):
        self.path = path

    def snapshot(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"API key store {self.path} is unreadable ({e})")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"API key store {self.path} is corrupt: {e}")
        return legacy_entries(data)

    def touch(self, key_id: str) -> None:
        pass

    def health(self) -> dict:
        return {"store": "file", "path": str(self.path)}


class SecretKeyStore:
    def __init__(self, namespace: str):
        self.namespace = namespace
        self._keys: dict[str, dict] = {}
        self._synced_at: float | None = None
        self._used: dict[str, int] = {}
        self.sync_error: str | None = None
        self.flush_error: str | None = None

    def snapshot(self) -> dict[str, dict]:
        if self._synced_at is None or time.monotonic() - self._synced_at > STALE_SECONDS:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"key store stale: no successful read of Secret {self.namespace}/{KEYS_SECRET} "
                    f"in {STALE_SECONDS}s (last error: {self.sync_error})"
                ),
            )
        return self._keys

    def touch(self, key_id: str) -> None:
        self._used[key_id] = int(time.time())

    def health(self) -> dict:
        age = None if self._synced_at is None else round(time.monotonic() - self._synced_at, 1)
        return {
            "store": "secret",
            "synced_age_s": age,
            "sync_error": self.sync_error,
            "usage_flush_error": self.flush_error,
        }

    async def refresh(self, core: K7Core) -> None:
        data = await core.read_secret_data(KEYS_SECRET, self.namespace)
        if data is None:
            raise RuntimeError(f"Secret {self.namespace}/{KEYS_SECRET} not found; run `k7 api keys migrate`")
        self._keys = decode_keys(data)
        self._synced_at = time.monotonic()
        metrics.KEY_STORE_SYNC.set_to_current_time()

    async def flush(self, core: K7Core) -> None:
        if not self._used:
            return
        pending = dict(self._used)
        current = await core.read_secret_data(USAGE_SECRET, self.namespace)
        if current is None:
            raise RuntimeError(f"Secret {self.namespace}/{USAGE_SECRET} not found; run `k7 api keys migrate`")
        merged = {k: str(max(v, int(current.get(k) or 0))) for k, v in pending.items()}
        await core.patch_secret_data(USAGE_SECRET, self.namespace, merged)
        for k, v in pending.items():
            if self._used.get(k) == v:
                del self._used[k]

    async def _step(self, op, label: str) -> str | None:
        # Fresh client per step so rotated ServiceAccount tokens are picked up.
        core = K7Core()
        try:
            await op(core)
            return None
        except Exception as e:
            log.error("key store %s failed: %s", label, e)
            return str(e)
        finally:
            await aclose_k7_core(core)

    async def run(self) -> None:
        last_flush = time.monotonic()
        while True:
            self.sync_error = await self._step(self.refresh, "sync")
            if time.monotonic() - last_flush >= FLUSH_SECONDS:
                last_flush = time.monotonic()
                self.flush_error = await self._step(self.flush, "usage flush")
            await asyncio.sleep(POLL_SECONDS)
