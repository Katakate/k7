#!/usr/bin/env python3

import asyncio
import builtins
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import typer
import yaml
from kubernetes_asyncio.client.exceptions import ApiException
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from k7 import __version__ as K7_VERSION
from k7 import logging as k7_logging
from k7.cli import _oidc
from k7.cli._client import CliContext, _resolve_api_ca, _resolve_api_url, handle_api_call, verify_ssl_for_url
from k7.cli._config import config_app
from k7.core import cluster_config, tenancy
from k7.core.core import CONTAINERD_SOCKET_FILE, K7Core, aclose_k7_core
from k7.core.docker import parse_docker_disk
from k7.core.models import SandboxConfig, SandboxConfigOverrides
from k7.core.sidecar import SIDECAR_REGISTRY
from k7.deploy import images as k7_images
from k7.deploy import k7_api_keys as api_keys_store
from k7.deploy import k7_metrics_policy
from k7.metering import config as metering_config
from k7.metering import showback
from k7_sdk.client import Client


def _core_run(core: K7Core, coro):
    """Run one K7Core coroutine and close its kube ApiClient.

    Typer is sync and uses ``asyncio.run``, which tears down the loop when
    the coroutine returns. kubernetes_asyncio binds aiohttp to that loop;
    without ``aclose`` the interpreter logs ``Event loop is closed``.
    """

    async def _run():
        try:
            return await coro
        finally:
            await aclose_k7_core(core)

    return asyncio.run(_run())


K3S_VERSION_RE = re.compile(r"^v1\.\d+\.\d+\+k3s\d+$")
# k7-install-node.yaml k7_monitoring_scraper: kube-prometheus-stack's Prometheus pods.
MONITORING_SCRAPER = "monitoring:app.kubernetes.io/name=prometheus"
MONITORING_PROMETHEUS_URL = "http://kube-prometheus-stack-prometheus.monitoring.svc:9090"
DEV_SAMPLE_INTERVAL = 15

app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})
app.add_typer(config_app, name="config")


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit",
        is_eager=True,
        is_flag=True,
    ),
    api_url: str | None = typer.Option(
        None,
        "--api-url",
        envvar="K7_API_URL",
        help="K7 API endpoint (overrides env / config file). e.g. https://10.0.0.1:31000",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="K7_API_KEY",
        help="API key for the K7 API (overrides env / config file).",
    ),
    api_ca: str | None = typer.Option(
        None,
        "--api-ca",
        envvar="K7_API_CA",
        help="CA file to trust for https:// API URLs (overrides env / config / /etc/k7/tls/ca.crt).",
    ),
    system_namespace: str | None = typer.Option(
        None,
        "--system-namespace",
        envvar="K7_SYSTEM_NAMESPACE",
        help="Namespace of k7-api / k7-agent (default: /etc/k7/config; kube-system for `k7 install`).",
    ),
    use_core: bool = typer.Option(
        False,
        "--core",
        hidden=True,
        help="Bypass the API; call K7Core directly in-process (debugging / on-node tests).",
    ),
):
    """K7 — sandbox management CLI.

    By default, sandbox-management commands talk to the K7 API. Set the URL
    and key via ``--api-url`` / ``--api-key``, ``K7_API_URL`` / ``K7_API_KEY``,
    or ``k7 config set api.url ...``. On a cluster node, the CLI falls back to
    ``/etc/k7/api_endpoint`` and ``/etc/k7/api_keys.json`` automatically.
    """
    if version:
        typer.echo(K7_VERSION)
        raise typer.Exit()
    k7_logging.configure("human", "info")
    if system_namespace:
        os.environ[cluster_config.SYSTEM_NAMESPACE] = system_namespace
    # CliContext is built lazily — handlers that never call ``ctx.obj.client()``
    # (install / start-api / config / ...) don't trigger the missing-URL error.
    ctx.obj = CliContext(use_core=use_core, api_url=api_url, api_key=api_key, api_ca=api_ca)
    if ctx.invoked_subcommand is None:
        # Show top-level help when no command is provided
        try:
            typer.echo(ctx.get_help())
        except Exception:
            typer.echo("Usage: k7 [OPTIONS] COMMAND [ARGS]...\nTry 'k7 -h' for help.")
        raise typer.Exit()


_ETC_K7_API_ENDPOINT = Path("/etc/k7/api_endpoint")
PLATFORM_DENY_POLICY = "k7-sandbox-platform-deny"


def _key_store_call(fn, *args, **kwargs):
    """Run a key-store op against the k7-api-keys Secret via the node's kubectl; loud on failure."""
    try:
        return fn(api_keys_store.Kubectl(_kubectl_cmd(), _system_ns()), *args, **kwargs)
    except (RuntimeError, ValueError, OSError) as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(1)


BACKEND_ALLOWED = {"kata-firecracker-devmapper", "kata-qemu-longhorn", "k7d", "k7d-fc"}
BACKEND_ALIASES = {
    "kfd": "kata-firecracker-devmapper",
    "kql": "kata-qemu-longhorn",
    "k7": "k7d",
    "k7-fc": "k7d-fc",
}
# Install-time empty set. Not a RuntimeClass — omit/blank is an error, not none.
BACKEND_NONE = "none"
INSTALL_BACKEND_REQUIRED = (
    "Specify --backend (required; there is no default).\n"
    "  kfd     Firecracker + jailer; needs a spare raw disk\n"
    "  kql     Kata QEMU + Longhorn on the OS disk\n"
    "  k7d     warm-fork microVM daemon (shared per node)\n"
    "  k7d-fc  same daemon, Firecracker under the jailer\n"
    "  none    no sandbox runtime on this node (control-plane only)\n"
    "Example: k7 install --backend kfd,kql,k7d\n"
    "Inventory (-i): every host needs k7_backends=… or k7_backends=none "
    "(group vars are fine). Empty/omitted is not none."
)
# Deprecated short + pre-rename full names. Still accepted; emit a warning.
BACKEND_DEPRECATED_ALIASES = {
    "fd": "kata-firecracker-devmapper",
    "ql": "kata-qemu-longhorn",
    "firecracker-devmapper": "kata-firecracker-devmapper",
    "qemu-longhorn": "kata-qemu-longhorn",
}


def _normalize_backend(backend: str | None) -> str | None:
    if backend is None:
        return None
    value = backend.strip().lower()
    if not value:
        raise typer.BadParameter("Backend value cannot be empty.")
    if value in BACKEND_ALIASES:
        return BACKEND_ALIASES[value]
    if value in BACKEND_DEPRECATED_ALIASES:
        canonical = BACKEND_DEPRECATED_ALIASES[value]
        short = {"kata-firecracker-devmapper": "kfd", "kata-qemu-longhorn": "kql"}[canonical]
        typer.echo(
            f"⚠️  Backend '{value}' is deprecated. Use '{short}' or '{canonical}'.",
            err=True,
        )
        return canonical
    if value in BACKEND_ALLOWED:
        return value
    allowed = ", ".join(sorted(BACKEND_ALLOWED))
    raise typer.BadParameter(
        f"Unsupported backend '{backend}'. Use {allowed} (aliases: kfd, kql, k7, k7-fc; deprecated: fd, ql)."
    )


def _parse_backends(value: str) -> list[str]:
    """Parse a comma-separated list of backend names, normalize and de-dupe.

    Used by `k7 install` where multiple backends can be installed on a node.
    Preserves first-seen order; raises typer.BadParameter on unknown entries.
    ``none`` is the empty set (no sandbox runtime); it cannot mix with others.
    """
    if value is None:
        raise typer.BadParameter("Backend value cannot be empty.")
    raw = [p for p in (s.strip() for s in value.split(",")) if p]
    if not raw:
        raise typer.BadParameter("Backend value cannot be empty.")
    lowered = [p.lower() for p in raw]
    if BACKEND_NONE in lowered:
        if lowered != [BACKEND_NONE]:
            raise typer.BadParameter(
                f"'{BACKEND_NONE}' cannot be combined with other backends. "
                "Use only none, or a list of runtimes (kfd, kql, k7d, k7d-fc)."
            )
        return []
    seen: builtins.list[str] = []
    for entry in raw:
        normalized = _normalize_backend(entry)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def _kubectl_cmd() -> list[str]:
    return cluster_config.kubectl_cmd()


def _system_ns() -> str:
    try:
        return cluster_config.system_namespace()
    except RuntimeError as e:
        typer.echo(f"❌ {e} Or pass --system-namespace.", err=True)
        raise typer.Exit(1) from None


def _kubectl_run(kubectl: list[str], args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run kubectl. ``None`` when the binary is missing (laptop / API-only host)."""
    try:
        return subprocess.run(kubectl + args, capture_output=True, text=True)
    except FileNotFoundError:
        return None


def _api_status_via_https() -> None:
    """Laptop path: probe ``GET /health`` on the configured API URL.

    ``k7 api status`` historically shelled out to kubectl, which crashes with
    ``FileNotFoundError`` on a machine that only has the .deb CLI. Fail loud
    if nothing is configured; otherwise print reachability + the URL.
    """
    url = _resolve_api_url(None)
    if not url:
        typer.echo("❌ kubectl is not installed, and no API URL is configured.")
        typer.echo("On a cluster node, `k7 api status` uses kubectl.")
        typer.echo("From a laptop, after `k7 config set api.url` / `api.ca` / `api.key`:")
        typer.echo("    curl --cacert ./k7-ca.crt https://<node-ip>:31007/health")
        raise typer.Exit(1)
    ca = _resolve_api_ca(None)
    verify = verify_ssl_for_url(url, ca)
    try:
        resp = requests.get(f"{url.rstrip('/')}/health", timeout=10, verify=verify)
    except requests.RequestException as e:
        typer.echo(f"❌ Could not reach K7 API at {url}: {e}", err=True)
        raise typer.Exit(1) from e
    if resp.ok:
        typer.echo("✅ K7 API is reachable")
        typer.echo(f"🌐 API endpoint: {url}")
        typer.echo("\n📝 Replica counts and pod placement need kubectl on a cluster node.")
        return
    typer.echo(f"❌ K7 API at {url} returned HTTP {resp.status_code}", err=True)
    raise typer.Exit(1)


def _build_default_inventory(
    hosts: list[str] | None,
    role: str,
    backends: list[str],
    disk: str | None,
    longhorn_extra_disk: str | None,
) -> str:
    """Build a minimal multi-group inventory when the user did not provide one.

    Single-host shortcut: no `hosts` argument → localhost server with the
    user-supplied backends (``--backend`` is required; ``none`` →
    ``k7_backends=none``).
    """
    server_lines: builtins.list[str] = []
    agent_lines: builtins.list[str] = []
    backends_csv = ",".join(backends) if backends else BACKEND_NONE
    has_longhorn = "kata-qemu-longhorn" in backends

    def _host_vars(extra_host: str | None = None) -> str:
        parts = [
            f"k7_backends={backends_csv}",
            f"k7_disk_source=device:{disk}" if disk else "k7_disk_source=spare-disk",
        ]
        if has_longhorn and longhorn_extra_disk:
            parts.append(f"longhorn_extra_disk={longhorn_extra_disk}")
        if extra_host:
            parts.insert(0, extra_host)
        return " ".join(parts)

    if hosts:
        for host in hosts:
            line = f"{host} ansible_user=root ansible_ssh_private_key_file=~/.ssh/id_rsa {_host_vars()}"
            (server_lines if role == "server" else agent_lines).append(line)
    else:
        server_lines.append(f"localhost ansible_connection=local ansible_user=root {_host_vars()}")

    lines = [
        "[k7_servers]",
        *server_lines,
        "",
        "[k7_agents]",
        *agent_lines,
        "",
        "[k7_cluster:children]",
        "k7_servers",
        "k7_agents",
    ]
    return "\n".join(lines)


def _tls_cert_key_match(cert: Path, key: Path) -> None:
    """Refuse a cert/key pair that openssl does not consider matching."""
    if not cert.is_file():
        raise typer.BadParameter(f"--api-tls-cert '{cert}' is not a file")
    if not key.is_file():
        raise typer.BadParameter(f"--api-tls-key '{key}' is not a file")
    cert_pub = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-pubkey"],
        capture_output=True,
        text=True,
    )
    key_pub = subprocess.run(
        ["openssl", "pkey", "-in", str(key), "-pubout"],
        capture_output=True,
        text=True,
    )
    if cert_pub.returncode != 0:
        raise typer.BadParameter(f"--api-tls-cert is not a readable PEM certificate: {cert_pub.stderr.strip()}")
    if key_pub.returncode != 0:
        raise typer.BadParameter(f"--api-tls-key is not a readable PEM private key: {key_pub.stderr.strip()}")
    if cert_pub.stdout != key_pub.stdout:
        raise typer.BadParameter("--api-tls-cert and --api-tls-key are not a matching pair")


def _validate_api_hostname(name: str) -> str:
    host = name.strip()
    if not host:
        raise typer.BadParameter("--api-hostname must be a DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise typer.BadParameter(
        "--api-hostname must be a DNS name, not an IP (Let's Encrypt will not issue for a bare IP)"
    )


def _validate_dns_upstreams(values: list[str]) -> list[str]:
    """Same rules as the playbook's dns_validate.yml, checked before Ansible runs."""
    ips = [v.strip() for v in values if v.strip()]
    if len(ips) > 3:
        raise typer.BadParameter(
            f"at most 3 --dns-upstream values (got {len(ips)}): the kubelet hands CoreDNS only 3 nameservers"
        )
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise typer.BadParameter(f"--dns-upstream '{raw}' is not an IP address: {exc}") from exc
        if ip.version != 4:
            raise typer.BadParameter(
                f"--dns-upstream {raw} is IPv6; the pod network is IPv4-only, so CoreDNS could not reach it"
            )
        if ip.is_loopback or ip.packed[0] == 0:
            raise typer.BadParameter(f"--dns-upstream {raw} would point CoreDNS back at itself")
    return ips


def _read_api_endpoint(kubectl: list[str]) -> str | None:
    """Read the K7 API NodePort endpoint from the file the playbook wrote, or the Service."""
    etc = _ETC_K7_API_ENDPOINT
    if etc.exists():
        try:
            written = etc.read_text().strip()
            if written:
                return written
        except OSError:
            pass
    try:
        port_result = subprocess.run(
            kubectl
            + [
                "get",
                "svc",
                "k7-api",
                "-n",
                _system_ns(),
                "-o",
                "jsonpath={.spec.ports[0].nodePort},{.spec.ports[0].targetPort}",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return _resolve_api_url(None)
    if port_result.returncode != 0 or not port_result.stdout.strip():
        return None
    node_port, _, target_port = port_result.stdout.strip().partition(",")
    if not node_port:
        return None

    # Get node addresses as JSON and pick the first IPv4 InternalIP
    node_result = subprocess.run(
        kubectl + ["get", "nodes", "-o", "jsonpath={.items[0].status.addresses}"],
        capture_output=True,
        text=True,
    )
    node_ip = "localhost"
    if node_result.returncode == 0 and node_result.stdout.strip():
        try:
            addrs = json.loads(node_result.stdout.strip())
            for addr in addrs:
                if addr.get("type") == "InternalIP" and ":" not in addr.get("address", ":"):
                    node_ip = addr["address"]
                    break
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    scheme = "https" if target_port.strip() == "8443" else "http"
    return f"{scheme}://{node_ip}:{node_port}"


def _get_api_manifests_dir() -> str:
    """Locate the embedded K3s API manifests directory."""
    from importlib import resources as _resources

    try:
        manifest_dir = _resources.files("k7.deploy").joinpath("manifests/k7-api")
        with _resources.as_file(manifest_dir) as p:
            if p.is_dir():
                return str(p)
    except Exception:
        pass
    # Fallback: relative to this file (source tree layout)
    src_path = Path(__file__).resolve().parent.parent / "deploy" / "manifests" / "k7-api"
    if src_path.is_dir():
        return str(src_path)
    typer.echo("❌ K3s API manifests not found in package", err=True)
    raise typer.Exit(1)


def _get_deploy_dir() -> str:
    """Packaged ``k7/deploy`` directory (manifests, k7d-fc, TLS renderer) — no checkout needed."""
    return str(Path(_get_api_manifests_dir()).parent.parent)


def _metering_settings(
    api_mode: str,
    sample_interval: int | None,
    storage_interval: int,
    spool_size: str,
    high_water: int,
    require_delivery: bool,
    prometheus_url: str | None,
    sinks_file: Path | None,
) -> dict:
    """The k7-metering document, validated exactly as k7-api and k7-agent will read it."""
    if sample_interval is None:
        if api_mode == "production":
            raise typer.BadParameter("--api-mode production needs an explicit --metering-sample-interval (5-60 s)")
        sample_interval = DEV_SAMPLE_INTERVAL
    sinks: object = []
    if sinks_file is not None:
        try:
            sinks = yaml.safe_load(sinks_file.read_text())
        except (OSError, yaml.YAMLError) as e:
            raise typer.BadParameter(f"--metering-sinks-file {sinks_file}: {e}") from e
    try:
        doc = {
            "sampleInterval": sample_interval,
            "storageInterval": storage_interval,
            "spool": {
                "maxBytes": int(tenancy.quantity(spool_size, "--metering-spool-size")),
                "highWaterPercent": high_water,
            },
            "requireDelivery": require_delivery,
            "prometheusUrl": prometheus_url,
            "sinks": sinks,
        }
        metering_config.parse(doc)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    return doc


@app.command()
def install(
    ctx: typer.Context,
    hosts: list[str] | None = typer.Argument(None, help="Optional target hosts; defaults to localhost"),
    playbook: str | None = typer.Option(None, "-p", "--playbook", help="Path to custom Ansible playbook"),
    inventory: str | None = typer.Option(None, "-i", "--inventory", help="Path to custom Ansible inventory"),
    disk: str | None = typer.Option(
        None,
        "--disk",
        help="Block device to use for LVM thin-pool (e.g., /dev/nvme2n1)",
    ),
    backend: str | None = typer.Option(
        None,
        "--backend",
        "-b",
        help=(
            "Comma-separated sandbox backends to install. Required when not using "
            "-i inventory (every inventory host must set k7_backends). "
            "Choices: kfd, kql, k7d, k7d-fc, none. No default — omit is an error. "
            "none means no sandbox runtime (scheduling-only node)."
        ),
    ),
    k7d_version: str | None = typer.Option(
        None,
        "--k7d-version",
        help="k7d release version to install (k7d backend only; default from playbook: 0.7.0)",
    ),
    k7d_artifact: str | None = typer.Option(
        None,
        "--k7d-artifact",
        help=(
            "Path to a local k7d release tarball (k7d-v<version>-x86_64-linux.tar.gz) to install "
            "instead of downloading from GitHub (k7d backend only)."
        ),
    ),
    role: str = typer.Option(
        "server",
        "--role",
        help="Role for ad-hoc hosts when no inventory is given: server (master) or agent (worker)",
        show_default=True,
    ),
    join: str | None = typer.Option(
        None,
        "--join",
        help="Join an existing cluster: master URL like https://<first-master>:6443 (requires --role agent or server)",
    ),
    join_token: str | None = typer.Option(
        None,
        "--join-token",
        help="K3s join token for --join (read from <first-master>:/var/lib/rancher/k3s/server/node-token)",
    ),
    ha: bool = typer.Option(
        False,
        "--ha",
        help="Enable HA: configure multiple masters with embedded etcd (requires inventory with 3+ servers)",
    ),
    replicas: int | None = typer.Option(
        None,
        "--replicas",
        help="Longhorn replica count (kata-qemu-longhorn only). Defaults to min(3, node count).",
    ),
    longhorn_data_path: str | None = typer.Option(
        None,
        "--longhorn-data-path",
        help="Longhorn default data path (default: /var/lib/longhorn)",
    ),
    longhorn_extra_disk: str | None = typer.Option(
        None,
        "--longhorn-extra-disk",
        help="Extra disk/partition to register as a Longhorn storage node disk",
    ),
    cni: str = typer.Option(
        "cilium",
        "--cni",
        help="CNI plugin: cilium (default, enables FQDN egress) or flannel (CIDR-only)",
        show_default=True,
    ),
    hubble: bool = typer.Option(
        False,
        "--hubble",
        help=("Enable Cilium Hubble flow observability (relay + CLI; no UI). Off by default. Requires --cni cilium."),
    ),
    api_allow_cidr: list[str] | None = typer.Option(
        None,
        "--api-allow-cidr",
        help=(
            "Restrict the k7-api NodePort to these source CIDRs (repeatable, e.g. 203.0.113.4/32). "
            "Off by default: the API answers any client that has an API key. Requires --cni cilium. "
            "Defence in depth only — this is not rate limiting and not a secure-API claim."
        ),
    ),
    metrics_scraper: str | None = typer.Option(
        None,
        "--metrics-scraper",
        help=(
            "Pods allowed to scrape /metrics on k7-api and k7-agent port 9464, as "
            "<namespace>:<label>=<value>[,<label>=<value>] (e.g. monitoring:app.kubernetes.io/name=prometheus). "
            "Without it no pod can reach the port."
        ),
    ),
    monitoring: bool = typer.Option(
        False,
        "--monitoring",
        help=(
            "Also install the pinned kube-prometheus-stack (namespace monitoring) with K7's dashboards and "
            "alerts, scraping K7. A convenience for single-box installs; implies the scraper selector."
        ),
    ),
    dns_upstream: list[str] | None = typer.Option(
        None,
        "--dns-upstream",
        help=(
            "Upstream resolver for CoreDNS (repeatable, IPv4, at most 3, e.g. 1.1.1.1). "
            "Default: the host's resolvers. Sets the playbook's k7_dns_upstreams."
        ),
    ),
    api_insecure_http: bool = typer.Option(
        False,
        "--api-insecure-http",
        help="Serve k7-api as today's plain HTTP NodePort (no Caddy sidecar, keys travel in cleartext).",
    ),
    api_hostname: str | None = typer.Option(
        None,
        "--api-hostname",
        help="DNS name pointing at the first master; Caddy obtains a Let's Encrypt cert (HTTP-01 on :80).",
    ),
    api_tls_cert: str | None = typer.Option(
        None,
        "--api-tls-cert",
        help="Operator-supplied server certificate (PEM). Requires --api-tls-key.",
    ),
    api_tls_key: str | None = typer.Option(
        None,
        "--api-tls-key",
        help="Operator-supplied server private key (PEM). Requires --api-tls-cert.",
    ),
    api_acme_staging: bool = typer.Option(
        False,
        "--api-acme-staging",
        hidden=True,
        help="Use the Let's Encrypt staging ACME directory (integration tests).",
    ),
    oidc_issuer: str | None = typer.Option(
        None,
        "--oidc-issuer",
        help=(
            "OIDC issuer URL (https). Enables `k7 login`. The five required settings are issuer, "
            "audience, client id, groups claim and username claim (or none). --oidc-jwks-ttl is optional."
        ),
    ),
    oidc_audience: str | None = typer.Option(None, "--oidc-audience", help="Expected `aud` of OIDC tokens."),
    oidc_client_id: str | None = typer.Option(None, "--oidc-client-id", help="Public OIDC client for `k7 login`."),
    oidc_groups_claim: str | None = typer.Option(None, "--oidc-groups-claim", help="Groups claim, e.g. groups."),
    oidc_username_claim: str | None = typer.Option(None, "--oidc-username-claim", help="Username claim, e.g. email."),
    oidc_jwks_ttl: int = typer.Option(
        3600,  # k7.api.oidc.JWKS_TTL_SECONDS
        "--oidc-jwks-ttl",
        help="Seconds to trust a cached OIDC JWKS before refetching (default 3600; minimum 10).",
    ),
    oidc_allow_http_issuer: bool = typer.Option(
        False, "--oidc-allow-http-issuer", hidden=True, help="Allow an http:// issuer (integration tests only)."
    ),
    api_mode: str | None = typer.Option(
        None,
        "--api-mode",
        help=(
            "Required with the API (no default): production (every key needs a tenant or a cluster role, sandboxes "
            "need cpu/memory, refuses --api-insecure-http) or development (today's behaviour)."
        ),
    ),
    no_api: bool = typer.Option(
        False,
        "--no-api",
        help="Skip deploying the K7 API into K3s (CLI-only install). Re-run `k7 install` later to add it.",
    ),
    k3s_version: str | None = typer.Option(
        None,
        "--k3s-version",
        help="Override the pinned K3s version (v1.X.Y+k3sZ). Only applies when K3s is not installed yet.",
    ),
    node_prep_only: bool = typer.Option(
        False,
        "--node-prep-only",
        hidden=True,
        help="Run only the k7_node_prep role (no K3s install, no cluster resources).",
    ),
    metering_sample_interval: int | None = typer.Option(
        None,
        "--metering-sample-interval",
        help="Usage sample period in seconds (5-60). Required with --api-mode production; development uses 15.",
    ),
    metering_storage_interval: int = typer.Option(
        60, "--metering-storage-interval", help="Disk/snapshot usage window in seconds (30-600)."
    ),
    metering_spool_size: str = typer.Option(
        "1Gi", "--metering-spool-size", help="Per-node usage spool cap; the oldest segment is dropped past it."
    ),
    metering_spool_high_water: int = typer.Option(
        80, "--metering-spool-high-water", help="Spool fill percent above which --metering-require-delivery refuses."
    ),
    metering_require_delivery: bool = typer.Option(
        False,
        "--metering-require-delivery",
        help="Refuse sandbox creates (503) on nodes whose usage spool is above its high-water mark.",
    ),
    metering_prometheus_url: str | None = typer.Option(
        None,
        "--metering-prometheus-url",
        help="Prometheus that scrapes K7, for `k7 usage`. --monitoring sets its own.",
    ),
    metering_sinks_file: Path | None = typer.Option(
        None,
        "--metering-sinks-file",
        help="YAML list of usage sinks (http with the openmeter preset, stdout); see docs/METERING.md.",
    ),
    api_image: str | None = typer.Option(
        None,
        "--api-image",
        help=(
            "k7-api image: 'local' builds k7-api:local from the checkout in the cwd (unsigned, needs "
            "Docker), or name@sha256:<digest>. Default: the digest in this release's images.lock.yaml."
        ),
    ),
    api_remote_install: bool = typer.Option(
        False,
        "--api-remote-install",
        help=(
            "Enable POST /api/v1/install (cluster-wide admin role): deploys the k7-node-installer DaemonSet in "
            "k7-system, configured from the first master, and lets the API label nodes into its pool. "
            "Re-installing without it removes that stack and the pool labels again."
        ),
    ),
    node_installer_image: str | None = typer.Option(
        None,
        "--node-installer-image",
        help=(
            "With --api-remote-install: 'local' builds k7-node-installer:local from the checkout in the "
            "cwd (unsigned, needs Docker), or name@sha256:<digest>. Default: images.lock.yaml."
        ),
    ),
    node_installer_allow_server_restart: bool = typer.Option(
        False,
        "--node-installer-allow-server-restart",
        help="With --api-remote-install: let the installer prepare K3s server nodes (restarts their apiserver).",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose output"),
):
    """Install K7 on target hosts using Ansible.

    Single-node: `k7 install --backend kfd,kql,k7d` (``--backend`` is required;
    there is no default). ``none`` installs K3s with no sandbox runtime.
    Pass ``k7d`` / ``k7d-fc`` for the warm-fork daemon (downloads
    `Katakate/k7d` v0.7.0 unless `--k7d-version` / `--k7d-artifact` override
    it). `--backend k7d-fc` (alias `k7-fc`) adds RuntimeClass `k7-fc` and
    the pinned Firecracker+jailer next to that daemon.

    Multi-node: `k7 install -i inventory.ini` reads roles, backends, and disks
    from the Ansible inventory. Every host must set ``k7_backends`` (or inherit
    it from group vars); use ``k7_backends=none`` for scheduling-only masters.
    Empty/omitted is not none. See `inventory.ini.example`.
    Dual-NVMe boxes need the OS on one disk and a raw spare for kfd — see
    `tutorials/k7_hetzner_node_setup.md` (`TWO_DISK=1`).
    """
    if role not in ("server", "agent"):
        raise typer.BadParameter(f"--role must be 'server' or 'agent', got '{role}'")

    cni_value = (cni or "").strip().lower()
    if cni_value not in ("cilium", "flannel"):
        raise typer.BadParameter(f"--cni must be 'cilium' or 'flannel', got '{cni}'")
    if hubble and cni_value != "cilium":
        raise typer.BadParameter(f"--hubble requires the Cilium CNI (got --cni {cni_value}).")

    # Validate the k7-api allowlist here, before Ansible runs. A bad
    # CIDR must never reach a security policy.
    api_cidrs = [c.strip() for c in (api_allow_cidr or []) if c.strip()]
    if api_cidrs:
        if cni_value != "cilium":
            raise typer.BadParameter(
                f"--api-allow-cidr requires the Cilium CNI (got --cni {cni_value}). The reserved "
                "host/remote-node/health/kube-apiserver peers are not expressible in a v1 "
                "NetworkPolicy, and a v1 policy without `host` would break the kubelet probes on "
                "the k7-api pod."
            )
        for cidr in api_cidrs:
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise typer.BadParameter(f"--api-allow-cidr '{cidr}' is not a valid CIDR: {exc}") from exc
            if network.prefixlen == 0:
                typer.echo(
                    f"⚠️  --api-allow-cidr {cidr} allows every source address — that is today's "
                    "behaviour with extra steps. Pass the CIDRs your clients actually come from.",
                    err=True,
                )

    scraper = (metrics_scraper or "").strip()
    if monitoring:
        if scraper:
            raise typer.BadParameter("--monitoring sets the scraper to its own Prometheus; drop --metrics-scraper")
        if no_api:
            raise typer.BadParameter("--monitoring scrapes the k7-api; it cannot be combined with --no-api")
        scraper = MONITORING_SCRAPER
    try:
        k7_metrics_policy.parse_scraper(scraper)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    dns_upstreams = _validate_dns_upstreams(dns_upstream or [])

    # TLS flag combinations fail here, before Ansible.
    hostname = (api_hostname or "").strip() or None
    cert_path = Path(api_tls_cert).expanduser() if api_tls_cert else None
    key_path = Path(api_tls_key).expanduser() if api_tls_key else None
    if api_insecure_http and (hostname or cert_path or key_path):
        raise typer.BadParameter(
            "--api-insecure-http cannot be combined with --api-hostname or --api-tls-cert/--api-tls-key"
        )
    if hostname and (cert_path or key_path):
        raise typer.BadParameter("--api-hostname cannot be combined with --api-tls-cert/--api-tls-key")
    if (cert_path is None) ^ (key_path is None):
        raise typer.BadParameter("--api-tls-cert and --api-tls-key must be passed together")
    if hostname:
        hostname = _validate_api_hostname(hostname)
    if cert_path is not None and key_path is not None:
        _tls_cert_key_match(cert_path.resolve(), key_path.resolve())
    if api_insecure_http and api_cidrs:
        typer.echo(
            "⚠️  --api-insecure-http with --api-allow-cidr: the allowlist still applies, "
            "but API keys still travel in cleartext.",
            err=True,
        )
    if api_acme_staging and not hostname:
        raise typer.BadParameter("--api-acme-staging requires --api-hostname")
    if k3s_version is not None and not K3S_VERSION_RE.match(k3s_version):
        raise typer.BadParameter(f"--k3s-version must look like v1.36.5+k3s1, got '{k3s_version}'")

    if join and not join_token:
        raise typer.BadParameter("--join requires --join-token")

    oidc = {
        "issuer": oidc_issuer,
        "audience": oidc_audience,
        "client_id": oidc_client_id,
        "groups_claim": oidc_groups_claim,
        "username_claim": oidc_username_claim,
    }
    oidc = {k: (v or "").strip() for k, v in oidc.items()}
    if any(oidc.values()) and not all(oidc.values()):
        missing = [f"--oidc-{k.replace('_', '-')}" for k, v in oidc.items() if not v]
        raise typer.BadParameter(f"OIDC needs all five --oidc-* flags; also pass {', '.join(missing)}")
    if oidc["issuer"] and not oidc["issuer"].startswith("https://") and not oidc_allow_http_issuer:
        raise typer.BadParameter(f"--oidc-issuer must be https:// (got {oidc['issuer']})")
    if oidc_jwks_ttl < 10:  # k7.api.oidc.REFRESH_SECONDS
        raise typer.BadParameter(f"--oidc-jwks-ttl must be an integer >= 10 seconds (got {oidc_jwks_ttl})")

    resolved_api_image = ""
    metering = None
    if not (no_api or node_prep_only):
        if api_mode not in cluster_config.API_MODES:
            raise typer.BadParameter(
                f"--api-mode is required (production or development; there is no default), got {api_mode!r}"
            )
        if api_mode == "production" and api_insecure_http:
            raise typer.BadParameter(
                "--api-mode production refuses --api-insecure-http (keys would travel in cleartext)"
            )
        metering = _metering_settings(
            api_mode,
            metering_sample_interval,
            metering_storage_interval,
            metering_spool_size,
            metering_spool_high_water,
            metering_require_delivery,
            MONITORING_PROMETHEUS_URL if monitoring else metering_prometheus_url,
            metering_sinks_file,
        )
        if monitoring and metering_prometheus_url:
            raise typer.BadParameter("--monitoring sets metering.prometheusUrl to its own Prometheus; drop the flag")
        try:
            resolved_api_image = k7_images.resolve_api_image(api_image)
        except ValueError as exc:
            raise typer.BadParameter(str(exc))
        except LookupError as exc:
            typer.echo(f"❌ {exc}", err=True)
            raise typer.Exit(1)

    if (node_installer_image or node_installer_allow_server_restart) and not api_remote_install:
        raise typer.BadParameter(
            "--node-installer-image / --node-installer-allow-server-restart need --api-remote-install"
        )
    resolved_installer_image = ""
    if api_remote_install:
        if no_api or node_prep_only:
            raise typer.BadParameter("--api-remote-install needs the k7-api (drop --no-api / --node-prep-only)")
        if api_mode == "production":
            raise typer.BadParameter(
                "--api-mode production refuses --api-remote-install (k7-api would exit at startup)"
            )
        try:
            resolved_installer_image = k7_images.resolve_image(k7_images.NODE_INSTALLER_IMAGE_KEY, node_installer_image)
        except ValueError as exc:
            raise typer.BadParameter(str(exc))
        except LookupError as exc:
            typer.echo(f"❌ {exc}", err=True)
            raise typer.Exit(1)

    if ha and not inventory and (not hosts or len(hosts) < 3):
        typer.echo(
            "⚠️  --ha is intended for inventory-based multi-master clusters with 3+ servers; "
            "ignoring (single-node install).",
            err=True,
        )

    playbook_content = None
    if playbook and os.path.exists(playbook):
        with open(playbook) as f:
            playbook_content = f.read()

    inventory_file = inventory if inventory and os.path.exists(inventory) else None
    # No default and no envvar: non-None means --backend was passed. typer 0.27 no longer
    # reports ParameterSource.COMMANDLINE for it.
    backend_explicit = backend is not None
    if not backend_explicit and not inventory_file:
        typer.echo(INSTALL_BACKEND_REQUIRED, err=True)
        raise typer.Exit(1)
    backends_list = _parse_backends(backend) if backend is not None else []

    inventory_content = None
    if inventory_file:
        with open(inventory_file) as f:
            inventory_content = f.read()
    else:
        inventory_content = _build_default_inventory(
            hosts=hosts,
            role=role,
            backends=backends_list,
            disk=disk,
            longhorn_extra_disk=longhorn_extra_disk,
        )

    core = K7Core()

    # Shared state for progress callback
    progress_state = {
        "current_task": "Preparing...",
        "total": 100,
        "completed": 0,
        "num_hosts": len(hosts) if hosts else 1,
    }

    # Build a single progress renderable and mount it under Live with a single line above
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    )

    # Create a task whose total equals number of ansible tasks (updated by callback)
    main_task = progress.add_task(
        f"Installing K7 on {progress_state['num_hosts']} host(s)...",
        total=progress_state["total"],
    )

    # Render current task one line above the progress bar
    current_text = Text(f"Current task: {progress_state['current_task']}")
    group = Group(current_text, progress)

    with Live(group, refresh_per_second=8, transient=False) as live:

        def on_progress(event: dict):
            try:
                if event.get("type") == "total":
                    total_tasks = max(1, int(event.get("total_tasks") or 0))
                    progress_state["total"] = total_tasks
                    progress.update(main_task, total=total_tasks)
                    live.refresh()
                elif event.get("type") == "task_start":
                    name = event.get("name") or "Running task"
                    idx = int(event.get("index") or 0)
                    total = int(event.get("total") or progress_state["total"])
                    progress_state["current_task"] = name
                    progress_state["total"] = max(1, total)
                    # Mark previous task as completed when a new task starts
                    progress.update(
                        main_task,
                        total=progress_state["total"],
                        completed=max(0, idx - 1),
                        description=f"Installing K7 on {progress_state['num_hosts']} host(s)...",
                    )
                    current_text.plain = f"Current task: {name}"
                    live.refresh()
            except Exception:
                pass

        # Kick off install and update UI as lines arrive.
        # When --replicas is unset and we built the inventory ourselves, default
        # to min(3, node count). When the user supplied an inventory we can't
        # know the node count up-front, so we let the playbook compute its own
        # default (min(3, len(k7_cluster))) and only forward an explicit value.
        inventory_user_supplied = bool(inventory_file)
        node_count = len(hosts) if hosts else 1
        if replicas is not None:
            effective_replicas: int | None = replicas
            if not inventory_user_supplied and replicas > node_count:
                typer.echo(
                    f"⚠️  --replicas={replicas} exceeds node count ({node_count}); "
                    "Longhorn cannot satisfy replica count > node count",
                    err=True,
                )
        elif inventory_user_supplied:
            effective_replicas = None
        else:
            effective_replicas = min(3, node_count)
        # Only a `local` image needs a checkout: the playbook builds it on
        # the first master from `k7_repo_root` (the cwd) and fails loudly if
        # the Dockerfile is not there. Everything else the playbook copies
        # comes from this package's deploy dir.
        is_local_image = resolved_api_image == k7_images.API_IMAGE_LOCAL
        is_local_installer = resolved_installer_image == k7_images.API_IMAGE_LOCAL
        extra_vars = {
            "k7_distro": "k3s",
            "k3s_version": k3s_version,
            "k7_node_prep_only": "true" if node_prep_only else None,
            # Without -i, --disk is already in the generated inventory.
            "k7_disk_source": f"device:{disk}" if disk and inventory_user_supplied else None,
            # `k7_backends` is required on every host. Empty extra-var would
            # look like "omitted" in Ansible — send the `none` sentinel.
            "k7_backends": ",".join(backends_list) if backends_list else BACKEND_NONE,
            "longhorn_replicas": effective_replicas,
            "longhorn_data_path": longhorn_data_path,
            "longhorn_extra_disk": longhorn_extra_disk,
            "k7_cni": cni_value,
            "k7_hubble_enabled": "true" if hubble else "false",
            "k7_deploy_dir": _get_deploy_dir(),
            "k7_api_image": resolved_api_image,
            "k7_repo_root": os.getcwd() if is_local_image or is_local_installer else None,
            "k7_api_remote_install": "true" if api_remote_install else "false",
            "k7_node_installer_image": resolved_installer_image,
            "k7_node_installer_allow_server_restart": "true" if node_installer_allow_server_restart else "false",
            # ``--no-api`` flips the playbook's existing
            # ``k7_api_enabled`` gate (default true) so the API
            # manifests aren't applied.
            "k7_api_enabled": "false" if no_api else "true",
            # Empty means no CiliumNetworkPolicy and no Service
            # change — today's install, bit for bit.
            "k7_api_allow_cidrs": ",".join(api_cidrs),
            # Empty renders a deny-all policy for port 9464.
            "k7_metrics_scraper": scraper,
            "k7_monitoring": "true" if monitoring else "false",
            # None when the flag is absent, so an inventory's k7_dns_upstreams wins.
            "k7_dns_upstreams": ",".join(dns_upstreams) if dns_upstreams else None,
            # Default is HTTPS + playbook CA. Extra-vars are
            # strings so Ansible's `| bool` filter stays deterministic.
            "k7_api_insecure_http": "true" if api_insecure_http else "false",
            "k7_api_mode": api_mode,
            "k7_api_hostname": hostname or "",
            "k7_api_tls_cert_src": str(cert_path.resolve()) if cert_path else "",
            "k7_api_tls_key_src": str(key_path.resolve()) if key_path else "",
            "k7_api_acme_staging": "true" if api_acme_staging else "false",
            # All empty means no k7-oidc ConfigMap (OIDC off), like the other API flags.
            **{f"k7_oidc_{k}": v for k, v in oidc.items()},
            "k7_oidc_jwks_ttl_seconds": str(oidc_jwks_ttl),
            "k7_oidc_allow_http_issuer": "true" if oidc_allow_http_issuer else "false",
            "k7_metering": metering,
            # k7d backend artifact source overrides.
            "k7d_version": k7d_version,
            "k7d_artifact_local_path": k7d_artifact,
        }
        # Drop None to avoid leaking unused vars
        extra_vars = {k: v for k, v in extra_vars.items() if v is not None}
        # Ansible extra-vars have the highest precedence and would clobber
        # per-host `k7_backends` declared in a user-supplied inventory. Only
        # forward the CLI value when the user explicitly passed --backend;
        # otherwise the inventory is authoritative.
        if inventory_user_supplied and not backend_explicit:
            extra_vars.pop("k7_backends", None)
        elif inventory_user_supplied and backend_explicit:
            typer.echo(
                "⚠️  --backend overrides any per-host k7_backends in the inventory "
                "(extra-vars precedence). Omit --backend to let the inventory win.",
                err=True,
            )
        result = core.install_node(
            playbook_content,
            inventory_content,
            verbose,
            progress_callback=on_progress,
            stream_output=verbose,  # only print full ansible output when -v is used
            extra_vars=extra_vars,
        )

        # Final update to 100% if succeeded; otherwise leave as-is and show error
        if result.success:
            progress.update(
                main_task,
                total=max(1, progress_state["total"]),
                completed=max(1, progress_state["total"]),
            )
            live.refresh()

    if result.success:
        typer.echo("✅ Installation completed successfully!")
        if is_local_image:
            typer.echo(f"⚠️  Deployed {k7_images.LOCAL_API_IMAGE_REF} built from {os.getcwd()} — unsigned.")
        elif resolved_api_image:
            typer.echo(f"Deployed k7-api image: {resolved_api_image}")
        if is_local_installer:
            typer.echo(f"⚠️  Deployed {k7_images.LOCAL_NODE_INSTALLER_IMAGE_REF} built from {os.getcwd()} — unsigned.")
        elif resolved_installer_image:
            typer.echo(f"Remote install on; k7-node-installer image: {resolved_installer_image}")
    else:
        typer.echo(f"❌ Installation failed: {result.error}", err=True)
        raise typer.Exit(1)


@app.command()
def create(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Sandbox name (auto-generated if omitted)"),
    image: str | None = typer.Argument(None, help="Container image (e.g., 'ubuntu:24.04')"),
    config: str | None = typer.Option(None, "-f", "--file", help="Path to k7.yaml config file"),
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
    cpu_limit: str | None = typer.Option(None, "--cpu", help="CPU limit (e.g., '1', '500m')"),
    memory_limit: str | None = typer.Option(None, "--memory", help="Memory limit (e.g., '1Gi', '512Mi')"),
    storage_limit: str | None = typer.Option(
        None, "--storage", help="Kubernetes ephemeral storage limit (tmpfs, emptyDir). Not the root disk."
    ),
    env_file: str | None = typer.Option(None, "--env-file", help="Path to environment file containing secrets"),
    egress_whitelist: list[str] | None = typer.Option(
        None,
        "--egress",
        help=(
            "Egress whitelist entry (CIDR like 10.0.0.0/8 or domain like api.openai.com; "
            "wildcards like *.huggingface.co supported with Cilium CNI). Repeatable. "
            "Default (no --egress/--egress-open): block all egress."
        ),
    ),
    egress_open: bool = typer.Option(
        False,
        "--egress-open",
        help=(
            "Allow ALL egress (no network policy). Mutually exclusive with --egress. "
            "Without this flag and without --egress, all egress is blocked."
        ),
    ),
    ingress_port: list[int] | None = typer.Option(
        None,
        "--ingress-port",
        help=("TCP port to open for inbound traffic. Repeatable. Default (no --ingress-port): deny all ingress."),
    ),
    ingress_from: list[str] | None = typer.Option(
        None,
        "--ingress-from",
        help=(
            "Who may reach --ingress-port: sandbox:<name> | namespace:<ns> | cidr:<cidr>. Repeatable. "
            "Default when --ingress-port is given: sandboxes in the same namespace. "
            "Reaching the sandbox from anywhere needs cidr:0.0.0.0/0, spelled out."
        ),
    ),
    expose_port: list[int] | None = typer.Option(
        None,
        "--expose-port",
        help=(
            "Publish a TCP port outside the cluster through a NodePort Service. Repeatable. "
            "Requires a matching --ingress-port. On a public node the node IP is public too."
        ),
    ),
    before_script: str | None = typer.Option(
        None,
        "--before-script",
        help="Script to run before starting the main container process",
    ),
    pod_non_root: bool | None = typer.Option(
        None,
        "--pod-non-root/--no-pod-non-root",
        help="Run pod with non-root defaults (user/group/fsGroup 65532)",
    ),
    container_non_root: bool | None = typer.Option(
        None,
        "--container-non-root/--no-container-non-root",
        help="Run main container as non-root (uid 65532)",
    ),
    cap_add: list[str] | None = typer.Option(
        None,
        "--cap-add",
        help="Linux capabilities to add back (can be used multiple times)",
    ),
    cap_drop: list[str] | None = typer.Option(
        None,
        "--cap-drop",
        help="Linux capabilities to drop (can be used multiple times)",
    ),
    runtime_class: str | None = typer.Option(
        None,
        "--runtime-class",
        help="Override runtime class (e.g., kata or kata-qemu)",
    ),
    entrypoint: list[str] | None = typer.Option(
        None,
        "--entrypoint",
        help="Override image ENTRYPOINT (repeatable, ordered)",
    ),
    cmd: list[str] | None = typer.Option(
        None,
        "--cmd",
        help="Override image CMD (repeatable, ordered)",
    ),
    root_disk_size: str | None = typer.Option(
        None,
        "--root-disk-size",
        help="Longhorn root disk size, kata-qemu-longhorn backend only (default: tenant defaultSize.disk, else 10Gi)",
    ),
    egress_bps: str | None = typer.Option(
        None, "--egress-bps", help="Cap guest egress, bits/s (e.g. 100M). kfd/kql; may only lower a tenant cap."
    ),
    ingress_bps: str | None = typer.Option(
        None, "--ingress-bps", help="Cap guest ingress, bits/s (e.g. 100M). kfd/kql; may only lower a tenant cap."
    ),
    backend: str | None = typer.Option(
        None,
        "--backend",
        "-b",
        help="Backend: kfd, kql, k7d, or k7d-fc (alias k7-fc) (auto-detected if not specified)",
    ),
    sidecar: str | None = typer.Option(
        None,
        "--sidecar",
        help=(
            "Sidecar daemon type to inject. --sidecar docker is a deprecated alias of "
            "--docker. Other types: see SIDECAR_REGISTRY."
        ),
    ),
    docker: bool = typer.Option(
        False,
        "--docker",
        help=(
            "Run a pinned dockerd (overlay2 on a block graph disk). "
            "k7d/k7d-fc: guest agent service, forkable. Kata (kfd/kql): privileged "
            "docker-vehicle in the VM; kql persists the graph, kfd is ephemeral."
        ),
    ),
    docker_disk: str | None = typer.Option(
        None,
        "--docker-disk",
        help="Docker graph disk size for --docker (e.g. 20Gi, 40Gi). Default 20Gi on all backends.",
    ),
    node: str | None = typer.Option(
        None,
        "--node",
        help="Pin to this Kubernetes node name (`kubectl get nodes` / `k7 nodes list`). Joins via kubernetes.io/hostname (kubelet-stamped). YAML field: node_name.",
    ),
):
    """Create a new sandbox from YAML config or CLI arguments."""
    # Three explicit egress modes. --egress-open → open
    # (no policy, egress_whitelist=None); --egress ... → whitelist; neither
    # → block-all ([]). Combining both is ambiguous — fail loudly.
    if egress_open and egress_whitelist:
        raise typer.BadParameter("--egress-open cannot be combined with --egress (pick one egress mode)")

    # Sources without ports open nothing — the user believes they
    # exposed the sandbox and did not, so fail instead of guessing.
    if ingress_from and not ingress_port:
        raise typer.BadParameter("--ingress-from requires --ingress-port (sources alone open no port)")

    # Auto-detect default config file in current directory when not provided
    if not config:
        for candidate in ("k7.yaml", "k7.yml"):
            if os.path.exists(candidate):
                config = candidate
                break

    if config:
        if not os.path.exists(config):
            raise typer.BadParameter(f"Config file {config} does not exist")

        sandbox_config = SandboxConfig.from_yaml(config)

        if name:
            sandbox_config.name = name
        if image:
            sandbox_config.image = image
        if namespace != "default":
            sandbox_config.namespace = namespace
        if env_file:
            sandbox_config.env_file = env_file
        if egress_open:
            sandbox_config.egress_whitelist = None
        elif egress_whitelist:
            sandbox_config.egress_whitelist = egress_whitelist
        if ingress_port:
            sandbox_config.ingress_ports = ingress_port
        if ingress_from:
            sandbox_config.ingress_from = ingress_from
        if expose_port:
            sandbox_config.expose_ports = expose_port
        if before_script:
            sandbox_config.before_script = before_script
        # Security overrides from CLI take precedence when provided
        if pod_non_root is not None:
            sandbox_config.pod_non_root = pod_non_root
        if container_non_root is not None:
            sandbox_config.container_non_root = container_non_root
        if cap_add is not None:
            sandbox_config.cap_add = cap_add
        if cap_drop is not None:
            sandbox_config.cap_drop = cap_drop
        if runtime_class:
            sandbox_config.runtime_class_name = runtime_class
        if entrypoint is not None:
            sandbox_config.entrypoint = entrypoint
        if cmd is not None:
            sandbox_config.cmd = cmd
        if root_disk_size:
            sandbox_config.root_disk_size = root_disk_size
        if backend:
            sandbox_config.backend = _normalize_backend(backend)
        if sidecar is not None:
            sandbox_config.sidecar = sidecar
        if docker:
            sandbox_config.docker = True
        if docker_disk is not None:
            sandbox_config.docker_disk = docker_disk

        if not sandbox_config.image:
            raise typer.BadParameter("image is required")

        if cpu_limit or memory_limit or storage_limit:
            if not sandbox_config.limits:
                sandbox_config.limits = {}
            if cpu_limit:
                sandbox_config.limits["cpu"] = cpu_limit
            if memory_limit:
                sandbox_config.limits["memory"] = memory_limit
            if storage_limit:
                sandbox_config.limits["ephemeral-storage"] = storage_limit
    else:
        if not name:
            raise typer.BadParameter("Name must be provided via CLI or k7.yaml")

        if not image:
            raise typer.BadParameter("image is required")

        limits = {}
        if cpu_limit:
            limits["cpu"] = cpu_limit
        if memory_limit:
            limits["memory"] = memory_limit
        if storage_limit:
            limits["ephemeral-storage"] = storage_limit

        sandbox_config = SandboxConfig(
            name=name,
            image=image,
            namespace=namespace,
            env_file=env_file,
            egress_whitelist=None if egress_open else (egress_whitelist or []),
            ingress_ports=ingress_port or None,
            ingress_from=ingress_from or None,
            expose_ports=expose_port or None,
            limits=limits if limits else None,
            before_script=before_script or "",
            entrypoint=entrypoint,
            cmd=cmd,
            sidecar=sidecar,
            pod_non_root=pod_non_root if pod_non_root is not None else False,
            container_non_root=container_non_root if container_non_root is not None else False,
            cap_add=cap_add,
            cap_drop=cap_drop,
            runtime_class_name=runtime_class,
            root_disk_size=root_disk_size,
            backend=_normalize_backend(backend) if backend else None,
            docker=docker,
            docker_disk=docker_disk,
        )

    network = {k: v for k, v in (("egressBps", egress_bps), ("ingressBps", ingress_bps)) if v}
    if network:
        sandbox_config.network_limits = {**(sandbox_config.network_limits or {}), **network}

    resolved_backend = _normalize_backend(sandbox_config.backend) if sandbox_config.backend else None
    if root_disk_size and resolved_backend == "kata-firecracker-devmapper":
        typer.echo("⚠️  --root-disk-size has no effect with the kata-firecracker-devmapper backend", err=True)

    if sandbox_config.sidecar == "docker":
        typer.echo("⚠️  --sidecar docker is deprecated; use --docker", err=True)
        sandbox_config.docker = True
        sandbox_config.sidecar = None

    if sandbox_config.docker and sandbox_config.sidecar:
        raise typer.BadParameter("--docker and --sidecar are mutually exclusive until multiple guest services exist")

    if sandbox_config.docker_disk:
        if not sandbox_config.docker:
            raise typer.BadParameter("--docker-disk requires --docker")
        try:
            sandbox_config.docker_disk = parse_docker_disk(sandbox_config.docker_disk)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e

    if sandbox_config.sidecar is not None and sandbox_config.sidecar not in SIDECAR_REGISTRY:
        others = ", ".join(sorted(k for k in SIDECAR_REGISTRY if k != "docker"))
        hint = others if others else "no non-docker sidecar types (--sidecar docker is --docker)"
        raise typer.BadParameter(f"Unknown sidecar type '{sandbox_config.sidecar}'. Available: {hint}")

    if node:
        sandbox_config.node_name = node

    core = K7Core()
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
    )
    stage_task = progress.add_task("Starting...", total=None)
    status_text = Text("Starting...", style="cyan")
    details_text = Text("")

    def on_progress(event: dict):
        try:
            stage = event.get("stage")
            status = event.get("status")
            if stage == "provisioning":
                if status == "start":
                    status_text.plain = "Provisioning deployment"
                    status_text.stylize("cyan")
                    progress.update(
                        stage_task,
                        description="[cyan]Provisioning deployment[/cyan]",
                        total=None,
                    )
                elif status == "done":
                    status_text.plain = "Provisioned"
                    status_text.stylize("green")
                    progress.update(stage_task, description="[green]Provisioned[/green]", total=None)
            elif stage == "before_script":
                if status == "waiting":
                    script = event.get("script", "")
                    first_line = script.strip().split("\n")[0]
                    shown = first_line if len(first_line) <= 200 else first_line[:200] + "..."
                    status_text.plain = "Running before script"
                    status_text.stylize("yellow")
                    details_text.plain = shown
                    details_text.stylize("dim")
                    progress.update(
                        stage_task,
                        description="[yellow]Running before script...[/yellow]",
                        total=None,
                    )
                elif status == "done":
                    status_text.plain = "Before script completed"
                    status_text.stylize("green")
                    details_text.plain = ""
                    progress.update(
                        stage_task,
                        description="[green]Before script completed[/green]",
                        total=None,
                    )
                elif status == "skipped":
                    status_text.plain = "No before script"
                    status_text.stylize("dim")
                    details_text.plain = ""
                    progress.update(
                        stage_task,
                        description="[dim]No before script[/dim]",
                        total=None,
                    )
            elif stage == "network_lockdown":
                if status == "applying":
                    status_text.plain = "Applying egress policy..."
                    status_text.stylize("yellow")
                    progress.update(
                        stage_task,
                        description="[yellow]Applying egress policy...[/yellow]",
                        total=None,
                    )
                elif status == "done":
                    status_text.plain = "Egress policy applied"
                    status_text.stylize("green")
                    progress.update(
                        stage_task,
                        description="[green]Egress policy applied[/green]",
                        total=None,
                    )
                elif status == "skipped":
                    status_text.plain = "No egress policy"
                    status_text.stylize("dim")
                    progress.update(
                        stage_task,
                        description="[dim]No egress policy[/dim]",
                        total=None,
                    )
            elif stage == "complete":
                msg = event.get("message", "Sandbox created")
                status_text.plain = msg
                status_text.stylize("green")
                details_text.plain = ""
                progress.update(stage_task, description=f"[green]{msg}[/green]", total=None)
            elif stage == "error":
                err = event.get("error", "")
                status_text.plain = f"Error: {err}"
                status_text.stylize("red")
                progress.update(stage_task, description=f"[red]Error: {err}[/red]", total=None)
        except Exception:
            pass

    from rich.console import Group as RichGroup
    from rich.live import Live as RichLive

    # Show key YAML parameters up-front for clarity
    try:
        egress_mode = (
            "open"
            if sandbox_config.egress_whitelist is None
            else (
                "block_all" if sandbox_config.egress_whitelist == [] else f"whitelist={sandbox_config.egress_whitelist}"
            )
        )
    except Exception:
        egress_mode = "unknown"

    try:
        cap_drop_display = (
            "ALL (default)" if getattr(sandbox_config, "cap_drop", None) is None else sandbox_config.cap_drop
        )
    except Exception:
        cap_drop_display = "unknown"

    # A NodePort on a Hetzner dedicated box lands on a public IP: say so on
    # every create, not just once in the docs.
    if sandbox_config.expose_ports and "cidr:0.0.0.0/0" in (sandbox_config.ingress_from or []):
        typer.echo(
            f"⚠️  {sandbox_config.name} is exposed on ports {sandbox_config.expose_ports} to cidr:0.0.0.0/0 — "
            "on a public node this publishes the sandbox to the internet.",
            err=True,
        )

    if sandbox_config.ingress_ports:
        ingress_mode = (
            f"ports={sandbox_config.ingress_ports} from="
            f"{sandbox_config.ingress_from or ['sandboxes in this namespace']}"
        )
    else:
        ingress_mode = "deny_all"

    expose_mode = f"NodePort for {sandbox_config.expose_ports}" if sandbox_config.expose_ports else "none"

    # Measured on Cilium: an ipBlock peer is not evaluated for pod-to-pod
    # traffic, so a rule carrying one degrades to a port-only allow in-cluster.
    # The flag reads like a restriction, so say plainly that it is not one.
    if any((s or "").strip().startswith("cidr:") for s in sandbox_config.ingress_from or []):
        typer.echo(
            f"⚠️  {sandbox_config.name}: a cidr: source does not restrict in-cluster peers on Cilium — "
            f"ports {sandbox_config.ingress_ports} are reachable from any sandbox in the cluster. "
            "Use sandbox:<name> or namespace:<ns> to scope in-cluster access.",
            err=True,
        )

    params_lines = [
        f"Image: {sandbox_config.image}",
        f"Namespace: {sandbox_config.namespace}",
        f"Egress: {egress_mode}",
        f"Ingress: {ingress_mode}",
        f"Expose: {expose_mode}",
        f"Before script: {'present' if (sandbox_config.before_script or '').strip() else 'none'}",
        f"Pod non-root: {getattr(sandbox_config, 'pod_non_root', False)}",
        f"Container non-root: {getattr(sandbox_config, 'container_non_root', False)}",
        f"Cap drop: {cap_drop_display}",
        f"Cap add: {getattr(sandbox_config, 'cap_add', [])}",
        f"Limits: {sandbox_config.limits if sandbox_config.limits else {}}",
    ]
    # Print summary statically so it persists regardless of Live updates
    try:
        console = Console()
        for line in params_lines:
            console.print(line)
    except Exception:
        pass
    # Also seed details pane initially
    details_text.plain = "\n".join(params_lines)
    details_text.stylize("dim")

    # Prepare before_script log streaming (started when core signals 'waiting')
    kubectl_cmd = _kubectl_cmd()
    stop_log_event: threading.Event = threading.Event()
    log_thread: threading.Thread | None = None
    log_started_event: threading.Event = threading.Event()
    log_ended_event: threading.Event = threading.Event()
    before_log_lines: builtins.list[str] = []

    resolved_pod_name: str | None = None

    def _stream_before_script_logs():
        pod_name = None
        # Resolve pod name with retries
        for _ in range(60):
            if stop_log_event.is_set():
                return
            try:
                proc = subprocess.run(
                    kubectl_cmd
                    + [
                        "get",
                        "pods",
                        "-n",
                        sandbox_config.namespace,
                        "-l",
                        f"app={sandbox_config.name}",
                        "-o",
                        "jsonpath={.items[0].metadata.name}",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                pod_name = (proc.stdout or "").strip()
                if pod_name:
                    nonlocal resolved_pod_name
                    resolved_pod_name = pod_name
                    break
            except Exception:
                pass
            time.sleep(1)
        if not pod_name or stop_log_event.is_set():
            return

        # Announce start
        try:
            from rich.text import Text as RichText

            live.console.print(RichText(f"===== START before_script ({sandbox_config.name}) =====", style="bold"))
            log_started_event.set()
        except Exception:
            pass

        # Keep attempting to attach to logs until stop is requested
        while not stop_log_event.is_set():
            p = None
            try:
                p = subprocess.Popen(
                    kubectl_cmd
                    + [
                        "logs",
                        pod_name,
                        "-n",
                        sandbox_config.namespace,
                        "--container",
                        "sandbox",
                        "-f",
                        "--since",
                        "10m",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert p.stdout is not None
                for line in p.stdout:
                    if stop_log_event.is_set():
                        break
                    # Filter noisy startup message while container is creating
                    if "is waiting to start: ContainerCreating" in line:
                        continue
                    try:
                        live.console.print(line.rstrip())
                    except Exception:
                        pass
                    try:
                        before_log_lines.append(line)
                    except Exception:
                        pass
                # If process exited quickly and we haven't been asked to stop, retry
                if stop_log_event.is_set():
                    break
                # Small backoff before retrying
                time.sleep(1)
            except Exception:
                time.sleep(1)
            finally:
                try:
                    if p and p.poll() is None:
                        p.terminate()
                except Exception:
                    pass
        try:
            from rich.text import Text as RichText

            live.console.print(RichText(f"===== END before_script ({sandbox_config.name}) =====", style="bold"))
            log_ended_event.set()
        except Exception:
            pass

    def on_progress(event: dict):  # noqa: F811
        try:
            stage = event.get("stage")
            status = event.get("status")
            if stage == "provisioning":
                if status == "start":
                    status_text.plain = "Provisioning deployment"
                    status_text.stylize("cyan")
                    progress.update(
                        stage_task,
                        description="[cyan]Provisioning deployment[/cyan]",
                        total=None,
                    )
                elif status == "done":
                    status_text.plain = "Provisioned"
                    status_text.stylize("green")
                    progress.update(stage_task, description="[green]Provisioned[/green]", total=None)
            elif stage == "before_script":
                if status == "waiting":
                    script = event.get("script", "")
                    first_line = script.strip().split("\n")[0]
                    shown = first_line if len(first_line) <= 200 else first_line[:200] + "..."
                    status_text.plain = "Running before script"
                    status_text.stylize("yellow")
                    details_text.plain = shown
                    details_text.stylize("dim")
                    progress.update(
                        stage_task,
                        description="[yellow]Running before script...[/yellow]",
                        total=None,
                    )
                    # Start log streaming once when before_script begins
                    nonlocal log_thread
                    if log_thread is None or not log_thread.is_alive():
                        log_thread = threading.Thread(target=_stream_before_script_logs, daemon=True)
                        log_thread.start()
                elif status == "done":
                    status_text.plain = "Before script completed"
                    status_text.stylize("green")
                    # Persist a summary of captured before_script logs below the bar
                    if before_log_lines:
                        try:
                            tail = "".join(before_log_lines[-50:]).rstrip()
                            details_text.plain = "Before script log tail (last 50 lines):\n" + tail
                            details_text.stylize("dim")
                        except Exception:
                            details_text.plain = ""
                    else:
                        details_text.plain = ""
                    progress.update(
                        stage_task,
                        description="[green]Before script completed[/green]",
                        total=None,
                    )
                    stop_log_event.set()
                elif status == "skipped":
                    status_text.plain = "No before script"
                    status_text.stylize("dim")
                    details_text.plain = ""
                    progress.update(
                        stage_task,
                        description="[dim]No before script[/dim]",
                        total=None,
                    )
                    stop_log_event.set()
            elif stage == "network_lockdown":
                if status == "applying":
                    status_text.plain = "Applying egress policy..."
                    status_text.stylize("yellow")
                    progress.update(
                        stage_task,
                        description="[yellow]Applying egress policy...[/yellow]",
                        total=None,
                    )
                elif status == "done":
                    status_text.plain = "Egress policy applied"
                    status_text.stylize("green")
                    progress.update(
                        stage_task,
                        description="[green]Egress policy applied[/green]",
                        total=None,
                    )
                elif status == "skipped":
                    status_text.plain = "No egress policy"
                    status_text.stylize("dim")
                    progress.update(
                        stage_task,
                        description="[dim]No egress policy[/dim]",
                        total=None,
                    )
            elif stage == "complete":
                msg = event.get("message", "Sandbox created")
                # Avoid duplicating the success message above the progress line
                status_text.plain = ""
                # Ensure END banner printed if logs started but not ended
                if log_started_event.is_set() and not log_ended_event.is_set():
                    try:
                        from rich.text import Text as RichText

                        live.console.print(
                            RichText(f"===== END before_script ({sandbox_config.name}) =====", style="bold")
                        )
                    except Exception:
                        pass
                # Keep the log tail summary visible after completion
                progress.update(stage_task, description=f"[green]{msg}[/green]", total=None)
                stop_log_event.set()
                # Do not attempt any file-based fallbacks; only stream container logs
            elif stage == "error":
                err = event.get("error", "")
                status_text.plain = f"Error: {err}"
                status_text.stylize("red")
                progress.update(stage_task, description=f"[red]Error: {err}[/red]", total=None)
                stop_log_event.set()
        except Exception:
            pass

    cli_ctx: CliContext = ctx.obj
    endpoints: builtins.list[dict] = []
    if cli_ctx.use_core:
        group = RichGroup(status_text, details_text, progress)
        with RichLive(group, refresh_per_second=8, transient=False) as live:
            result = _core_run(core, core.create_sandbox(sandbox_config, progress_callback=on_progress))
        if not result.success:
            typer.echo(f"❌ Failed to create sandbox: {result.error}", err=True)
            raise typer.Exit(1)
        endpoints = (result.data or {}).get("endpoints") or []
        sandboxes_for_ready = _core_run(core, core.list_sandboxes(sandbox_config.namespace))
        target_ready = any(s.name == sandbox_config.name and s.ready == "True" for s in sandboxes_for_ready)
    else:
        # The API doesn't stream progress yet (a separate spec). Print a
        # one-shot status line, then call the SDK. The server-side handler
        # waits for the pod to become Ready before responding, so by the
        # time we get a result back the sandbox is up.
        typer.echo(f"Creating sandbox {sandbox_config.name} via API ...")
        handle_api_call(lambda: cli_ctx.client().create(sandbox_config.to_dict()))
        sandboxes = handle_api_call(lambda: cli_ctx.client().list(namespace=sandbox_config.namespace))
        target_ready = any(s.get("name") == sandbox_config.name and s.get("ready") == "True" for s in sandboxes)
        if sandbox_config.expose_ports:
            # The SDK's create() returns a proxy, so the API path cannot show a
            # resolved URL here. Point at the NodePorts instead of guessing one.
            typer.echo(f"🌐 Exposed ports: see the NodePorts column in `k7 list` (sandbox {sandbox_config.name})")
    for endpoint in endpoints:
        typer.echo(f"🌐 Port {endpoint['port']} exposed at {endpoint['url']}")
    if target_ready:
        typer.echo(f"✅ Sandbox {sandbox_config.name} created.")
        typer.echo(
            f"👉 Shell in: k7 shell {sandbox_config.name}"
            + (f" -n {sandbox_config.namespace}" if sandbox_config.namespace != "default" else "")
        )
    else:
        typer.echo(
            f"✅ Sandbox {sandbox_config.name} created. Check status with:"
            + (f" k7 list -n {sandbox_config.namespace}" if sandbox_config.namespace != "default" else " k7 list")
        )
        typer.echo(
            f"👉 When Ready, run: k7 shell {sandbox_config.name}"
            + (f" -n {sandbox_config.namespace}" if sandbox_config.namespace != "default" else "")
        )


@app.command()
def list(
    ctx: typer.Context,
    namespace: str | None = typer.Option(
        None,
        "-n",
        "--namespace",
        help="Filter sandboxes by namespace. If not provided, shows sandboxes from all namespaces.",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Filter sandboxes by exact name.",
    ),
):
    """List all running sandboxes."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        sandboxes = [s.to_dict() for s in _core_run(core, core.list_sandboxes(namespace))]
    else:
        sandboxes = handle_api_call(lambda: cli_ctx.client().list(namespace=namespace))

    if name:
        sandboxes = [s for s in sandboxes if s.get("name") == name]

    if not sandboxes:
        if namespace:
            if name:
                typer.echo(f"No sandbox named '{name}' found in namespace '{namespace}'.")
            else:
                typer.echo(f"No sandboxes found in namespace '{namespace}'.")
        else:
            if name:
                typer.echo(f"No sandbox named '{name}' found.")
            else:
                typer.echo("No sandboxes found.")
        return

    console = Console()
    table = Table(title="K7 Sandboxes")
    table.add_column("Name", style="cyan")
    table.add_column("Namespace", style="blue")
    table.add_column("Status", justify="center")
    table.add_column("Ready", justify="center")
    table.add_column("Restarts", justify="center")
    table.add_column("Age")
    table.add_column("Image", style="green")
    table.add_column("Backend", style="magenta")
    table.add_column("Node", style="yellow")
    table.add_column("NodePorts", style="yellow")
    table.add_column("Error", style="red")

    for sandbox in sandboxes:
        status_raw = sandbox.get("status", "")
        ready_raw = sandbox.get("ready", "")
        if status_raw == "Running":
            status_display = f"[green]{status_raw}[/green]"
        elif status_raw == "Pending":
            status_display = f"[yellow]{status_raw}[/yellow]"
        elif status_raw == "Failed":
            status_display = f"[red]{status_raw}[/red]"
        else:
            status_display = status_raw

        ready_display = f"[green]{ready_raw}[/green]" if ready_raw == "True" else f"[red]{ready_raw}[/red]"

        table.add_row(
            sandbox.get("name", ""),
            sandbox.get("namespace", ""),
            status_display,
            ready_display,
            str(sandbox.get("restarts", 0)),
            sandbox.get("age", ""),
            sandbox.get("image", ""),
            sandbox.get("backend", ""),
            sandbox.get("node", ""),
            ",".join(str(p) for p in sandbox.get("node_ports") or []),
            sandbox.get("error_message", ""),
        )

    console.print(table)


@app.command()
def delete(
    ctx: typer.Context,
    name: str,
    namespace: str = typer.Option(
        "default",
        "-n",
        "--namespace",
        help="Kubernetes namespace containing the sandbox.",
    ),
):
    """Delete a sandbox and all its associated resources."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(core, core.delete_sandbox(name, namespace))
        if not result.success:
            typer.echo(f"❌ Failed to delete sandbox: {result.error}", err=True)
            raise typer.Exit(1)
        typer.echo(f"✅ {result.message}")
        return
    data = handle_api_call(lambda: cli_ctx.client().delete(name, namespace=namespace))
    typer.echo(f"✅ {data.get('message', f'Sandbox {name} deleted')}")


@app.command()
def delete_all(
    ctx: typer.Context,
    namespace: str = typer.Option(
        "default",
        "-n",
        "--namespace",
        help="Kubernetes namespace to delete sandboxes from.",
    ),
):
    """Delete all sandboxes in a namespace."""
    cli_ctx: CliContext = ctx.obj
    # Always show the user the list + a confirm prompt before destroying. List
    # goes through the same routing as `k7 list` so off-cluster runs work.
    if cli_ctx.use_core:
        core = K7Core()
        sandboxes = [s.to_dict() for s in _core_run(core, core.list_sandboxes(namespace))]
    else:
        sandboxes = handle_api_call(lambda: cli_ctx.client().list(namespace=namespace))

    if not sandboxes:
        typer.echo(f"No sandboxes found in namespace {namespace}")
        return

    sandbox_names = [s.get("name", "") for s in sandboxes]
    typer.echo(f"Found {len(sandbox_names)} sandbox(es) in namespace {namespace}:")
    for n in sandbox_names:
        typer.echo(f"  - {n}")

    if not typer.confirm("Are you sure you want to delete all these sandboxes?"):
        typer.echo("Deletion cancelled")
        return

    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(core, core.delete_all_sandboxes(namespace))
        if result.success:
            typer.echo(f"✅ {result.message}")
            return
        typer.echo(f"❌ Failed to delete all sandboxes: {result.error}", err=True)
        if result.data:
            for item in result.data:
                if not item["success"]:
                    typer.echo(f"  - {item['name']}: {item['error']}")
        raise typer.Exit(1)
    data = handle_api_call(lambda: cli_ctx.client().delete_all(namespace=namespace))
    typer.echo(f"✅ {data.get('message', 'Deleted all sandboxes')}")


@app.command()
def shell(
    name: str,
    namespace: str = typer.Option(
        "default",
        "-n",
        "--namespace",
        help="Kubernetes namespace containing the sandbox.",
    ),
):
    """Shell into sandbox (bypasses network policy)."""
    core = K7Core()
    result = core.shell_into_sandbox(name, namespace)
    if not result.success:
        typer.echo(f"Error: {result.error}", err=True)
        raise typer.Exit(1)


@app.command()
def logs(
    ctx: typer.Context,
    name: str,
    namespace: str = typer.Option(
        "default",
        "-n",
        "--namespace",
        help="Kubernetes namespace containing the sandbox.",
    ),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow logs output (--core only)"),
    tail: int = typer.Option(200, "--tail", help="Number of lines to show from the end of the logs"),
):
    """Show sandbox pod logs (before script and main container).

    Default route is the K7 API (a snapshot, no streaming). For live follow,
    pass ``--core`` to run against ``kubectl logs -f`` directly on the node.
    """
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        kubectl_cmd = _kubectl_cmd()
        try:
            pod_name_proc = subprocess.run(
                kubectl_cmd
                + [
                    "get",
                    "pods",
                    "-n",
                    namespace,
                    "-l",
                    f"app={name}",
                    "-o",
                    "jsonpath={.items[0].metadata.name}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            pod_name = pod_name_proc.stdout.strip()
        except subprocess.CalledProcessError as e:
            typer.echo(f"❌ Failed to resolve pod for sandbox '{name}': {e.stderr.strip()}", err=True)
            raise typer.Exit(1) from None
        if not pod_name:
            typer.echo(f"❌ No pod found for sandbox '{name}' in namespace '{namespace}'.", err=True)
            raise typer.Exit(1)
        args = ["logs", pod_name, "-n", namespace, "--tail", str(tail)]
        if follow:
            args.append("-f")
        subprocess.run(kubectl_cmd + args)
        return

    if follow:
        typer.echo(
            "⚠️  --follow is not supported via the API yet; printing a snapshot instead. "
            "Use `k7 --core logs --follow ...` on the node for live tailing.",
            err=True,
        )
    text = handle_api_call(lambda: cli_ctx.client().logs(name, namespace=namespace, tail=tail))
    if text:
        typer.echo(text, nl=False)


@app.command()
def exec(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Sandbox name"),
    command: builtins.list[str] = typer.Argument(..., help="Shell command to run (joined with spaces)"),
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
):
    """Run a shell command in a sandbox; prints stdout, exits with the command's exit code.

    Buffered (non-streaming) — large outputs are capped server-side. For
    interactive shells use ``k7 shell`` (currently --core only; WebSocket
    proxy is a follow-up).
    """
    joined = " ".join(command)
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(core, core.exec_command(name, joined, namespace=namespace))
        data = result.to_dict()
    else:
        data = handle_api_call(lambda: cli_ctx.client().exec(name, joined, namespace=namespace))
    if data.get("stdout"):
        typer.echo(data["stdout"], nl=False)
    if data.get("stderr"):
        typer.echo(data["stderr"], err=True, nl=False)
    raise typer.Exit(int(data.get("exit_code", 0) or 0))


@app.command()
def top(
    refresh_interval: int = 1,
    namespace: str | None = typer.Option(
        None,
        "-n",
        "--namespace",
        help="Filter sandboxes by namespace. If not provided, shows sandboxes from all namespaces.",
    ),
):
    """Dynamic top-like view of sandbox resource usage (CPU, Memory). Press Ctrl+C to exit."""
    core = K7Core()

    def generate_table() -> Table:
        table = Table(title="K7 Sandboxes Resource Usage")
        table.add_column("Name", style="cyan")
        table.add_column("Namespace", style="blue")
        table.add_column("CPU Usage (cores)")
        table.add_column("Memory Usage (MiB)")

        metrics_list = _core_run(core, core.get_sandbox_metrics(namespace))

        for metric in metrics_list:
            sb_name = metric["name"]
            sb_namespace = metric["namespace"]
            cpu_usage = metric["cpu_usage"]
            mem_usage = metric["memory_usage"]

            cpu_usage_str = "[dim]N/A[/dim]"
            mem_usage_str = "[dim]N/A[/dim]"

            # CPU Parsing
            try:
                cpu_usage_n = 0
                if cpu_usage.endswith("n"):
                    cpu_usage_n = int(cpu_usage[:-1])
                elif cpu_usage.endswith("u"):
                    cpu_usage_n = int(cpu_usage[:-1]) * 1000
                elif cpu_usage.endswith("m"):
                    cpu_usage_n = int(cpu_usage[:-1]) * 1000 * 1000
                else:
                    cpu_usage_n = int(cpu_usage) * 1000 * 1000 * 1000

                cpu_usage_cores = cpu_usage_n / (1000 * 1000 * 1000)

                # Colorize CPU usage based on thresholds
                if cpu_usage_cores >= 0.8:
                    cpu_usage_str = f"[red]{cpu_usage_cores:.3f}[/red]"
                elif cpu_usage_cores >= 0.5:
                    cpu_usage_str = f"[yellow]{cpu_usage_cores:.3f}[/yellow]"
                else:
                    cpu_usage_str = f"[green]{cpu_usage_cores:.3f}[/green]"
            except (ValueError, TypeError):
                cpu_usage_str = "[red]Invalid[/red]"

            # Memory Parsing
            try:
                mem_usage_mib = 0.0
                if mem_usage.endswith("Ki"):
                    mem_usage_mib = int(mem_usage[:-2]) / 1024.0
                elif mem_usage.endswith("Mi"):
                    mem_usage_mib = float(mem_usage[:-2])
                elif mem_usage.endswith("Gi"):
                    mem_usage_mib = float(mem_usage[:-2]) * 1024.0
                else:
                    mem_usage_mib = int(mem_usage) / (1024.0 * 1024.0)

                # Colorize memory usage based on thresholds
                if mem_usage_mib >= 1500:  # High usage (1.5GB+)
                    mem_usage_str = f"[red]{mem_usage_mib:.2f}[/red]"
                elif mem_usage_mib >= 500:  # Medium usage (500MB+)
                    mem_usage_str = f"[yellow]{mem_usage_mib:.2f}[/yellow]"
                else:  # Low/very low usage
                    mem_usage_str = f"[green]{mem_usage_mib:.2f}[/green]"
            except (ValueError, TypeError):
                mem_usage_str = "[red]Invalid[/red]"

            table.add_row(sb_name, sb_namespace, cpu_usage_str, mem_usage_str)

        return table

    with Live(auto_refresh=False) as live:
        while True:
            live.update(generate_table(), refresh=True)
            time.sleep(refresh_interval)


def _dedupe_keep_order(values: builtins.list[str] | None) -> builtins.list[str]:
    """Preserve first-seen order; drop empties and duplicates."""
    seen: set[str] = set()
    out: builtins.list[str] = []
    for v in values or []:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


_API_FLAG_HELP = "Go through k7-api (admin key or `k7 login`) instead of kubectl on a server node."


@app.command()
def generate_api_key(
    ctx: typer.Context,
    name: str,
    role: str = typer.Option(..., "--role", help=f"One of {', '.join(api_keys_store.ROLES)}."),
    tenant: str | None = typer.Option(
        None, "--tenant", help="Tenant for read-only/user; forbidden for operator/admin."
    ),
    expires_days: int = typer.Option(365, help="API key expiration in days"),
    namespace: builtins.list[str] | None = typer.Option(
        None,
        "--namespace",
        "-n",
        help="Further restrict the key to this namespace (repeatable).",
    ),
    node: builtins.list[str] | None = typer.Option(
        None,
        "--node",
        help="Restrict key to this Kubernetes node name (repeatable; copy from `k7 nodes list`). Placement uses kubernetes.io/hostname (kubelet-stamped, not inventory). Omit for unrestricted placement.",
    ),
    use_api: bool = typer.Option(False, "--api", help=_API_FLAG_HELP),
):
    """Generate a new API key with a role (k7-api-keys Secret; kubectl on a node, or --api)."""
    scope = {
        "role": role,
        "tenant": tenant,
        "expires_days": expires_days,
        "namespaces": _dedupe_keep_order(namespace),
        "nodes": _dedupe_keep_order(node),
    }
    if use_api:
        cli_ctx: CliContext = ctx.obj
        entry = handle_api_call(lambda: cli_ctx.client().create_api_key(name, **scope))
        token, key_id = entry["token"], entry["id"]
    else:
        token, key_id, entry = _key_store_call(api_keys_store.generate, name, **scope)
    typer.echo(f"Generated API key for '{name}' (id {key_id}):")
    typer.echo(f"API Key: {token}")
    typer.echo(f"Role: {entry['role']}" + (f" on tenant {entry['tenant']}" if entry.get("tenant") else ""))
    typer.echo(f"Expires: {datetime.fromtimestamp(entry['expires'])}")
    if entry.get("namespaces"):
        typer.echo(f"Namespaces: {', '.join(entry['namespaces'])}")
    typer.echo(f"Nodes: {', '.join(entry['nodes'])}" if entry.get("nodes") else "Nodes: * (unrestricted)")
    typer.echo("Keep this key secure - it won't be shown again!")


def _read_keys_and_usage(kc: "api_keys_store.Kubectl") -> tuple[dict, dict]:
    keys = api_keys_store.decode_keys(kc.read(api_keys_store.KEYS_SECRET) or {})
    return keys, kc.read(api_keys_store.USAGE_SECRET) or {}


@app.command()
def list_api_keys(ctx: typer.Context, use_api: bool = typer.Option(False, "--api", help=_API_FLAG_HELP)):
    """List all API keys (legacy pre-0.5 and unscoped keys from before roles are flagged)."""
    if use_api:
        cli_ctx: CliContext = ctx.obj
        rows = handle_api_call(lambda: cli_ctx.client().list_api_keys())
        keys = {r["id"]: r for r in rows}
        usage = {r["id"]: r["last_used"] for r in rows if r.get("last_used")}
    else:
        keys, usage = _key_store_call(_read_keys_and_usage)
    if not keys:
        typer.echo("No API keys found.")
        return

    console = Console()
    table = Table(title="API Keys")
    for col, style in [
        ("ID", "white"),
        ("Name", "cyan"),
        ("Created", "blue"),
        ("Expires", "yellow"),
        ("Last Used", "green"),
        ("Role", "red"),
        ("Tenant", "magenta"),
        ("Namespaces", "magenta"),
        ("Nodes", "magenta"),
    ]:
        table.add_column(col, style=style)

    def _ts(v) -> str:
        return datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M") if v else "Never"

    for key_id, key_data in keys.items():
        table.add_row(
            key_id,
            key_data["name"],
            _ts(key_data.get("created")),
            _ts(key_data.get("expires")),
            _ts(usage.get(key_id)),
            key_data.get("role") or "NONE (run `k7 api keys migrate`)",
            key_data.get("tenant") or "-",
            ", ".join(key_data.get("namespaces") or []) or "*",
            ", ".join(key_data.get("nodes") or []) or "*",
        )
    console.print(table)
    unscoped = [k for k, v in keys.items() if v.get("unscoped_legacy")]
    if unscoped:
        typer.echo(
            f"⚠️  {len(unscoped)} unscoped key(s) from before roles ({', '.join(unscoped)}) were migrated to role admin "
            "(development mode only). Replace them with tenant-scoped keys: "
            "`k7 generate-api-key --role user --tenant <t>` + `k7 revoke-api-key`."
        )
    legacy = [k for k in keys if k.startswith(api_keys_store.LEGACY_PREFIX)]
    if legacy:
        typer.echo(
            f"⚠️  {len(legacy)} legacy key(s) ({', '.join(legacy)}): pre-k7_<id>_<secret> format, matched by hash "
            "scan until they expire. Rotate with `k7 generate-api-key` + `k7 revoke-api-key`."
        )


@app.command()
def revoke_api_key(ctx: typer.Context, name: str, use_api: bool = typer.Option(False, "--api", help=_API_FLAG_HELP)):
    """Revoke every API key with this name (takes effect within seconds on all API replicas)."""
    if use_api:
        cli_ctx: CliContext = ctx.obj
        ids = handle_api_call(lambda: cli_ctx.client().revoke_api_key(name))["revoked"]
    else:
        ids = _key_store_call(api_keys_store.revoke, name)
    if ids:
        typer.echo(f"API key '{name}' revoked successfully ({', '.join(ids)}).")
    else:
        typer.echo(f"API key '{name}' not found.")


def _login_url(ctx: typer.Context, api: str | None) -> str:
    url = (api or _resolve_api_url(ctx.obj.api_url) or "").rstrip("/")
    if not url:
        typer.echo("❌ Pass --api https://<k7-api> (or set api.url).", err=True)
        raise typer.Exit(1)
    return url


@app.command()
def login(
    ctx: typer.Context,
    api: str | None = typer.Option(None, "--api", help="K7 API URL (default: --api-url / K7_API_URL / api.url)."),
    browser: bool = typer.Option(False, "--browser", help="Authorization Code + PKCE in a local browser."),
    scopes: str = typer.Option(_oidc.DEFAULT_SCOPES, "--scopes", help="OAuth scopes to request."),
):
    """Sign in through the API's OIDC IdP (device flow by default; works over SSH)."""
    url = _login_url(ctx, api)
    verify = verify_ssl_for_url(url, _resolve_api_ca(ctx.obj.api_ca))
    try:
        r = requests.get(f"{url}/api/v1/auth/oidc", timeout=15, verify=verify)
        if r.status_code >= 400:
            raise RuntimeError(f"{url} returned HTTP {r.status_code}: {r.text[:300]}")
        _oidc.login(url, r.json()["data"], browser=browser, scopes=scopes, echo=lambda m: typer.echo(m))
    except (RuntimeError, requests.RequestException) as e:
        typer.echo(f"❌ Login failed: {e}", err=True)
        raise typer.Exit(1)
    me = handle_api_call(
        lambda: Client(endpoint=url, token_provider=_oidc.token_provider(url), verify_ssl=verify).whoami()
    )
    typer.echo(f"✅ Logged in to {url} as {me['subject']} ({'; '.join(me['grants'])})")


@app.command()
def logout(
    ctx: typer.Context,
    api: str | None = typer.Option(None, "--api", help="K7 API URL (default: --api-url / K7_API_URL / api.url)."),
):
    """Delete the cached OIDC login for this API and revoke it at the IdP when supported."""
    try:
        typer.echo(_oidc.logout(_login_url(ctx, api)))
    except (RuntimeError, requests.RequestException) as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(1)


@app.command()
def whoami(ctx: typer.Context):
    """Show the principal (API key or OIDC subject) and the roles the API grants it."""
    me = handle_api_call(lambda: ctx.obj.client().whoami())
    typer.echo(f"{me['type']} {me['subject']}: {'; '.join(me['grants'])}")
    if me.get("groups"):
        typer.echo(f"groups: {', '.join(me['groups'])}")


_USAGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _usage_time(raw: str, now: float) -> float:
    """``now``, a duration ago (``24h``, ``7d``), unix seconds, or RFC 3339 with an offset."""
    m = re.fullmatch(r"([0-9]+)([smhd])", raw)
    if raw == "now":
        return now
    if m:
        return now - int(m.group(1)) * _USAGE_UNITS[m.group(2)]
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", raw):
        return float(raw)
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        dt = None
    if dt is None or dt.tzinfo is None:
        raise typer.BadParameter(f"{raw!r}: use now, a duration like 24h/7d, unix seconds or RFC 3339 with an offset")
    return dt.timestamp()


@app.command()
def usage(
    ctx: typer.Context,
    tenant: str | None = typer.Option(None, "--tenant", help="One tenant (a tenant key must name its own)"),
    since: str = typer.Option("24h", "--from", help="Start: a duration ago (24h, 7d), unix seconds or RFC 3339"),
    until: str = typer.Option("now", "--to", help="End: now, a duration ago, unix seconds or RFC 3339"),
    group_by: str = typer.Option("tenant", "--group-by", help="Comma-separated: tenant,namespace,backend,state"),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
):
    """Showback from Prometheus: sandbox/vCPU/memory seconds, egress bytes, disk and snapshot GiB·s."""
    if ctx.obj.use_core:
        typer.echo("❌ k7 usage queries Prometheus through k7-api; drop --core", err=True)
        raise typer.Exit(1)
    now = time.time()
    start, end = _usage_time(since, now), _usage_time(until, now)
    data = handle_api_call(lambda: ctx.obj.client().usage(start, end, tenant=tenant, group_by=group_by))
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    table = Table(
        title=f"K7 usage {datetime.fromtimestamp(start):%Y-%m-%d %H:%M} → {datetime.fromtimestamp(end):%Y-%m-%d %H:%M}"
    )
    cols = [*showback.COUNTERS, *showback.GAUGES]
    for col in [*data["group_by"], *cols]:
        table.add_column(col.upper(), justify="left" if col in data["group_by"] else "right")
    for row in data["rows"]:
        table.add_row(
            *[str(row[g]) for g in data["group_by"]], *[f"{row[c]:,.3f}".rstrip("0").rstrip(".") for c in cols]
        )
    Console().print(table)


role_bindings_app = typer.Typer(
    help="OIDC group/user/subject → role bindings (ConfigMap k7-role-bindings; admin only).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(role_bindings_app, name="role-bindings")


@role_bindings_app.command("get")
def role_bindings_get(ctx: typer.Context):
    """Print the current bindings and deny-subjects as YAML (input for `apply -f`)."""
    data = handle_api_call(lambda: ctx.obj.client().get_role_bindings())
    typer.echo(yaml.safe_dump(data, sort_keys=False), nl=False)


@role_bindings_app.command("apply")
def role_bindings_apply(
    ctx: typer.Context,
    file: Path = typer.Option(
        ..., "-f", "--file", exists=True, dir_okay=False, help="YAML with bindings/deny_subjects"
    ),
):
    """Replace all bindings and deny-subjects with the contents of FILE."""
    data = yaml.safe_load(file.read_text()) or {}
    if not isinstance(data, dict) or set(data) - {"bindings", "deny_subjects"} or "bindings" not in data:
        typer.echo("❌ FILE must be a mapping with `bindings` (list) and optional `deny_subjects` (list).", err=True)
        raise typer.Exit(1)
    out = handle_api_call(lambda: ctx.obj.client().put_role_bindings(data["bindings"], data.get("deny_subjects") or []))
    typer.echo(f"✅ Applied {len(out['bindings'])} binding(s), {len(out['deny_subjects'])} deny-subject(s).")


_PAUSE_SNAPSHOT_AUTO = "__auto__"


def _rewrite_bare_snapshot_flag(argv: builtins.list[str]) -> builtins.list[str]:
    """Rewrite bare ``--snapshot`` on the ``pause`` subcommand to ``--snapshot=<SENTINEL>``.

    Typer no longer supports the Click ``is_flag=False, flag_value=...`` pattern,
    so we emulate "flag-or-value" by preprocessing ``sys.argv``: ``--snapshot``
    not followed by a value (next token absent or another flag) becomes
    ``--snapshot=__auto__``. ``--snapshot=NAME`` and ``--snapshot NAME`` are
    untouched. Other subcommands are not affected.
    """
    if "pause" not in argv:
        return argv
    out: builtins.list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--snapshot":
            nxt = argv[i + 1] if i + 1 < n else None
            if nxt is None or nxt.startswith("-"):
                out.append(f"--snapshot={_PAUSE_SNAPSHOT_AUTO}")
                i += 1
                continue
        out.append(tok)
        i += 1
    return out


@app.command()
def pause(
    ctx: typer.Context,
    name: str,
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
    snapshot: str | None = typer.Option(
        None,
        "--snapshot",
        help=(
            "Take a crash-consistent VolumeSnapshot of the sandbox PVC. "
            "Pass --snapshot for an auto-named snapshot, or --snapshot=NAME for a custom name."
        ),
    ),
):
    """Pause a sandbox (scale to 0) and optionally take a crash-consistent disk snapshot."""
    snapshot_name: str | None = None
    if snapshot is not None:
        snapshot_name = f"{name}-paused-{int(time.time())}" if snapshot == _PAUSE_SNAPSHOT_AUTO else snapshot
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(core, core.pause_sandbox(name=name, namespace=namespace, snapshot_name=snapshot_name))
        if not result.success:
            typer.echo(f"❌ {result.error}", err=True)
            raise typer.Exit(1)
        typer.echo(result.message)
        return
    data = handle_api_call(lambda: cli_ctx.client().pause(name, namespace=namespace, snapshot=snapshot_name))
    typer.echo(data.get("message", f"Sandbox {name} paused"))


@app.command()
def resume(
    ctx: typer.Context,
    name: str,
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
):
    """Resume a paused sandbox (scale to 1)."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(core, core.resume_sandbox(name=name, namespace=namespace))
        if not result.success:
            typer.echo(f"❌ {result.error}", err=True)
            raise typer.Exit(1)
        typer.echo(result.message)
        return
    data = handle_api_call(lambda: cli_ctx.client().resume(name, namespace=namespace))
    typer.echo(data.get("message", f"Sandbox {name} resumed"))


@app.command()
def restore(
    ctx: typer.Context,
    snapshot_name: str = typer.Argument(..., help="Existing VolumeSnapshot name"),
    new_sandbox_name: str = typer.Argument(..., help="Name for the restored sandbox"),
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
    image: str | None = typer.Option(
        None,
        "--image",
        help="Override the source image; required if the snapshot lacks the k7.io/source-image annotation",
    ),
    root_disk_size: str | None = typer.Option(
        None,
        "--root-disk-size",
        help="Override root disk size (must be >= the snapshot's restoreSize); defaults to source size",
    ),
    cpu: str | None = typer.Option(None, "--cpu", help="Override CPU limit (e.g. 1, 500m)"),
    memory: str | None = typer.Option(None, "--memory", help="Override memory limit (e.g. 1Gi)"),
    storage: str | None = typer.Option(None, "--storage", help="Override ephemeral-storage limit"),
    sidecar: str | None = typer.Option(
        None,
        "--sidecar",
        help="Override sidecar plugin (e.g. docker); pass empty string to disable",
    ),
    cmd: builtins.list[str] | None = typer.Option(None, "--cmd", help="Override container CMD (repeatable)"),
    entrypoint: builtins.list[str] | None = typer.Option(
        None, "--entrypoint", help="Override container ENTRYPOINT (repeatable)"
    ),
    keep_snapshot: bool = typer.Option(
        True,
        "--keep-snapshot/--no-keep-snapshot",
        help="Keep the source snapshot after a successful restore (default: keep)",
    ),
):
    """Restore a brand-new sandbox from a standalone VolumeSnapshot.

    Unlike ``k7 fork``, this does not require the original sandbox's Deployment
    to still exist — it only needs the snapshot. The new sandbox boots from a
    PVC cloned from the snapshot's data; image / backend / sidecar / limits /
    root-disk-size default to the source sandbox's values via the
    ``k7.io/source-*`` annotations stamped on the snapshot at creation time.
    Use the flags above to override individual fields, or to supply them when
    the snapshot was created before those annotations existed.
    """
    limits: dict[str, str] | None = None
    if cpu or memory or storage:
        limits = {}
        if cpu:
            limits["cpu"] = cpu
        if memory:
            limits["memory"] = memory
        if storage:
            limits["ephemeral-storage"] = storage
    overrides = SandboxConfigOverrides(
        image=image,
        root_disk_size=root_disk_size,
        sidecar=sidecar,
        limits=limits,
        entrypoint=builtins.list(entrypoint) if entrypoint else None,
        cmd=builtins.list(cmd) if cmd else None,
    )
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(
            core,
            core.restore_sandbox(
                snapshot_name=snapshot_name,
                new_sandbox_name=new_sandbox_name,
                namespace=namespace,
                overrides=overrides,
                keep_snapshot=keep_snapshot,
            ),
        )
        if not result.success:
            typer.echo(f"❌ {result.error}", err=True)
            raise typer.Exit(1)
        typer.echo(result.message)
        return
    handle_api_call(
        lambda: cli_ctx.client().restore(
            snapshot_name=snapshot_name,
            new_sandbox_name=new_sandbox_name,
            namespace=namespace,
            overrides=overrides.to_dict() or None,
            keep_snapshot=keep_snapshot,
        )
    )
    typer.echo(f"Sandbox {new_sandbox_name} restored from snapshot {snapshot_name}")


@app.command()
def fork(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="Existing sandbox name to fork"),
    new_name: str = typer.Argument(..., help="Name of the new sandbox"),
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
    snapshot: str | None = typer.Option(
        None,
        "--snapshot",
        help="Optional snapshot name to use for the forked disk (kata-qemu-longhorn)",
    ),
):
    """Clone a sandbox and its root disk (kata-qemu-longhorn) to a new name."""
    cli_ctx: CliContext = ctx.obj
    if not cli_ctx.use_core:
        handle_api_call(lambda: cli_ctx.client().fork(source, new_name, namespace=namespace, snapshot=snapshot))
        typer.echo(f"Forked {source} -> {new_name}")
        return
    core = K7Core()
    result = _core_run(
        core,
        core.fork_sandbox(
            source_name=source,
            new_name=new_name,
            namespace=namespace,
            snapshot_name=snapshot,
        ),
    )
    if result.success:
        typer.echo(result.message)
    else:
        typer.echo(f"❌ {result.error}", err=True)
        raise typer.Exit(1)


def _build_and_import_api_image() -> bool:
    """Build k7-api:local with Docker and import into k3s containerd.

    Returns True on success, False if Docker is unavailable or the build fails.
    """
    if not shutil.which("docker"):
        typer.echo("❌ Docker is required to build the API image. Install Docker first.", err=True)
        return False

    # Find the Dockerfile relative to the package
    from importlib import resources as _resources

    dockerfile = None
    try:
        ref = _resources.files("k7.api").joinpath("Dockerfile.api")
        with _resources.as_file(ref) as p:
            if p.exists():
                dockerfile = str(p)
    except Exception:
        pass
    if not dockerfile:
        src_path = Path(__file__).resolve().parent.parent / "api" / "Dockerfile.api"
        if src_path.exists():
            dockerfile = str(src_path)
    if not dockerfile:
        typer.echo("⚠️  Dockerfile.api not found; skipping local image build", err=True)
        return False

    # Dockerfile is at src/k7/api/Dockerfile.api; context must be repo root
    # (the dir containing src/) so COPY src/k7/... paths resolve correctly
    dockerfile_path = Path(dockerfile).resolve()
    context = str(dockerfile_path.parent.parent.parent.parent)

    typer.echo("Building k7-api:local image...")
    build = subprocess.run(
        # ``--network=host`` works around a Hetzner-node DNS flake where the
        # default Docker bridge fails to resolve pypi.org mid-build; matches
        # the Ansible playbook's invocation.
        ["docker", "build", "--network=host", "-f", dockerfile, "-t", "k7-api:local", context],
    )
    if build.returncode != 0:
        typer.echo("❌ Docker build failed", err=True)
        return False

    typer.echo("Importing image into k3s containerd...")
    save = subprocess.run(
        ["docker", "save", "k7-api:local"],
        capture_output=True,
    )
    if save.returncode != 0:
        typer.echo("❌ docker save failed", err=True)
        return False

    ctr_import = subprocess.run(
        ["k3s", "ctr", "images", "import", "-"],
        input=save.stdout,
    )
    if ctr_import.returncode != 0:
        typer.echo("❌ k3s ctr images import failed", err=True)
        return False

    return True


# ---------------------------------------------------------------------------
# ``k7 api ...`` sub-app — feature-toggle framing for the API server.
# The API is deployed by ``k7 install`` (gated on ``k7_api_enabled``) and lives
# on as a normal Kubernetes Deployment from then on. ``api enable/disable``
# scale that Deployment up/down for temporary off/on; ``status``/``endpoint``
# are read-only diagnostics. The dev-only "rebuild + roll" path moved under
# the hidden ``k7 dev api`` group so it stops cluttering ``--help`` for users
# who never set foot in the source tree.
# ---------------------------------------------------------------------------


api_app = typer.Typer(
    help="API server feature toggle and diagnostics (status / endpoint / enable / disable).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(api_app, name="api")

# Hidden ``k7 dev …`` group for developer-only utilities (mostly local-build
# flows that only make sense when you're iterating on the source tree).
dev_app = typer.Typer(
    help="Developer-only commands (not for normal users).",
    hidden=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(dev_app, name="dev")
dev_api_app = typer.Typer(
    help="Developer-only API commands (local Docker build / re-import / roll).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
dev_app.add_typer(dev_api_app, name="api")


def _api_scale_deployment(replicas: int) -> None:
    """Scale ``deployment/k7-api`` in ``kube-system`` to ``replicas``.

    Errors are loud — they exit the CLI with a non-zero status — because
    "I asked to disable but nothing happened" is exactly the silent
    failure mode this is meant to eliminate.
    """
    kubectl = _kubectl_cmd()
    result = _kubectl_run(kubectl, ["scale", "deployment", "k7-api", "-n", _system_ns(), f"--replicas={replicas}"])
    if result is None:
        typer.echo("❌ `k7 api enable`/`disable` need kubectl on a cluster node.", err=True)
        raise typer.Exit(1)
    if result.returncode != 0:
        typer.echo(f"❌ Failed to scale k7-api to {replicas}: {result.stderr.strip()}", err=True)
        raise typer.Exit(1)


@api_app.command("status")
def api_status_cmd():
    """Show API server readiness, endpoint, and key-management hints."""
    kubectl = _kubectl_cmd()
    if shutil.which(kubectl[0]) is None:
        _api_status_via_https()
        return
    ns = _system_ns()
    result = _kubectl_run(
        kubectl,
        ["get", "deployment", "k7-api", "-n", ns, "-o", "jsonpath={.status.readyReplicas}/{.spec.replicas}"],
    )
    if result is None:
        _api_status_via_https()
        return
    if result.returncode != 0:
        typer.echo(f"❌ K7 API deployment not found in namespace {ns}")
        typer.echo("Run `k7 install` to deploy it, or `k7 install --no-api` if that's deliberate.")
        return

    ready_info = result.stdout.strip()
    parts = ready_info.split("/")
    ready_count = int(parts[0]) if parts[0] else 0
    desired = int(parts[1]) if len(parts) > 1 and parts[1] else 0

    if ready_count > 0:
        typer.echo(f"✅ K7 API is running in {ns} ({ready_count}/{desired} ready)")
    elif desired == 0:
        typer.echo("⚠️  K7 API deployment exists but is scaled to 0 (disabled). Run `k7 api enable`.")
    else:
        typer.echo(f"⚠️  K7 API deployment exists but no pods ready ({ready_count}/{desired})")

    pod_result = subprocess.run(
        kubectl + ["get", "pods", "-n", ns, "-l", "app=k7-api", "-o", "wide", "--no-headers"],
        capture_output=True,
        text=True,
    )
    if pod_result.returncode == 0 and pod_result.stdout.strip():
        typer.echo(f"\n📦 Pod(s):\n{pod_result.stdout.strip()}")

    deny = _kubectl_run(kubectl, ["get", "ciliumclusterwidenetworkpolicy", PLATFORM_DENY_POLICY, "-o", "name"])
    if deny is None or deny.returncode != 0:
        typer.echo(
            f"\n⚠️  Sandbox→platform deny ({PLATFORM_DENY_POLICY}) is absent: sandboxes can reach the nodes, "
            "the Kubernetes API and cloud metadata. Only Cilium enforces it (chart networkPolicy.provider=cilium)."
        )

    endpoint = _read_api_endpoint(kubectl)
    if endpoint:
        typer.echo(f"\n🌐 API endpoint: {endpoint}")

    typer.echo("\n📝 SDK usage example:")
    typer.echo("    from k7_sdk import Client")
    typer.echo("    k7 = Client(")
    typer.echo("        endpoint='<endpoint>', api_key='<key>',")
    typer.echo("        verify_ssl='./k7-ca.crt',  # cluster CA; on the node use /etc/k7/tls/ca.crt")
    typer.echo("    )")
    typer.echo("    sb = k7.create({'name': 'test', 'image': 'alpine:3.21'})")
    typer.echo("\n🔐 Manage API keys:")
    typer.echo("    k7 generate-api-key <name>")
    typer.echo("    k7 list-api-keys")
    typer.echo("    k7 revoke-api-key <name>")


@api_app.command("endpoint")
def api_endpoint_cmd():
    """Print the K7 API NodePort URL (machine-readable, single line)."""
    kubectl = _kubectl_cmd()
    endpoint = _read_api_endpoint(kubectl)
    if endpoint:
        typer.echo(endpoint)
    else:
        typer.echo("❌ K7 API service not found", err=True)
        raise typer.Exit(1)


_API_REPLICAS_ANN = "k7.katakate.org/enabled-replicas"


def _api_kubectl_or_exit(args: builtins.list[str]) -> str:
    result = _kubectl_run(_kubectl_cmd(), args)
    if result is None:
        typer.echo("❌ `k7 api enable`/`disable` need kubectl on a cluster node.", err=True)
        raise typer.Exit(1)
    if result.returncode != 0:
        typer.echo(f"❌ kubectl {' '.join(args)} failed: {result.stderr.strip()}", err=True)
        raise typer.Exit(1)
    return result.stdout.strip()


@api_app.command("enable")
def api_enable_cmd(
    replicas: int | None = typer.Option(
        None, "--replicas", help="Replica count (default: the count recorded by `k7 api disable`)."
    ),
):
    """Scale the k7-api Deployment back up."""
    if replicas is None:
        jsonpath = "jsonpath={.metadata.annotations." + _API_REPLICAS_ANN.replace(".", "\\.") + "}"
        recorded = _api_kubectl_or_exit(["get", "deployment", "k7-api", "-n", _system_ns(), "-o", jsonpath])
        if not recorded:
            typer.echo("❌ No replica count recorded by `k7 api disable`; pass --replicas N.", err=True)
            raise typer.Exit(1)
        replicas = int(recorded)
    _api_scale_deployment(replicas)
    typer.echo(f"✅ K7 API enabled (replicas={replicas}).")


@api_app.command("disable")
def api_disable_cmd():
    """Scale the existing k7-api Deployment to 0 replicas (temporary off)."""
    current = _api_kubectl_or_exit(
        ["get", "deployment", "k7-api", "-n", _system_ns(), "-o", "jsonpath={.spec.replicas}"]
    )
    if current and int(current) > 0:
        _api_kubectl_or_exit(
            ["annotate", "deployment", "k7-api", "-n", _system_ns(), f"{_API_REPLICAS_ANN}={current}", "--overwrite"]
        )
    _api_scale_deployment(0)
    typer.echo("✅ K7 API disabled (replicas=0). Run `k7 api enable` to bring it back.")


api_keys_app = typer.Typer(
    help="API key store maintenance.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
api_app.add_typer(api_keys_app, name="keys")


@api_keys_app.command("migrate")
def api_keys_migrate_cmd():
    """Copy /etc/k7/api_keys.json into the k7-api-keys Secret, then give every key a role. Idempotent."""
    typer.echo(_key_store_call(api_keys_store.migrate))


@dev_api_app.command("rebuild")
def dev_api_rebuild_cmd(
    api_port: int = typer.Option(31007, "--api-port", help="NodePort for the API service (default 31007)"),
    skip_build: bool = typer.Option(False, "--skip-build", help="Skip building the API image (assume it exists)"),
):
    """Rebuild the local k7-api image, re-import into k3s, re-apply manifests.

    Developer iteration loop — only makes sense when the controller IS a
    cluster node (single-node dev). Refuses to run otherwise (kubectl must be
    reachable and the k7-api manifests must be present).
    """
    kubectl = _kubectl_cmd()

    if not skip_build and not _build_and_import_api_image():
        typer.echo("⚠️  Image build skipped or failed; deployment may fail if image is not present", err=True)

    if not CONTAINERD_SOCKET_FILE.is_file():
        typer.echo(f"❌ {CONTAINERD_SOCKET_FILE} is missing; run `k7 install` on this node first.", err=True)
        raise typer.Exit(1)
    metering = subprocess.run(
        kubectl + ["-n", "kube-system", "get", "configmap", "k7-metering", "-o", "jsonpath={.data.metering\\.json}"],
        capture_output=True,
        text=True,
    )
    if metering.returncode != 0 or not metering.stdout:
        typer.echo(
            f"❌ ConfigMap k7-metering is unreadable ({metering.stderr.strip()}); run `k7 install` first.", err=True
        )
        raise typer.Exit(1)
    typer.echo(f"Re-applying K7 API manifests with {k7_images.LOCAL_API_IMAGE_REF} (unsigned)...")
    with tempfile.TemporaryDirectory() as tmp:
        manifests = Path(tmp) / "k7-api"
        k7_images.render_manifests(
            Path(_get_api_manifests_dir()),
            manifests,
            k7_images.API_IMAGE_LOCAL,
            CONTAINERD_SOCKET_FILE.read_text().strip(),
            cluster_config.api_mode(),
            metering.stdout,
        )
        service_file = manifests / "service.yaml"
        if api_port != 31007:
            service_file.write_text(service_file.read_text().replace("nodePort: 31007", f"nodePort: {api_port}"))
        apply = subprocess.run(kubectl + ["apply", "-f", str(manifests)], capture_output=True, text=True)
    if apply.returncode != 0:
        typer.echo(f"❌ Failed to apply API manifests: {apply.stderr.strip()}", err=True)
        raise typer.Exit(1)

    typer.echo("Rolling k7-api deployment to pick up new image...")
    subprocess.run(
        kubectl + ["rollout", "restart", "deployment/k7-api", "-n", "kube-system"],
        capture_output=True,
        text=True,
    )
    rollout = subprocess.run(
        kubectl + ["rollout", "status", "deployment/k7-api", "-n", "kube-system", "--timeout=180s"],
        capture_output=True,
        text=True,
    )
    if rollout.returncode != 0:
        typer.echo(f"⚠️  Deployment not ready yet: {rollout.stderr.strip()}", err=True)
        typer.echo("Check status with: k7 api status")
    else:
        typer.echo("✅ K7 API rolled out with the new image.")

    endpoint = _read_api_endpoint(kubectl)
    if endpoint:
        typer.echo(f"🌐 API endpoint: {endpoint}")


# Deprecated top-level commands. ``hidden=True`` keeps them off the main
# ``--help`` output while leaving the names callable so existing user
# scripts don't break. They emit a one-line deprecation warning to stderr
# and forward to the new home. Drop these in the next release.


@app.command(name="start-api", hidden=True)
def _deprecated_start_api(
    api_port: int = typer.Option(31007, "--api-port", help="NodePort for the API service"),
    skip_build: bool = typer.Option(False, "--skip-build", help="Skip building the API image"),
):
    """Deprecated alias for ``k7 dev api rebuild``."""
    typer.echo(
        "⚠️  `k7 start-api` is deprecated. The API is deployed by `k7 install`. "
        "For dev iteration use `k7 dev api rebuild`.",
        err=True,
    )
    dev_api_rebuild_cmd(api_port=api_port, skip_build=skip_build)


@app.command(name="stop-api", hidden=True)
def _deprecated_stop_api(
    delete: bool = typer.Option(False, "--delete", help="Delete API resources instead of scaling to 0"),
):
    """Deprecated alias for ``k7 api disable``."""
    if delete:
        typer.echo(
            "⚠️  `k7 stop-api --delete` is deprecated. Remove the manifests with "
            "`kubectl delete -f /etc/k7/manifests/k7-api/` if you really mean to uninstall.",
            err=True,
        )
        kubectl = _kubectl_cmd()
        manifests = _get_api_manifests_dir()
        subprocess.run(
            kubectl + ["delete", "-f", manifests, "--ignore-not-found"],
            capture_output=True,
            text=True,
        )
        typer.echo("K7 API resources deleted")
        return
    typer.echo(
        "⚠️  `k7 stop-api` is deprecated. Use `k7 api disable` (scale to 0).",
        err=True,
    )
    api_disable_cmd()


@app.command(name="api-status", hidden=True)
def _deprecated_api_status():
    """Deprecated alias for ``k7 api status``."""
    typer.echo("⚠️  `k7 api-status` is deprecated. Use `k7 api status`.", err=True)
    api_status_cmd()


@app.command(name="get-api-endpoint", hidden=True)
def _deprecated_get_api_endpoint():
    """Deprecated alias for ``k7 api endpoint``."""
    typer.echo("⚠️  `k7 get-api-endpoint` is deprecated. Use `k7 api endpoint`.", err=True)
    api_endpoint_cmd()


# ---------------------------------------------------------------------------
# ``k7 nodes …`` sub-app for cluster-node queries.
# ---------------------------------------------------------------------------


def _tenant_run(fn):
    """Run ``fn(core)`` against the cluster (node kubeconfig, not k7-api); loud on failure."""
    core = K7Core()
    try:
        return _core_run(core, fn(core))
    except (LookupError, ValueError, RuntimeError, ApiException) as e:
        typer.echo(f"❌ {getattr(e, 'body', None) or e}", err=True)
        raise typer.Exit(1)


def _kv(values: builtins.list[str] | None, flag: str) -> dict | None:
    """``--quota sandboxes=10,vcpu=8 --quota memory=16Gi`` → dict; None when the flag is absent."""
    if not values:
        return None
    out: dict = {}
    for item in ",".join(values).split(","):
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise typer.BadParameter(f"{flag} expects KEY=VALUE[,KEY=VALUE], got {item!r}")
        value = value.strip()
        out[key.strip()] = int(value) if key.strip() == "sandboxes" and value.isdigit() else value
    return out


def _tenant_changes(
    namespace: builtins.list[str] | None,
    node: builtins.list[str] | None,
    default_size: builtins.list[str] | None,
    quota: builtins.list[str] | None,
    max_size: builtins.list[str] | None,
    disk_io: builtins.list[str] | None,
    network: builtins.list[str] | None,
) -> dict:
    changes: dict = {}
    if namespace:
        changes["namespaces"] = _dedupe_keep_order(namespace)
    if node:
        changes["nodes"] = _dedupe_keep_order(node)
    for key, value in (("defaultSize", _kv(default_size, "--default-size")), ("quotas", _kv(quota, "--quota"))):
        if value is not None:
            changes[key] = value
    limits = {
        k: v
        for k, v in (
            ("maxSandboxSize", _kv(max_size, "--max-size")),
            ("diskIO", _kv(disk_io, "--disk-io")),
            ("network", _kv(network, "--network")),
        )
        if v is not None
    }
    if limits:
        changes["limits"] = limits
    return changes


_T_NS = typer.Option(None, "-n", "--namespace", help="Tenant namespace (repeatable; created if missing)")
_T_NODE = typer.Option(None, "--node", help="Dedicated node (repeatable; labelled and tainted)")
_T_DEFAULT = typer.Option(None, "--default-size", help="cpu=1,memory=1Gi,disk=10Gi for creates without limits")
_T_QUOTA = typer.Option(
    None, "--quota", help="sandboxes=N,vcpu=..,memory=..,disk=..,snapshotStorage=.. (absent = no quota)"
)
_T_MAX = typer.Option(None, "--max-size", help="Per-sandbox maxSandboxSize: cpu=8,memory=32Gi,disk=200Gi")
_T_DISKIO = typer.Option(
    None, "--disk-io", help="Per-VM readBps/writeBps/readIops/writeIops (no backend enforces these yet)"
)
_T_NET = typer.Option(None, "--network", help="Per-VM egressBps=500M,ingressBps=500M in bits/s (kfd/kql)")

tenant_app = typer.Typer(
    help="Tenants: namespaces, API keys, dedicated nodes, default size, quotas and per-VM limits.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(tenant_app, name="tenant")


@tenant_app.command("create")
def tenant_create(
    name: str = typer.Argument(..., help="Tenant name (DNS label)"),
    namespace: builtins.list[str] | None = _T_NS,
    node: builtins.list[str] | None = _T_NODE,
    default_size: builtins.list[str] | None = _T_DEFAULT,
    quota: builtins.list[str] | None = _T_QUOTA,
    max_size: builtins.list[str] | None = _T_MAX,
    disk_io: builtins.list[str] | None = _T_DISKIO,
    network: builtins.list[str] | None = _T_NET,
):
    """Create a Tenant; refuses a namespace another tenant already claims."""
    spec = _tenant_changes(namespace, node, default_size, quota, max_size, disk_io, network)
    _tenant_run(lambda core: tenancy.create_tenant(core, name, spec))
    typer.echo(f"✅ tenant {name} created (namespaces: {', '.join(spec.get('namespaces') or [])})")


@tenant_app.command("update")
def tenant_update(
    name: str = typer.Argument(...),
    namespace: builtins.list[str] | None = _T_NS,
    node: builtins.list[str] | None = _T_NODE,
    default_size: builtins.list[str] | None = _T_DEFAULT,
    quota: builtins.list[str] | None = _T_QUOTA,
    max_size: builtins.list[str] | None = _T_MAX,
    disk_io: builtins.list[str] | None = _T_DISKIO,
    network: builtins.list[str] | None = _T_NET,
    clear: builtins.list[str] | None = typer.Option(
        None, "--clear", help="Remove a spec section: nodes, defaultSize, quotas or limits (repeatable)"
    ),
):
    """Replace the given spec sections (each flag replaces its whole section)."""
    changes: dict = _tenant_changes(namespace, node, default_size, quota, max_size, disk_io, network)
    for section in clear or []:
        if section not in ("nodes", "defaultSize", "quotas", "limits") or section in changes:
            raise typer.BadParameter(f"--clear {section}: not a clearable section, or also being set")
        changes[section] = None
    if not changes:
        raise typer.BadParameter("nothing to update")
    _tenant_run(lambda core: tenancy.update_tenant(core, name, changes))
    typer.echo(f"✅ tenant {name} updated ({', '.join(changes)})")


@tenant_app.command("get")
def tenant_get(name: str = typer.Argument(...)):
    """Print a Tenant (spec and status) as JSON."""
    tenant = _tenant_run(lambda core: tenancy.get_tenant(core, name))
    if tenant is None:
        typer.echo(f"❌ tenant {name} not found", err=True)
        raise typer.Exit(1)
    tenant["metadata"].pop("managedFields", None)
    typer.echo(json.dumps({k: tenant.get(k) for k in ("metadata", "spec", "status")}, indent=2, default=str))


@tenant_app.command("list")
def tenant_list(as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table")):
    """List Tenants with namespaces, nodes, quotas and last reconciled usage."""
    tenants = _tenant_run(tenancy.list_tenants)
    if as_json:
        typer.echo(json.dumps(tenants, indent=2, default=str))
        return
    table = Table(title="K7 Tenants")
    for col in ("NAME", "NAMESPACES", "NODES", "QUOTAS", "USAGE"):
        table.add_column(col)
    for t in tenants:
        spec, usage = tenancy.spec_of(t), (t.get("status") or {}).get("usage") or {}
        quotas = spec.get("quotas") or {}
        table.add_row(
            tenancy.name_of(t),
            ",".join(spec.get("namespaces") or []),
            ",".join(spec.get("nodes") or []) or "-",
            ",".join(f"{k}={v}" for k, v in quotas.items()) or "-",
            ",".join(f"{k}={v}" for k, v in usage.items()) or "-",
        )
    Console().print(table)


@tenant_app.command("delete")
def tenant_delete(
    name: str = typer.Argument(...),
    cascade: bool = typer.Option(False, "--cascade", help="Also delete the tenant's snapshots and sandboxes"),
):
    """Delete a Tenant. Refuses while it has sandboxes or snapshots unless --cascade."""
    deleted = _tenant_run(lambda core: tenancy.delete_tenant(core, name, cascade))
    for item in deleted:
        typer.echo(f"deleted {item}")
    typer.echo(f"✅ tenant {name} deleted")


@tenant_app.command("adopt")
def tenant_adopt(
    from_keys: bool = typer.Option(False, "--from-keys", help="Propose tenants from existing key namespace scopes"),
    apply: bool = typer.Option(False, "--apply", help="Create the proposed tenants and bind the keys"),
):
    """Migration: one tenant per connected group of key namespaces. Prints the plan; writes only with --apply."""
    if not from_keys:
        raise typer.BadParameter("only --from-keys is supported")
    keys = _key_store_call(lambda kc: api_keys_store.decode_keys(kc.read(api_keys_store.KEYS_SECRET) or {}))
    tenants = _tenant_run(tenancy.list_tenants)
    try:
        plan, unscoped = tenancy.adopt_plan(keys, tenants)
    except ValueError as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(1)
    for item in plan:
        typer.echo(
            f"tenant {item['tenant']}: namespaces {', '.join(item['namespaces'])}; keys {', '.join(item['keys'])}"
        )
    if unscoped:
        typer.echo(
            f"⚠️  unscoped key(s) {', '.join(unscoped)} reach every namespace: production mode refuses them. "
            "Revoke and reissue with --role user --tenant T (or --role admin for a real admin)."
        )
    if not plan:
        typer.echo("nothing to adopt")
        return
    if not apply:
        typer.echo("dry run; re-run with --apply to create these tenants and bind the keys")
        return
    for item in plan:
        _tenant_run(
            lambda core, item=item: tenancy.create_tenant(core, item["tenant"], {"namespaces": item["namespaces"]})
        )
        _key_store_call(api_keys_store.bind_tenant, item["keys"], item["tenant"])
        typer.echo(f"✅ tenant {item['tenant']} created; {len(item['keys'])} key(s) bound")


nodes_app = typer.Typer(
    help="Query and dedicate cluster nodes (storage pools, tenant isolation).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(nodes_app, name="nodes")


def _print_nodes_list_table(rows: builtins.list[dict]) -> None:
    """Render ``list_cluster_nodes()`` as NAME / HOSTNAME / BACKENDS / TENANT."""
    if not rows:
        typer.echo("No nodes found.")
        return
    table = Table(title="K7 Nodes")
    table.add_column("NAME", style="cyan")
    table.add_column("HOSTNAME")
    table.add_column("BACKENDS")
    table.add_column("TENANT")
    for n in rows:
        backends = ",".join(n.get("backends") or []) or "-"
        table.add_row(n.get("name") or "-", n.get("hostname") or "-", backends, n.get("tenant") or "-")
    Console().print(table)


@nodes_app.command("list")
def nodes_list(
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
):
    """Show cluster node names, hostname labels, backends, and tenant dedication.

    NAME is what ``--node`` and ``k7 nodes dedicate`` take. It is the K3s
    Node name (machine hostname), not something you label by hand.
    HOSTNAME is ``kubernetes.io/hostname`` (kubelet). BACKENDS come from
    ``k7 install --backend`` / inventory ``k7_backends`` (``none`` means
    no sandbox runtime). TENANT comes from ``k7 nodes dedicate``.
    """
    core = K7Core()
    rows = _core_run(core, core.list_cluster_nodes())
    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    _print_nodes_list_table(rows)


def _print_nodes_storage_table(data: dict) -> None:
    """Render ``nodes_storage()`` output as a per-node Rich table."""
    if not data:
        typer.echo("No nodes found.")
        return
    table = Table(title="K7 Node Storage")
    table.add_column("NODE", style="cyan")
    table.add_column("THINPOOL", justify="right")
    table.add_column("DATA%", justify="right")
    table.add_column("META%", justify="right")
    table.add_column("K7D POOL", justify="right")
    table.add_column("K7D USED%", justify="right")
    table.add_column("USAGE SPOOL", justify="right")
    table.add_column("ERROR", style="red")
    for node in sorted(data):
        entry = data[node]
        if "error" in entry:
            table.add_row(node, "-", "-", "-", "-", "-", "-", entry["error"])
            continue
        tp = entry.get("kata_thinpool") or {}
        kd = entry.get("k7d_disks") or {}
        sp = entry.get("usage_spool")
        if sp is None:
            spool = "not reported (k7-agent predates metering)"
        else:
            spool = f"{_format_size_bytes(sp['bytes'])}/{_format_size_bytes(sp['max_bytes'])}"
            spool += " (above high-water)" if sp["above_high_water"] else ""
        table.add_row(
            node,
            _format_size_bytes(int(tp.get("size_bytes", 0) or 0)),
            f"{float(tp.get('data_percent', 0)):.2f}",
            f"{float(tp.get('metadata_percent', 0)):.2f}",
            _format_size_bytes(int(kd.get("size_bytes", 0) or 0)),
            f"{float(kd.get('used_percent', 0)):.2f}",
            spool,
            f"{sp['dropped_events']} usage events dropped" if sp and sp["dropped_events"] else "",
        )
    Console().print(table)


@nodes_app.command("storage")
def nodes_storage(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
):
    """Show per-node kfd thin-pool, k7d disks-pool and usage-spool utilization."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        data = _core_run(core, core.nodes_storage())
    else:
        data = handle_api_call(lambda: cli_ctx.client().nodes_storage())
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    _print_nodes_storage_table(data)


@nodes_app.command("dedicate")
def nodes_dedicate(
    ctx: typer.Context,
    node: str = typer.Argument(..., help="Kubernetes node name (metadata.name / hostname)"),
    tenant: str = typer.Option(..., "--tenant", help="Existing Tenant (k7 tenant create) to dedicate the node to"),
    use_api: bool = typer.Option(False, "--api", help="Go through k7-api (operator/admin) instead of kubeconfig."),
):
    """Label and taint a node for a tenant and add it to ``Tenant.spec.nodes``.

    The label is the join key; the NoSchedule taint is the exclusive lock.
    Every sandbox of the tenant then lands only on its nodes. Fails if the
    Tenant does not exist. Runs with cluster access (on a node, or
    kubeconfig), or with ``--api`` as an operator/admin through k7-api.
    """
    if use_api:
        cli_ctx: CliContext = ctx.obj
        typer.echo(f"✅ {handle_api_call(lambda: cli_ctx.client().dedicate_node(node, tenant))['message']}")
        return
    _tenant_run(lambda core: tenancy.dedicate(core, node, tenant))
    typer.echo(f"✅ node {node} dedicated to tenant {tenant} (label+taint {tenancy.TENANT_LABEL}, spec.nodes)")


@nodes_app.command("undedicate")
def nodes_undedicate(
    ctx: typer.Context,
    node: str = typer.Argument(..., help="Kubernetes node name (metadata.name / hostname)"),
    use_api: bool = typer.Option(False, "--api", help="Go through k7-api (operator/admin) instead of kubeconfig."),
):
    """Remove the k7 tenant label and taint from a node, and the node from every Tenant's spec.nodes."""
    if use_api:
        cli_ctx: CliContext = ctx.obj
        typer.echo(f"✅ {handle_api_call(lambda: cli_ctx.client().undedicate_node(node))['message']}")
        return
    _tenant_run(lambda core: tenancy.undedicate(core, node))
    typer.echo(f"✅ node {node} undedicated")


# ``k7 snapshot …`` sub-app for VolumeSnapshot CRUD + GC.
# ---------------------------------------------------------------------------


snapshot_app = typer.Typer(
    help="Manage VolumeSnapshots (pause / fork / named).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(snapshot_app, name="snapshot")


def _format_size_bytes(size: int) -> str:
    """Render a byte count as Gi/Mi/Ki (matches ``kubectl get pvc`` style)."""
    if size <= 0:
        return "-"
    for unit, factor in (("Gi", 1 << 30), ("Mi", 1 << 20), ("Ki", 1 << 10)):
        if size >= factor:
            return f"{size / factor:.1f}{unit}"
    return f"{size}B"


def _format_age(age: str) -> str:
    """Trim a ``str(timedelta)`` to a kubectl-style short form."""
    if not age or age == "Unknown":
        return age or "-"
    # ``str(datetime.now(...) - created)`` looks like ``0:05:42.123456`` or
    # ``2 days, 0:05:42``. Strip the microseconds and the "0 days," prefix.
    out = age.split(".", 1)[0]
    return out.replace("0:", "0h", 1) if out.startswith("0:") else out


def _print_snapshot_table(snaps: builtins.list[dict], all_namespaces: bool) -> None:
    """Render a list of snapshot dicts (from K7Core or the SDK) as a Rich table."""
    if not snaps:
        typer.echo("No snapshots found.")
        return
    table = Table(box=None)
    table.add_column("NAME")
    if all_namespaces:
        table.add_column("NAMESPACE")
    table.add_column("SANDBOX")
    table.add_column("SIZE")
    table.add_column("AGE")
    table.add_column("READY")
    table.add_column("KIND")
    for s in snaps:
        row = [s.get("name", "")]
        if all_namespaces:
            row.append(s.get("namespace", ""))
        row.extend(
            [
                s.get("source_sandbox") or "-",
                _format_size_bytes(int(s.get("size_bytes", 0) or 0)),
                _format_age(s.get("age", "")),
                "True" if s.get("ready_to_use") else "False",
                s.get("kind", ""),
            ]
        )
        table.add_row(*row)
    Console().print(table)


@snapshot_app.command("list")
def snapshot_list(
    ctx: typer.Context,
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
    all_namespaces: bool = typer.Option(False, "-A", "--all-namespaces", help="List across all namespaces"),
    sandbox: str | None = typer.Option(None, "--sandbox", help="Filter to snapshots tied to a sandbox"),
    kind: str | None = typer.Option(None, "--kind", help="Filter by kind: pause / fork / named"),
):
    """List VolumeSnapshots managed by k7."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        snap_objs = _core_run(
            core,
            core.list_snapshots(
                namespace=namespace,
                all_namespaces=all_namespaces,
                sandbox=sandbox,
                kind=kind,
            ),
        )
        snaps = [s.to_dict() for s in snap_objs]
    else:
        snaps = handle_api_call(
            lambda: cli_ctx.client().list_snapshots(
                namespace=namespace,
                all_namespaces=all_namespaces,
                sandbox=sandbox,
                kind=kind,
            )
        )
    _print_snapshot_table(snaps, all_namespaces)


@snapshot_app.command("inspect")
def snapshot_inspect(
    ctx: typer.Context,
    name: str,
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
):
    """Show full details for a single snapshot."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        snap = _core_run(core, core.get_snapshot(name, namespace=namespace))
        if snap is None:
            typer.echo(f"❌ Snapshot {name} not found in namespace {namespace}", err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(snap.to_dict(), indent=2, default=str))
        return
    data = handle_api_call(lambda: cli_ctx.client().get_snapshot(name, namespace=namespace))
    if data is None:
        typer.echo(f"❌ Snapshot {name} not found in namespace {namespace}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(data, indent=2, default=str))


@snapshot_app.command("create")
def snapshot_create(
    ctx: typer.Context,
    sandbox_name: str = typer.Argument(..., help="Sandbox whose root PVC to snapshot"),
    snapshot_name: str = typer.Argument(..., help="Name to give the new VolumeSnapshot"),
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
):
    """Snapshot a running sandbox's root PVC without pausing it (kind=named)."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(core, core.create_snapshot(sandbox_name, snapshot_name, namespace=namespace))
        if not result.success:
            typer.echo(f"❌ {result.error}", err=True)
            raise typer.Exit(1)
        typer.echo(result.message)
        return
    handle_api_call(lambda: cli_ctx.client().create_snapshot(sandbox_name, snapshot_name, namespace=namespace))
    typer.echo(f"Snapshot {snapshot_name} created for sandbox {sandbox_name}")


@snapshot_app.command("delete")
def snapshot_delete(
    ctx: typer.Context,
    name: str,
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Delete a snapshot by name."""
    if not yes:
        if not sys.stdin.isatty():
            typer.echo("❌ Refusing to read confirmation from a non-TTY; pass --yes to proceed.", err=True)
            raise typer.Exit(1)
        confirmed = typer.confirm(f"Delete VolumeSnapshot {name} in namespace {namespace}?")
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(1)
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        core = K7Core()
        result = _core_run(core, core.delete_snapshot(name, namespace=namespace))
        if not result.success:
            typer.echo(f"❌ {result.error}", err=True)
            raise typer.Exit(1)
        typer.echo(result.message)
        return
    data = handle_api_call(lambda: cli_ctx.client().delete_snapshot(name, namespace=namespace))
    typer.echo(data.get("message", f"Snapshot {name} deleted"))


@snapshot_app.command("gc")
def snapshot_gc(
    ctx: typer.Context,
    namespace: str = typer.Option("default", "-n", "--namespace", help="Kubernetes namespace"),
    all_namespaces: bool = typer.Option(False, "-A", "--all-namespaces", help="GC across all namespaces"),
    keep_fork_for: str = typer.Option(
        "10m",
        "--keep-fork-for",
        help="Keep fork snapshots younger than this duration (e.g. 10m, 2h, 45s)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be deleted without deleting"),
):
    """Delete stale ``kind=fork`` snapshots older than ``--keep-fork-for``.

    Pause snapshots and named snapshots are never touched. Also reaps
    orphan VolumeSnapshotContents whose VolumeSnapshot is already gone.
    """
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        if keep_fork_for.endswith("m"):
            td = timedelta(minutes=int(keep_fork_for[:-1]))
        elif keep_fork_for.endswith("h"):
            td = timedelta(hours=int(keep_fork_for[:-1]))
        elif keep_fork_for.endswith("s"):
            td = timedelta(seconds=int(keep_fork_for[:-1]))
        else:
            td = timedelta(seconds=int(keep_fork_for))
        core = K7Core()
        result = _core_run(
            core,
            core.gc_snapshots(
                namespace=namespace,
                all_namespaces=all_namespaces,
                keep_fork_for=td,
                dry_run=dry_run,
            ),
        )
        if not result.success:
            typer.echo(f"❌ {result.error}", err=True)
            raise typer.Exit(1)
        typer.echo(result.message)
        for record in result.data or []:
            marker = "would-delete" if dry_run else ("deleted" if record.get("deleted") else "failed")
            typer.echo(f"  [{marker}] {record['namespace']}/{record['name']} (age={record['age']})")
        return
    data = handle_api_call(
        lambda: cli_ctx.client().gc_snapshots(
            namespace=namespace,
            all_namespaces=all_namespaces,
            keep_fork_for=keep_fork_for,
            dry_run=dry_run,
        )
    )
    typer.echo(data.get("message", "gc complete"))
    for record in data.get("results", []) or []:
        marker = "would-delete" if dry_run else ("deleted" if record.get("deleted") else "failed")
        typer.echo(f"  [{marker}] {record['namespace']}/{record['name']} (age={record['age']})")


if __name__ == "__main__":
    sys.argv = _rewrite_bare_snapshot_flag(sys.argv)
    app()
