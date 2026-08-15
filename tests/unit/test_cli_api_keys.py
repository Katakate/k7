"""Unit tests for API key generation / namespace scoping (spec 10h)."""

import json
from pathlib import Path
from unittest.mock import patch

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
                }
            )
        )
        with patch("k7.cli.k7.API_KEYS_FILE", keys_file):
            result = runner.invoke(app, ["list-api-keys"])
        assert result.exit_code == 0, result.output
        assert "Namespaces" in result.output
        assert "alpha" in result.output
        assert "*" in result.output
