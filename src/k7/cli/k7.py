#!/usr/bin/env python3

import os
import subprocess
import time
import threading
import typer
import json
import hashlib
import secrets
from pathlib import Path
from typing import Optional, List
from datetime import datetime, timedelta
from rich.live import Live
from rich.table import Table
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.console import Console, Group
from rich.text import Text
import shutil
import socket

from k7 import __version__ as K7_VERSION

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

app = typer.Typer(context_settings={"help_option_names": ["-h", "--help"]})


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
):
    if version:
        typer.echo(K7_VERSION)
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # Show top-level help when no command is provided
        try:
            typer.echo(ctx.get_help())
        except Exception:
            typer.echo("Usage: k7 [OPTIONS] COMMAND [ARGS]...\nTry 'k7 -h' for help.")
        raise typer.Exit()


API_KEYS_FILE = Path(os.getenv("K7_API_KEYS_FILE", "/etc/k7/api_keys.json"))
def _detect_host_ip_for_kubeapi() -> Optional[str]:
    """Detect the host IP address to reach the kube-apiserver from a container.

    Uses a UDP connect trick to a well-known internet IP to determine the
    primary outbound interface IP.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("1.1.1.1", 80))
        host_ip = s.getsockname()[0]
        s.close()
        return host_ip
    except Exception:
        return None


def _prepare_container_kubeconfig_and_override(compose_path: str) -> Optional[str]:
    """Create a container-friendly kubeconfig and a compose override.

    - Copies host kubeconfig (env KUBECONFIG or /etc/rancher/k3s/k3s.yaml)
    - Rewrites server URL from 127.0.0.1 to host primary IP (if needed)
    - Writes to /etc/k7/k3s.docker.yaml (fallbacks to user data dir if needed)
    - Generates a small compose override that mounts the rewritten kubeconfig
      to /etc/rancher/k3s/k3s.yaml inside the container.

    Returns the override file path to pass as an extra -f to docker compose.
    """
    host_kube = os.getenv("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml")
    try:
        content = Path(host_kube).read_text()
    except Exception:
        return None

    # Only rewrite if server is pointing to 127.0.0.1
    if "https://127.0.0.1:6443" in content:
        host_ip = _detect_host_ip_for_kubeapi()
        if not host_ip:
            return None
        content = content.replace("https://127.0.0.1:6443", f"https://{host_ip}:6443")

    # Always use root-owned secure path only
    d = Path("/etc/k7")
    try:
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except Exception:
            pass
        kube_out = d / "k3s.docker.yaml"
        kube_out.write_text(content)
        try:
            os.chmod(kube_out, 0o600)
        except Exception:
            pass
        # Verify effective permissions
        try:
            dir_mode = d.stat().st_mode & 0o777
            file_mode = kube_out.stat().st_mode & 0o777
            if (dir_mode & 0o077) != 0 or (file_mode & 0o077) != 0:
                try:
                    kube_out.unlink(missing_ok=True)
                except Exception:
                    pass
                return None
        except Exception:
            return None
    except Exception:
        return None

    # Build a minimal override compose that remaps the kubeconfig source
    override_content = (
        "services:\n"
        "  k7-api:\n"
        f"    volumes:\n      - {str(kube_out)}:/etc/rancher/k3s/k3s.yaml:ro\n"
    )

    override_path = d / "k7-compose.override.yml"
    try:
        override_path.write_text(override_content)
    except Exception:
        return None

    return str(override_path)


def _resolve_compose_path_or_fail(user_compose_file: Optional[str]) -> (str, str):
    """Resolve the docker-compose.yml path and its working directory.

    Preference order:
    1) Explicit --compose-file (must exist)
    2) Packaged compose inside the installed/bundled package (works with Nuitka)

    Fails with exit code 1 if none are available.
    Returns (compose_path, workdir)
    """
    if user_compose_file:
        p = Path(user_compose_file)
        if not p.exists():
            typer.echo(f"❌ Provided compose file not found: {user_compose_file}", err=True)
            raise typer.Exit(1)
        return str(p), str(p.parent)

    # Use the embedded paths exposed by core helpers to ensure consistency
    core = K7Core()
    compose_path = core._get_embedded_docker_compose()
    dockerfile_path = core._get_embedded_dockerfile_api()
    if compose_path and dockerfile_path:
        return compose_path, str(Path(compose_path).parent)

    typer.echo("❌ docker-compose.yml not found. Ensure it is available in the package or specify --compose-file.", err=True)
    raise typer.Exit(1)


@app.command()
def install(
    hosts: Optional[List[str]] = typer.Argument(
        None, help="Optional target hosts; defaults to localhost"
    ),
    playbook: Optional[str] = typer.Option(
        None, "-p", "--playbook", help="Path to custom Ansible playbook"
    ),
    inventory: Optional[str] = typer.Option(
        None, "-i", "--inventory", help="Path to custom Ansible inventory"
    ),
    disk: Optional[str] = typer.Option(
        None,
        "--disk",
        help="Block device to use for LVM thin-pool (e.g., /dev/nvme2n1)",
    ),
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Enable verbose output"
    ),
):
    """Install K7 on target hosts using Ansible."""

    playbook_content = None
    if playbook and os.path.exists(playbook):
        with open(playbook, "r") as f:
            playbook_content = f.read()

    inventory_content = None
    if inventory and os.path.exists(inventory):
        with open(inventory, "r") as f:
            inventory_content = f.read()
    else:
        inventory_lines = ["[k7_nodes]"]
        if hosts:
            for host in hosts:
                inventory_lines.append(
                    f"{host} ansible_user=root ansible_ssh_private_key_file=~/.ssh/id_rsa"
                )
        else:
            inventory_lines.append(
                "localhost ansible_connection=local ansible_user=root"
            )
        inventory_content = "\n".join(inventory_lines)

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

        # Kick off install and update UI as lines arrive
        extra_vars = {"k7_disk": disk} if disk else None
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
    name: Optional[str] = None,
    image: Optional[str] = None,
    config: Optional[str] = typer.Option(
        None, "-f", "--file", help="Path to k7.yaml config file"
    ),
    namespace: str = typer.Option(
        "default", "-n", "--namespace", help="Kubernetes namespace"
    ),
    cpu_limit: Optional[str] = typer.Option(
        None, "--cpu", help="CPU limit (e.g., '1', '500m')"
    ),
    memory_limit: Optional[str] = typer.Option(
        None, "--memory", help="Memory limit (e.g., '1Gi', '512Mi')"
    ),
    storage_limit: Optional[str] = typer.Option(
        None, "--storage", help="Ephemeral storage limit (e.g., '2Gi', '1Gi')"
    ),
    env_file: Optional[str] = typer.Option(
        None, "--env-file", help="Path to environment file containing secrets"
    ),
    egress_whitelist: Optional[List[str]] = typer.Option(
        None,
        "--egress",
        help="CIDR blocks for egress whitelist (can be used multiple times)",
    ),
    before_script: Optional[str] = typer.Option(
        None,
        "--before-script",
        help="Script to run before starting the main container process",
    ),
    pod_non_root: Optional[bool] = typer.Option(
        None,
        "--pod-non-root/--no-pod-non-root",
        help="Run pod with non-root defaults (user/group/fsGroup 65532)",
    ),
    container_non_root: Optional[bool] = typer.Option(
        None,
        "--container-non-root/--no-container-non-root",
        help="Run main container as non-root (uid 65532)",
    ),
    cap_add: Optional[List[str]] = typer.Option(
        None,
        "--cap-add",
        help="Linux capabilities to add back (can be used multiple times)",
    ),
    cap_drop: Optional[List[str]] = typer.Option(
        None,
        "--cap-drop",
        help="Linux capabilities to drop (can be used multiple times)",
    ),
):
    """Create a new sandbox from YAML config or CLI arguments."""
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
        if egress_whitelist:
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
        if not name or not image:
            raise typer.BadParameter(
                "Name and image must be provided via CLI or k7.yaml"
            )

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
            egress_whitelist=egress_whitelist or [],
            limits=limits if limits else None,
            before_script=before_script or "",
            pod_non_root=pod_non_root if pod_non_root is not None else False,
            container_non_root=container_non_root if container_non_root is not None else False,
            cap_add=cap_add,
            cap_drop=cap_drop,
        )

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
                    progress.update(
                        stage_task, description="[green]Provisioned[/green]", total=None
                    )
            elif stage == "before_script":
                if status == "waiting":
                    script = event.get("script", "")
                    first_line = script.strip().split("\n")[0]
                    shown = (
                        first_line
                        if len(first_line) <= 200
                        else first_line[:200] + "..."
                    )
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
                progress.update(
                    stage_task, description=f"[green]{msg}[/green]", total=None
                )
            elif stage == "error":
                err = event.get("error", "")
                status_text.plain = f"Error: {err}"
                status_text.stylize("red")
                progress.update(
                    stage_task, description=f"[red]Error: {err}[/red]", total=None
                )
        except Exception:
            pass

    from rich.console import Group as RichGroup
    from rich.live import Live as RichLive

    # Show key YAML parameters up-front for clarity
    try:
        egress_mode = (
            "open" if sandbox_config.egress_whitelist is None else (
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
    log_thread: Optional[threading.Thread] = None
    log_started_event: threading.Event = threading.Event()
    log_ended_event: threading.Event = threading.Event()
    before_log_lines: list[str] = []

    resolved_pod_name: Optional[str] = None

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
                    progress.update(
                        stage_task, description="[green]Provisioned[/green]", total=None
                    )
            elif stage == "before_script":
                if status == "waiting":
                    script = event.get("script", "")
                    first_line = script.strip().split("\n")[0]
                    shown = (
                        first_line
                        if len(first_line) <= 200
                        else first_line[:200] + "..."
                    )
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
                            details_text.plain = ("Before script log tail (last 50 lines):\n" + tail)
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
                        live.console.print(RichText(f"===== END before_script ({sandbox_config.name}) =====", style="bold"))
                    except Exception:
                        pass
                # Keep the log tail summary visible after completion
                progress.update(
                    stage_task, description=f"[green]{msg}[/green]", total=None
                )
                stop_log_event.set()
                # Do not attempt any file-based fallbacks; only stream container logs
            elif stage == "error":
                err = event.get("error", "")
                status_text.plain = f"Error: {err}"
                status_text.stylize("red")
                progress.update(
                    stage_task, description=f"[red]Error: {err}[/red]", total=None
                )
                stop_log_event.set()
        except Exception:
            pass

    group = RichGroup(status_text, details_text, progress)
    with RichLive(group, refresh_per_second=8, transient=False) as live:
        result = core.create_sandbox(sandbox_config, progress_callback=on_progress)

    if not result.success:
        typer.echo(f"❌ Failed to create sandbox: {result.error}", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"👉 Shell in: k7 shell {sandbox_config.name}"
        + (
            f" -n {sandbox_config.namespace}"
            if sandbox_config.namespace != "default"
            else ""
        )
    )


@app.command()
def list(
    namespace: Optional[str] = typer.Option(
        None,
        "-n",
        "--namespace",
        help="Filter sandboxes by namespace. If not provided, shows sandboxes from all namespaces.",
    ),
):
    """List all running sandboxes."""
    core = K7Core()
    sandboxes = core.list_sandboxes(namespace)

    if not sandboxes:
        if namespace:
            typer.echo(f"No sandboxes found in namespace '{namespace}'.")
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
    table.add_column("Error", style="red")

    for sandbox in sandboxes:
        if sandbox.status == "Running":
            status_display = f"[green]{sandbox.status}[/green]"
        elif sandbox.status == "Pending":
            status_display = f"[yellow]{sandbox.status}[/yellow]"
        elif sandbox.status == "Failed":
            status_display = f"[red]{sandbox.status}[/red]"
        else:
            status_display = sandbox.status

        ready_display = (
            f"[green]{sandbox.ready}[/green]"
            if sandbox.ready == "True"
            else f"[red]{sandbox.ready}[/red]"
        )

        table.add_row(
            sandbox.name,
            sandbox.namespace,
            status_display,
            ready_display,
            str(sandbox.restarts),
            sandbox.age,
            sandbox.image,
            sandbox.error_message,
        )

    console.print(table)


@app.command()
def delete(
    name: str,
    namespace: str = typer.Option(
        "default",
        "-n",
        "--namespace",
        help="Kubernetes namespace containing the sandbox.",
    ),
):
    """Delete a sandbox and all its associated resources."""
    core = K7Core()
    result = core.delete_sandbox(name, namespace)

    if result.success:
        typer.echo(f"✅ {result.message}")
    else:
        typer.echo(f"❌ Failed to delete sandbox: {result.error}", err=True)
        raise typer.Exit(1)


@app.command()
def delete_all(
    namespace: str = typer.Option(
        "default",
        "-n",
        "--namespace",
        help="Kubernetes namespace to delete sandboxes from.",
    ),
):
    """Delete all sandboxes in a namespace."""
    core = K7Core()

    sandboxes = core.list_sandboxes(namespace)

    if not sandboxes:
        typer.echo(f"No sandboxes found in namespace {namespace}")
        return

    sandbox_names = [s.name for s in sandboxes]
    typer.echo(f"Found {len(sandbox_names)} sandbox(es) in namespace {namespace}:")
    for name in sandbox_names:
        typer.echo(f"  - {name}")

    if not typer.confirm("Are you sure you want to delete all these sandboxes?"):
        typer.echo("Deletion cancelled")
        return

    result = core.delete_all_sandboxes(namespace)

    if result.success:
        typer.echo(f"✅ {result.message}")
    else:
        typer.echo(f"❌ Failed to delete all sandboxes: {result.error}", err=True)
        if result.data:
            for item in result.data:
                if not item["success"]:
                    typer.echo(f"  - {item['name']}: {item['error']}")
        raise typer.Exit(1)


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
    kubectl_cmd = ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]
    subprocess.run(
        kubectl_cmd + ["exec", "-it", f"deploy/{name}", "-n", namespace, "--", "sh"]
    )


@app.command()
def logs(
    name: str,
    namespace: str = typer.Option(
        "default",
        "-n",
        "--namespace",
        help="Kubernetes namespace containing the sandbox.",
    ),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow logs output"),
    tail: int = typer.Option(
        200, "--tail", help="Number of lines to show from the end of the logs"
    ),
):
    """Show sandbox pod logs (before script and main container)."""
    kubectl_cmd = ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]

    # Resolve pod name
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
        typer.echo(
            f"❌ Failed to resolve pod for sandbox '{name}': {e.stderr.strip()}",
            err=True,
        )
        raise typer.Exit(1)

    if not pod_name:
        typer.echo(
            f"❌ No pod found for sandbox '{name}' in namespace '{namespace}'.",
            err=True,
        )
        raise typer.Exit(1)

    # Show logs; since before_script runs in main container, one container is enough
    args = ["logs", pod_name, "-n", namespace, "--tail", str(tail)]
    if follow:
        args.append("-f")
    subprocess.run(kubectl_cmd + args)


@app.command()
def top(
    refresh_interval: int = 1,
    namespace: Optional[str] = typer.Option(
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

        # Get metrics from core
        metrics_list = core.get_sandbox_metrics(namespace)

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
    name: str, expires_days: int = typer.Option(365, help="API key expiration in days")
):
    """Generate a new API key."""
    api_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    api_keys = {}
    if API_KEYS_FILE.exists():
        with open(API_KEYS_FILE, "r") as f:
            api_keys = json.load(f)

    expiry_timestamp = int((datetime.now() + timedelta(days=expires_days)).timestamp())
    api_keys[key_hash] = {
        "name": name,
        "created": int(time.time()),
        "expires": expiry_timestamp,
        "last_used": None,
    }

    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w") as f:
        json.dump(api_keys, f, indent=2)
    os.chmod(API_KEYS_FILE, 0o600)

    typer.echo(f"Generated API key for '{name}':")
    typer.echo(f"API Key: {api_key}")
    typer.echo(f"Expires: {datetime.fromtimestamp(expiry_timestamp)}")
    typer.echo("Keep this key secure - it won't be shown again!")


@app.command()
def list_api_keys():
    """List all API keys."""
    if not API_KEYS_FILE.exists():
        typer.echo("No API keys found.")
        return

    with open(API_KEYS_FILE, "r") as f:
        api_keys = json.load(f)

    console = Console()
    table = Table(title="API Keys")
    table.add_column("Name", style="cyan")
    table.add_column("Created", style="blue")
    table.add_column("Expires", style="yellow")
    table.add_column("Last Used", style="green")

    for key_hash, key_data in api_keys.items():
        created = datetime.fromtimestamp(key_data["created"]).strftime("%Y-%m-%d %H:%M")
        expires = datetime.fromtimestamp(key_data["expires"]).strftime("%Y-%m-%d %H:%M")
        last_used = "Never"
        if key_data["last_used"]:
            last_used = datetime.fromtimestamp(key_data["last_used"]).strftime(
                "%Y-%m-%d %H:%M"
            )

        table.add_row(key_data["name"], created, expires, last_used)

    console.print(table)


@app.command()
def revoke_api_key(name: str):
    """Revoke an API key by name."""
    if not API_KEYS_FILE.exists():
        typer.echo("No API keys found.")
        return

    with open(API_KEYS_FILE, "r") as f:
        api_keys = json.load(f)

    key_to_remove = None
    for key_hash, key_data in api_keys.items():
        if key_data["name"] == name:
            key_to_remove = key_hash
            break

    if key_to_remove:
        del api_keys[key_to_remove]
        with open(API_KEYS_FILE, "w") as f:
            json.dump(api_keys, f, indent=2)
        typer.echo(f"API key '{name}' revoked successfully.")
    else:
        typer.echo(f"API key '{name}' not found.")


@app.command()
def start_api(
    port: int = typer.Option(8000, help="Port to run API on"),
    host: str = typer.Option("0.0.0.0", help="Host to bind to"),
    compose_file: Optional[str] = typer.Option(
        None,
        "--compose-file",
        help="Path to docker-compose.yml to use when --docker is set",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Non-interactive; accept using local image overrides if set",
    ),
):
    """Start the K7 API server."""
    typer.echo("Starting K7 API with Docker Compose...")
    cmd = ["docker", "compose"]
    workdir = None
    compose_path: Optional[str] = None
    compose_path, workdir = _resolve_compose_path_or_fail(compose_file)

    cmd += ["-f", compose_path]
    # Add kubeconfig override if needed so container can reach apiserver
    kube_override = _prepare_container_kubeconfig_and_override(compose_path)
    if kube_override:
        cmd += ["-f", kube_override]
    # Safety: if user overrides image/tag via env, confirm unless --yes provided
    use_local_image = bool(os.getenv("K7_API_IMAGE")) or bool(os.getenv("K7_API_TAG"))
    auto_yes = yes
    if use_local_image and not auto_yes:
        typer.echo("Detected local override via K7_API_IMAGE/K7_API_TAG.")
        typer.echo("Use this local image? [y/N] (unset env vars to use remote)")
        try:
            choice = input().strip().lower()
        except EOFError:
            choice = "n"
        if choice != "y":
            # Unset envs for this process so compose pulls remote
            os.environ.pop("K7_API_IMAGE", None)
            os.environ.pop("K7_API_TAG", None)
    # Ensure relative paths in compose resolve properly
    workdir = str(Path(workdir))
    # No build step: image is either remote (default) or local (override)
    up_args = cmd + ["up", "-d"]
    if use_local_image:
        # Avoid pulling when a local override is requested
        up_args += ["--pull", "never"]
    up = subprocess.run(up_args, cwd=workdir)
    if up.returncode != 0:
        typer.echo("❌ Failed to start API via Docker Compose", err=True)
        raise typer.Exit(1)

    # Try to display endpoint if available (read docker logs directly)
    try:
        # Try docker logs first
        logs = subprocess.run(
            ["docker", "logs", "--since", "15m", "--tail", "10000", "k7-cloudflared"],
            capture_output=True,
            text=True,
        )
        public_url = None
        if logs.returncode == 0 and logs.stdout:
            for line in logs.stdout.splitlines():
                if "trycloudflare.com" in line:
                    parts = [
                        tok
                        for tok in line.split()
                        if tok.startswith("https://") or tok.startswith("http://")
                    ]
                    if parts:
                        public_url = parts[0]
                        break
        # Fallback to compose logs if not found
        if not public_url:
            compose_logs = subprocess.run(cmd + ["logs", "cloudflared"], capture_output=True, text=True)
            if compose_logs.returncode == 0 and compose_logs.stdout:
                for line in compose_logs.stdout.splitlines():
                    if "trycloudflare.com" in line:
                        parts = [
                            tok
                            for tok in line.split()
                            if tok.startswith("https://") or tok.startswith("http://")
                        ]
                        if parts:
                            public_url = parts[0]
                            break
        if public_url:
            typer.echo(f"API started. Public endpoint: {public_url}")
        else:
            typer.echo("API started.")
            typer.echo("Next steps:")
            typer.echo("- Run: k7 api-status")
            typer.echo("- Run: k7 get-api-endpoint")
            typer.echo("- Generate API key: k7 generate-api-key <name>")
            typer.echo("- List API keys: k7 list-api-keys")
            typer.echo("- Stop when done: k7 stop-api")
    except Exception:
        typer.echo("API started.")
        typer.echo("Next steps:")
        typer.echo("- Run: k7 api-status")
        typer.echo("- Run: k7 get-api-endpoint")
        typer.echo("- Generate API key: k7 generate-api-key <name>")
        typer.echo("- List API keys: k7 list-api-keys")
        typer.echo("- Stop when done: k7 stop-api")


@app.command()
def api_status(
    compose_file: Optional[str] = typer.Option(
        None, "--compose-file", help="Path to docker-compose.yml used to start the API"
    ),
):
    """Show API server status and connection info."""
    try:
        cmd = ["docker", "compose"]
        compose_path, _workdir = _resolve_compose_path_or_fail(compose_file)
        cmd += ["-f", compose_path]

        # Prefer raw docker inspect to avoid compose context mismatches
        inspect_api = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "k7-api"], capture_output=True, text=True)
        inspect_tun = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "k7-cloudflared"], capture_output=True, text=True)

        if inspect_api.stdout.strip() == "true" and inspect_tun.stdout.strip() == "true":
            typer.echo("✅ K7 API is running via Docker Compose")
            # Try to extract Cloudflared public URL from logs
            logs = subprocess.run(["docker", "logs", "--since", "15m", "--tail", "10000", "k7-cloudflared"], capture_output=True, text=True)
            public_url = None
            for line in logs.stdout.splitlines():
                if "trycloudflare.com" in line:
                    # simple parse to extract URL
                    parts = [
                        tok
                        for tok in line.split()
                        if tok.startswith("https://") or tok.startswith("http://")
                    ]
                    if parts:
                        public_url = parts[0]
                        break
            if public_url:
                typer.echo(f"🌐 Public URL: {public_url}")
            else:
                # Fallback to compose logs if docker logs missed it
                compose_logs = subprocess.run(cmd + ["logs", "cloudflared"], capture_output=True, text=True)
                if compose_logs.returncode == 0:
                    for line in compose_logs.stdout.splitlines():
                        if "trycloudflare.com" in line:
                            parts = [
                                tok
                                for tok in line.split()
                                if tok.startswith("https://") or tok.startswith("http://")
                            ]
                            if parts:
                                public_url = parts[0]
                                break
                if public_url:
                    typer.echo(f"🌐 Public URL: {public_url}")
                else:
                    typer.echo("🌐 Public URL: not detected yet; try 'k7 get-api-endpoint'")

            typer.echo("\n📝 SDK Usage Example:")
            typer.echo("from katakate import Client")
            typer.echo("k7 = Client(endpoint='https://<endpoint>', api_key='<key>')")
            typer.echo("sb = k7.create({'name': 'test', 'image': 'alpine:latest'})")
            typer.echo("print(k7.list())")
            typer.echo("print(sb.exec('echo Hello'))  # returns dict with stdout/stderr")
            typer.echo("\n🔐 Manage API keys:")
            typer.echo("k7 generate-api-key <name>")
            typer.echo("k7 list-api-keys")
            typer.echo("k7 revoke-api-key <name>")
        else:
            typer.echo("❌ K7 API is not running")
            typer.echo("Start with: k7 start-api")

    except FileNotFoundError:
        typer.echo("❌ Docker not found or compose plugin missing")
        typer.echo("Install Docker and compose plugin.")


@app.command()
def stop_api(
    compose_file: Optional[str] = typer.Option(
        None, "--compose-file", help="Path to docker-compose.yml used to start the API"
    ),
    remove_volumes: bool = typer.Option(
        False, "--prune", help="Remove named volumes as part of shutdown"
    ),
):
    """Stop the K7 API server and Cloudflared tunnel."""
    cmd = ["docker", "compose"]
    compose_path, _workdir = _resolve_compose_path_or_fail(compose_file)
    cmd += ["-f", compose_path]
    down_cmd = cmd + ["down"] + (["-v"] if remove_volumes else [])
    try:
        subprocess.run(down_cmd, check=False)
    finally:
        # Ensure containers are gone even if compose file moved
        subprocess.run(
            ["docker", "rm", "-f", "k7-api", "k7-cloudflared"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Clean extracted build context for a fresh next start
        try:
            shutil.rmtree("/tmp/k7-api-build")
        except Exception:
            pass
    typer.echo("K7 API stopped")


@app.command()
def get_api_endpoint(
    compose_file: Optional[str] = typer.Option(
        None, "--compose-file", help="Path to docker-compose.yml used to start the API"
    ),
):
    """Print the current Cloudflared public URL for the API, if available."""
    cmd = ["docker", "compose"]
    compose_path, _workdir = _resolve_compose_path_or_fail(compose_file)
    cmd += ["-f", compose_path]

    # Check if service is up via raw docker inspect
    inspect_api = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "k7-api"], capture_output=True, text=True)
    inspect_tun = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "k7-cloudflared"], capture_output=True, text=True)
    if inspect_api.stdout.strip() != "true" or inspect_tun.stdout.strip() != "true":
        raise typer.Exit(1)

    # Get URL from docker logs (same approach as api-status for consistency)
    url = None
    logs = subprocess.run(["docker", "logs", "--since", "15m", "--tail", "10000", "k7-cloudflared"], capture_output=True, text=True)
    if logs.returncode == 0 and logs.stdout:
        for line in logs.stdout.splitlines():
            if "trycloudflare.com" in line:
                parts = [
                    tok
                    for tok in line.split()
                    if tok.startswith("https://") or tok.startswith("http://")
                ]
                if parts:
                    url = parts[0]
                    break
    if not url:
        # Fallback to compose logs using resolved compose file (no /tmp fallback)
        cmd = ["docker", "compose", "-f", compose_path]
        for _ in range(5):
            clogs = subprocess.run(cmd + ["logs", "cloudflared"], capture_output=True, text=True)
            if clogs.returncode == 0 and clogs.stdout:
                for line in clogs.stdout.splitlines():
                    if "trycloudflare.com" in line:
                        parts = [
                            tok
                            for tok in line.split()
                            if tok.startswith("https://") or tok.startswith("http://")
                        ]
                        if parts:
                            url = parts[0]
                            break
            if url:
                break
            time.sleep(1)
    if url:
        typer.echo(url)
    else:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
