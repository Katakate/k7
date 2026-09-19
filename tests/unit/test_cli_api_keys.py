"""Unit tests for API key generation / namespace scoping."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from k7.cli.k7 import app

runner = CliRunner()


class TestGenerateApiKeyNamespaces:
    def test_scoped_namespaces_persisted(self, tmp_path: Path):
        keys_file = tmp_path / "api_keys.json"
        with patch("k7.cli.k7.API_KEYS_FILE", keys_file):
            result = runner.invoke(
                app,
                ["generate-api-key", "scoped", "-n", "a", "--namespace", "b"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(keys_file.read_text())
        assert len(data) == 1
        entry = next(iter(data.values()))
        assert entry["name"] == "scoped"
        assert entry["namespaces"] == ["a", "b"]

    def test_unscoped_key_omits_namespaces_field(self, tmp_path: Path):
        keys_file = tmp_path / "api_keys.json"
        with patch("k7.cli.k7.API_KEYS_FILE", keys_file):
            result = runner.invoke(app, ["generate-api-key", "open"])
        assert result.exit_code == 0, result.output
        data = json.loads(keys_file.read_text())
        entry = next(iter(data.values()))
        assert "namespaces" not in entry
        assert "nodes" not in entry

    def test_scoped_nodes_persisted(self, tmp_path: Path):
        keys_file = tmp_path / "api_keys.json"
        with patch("k7.cli.k7.API_KEYS_FILE", keys_file):
            result = runner.invoke(
                app,
                ["generate-api-key", "pinned", "--node", "k7-node-01", "--node", "k7-node-02"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(keys_file.read_text())
        entry = next(iter(data.values()))
        assert entry["nodes"] == ["k7-node-01", "k7-node-02"]
        assert "Nodes: k7-node-01, k7-node-02" in result.output

    def test_list_shows_namespaces_column(self, tmp_path: Path):
        keys_file = tmp_path / "api_keys.json"
        keys_file.write_text(
            json.dumps(
                {
                    "h1": {
                        "name": "scoped",
                        "created": 1,
                        "expires": 2,
                        "last_used": None,
                        "namespaces": ["alpha"],
                    },
                    "h2": {
                        "name": "open",
                        "created": 1,
                        "expires": 2,
                        "last_used": None,
                    },
                    "h3": {
                        "name": "pinned",
                        "created": 1,
                        "expires": 2,
                        "last_used": None,
                        "nodes": ["k7-node-01"],
                    },
                }
            )
        )
        with patch("k7.cli.k7.API_KEYS_FILE", keys_file):
            result = runner.invoke(app, ["list-api-keys"])
        assert result.exit_code == 0, result.output
        assert "Namespaces" in result.output
        assert "Nodes" in result.output
        assert "alpha" in result.output
        assert "k7-node-01" in result.output
        assert "*" in result.output


class TestNodesDedicateCli:
    def test_dedicate_requires_tenant(self):
        result = runner.invoke(app, ["nodes", "dedicate", "k7-node-01"])
        assert result.exit_code != 0

    def test_dedicate_calls_core(self):
        from k7.core.models import OperationResult

        with patch("k7.cli.k7.K7Core") as core_cls:
            core_cls.return_value.dedicate_node = AsyncMock(return_value=OperationResult(success=True, message="ok"))
            result = runner.invoke(app, ["nodes", "dedicate", "k7-node-01", "--tenant", "acme"])
        assert result.exit_code == 0, result.output
        core_cls.return_value.dedicate_node.assert_awaited_once_with("k7-node-01", "acme")


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
