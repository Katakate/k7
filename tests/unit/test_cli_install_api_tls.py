"""Unit tests for ``k7 install`` TLS flags and the TLS manifest renderer."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from k7.cli.k7 import app
from k7.core.models import OperationResult
from k7.deploy.k7_api_tls import (
    CADDY_IMAGE,
    CADDY_PIN,
    render_caddyfile,
    render_deployment,
    render_pdb,
    render_service,
)
from k7.deploy.k7_api_tls import main as tls_main
from tests.unit.conftest import install_tree_text

runner = CliRunner()

_PLAYBOOK = install_tree_text()
_HTTP_DEPLOYMENT = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/deployment.yaml").read_text()
_HTTP_SERVICE = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/service.yaml").read_text()
_CLUSTERROLE = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/clusterrole.yaml").read_text()


def _install(args: list[str]):
    if "--backend" not in args and "-b" not in args:
        args = ["--backend", "kql", *args]
    if "--api-mode" not in args:
        args = ["--api-mode", "development", *args]
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


def test_renderer_files_mode_has_no_host_paths_and_stays_ha():
    out = render_deployment(_HTTP_DEPLOYMENT, acme=False, caddyfile="x")
    assert "name: caddy" in out
    assert CADDY_IMAGE in out
    assert "secretName: k7-api-tls" in out
    assert "name: k7-api-caddyfile" in out
    assert "path: /health" in out
    assert "port: 8000" in out
    assert "ca.key" not in out
    assert "hostPort: 80" not in out
    assert "hostPath" not in out
    assert "replicas: 2" in out
    assert 'first-master: "true"\n' not in out.split("affinity:")[0]


def test_renderer_acme_pins_one_replica_to_first_master():
    out = render_deployment(_HTTP_DEPLOYMENT, acme=True, caddyfile="x")
    assert "hostPort: 80" in out
    assert "secretName: k7-api-tls" not in out
    assert "replicas: 1" in out
    assert 'nodeSelector:\n        k7.katakate.org/first-master: "true"' in out
    assert "path: /etc/k7/caddy" in out


def test_caddyfile_change_rolls_pods():
    a = render_deployment(_HTTP_DEPLOYMENT, acme=False, caddyfile="a")
    b = render_deployment(_HTTP_DEPLOYMENT, acme=False, caddyfile="b")
    assert "k7.katakate.org/caddyfile-sha256" in a and a != b


@pytest.mark.parametrize(("acme", "source", "sni"), [(False, "files", ""), (True, "acme", "api.example.com")])
def test_renderer_tells_k7_api_its_cert_source(acme: bool, source: str, sni: str):
    out = yaml.safe_load(render_deployment(_HTTP_DEPLOYMENT, acme=acme, caddyfile="x", hostname="api.example.com"))
    pod = out["spec"]["template"]
    assert set(pod["metadata"]["annotations"]) >= {"prometheus.io/scrape", "k7.katakate.org/caddyfile-sha256"}
    api = next(c for c in pod["spec"]["containers"] if c["name"] == "k7-api")
    env = {e["name"]: e.get("value") for e in api["env"]}
    assert (env["K7_TLS_CERT_SOURCE"], env["K7_TLS_SNI"]) == (source, sni)
    caddy = next(c for c in pod["spec"]["containers"] if c["name"] == "caddy")
    assert "K7_TLS_CERT_SOURCE" not in str(caddy)


def test_acme_pdb_does_not_block_drains():
    pdb = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/pdb.yaml").read_text()
    assert "minAvailable: 1" in pdb
    assert "maxUnavailable: 1" in render_pdb(pdb) and "minAvailable" not in render_pdb(pdb)


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


def test_caddy_is_pulled_by_digest_not_imported_through_docker():
    for acme in (False, True):
        out = render_deployment(_HTTP_DEPLOYMENT, acme=acme)
        assert f"image: {CADDY_PIN}\n          imagePullPolicy: IfNotPresent" in out
    assert "docker pull" not in _PLAYBOOK
    assert (
        "caddy" not in _PLAYBOOK.split("Overlay the TLS Deployment", 1)[0].split("Create or update the k7-api TLS")[1]
    )


def test_playbook_keeps_the_20d_reserved_entities():
    assert '- fromEntities: ["host", "remote-node", "health", "kube-apiserver"]' in _PLAYBOOK


def test_playbook_does_not_enable_a_host_firewall():
    assert "enable-host-firewall" not in _PLAYBOOK
    assert "CiliumHostFirewall" not in _PLAYBOOK


def test_clusterrole_can_manage_expose_services():
    # Delete always removes `{name}-expose`. Without this verb the API
    # gets 403 (not 404) and every sandbox delete through k7-api is 400.
    assert '"services"' in _CLUSTERROLE


@pytest.mark.parametrize("acme", [False, True])
def test_render_keeps_one_annotations_block_with_oidc_and_caddyfile_hashes(acme):
    src = (Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api/deployment.yaml").read_text()
    out = render_deployment(src, acme=acme, caddyfile="x")
    assert out.count("      annotations:\n") == 1
    annotations = yaml.safe_load(out)["spec"]["template"]["metadata"]["annotations"]
    assert set(annotations) == {
        "k7.katakate.org/oidc-config",
        "k7.katakate.org/metering-config",
        "k7.katakate.org/caddyfile-sha256",
        "prometheus.io/scrape",
        "prometheus.io/port",
        "prometheus.io/path",
    }


def _manifest_dir(tmp_path: Path) -> Path:
    src = Path(__file__).resolve().parents[2] / "src/k7/deploy/manifests/k7-api"
    d = tmp_path / "manifests"
    d.mkdir()
    for name in ("deployment.yaml", "service.yaml", "pdb.yaml"):
        (d / name).write_text((src / name).read_text())
    return d


@pytest.mark.parametrize("mode", ["files", "acme"])
def test_tls_main_writes_every_manifest_and_the_caddyfile(tmp_path: Path, mode: str):
    d = _manifest_dir(tmp_path)
    pdb_before = (d / "pdb.yaml").read_text()
    argv = ["write", "--manifest-dir", str(d), "--caddy-dir", str(tmp_path / "caddy"), "--mode", mode]
    if mode == "acme":
        argv += ["--hostname", "k7.example.com", "--acme-staging"]
    assert tls_main(argv) == 0
    caddyfile = (tmp_path / "caddy/Caddyfile").read_text()
    assert caddyfile == render_caddyfile(acme=mode == "acme", hostname="k7.example.com", staging=True)
    assert "name: caddy" in (d / "deployment.yaml").read_text()
    assert "targetPort: 8443" in (d / "service.yaml").read_text()
    pdb = (d / "pdb.yaml").read_text()
    assert ("maxUnavailable: 1" in pdb) if mode == "acme" else (pdb == pdb_before)


def test_tls_render_refuses_a_second_pass_and_unexpected_input():
    with pytest.raises(ValueError, match="already has a caddy container"):
        render_deployment(render_deployment(_HTTP_DEPLOYMENT, acme=False), acme=False)
    with pytest.raises(ValueError, match="volumes: block"):
        render_deployment(_HTTP_DEPLOYMENT.replace("      volumes:\n", "      vols:\n"), acme=False)
    with pytest.raises(ValueError, match="replicas: 2"):
        render_deployment(_HTTP_DEPLOYMENT.replace("  replicas: 2\n", "  replicas: 3\n"), acme=True)
    with pytest.raises(ValueError, match="targetPort: 8000"):
        render_service(render_service(_HTTP_SERVICE))
    with pytest.raises(ValueError, match="minAvailable: 1"):
        render_pdb("spec:\n  maxUnavailable: 1\n")


def test_tls_render_appends_volumes_to_a_file_without_trailing_newline():
    full = yaml.safe_load(render_deployment(_HTTP_DEPLOYMENT, acme=False))
    trimmed = yaml.safe_load(render_deployment(_HTTP_DEPLOYMENT.rstrip("\n"), acme=False))
    assert trimmed == full
    assert {"caddyfile", "tls-certs"} <= {v["name"] for v in full["spec"]["template"]["spec"]["volumes"]}
