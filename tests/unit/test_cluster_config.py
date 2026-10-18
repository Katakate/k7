"""k7.core.cluster_config: env, then /etc/k7/config, else a loud error; the
egress refusal a chart cluster without Cilium returns at create time."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from k7.core import cluster_config
from k7.core.core import K7Core
from k7.core.models import SandboxConfig

from .test_core_mocked import _setup_clients


def test_env_wins_over_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("# k7\nK7_SYSTEM_NAMESPACE=kube-system\n")
    monkeypatch.setenv("K7_SYSTEM_NAMESPACE", "k7-system")
    assert cluster_config.setting("K7_SYSTEM_NAMESPACE", cfg) == "k7-system"
    monkeypatch.delenv("K7_SYSTEM_NAMESPACE")
    assert cluster_config.setting("K7_SYSTEM_NAMESPACE", cfg) == "kube-system"


def test_missing_setting_is_loud(monkeypatch, tmp_path):
    monkeypatch.delenv("K7_KQL_STORAGE_CLASS", raising=False)
    with pytest.raises(RuntimeError, match="K7_KQL_STORAGE_CLASS is not set"):
        cluster_config.setting("K7_KQL_STORAGE_CLASS", tmp_path / "absent")


def test_malformed_file_is_loud(monkeypatch, tmp_path):
    monkeypatch.delenv("K7_EGRESS_MODE", raising=False)
    cfg = tmp_path / "config"
    cfg.write_text("K7_EGRESS_MODE\n")
    with pytest.raises(RuntimeError, match="malformed line"):
        cluster_config.setting("K7_EGRESS_MODE", cfg)


def test_unknown_egress_mode(monkeypatch):
    monkeypatch.setenv("K7_EGRESS_MODE", "fqdn")
    with pytest.raises(RuntimeError, match="allowed: full, cidr-only, none"):
        cluster_config.egress_mode()


@pytest.mark.parametrize(
    ("mode", "entries", "fqdns", "refused"),
    [
        ("full", ["github.com"], ["github.com"], False),
        ("cidr-only", ["10.0.0.0/8"], [], False),
        ("cidr-only", ["github.com"], ["github.com"], True),
        ("cidr-only", None, [], False),
        ("none", [], [], True),
        ("none", None, [], False),
    ],
)
def test_egress_mode_error(monkeypatch, mode, entries, fqdns, refused):
    monkeypatch.setenv("K7_EGRESS_MODE", mode)
    assert (cluster_config.egress_mode_error(entries, fqdns) is not None) is refused


def test_kubectl_cmd(monkeypatch):
    monkeypatch.setenv("K7_KUBECONFIG", "/etc/rancher/rke2/rke2.yaml")
    assert cluster_config.kubectl_cmd() == ["kubectl", "--kubeconfig=/etc/rancher/rke2/rke2.yaml"]


async def test_create_refuses_fqdn_egress_on_cidr_only(monkeypatch, core: K7Core):
    monkeypatch.setenv("K7_EGRESS_MODE", "cidr-only")
    apps = AsyncMock()
    _setup_clients(core, apps=apps, v1=AsyncMock(), net=AsyncMock())
    result = await core.create_sandbox(SandboxConfig(name="sb1", image="alpine:3.18", egress_whitelist=["github.com"]))
    assert result.success is False
    assert "Domain-based egress (github.com) cannot be enforced" in result.error
    apps.create_namespaced_deployment.assert_not_called()
