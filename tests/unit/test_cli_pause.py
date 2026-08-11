"""Unit tests for the ``k7 pause`` CLI command (Spec 10c).

These verify the cleaned-up CLI surface:
- ``k7 pause NAME`` → no snapshot, snapshot_name=None forwarded to Core.
- ``k7 pause NAME --snapshot`` → auto-named snapshot (``{name}-paused-<unix>``).
- ``k7 pause NAME --snapshot=foo`` → snapshot_name="foo".
- ``k7 pause NAME --pvc=...`` → fails fast with "no such option" (the flag was dropped).
"""

import re
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from k7.cli.k7 import _PAUSE_SNAPSHOT_AUTO, _rewrite_bare_snapshot_flag, app
from k7.core.models import OperationResult

runner = CliRunner()


def _mock_pause_returning_success():
    """Patch K7Core() in the CLI module so pause_sandbox is async-callable and tracked."""
    fake = AsyncMock(return_value=OperationResult(success=True, message="Sandbox demo paused (replicas=0)."))
    patcher = patch("k7.cli.k7.K7Core")
    core_cls = patcher.start()
    core_cls.return_value.pause_sandbox = fake
    return patcher, fake


def test_pause_no_snapshot():
    patcher, fake = _mock_pause_returning_success()
    try:
        # ``--core`` forces the K7Core path so the mocked K7Core in
        # ``_mock_pause_returning_success`` is exercised. Spec 10g default
        # routing is through the HTTP API (see test_cli_routing.py).
        result = runner.invoke(app, ["--core", "pause", "demo"])
    finally:
        patcher.stop()
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once_with(name="demo", namespace="default", snapshot_name=None)


def test_pause_auto_snapshot_name():
    patcher, fake = _mock_pause_returning_success()
    try:
        # Bare ``--snapshot`` is rewritten to ``--snapshot=__auto__`` by
        # _rewrite_bare_snapshot_flag at the CLI entry point. CliRunner skips
        # the entry-point wrapper, so we drive it with the sentinel directly.
        result = runner.invoke(app, ["--core", "pause", "demo", f"--snapshot={_PAUSE_SNAPSHOT_AUTO}"])
    finally:
        patcher.stop()
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once()
    kwargs = fake.await_args.kwargs
    assert kwargs["name"] == "demo"
    assert kwargs["namespace"] == "default"
    assert re.fullmatch(r"demo-paused-\d+", kwargs["snapshot_name"]), kwargs["snapshot_name"]


# --- _rewrite_bare_snapshot_flag --------------------------------------------


def test_rewriter_passes_through_when_pause_absent():
    argv = ["k7", "list", "--snapshot"]
    assert _rewrite_bare_snapshot_flag(argv) == argv


def test_rewriter_rewrites_bare_snapshot_at_end():
    out = _rewrite_bare_snapshot_flag(["k7", "pause", "demo", "--snapshot"])
    assert out == ["k7", "pause", "demo", f"--snapshot={_PAUSE_SNAPSHOT_AUTO}"]


def test_rewriter_rewrites_bare_snapshot_before_other_flag():
    out = _rewrite_bare_snapshot_flag(["k7", "pause", "demo", "--snapshot", "-n", "ns"])
    assert out == ["k7", "pause", "demo", f"--snapshot={_PAUSE_SNAPSHOT_AUTO}", "-n", "ns"]


def test_rewriter_preserves_explicit_value_with_equals():
    out = _rewrite_bare_snapshot_flag(["k7", "pause", "demo", "--snapshot=demo-v1"])
    assert out == ["k7", "pause", "demo", "--snapshot=demo-v1"]


def test_rewriter_preserves_space_separated_value():
    out = _rewrite_bare_snapshot_flag(["k7", "pause", "demo", "--snapshot", "demo-v1"])
    assert out == ["k7", "pause", "demo", "--snapshot", "demo-v1"]


def test_pause_explicit_snapshot_name():
    patcher, fake = _mock_pause_returning_success()
    try:
        result = runner.invoke(app, ["--core", "pause", "demo", "--snapshot=demo-v1"])
    finally:
        patcher.stop()
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once_with(name="demo", namespace="default", snapshot_name="demo-v1")


def test_pause_with_namespace():
    patcher, fake = _mock_pause_returning_success()
    try:
        result = runner.invoke(app, ["--core", "pause", "demo", "-n", "my-ns", "--snapshot=v1"])
    finally:
        patcher.stop()
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once_with(name="demo", namespace="my-ns", snapshot_name="v1")


def test_pause_unknown_pvc_flag_rejected():
    """Spec 10c: --pvc was removed. Passing it must fail fast."""
    result = runner.invoke(app, ["pause", "demo", "--pvc=demo-root-lh"])
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "No such option" in combined or "no such option" in combined.lower()


def test_pause_unknown_snapshot_class_flag_rejected():
    """Spec 10c: --snapshot-class was removed. Passing it must fail fast."""
    result = runner.invoke(app, ["pause", "demo", "--snapshot-class=longhorn"])
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "No such option" in combined or "no such option" in combined.lower()


def test_pause_core_failure_propagates_nonzero_exit():
    """When pause_sandbox returns failure, the CLI exits non-zero."""
    fake = AsyncMock(return_value=OperationResult(success=False, error="boom"))
    with patch("k7.cli.k7.K7Core") as core_cls:
        core_cls.return_value.pause_sandbox = fake
        result = runner.invoke(app, ["pause", "demo"])
    assert result.exit_code != 0
