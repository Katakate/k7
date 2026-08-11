"""Unit tests for the Spec 10d ``k7 api ...`` sub-app + deprecation aliases.

Coverage:

- ``k7 api status / endpoint / enable / disable`` shell out to the right
  kubectl commands and surface the standard error paths.
- ``k7 dev api rebuild`` is registered (hidden) and routes through the
  rebuild path.
- The deprecated top-level commands (``start-api`` / ``stop-api`` /
  ``api-status`` / ``get-api-endpoint``) emit a stderr warning and
  forward to the new home.
- ``k7 install --no-api`` passes ``k7_api_enabled=false`` into the
  Ansible extra_vars (which is what the playbook actually keys off).
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from k7.cli.k7 import app

runner = CliRunner()


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# ``k7 api enable / disable``.
# ---------------------------------------------------------------------------


class TestApiEnableDisable:
    def test_enable_calls_kubectl_scale_1(self):
        with patch("k7.cli.k7.subprocess.run", return_value=_make_completed()) as run:
            result = runner.invoke(app, ["api", "enable"])
        assert result.exit_code == 0, result.output
        # Find the scale call.
        scale_calls = [c for c in run.call_args_list if "scale" in (c.args[0] if c.args else [])]
        assert scale_calls, "expected at least one kubectl scale call"
        cmd = scale_calls[0].args[0]
        assert "deployment" in cmd
        assert "k7-api" in cmd
        assert "--replicas=1" in cmd
        assert "enabled" in result.output.lower()

    def test_disable_calls_kubectl_scale_0(self):
        with patch("k7.cli.k7.subprocess.run", return_value=_make_completed()) as run:
            result = runner.invoke(app, ["api", "disable"])
        assert result.exit_code == 0, result.output
        scale_calls = [c for c in run.call_args_list if "scale" in (c.args[0] if c.args else [])]
        assert scale_calls
        cmd = scale_calls[0].args[0]
        assert "--replicas=0" in cmd
        assert "disabled" in result.output.lower()

    def test_enable_exits_nonzero_on_kubectl_failure(self):
        with patch(
            "k7.cli.k7.subprocess.run",
            return_value=_make_completed(stderr="not found", returncode=1),
        ):
            result = runner.invoke(app, ["api", "enable"])
        assert result.exit_code != 0
        assert "failed" in result.output.lower()


# ---------------------------------------------------------------------------
# ``k7 api status``.
# ---------------------------------------------------------------------------


class TestApiStatus:
    def test_reports_running_when_replicas_ready(self):
        # subprocess.run is called three times by api_status_cmd:
        # 1) jsonpath get deployment → "1/1"
        # 2) get pods -l app=k7-api → human-readable line
        # 3) _read_api_endpoint -> get svc / get nodes (two calls inside)
        calls = [
            _make_completed(stdout="1/1"),  # deployment readyReplicas/replicas
            _make_completed(stdout="k7-api-abc 1/1 Running 0 1m k7-node-01"),  # pods
            _make_completed(stdout="31007"),  # service nodePort
            _make_completed(stdout='[{"type":"InternalIP","address":"10.0.0.1"}]'),
        ]
        with patch("k7.cli.k7.subprocess.run", side_effect=calls):
            result = runner.invoke(app, ["api", "status"])
        assert result.exit_code == 0, result.output
        assert "running" in result.output.lower()
        assert "1/1" in result.output

    def test_reports_disabled_when_replicas_zero(self):
        calls = [_make_completed(stdout="0/0")]
        # status_cmd may make more subprocess calls (pods/endpoint); pad with no-ops.
        calls.extend(_make_completed(stdout="") for _ in range(5))
        with patch("k7.cli.k7.subprocess.run", side_effect=calls):
            result = runner.invoke(app, ["api", "status"])
        assert result.exit_code == 0, result.output
        assert "disabled" in result.output.lower() or "scaled to 0" in result.output.lower()

    def test_reports_missing_when_deployment_absent(self):
        with patch(
            "k7.cli.k7.subprocess.run",
            return_value=_make_completed(stderr="NotFound", returncode=1),
        ):
            result = runner.invoke(app, ["api", "status"])
        # status reports the problem but doesn't exit 1 — diagnostic only.
        assert result.exit_code == 0
        assert "not found" in result.output.lower()
        assert "k7 install" in result.output


# ---------------------------------------------------------------------------
# ``k7 api endpoint``.
# ---------------------------------------------------------------------------


class TestApiEndpoint:
    def test_prints_url_when_service_exists(self):
        with patch("k7.cli.k7._read_api_endpoint", return_value="http://10.0.0.1:31007"):
            result = runner.invoke(app, ["api", "endpoint"])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "http://10.0.0.1:31007"

    def test_exits_nonzero_when_service_missing(self):
        with patch("k7.cli.k7._read_api_endpoint", return_value=None):
            result = runner.invoke(app, ["api", "endpoint"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Deprecated command aliases.
# ---------------------------------------------------------------------------


class TestDeprecatedAliases:
    def test_api_status_alias_warns_and_forwards(self):
        # Calls subprocess.run several times under the hood; first call returns
        # something that lets api_status_cmd produce output.
        seq = [_make_completed(stdout="1/1")] + [_make_completed(stdout="") for _ in range(5)]
        with patch("k7.cli.k7.subprocess.run", side_effect=seq):
            result = runner.invoke(app, ["api-status"])
        assert result.exit_code == 0
        combined = result.output + (result.stderr or "")
        assert "deprecated" in combined.lower()
        assert "k7 api status" in combined

    def test_get_api_endpoint_alias_warns_and_forwards(self):
        with patch("k7.cli.k7._read_api_endpoint", return_value="http://x:31007"):
            result = runner.invoke(app, ["get-api-endpoint"])
        assert result.exit_code == 0
        combined = result.output + (result.stderr or "")
        assert "deprecated" in combined.lower()
        assert "k7 api endpoint" in combined
        assert "http://x:31007" in result.output

    def test_stop_api_alias_warns_and_forwards_to_disable(self):
        with patch("k7.cli.k7.subprocess.run", return_value=_make_completed()) as run:
            result = runner.invoke(app, ["stop-api"])
        assert result.exit_code == 0
        combined = result.output + (result.stderr or "")
        assert "deprecated" in combined.lower()
        assert "k7 api disable" in combined
        # The scale-to-0 call still went through.
        scale_calls = [c for c in run.call_args_list if "--replicas=0" in (c.args[0] if c.args else [])]
        assert scale_calls

    def test_start_api_alias_warns_and_forwards_to_dev_rebuild(self):
        # Avoid actually building Docker images: stub the build helper.
        with (
            patch("k7.cli.k7._build_and_import_api_image", return_value=False),
            patch("k7.cli.k7.subprocess.run", return_value=_make_completed()),
        ):
            result = runner.invoke(app, ["start-api"])
        combined = result.output + (result.stderr or "")
        assert "deprecated" in combined.lower()
        assert "k7 dev api rebuild" in combined


# ---------------------------------------------------------------------------
# ``k7 install --no-api`` passes the right extra var.
# ---------------------------------------------------------------------------


class TestInstallNoApi:
    def test_install_no_api_passes_k7_api_enabled_false(self):
        seen: dict = {}

        def _capture(*args, **kwargs):
            # ``K7Core.install_node`` is sync (it shells out to ansible-playbook).
            seen["kwargs"] = kwargs
            return SimpleNamespace(success=True, message="ok", error="")

        with (
            patch("k7.cli.k7.K7Core") as core_cls,
            patch("k7.cli.k7._build_default_inventory", return_value="[k7_servers]\nhost ansible_host=1.2.3.4"),
        ):
            core_cls.return_value.install_node = _capture
            result = runner.invoke(app, ["install", "--no-api", "host"])
        assert result.exit_code == 0, result.output
        extra_vars = seen["kwargs"]["extra_vars"]
        assert extra_vars.get("k7_api_enabled") == "false"

    def test_install_without_flag_passes_k7_api_enabled_true(self):
        seen: dict = {}

        def _capture(*args, **kwargs):
            seen["kwargs"] = kwargs
            return SimpleNamespace(success=True, message="ok", error="")

        with (
            patch("k7.cli.k7.K7Core") as core_cls,
            patch("k7.cli.k7._build_default_inventory", return_value="[k7_servers]\nhost ansible_host=1.2.3.4"),
        ):
            core_cls.return_value.install_node = _capture
            result = runner.invoke(app, ["install", "host"])
        assert result.exit_code == 0, result.output
        extra_vars = seen["kwargs"]["extra_vars"]
        assert extra_vars.get("k7_api_enabled") == "true"


# ---------------------------------------------------------------------------
# ``k7 --help`` no longer lists the deprecated commands.
# ---------------------------------------------------------------------------


def test_top_level_help_hides_deprecated_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("start-api", "stop-api", "api-status", "get-api-endpoint"):
        assert name not in result.output, f"deprecated command {name!r} still appears in `k7 --help`"


def test_api_subapp_help_lists_new_commands():
    result = runner.invoke(app, ["api", "--help"])
    assert result.exit_code == 0
    for name in ("status", "endpoint", "enable", "disable"):
        assert name in result.output, f"missing `{name}` under `k7 api --help`"


def test_dev_api_subapp_help_lists_rebuild():
    result = runner.invoke(app, ["dev", "api", "--help"])
    assert result.exit_code == 0
    assert "rebuild" in result.output
