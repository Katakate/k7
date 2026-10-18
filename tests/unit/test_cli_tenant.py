"""CLI: `k7 tenant`, `generate-api-key --tenant`, `install --api-mode`, `create --egress-bps`."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.models import OperationResult
from k7.deploy import k7_api_keys as api_keys_store
from tests.unit.test_cli_api_keys import _FakeKubectl, _keys, fake_secrets  # noqa: F401

runner = CliRunner()
ACME = {"metadata": {"name": "acme"}, "spec": {"namespaces": ["acme-dev", "acme-prod"]}}


def _invoke(args, **patches):
    with patch("k7.cli.k7.K7Core"):
        ctx = [patch(f"k7.cli.k7.tenancy.{name}", new=mock) for name, mock in patches.items()]
        for c in ctx:
            c.start()
        try:
            return runner.invoke(app, args)
        finally:
            for c in ctx:
                c.stop()


def test_tenant_create_builds_the_spec():
    create = AsyncMock()
    result = _invoke(
        [
            "tenant",
            "create",
            "acme",
            "-n",
            "acme-dev",
            "-n",
            "acme-prod",
            "--node",
            "n1",
            "--default-size",
            "cpu=1,memory=1Gi",
            "--quota",
            "sandboxes=10,vcpu=8",
            "--quota",
            "memory=16Gi",
            "--max-size",
            "cpu=4",
            "--network",
            "egressBps=100M",
        ],
        create_tenant=create,
    )
    assert result.exit_code == 0, result.output
    _core, name, spec = create.await_args.args
    assert name == "acme"
    assert spec == {
        "namespaces": ["acme-dev", "acme-prod"],
        "nodes": ["n1"],
        "defaultSize": {"cpu": "1", "memory": "1Gi"},
        "quotas": {"sandboxes": 10, "vcpu": "8", "memory": "16Gi"},
        "limits": {"maxSandboxSize": {"cpu": "4"}, "network": {"egressBps": "100M"}},
    }


def test_tenant_create_conflict_exits_1_with_reason():
    result = _invoke(
        ["tenant", "create", "acme", "-n", "globex"],
        create_tenant=AsyncMock(side_effect=ValueError("namespace(s) globex already belong to tenant globex")),
    )
    assert result.exit_code == 1 and "already belong to tenant globex" in result.output


@pytest.mark.parametrize("bad", ["sandboxes", "=3", "sandboxes="])
def test_tenant_create_rejects_malformed_kv(bad):
    result = _invoke(["tenant", "create", "acme", "-n", "a", "--quota", bad], create_tenant=AsyncMock())
    assert result.exit_code != 0 and "KEY=VALUE" in result.output


def test_tenant_update_clear_and_replace():
    update = AsyncMock()
    result = _invoke(["tenant", "update", "acme", "--quota", "sandboxes=3", "--clear", "limits"], update_tenant=update)
    assert result.exit_code == 0, result.output
    assert update.await_args.args[2] == {"quotas": {"sandboxes": 3}, "limits": None}
    assert _invoke(["tenant", "update", "acme"], update_tenant=update).exit_code != 0
    assert _invoke(["tenant", "update", "acme", "--clear", "namespaces"], update_tenant=update).exit_code != 0


def test_tenant_delete_reports_each_object():
    deleted = ["snapshot acme-dev/s1", "sandbox acme-dev/sb1", "tenant acme"]
    delete = AsyncMock(return_value=deleted)
    result = _invoke(["tenant", "delete", "acme", "--cascade"], delete_tenant=delete)
    assert result.exit_code == 0, result.output
    assert delete.await_args.args[1:] == ("acme", True)
    for item in deleted:
        assert f"deleted {item}" in result.output


def test_tenant_delete_without_cascade_refuses():
    refusal = ValueError("tenant acme still has sandbox acme-dev/sb1; delete them or pass --cascade")
    result = _invoke(["tenant", "delete", "acme"], delete_tenant=AsyncMock(side_effect=refusal))
    assert result.exit_code == 1 and "--cascade" in result.output


def test_tenant_get_missing_is_exit_1():
    assert _invoke(["tenant", "get", "nope"], get_tenant=AsyncMock(return_value=None)).exit_code == 1


def test_tenant_adopt_dry_run_then_apply(fake_secrets, monkeypatch):  # noqa: F811
    monkeypatch.setattr(_FakeKubectl, "tenants", {"team-a": ["team-a"]})
    fake_secrets["k7-api-keys"] = {
        "aaaaaaaa": '{"name": "ci", "hash": "h", "role": "user", "namespaces": ["team-a"]}',
        "bbbbbbbb": '{"name": "open", "hash": "h", "role": "admin", "unscoped_legacy": true}',
        "cccccccc": '{"name": "root", "hash": "h", "role": "admin"}',
    }
    create = AsyncMock()
    dry = _invoke(["tenant", "adopt", "--from-keys"], list_tenants=AsyncMock(return_value=[]), create_tenant=create)
    assert dry.exit_code == 0, dry.output
    assert "tenant team-a: namespaces team-a; keys aaaaaaaa" in dry.output and "dry run" in dry.output
    assert "bbbbbbbb" in dry.output and "unscoped" in dry.output and "cccccccc" not in dry.output
    create.assert_not_awaited()
    assert "tenant" not in _keys(fake_secrets)["aaaaaaaa"]
    applied = _invoke(
        ["tenant", "adopt", "--from-keys", "--apply"], list_tenants=AsyncMock(return_value=[]), create_tenant=create
    )
    assert applied.exit_code == 0, applied.output
    assert create.await_args.args[1:] == ("team-a", {"namespaces": ["team-a"]})
    keys = _keys(fake_secrets)
    assert keys["aaaaaaaa"] == {"name": "ci", "hash": "h", "role": "user", "namespaces": ["team-a"], "tenant": "team-a"}
    assert "tenant" not in keys["bbbbbbbb"] and "tenant" not in keys["cccccccc"]


def test_bind_tenant_makes_keys_tenant_users(fake_secrets):  # noqa: F811
    fake_secrets["k7-api-keys"] = {
        "aaaaaaaa": '{"name": "a", "hash": "h", "role": "read-only", "namespaces": ["a"]}',
        "bbbbbbbb": '{"name": "b", "hash": "h", "admin": true, "unscoped_legacy": true, "namespaces": ["a"]}',
    }
    api_keys_store.bind_tenant(api_keys_store.Kubectl(["kubectl"]), ["aaaaaaaa", "bbbbbbbb"], "acme")
    keys = _keys(fake_secrets)
    assert (keys["aaaaaaaa"]["role"], keys["aaaaaaaa"]["tenant"]) == ("read-only", "acme")
    assert keys["bbbbbbbb"] == {"name": "b", "hash": "h", "namespaces": ["a"], "role": "user", "tenant": "acme"}
    with pytest.raises(RuntimeError, match="does not exist"):
        api_keys_store.bind_tenant(api_keys_store.Kubectl(["kubectl"]), ["aaaaaaaa"], "gone")


def test_bind_tenant_refuses_unknown_key(fake_secrets):  # noqa: F811
    fake_secrets["k7-api-keys"] = {}
    with pytest.raises(RuntimeError, match="not found"):
        api_keys_store.bind_tenant(api_keys_store.Kubectl(["kubectl"]), ["zzzzzzzz"], "acme")


# ── generate-api-key --tenant ───────────────────────────────────────


def test_generate_tenant_key_is_stored_bound(fake_secrets):  # noqa: F811
    result = runner.invoke(app, ["generate-api-key", "bot", "--role", "user", "--tenant", "acme", "-n", "b"])
    assert result.exit_code == 0, result.output
    entry = next(iter(_keys(fake_secrets).values()))
    assert (entry["role"], entry["tenant"], entry["namespaces"]) == ("user", "acme", ["b"])
    assert "Role: user on tenant acme" in result.output


def test_generate_key_for_missing_tenant_fails(fake_secrets):  # noqa: F811
    result = runner.invoke(app, ["generate-api-key", "bot", "--role", "user", "--tenant", "gone"])
    assert result.exit_code == 1 and "does not exist" in result.output
    assert not fake_secrets.get("k7-api-keys")


def test_generate_key_namespace_outside_tenant_fails(fake_secrets):  # noqa: F811
    result = runner.invoke(app, ["generate-api-key", "bot", "--role", "user", "--tenant", "acme", "-n", "globex"])
    assert result.exit_code == 1 and "globex" in result.output and "never reach" in result.output
    assert not fake_secrets.get("k7-api-keys")


def test_no_unscoped_key_can_be_generated(fake_secrets, monkeypatch):  # noqa: F811
    monkeypatch.setenv("K7_API_MODE", "production")
    result = runner.invoke(app, ["generate-api-key", "open", "--role", "user", "-n", "a"])
    assert result.exit_code != 0 and "tenant" in result.output
    assert runner.invoke(app, ["generate-api-key", "root", "--role", "admin"]).exit_code == 0
    assert not any(api_keys_store.unscoped_keys(_keys(fake_secrets)))


def test_list_api_keys_shows_tenant(fake_secrets):  # noqa: F811
    runner.invoke(app, ["generate-api-key", "bot", "--role", "user", "--tenant", "acme"])
    result = runner.invoke(app, ["list-api-keys"])
    assert result.exit_code == 0 and "acme" in result.output


# ── install --api-mode ──────────────────────────────────────────────


def _install(args):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = runner.invoke(app, ["--core", "install", "--backend", "kfd", *args])
    return result, install


def test_install_without_api_mode_fails_before_ansible():
    result, install = _install([])
    assert result.exit_code != 0 and "--api-mode is required" in result.output
    install.assert_not_called()


@pytest.mark.parametrize("mode", ["production", "development"])
def test_install_forwards_api_mode(mode):
    result, install = _install(["--api-mode", mode, "--metering-sample-interval", "10"])
    assert result.exit_code == 0, result.output
    assert install.call_args.kwargs["extra_vars"]["k7_api_mode"] == mode


def test_install_production_refuses_insecure_http():
    result, install = _install(["--api-mode", "production", "--api-insecure-http"])
    assert result.exit_code != 0 and "insecure" in result.output
    install.assert_not_called()


def test_install_without_api_needs_no_mode():
    result, install = _install(["--no-api"])
    assert result.exit_code == 0, result.output
    assert "k7_api_mode" not in install.call_args.kwargs["extra_vars"]


def test_playbook_validates_api_mode():
    from tests.unit.conftest import install_tree_text

    text = install_tree_text()
    assert "k7_api_mode" in text and "production" in text
    assert "K7_API_MODE={{ k7_api_mode }}" in text


# ── create --egress-bps / --ingress-bps ─────────────────────────────


def test_create_network_flags_set_network_limits():
    with patch("k7.cli.k7.K7Core") as core_cls:
        core_cls.return_value = AsyncMock()
        core_cls.return_value.create_sandbox = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        result = runner.invoke(
            app, ["--core", "create", "sb", "alpine:3.20", "--egress-bps", "100M", "--ingress-bps", "50M"]
        )
    assert result.exit_code == 0, result.output
    cfg = core_cls.return_value.create_sandbox.await_args.args[0]
    assert cfg.network_limits == {"egressBps": "100M", "ingressBps": "50M"}
