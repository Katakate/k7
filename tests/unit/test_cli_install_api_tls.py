"""Unit tests for ``k7 install`` TLS flags and the TLS manifest renderer."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.models import OperationResult
from k7.deploy.k7_api_tls import CADDY_IMAGE, CADDY_PIN, render_caddyfile, render_deployment, render_service

runner = CliRunner()

_PLAYBOOK = (Path(__file__).resolve().parents[2] / "src/k7/deploy/k7-install-node.yaml").read_text()
_HTTP_DEPLOYMENT = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/deployment.yaml").read_text()
_HTTP_SERVICE = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/service.yaml").read_text()
_CLUSTERROLE = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/clusterrole.yaml").read_text()


def _install(args: list[str]):
    if "--backend" not in args and "-b" not in args:
        args = ["--backend", "kql", *args]
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = runner.invoke(app, ["--core", "install", *args])
    return result, install


def _extra_vars(install: MagicMock) -> dict:
    return install.call_args.kwargs["extra_vars"]


def test_default_install_is_https():
    result, install = _install([])
    assert result.exit_code == 0, result.output
    ev = _extra_vars(install)
    assert ev["k7_api_insecure_http"] == "false"
    assert ev["k7_api_hostname"] == ""
    assert ev["k7_api_tls_cert_src"] == ""
    assert ev["k7_api_tls_key_src"] == ""


def test_insecure_http_is_forwarded():
    result, install = _install(["--api-insecure-http"])
    assert result.exit_code == 0, result.output
    assert _extra_vars(install)["k7_api_insecure_http"] == "true"


def test_insecure_http_with_allowlist_warns():
    result, install = _install(["--api-insecure-http", "--api-allow-cidr", "203.0.113.4/32"])
    assert result.exit_code == 0, result.output
    assert "keys still travel in cleartext" in result.output
    ev = _extra_vars(install)
    assert ev["k7_api_insecure_http"] == "true"
    assert ev["k7_api_allow_cidrs"] == "203.0.113.4/32"


def test_hostname_and_insecure_http_fail_before_ansible():
    result, install = _install(["--api-insecure-http", "--api-hostname", "api.example.com"])
    assert result.exit_code != 0
    assert "--api-insecure-http cannot be combined" in result.output
    install.assert_not_called()


def test_hostname_and_operator_cert_fail_before_ansible(tmp_path: Path):
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    cert.write_text("x")
    key.write_text("y")
    result, install = _install(
        ["--api-hostname", "api.example.com", "--api-tls-cert", str(cert), "--api-tls-key", str(key)]
    )
    assert result.exit_code != 0
    assert "--api-hostname cannot be combined" in result.output
    install.assert_not_called()


def test_hostname_must_be_a_dns_name():
    result, install = _install(["--api-hostname", "203.0.113.10"])
    assert result.exit_code != 0
    assert "must be a DNS name" in result.output
    install.assert_not_called()


def test_cert_without_key_fails():
    result, install = _install(["--api-tls-cert", "/tmp/server.crt"])
    assert result.exit_code != 0
    assert "must be passed together" in result.output
    install.assert_not_called()


def test_mismatched_cert_key_fails_before_ansible(tmp_path: Path):
    cert = tmp_path / "a.crt"
    key = tmp_path / "b.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=a",
            "-keyout",
            str(tmp_path / "a.key"),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=b",
            "-keyout",
            str(key),
            "-out",
            str(tmp_path / "b.crt"),
        ],
        check=True,
        capture_output=True,
    )
    result, install = _install(["--api-tls-cert", str(cert), "--api-tls-key", str(key)])
    assert result.exit_code != 0
    assert "not a matching pair" in result.output
    install.assert_not_called()


def test_matching_cert_key_is_forwarded(tmp_path: Path):
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=k7-api",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    result, install = _install(["--api-tls-cert", str(cert), "--api-tls-key", str(key)])
    assert result.exit_code == 0, result.output
    ev = _extra_vars(install)
    assert ev["k7_api_tls_cert_src"] == str(cert.resolve())
    assert ev["k7_api_tls_key_src"] == str(key.resolve())


def test_acme_staging_requires_hostname():
    result, install = _install(["--api-acme-staging"])
    assert result.exit_code != 0
    assert "--api-acme-staging requires --api-hostname" in result.output
    install.assert_not_called()


def test_renderer_adds_caddy_and_hides_ca_key():
    out = render_deployment(_HTTP_DEPLOYMENT, acme=False)
    assert "name: caddy" in out
    assert CADDY_IMAGE in out
    assert "secretName: k7-api-tls" in out
    assert "mountPath: /etc/k7/tls" in out
    assert "name: tls-hide" in out
    assert "path: /health" in out
    assert "port: 8000" in out
    assert "ca.key" not in out
    assert "hostPort: 80" not in out


def test_renderer_acme_exposes_host_port_80_and_no_tls_secret():
    out = render_deployment(_HTTP_DEPLOYMENT, acme=True)
    assert "hostPort: 80" in out
    assert "secretName: k7-api-tls" not in out


def test_renderer_service_targets_8443():
    assert "targetPort: 8443" in render_service(_HTTP_SERVICE)
    assert "nodePort: 31007" in render_service(_HTTP_SERVICE)


def test_caddyfile_files_mode_disables_auto_https():
    text = render_caddyfile(acme=False)
    assert "auto_https off" in text
    assert "tls /certs/tls.crt /certs/tls.key" in text
    assert "reverse_proxy 127.0.0.1:8000" in text


def test_caddyfile_acme_uses_hostname_and_optional_staging():
    text = render_caddyfile(acme=True, hostname="api.example.com", staging=True)
    assert "api.example.com {" in text
    assert "https_port 8443" in text
    assert "http_port 80" in text
    assert "acme-staging-v02" in text
    with pytest.raises(ValueError, match="hostname"):
        render_caddyfile(acme=True, hostname="")


def test_playbook_caddy_pin_matches_the_renderer():
    assert f'k7_caddy_image: "{CADDY_PIN}"' in _PLAYBOOK
    assert CADDY_IMAGE in render_deployment(_HTTP_DEPLOYMENT, acme=False)


def test_playbook_keeps_the_20d_reserved_entities():
    assert '- fromEntities: ["host", "remote-node", "health", "kube-apiserver"]' in _PLAYBOOK


def test_playbook_does_not_enable_a_host_firewall():
    assert "enable-host-firewall" not in _PLAYBOOK
    assert "CiliumHostFirewall" not in _PLAYBOOK


def test_clusterrole_can_manage_expose_services():
    # Delete always removes `{name}-expose`. Without this verb the API
    # gets 403 (not 404) and every sandbox delete through k7-api is 400.
    assert '"services"' in _CLUSTERROLE
