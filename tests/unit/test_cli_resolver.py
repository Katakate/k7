"""Unit tests for the Spec 10g CLI ↔ API client plumbing.

Coverage:

- ``k7.cli._config`` — TOML round-trip (``set`` / ``get`` / ``show``)
  through the simple hand-rolled parser, including a malformed-file
  fallback that returns ``{}``.
- ``k7.cli._client`` — the four-stage resolution chain for both
  ``--api-url`` and ``--api-key`` (flag → env → config file →
  ``/etc/k7/api_*``), plus the "exit 1 with a helpful message" branch.
- CLI routing — the global ``--core`` flag drops back to ``K7Core``;
  the default behaviour routes through ``k7_sdk.Client``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from k7.cli import _client as cli_client
from k7.cli import _config as cli_config
from k7.cli.k7 import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Config file CRUD.
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_config_dir(tmp_path: Path, monkeypatch):
    """Point ``K7_CONFIG_DIR`` at ``tmp_path`` so tests don't touch ``~``."""
    monkeypatch.setenv(cli_config.CONFIG_DIR_ENV, str(tmp_path))
    yield tmp_path


def test_config_read_returns_empty_dict_when_file_missing(isolated_config_dir):
    assert cli_config.read_config() == {}


def test_config_set_writes_toml_then_get_reads_it_back(isolated_config_dir):
    cli_config.set_config_value("api.url", "https://10.0.0.1:31000")
    cli_config.set_config_value("api.key", "abc123")
    assert cli_config.get_config_value("api.url") == "https://10.0.0.1:31000"
    assert cli_config.get_config_value("api.key") == "abc123"
    # File is mode 0600.
    assert (isolated_config_dir / "config.toml").stat().st_mode & 0o777 == 0o600


def test_config_set_rejects_unknown_key(isolated_config_dir):
    with pytest.raises(ValueError, match="Unknown config key"):
        cli_config.set_config_value("api.token", "x")


def test_config_read_tolerates_malformed_file(isolated_config_dir):
    p = isolated_config_dir / "config.toml"
    p.write_text("this is not [valid toml\n")
    # Should not raise; just return {}.
    assert cli_config.read_config() == {}


def test_config_cli_set_get_show_round_trip(isolated_config_dir):
    set_res = runner.invoke(app, ["config", "set", "api.url", "https://10.0.0.1:31000"])
    assert set_res.exit_code == 0, set_res.output

    get_res = runner.invoke(app, ["config", "get", "api.url"])
    assert get_res.exit_code == 0
    assert get_res.output.strip() == "https://10.0.0.1:31000"

    # Unknown key → exit 1 (Typer prints help on stderr in some versions).
    bad = runner.invoke(app, ["config", "set", "api.bogus", "x"])
    assert bad.exit_code != 0

    # ``config show`` redacts the key.
    runner.invoke(app, ["config", "set", "api.key", "supersecret"])
    show = runner.invoke(app, ["config", "show"])
    assert show.exit_code == 0
    assert "supersecret" not in show.output
    assert "<redacted>" in show.output


# ---------------------------------------------------------------------------
# URL / key resolver — flag > env > config > /etc/k7/*.
# ---------------------------------------------------------------------------


def test_resolve_api_url_prefers_flag_over_env(isolated_config_dir, monkeypatch):
    monkeypatch.setenv("K7_API_URL", "https://env.local:31000")
    assert cli_client._resolve_api_url("https://flag.local:31000") == "https://flag.local:31000"


def test_resolve_api_url_prefers_env_over_config_file(isolated_config_dir, monkeypatch):
    cli_config.set_config_value("api.url", "https://config.local:31000")
    monkeypatch.setenv("K7_API_URL", "https://env.local:31000")
    assert cli_client._resolve_api_url(None) == "https://env.local:31000"


def test_resolve_api_url_prefers_config_over_etc_fallback(isolated_config_dir, monkeypatch, tmp_path):
    cli_config.set_config_value("api.url", "https://config.local:31000")
    fake_etc = tmp_path / "fake_etc_api_endpoint"
    fake_etc.write_text("https://etc.local:31000")
    monkeypatch.delenv("K7_API_URL", raising=False)
    monkeypatch.setattr(cli_client, "_ETC_API_ENDPOINT", fake_etc)
    assert cli_client._resolve_api_url(None) == "https://config.local:31000"


def test_resolve_api_url_falls_back_to_etc_when_nothing_else_set(isolated_config_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("K7_API_URL", raising=False)
    fake_etc = tmp_path / "fake_etc_api_endpoint"
    fake_etc.write_text("https://etc.local:31000\n")
    monkeypatch.setattr(cli_client, "_ETC_API_ENDPOINT", fake_etc)
    assert cli_client._resolve_api_url(None) == "https://etc.local:31000"


def test_resolve_api_url_returns_none_when_nothing_set(isolated_config_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("K7_API_URL", raising=False)
    monkeypatch.setattr(cli_client, "_ETC_API_ENDPOINT", tmp_path / "does_not_exist")
    assert cli_client._resolve_api_url(None) is None


def test_resolve_api_key_chain(isolated_config_dir, monkeypatch, tmp_path):
    # 1) Flag wins.
    assert cli_client._resolve_api_key("flag-key") == "flag-key"
    # 2) Env wins over config.
    cli_config.set_config_value("api.key", "config-key")
    monkeypatch.setenv("K7_API_KEY", "env-key")
    assert cli_client._resolve_api_key(None) == "env-key"
    monkeypatch.delenv("K7_API_KEY")
    # 3) Config wins over /etc fallback.
    fake_keys = tmp_path / "fake_api_keys.json"
    fake_keys.write_text(json.dumps({"hash": {"name": "x", "token": "etc-key"}}))
    monkeypatch.setattr(cli_client, "_ETC_API_KEYS", fake_keys)
    assert cli_client._resolve_api_key(None) == "config-key"


def test_resolve_api_key_etc_fallback_picks_first_token(isolated_config_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("K7_API_KEY", raising=False)
    fake_keys = tmp_path / "fake_api_keys.json"
    fake_keys.write_text(
        json.dumps(
            {
                "h1": {"name": "first", "token": "first-key"},
                "h2": {"name": "second", "token": "second-key"},
            }
        )
    )
    monkeypatch.setattr(cli_client, "_ETC_API_KEYS", fake_keys)
    assert cli_client._resolve_api_key(None) == "first-key"


def test_resolve_client_exits_when_url_missing(isolated_config_dir, monkeypatch):
    monkeypatch.delenv("K7_API_URL", raising=False)
    monkeypatch.delenv("K7_API_KEY", raising=False)
    monkeypatch.setattr(cli_client, "_ETC_API_ENDPOINT", isolated_config_dir / "no_endpoint")
    with pytest.raises(typer.Exit):
        cli_client.resolve_client(None, None)


def test_resolve_client_exits_when_key_missing(isolated_config_dir, monkeypatch):
    monkeypatch.delenv("K7_API_KEY", raising=False)
    monkeypatch.setattr(cli_client, "_ETC_API_KEYS", isolated_config_dir / "no_keys")
    with pytest.raises(typer.Exit):
        cli_client.resolve_client("https://flag.local:31000", None)


# ---------------------------------------------------------------------------
# CLI routing — --core flag vs default API path.
# ---------------------------------------------------------------------------


class TestCliRoutingList:
    def test_default_routes_through_api_client(self, isolated_config_dir):
        fake_client = MagicMock()
        fake_client.list.return_value = [
            {
                "name": "demo",
                "namespace": "default",
                "status": "Running",
                "ready": "True",
                "restarts": 0,
                "age": "1m",
                "image": "alpine:3.20",
                "backend": "kata-qemu-longhorn",
                "error_message": "",
            }
        ]
        with patch("k7.cli._client.resolve_client", return_value=fake_client):
            result = runner.invoke(
                app,
                ["--api-url", "https://x:1", "--api-key", "k", "list"],
            )
        assert result.exit_code == 0, result.output
        fake_client.list.assert_called_once_with(namespace=None)
        # Table renders the sandbox name.
        assert "demo" in result.output

    def test_core_flag_falls_back_to_k7core(self, isolated_config_dir):
        from k7.core.models import SandboxInfo

        fake_core = MagicMock()
        fake_core.list_sandboxes = MagicMock(
            return_value=_awaitable(
                [
                    SandboxInfo(
                        name="byc",
                        namespace="ns",
                        status="Running",
                        ready="True",
                        restarts=0,
                        age="1m",
                        image="alpine",
                        backend="ql",
                    )
                ]
            )
        )
        with (
            patch("k7.cli.k7.K7Core", return_value=fake_core),
            patch("k7.cli._client.resolve_client") as rc,
        ):
            result = runner.invoke(app, ["--core", "list"])
        assert result.exit_code == 0, result.output
        rc.assert_not_called()
        # Rich may truncate long cells; assert on the short fixture name.
        assert "byc" in result.output


class TestCliRoutingDelete:
    def test_default_routes_through_api_client(self, isolated_config_dir):
        fake_client = MagicMock()
        fake_client.delete.return_value = {"message": "Sandbox demo deleted"}
        with patch("k7.cli._client.resolve_client", return_value=fake_client):
            result = runner.invoke(app, ["--api-url", "https://x:1", "--api-key", "k", "delete", "demo"])
        assert result.exit_code == 0, result.output
        fake_client.delete.assert_called_once_with("demo", namespace="default")
        assert "deleted" in result.output.lower()


class TestCliRoutingExec:
    def test_default_routes_through_api_client(self, isolated_config_dir):
        fake_client = MagicMock()
        fake_client.exec.return_value = {"stdout": "hello\n", "stderr": "", "exit_code": 0}
        with patch("k7.cli._client.resolve_client", return_value=fake_client):
            result = runner.invoke(
                app,
                [
                    "--api-url",
                    "https://x:1",
                    "--api-key",
                    "k",
                    "exec",
                    "demo",
                    "echo",
                    "hello",
                ],
            )
        assert result.exit_code == 0, result.output
        fake_client.exec.assert_called_once_with("demo", "echo hello", namespace="default")
        assert "hello" in result.output


class TestCliRoutingLogs:
    def test_default_routes_through_api_client(self, isolated_config_dir):
        fake_client = MagicMock()
        fake_client.logs.return_value = "line1\nline2\n"
        with patch("k7.cli._client.resolve_client", return_value=fake_client):
            result = runner.invoke(
                app,
                ["--api-url", "https://x:1", "--api-key", "k", "logs", "demo", "--tail", "50"],
            )
        assert result.exit_code == 0, result.output
        fake_client.logs.assert_called_once_with("demo", namespace="default", tail=50)
        assert "line1" in result.output


class TestCliRoutingSnapshotList:
    def test_default_routes_through_api_client(self, isolated_config_dir):
        fake_client = MagicMock()
        fake_client.list_snapshots.return_value = [
            {
                "name": "demo-paused-1",
                "namespace": "default",
                "source_sandbox": "demo",
                "size_bytes": 1024 * 1024 * 1024,
                "age": "1m",
                "ready_to_use": True,
                "kind": "pause",
            }
        ]
        with patch("k7.cli._client.resolve_client", return_value=fake_client):
            result = runner.invoke(
                app,
                ["--api-url", "https://x:1", "--api-key", "k", "snapshot", "list"],
            )
        assert result.exit_code == 0, result.output
        fake_client.list_snapshots.assert_called_once_with(
            namespace="default", all_namespaces=False, sandbox=None, kind=None
        )
        assert "demo-paused-1" in result.output


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _awaitable(value):
    """Return a coroutine factory that yields ``value`` once awaited.

    Lets us hand the mocked ``K7Core.list_sandboxes`` an ``asyncio.run``-able
    object (the CLI calls ``asyncio.run(core.list_sandboxes(...))``).
    """

    async def _coro():
        return value

    return _coro()
