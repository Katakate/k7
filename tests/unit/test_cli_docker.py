"""CLI tests for --docker / --docker-disk and the --sidecar docker alias."""

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.models import OperationResult

runner = CliRunner()


def _mock_create():
    fake = AsyncMock(return_value=OperationResult(success=True, message="created"))
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


def test_docker_flag_sets_config():
    result, fake = _config_for(["demo", "ubuntu:24.04", "--docker", "--backend", "k7d"])
    assert result.exit_code == 0, result.output
    cfg = fake.await_args.args[0]
    assert cfg.docker is True
    assert cfg.sidecar is None
    assert cfg.docker_disk is None


def test_docker_disk_requires_docker():
    result, _ = _config_for(["demo", "ubuntu:24.04", "--docker-disk", "40Gi", "--backend", "k7d"])
    assert result.exit_code != 0
    assert "docker-disk" in result.output.lower() or "docker-disk" in (result.exception and str(result.exception) or "")


def test_docker_disk_forwarded():
    result, fake = _config_for(["demo", "ubuntu:24.04", "--docker", "--docker-disk", "40Gi", "--backend", "k7d"])
    assert result.exit_code == 0, result.output
    cfg = fake.await_args.args[0]
    assert cfg.docker is True
    assert cfg.docker_disk == "40Gi"


def test_sidecar_docker_alias_warns_and_maps():
    result, fake = _config_for(["demo", "ubuntu:24.04", "--sidecar", "docker", "--backend", "k7d"])
    assert result.exit_code == 0, result.output
    assert "deprecated" in result.output.lower()
    cfg = fake.await_args.args[0]
    assert cfg.docker is True
    assert cfg.sidecar is None


def test_sidecar_docker_alias_on_kata_maps_to_docker():
    result, fake = _config_for(["demo", "ubuntu:24.04", "--sidecar", "docker", "--backend", "kata-qemu-longhorn"])
    assert result.exit_code == 0, result.output
    assert "deprecated" in result.output.lower()
    cfg = fake.await_args.args[0]
    assert cfg.docker is True
    assert cfg.sidecar is None
    assert cfg.backend == "kata-qemu-longhorn"


def test_unknown_sidecar_lists_non_docker_types():
    result, _ = _config_for(["demo", "ubuntu:24.04", "--sidecar", "nosuch"])
    assert result.exit_code != 0
    text = result.output + (str(result.exception) if result.exception else "")
    assert "nosuch" in text
    assert "Available: docker" not in text
    assert "--docker" in text or "no non-docker" in text
