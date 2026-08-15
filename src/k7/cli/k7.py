#!/usr/bin/env python3

import asyncio
import builtins
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import typer
from click.core import ParameterSource
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
from k7.cli._client import CliContext, handle_api_call
from k7.cli._config import config_app
from k7.core.core import K7Core
from k7.core.models import SandboxConfig, SandboxConfigOverrides
from k7.core.sidecar import SIDECAR_REGISTRY

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
    # CliContext is built lazily — handlers that never call ``ctx.obj.client()``
    # (install / start-api / config / ...) don't trigger the missing-URL error.
    ctx.obj = CliContext(use_core=use_core, api_url=api_url, api_key=api_key)
    if ctx.invoked_subcommand is None:
        # Show top-level help when no command is provided
        try:
            typer.echo(ctx.get_help())
        except Exception:
            typer.echo("Usage: k7 [OPTIONS] COMMAND [ARGS]...\nTry 'k7 -h' for help.")
        raise typer.Exit()


API_KEYS_FILE = Path(os.getenv("K7_API_KEYS_FILE", "/etc/k7/api_keys.json"))
# The k7-api container runs as this fixed non-root uid (`useradd -u 1000
# k7user` in src/k7/api/Dockerfile.api) and reads the key store through a
# hostPath mount. The store holds sha256 *hashes* (never raw keys), but we
# still keep it 0600 — which means it must be owned by the API uid or the
# pod cannot read it and every key is rejected.
K7_API_UID = 1000


def _write_api_keys(api_keys: dict) -> None:
    """Persist the key store with permissions the k7-api pod can use.

    Mode 0600 owned by the API container uid. The chown needs root (the
    normal case on a node); when unavailable we warn loudly instead of
    leaving the API silently locked out.
    """
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w") as f:
        json.dump(api_keys, f, indent=2)
    os.chmod(API_KEYS_FILE, 0o600)
    try:
        os.chown(API_KEYS_FILE, K7_API_UID, K7_API_UID)
    except (PermissionError, OSError) as e:
        typer.echo(
            f"⚠️  Could not chown {API_KEYS_FILE} to uid {K7_API_UID} ({e}). "
            "The k7-api pod runs as that uid and will NOT be able to read the "
            "key store — re-run as root on the node hosting the k7-api pod.",
            err=True,
        )


BACKEND_ALLOWED = {"kata-firecracker-devmapper", "kata-qemu-longhorn", "k7d"}
BACKEND_ALIASES = {
    "kfd": "kata-firecracker-devmapper",
    "kql": "kata-qemu-longhorn",
    "k7": "k7d",
}
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
        f"Unsupported backend '{backend}'. Use {allowed} (aliases: kfd, kql, k7; deprecated: fd, ql)."
    )


def _parse_backends(value: str) -> list[str]:
    """Parse a comma-separated list of backend names, normalize and de-dupe.

    Used by `k7 install` where multiple backends can be installed on a node.
    Preserves first-seen order; raises typer.BadParameter on unknown entries.
    """
    if value is None:
        raise typer.BadParameter("Backend value cannot be empty.")
    raw = [p for p in (s.strip() for s in value.split(",")) if p]
    if not raw:
        raise typer.BadParameter("Backend value cannot be empty.")
    seen: builtins.list[str] = []
    for entry in raw:
        normalized = _normalize_backend(entry)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def _kubectl_cmd() -> list[str]:
    """Return the kubectl command prefix (k3s kubectl or kubectl)."""
    return ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]


def _build_default_inventory(
    hosts: list[str] | None,
    role: str,
    backends: list[str],
    disk: str | None,
    longhorn_extra_disk: str | None,
) -> str:
    """Build a minimal multi-group inventory when the user did not provide one.

    Single-host shortcut: no `hosts` argument → localhost server with the
    user-supplied backends. `backends` may contain one or both of
    `kata-firecracker-devmapper` and `kata-qemu-longhorn`; the playbook will install
    each backend's prerequisites only when listed.
    """
    server_lines: builtins.list[str] = []
    agent_lines: builtins.list[str] = []
    backends_csv = ",".join(backends)
    has_devmapper = "kata-firecracker-devmapper" in backends
    has_longhorn = "kata-qemu-longhorn" in backends

    def _host_vars(extra_host: str | None = None) -> str:
        parts = [f"k7_backends={backends_csv}"]
        if has_devmapper and disk:
            parts.append(f"k7_devmapper_disk={disk}")
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


def _read_api_endpoint(kubectl: list[str]) -> str | None:
    """Read the K7 API NodePort endpoint from the K3s service."""
    port_result = subprocess.run(
        kubectl + ["get", "svc", "k7-api", "-n", "kube-system", "-o", "jsonpath={.spec.ports[0].nodePort}"],
        capture_output=True,
        text=True,
    )
    if port_result.returncode != 0 or not port_result.stdout.strip():
        return None
    node_port = port_result.stdout.strip()

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

    return f"http://{node_ip}:{node_port}"


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
    backend: str = typer.Option(
        "kata-firecracker-devmapper,kata-qemu-longhorn",
        "--backend",
        "-b",
        help=(
            "Comma-separated sandbox backends to install on this node. "
            "Choices: kata-firecracker-devmapper (kfd), kata-qemu-longhorn (kql), k7d. "
            "Default installs the two Kata backends; add k7d explicitly for the "
            "warm-fork microVM runtime."
        ),
        show_default=True,
    ),
    k7d_version: str | None = typer.Option(
        None,
        "--k7d-version",
        help="k7d release version to install (k7d backend only; default from playbook: 0.1.0)",
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
    no_api: bool = typer.Option(
        False,
        "--no-api",
        help="Skip deploying the K7 API into K3s (CLI-only install). Re-run `k7 install` later to add it.",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Enable verbose output"),
):
    """Install K7 on target hosts using Ansible.

    Single-node (default): `k7 install` provisions localhost with **both**
    backends (kata-firecracker-devmapper + kata-qemu-longhorn). Pass `--backend kfd` or
    `--backend kql` to install only one.

    Multi-node: `k7 install -i inventory.ini` reads roles, backends, and disks
    from the Ansible inventory. See `inventory.ini.example` for the layout.
    """
    if role not in ("server", "agent"):
        raise typer.BadParameter(f"--role must be 'server' or 'agent', got '{role}'")

    cni_value = (cni or "").strip().lower()
    if cni_value not in ("cilium", "flannel"):
        raise typer.BadParameter(f"--cni must be 'cilium' or 'flannel', got '{cni}'")

    if join and not join_token:
        raise typer.BadParameter("--join requires --join-token")

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

    backends_list = _parse_backends(backend)

    inventory_content = None
    if inventory and os.path.exists(inventory):
        with open(inventory) as f:
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
        inventory_user_supplied = bool(inventory)
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
        # The playbook builds the k7-api Docker image on the first master via
        # `delegate_to`, with `chdir` pointing to the source tree. Pass the
        # controller's cwd as `k7_repo_root`; the playbook validates that the
        # path exists on the first master and fails loudly otherwise. The user
        # is expected to invoke `k7 install` from a checkout of the repo (or
        # via SSH on a node that has the source tree at the same path).
        repo_root = os.getcwd()
        extra_vars = {
            # `k7_disk` is the legacy name; the playbook prefers `k7_devmapper_disk`
            # but still falls back to `k7_disk` for backward compat.
            "k7_disk": disk,
            # `k7_backends` (plural, comma-separated) is authoritative; the
            # playbook accepts a single `k7_backend` only as a back-compat
            # fallback when neither inventory `k7_backends` nor extra-var
            # `k7_backends` is set.
            "k7_backends": ",".join(backends_list),
            "longhorn_replicas": effective_replicas,
            "longhorn_data_path": longhorn_data_path,
            "longhorn_extra_disk": longhorn_extra_disk,
            "k7_cni": cni_value,
            "k7_repo_root": repo_root,
            # Spec 10d: ``--no-api`` flips the playbook's existing
            # ``k7_api_enabled`` gate (default true) so the API
            # manifests aren't applied.
            "k7_api_enabled": "false" if no_api else "true",
            # k7d backend artifact source overrides (spec 9a M11).
            "k7d_version": k7d_version,
            "k7d_artifact_local_path": k7d_artifact,
        }
        # Drop None to avoid leaking unused vars
        extra_vars = {k: v for k, v in extra_vars.items() if v is not None}
        # Ansible extra-vars have the highest precedence and would clobber
        # per-host `k7_backends` declared in a user-supplied inventory. Only
        # forward the CLI value when the user explicitly passed --backend;
        # otherwise the inventory is authoritative (spec 18e).
        backend_explicit = ctx.get_parameter_source("backend") == ParameterSource.COMMANDLINE
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
        "10Gi",
        "--root-disk-size",
        help="Longhorn root disk size, kata-qemu-longhorn backend only (e.g., 10Gi, 20Gi)",
    ),
    backend: str | None = typer.Option(
        None,
        "--backend",
        "-b",
        help="Backend: kata-firecracker-devmapper (kfd) or kata-qemu-longhorn (kql) (auto-detected if not specified)",
    ),
    sidecar: str | None = typer.Option(
        None,
        "--sidecar",
        help="Sidecar daemon type to inject (e.g. 'docker'). See SIDECAR_REGISTRY for available types.",
    ),
):
    """Create a new sandbox from YAML config or CLI arguments."""
    # Spec 18f issue 4: three explicit egress modes. --egress-open → open
    # (no policy, egress_whitelist=None); --egress ... → whitelist; neither
    # → block-all ([]). Combining both is ambiguous — fail loudly.
    if egress_open and egress_whitelist:
        raise typer.BadParameter("--egress-open cannot be combined with --egress (pick one egress mode)")

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
        )

    resolved_backend = _normalize_backend(sandbox_config.backend) if sandbox_config.backend else None
    if root_disk_size and resolved_backend == "kata-firecracker-devmapper":
        typer.echo("⚠️  --root-disk-size has no effect with the kata-firecracker-devmapper backend", err=True)

    if sandbox_config.sidecar is not None and sandbox_config.sidecar not in SIDECAR_REGISTRY:
        available = ", ".join(sorted(SIDECAR_REGISTRY.keys()))
        raise typer.BadParameter(f"Unknown sidecar type '{sandbox_config.sidecar}'. Available: {available}")

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

    params_lines = [
        f"Image: {sandbox_config.image}",
        f"Namespace: {sandbox_config.namespace}",
        f"Egress: {egress_mode}",
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
    kubectl_cmd = ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]
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
    if cli_ctx.use_core:
        group = RichGroup(status_text, details_text, progress)
        with RichLive(group, refresh_per_second=8, transient=False) as live:
            result = asyncio.run(core.create_sandbox(sandbox_config, progress_callback=on_progress))
        if not result.success:
            typer.echo(f"❌ Failed to create sandbox: {result.error}", err=True)
            raise typer.Exit(1)
        sandboxes_for_ready = asyncio.run(core.list_sandboxes(sandbox_config.namespace))
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
        sandboxes = [s.to_dict() for s in asyncio.run(core.list_sandboxes(namespace))]
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
        result = asyncio.run(core.delete_sandbox(name, namespace))
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
        sandboxes = [s.to_dict() for s in asyncio.run(core.list_sandboxes(namespace))]
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
        result = asyncio.run(core.delete_all_sandboxes(namespace))
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
        kubectl_cmd = ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]
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
        result = asyncio.run(core.exec_command(name, joined, namespace=namespace))
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

        metrics_list = asyncio.run(core.get_sandbox_metrics(namespace))

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


@app.command()
def generate_api_key(
    name: str,
    expires_days: int = typer.Option(365, help="API key expiration in days"),
    namespace: builtins.list[str] | None = typer.Option(
        None,
        "--namespace",
        "-n",
        help="Restrict key to this namespace (repeatable). Omit for unrestricted access.",
    ),
):
    """Generate a new API key."""
    api_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    api_keys = {}
    if API_KEYS_FILE.exists():
        with open(API_KEYS_FILE) as f:
            api_keys = json.load(f)

    expiry_timestamp = int((datetime.now() + timedelta(days=expires_days)).timestamp())
    entry: dict = {
        "name": name,
        "created": int(time.time()),
        "expires": expiry_timestamp,
        "last_used": None,
    }
    if namespace:
        # Preserve order, drop empties/duplicates.
        seen: set[str] = set()
        scoped: builtins.list[str] = []
        for ns in namespace:
            if ns and ns not in seen:
                seen.add(ns)
                scoped.append(ns)
        if scoped:
            entry["namespaces"] = scoped
    api_keys[key_hash] = entry

    _write_api_keys(api_keys)

    typer.echo(f"Generated API key for '{name}':")
    typer.echo(f"API Key: {api_key}")
    typer.echo(f"Expires: {datetime.fromtimestamp(expiry_timestamp)}")
    if entry.get("namespaces"):
        typer.echo(f"Namespaces: {', '.join(entry['namespaces'])}")
    else:
        typer.echo("Namespaces: * (unrestricted)")
    typer.echo("Keep this key secure - it won't be shown again!")


@app.command()
def list_api_keys():
    """List all API keys."""
    if not API_KEYS_FILE.exists():
        typer.echo("No API keys found.")
        return

    with open(API_KEYS_FILE) as f:
        api_keys = json.load(f)

    console = Console()
    table = Table(title="API Keys")
    table.add_column("Name", style="cyan")
    table.add_column("Created", style="blue")
    table.add_column("Expires", style="yellow")
    table.add_column("Last Used", style="green")
    table.add_column("Namespaces", style="magenta")

    for _key_hash, key_data in api_keys.items():
        created = datetime.fromtimestamp(key_data["created"]).strftime("%Y-%m-%d %H:%M")
        expires = datetime.fromtimestamp(key_data["expires"]).strftime("%Y-%m-%d %H:%M")
        last_used = "Never"
        if key_data["last_used"]:
            last_used = datetime.fromtimestamp(key_data["last_used"]).strftime("%Y-%m-%d %H:%M")
        namespaces = key_data.get("namespaces") or []
        ns_col = "*" if not namespaces else ", ".join(namespaces)

        table.add_row(key_data["name"], created, expires, last_used, ns_col)

    console.print(table)


@app.command()
def revoke_api_key(name: str):
    """Revoke an API key by name."""
    if not API_KEYS_FILE.exists():
        typer.echo("No API keys found.")
        return

    with open(API_KEYS_FILE) as f:
        api_keys = json.load(f)

    key_to_remove = None
    for key_hash, key_data in api_keys.items():
        if key_data["name"] == name:
            key_to_remove = key_hash
            break

    if key_to_remove:
        del api_keys[key_to_remove]
        _write_api_keys(api_keys)
        typer.echo(f"API key '{name}' revoked successfully.")
    else:
        typer.echo(f"API key '{name}' not found.")


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
        result = asyncio.run(core.pause_sandbox(name=name, namespace=namespace, snapshot_name=snapshot_name))
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
        result = asyncio.run(core.resume_sandbox(name=name, namespace=namespace))
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
        result = asyncio.run(
            core.restore_sandbox(
                snapshot_name=snapshot_name,
                new_sandbox_name=new_sandbox_name,
                namespace=namespace,
                overrides=overrides,
                keep_snapshot=keep_snapshot,
            )
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
    result = asyncio.run(
        core.fork_sandbox(
            source_name=source,
            new_name=new_name,
            namespace=namespace,
            snapshot_name=snapshot,
        )
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
# Spec 10d: ``k7 api ...`` sub-app — feature-toggle framing for the API server.
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
    failure mode 10d is trying to eliminate.
    """
    kubectl = _kubectl_cmd()
    result = subprocess.run(
        kubectl + ["scale", "deployment", "k7-api", "-n", "kube-system", f"--replicas={replicas}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        typer.echo(f"❌ Failed to scale k7-api to {replicas}: {result.stderr.strip()}", err=True)
        raise typer.Exit(1)


@api_app.command("status")
def api_status_cmd():
    """Show API server readiness, endpoint, and key-management hints."""
    kubectl = _kubectl_cmd()
    result = subprocess.run(
        kubectl
        + [
            "get",
            "deployment",
            "k7-api",
            "-n",
            "kube-system",
            "-o",
            "jsonpath={.status.readyReplicas}/{.spec.replicas}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        typer.echo("❌ K7 API deployment not found in K3s")
        typer.echo("Run `k7 install` to deploy it, or `k7 install --no-api` if that's deliberate.")
        return

    ready_info = result.stdout.strip()
    parts = ready_info.split("/")
    ready_count = int(parts[0]) if parts[0] else 0
    desired = int(parts[1]) if len(parts) > 1 and parts[1] else 0

    if ready_count > 0:
        typer.echo(f"✅ K7 API is running in K3s ({ready_count}/{desired} ready)")
    elif desired == 0:
        typer.echo("⚠️  K7 API deployment exists but is scaled to 0 (disabled). Run `k7 api enable`.")
    else:
        typer.echo(f"⚠️  K7 API deployment exists but no pods ready ({ready_count}/{desired})")

    pod_result = subprocess.run(
        kubectl + ["get", "pods", "-n", "kube-system", "-l", "app=k7-api", "-o", "wide", "--no-headers"],
        capture_output=True,
        text=True,
    )
    if pod_result.returncode == 0 and pod_result.stdout.strip():
        typer.echo(f"\n📦 Pod(s):\n{pod_result.stdout.strip()}")

    endpoint = _read_api_endpoint(kubectl)
    if endpoint:
        typer.echo(f"\n🌐 API endpoint: {endpoint}")

    typer.echo("\n📝 SDK usage example:")
    typer.echo("    from k7_sdk import Client")
    typer.echo("    k7 = Client(endpoint='<endpoint>', api_key='<key>')")
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


@api_app.command("enable")
def api_enable_cmd():
    """Scale the existing k7-api Deployment to 1 replica."""
    _api_scale_deployment(1)
    typer.echo("✅ K7 API enabled (replicas=1).")


@api_app.command("disable")
def api_disable_cmd():
    """Scale the existing k7-api Deployment to 0 replicas (temporary off)."""
    _api_scale_deployment(0)
    typer.echo("✅ K7 API disabled (replicas=0). Run `k7 api enable` to bring it back.")


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
    manifests = _get_api_manifests_dir()

    if not skip_build and not _build_and_import_api_image():
        typer.echo("⚠️  Image build skipped or failed; deployment may fail if image is not present", err=True)

    # Patch NodePort in the service manifest if non-default.
    service_file = Path(manifests) / "service.yaml"
    if api_port != 31007 and service_file.exists():
        content = service_file.read_text()
        content = content.replace("nodePort: 31007", f"nodePort: {api_port}")
        service_file.write_text(content)

    typer.echo("Re-applying K7 API manifests...")
    apply = subprocess.run(kubectl + ["apply", "-f", manifests], capture_output=True, text=True)
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
# Spec 18h: ``k7 nodes …`` sub-app for cluster-node queries.
# ---------------------------------------------------------------------------


nodes_app = typer.Typer(
    help="Query cluster nodes (storage pools, …).",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(nodes_app, name="nodes")


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
    table.add_column("ERROR", style="red")
    for node in sorted(data):
        entry = data[node]
        if "error" in entry:
            table.add_row(node, "-", "-", "-", "-", "-", entry["error"])
            continue
        tp = entry.get("kata_thinpool") or {}
        kd = entry.get("k7d_disks") or {}
        table.add_row(
            node,
            _format_size_bytes(int(tp.get("size_bytes", 0) or 0)),
            f"{float(tp.get('data_percent', 0)):.2f}",
            f"{float(tp.get('metadata_percent', 0)):.2f}",
            _format_size_bytes(int(kd.get("size_bytes", 0) or 0)),
            f"{float(kd.get('used_percent', 0)):.2f}",
            "",
        )
    Console().print(table)


@nodes_app.command("storage")
def nodes_storage(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON instead of a table"),
):
    """Show per-node kfd thin-pool and k7d disks-pool utilization."""
    cli_ctx: CliContext = ctx.obj
    if cli_ctx.use_core:
        data = asyncio.run(K7Core().nodes_storage())
    else:
        data = handle_api_call(lambda: cli_ctx.client().nodes_storage())
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    _print_nodes_storage_table(data)


# Spec 10e: ``k7 snapshot …`` sub-app for VolumeSnapshot CRUD + GC.
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
        snap_objs = asyncio.run(
            core.list_snapshots(
                namespace=namespace,
                all_namespaces=all_namespaces,
                sandbox=sandbox,
                kind=kind,
            )
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
        snap = asyncio.run(core.get_snapshot(name, namespace=namespace))
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
        result = asyncio.run(core.create_snapshot(sandbox_name, snapshot_name, namespace=namespace))
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
        result = asyncio.run(core.delete_snapshot(name, namespace=namespace))
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

    Pause snapshots and named snapshots are never touched.
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
        result = asyncio.run(
            core.gc_snapshots(
                namespace=namespace,
                all_namespaces=all_namespaces,
                keep_fork_for=td,
                dry_run=dry_run,
            )
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
