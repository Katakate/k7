"""``k7 install`` requires --backend; none is the empty set."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from k7.cli.k7 import INSTALL_BACKEND_REQUIRED, app
from k7.core.models import OperationResult

runner = CliRunner()


def _install(args: list[str]):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = runner.invoke(app, ["--core", "install", *args])
    return result, install


def test_install_without_backend_fails():
    result, install = _install([])
    assert result.exit_code != 0
    assert "Specify --backend" in result.output
    assert "none" in result.output
    install.assert_not_called()
    assert "kfd" in INSTALL_BACKEND_REQUIRED


def test_install_backend_none_forwards_sentinel():
    result, install = _install(["--backend", "none"])
    assert result.exit_code == 0, result.output
    extra = install.call_args.kwargs["extra_vars"]
    assert extra["k7_backends"] == "none"
    inv = install.call_args.args[1]
    assert "k7_backends=none" in inv


def test_install_inventory_without_backend_does_not_send_extra_var(tmp_path: Path):
    inv = tmp_path / "inventory.ini"
    inv.write_text("[k7_servers]\nhost ansible_host=1.2.3.4 k7_backends=kql\n")
    result, install = _install(["-i", str(inv)])
    assert result.exit_code == 0, result.output
    extra = install.call_args.kwargs["extra_vars"]
    assert "k7_backends" not in extra


def test_install_none_mixed_fails_before_ansible():
    result, install = _install(["--backend", "none,kql"])
    assert result.exit_code != 0
    assert "cannot be combined" in result.output
    install.assert_not_called()
