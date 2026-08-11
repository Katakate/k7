"""TOML config file CRUD + ``k7 config`` sub-app (Spec 10g).

Stores per-user CLI config at ``$XDG_CONFIG_HOME/k7/config.toml``
(falling back to ``~/.config/k7/config.toml``). The single supported
section today is ``[api]`` with ``url`` and ``key`` keys — enough to
power the ``_resolve_api_url`` / ``_resolve_api_key`` chain in
``_client.py`` so users don't have to pass ``--api-url`` /
``--api-key`` on every invocation.

The file is the same posture as ``~/.docker/config.json`` and
``~/.kube/config`` — plaintext, chmod 0600 on write. A future spec
can layer OS keychain integration on top.
"""

from __future__ import annotations

import builtins
import json
import os
import re
import sys
from pathlib import Path

import typer

CONFIG_DIR_ENV = "K7_CONFIG_DIR"  # tests override this to a tmp path
_SUPPORTED_KEYS: set[str] = {"api.url", "api.key"}


def _config_dir() -> Path:
    """Return the directory holding ``config.toml``.

    Honours ``K7_CONFIG_DIR`` (tests / explicit overrides), then
    ``XDG_CONFIG_HOME``, then ``~/.config/k7``.
    """
    explicit = os.environ.get(CONFIG_DIR_ENV)
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "k7"


def config_file_path() -> Path:
    return _config_dir() / "config.toml"


_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*$")
_KEY_RE = re.compile(r'^\s*([A-Za-z0-9_-]+)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$')


def _toml_loads_simple(text: str) -> dict:
    """Tiny TOML reader for the section-of-string-values shape this file uses.

    Python 3.10 (k7's minimum) lacks ``tomllib`` and we don't want to add
    ``tomli`` just for two-key parsing. Supports::

        [api]
        url = "https://10.0.0.1:31000"
        key = "k7-..."

    Unknown / malformed lines are ignored (the user's view is "k7 config
    wrote the file; k7 config can read it back" — anything outside that
    contract is best-effort).
    """
    data: dict = {}
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SECTION_RE.match(line)
        if m:
            current = data.setdefault(m.group(1), {})
            continue
        m = _KEY_RE.match(line)
        if not m or current is None:
            continue
        value = m.group(2).encode("utf-8").decode("unicode_escape")
        current[m.group(1)] = value
    return data


def read_config() -> dict:
    """Return the parsed config dict; ``{}`` when the file is absent / unreadable."""
    p = config_file_path()
    if not p.exists():
        return {}
    try:
        return _toml_loads_simple(p.read_text())
    except Exception:
        # A malformed config file shouldn't crash the CLI — fall back to no config
        # and let the next resolution layer (env vars / on-node fallback) take over.
        return {}


def _get_dotted(d: dict, key: str) -> str | None:
    """Look up ``"api.url"`` against ``{"api": {"url": "..."}}``."""
    section, _, leaf = key.partition(".")
    if not leaf:
        return None
    sub = d.get(section)
    if not isinstance(sub, dict):
        return None
    val = sub.get(leaf)
    return str(val) if val is not None else None


def _set_dotted(d: dict, key: str, value: str) -> None:
    section, _, leaf = key.partition(".")
    if not leaf:
        raise ValueError(f"Invalid config key {key!r}; expected 'section.field' form")
    sub = d.setdefault(section, {})
    if not isinstance(sub, dict):  # pragma: no cover - defensive against malformed user files
        sub = {}
        d[section] = sub
    sub[leaf] = value


def _toml_dumps(d: dict) -> str:
    """Serialise a simple ``{section: {key: str}}`` dict to TOML.

    We don't pull in ``tomli_w`` for this — k7 only ever writes flat
    string fields under a single ``[api]`` section. Keep the surface
    tiny on purpose.
    """
    lines: builtins.list[str] = []
    for section in sorted(d.keys()):
        body = d[section]
        if not isinstance(body, dict):
            continue
        lines.append(f"[{section}]")
        for key in sorted(body.keys()):
            value = body[key]
            if value is None:
                continue
            lines.append(f"{key} = {json.dumps(str(value))}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_config(data: dict) -> Path:
    """Persist ``data`` to disk, creating the directory if needed (mode 0600)."""
    p = config_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_toml_dumps(data))
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def get_config_value(key: str) -> str | None:
    return _get_dotted(read_config(), key)


def set_config_value(key: str, value: str) -> Path:
    if key not in _SUPPORTED_KEYS:
        raise ValueError(f"Unknown config key {key!r}. Supported keys: {', '.join(sorted(_SUPPORTED_KEYS))}")
    data = read_config()
    _set_dotted(data, key, value)
    return write_config(data)


# ---------------------------------------------------------------------------
# ``k7 config`` Typer sub-app.
# ---------------------------------------------------------------------------


config_app = typer.Typer(
    help="Read / write per-user CLI configuration.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key in 'section.field' form (e.g. api.url)"),
    value: str = typer.Argument(..., help="Value to store"),
):
    """Set a config value (persists to ~/.config/k7/config.toml)."""
    try:
        path = set_config_value(key, value)
    except ValueError as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(1) from None
    redacted = "<redacted>" if key.endswith(".key") else value
    typer.echo(f"Wrote {key} = {redacted} to {path}")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Config key in 'section.field' form"),
):
    """Print one config value. Exits 1 when the key is unset."""
    value = get_config_value(key)
    if value is None:
        typer.echo("", err=False)
        raise typer.Exit(1)
    typer.echo(value)


@config_app.command("show")
def config_show():
    """Print the full config (API keys are redacted)."""
    data = read_config()
    if not data:
        typer.echo(f"(empty — config file: {config_file_path()})")
        return
    # Redact secrets for terminal display.
    redacted_view: dict = {}
    for section, body in data.items():
        if not isinstance(body, dict):
            continue
        redacted_view[section] = {k: ("<redacted>" if k == "key" else v) for k, v in body.items()}
    sys.stdout.write(_toml_dumps(redacted_view))
