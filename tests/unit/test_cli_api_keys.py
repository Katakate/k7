"""Unit tests for API key generation / namespace scoping."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.deploy import k7_api_keys as api_keys_store

runner = CliRunner()


class _FakeKubectl:
    """In-memory stand-in for k7_api_keys.Kubectl (Secret name → data)."""

    secrets: dict[str, dict[str, str]] = {}
    tenants: dict[str, list[str]] = {"acme": ["a", "b"]}

    def __init__(self, argv, namespace="kube-system"):
        self.namespace = namespace

    def read(self, name):
        return dict(self.secrets[name]) if name in self.secrets else None

    def ensure(self, name):
        self.secrets.setdefault(name, {})

    def require_tenant(self, tenant):
        if tenant not in self.tenants:
            raise RuntimeError(f"tenant {tenant!r} does not exist")
        return self.tenants[tenant]

    def patch(self, name, data):
        for k, v in data.items():
            if v is None:
                self.secrets[name].pop(k, None)
            else:
                self.secrets[name][k] = v


@pytest.fixture()
def fake_secrets(tmp_path: Path, monkeypatch):
    _FakeKubectl.secrets = {}
    monkeypatch.setattr(api_keys_store, "Kubectl", _FakeKubectl)
    monkeypatch.setattr(api_keys_store, "LEGACY_FILE", tmp_path / "api_keys.json")
    return _FakeKubectl.secrets


def _keys(secrets: dict) -> dict:
    return api_keys_store.decode_keys(secrets["k7-api-keys"])


class TestGenerateApiKey:
    def test_scoped_namespaces_persisted(self, fake_secrets):
        result = runner.invoke(
            app, ["generate-api-key", "scoped", "--role", "user", "--tenant", "acme", "-n", "a", "--namespace", "b"]
        )
        assert result.exit_code == 0, result.output
        ((key_id, entry),) = _keys(fake_secrets).items()
        assert (entry["name"], entry["role"], entry["tenant"]) == ("scoped", "user", "acme")
        assert entry["namespaces"] == ["a", "b"]
        token = next(line.split(": ", 1)[1] for line in result.output.splitlines() if line.startswith("API Key:"))
        assert api_keys_store.match_token(_keys(fake_secrets), token) == (key_id, entry)
        assert entry["hash"] not in result.output
        assert "Role: user on tenant acme" in result.output

    @pytest.mark.parametrize("role", ["operator", "admin"])
    def test_cluster_role_omits_scope_fields(self, fake_secrets, role):
        result = runner.invoke(app, ["generate-api-key", "open", "--role", role])
        assert result.exit_code == 0, result.output
        entry = next(iter(_keys(fake_secrets).values()))
        assert entry["role"] == role
        assert not {"namespaces", "nodes", "admin", "tenant"} & set(entry)

    @pytest.mark.parametrize(
        ("argv", "match"),
        [
            ([], "--role"),
            (["--role", "root"], "role must be one of"),
            (["--role", "user"], "tenant-scoped"),
            (["--role", "read-only"], "tenant-scoped"),
            (["--role", "admin", "--tenant", "acme"], "cluster-scoped"),
            (["--role", "operator", "--tenant", "acme"], "cluster-scoped"),
            (["--role", "user", "--tenant", "ghost"], "does not exist"),
            (["--admin"], "No such option"),
        ],
    )
    def test_refused_scopes_write_nothing(self, fake_secrets, argv, match):
        result = runner.invoke(app, ["generate-api-key", "x", *argv])
        assert result.exit_code != 0
        assert match in result.output
        assert not fake_secrets.get("k7-api-keys")

    def test_scoped_nodes_persisted(self, fake_secrets):
        result = runner.invoke(
            app, ["generate-api-key", "pinned", "--role", "admin", "--node", "k7-node-01", "--node", "k7-node-02"]
        )
        assert result.exit_code == 0, result.output
        assert next(iter(_keys(fake_secrets).values()))["nodes"] == ["k7-node-01", "k7-node-02"]
        assert "Nodes: k7-node-01, k7-node-02" in result.output

    def test_refuses_while_legacy_file_unmigrated(self, fake_secrets, tmp_path: Path):
        (tmp_path / "api_keys.json").write_text("{}")
        result = runner.invoke(app, ["generate-api-key", "x", "--role", "admin"])
        assert result.exit_code != 0
        assert "k7 api keys migrate" in result.output

    def test_list_flags_legacy_and_shows_scopes(self, fake_secrets):
        fake_secrets["k7-api-keys"] = {
            "legacy-abcd1234": json.dumps({"name": "old", "hash": "h", "created": 1, "expires": 2}),
            "q3vxk2ab": json.dumps(
                {
                    "name": "pinned",
                    "hash": "h2",
                    "created": 1,
                    "expires": 2,
                    "nodes": ["k7-node-01"],
                    "namespaces": ["alpha"],
                }
            ),
        }
        fake_secrets["k7-api-keys-usage"] = {"q3vxk2ab": "100"}
        with patch("k7.cli.k7.Console", lambda: Console(width=200, height=50)):
            result = runner.invoke(app, ["list-api-keys"])
        assert result.exit_code == 0, result.output
        for needle in ("1 legacy key(s) (legacy-abcd1234)", "alpha", "k7-node-01", "Namespaces", "Nodes", "*"):
            assert needle in result.output

    def test_revoke_removes_all_with_name(self, fake_secrets):
        fake_secrets["k7-api-keys"] = {
            "aaaaaaaa": json.dumps({"name": "dup", "hash": "1"}),
            "bbbbbbbb": json.dumps({"name": "dup", "hash": "2"}),
            "cccccccc": json.dumps({"name": "keep", "hash": "3"}),
        }
        result = runner.invoke(app, ["revoke-api-key", "dup"])
        assert result.exit_code == 0, result.output
        assert list(fake_secrets["k7-api-keys"]) == ["cccccccc"]


class TestMigrate:
    LEGACY = {"f" * 64: {"name": "old", "created": 1, "expires": 9_999_999_999, "last_used": 42}}

    def test_copies_verifies_renames_and_is_idempotent(self, fake_secrets, tmp_path: Path):
        legacy = tmp_path / "api_keys.json"
        legacy.write_text(json.dumps(self.LEGACY))
        result = runner.invoke(app, ["api", "keys", "migrate"])
        assert result.exit_code == 0, result.output
        assert not legacy.exists() and (tmp_path / "api_keys.json.migrated").exists()
        entry = _keys(fake_secrets)["legacy-ffffffff"]
        assert entry["hash"] == "f" * 64 and "last_used" not in entry
        assert fake_secrets["k7-api-keys-usage"] == {"legacy-ffffffff": "42"}
        again = runner.invoke(app, ["api", "keys", "migrate"])
        assert again.exit_code == 0, again.output
        assert "legacy-ffffffff" in _keys(fake_secrets)

    def test_creates_empty_secrets_without_file(self, fake_secrets):
        result = runner.invoke(app, ["api", "keys", "migrate"])
        assert result.exit_code == 0, result.output
        assert fake_secrets == {"k7-api-keys-usage": {}, "k7-api-keys": {}}

    def test_conflicting_secret_and_file_fails_with_both_paths(self, fake_secrets, tmp_path: Path):
        legacy = tmp_path / "api_keys.json"
        legacy.write_text(json.dumps(self.LEGACY))
        fake_secrets["k7-api-keys"] = {"zzzzzzzz": json.dumps({"name": "new", "hash": "x"})}
        result = runner.invoke(app, ["api", "keys", "migrate"])
        assert result.exit_code != 0
        assert str(legacy) in result.output and "kube-system/k7-api-keys" in result.output
        assert legacy.exists()


class TestNodesDedicateCli:
    def test_dedicate_requires_tenant(self):
        result = runner.invoke(app, ["nodes", "dedicate", "k7-node-01"])
        assert result.exit_code != 0

    def test_dedicate_writes_the_tenant(self):
        with patch("k7.cli.k7.K7Core") as core_cls, patch("k7.cli.k7.tenancy.dedicate", new=AsyncMock()) as dedicate:
            result = runner.invoke(app, ["nodes", "dedicate", "k7-node-01", "--tenant", "acme"])
        assert result.exit_code == 0, result.output
        dedicate.assert_awaited_once_with(core_cls.return_value, "k7-node-01", "acme")

    def test_dedicate_to_missing_tenant_fails(self):
        with (
            patch("k7.cli.k7.K7Core"),
            patch("k7.cli.k7.tenancy.dedicate", new=AsyncMock(side_effect=LookupError("tenant acme does not exist"))),
        ):
            result = runner.invoke(app, ["nodes", "dedicate", "k7-node-01", "--tenant", "acme"])
        assert result.exit_code == 1
        assert "tenant acme does not exist" in result.output


class TestNodesListCli:
    def test_json_prints_k3s_names(self):
        rows = [
            {
                "name": "k7-node-01",
                "hostname": "k7-node-01",
                "backends": ["k7d"],
                "tenant": "",
            }
        ]
        with patch("k7.cli.k7.K7Core") as core_cls:
            core_cls.return_value.list_cluster_nodes = AsyncMock(return_value=rows)
            result = runner.invoke(app, ["nodes", "list", "--json"])
        assert result.exit_code == 0, result.output
        assert "k7-node-01" in result.output
        assert "k7d" in result.output
