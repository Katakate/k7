"""Unit tests for the ``k7 create`` ingress flags."""

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.models import OperationResult

runner = CliRunner()


def _mock_create():
    fake = AsyncMock(return_value=OperationResult(success=True, message="Sandbox demo created successfully"))
    patcher = patch("k7.cli.k7.K7Core")
    core_cls = patcher.start()
    core_cls.return_value.create_sandbox = fake
    core_cls.return_value.list_sandboxes = AsyncMock(return_value=[])
    return patcher, fake


def _config_for(args: list[str]):
    patcher, fake = _mock_create()
    try:
        result = runner.invoke(app, ["--core", "create", *args])
    finally:
        patcher.stop()
    return result, fake


def test_no_ingress_flags_keeps_deny_all():
    result, fake = _config_for(["demo", "alpine:3.20"])
    assert result.exit_code == 0, result.output
    cfg = fake.await_args.args[0]
    assert cfg.ingress_ports is None
    assert cfg.ingress_from is None


def test_ingress_port_without_sources_defaults_to_same_namespace():
    result, fake = _config_for(["demo", "alpine:3.20", "--ingress-port", "8000"])
    assert result.exit_code == 0, result.output
    cfg = fake.await_args.args[0]
    assert cfg.ingress_ports == [8000]
    assert cfg.ingress_from is None


def test_ingress_from_is_forwarded():
    result, fake = _config_for(["demo", "alpine:3.20", "--ingress-port", "8000", "--ingress-from", "sandbox:alice"])
    assert result.exit_code == 0, result.output
    cfg = fake.await_args.args[0]
    assert cfg.ingress_from == ["sandbox:alice"]


def test_expose_port_is_forwarded():
    result, fake = _config_for(["demo", "alpine:3.20", "--ingress-port", "8000", "--expose-port", "8000"])
    assert result.exit_code == 0, result.output
    assert fake.await_args.args[0].expose_ports == [8000]


def test_expose_to_the_world_warns_on_stderr():
    patcher, _ = _mock_create()
    try:
        result = runner.invoke(
            app,
            [
                "--core",
                "create",
                "demo",
                "alpine:3.20",
                "--ingress-port",
                "8000",
                "--ingress-from",
                "cidr:0.0.0.0/0",
                "--expose-port",
                "8000",
            ],
        )
    finally:
        patcher.stop()
    assert result.exit_code == 0, result.output
    assert "publishes the sandbox to the internet" in result.output


def test_ingress_from_without_port_is_an_error():
    result, fake = _config_for(["demo", "alpine:3.20", "--ingress-from", "sandbox:alice"])
    assert result.exit_code != 0
    assert "--ingress-from requires --ingress-port" in result.output
    fake.assert_not_awaited()


def test_cidr_source_warns_that_it_does_not_scope_in_cluster_peers():
    result, _ = _config_for(["demo", "alpine:3.20", "--ingress-port", "8000", "--ingress-from", "cidr:203.0.113.0/24"])
    assert result.exit_code == 0, result.output
    assert "does not restrict in-cluster peers" in result.output


def test_sandbox_source_does_not_warn():
    result, _ = _config_for(["demo", "alpine:3.20", "--ingress-port", "8000", "--ingress-from", "sandbox:alice"])
    assert result.exit_code == 0, result.output
    assert "does not restrict in-cluster peers" not in result.output
