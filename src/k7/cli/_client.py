"""CLI ↔ API client plumbing (Spec 10g).

This module owns:

- ``_resolve_api_url`` / ``_resolve_api_key`` — the four-stage lookup
  chain (flag → env → config file → ``/etc/k7/api_*``) shared by every
  CLI handler that talks to the API.
- ``CliContext`` — the dataclass carried on ``typer.Context.obj``. Holds
  either a configured ``katakate.Client`` (the default) or a flag
  signalling the ``--core`` escape hatch (direct ``K7Core`` calls, no
  HTTP, used by integration tests and node-side debugging).
- ``ApiUnreachable`` — a small exception type the CLI catches to map
  ``requests.ConnectionError`` / ``Timeout`` to a single helpful exit
  message ("could not reach API at <URL>: ...").
- ``handle_api_call(fn)`` — decorator-style helper that runs an SDK
  call, maps HTTP / connection errors to ``typer.Exit(1)``, and surfaces
  the server's ``{"error": {"message": ...}}`` body when present.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import requests
import typer

from k7_sdk.client import Client

from ._config import get_config_value

# ---------------------------------------------------------------------------
# Endpoint + key resolution.
# ---------------------------------------------------------------------------

_ETC_API_ENDPOINT = Path("/etc/k7/api_endpoint")
_ETC_API_KEYS = Path("/etc/k7/api_keys.json")


def _resolve_api_url(flag: str | None) -> str | None:
    """Look up the API URL through the documented chain.

    Precedence: ``--api-url`` flag > ``K7_API_URL`` env var > config file >
    ``/etc/k7/api_endpoint`` (on a cluster node). Returns ``None`` when
    nothing is set; the caller decides whether that's an error.
    """
    if flag:
        return flag
    env = os.environ.get("K7_API_URL")
    if env:
        return env
    cfg = get_config_value("api.url")
    if cfg:
        return cfg
    if _ETC_API_ENDPOINT.exists():
        try:
            v = _ETC_API_ENDPOINT.read_text().strip()
            if v:
                return v
        except OSError:
            pass
    return None


def _resolve_api_key(flag: str | None) -> str | None:
    """Same chain as ``_resolve_api_url`` but for the API key.

    Cluster-node fallback reads ``/etc/k7/api_keys.json`` (the same file
    ``generate_api_key`` writes to) and picks the **first** entry's raw
    token. Note: that file stores SHA-256 hashes, not raw keys, so the
    fallback only works when k7 wrote the raw key alongside (current
    layout: each entry has both ``hash`` and ``token`` fields when
    created by ``k7 generate-api-key``). If only hashes are present the
    fallback fails gracefully and the user must pass ``--api-key`` or
    set ``K7_API_KEY``.
    """
    if flag:
        return flag
    env = os.environ.get("K7_API_KEY")
    if env:
        return env
    cfg = get_config_value("api.key")
    if cfg:
        return cfg
    if _ETC_API_KEYS.exists():
        try:
            data = json.loads(_ETC_API_KEYS.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        # File layout: {sha256_hash: {"name": ..., "token": "...", ...}, ...}.
        # Newer entries store the raw token under ``token``; pick the first
        # one we find (deterministic since dict preserves insertion order).
        if isinstance(data, dict):
            for entry in data.values():
                if isinstance(entry, dict):
                    token = entry.get("token")
                    if isinstance(token, str) and token:
                        return token
    return None


# ---------------------------------------------------------------------------
# CliContext + adapters.
# ---------------------------------------------------------------------------


@dataclass
class CliContext:
    """The ``typer.Context.obj`` populated by ``@app.callback()``.

    The ``katakate.Client`` is built **lazily** by :meth:`client`, so
    handlers that don't talk to the API (``install``, ``start-api``,
    ``config set`` …) never trigger the missing-URL / missing-key
    error paths.

    ``use_core=True`` flips the CLI to direct ``K7Core`` calls (the
    legacy in-process path) — useful for integration tests on the node
    and for debugging when the API itself is misbehaving.
    """

    use_core: bool = False
    api_url: str | None = None
    api_key: str | None = None
    _client: Client | None = None

    def client(self) -> Client:
        """Resolve and cache the SDK client; exits 1 when the URL / key are unset."""
        if self._client is None:
            self._client = resolve_client(self.api_url, self.api_key)
        return self._client


class ApiUnreachable(RuntimeError):
    """Raised when the SDK can't reach the API at all (connection / timeout)."""


def resolve_client(api_url: str | None, api_key: str | None) -> Client:
    """Build a ``katakate.Client`` from the resolved URL + key.

    Raises ``typer.Exit(1)`` with a pointed message when either is
    missing — the CLI should never silently fall through to a broken
    Client constructor.
    """
    url = _resolve_api_url(api_url)
    if not url:
        typer.echo(
            "❌ No API URL configured. Pass --api-url, set K7_API_URL, "
            "or run `k7 config set api.url https://<node>:<nodeport>`.",
            err=True,
        )
        raise typer.Exit(1)
    key = _resolve_api_key(api_key)
    if not key:
        typer.echo(
            "❌ No API key configured. Pass --api-key, set K7_API_KEY, "
            "or run `k7 config set api.key <KEY>` "
            "(generate one with `k7 generate-api-key <name>` on the node).",
            err=True,
        )
        raise typer.Exit(1)
    # NodePort exposes plain HTTP; off-cluster setups should put the API
    # behind ingress + TLS, in which case verify_ssl=True (the default)
    # does the right thing. For local/demo HTTP, the SDK skips verify.
    return Client(endpoint=url, api_key=key, verify_ssl=url.startswith("https://"))


T = TypeVar("T")


def handle_api_call(fn: Callable[[], T]) -> T:
    """Run an SDK call and exit-1 on the documented failure shapes.

    Translates ``requests.ConnectionError`` / ``Timeout`` into a single
    "could not reach API" message and maps ``HTTPError`` to the server's
    ``{"error": {"message": ...}}`` body when present (otherwise the
    raw ``status_code``).
    """
    try:
        return fn()
    except requests.ConnectionError as e:
        typer.echo(f"❌ Could not reach the K7 API: {e}", err=True)
        raise typer.Exit(1) from None
    except requests.Timeout as e:
        typer.echo(f"❌ Timed out talking to the K7 API: {e}", err=True)
        raise typer.Exit(1) from None
    except requests.HTTPError as e:
        message: str | None = None
        if e.response is not None:
            try:
                body = e.response.json()
                if isinstance(body, dict) and isinstance(body.get("error"), dict):
                    message = body["error"].get("message")
            except ValueError:
                pass
            if not message:
                message = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        typer.echo(f"❌ {message or e}", err=True)
        raise typer.Exit(1) from None
