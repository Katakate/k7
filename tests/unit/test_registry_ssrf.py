"""Unit tests for the control-plane registry SSRF guard."""

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7.core.core import K7Core
from k7.core.models import RegistryUnresolvableError, SandboxConfig


def _public_addrinfo(host: str = "1.2.3.4"):
    """Minimal getaddrinfo return shape: one public IPv4."""
    return [(None, None, None, None, (host, 0))]


def _private_addrinfo(host: str = "10.0.0.5"):
    return [(None, None, None, None, (host, 0))]


class TestAssertRegistryHostAllowed:
    def test_rejects_link_local_metadata(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("169.254.169.254")

    def test_rejects_loopback_ip(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("127.0.0.1")

    def test_rejects_localhost(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("localhost")

    def test_rejects_localhost_with_port(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("localhost:5000")

    def test_rejects_rfc1918_10(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("10.0.0.5")

    def test_rejects_rfc1918_192(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("192.168.1.1")

    def test_rejects_rfc1918_172(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("172.16.0.1")

    def test_rejects_ipv6_loopback(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("[::1]")

    def test_rejects_unspecified(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            core._assert_registry_host_allowed("0.0.0.0")

    def test_rejects_hostname_resolving_to_private(self, core: K7Core, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("K7_REGISTRY_ALLOWLIST", "evil.example.com")
        monkeypatch.setattr(
            "k7.core.core.socket.getaddrinfo",
            lambda *a, **k: _private_addrinfo("10.1.2.3"),
        )
        with pytest.raises(ValueError, match="non-public address"):
            core._assert_registry_host_allowed("evil.example.com")

    @pytest.mark.parametrize("host", ["registry-1.docker.io", "ghcr.io", "quay.io"])
    def test_accepts_public_registries(self, core: K7Core, monkeypatch: pytest.MonkeyPatch, host: str):
        monkeypatch.setattr(
            "k7.core.core.socket.getaddrinfo",
            lambda *a, **k: _public_addrinfo("1.1.1.1"),
        )
        core._assert_registry_host_allowed(host)

    def test_dns_failure_is_unresolvable_not_rejected(self, core: K7Core, monkeypatch: pytest.MonkeyPatch):
        def _gaierror(*a, **k):
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

        monkeypatch.setattr("k7.core.core.socket.getaddrinfo", _gaierror)
        with pytest.raises(RegistryUnresolvableError, match="Cannot resolve registry host ghcr.io"):
            core._assert_registry_host_allowed("ghcr.io")

    def test_no_addresses_is_unresolvable(self, core: K7Core, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("k7.core.core.socket.getaddrinfo", lambda *a, **k: [])
        with pytest.raises(RegistryUnresolvableError, match="no addresses"):
            core._assert_registry_host_allowed("ghcr.io")

    def test_rejection_is_not_unresolvable(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed") as exc:
            core._assert_registry_host_allowed("127.0.0.1")
        assert not isinstance(exc.value, RegistryUnresolvableError)


class TestCreateSandboxRegistryDns:
    async def test_unresolvable_registry_is_transient(self, core: K7Core, monkeypatch: pytest.MonkeyPatch):
        def _gaierror(*a, **k):
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

        monkeypatch.setattr("k7.core.core.socket.getaddrinfo", _gaierror)
        result = await core.create_sandbox(SandboxConfig(name="dns-down", image="ghcr.io/org/app:1"))
        assert not result.success
        assert result.transient
        assert "Cannot resolve registry host ghcr.io" in result.error

    async def test_rejected_registry_is_not_transient(self, core: K7Core):
        result = await core.create_sandbox(SandboxConfig(name="ssrf", image="127.0.0.1:5000/x:1"))
        assert not result.success
        assert not result.transient
        assert "not allowed" in result.error


class TestGetRegistryImageConfigSsrf:
    async def test_blocked_host_never_calls_http(self, core: K7Core):
        mock_cls = MagicMock()
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("k7.core.core.httpx.AsyncClient", mock_cls), pytest.raises(ValueError, match="not allowed"):
            await core._get_registry_image_config("127.0.0.1:8199/x:1")

        mock_client.get.assert_not_called()

    async def test_entrypoint_cmd_rethrows_host_rejection(self, core: K7Core):
        with pytest.raises(ValueError, match="not allowed"):
            await core._get_image_entrypoint_cmd("169.254.169.254/meta:1")
