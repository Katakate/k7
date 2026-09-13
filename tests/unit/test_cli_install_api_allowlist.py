"""Unit tests for ``k7 install --api-allow-cidr``.

Everything here is CLI-side: an invalid CIDR, or the flag combined with
``--cni flannel``, must fail *before* Ansible runs.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.models import OperationResult

runner = CliRunner()


def _install(args: list[str]):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = runner.invoke(app, ["--core", "install", *args])
    return result, install


def _extra_vars(install: MagicMock) -> dict:
    return install.call_args.kwargs["extra_vars"]


def test_default_install_sends_an_empty_allowlist():
    result, install = _install([])
    assert result.exit_code == 0, result.output
    assert _extra_vars(install)["k7_api_allow_cidrs"] == ""


def test_repeatable_flag_is_forwarded_comma_joined():
    result, install = _install(["--api-allow-cidr", "203.0.113.4/32", "--api-allow-cidr", "198.51.100.0/24"])
    assert result.exit_code == 0, result.output
    assert _extra_vars(install)["k7_api_allow_cidrs"] == "203.0.113.4/32,198.51.100.0/24"


def test_bare_address_is_accepted_as_a_host_route():
    result, install = _install(["--api-allow-cidr", "203.0.113.4"])
    assert result.exit_code == 0, result.output
    assert _extra_vars(install)["k7_api_allow_cidrs"] == "203.0.113.4"


def test_host_bits_set_are_accepted_strict_false():
    result, install = _install(["--api-allow-cidr", "203.0.113.4/24"])
    assert result.exit_code == 0, result.output


def test_invalid_cidr_fails_before_ansible():
    result, install = _install(["--api-allow-cidr", "203.0.113.0/33"])
    assert result.exit_code != 0
    assert "is not a valid CIDR" in result.output
    install.assert_not_called()


def test_garbage_cidr_fails_before_ansible():
    result, install = _install(["--api-allow-cidr", "not-a-cidr"])
    assert result.exit_code != 0
    assert "is not a valid CIDR" in result.output
    install.assert_not_called()


def test_flannel_is_rejected():
    result, install = _install(["--cni", "flannel", "--api-allow-cidr", "203.0.113.4/32"])
    assert result.exit_code != 0
    assert "--api-allow-cidr requires the Cilium CNI" in result.output
    install.assert_not_called()


def test_flannel_without_the_flag_still_installs():
    result, install = _install(["--cni", "flannel"])
    assert result.exit_code == 0, result.output
    install.assert_called_once()


def test_allow_everything_warns_loudly():
    result, install = _install(["--api-allow-cidr", "0.0.0.0/0"])
    assert result.exit_code == 0, result.output
    assert "allows every source address" in result.output
    assert _extra_vars(install)["k7_api_allow_cidrs"] == "0.0.0.0/0"


def test_specific_cidr_does_not_warn():
    result, _ = _install(["--api-allow-cidr", "203.0.113.4/32"])
    assert result.exit_code == 0, result.output
    assert "allows every source address" not in result.output


_PLAYBOOK = (Path(__file__).resolve().parents[2] / "src/k7/deploy/k7-install-node.yaml").read_text()


def test_playbook_policy_keeps_the_reserved_entities():
    """Dropping `host` from the rendered policy crash-loops the k7-api pod:
    the kubelet liveness/readiness probes come from the node."""
    assert '- fromEntities: ["host", "remote-node", "health", "kube-apiserver"]' in _PLAYBOOK


def test_playbook_only_switches_external_traffic_policy_with_an_allowlist():
    assert "'Local' if k7_api_allow_cidrs_list | length > 0 else 'Cluster'" in _PLAYBOOK
