import asyncio
import copy
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from importlib import resources
from typing import Any

import httpx
import yaml
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.stream import WsApiClient

from .models import (
    SNAPSHOT_KIND_FORK,
    SNAPSHOT_KIND_NAMED,
    SNAPSHOT_KIND_PAUSE,
    ExecResult,
    OperationResult,
    SandboxConfig,
    SandboxConfigOverrides,
    SandboxInfo,
    SnapshotInfo,
)
from .sidecar import SIDECAR_REGISTRY

# k7d backend (spec 9a M11): pod-level annotations the k7d containerd shim
# understands. `fork-source-*` boot a pod as a warm (CoW disk+memory) fork
# of a live source sandbox VM (k7d spec 17d).
K7D_ANN_FORK_SOURCE_CLUSTER = "k7d.katakate.org/fork-source-cluster"
K7D_ANN_FORK_SOURCE_VM = "k7d.katakate.org/fork-source-vm"
# Deployment-level marker k7 stamps so `k7 list` / resume can tell a
# VM-frozen (paused) k7d sandbox from a running one.
K7D_ANN_PAUSED = "k7.katakate.org/k7d-paused"
# Message pointing users at k7d's native VM snapshot trees for the verbs
# that intentionally do NOT map onto the k7d backend.
K7D_SNAPSHOT_UNSUPPORTED = (
    "named disk snapshots/restore are a Longhorn feature of the kata-qemu-longhorn (kql) "
    "backend. k7d sandboxes support live warm fork (`k7 fork`) and VM pause/resume instead; "
    "VM-level snapshot trees (fork/rollback/suspend of whole VM states) are available through "
    "the k7d daemon API — see https://github.com/katakate/k7d"
)


def _classify_egress_entries(entries: list[str]) -> tuple[list[str], list[str]]:
    """Split egress values into (CIDRs, FQDNs).

    An entry is treated as a CIDR when it is a valid IPv4/IPv6 network; bare
    IPs are normalised to their /32 (or /128) form. Anything else is treated
    as an FQDN (supports exact names and simple `*.example.com` wildcards).
    """
    cidrs: list[str] = []
    fqdns: list[str] = []
    for raw in entries:
        value = (raw or "").strip()
        if not value:
            continue
        try:
            if "/" in value:
                net = ipaddress.ip_network(value, strict=False)
                cidrs.append(str(net))
            else:
                ip = ipaddress.ip_address(value)
                cidrs.append(f"{ip}/{'32' if ip.version == 4 else '128'}")
        except ValueError:
            fqdns.append(value)
    return cidrs, fqdns


class K7Core:
    """Core business logic for sandbox management"""

    def __init__(self, kubeconfig_path: str | None = None):
        self.kubeconfig_path = kubeconfig_path
        self._apps_v1_client = None
        self._core_v1_client = None
        self._networking_v1_client = None
        self._metrics_client = None
        self._custom_objects_client = None
        self._config_loaded = False
        self._api_client = None

    async def _load_k3s_config(self):
        """Load k3s kubeconfig with fallback to standard locations.

        When running inside a pod (KUBERNETES_SERVICE_HOST is set), prefer
        in-cluster config via the ServiceAccount token.  This avoids needing
        a hostPath kubeconfig mount and gives explicit RBAC scoping.
        """
        if self._config_loaded:
            return

        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            try:
                config.load_incluster_config()
                self._config_loaded = True
                return
            except config.ConfigException:
                pass

        k3s_config_path = self.kubeconfig_path or "/etc/rancher/k3s/k3s.yaml"

        try:
            if os.path.exists(k3s_config_path):
                await config.load_kube_config(config_file=k3s_config_path)
            else:
                await config.load_kube_config()
        except config.ConfigException:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                raise Exception("Could not load Kubernetes config")

        self._config_loaded = True

    async def _get_apps_v1_client(self):
        """Get or create AppsV1Api client instance."""
        if self._apps_v1_client is None:
            await self._load_k3s_config()
            self._apps_v1_client = client.AppsV1Api()
        return self._apps_v1_client

    async def _get_core_v1_client(self):
        """Get or create CoreV1Api client instance."""
        if self._core_v1_client is None:
            await self._load_k3s_config()
            self._core_v1_client = client.CoreV1Api()
        return self._core_v1_client

    async def _get_networking_v1_client(self):
        """Get or create NetworkingV1Api client instance."""
        if self._networking_v1_client is None:
            await self._load_k3s_config()
            self._networking_v1_client = client.NetworkingV1Api()
        return self._networking_v1_client

    async def _get_metrics_client(self):
        """Get or create CustomObjectsApi client for metrics."""
        if self._metrics_client is None:
            await self._load_k3s_config()
            self._metrics_client = client.CustomObjectsApi()
        return self._metrics_client

    async def _get_custom_objects_client(self):
        """Get or create CustomObjectsApi client for VolumeSnapshots."""
        if self._custom_objects_client is None:
            await self._load_k3s_config()
            self._custom_objects_client = client.CustomObjectsApi()
        return self._custom_objects_client

    def _load_persist_bind_script(self) -> str:
        """Load the bind-mount wrapper script from package assets."""
        try:
            return resources.files("k7").joinpath("assets/k7-persist-bind.sh").read_text()
        except Exception as e:
            raise Exception(f"Failed to load k7-persist-bind.sh: {e}") from e

    def _normalize_image_argv(self, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value if v is not None]
        if isinstance(value, str):
            return [value] if value else []
        return []

    def _extract_entrypoint_cmd(self, data: dict) -> tuple[list, list]:
        info = data.get("info") if isinstance(data, dict) else None
        if isinstance(info, dict) and isinstance(info.get("config"), dict):
            ep = self._normalize_image_argv(info["config"].get("Entrypoint"))
            cmd = self._normalize_image_argv(info["config"].get("Cmd"))
            return ep, cmd
        config_data = data.get("config") if isinstance(data, dict) else None
        if isinstance(config_data, dict):
            ep = self._normalize_image_argv(config_data.get("Entrypoint"))
            cmd = self._normalize_image_argv(config_data.get("Cmd"))
            return ep, cmd
        return [], []

    def _parse_image_reference(self, image: str) -> tuple[str, str, str]:
        """Parse a container image reference into (registry, repository, tag_or_digest).

        Handles Docker Hub short names (alpine:3.21 → registry-1.docker.io/library/alpine, 3.21).
        """
        ref = image
        tag = "latest"
        if "@" in ref:
            ref, tag = ref.rsplit("@", 1)
        elif ":" in ref:
            parts = ref.rsplit(":", 1)
            if "/" in parts[1] or parts[1].isdigit():
                pass
            else:
                ref, tag = parts[0], parts[1]

        if "/" not in ref:
            return "registry-1.docker.io", f"library/{ref}", tag
        first_part = ref.split("/")[0]
        if "." in first_part or ":" in first_part or first_part == "localhost":
            registry = first_part
            repo = ref[len(first_part) + 1 :]
        else:
            registry = "registry-1.docker.io"
            repo = ref
        return registry, repo, tag

    @staticmethod
    def _registry_hostname(registry: str) -> str:
        """Extract hostname from a registry authority (host or host:port or [ipv6]:port)."""
        if not registry or not isinstance(registry, str):
            raise ValueError("Registry host must be a non-empty string")
        if registry.startswith("["):
            end = registry.find("]")
            if end == -1:
                raise ValueError(f"Invalid registry host: {registry}")
            return registry[1:end]
        if ":" in registry:
            host, maybe_port = registry.rsplit(":", 1)
            if maybe_port.isdigit():
                return host
        return registry

    def _registry_allowlist(self) -> set[str]:
        """Default public registries, extended by ``K7_REGISTRY_ALLOWLIST`` (comma-separated)."""
        allow = {"registry-1.docker.io", "ghcr.io", "quay.io", "public.ecr.aws"}
        extra = os.environ.get("K7_REGISTRY_ALLOWLIST", "").strip()
        if extra:
            allow |= {h.strip().lower() for h in extra.split(",") if h.strip()}
        return allow

    def _assert_registry_host_allowed(self, registry: str) -> None:
        """Reject registry hosts that resolve to non-public addresses (SSRF guard).

        Always-on backstop: every resolved A/AAAA must be a public unicast address
        (not loopback/private/link-local/reserved/multicast/unspecified). Tightening
        layer: hostname must be in the allowlist (defaults + ``K7_REGISTRY_ALLOWLIST``).
        """
        hostname = self._registry_hostname(registry).lower()
        if hostname in {"localhost", "metadata.google.internal"}:
            raise ValueError(f"Registry host not allowed: {registry}")

        allowlist = self._registry_allowlist()
        if hostname not in allowlist:
            raise ValueError(
                f"Registry host not allowed: {registry} (not in allowlist; set K7_REGISTRY_ALLOWLIST to extend)"
            )

        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise ValueError(f"Cannot resolve registry host {hostname}: {e}") from e
        if not infos:
            raise ValueError(f"Cannot resolve registry host {hostname}: no addresses")

        for info in infos:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise ValueError(f"Registry host not allowed: {registry} resolves to non-public address {ip_str}")

    async def _assert_registry_host_allowed_async(self, registry: str) -> None:
        """Async wrapper so DNS resolution does not block the API event loop."""
        await asyncio.to_thread(self._assert_registry_host_allowed, registry)

    async def _get_registry_image_config(self, image: str) -> dict:
        """Fetch the OCI image config from a container registry.

        Supports Docker Hub (with token auth), ghcr.io, quay.io, and any
        OCI-compliant registry that supports anonymous pulls.
        """
        registry, repo, tag = self._parse_image_reference(image)
        await self._assert_registry_host_allowed_async(registry)
        # Always HTTPS — the previous localhost→http downgrade was an SSRF footgun.
        base = f"https://{registry}"

        headers: dict[str, str] = {}

        async with httpx.AsyncClient(follow_redirects=False) as http_client:
            # Token endpoint is a fixed public host; only for the real Docker Hub registry.
            if registry in {"registry-1.docker.io", "docker.io"}:
                try:
                    token_resp = await http_client.get(
                        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull",
                        timeout=10,
                        follow_redirects=False,
                    )
                    token_resp.raise_for_status()
                    token = token_resp.json()["token"]
                    headers["Authorization"] = f"Bearer {token}"
                except Exception:
                    pass

            accept = ", ".join(
                [
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                ]
            )
            headers["Accept"] = accept

            manifest_url = f"{base}/v2/{repo}/manifests/{tag}"
            manifest_resp = await http_client.get(manifest_url, headers=headers, timeout=15, follow_redirects=False)
            manifest_resp.raise_for_status()
            manifest = manifest_resp.json()

            media_type = manifest.get("mediaType", "")
            if "manifest.list" in media_type or "image.index" in media_type:
                for m in manifest.get("manifests", []):
                    platform = m.get("platform", {})
                    if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                        digest = m["digest"]
                        headers["Accept"] = (
                            "application/vnd.oci.image.manifest.v1+json, "
                            "application/vnd.docker.distribution.manifest.v2+json"
                        )
                        inner_resp = await http_client.get(
                            f"{base}/v2/{repo}/manifests/{digest}",
                            headers=headers,
                            timeout=15,
                            follow_redirects=False,
                        )
                        inner_resp.raise_for_status()
                        manifest = inner_resp.json()
                        break
                else:
                    raise ValueError(f"No linux/amd64 manifest found in index for {image}")

            config_digest = manifest.get("config", {}).get("digest")
            if not config_digest:
                raise ValueError(f"No config digest in manifest for {image}")

            config_resp = await http_client.get(
                f"{base}/v2/{repo}/blobs/{config_digest}",
                headers=headers,
                timeout=15,
                follow_redirects=False,
            )
            config_resp.raise_for_status()
            return config_resp.json()

    async def _get_image_entrypoint_cmd(self, image: str) -> tuple[list, list]:
        """Extract Entrypoint and Cmd from an image via OCI registry inspection."""
        if not image:
            return [], []
        try:
            image_config = await self._get_registry_image_config(image)
            container_config = image_config.get("config", {})
            ep = self._normalize_image_argv(container_config.get("Entrypoint"))
            cmd = self._normalize_image_argv(container_config.get("Cmd"))
            return ep, cmd
        except ValueError as e:
            # SSRF / allowlist rejection must fail loud — do not soft-fail to [].
            msg = str(e).lower()
            if "not allowed" in msg or "cannot resolve registry" in msg:
                raise
            return [], []
        except Exception:
            return [], []

    def _compute_image_argv(
        self,
        image_entrypoint: list,
        image_cmd: list,
        override_entrypoint: list | None,
        override_cmd: list | None,
    ) -> list[str]:
        if override_entrypoint is not None:
            ep = list(override_entrypoint)
            cmd = list(override_cmd) if override_cmd is not None else list(image_cmd)
            return ep + cmd
        ep = list(image_entrypoint)
        cmd = list(override_cmd) if override_cmd is not None else list(image_cmd)
        return ep + cmd

    async def _list_backends_per_node(self) -> dict[str, list[str]]:
        """Return {node_name: [supported backends]} from k7 backend labels."""
        v1 = await self._get_core_v1_client()
        nodes = await v1.list_node()
        out: dict[str, list[str]] = {}
        for n in nodes.items:
            labels = n.metadata.labels or {}
            backends = sorted(
                {
                    self._canonicalize_backend(k.removeprefix("k7.katakate.org/backend-")) or ""
                    for k, v in labels.items()
                    if k.startswith("k7.katakate.org/backend-") and v == "true"
                }
                - {""}
            )
            out[n.metadata.name] = backends
        return out

    async def _check_scheduling(
        self,
        sandbox_name: str,
        namespace: str,
        backend: str,
        timeout_seconds: int = 30,
    ) -> OperationResult:
        """Poll the sandbox pod for FailedScheduling events tied to the backend selector.

        Returns success when no scheduling failure is detected within `timeout_seconds`,
        or when the pod is already running. Returns an explanatory error when the
        node selector does not match any node.
        """
        v1 = await self._get_core_v1_client()
        selector_label = f"k7.katakate.org/backend-{backend}"
        start = time.time()
        while time.time() - start < timeout_seconds:
            pods = await v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={sandbox_name}")
            if pods.items:
                pod = pods.items[0]
                phase = (pod.status.phase if pod.status else None) or "Pending"
                if phase != "Pending":
                    return OperationResult(success=True)
                try:
                    events = await v1.list_namespaced_event(
                        namespace=namespace,
                        field_selector=f"involvedObject.name={pod.metadata.name}",
                    )
                except ApiException:
                    events = None
                if events:
                    for ev in events.items:
                        if (
                            ev.reason == "FailedScheduling"
                            and ev.message
                            and ("node affinity/selector" in ev.message or selector_label in ev.message)
                        ):
                            backends_per_node = await self._list_backends_per_node()
                            available: dict[str, list[str]] = {}
                            for node, backends in backends_per_node.items():
                                for b in backends:
                                    available.setdefault(b, []).append(node)
                            summary = (
                                ", ".join(f"{b} (nodes: {', '.join(sorted(nodes))})" for b, nodes in available.items())
                                or "<none>"
                            )
                            return OperationResult(
                                success=False,
                                error=(
                                    f"No node available supporting backend '{backend}'. "
                                    "This can happen if all nodes supporting this backend are cordoned, "
                                    "tainted, or have insufficient resources.\n"
                                    f"Available backends across cluster: {summary}"
                                ),
                            )
            await asyncio.sleep(2)
        return OperationResult(success=True)

    async def _wait_for_pod_container_started(
        self,
        sandbox_name: str,
        namespace: str,
        timeout_seconds: int = 300,
        wait_all_containers: bool = False,
    ) -> str | None:
        v1 = await self._get_core_v1_client()
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            pods = await v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={sandbox_name}")
            if not pods.items:
                await asyncio.sleep(2)
                continue
            pod = pods.items[0]
            if pod.status and pod.status.phase != "Running":
                await asyncio.sleep(2)
                continue
            statuses = pod.status.container_statuses or []
            if wait_all_containers and statuses:
                if all(s.state and s.state.running for s in statuses):
                    return pod.metadata.name
            else:
                for status in statuses:
                    if status.name == "sandbox" and status.state and status.state.running:
                        return pod.metadata.name
            await asyncio.sleep(2)
        return None

    @staticmethod
    def _canonicalize_backend(backend: str | None) -> str | None:
        """Map deprecated backend names to canonical ones; pass through unknown values."""
        if backend is None:
            return None
        legacy = {
            "firecracker-devmapper": "kata-firecracker-devmapper",
            "qemu-longhorn": "kata-qemu-longhorn",
            "fd": "kata-firecracker-devmapper",
            "ql": "kata-qemu-longhorn",
            "kfd": "kata-firecracker-devmapper",
            "kql": "kata-qemu-longhorn",
            "k7": "k7d",
        }
        return legacy.get(backend, backend)

    async def _detect_backend(self, sandbox_name: str | None = None, namespace: str = "default") -> str:
        """Detect backend with priority: deployment annotation > /etc/k7/backend > default."""
        allowed = ("kata-firecracker-devmapper", "kata-qemu-longhorn", "k7d")
        if sandbox_name:
            try:
                apps_v1 = await self._get_apps_v1_client()
                deployment = await apps_v1.read_namespaced_deployment(sandbox_name, namespace)
                backend = self._canonicalize_backend(deployment.metadata.annotations.get("k7.katakate.org/backend"))
                if backend in allowed:
                    return backend
            except Exception:
                pass

        try:
            if os.path.exists("/etc/k7/backend"):
                with open("/etc/k7/backend") as f:
                    backend = self._canonicalize_backend(f.read().strip())
                    if backend in allowed:
                        return backend
        except Exception:
            pass

        return "kata-firecracker-devmapper"

    async def _update_deployment_annotation(self, name: str, namespace: str, key: str, value: str):
        """Update a single deployment annotation."""
        apps_v1 = await self._get_apps_v1_client()
        body = {"metadata": {"annotations": {key: value}}}
        await apps_v1.patch_namespaced_deployment(name=name, namespace=namespace, body=body)

    # -------------------------------------------------------------------
    # k7d backend (spec 9a M11): daemon control-socket plumbing.
    #
    # The k7d daemon owns every `runtimeClassName: k7` VM on the node and
    # exposes VM-level operations (fork / pause / resume / lookup) over a
    # newline-delimited-JSON Unix socket. These helpers run on the node
    # that hosts the sandbox pod — a remote pod is a loud error, never a
    # silent no-op.
    # -------------------------------------------------------------------

    @staticmethod
    def _k7d_socket_path() -> str:
        return os.environ.get("K7D_SOCKET", "/run/k7d/k7d.sock")

    async def _k7d_request(self, payload: dict) -> dict:
        """Send one request to the local k7d daemon and return its response.

        Raises ``RuntimeError`` on transport errors or an ``error`` response —
        callers surface the message verbatim (fail loud, no fallbacks).
        """
        socket_path = self._k7d_socket_path()

        def _call() -> dict:
            import socket as _socket

            if not os.path.exists(socket_path):
                raise RuntimeError(
                    f"k7d control socket {socket_path} not found — is the k7d daemon "
                    "running on this node? (k7 install --backend k7d sets it up)"
                )
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
                sock.settimeout(120)
                sock.connect(socket_path)
                sock.sendall((json.dumps(payload) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            if not buf:
                raise RuntimeError("k7d daemon closed the control connection without a response")
            return json.loads(buf.decode())

        resp = await asyncio.to_thread(_call)
        if resp.get("status") == "error":
            raise RuntimeError(f"k7d daemon error for op={payload.get('op')}: {resp.get('message')}")
        return resp

    async def _k7d_running_pod(self, sandbox_name: str, namespace: str):
        """Return the Running pod of a k7d sandbox, failing loud when the pod
        is missing or scheduled on a different node than this process."""
        v1 = await self._get_core_v1_client()
        pods = await v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={sandbox_name}")
        running = [
            p for p in pods.items if p.status and p.status.phase == "Running" and not p.metadata.deletion_timestamp
        ]
        if not running:
            raise RuntimeError(f"no Running pod found for sandbox {sandbox_name} in {namespace}")
        pod = running[0]
        # Inside the k7-api pod, os.uname().nodename is the POD name, not the
        # Kubernetes node — the deployment injects K7_NODE_NAME via the
        # downward API so co-located k7d VM ops still work through the API.
        local_hostname = os.environ.get("K7_NODE_NAME") or os.uname().nodename
        if pod.spec.node_name and pod.spec.node_name != local_hostname:
            raise RuntimeError(
                f"sandbox {sandbox_name} runs on node {pod.spec.node_name}, but this k7 process "
                f"runs on {local_hostname}; k7d VM operations must run on the pod's node"
            )
        return pod

    async def _k7d_cri_sandbox_id(self, pod_name: str, namespace: str) -> str:
        """Resolve a pod to its CRI (containerd) sandbox id via crictl."""
        crictl = shutil.which("crictl") or "/usr/local/bin/crictl"
        cmd = [
            crictl,
            "--runtime-endpoint",
            "unix:///run/k3s/containerd/containerd.sock",
            "pods",
            "--name",
            pod_name,
            "--namespace",
            namespace,
            "--state",
            "ready",
            "-q",
        ]

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)

        result = await asyncio.to_thread(_run)
        if result.returncode != 0:
            raise RuntimeError(f"crictl pods failed for {pod_name}: {result.stderr.strip()}")
        sandbox_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not sandbox_ids:
            raise RuntimeError(f"no ready CRI sandbox found for pod {pod_name} in {namespace}")
        return sandbox_ids[0]

    async def _k7d_vm_for_sandbox(self, sandbox_name: str, namespace: str) -> dict:
        """Resolve a k7d sandbox to its daemon VM: returns the daemon's
        ``sandbox_found`` response plus the CRI ``sandbox_id`` used."""
        pod = await self._k7d_running_pod(sandbox_name, namespace)
        cri_sandbox_id = await self._k7d_cri_sandbox_id(pod.metadata.name, namespace)
        resp = await self._k7d_request({"op": "lookup_sandbox", "sandbox_id": cri_sandbox_id})
        if resp.get("status") != "sandbox_found":
            raise RuntimeError(f"unexpected k7d lookup_sandbox response: {resp}")
        resp["sandbox_id"] = cri_sandbox_id
        return resp

    # -------------------------------------------------------------------
    # Spec 18g: per-node k7 agent. When a k7d sandbox lives on a different
    # node than this process, VM ops are forwarded over HTTP to the
    # k7-agent DaemonSet pod on that node (which runs this same code with
    # the node-local sockets). No agent there / no token → loud error.
    # -------------------------------------------------------------------

    K7_AGENT_PORT = 8000

    @staticmethod
    def _k7d_local_node() -> str:
        # Inside a pod, os.uname().nodename is the POD name — deployments
        # inject the real node via the downward API (K7_NODE_NAME).
        return os.environ.get("K7_NODE_NAME") or os.uname().nodename

    @staticmethod
    def _k7d_agent_token() -> str:
        path = os.environ.get("K7_AGENT_TOKEN_FILE", "/etc/k7/agent_token")
        try:
            with open(path) as f:
                token = f.read().strip()
        except OSError as e:
            raise RuntimeError(
                f"k7 agent token {path} is unreadable ({e}) — cannot talk to the per-node "
                "k7-agent. The install playbook provisions it on every node; re-run `k7 install`."
            )
        if not token:
            raise RuntimeError(f"k7 agent token {path} is empty — re-run `k7 install`")
        return token

    async def _k7d_sandbox_node(self, sandbox_name: str, namespace: str) -> str:
        """Node hosting the sandbox's Running pod (loud when there is none)."""
        v1 = await self._get_core_v1_client()
        pods = await v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={sandbox_name}")
        running = [
            p for p in pods.items if p.status and p.status.phase == "Running" and not p.metadata.deletion_timestamp
        ]
        if not running:
            raise RuntimeError(f"no Running pod found for sandbox {sandbox_name} in {namespace}")
        return running[0].spec.node_name

    async def _k7d_agent_base_url(self, node_name: str) -> str:
        """Resolve the Ready k7-agent pod on ``node_name`` to its pod IP."""
        v1 = await self._get_core_v1_client()
        pods = await v1.list_namespaced_pod(
            namespace="kube-system",
            label_selector="app=k7-agent",
            field_selector=f"spec.nodeName={node_name}",
        )
        for p in pods.items:
            ready = any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or []))
            if p.status.phase == "Running" and ready and p.status.pod_ip:
                return f"http://{p.status.pod_ip}:{self.K7_AGENT_PORT}"
        raise RuntimeError(
            f"no Ready k7-agent pod on node {node_name} — k7d VM operations for sandboxes on "
            "another node need the k7-agent DaemonSet (deployed by `k7 install`); check "
            "`kubectl -n kube-system get pods -l app=k7-agent -o wide`"
        )

    async def _k7d_forward_vm_op(self, node_name: str, op: str, body: dict, timeout: float) -> OperationResult:
        if os.environ.get("K7_AGENT") == "1":
            raise RuntimeError(
                f"k7-agent on {self._k7d_local_node()} received op={op} for a sandbox on node "
                f"{node_name} — refusing to re-forward (the caller resolved the wrong agent)"
            )
        base = await self._k7d_agent_base_url(node_name)
        token = self._k7d_agent_token()
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(f"{base}/agent/v1/vm/{op}", json=body, headers={"X-K7-Agent-Token": token})
        if resp.status_code != 200:
            raise RuntimeError(f"k7-agent on {node_name} failed op={op}: HTTP {resp.status_code}: {resp.text}")
        data = resp.json()
        return OperationResult(
            success=bool(data.get("success")),
            message=data.get("message") or "",
            error=data.get("error") or "",
            data=data.get("data"),
        )

    async def lookup_k7d_vm(self, sandbox_name: str, namespace: str = "default") -> dict:
        """Resolve a k7d sandbox to its daemon VM record, forwarding to the
        per-node agent when the sandbox lives on another node."""
        node = await self._k7d_sandbox_node(sandbox_name, namespace)
        if node and node != self._k7d_local_node():
            base = await self._k7d_agent_base_url(node)
            token = self._k7d_agent_token()
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.post(
                    f"{base}/agent/v1/vm/lookup",
                    json={"name": sandbox_name, "namespace": namespace},
                    headers={"X-K7-Agent-Token": token},
                )
            if resp.status_code != 200:
                raise RuntimeError(f"k7-agent on {node} failed op=lookup: HTTP {resp.status_code}: {resp.text}")
            return resp.json()
        return await self._k7d_vm_for_sandbox(sandbox_name, namespace)

    async def nodes_storage(self) -> dict:
        """Per-node storage-pool utilization, aggregated from every node's
        k7-agent (spec 18g part 2). Nodes whose agent is unreachable get a
        loud ``{"error": ...}`` entry — never silently omitted."""
        v1 = await self._get_core_v1_client()
        nodes = await v1.list_node()
        token = self._k7d_agent_token()
        out: dict[str, dict] = {}
        for node in nodes.items:
            name = node.metadata.name
            try:
                base = await self._k7d_agent_base_url(name)
                async with httpx.AsyncClient(timeout=30) as http:
                    resp = await http.get(f"{base}/agent/v1/storage", headers={"X-K7-Agent-Token": token})
                if resp.status_code != 200:
                    raise RuntimeError(f"k7-agent storage query failed: HTTP {resp.status_code}: {resp.text}")
                out[name] = resp.json()
            except Exception as e:
                out[name] = {"error": str(e)}
        return out

    def _get_embedded_playbook(self) -> str:
        """Get embedded Ansible playbook content."""
        try:
            return resources.files("k7.deploy").joinpath("k7-install-node.yaml").read_text()
        except Exception:
            return ""

    def _get_embedded_inventory(self, hosts: list[str]) -> str:
        """Generate Ansible inventory from host list (treats every host as a server)."""
        server_lines = [f"{host} ansible_user=root ansible_ssh_private_key_file=~/.ssh/id_rsa" for host in hosts]
        return "\n".join(
            [
                "[k7_servers]",
                *server_lines,
                "",
                "[k7_agents]",
                "",
                "[k7_cluster:children]",
                "k7_servers",
                "k7_agents",
            ]
        )

    def _parse_resource_value(self, value: str) -> int:
        """Parse Kubernetes resource value to numeric form."""
        if not value:
            return 0

        value = value.strip().lower()
        if value.endswith("m"):
            return int(value[:-1])
        elif value.endswith("gi"):
            return int(value[:-2]) * 1024
        elif value.endswith("mi"):
            return int(value[:-2])
        elif value.endswith("ki"):
            return int(value[:-2]) // 1024
        else:
            try:
                return int(value)
            except ValueError:
                return 0

    def _validate_limits(self, limits: dict[str, str]) -> bool:
        """Validate resource limits."""
        if not limits:
            return True

        for key, value in limits.items():
            if key in ["cpu", "memory", "ephemeral-storage"] and self._parse_resource_value(value) <= 0:
                return False
        return True

    def _memory_limit_to_mib(self, value: str) -> int:
        """Convert a Kubernetes-style memory string to MiB (Ki/Mi/Gi/Ti only)."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Memory limit must be a non-empty string")

        raw = value.strip()
        match = re.match(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti)$", raw)
        if not match:
            raise ValueError(f"Unsupported memory format '{value}'. Use Ki, Mi, Gi, or Ti.")

        amount = float(match.group(1))
        unit = match.group(2)
        if unit == "Ki":
            mib = amount / 1024.0
        elif unit == "Mi":
            mib = amount
        elif unit == "Gi":
            mib = amount * 1024.0
        else:  # Ti
            mib = amount * 1024.0 * 1024.0

        return int(math.ceil(mib))

    def _count_playbook_tasks(self, playbook_content: str) -> int:
        """Count tasks in Ansible playbook."""
        try:
            playbook = yaml.safe_load(playbook_content)
            if isinstance(playbook, list) and len(playbook) > 0:
                return len(playbook[0].get("tasks", []))
        except Exception:
            pass
        return 0

    async def _collect_source_annotations(
        self,
        source_sandbox: str,
        namespace: str,
    ) -> dict[str, str]:
        """Read a source sandbox's Deployment + container spec into ``k7.io/source-*``
        annotations that ``restore_sandbox`` can later use to rehydrate a
        :class:`SandboxConfig` from a standalone snapshot (Spec 10f).

        Best-effort: any lookup failure returns an empty dict (the snapshot
        still gets ``k7.io/kind`` and ``k7.io/source-sandbox`` — restore will
        require the user to pass the missing fields explicitly).
        """
        out: dict[str, str] = {}
        try:
            apps_v1 = await self._get_apps_v1_client()
            deployment = await apps_v1.read_namespaced_deployment(name=source_sandbox, namespace=namespace)
        except Exception:
            return out
        dep_annotations = (deployment.metadata.annotations or {}) if deployment.metadata else {}
        backend = dep_annotations.get("k7.katakate.org/backend")
        if backend:
            out["k7.io/source-backend"] = backend
        sidecar = dep_annotations.get("k7.katakate.org/sidecar")
        if sidecar:
            out["k7.io/source-sidecar"] = sidecar
        containers = []
        try:
            containers = deployment.spec.template.spec.containers or []  # type: ignore[union-attr]
        except Exception:
            containers = []
        # First non-sidecar container is the sandbox; sidecars are named
        # ``docker-sidecar`` etc. via the SIDECAR_REGISTRY.
        sandbox_container = next((c for c in containers if c.name == "sandbox"), containers[0] if containers else None)
        if sandbox_container is not None:
            if sandbox_container.image:
                out["k7.io/source-image"] = sandbox_container.image
            limits: dict[str, str] = {}
            try:
                raw_limits = (sandbox_container.resources.limits or {}) if sandbox_container.resources else {}
                for k, v in raw_limits.items():
                    if k in ("cpu", "memory", "ephemeral-storage"):
                        limits[k] = str(v)
            except Exception:
                pass
            if limits:
                out["k7.io/source-limits"] = json.dumps(limits, sort_keys=True)
        # Root PVC size: read from the PVC the sandbox is bound to. Falls back
        # to the snapshot's restoreSize at restore time when unset.
        pvc_name = dep_annotations.get("k7.katakate.org/root-pvc-name") or self._root_pvc_name(source_sandbox)
        try:
            v1 = await self._get_core_v1_client()
            pvc = await v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
            size = None
            if pvc.spec and pvc.spec.resources and pvc.spec.resources.requests:
                size = pvc.spec.resources.requests.get("storage")
            if size:
                out["k7.io/source-root-disk-size"] = str(size)
        except Exception:
            pass
        return out

    async def _create_volume_snapshot(
        self,
        pvc_name: str,
        snapshot_name: str,
        snapshot_class: str = "longhorn",
        namespace: str = "default",
        kind: str = SNAPSHOT_KIND_NAMED,
        source_sandbox: str | None = None,
    ) -> OperationResult:
        """Create a crash-consistent VolumeSnapshot for a PVC via the K8s API.

        ``kind`` and ``source_sandbox`` are stamped onto the resulting
        ``VolumeSnapshot`` via ``k7.io/`` annotations so ``list_snapshots`` /
        ``gc_snapshots`` can classify it reliably (no name-pattern guessing).
        When ``source_sandbox`` is set we also stamp ``k7.io/source-*``
        annotations describing the source Deployment's image / backend /
        sidecar / limits / root-disk-size (Spec 10f), so a future
        :meth:`restore_sandbox` can rehydrate a config without those
        being passed on the command line.
        """
        custom = await self._get_custom_objects_client()
        annotations: dict[str, str] = {
            "k7.io/kind": kind,
            "k7.io/created-by": "k7-core",
        }
        if source_sandbox:
            annotations["k7.io/source-sandbox"] = source_sandbox
            # Only worth the lookup for snapshots a user might later restore.
            # ``fork`` auto-snapshots get cleaned up immediately, so we skip
            # the extra API calls for them.
            if kind != SNAPSHOT_KIND_FORK:
                annotations.update(await self._collect_source_annotations(source_sandbox, namespace))
        snapshot_body = {
            "apiVersion": "snapshot.storage.k8s.io/v1",
            "kind": "VolumeSnapshot",
            "metadata": {
                "name": snapshot_name,
                "namespace": namespace,
                "annotations": annotations,
            },
            "spec": {
                "volumeSnapshotClassName": snapshot_class,
                "source": {
                    "persistentVolumeClaimName": pvc_name,
                },
            },
        }
        try:
            await custom.create_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                body=snapshot_body,
            )
        except ApiException as e:
            return OperationResult(
                success=False,
                error=f"Snapshot creation failed: {e.reason or e.body}",
            )
        return OperationResult(success=True, message=f"Snapshot {snapshot_name} created for PVC {pvc_name}")

    async def _wait_for_snapshot_ready(
        self, snapshot_name: str, namespace: str = "default", timeout: int = 120
    ) -> OperationResult:
        """Wait for a VolumeSnapshot to become readyToUse."""
        custom = await self._get_custom_objects_client()
        start = time.time()
        poll_interval = 0.5
        while time.time() - start < timeout:
            try:
                snap = await custom.get_namespaced_custom_object(
                    group="snapshot.storage.k8s.io",
                    version="v1",
                    namespace=namespace,
                    plural="volumesnapshots",
                    name=snapshot_name,
                )
                ready = (snap.get("status") or {}).get("readyToUse")
                if ready is True:
                    elapsed = time.time() - start
                    print(f"⏱️  Snapshot {snapshot_name} ready in {elapsed:.2f}s", file=sys.stderr)
                    return OperationResult(success=True)
            except ApiException:
                pass
            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.2, 2.0)
        return OperationResult(success=False, error=f"VolumeSnapshot {snapshot_name} not ready within {timeout}s")

    def _infer_snapshot_kind(self, name: str, annotations: dict | None) -> str:
        """Classify a VolumeSnapshot for ``k7 snapshot list`` / ``gc``.

        Prefer the ``k7.io/kind`` annotation we stamp at creation time. Fall back
        to a name-pattern heuristic for snapshots created before Spec 10e or by
        external tools — pause snapshots end in ``-paused-<digits>``, fork
        snapshots end in ``-fork-<digits>``; anything else is "named".
        """
        if annotations and annotations.get("k7.io/kind") in (
            SNAPSHOT_KIND_PAUSE,
            SNAPSHOT_KIND_FORK,
            SNAPSHOT_KIND_NAMED,
        ):
            return annotations["k7.io/kind"]
        if re.search(r"-paused-\d+$", name):
            return SNAPSHOT_KIND_PAUSE
        if re.search(r"-fork-\d+$", name):
            return SNAPSHOT_KIND_FORK
        return SNAPSHOT_KIND_NAMED

    def _snapshot_info_from_object(self, snap: dict) -> SnapshotInfo:
        """Convert a raw ``VolumeSnapshot`` dict to a typed :class:`SnapshotInfo`."""
        metadata = snap.get("metadata") or {}
        spec = snap.get("spec") or {}
        status = snap.get("status") or {}
        annotations = metadata.get("annotations") or {}
        source_pvc = ((spec.get("source") or {}).get("persistentVolumeClaimName")) or ""
        # ``status.restoreSize`` is "Xi" style (e.g. "10Gi") once readyToUse is True.
        size_bytes = self._parse_resource_value(str(status.get("restoreSize") or "0"))
        # Convert "10Gi" → 10*1024*1024*1024 if _parse_resource_value returned Mi units;
        # however our existing helper already returns Mi for "Gi"-suffixed inputs. For
        # display we just keep the raw byte estimate; consumers care about presence.
        ready = bool(status.get("readyToUse"))
        ctime = metadata.get("creationTimestamp") or ""
        age = "Unknown"
        try:
            if ctime:
                created = datetime.fromisoformat(ctime.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age = str(datetime.now(timezone.utc) - created)
        except Exception:
            age = "Unknown"
        name = metadata.get("name") or ""
        return SnapshotInfo(
            name=name,
            namespace=metadata.get("namespace") or "default",
            source_pvc=source_pvc,
            source_sandbox=annotations.get("k7.io/source-sandbox", ""),
            kind=self._infer_snapshot_kind(name, annotations),
            ready_to_use=ready,
            creation_timestamp=ctime,
            age=age,
            size_bytes=size_bytes,
            snapshot_class=spec.get("volumeSnapshotClassName") or "longhorn",
        )

    async def _create_pvc_from_snapshot(
        self,
        target_pvc_name: str,
        namespace: str,
        snapshot_name: str,
        storage_size: str,
        source_pvc_spec: Any = None,
    ) -> OperationResult:
        """Create a PVC cloned from a VolumeSnapshot's data (Spec 10e/10f shared helper).

        ``source_pvc_spec`` is the ``spec`` of an existing PVC to copy
        ``access_modes`` / ``volume_mode`` / ``storage_class_name`` from
        (fork's case, where the source sandbox still exists). When ``None``
        (restore's case), defaults to ``ReadWriteOnce`` / ``Filesystem`` /
        ``longhorn`` — the only combination k7's kata-qemu-longhorn backend uses
        today.
        """
        v1 = await self._get_core_v1_client()
        if source_pvc_spec is not None:
            access_modes = source_pvc_spec.access_modes or ["ReadWriteOnce"]
            volume_mode = source_pvc_spec.volume_mode or "Filesystem"
            storage_class = source_pvc_spec.storage_class_name or "longhorn"
        else:
            access_modes = ["ReadWriteOnce"]
            volume_mode = "Filesystem"
            storage_class = "longhorn"
        pvc_body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=target_pvc_name, namespace=namespace),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=access_modes,
                volume_mode=volume_mode,
                storage_class_name=storage_class,
                resources=client.V1VolumeResourceRequirements(requests={"storage": storage_size}),
                data_source=client.V1TypedLocalObjectReference(
                    api_group="snapshot.storage.k8s.io",
                    kind="VolumeSnapshot",
                    name=snapshot_name,
                ),
            ),
        )
        try:
            await v1.create_namespaced_persistent_volume_claim(namespace=namespace, body=pvc_body)
        except ApiException as e:
            if e.status == 409:
                return OperationResult(
                    success=False,
                    error=f"PVC {target_pvc_name} already exists in namespace {namespace}",
                )
            return OperationResult(
                success=False,
                error=f"Failed to create cloned PVC {target_pvc_name}: {e}",
            )
        return OperationResult(success=True, message=f"PVC {target_pvc_name} created from snapshot {snapshot_name}")

    async def _wait_for_pvc_bound(
        self, pvc_name: str, namespace: str = "default", timeout: int = 180
    ) -> OperationResult:
        """Wait for PVC to reach Bound."""
        v1 = await self._get_core_v1_client()
        start = time.time()
        poll_interval = 0.5
        while time.time() - start < timeout:
            try:
                pvc = await v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
                phase = getattr(pvc.status, "phase", None)
                if phase == "Bound":
                    elapsed = time.time() - start
                    print(f"⏱️  PVC {pvc_name} bound in {elapsed:.2f}s", file=sys.stderr)
                    return OperationResult(success=True)
            except ApiException as e:
                if e.status == 404:
                    pass
                else:
                    return OperationResult(success=False, error=f"PVC check error: {e}")
            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.2, 2.0)
        return OperationResult(success=False, error=f"PVC {pvc_name} not Bound within {timeout}s")

    def _root_pvc_name(self, sandbox_name: str) -> str:
        """Derive the canonical root PVC name for a sandbox."""
        return f"{sandbox_name}-root-lh"

    async def _wait_for_job(self, job_name: str, namespace: str = "default", timeout: int = 600) -> OperationResult:
        """Wait for a Kubernetes Job to complete."""
        await self._load_k3s_config()
        start = time.time()
        while time.time() - start < timeout:
            try:
                batch = client.BatchV1Api()
                job = await batch.read_namespaced_job(name=job_name, namespace=namespace)
                status = job.status
                if status.succeeded and status.succeeded >= 1:
                    return OperationResult(success=True, message=f"Job {job_name} completed")
                if status.failed and status.failed > 0:
                    return OperationResult(success=False, error=f"Job {job_name} failed")
            except ApiException as e:
                if e.status == 404:
                    pass
                else:
                    return OperationResult(success=False, error=f"Job wait error: {e}")
            await asyncio.sleep(3)
        return OperationResult(success=False, error=f"Job {job_name} did not complete within {timeout}s")

    async def _ensure_root_pvc(
        self,
        sandbox_name: str,
        namespace: str,
        root_disk_size: str,
    ) -> OperationResult:
        """Ensure the Longhorn filesystem PVC exists for the sandbox."""
        v1 = await self._get_core_v1_client()
        pvc_name = self._root_pvc_name(sandbox_name)
        try:
            existing_pvc = await v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
            existing_mode = (existing_pvc.spec.volume_mode or "").lower() if existing_pvc and existing_pvc.spec else ""
            if existing_mode != "filesystem":
                return OperationResult(
                    success=False,
                    error=(
                        f"PVC {pvc_name} has volumeMode '{existing_pvc.spec.volume_mode}', "
                        "but kata-qemu-longhorn requires Filesystem. Delete/recreate the PVC."
                    ),
                )
            return OperationResult(
                success=True,
                message=f"PVC {pvc_name} already exists",
                data={"created": False, "pvc_name": pvc_name},
            )
        except ApiException as e:
            if e.status != 404:
                return OperationResult(success=False, error=f"PVC lookup error: {e}")
        pvc_body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=pvc_name, namespace=namespace),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                volume_mode="Filesystem",
                storage_class_name="longhorn",
                resources=client.V1VolumeResourceRequirements(requests={"storage": root_disk_size}),
            ),
        )
        try:
            await v1.create_namespaced_persistent_volume_claim(namespace=namespace, body=pvc_body)
        except ApiException as ce:
            return OperationResult(success=False, error=f"Failed to create PVC {pvc_name}: {ce}")
        return OperationResult(
            success=True,
            message=f"Created PVC {pvc_name}",
            data={"created": True, "pvc_name": pvc_name},
        )

    async def _ensure_persist_wrapper_configmap(
        self,
        namespace: str,
        name: str,
        script_text: str,
    ) -> OperationResult:
        """Ensure the persistence wrapper ConfigMap exists."""
        v1 = await self._get_core_v1_client()
        cm_name = f"{name}-persist-wrapper"
        cm_body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=cm_name, namespace=namespace),
            data={"k7-persist-bind.sh": script_text},
        )
        try:
            await v1.read_namespaced_config_map(name=cm_name, namespace=namespace)
            return OperationResult(
                success=True,
                message=f"ConfigMap {cm_name} already exists",
                data={"created": False, "cm_name": cm_name},
            )
        except ApiException as e:
            if e.status != 404:
                return OperationResult(success=False, error=f"ConfigMap lookup error: {e}")
        try:
            await v1.create_namespaced_config_map(namespace=namespace, body=cm_body)
        except ApiException as ce:
            return OperationResult(success=False, error=f"Failed to create ConfigMap {cm_name}: {ce}")
        return OperationResult(
            success=True,
            message=f"Created ConfigMap {cm_name}",
            data={"created": True, "cm_name": cm_name},
        )

    async def _cilium_available(self) -> bool:
        """Return True when the CiliumNetworkPolicy CRD is registered on the cluster."""
        await self._load_k3s_config()
        api_ext = client.ApiextensionsV1Api()
        try:
            await api_ext.read_custom_resource_definition(name="ciliumnetworkpolicies.cilium.io")
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    async def _apply_cilium_egress_policy(
        self,
        sandbox_name: str,
        namespace: str,
        cidrs: list[str],
        fqdns: list[str],
    ) -> OperationResult:
        """Create (or replace) a CiliumNetworkPolicy that allows FQDN + optional CIDR egress.

        Cilium's FQDN matching requires DNS lookups to pass through Cilium's DNS
        proxy, so we always whitelist cluster DNS (kube-dns) on UDP/TCP 53 when
        FQDNs are present.
        """
        custom = await self._get_custom_objects_client()

        egress_rules: list[dict] = []

        # DNS egress + FQDN allowlist (paired so the FQDN proxy can observe lookups).
        dns_rule: dict = {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "kube-system",
                        "k8s:k8s-app": "kube-dns",
                    }
                }
            ],
            "toPorts": [
                {
                    "ports": [
                        {"port": "53", "protocol": "UDP"},
                        {"port": "53", "protocol": "TCP"},
                    ],
                    "rules": {"dns": [{"matchPattern": "*"}]},
                }
            ],
        }
        egress_rules.append(dns_rule)

        fqdn_matchers: list[dict] = []
        for fqdn in fqdns:
            if "*" in fqdn:
                # Cilium matchPattern semantics trap (spec 18f issue 3): `*`
                # matches DNS characters within a SINGLE label — it never
                # crosses dots. `*.docker.com` therefore does NOT match
                # `production.cloudfront.docker.com`, which silently breaks
                # CDN-backed registries (Docker Hub blobs). k7 promises
                # "any subdomain", so translate a leading `*.` into Cilium's
                # multi-label subdomain wildcard `**.` (one or more labels).
                if fqdn.startswith("*.") and not fqdn.startswith("**"):
                    fqdn = "*" + fqdn
                fqdn_matchers.append({"matchPattern": fqdn})
            else:
                fqdn_matchers.append({"matchName": fqdn})
        if fqdn_matchers:
            egress_rules.append({"toFQDNs": fqdn_matchers})

        if cidrs:
            egress_rules.append({"toCIDR": cidrs})

        body = {
            "apiVersion": "cilium.io/v2",
            "kind": "CiliumNetworkPolicy",
            "metadata": {
                "name": f"{sandbox_name}-egress",
                "namespace": namespace,
            },
            "spec": {
                "endpointSelector": {"matchLabels": {"katakate.org/sandbox": sandbox_name}},
                "egress": egress_rules,
            },
        }

        try:
            await custom.create_namespaced_custom_object(
                group="cilium.io",
                version="v2",
                namespace=namespace,
                plural="ciliumnetworkpolicies",
                body=body,
            )
        except ApiException as e:
            if e.status == 409:
                try:
                    await custom.replace_namespaced_custom_object(
                        group="cilium.io",
                        version="v2",
                        namespace=namespace,
                        plural="ciliumnetworkpolicies",
                        name=f"{sandbox_name}-egress",
                        body=body,
                    )
                except ApiException as replace_err:
                    return OperationResult(
                        success=False,
                        error=f"Failed to update CiliumNetworkPolicy: {replace_err}",
                    )
            else:
                return OperationResult(
                    success=False,
                    error=f"Failed to create CiliumNetworkPolicy: {e}",
                )
        return OperationResult(success=True)

    async def _get_kata_sandboxes(self, namespace: str | None = None) -> list:
        """Get all Kata sandboxes (deployments with kata runtime)."""
        apps_v1 = await self._get_apps_v1_client()

        if namespace:
            deployments = await apps_v1.list_namespaced_deployment(namespace=namespace)
        else:
            deployments = await apps_v1.list_deployment_for_all_namespaces()

        kata_deployments = []
        for deployment in deployments.items:
            if deployment.spec.template.spec.runtime_class_name == "kata" or (
                deployment.metadata.labels and deployment.metadata.labels.get("runtime") == "kata"
            ):
                kata_deployments.append(deployment)

        return kata_deployments

    async def _delete_sandbox_resources(self, name: str, namespace: str) -> OperationResult:
        """Delete all resources associated with a sandbox."""
        apps_v1 = await self._get_apps_v1_client()
        v1 = await self._get_core_v1_client()
        networking_v1 = await self._get_networking_v1_client()

        errors = []

        try:
            await apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                errors.append(f"deployment: {e}")

        try:
            await v1.delete_namespaced_secret(name=f"{name}-env", namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                errors.append(f"secret: {e}")

        try:
            await v1.delete_namespaced_config_map(name=f"{name}-persist-wrapper", namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                errors.append(f"configmap: {e}")

        # Spec 10e/10f contract: pause and named snapshots persist until the
        # user explicitly deletes them — that's what makes ``k7 restore`` work
        # after the source sandbox is gone. Only sweep ``kind=fork`` stragglers
        # here (the inline-delete in ``fork_sandbox`` already cleans the happy
        # path; this is a defence-in-depth safety net alongside the
        # snapshot-gc CronJob).
        #
        # Also count surviving pause/named snapshots that target this sandbox's
        # root PVC: if any exist, we must KEEP the PVC. Longhorn snapshots
        # reference their source volume's data, so deleting the PVC (and the
        # underlying Longhorn volume via the StorageClass's Delete reclaim
        # policy) makes the snapshot un-restorable later. The PVC is annotated
        # ``k7.io/orphaned-by`` so users can find and clean up by hand when
        # they delete the last referencing snapshot.
        pvc_name = f"{name}-root-lh"
        persistent_snapshots_present = False
        try:
            custom = await self._get_custom_objects_client()
            snap_list = await custom.list_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
            )
            for snap in snap_list.get("items", []):
                metadata = snap.get("metadata") or {}
                snap_name = metadata.get("name", "")
                annotations = metadata.get("annotations") or {}
                source = annotations.get("k7.io/source-sandbox")
                snap_source_pvc = ((snap.get("spec") or {}).get("source") or {}).get("persistentVolumeClaimName")
                kind = self._infer_snapshot_kind(snap_name, annotations)
                if kind == SNAPSHOT_KIND_FORK:
                    if source and source != name:
                        continue
                    if not source and not snap_name.startswith(f"{name}-fork-"):
                        continue
                    try:
                        await custom.delete_namespaced_custom_object(
                            group="snapshot.storage.k8s.io",
                            version="v1",
                            namespace=namespace,
                            plural="volumesnapshots",
                            name=snap_name,
                        )
                    except Exception:
                        pass
                else:
                    # pause/named: track whether any reference our PVC.
                    if snap_source_pvc == pvc_name or source == name:
                        persistent_snapshots_present = True
        except Exception:
            pass

        if persistent_snapshots_present:
            # Mark the PVC orphaned so the user can find it; do NOT delete it
            # — Longhorn would tear down the underlying volume and break
            # ``k7 restore`` from any pause/named snapshot that points here.
            try:
                await v1.patch_namespaced_persistent_volume_claim(
                    name=pvc_name,
                    namespace=namespace,
                    body={
                        "metadata": {
                            "annotations": {
                                "k7.io/orphaned-by": f"sandbox-deleted:{name}",
                            }
                        }
                    },
                )
            except ApiException:
                pass
        else:
            try:
                await v1.delete_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
            except ApiException as e:
                if e.status != 404:
                    errors.append(f"pvc: {e}")
            try:
                await v1.patch_namespaced_persistent_volume_claim(
                    name=pvc_name,
                    namespace=namespace,
                    body={"metadata": {"finalizers": []}},
                )
            except ApiException:
                pass

        try:
            await networking_v1.delete_namespaced_network_policy(name=f"{name}-netpol", namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                errors.append(f"network policy: {e}")

        try:
            await networking_v1.delete_namespaced_network_policy(name=f"{name}-deny-ingress", namespace=namespace)
        except ApiException as e:
            if e.status != 404:
                errors.append(f"network policy deny-ingress: {e}")

        # Delete CiliumNetworkPolicy (FQDN egress) if present; ignore if CRD absent.
        try:
            custom = await self._get_custom_objects_client()
            await custom.delete_namespaced_custom_object(
                group="cilium.io",
                version="v2",
                namespace=namespace,
                plural="ciliumnetworkpolicies",
                name=f"{name}-egress",
            )
        except ApiException as e:
            if e.status not in (404, 405):
                errors.append(f"cilium network policy: {e}")
        except Exception:
            pass

        if errors:
            return OperationResult(success=False, error="; ".join(errors))

        return OperationResult(success=True, message=f"Sandbox {name} deleted successfully")

    def install_node(
        self,
        playbook_content: str | None = None,
        inventory_content: str | None = None,
        verbose: bool = False,
        progress_callback: Callable[[dict], None] | None = None,
        stream_output: bool = False,
        extra_vars: dict | None = None,
    ) -> OperationResult:
        """Install K7 on target nodes using Ansible. Stays sync (interactive/CLI-only)."""
        try:
            if not playbook_content:
                playbook_content = self._get_embedded_playbook()

            if not playbook_content:
                return OperationResult(success=False, error="No playbook content available")

            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as playbook_file:
                playbook_file.write(playbook_content)
                playbook_path = playbook_file.name

            with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as inventory_file:
                inventory_file.write(
                    inventory_content
                    or (
                        "[k7_servers]\n"
                        "localhost ansible_connection=local ansible_user=root\n"
                        "\n[k7_agents]\n"
                        "\n[k7_cluster:children]\nk7_servers\nk7_agents\n"
                    )
                )
                inventory_path = inventory_file.name

            extra_vars_path: str | None = None
            if extra_vars:
                try:
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as evf:
                        yaml.safe_dump(extra_vars, evf, default_flow_style=False)
                        extra_vars_path = evf.name
                except Exception:
                    extra_vars_path = None

            total_tasks = self._count_playbook_tasks(playbook_content)
            if progress_callback:
                try:
                    progress_callback({"type": "total", "total_tasks": total_tasks})
                except Exception:
                    pass

            cmd = ["ansible-playbook", "-i", inventory_path, playbook_path]
            if extra_vars_path:
                cmd += ["-e", f"@{extra_vars_path}"]
            if verbose:
                cmd.append("-v")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            current_index = 0
            task_pattern = re.compile(r"^TASK \[(.*?)\]")
            ansi_escape = re.compile(r"\x1b\[[0-9;]*[mK]")
            combined_output = []
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    combined_output.append(line)
                    if stream_output:
                        try:
                            sys.stdout.write(line)
                            sys.stdout.flush()
                        except Exception:
                            pass
                    clean_line = ansi_escape.sub("", line)
                    match = task_pattern.search(clean_line)
                    if match:
                        current_index += 1
                        if progress_callback:
                            try:
                                progress_callback(
                                    {
                                        "type": "task_start",
                                        "name": match.group(1),
                                        "index": current_index,
                                        "total": total_tasks,
                                    }
                                )
                            except Exception:
                                pass
                process.wait()
            finally:
                try:
                    os.unlink(playbook_path)
                except Exception:
                    pass
                try:
                    os.unlink(inventory_path)
                except Exception:
                    pass
                try:
                    if extra_vars_path:
                        os.unlink(extra_vars_path)
                except Exception:
                    pass

            if process.returncode == 0:
                return OperationResult(success=True, message="Installation completed successfully")
            else:
                tail = "".join(combined_output[-50:]) if combined_output else ""
                return OperationResult(
                    success=False,
                    error=f"Installation failed (code {process.returncode}). Output tail:\n{tail}",
                )

        except Exception as e:
            return OperationResult(success=False, error=str(e))

    async def create_sandbox(
        self,
        config: SandboxConfig,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> OperationResult:
        """Create a new sandbox with the given configuration."""
        try:

            def _emit(event: dict):
                if progress_callback:
                    try:
                        progress_callback(event)
                    except Exception:
                        pass

            if config.limits and not self._validate_limits(config.limits):
                return OperationResult(success=False, error="Invalid resource limits")

            # SSRF guard: refuse image registries that resolve to non-public
            # addresses before any control-plane HTTP or cluster create work.
            if config.image:
                registry, _, _ = self._parse_image_reference(config.image)
                await self._assert_registry_host_allowed_async(registry)

            apps_v1 = await self._get_apps_v1_client()
            v1 = await self._get_core_v1_client()
            networking_v1 = await self._get_networking_v1_client()

            _emit({"stage": "provisioning", "status": "start"})

            if config.env_file and os.path.exists(config.env_file):
                with open(config.env_file) as f:
                    env_content = f.read()

                env_vars: dict[str, str] = {}
                for line in env_content.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, value = stripped.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key:
                        env_vars[key] = value

                if not env_vars:
                    return OperationResult(
                        success=False,
                        error="env_file is empty or invalid; no variables parsed",
                    )

                secret = client.V1Secret(
                    metadata=client.V1ObjectMeta(name=f"{config.name}-env", namespace=config.namespace),
                    string_data=env_vars,
                )

                try:
                    await v1.create_namespaced_secret(namespace=config.namespace, body=secret)
                except ApiException as e:
                    if e.status != 409:
                        return OperationResult(success=False, error=f"Failed to create secret: {e}")

            # Per-sandbox marker file: /tmp is shared with anything else running
            # in the same container, so a generic name races with a previous
            # `before_script` run (or a parallel one in the same image's
            # container scope).
            before_done_file = f"/tmp/k7_before_done_{config.name}"

            drop_caps: list[str] | None
            add_caps: list[str] | None
            if getattr(config, "cap_drop", None) is None:
                drop_caps = ["ALL"]
            else:
                drop_caps = [c.upper() for c in (config.cap_drop or [])]
            add_caps = [c.upper() for c in (getattr(config, "cap_add", None) or [])] or None

            container_sec_ctx = client.V1SecurityContext(
                allow_privilege_escalation=False,
                run_as_non_root=True if getattr(config, "container_non_root", False) else None,
                run_as_user=65532 if getattr(config, "container_non_root", False) else None,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
                capabilities=client.V1Capabilities(
                    drop=drop_caps if drop_caps else None,
                    add=add_caps,
                ),
            )

            backend = self._canonicalize_backend(config.backend) or await self._detect_backend()
            if backend not in ("kata-firecracker-devmapper", "kata-qemu-longhorn", "k7d"):
                return OperationResult(
                    success=False,
                    error=(
                        f"Unsupported backend '{backend}'. "
                        "Use kata-firecracker-devmapper (kfd), kata-qemu-longhorn (kql), or k7d."
                    ),
                )

            container_command = None
            container_args = None
            if backend != "kata-qemu-longhorn":
                container_command = ["/bin/sh", "-c", "sleep 365d"]

            container = client.V1Container(
                name="sandbox",
                image=config.image or "busybox",
                command=container_command,
                args=container_args,
                resources=client.V1ResourceRequirements(
                    limits=config.limits if config.limits else None,
                    requests=config.limits if config.limits else None,
                ),
                security_context=container_sec_ctx,
            )

            if config.env_file:
                container.env_from = [
                    client.V1EnvFromSource(secret_ref=client.V1SecretEnvSource(name=f"{config.name}-env"))
                ]

            if config.before_script:
                container.readiness_probe = client.V1Probe(
                    _exec=client.V1ExecAction(command=["/bin/sh", "-c", f"test -f {before_done_file}"]),
                    initial_delay_seconds=1,
                    period_seconds=5,
                    timeout_seconds=5,
                    failure_threshold=30,
                )
            else:
                # Exec probes on kata go shim→ttrpc→agent→vsock; kubelet's
                # default timeoutSeconds=1 cancels them whenever the guest is
                # busy, and a flood of cancelled execs corrupts the shim↔agent
                # ttrpc connection until the shim declares "Dead agent" and
                # kills a healthy VM (spec 18g, the kql-r3 dind IO wedge).
                # Generous timeout + long period keep probe pressure low.
                container.readiness_probe = client.V1Probe(
                    _exec=client.V1ExecAction(command=["/bin/sh", "-c", "true"]),
                    initial_delay_seconds=0,
                    period_seconds=10,
                    timeout_seconds=5,
                    failure_threshold=6,
                )

            base_annotations = {
                "k7.katakate.org/backend": backend,
            }

            memory_annotation_value: str | None = None
            # The Kata hypervisor memory annotation is meaningless for k7d —
            # the k7d shim sizes the VM straight from the pod's CRI
            # cpu/memory limits (spec 10c/17a).
            if config.limits and "memory" in config.limits and backend != "k7d":
                try:
                    memory_mib = self._memory_limit_to_mib(config.limits["memory"])
                except ValueError as e:
                    return OperationResult(success=False, error=str(e))
                base_annotations["io.katacontainers.config.hypervisor.default_memory"] = str(memory_mib)
                memory_annotation_value = str(memory_mib)

            if backend == "kata-qemu-longhorn":
                default_runtime_class = "kata-qemu"
            elif backend == "k7d":
                default_runtime_class = "k7"
            else:
                default_runtime_class = "kata"

            runtime_class = getattr(config, "runtime_class_name", None) or default_runtime_class

            root_pvc_name = None
            pvc_created = False
            wrapper_created = False
            wrapper_cm_name = None
            if backend == "kata-qemu-longhorn" and runtime_class == "kata-qemu":
                if not config.image:
                    return OperationResult(
                        success=False,
                        error="Image is required for kata-qemu-longhorn backend",
                    )
                root_pvc_name = self._root_pvc_name(config.name)
                base_annotations["k7.katakate.org/root-pvc-name"] = root_pvc_name
                ensure_res = await self._ensure_root_pvc(
                    sandbox_name=config.name,
                    namespace=config.namespace,
                    root_disk_size=config.root_disk_size or "10Gi",
                )
                if not ensure_res.success:
                    return ensure_res
                pvc_created = bool(ensure_res.data and ensure_res.data.get("created"))
                wrapper_script = self._load_persist_bind_script()
                wrapper_res = await self._ensure_persist_wrapper_configmap(
                    namespace=config.namespace,
                    name=config.name,
                    script_text=wrapper_script,
                )
                if not wrapper_res.success:
                    return wrapper_res
                wrapper_cm_name = wrapper_res.data["cm_name"]
                wrapper_created = bool(wrapper_res.data and wrapper_res.data.get("created"))
            else:
                # kata-firecracker-devmapper and k7d: no PVC, no ConfigMap
                # wrapper, no persist-bind script (k7d persistence rides the
                # VM's own state; spec 9a M11).
                if not config.image:
                    return OperationResult(
                        success=False,
                        error=f"Image is required for {backend} backend",
                    )

            pod_sec_ctx = None
            if getattr(config, "pod_non_root", False):
                pod_sec_ctx = client.V1PodSecurityContext(
                    run_as_non_root=True,
                    run_as_user=65532,
                    run_as_group=65532,
                    fs_group=65532,
                )

            pod_spec = client.V1PodSpec(
                containers=[container],
                runtime_class_name=runtime_class,
                restart_policy="Always",
                security_context=pod_sec_ctx,
                node_selector={f"k7.katakate.org/backend-{backend}": "true"},
                node_name=getattr(config, "node_name", None) or None,
            )

            if root_pvc_name and wrapper_cm_name:
                pod_spec.volumes = (pod_spec.volumes or []) + [
                    client.V1Volume(
                        name="state-disk",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=root_pvc_name),
                    ),
                    client.V1Volume(
                        name="persist-wrapper",
                        config_map=client.V1ConfigMapVolumeSource(
                            name=wrapper_cm_name,
                            default_mode=0o755,
                        ),
                    ),
                ]
                init_cmd = "set -eu; mkdir -p /mnt/state/pods"
                if config.sidecar and config.sidecar in SIDECAR_REGISTRY:
                    init_cmd += f"; mkdir -p /mnt/state/{SIDECAR_REGISTRY[config.sidecar].pvc_subdir}"
                init_cmd += "; echo ok > /mnt/state/.ready"
                pod_spec.init_containers = [
                    client.V1Container(
                        name="init-state",
                        image="busybox:1.36",
                        command=["/bin/sh", "-c", init_cmd],
                        volume_mounts=[client.V1VolumeMount(name="state-disk", mount_path="/mnt/state")],
                    )
                ]
                container.volume_mounts = (container.volume_mounts or []) + [
                    client.V1VolumeMount(name="state-disk", mount_path="/mnt/state"),
                    client.V1VolumeMount(name="persist-wrapper", mount_path="/opt/k7/bin"),
                ]
                container.env = (container.env or []) + [
                    client.V1EnvVar(name="K7_PERSIST_SLOT", value="main"),
                ]
                container.command = ["/opt/k7/bin/k7-persist-bind.sh"]
                image_entrypoint, image_cmd = await self._get_image_entrypoint_cmd(config.image)
                orig_argv = self._compute_image_argv(
                    image_entrypoint=image_entrypoint,
                    image_cmd=image_cmd,
                    override_entrypoint=config.entrypoint,
                    override_cmd=config.cmd,
                )
                container.args = orig_argv
                if container.security_context is None:
                    container.security_context = client.V1SecurityContext()
                container.security_context.privileged = True
                container.security_context.allow_privilege_escalation = True

            # --- Generic sidecar injection (driven entirely by SIDECAR_REGISTRY) ---
            if config.sidecar:
                spec = SIDECAR_REGISTRY[config.sidecar]

                socket_vol = client.V1Volume(
                    name="sidecar-socket",
                    empty_dir=client.V1EmptyDirVolumeSource(),
                )
                pod_spec.volumes = (pod_spec.volumes or []) + [socket_vol]

                if backend == "kata-qemu-longhorn":
                    data_mount = client.V1VolumeMount(
                        name="state-disk",
                        mount_path=spec.data_path,
                        sub_path=spec.pvc_subdir,
                    )
                else:
                    data_vol = client.V1Volume(
                        name="sidecar-data",
                        empty_dir=client.V1EmptyDirVolumeSource(),
                    )
                    pod_spec.volumes = (pod_spec.volumes or []) + [data_vol]
                    data_mount = client.V1VolumeMount(
                        name="sidecar-data",
                        mount_path=spec.data_path,
                    )

                sidecar_container = client.V1Container(
                    name="sidecar",
                    image=spec.image,
                    args=spec.args or None,
                    security_context=client.V1SecurityContext(privileged=spec.privileged),
                    env=[client.V1EnvVar(name=k, value=v) for k, v in spec.env.items()],
                    volume_mounts=[
                        client.V1VolumeMount(name="sidecar-socket", mount_path=spec.socket_mount),
                        data_mount,
                    ],
                    # `docker info` legitimately takes >1s while dockerd is
                    # busy (vfs copies a whole image rootfs per `docker run`,
                    # amplified by Longhorn r≥2). With kubelet's default 1s
                    # timeout this probe was cancelled dozens of times per
                    # workload, poisoning the kata shim↔agent ttrpc channel
                    # ("received message on inactive stream") until the shim
                    # killed the healthy VM (spec 18g wedge root cause).
                    readiness_probe=client.V1Probe(
                        _exec=client.V1ExecAction(command=spec.readiness_cmd),
                        initial_delay_seconds=3,
                        period_seconds=15,
                        timeout_seconds=12,
                        failure_threshold=4,
                    ),
                )
                pod_spec.containers.append(sidecar_container)

                container.volume_mounts = (container.volume_mounts or []) + [
                    client.V1VolumeMount(
                        name="sidecar-socket",
                        mount_path=spec.socket_mount,
                        read_only=False,
                    )
                ]

                base_annotations["k7.katakate.org/sidecar"] = config.sidecar

            pod_metadata = client.V1ObjectMeta(
                labels={
                    "app": config.name,
                    "katakate.org/sandbox": config.name,
                },
                annotations=base_annotations,
            )

            deployment = client.V1Deployment(
                metadata=client.V1ObjectMeta(
                    name=config.name,
                    namespace=config.namespace,
                    labels={
                        "app": config.name,
                        "runtime": "kata",
                        "katakate.org/sandbox": config.name,
                    },
                    annotations=base_annotations,
                ),
                spec=client.V1DeploymentSpec(
                    replicas=1,
                    selector=client.V1LabelSelector(match_labels={"app": config.name}),
                    template=client.V1PodTemplateSpec(
                        metadata=pod_metadata,
                        spec=pod_spec,
                    ),
                ),
            )

            deployment_created = False
            create_error: ApiException | None = None
            deployment_create_start = time.time()
            try:
                await apps_v1.create_namespaced_deployment(namespace=config.namespace, body=deployment)
                deployment_created = True
                print(
                    f"⏱️  Deployment created in {time.time() - deployment_create_start:.2f}s",
                    file=sys.stderr,
                )
            except ApiException as e:
                create_error = e
            finally:
                if not deployment_created:
                    if wrapper_created and wrapper_cm_name:
                        try:
                            await v1.delete_namespaced_config_map(name=wrapper_cm_name, namespace=config.namespace)
                        except Exception:
                            pass
                    if pvc_created and root_pvc_name:
                        try:
                            await v1.delete_namespaced_persistent_volume_claim(
                                name=root_pvc_name, namespace=config.namespace
                            )
                        except Exception:
                            pass
            if not deployment_created:
                if create_error and create_error.status == 409:
                    return OperationResult(success=False, error=f"Sandbox {config.name} already exists")
                return OperationResult(
                    success=False,
                    error=f"Failed to create deployment: {create_error}",
                )

            sched_check = await self._check_scheduling(
                sandbox_name=config.name,
                namespace=config.namespace,
                backend=backend,
            )
            if not sched_check.success:
                # Scheduling failed (no node matches the backend selector).
                # Roll back the deployment + dependent resources before returning.
                try:
                    await apps_v1.delete_namespaced_deployment(name=config.name, namespace=config.namespace)
                except Exception:
                    pass
                if wrapper_created and wrapper_cm_name:
                    try:
                        await v1.delete_namespaced_config_map(name=wrapper_cm_name, namespace=config.namespace)
                    except Exception:
                        pass
                if pvc_created and root_pvc_name:
                    try:
                        await v1.delete_namespaced_persistent_volume_claim(
                            name=root_pvc_name, namespace=config.namespace
                        )
                    except Exception:
                        pass
                _emit({"stage": "error", "error": sched_check.error})
                return sched_check

            async def _fail_and_rollback(error: str) -> OperationResult:
                """A failed create must not leave a half-provisioned sandbox
                behind (deployment/PVC/netpols). delete_sandbox already
                tears down every resource and tolerates missing ones."""
                try:
                    await self.delete_sandbox(config.name, namespace=config.namespace)
                except Exception as cleanup_err:
                    error = f"{error} (rollback also failed: {cleanup_err})"
                _emit({"stage": "error", "error": error})
                return OperationResult(success=False, error=error)

            if memory_annotation_value:
                try:
                    await apps_v1.patch_namespaced_deployment(
                        name=config.name,
                        namespace=config.namespace,
                        body={
                            "spec": {
                                "template": {
                                    "metadata": {
                                        "annotations": {
                                            "io.katacontainers.config.hypervisor.default_memory": memory_annotation_value
                                        }
                                    }
                                }
                            }
                        },
                    )
                except ApiException as e:
                    return await _fail_and_rollback(f"Failed to patch pod template annotations: {e}")
            _emit({"stage": "provisioning", "status": "done"})

            if config.before_script:
                _emit(
                    {
                        "stage": "before_script",
                        "status": "waiting",
                        "script": config.before_script,
                    }
                )
                try:
                    pod_wait_start = time.time()
                    pod_name = await self._wait_for_pod_container_started(
                        sandbox_name=config.name,
                        namespace=config.namespace,
                        timeout_seconds=300,
                        wait_all_containers=bool(config.sidecar),
                    )
                    if not pod_name:
                        return await _fail_and_rollback("Timed out waiting for sandbox container to start")
                    print(
                        f"⏱️  Pod container started in {time.time() - pod_wait_start:.2f}s",
                        file=sys.stderr,
                    )
                    script = config.before_script.strip()
                    # `pipefail` isn't a POSIX sh builtin (the `sleep 365d` pid 1
                    # may be busybox sh, dash, ...). Try to enable it but fall
                    # back gracefully so the script still runs under stricter
                    # shells; `set -eu` is universal.
                    exec_cmd = [
                        "/bin/sh",
                        "-c",
                        (
                            "(set -o pipefail) 2>/dev/null && set -o pipefail; "
                            f"set -eu; {script}; touch {before_done_file}"
                        ),
                    ]
                    async with WsApiClient() as ws_api:
                        v1_ws = client.CoreV1Api(api_client=ws_api)
                        await v1_ws.connect_get_namespaced_pod_exec(
                            pod_name,
                            config.namespace,
                            container="sandbox",
                            command=exec_cmd,  # ty: ignore[invalid-argument-type]
                            stderr=True,
                            stdin=False,
                            stdout=True,
                            tty=False,
                        )
                except Exception as e:
                    return await _fail_and_rollback(f"before_script failed: {e}")
                _emit({"stage": "before_script", "status": "done"})
            else:
                _emit({"stage": "before_script", "status": "skipped"})

            if config.egress_whitelist is not None:
                _emit({"stage": "network_lockdown", "status": "applying"})
                cidrs, fqdns = _classify_egress_entries(config.egress_whitelist)

                if fqdns:
                    cilium_available = await self._cilium_available()
                    if not cilium_available:
                        return await _fail_and_rollback(
                            "Domain-based egress "
                            f"({', '.join(fqdns)}) requires the Cilium CNI. "
                            "Re-install the cluster with: k7 install --cni cilium"
                        )
                    cnp_result = await self._apply_cilium_egress_policy(
                        sandbox_name=config.name,
                        namespace=config.namespace,
                        cidrs=cidrs,
                        fqdns=fqdns,
                    )
                    if not cnp_result.success:
                        return await _fail_and_rollback(cnp_result.error or "Cilium egress policy failed")
                else:
                    egress_rules = [
                        client.V1NetworkPolicyEgressRule(
                            to=[client.V1NetworkPolicyPeer(ip_block=client.V1IPBlock(cidr=cidr))]
                        )
                        for cidr in cidrs
                    ]

                    network_policy = client.V1NetworkPolicy(
                        metadata=client.V1ObjectMeta(name=f"{config.name}-netpol", namespace=config.namespace),
                        spec=client.V1NetworkPolicySpec(
                            pod_selector=client.V1LabelSelector(match_labels={"katakate.org/sandbox": config.name}),
                            policy_types=["Egress"],
                            egress=egress_rules,
                        ),
                    )

                    try:
                        await networking_v1.create_namespaced_network_policy(
                            namespace=config.namespace, body=network_policy
                        )
                    except ApiException as e:
                        if e.status != 409:
                            return await _fail_and_rollback(f"Failed to create network policy: {e}")
                _emit({"stage": "network_lockdown", "status": "done"})
            else:
                _emit({"stage": "network_lockdown", "status": "skipped"})

            try:
                ingress_np = client.V1NetworkPolicy(
                    metadata=client.V1ObjectMeta(
                        name=f"{config.name}-deny-ingress",
                        namespace=config.namespace,
                    ),
                    spec=client.V1NetworkPolicySpec(
                        pod_selector=client.V1LabelSelector(match_labels={"katakate.org/sandbox": config.name}),
                        policy_types=["Ingress"],
                        ingress=[],
                    ),
                )
                await networking_v1.create_namespaced_network_policy(namespace=config.namespace, body=ingress_np)
            except ApiException as e:
                status = getattr(e, "status", None)
                if status == 409:
                    try:
                        _emit(
                            {
                                "stage": "network_lockdown",
                                "status": "exists",
                                "policy": f"{config.name}-deny-ingress",
                            }
                        )
                    except Exception:
                        pass
                else:
                    try:
                        _emit(
                            {
                                "stage": "network_lockdown",
                                "status": "error",
                                "error": f"Ingress deny policy error: {e}",
                            }
                        )
                    except Exception:
                        pass
                    return await _fail_and_rollback(f"Failed to create ingress deny policy: {e}")

            _emit(
                {
                    "stage": "complete",
                    "status": "success",
                    "message": f"Sandbox {config.name} created successfully",
                }
            )
            return OperationResult(success=True, message=f"Sandbox {config.name} created successfully")

        except Exception as e:
            try:
                _emit({"stage": "error", "error": str(e)})
            except Exception:
                pass
            return OperationResult(success=False, error=str(e))

    async def list_sandboxes(self, namespace: str | None = None) -> list[SandboxInfo]:
        """List all sandboxes."""
        try:
            v1 = await self._get_core_v1_client()
            sandboxes = await self._get_kata_sandboxes(namespace)

            sandbox_list = []
            for deployment in sandboxes:
                name = deployment.metadata.name
                ns = deployment.metadata.namespace

                annotations = deployment.metadata.annotations or {}
                backend = annotations.get("k7.katakate.org/backend", "unknown")

                try:
                    pods = await v1.list_namespaced_pod(namespace=ns, label_selector=f"app={name}")
                    # Ignore Terminating pods: right after a resume the old
                    # replica can linger with a stale Ready=True condition,
                    # making clients (and tests) exec into a dying VM.
                    live = [p for p in pods.items if not p.metadata.deletion_timestamp]
                    if live:
                        pod = live[0]
                        status = pod.status.phase or "Unknown"
                        ready = (
                            "True"
                            if pod.status.conditions
                            and any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions)
                            else "False"
                        )
                        restarts = sum(cs.restart_count for cs in pod.status.container_statuses or [])
                        created_at = pod.metadata.creation_timestamp
                        if created_at is None:
                            age = "Unknown"
                        else:
                            if created_at.tzinfo is None:
                                created_at = created_at.replace(tzinfo=timezone.utc)
                            age = str(datetime.now(timezone.utc) - created_at)
                        image = pod.spec.containers[0].image if pod.spec.containers else "Unknown"
                        node = pod.spec.node_name or ""
                    else:
                        status = "No Pods"
                        ready = "False"
                        restarts = 0
                        age = "Unknown"
                        image = "Unknown"
                        node = ""
                except Exception:
                    status = "Error"
                    ready = "False"
                    restarts = 0
                    age = "Unknown"
                    image = "Unknown"
                    node = ""

                sandbox_list.append(
                    SandboxInfo(
                        name=name,
                        namespace=ns,
                        status=status,
                        ready=ready,
                        restarts=restarts,
                        age=age,
                        image=image,
                        backend=backend,
                        node=node,
                    )
                )

            return sandbox_list

        except Exception:
            return []

    async def pause_sandbox(
        self,
        name: str,
        namespace: str = "default",
        pvc_name: str | None = None,
        snapshot_name: str | None = None,
        snapshot_class: str = "longhorn",
    ) -> OperationResult:
        """Scale down the sandbox and optionally take a crash-consistent snapshot of its PVC.

        Snapshot is taken whenever ``snapshot_name`` is set. ``pvc_name`` defaults
        to ``_root_pvc_name(name)`` (kata-qemu-longhorn convention) when not supplied.

        k7d backend: the sandbox's microVM is paused **in place** through the
        k7d daemon (vCPUs stopped, guest memory and vsock identity retained) —
        the pod stays scheduled, and ``resume_sandbox`` restarts the vCPUs on
        the exact same state. Longhorn-style ``snapshot_name`` does not apply.
        """
        try:
            backend = await self._detect_backend(name, namespace)
            if backend == "k7d":
                if snapshot_name:
                    return OperationResult(
                        success=False,
                        error=f"--snapshot is not supported when pausing a k7d sandbox: {K7D_SNAPSHOT_UNSUPPORTED}",
                    )
                node = await self._k7d_sandbox_node(name, namespace)
                if node and node != self._k7d_local_node():
                    return await self._k7d_forward_vm_op(
                        node, "pause", {"name": name, "namespace": namespace}, timeout=120
                    )
                vm = await self._k7d_vm_for_sandbox(name, namespace)
                await self._k7d_request({"op": "pause_vm", "vm_id": vm["vm_id"]})
                await self._update_deployment_annotation(name, namespace, K7D_ANN_PAUSED, "true")
                return OperationResult(
                    success=True,
                    message=(
                        f"Sandbox {name} paused (k7d VM {vm['vm_id']} frozen in place; memory retained, vCPUs stopped)."
                    ),
                )

            apps_v1 = await self._get_apps_v1_client()
            scale_body = {"spec": {"replicas": 0}}
            await apps_v1.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=scale_body)
            snap_result = None
            if snapshot_name:
                effective_pvc = pvc_name or self._root_pvc_name(name)
                snap_result = await self._create_volume_snapshot(
                    pvc_name=effective_pvc,
                    snapshot_name=snapshot_name,
                    snapshot_class=snapshot_class,
                    namespace=namespace,
                    kind=SNAPSHOT_KIND_PAUSE,
                    source_sandbox=name,
                )
                if not snap_result.success:
                    return snap_result
            msg = f"Sandbox {name} paused (replicas=0)."
            if snap_result and snap_result.success:
                msg += f" Snapshot {snapshot_name} created."
            return OperationResult(success=True, message=msg)
        except ApiException as e:
            return OperationResult(success=False, error=f"Kubernetes error while pausing: {e}")
        except Exception as e:
            return OperationResult(success=False, error=str(e))

    async def resume_sandbox(self, name: str, namespace: str = "default") -> OperationResult:
        """Scale sandbox back to 1 replica (k7d: unfreeze the VM in place)."""
        try:
            backend = await self._detect_backend(name, namespace)
            if backend == "k7d":
                node = await self._k7d_sandbox_node(name, namespace)
                if node and node != self._k7d_local_node():
                    return await self._k7d_forward_vm_op(
                        node, "resume", {"name": name, "namespace": namespace}, timeout=120
                    )
                vm = await self._k7d_vm_for_sandbox(name, namespace)
                await self._k7d_request({"op": "resume_vm", "vm_id": vm["vm_id"]})
                await self._update_deployment_annotation(name, namespace, K7D_ANN_PAUSED, "false")
                return OperationResult(
                    success=True,
                    message=f"Sandbox {name} resumed (k7d VM {vm['vm_id']} vCPUs restarted).",
                )

            apps_v1 = await self._get_apps_v1_client()
            scale_body = {"spec": {"replicas": 1}}
            await apps_v1.patch_namespaced_deployment_scale(name=name, namespace=namespace, body=scale_body)
            return OperationResult(success=True, message=f"Sandbox {name} resumed (replicas=1).")
        except ApiException as e:
            return OperationResult(success=False, error=f"Kubernetes error while resuming: {e}")
        except Exception as e:
            return OperationResult(success=False, error=str(e))

    def shell_into_sandbox(
        self,
        sandbox_name: str,
        namespace: str = "default",
    ) -> OperationResult:
        """Shell into a sandbox pod via kubectl exec. Stays sync (interactive CLI-only)."""
        v1 = asyncio.run(self._get_core_v1_client())
        pods = asyncio.run(v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={sandbox_name}"))
        if not pods.items:
            return OperationResult(success=False, error="Pod not found")

        pod_name = pods.items[0].metadata.name
        kubectl_cmd = ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]
        exec_command = kubectl_cmd + ["exec", "-it", pod_name, "-n", namespace, "-c", "sandbox", "--", "/bin/sh"]

        try:
            result = subprocess.run(exec_command, check=False)
        except Exception as e:
            return OperationResult(success=False, error=f"Failed to exec into pod: {e}")

        if result.returncode != 0:
            return OperationResult(
                success=False,
                error=f"Shell exited with code {result.returncode}",
            )
        return OperationResult(success=True)

    async def fork_sandbox(
        self,
        source_name: str,
        new_name: str,
        namespace: str = "default",
        snapshot_name: str | None = None,
    ) -> OperationResult:
        """Clone a sandbox Deployment definition to a new name, cloning its root PVC.

        k7d backend: instead of a Longhorn disk clone, the new pod carries the
        ``k7d.katakate.org/fork-source-*`` annotations, so its containerd shim
        boots it as a **warm fork** (CoW disk+memory) of the source's live VM
        through the k7d daemon.
        """
        fork_start = time.time()
        try:
            backend = await self._detect_backend(source_name, namespace)
            if backend == "k7d":
                return await self._fork_sandbox_k7d(
                    source_name=source_name,
                    new_name=new_name,
                    namespace=namespace,
                    snapshot_name=snapshot_name,
                    fork_start=fork_start,
                )

            apps_v1 = await self._get_apps_v1_client()
            v1 = await self._get_core_v1_client()
            src = await apps_v1.read_namespaced_deployment(name=source_name, namespace=namespace)

            source_pvc_name = self._root_pvc_name(source_name)
            target_pvc_name = self._root_pvc_name(new_name)

            try:
                source_pvc = await v1.read_namespaced_persistent_volume_claim(name=source_pvc_name, namespace=namespace)
            except ApiException as e:
                if e.status == 404:
                    return OperationResult(
                        success=False,
                        error=f"Source root PVC {source_pvc_name} not found; cannot fork storage",
                    )
                return OperationResult(success=False, error=f"PVC lookup error: {e}")

            # Spec 10e: classify the snapshot so list/gc can find it. A user-supplied
            # ``snapshot_name`` is treated as a "named" snapshot (persists across the
            # fork). The auto-named case uses the canonical ``<source>-fork-<ts>``
            # form and is marked ``kind=fork`` so it gets cleaned up below (and by
            # the GC backstop if the inline delete fails for any reason).
            if snapshot_name:
                snap_name = snapshot_name
                snap_kind = SNAPSHOT_KIND_NAMED
                auto_temp_snapshot = False
            else:
                snap_name = f"{source_name}-fork-{int(time.time())}"
                snap_kind = SNAPSHOT_KIND_FORK
                auto_temp_snapshot = True
            # A Longhorn VolumeSnapshot is block-level and only
            # crash-consistent: guest writes still sitting in the VM's page
            # cache would be missing from the fork's clone. Flush them first
            # when the source is running (fail loudly if the flush fails);
            # a scaled-to-0 (paused) source has no writers and needs no flush.
            if (src.status.ready_replicas or 0) > 0:
                sync_res = await self.exec_command(source_name, "sync", namespace=namespace)
                if sync_res.exit_code != 0:
                    return OperationResult(
                        success=False,
                        error=f"pre-fork sync in source sandbox failed: {sync_res.stderr}",
                    )
            snap_result = await self._create_volume_snapshot(
                pvc_name=source_pvc_name,
                snapshot_name=snap_name,
                snapshot_class="longhorn",
                namespace=namespace,
                kind=snap_kind,
                source_sandbox=source_name,
            )
            if not snap_result.success:
                return snap_result

            ready = await self._wait_for_snapshot_ready(snap_name, namespace=namespace)
            if not ready.success:
                return ready

            # Derive the cloned PVC's size from the source PVC's request.
            source_size = "10Gi"
            try:
                source_size = str(source_pvc.spec.resources.requests.get("storage") or source_size)
            except Exception:
                pass
            pvc_clone = await self._create_pvc_from_snapshot(
                target_pvc_name=target_pvc_name,
                namespace=namespace,
                snapshot_name=snap_name,
                storage_size=source_size,
                source_pvc_spec=source_pvc.spec,
            )
            if not pvc_clone.success:
                return pvc_clone

            new_dep = copy.deepcopy(src)
            new_dep.metadata.name = new_name
            new_dep.metadata.resource_version = None
            new_dep.metadata.uid = None
            if hasattr(new_dep.metadata, "managed_fields"):
                new_dep.metadata.managed_fields = None
            if hasattr(new_dep.metadata, "creation_timestamp"):
                new_dep.metadata.creation_timestamp = None
            if hasattr(new_dep.metadata, "generation"):
                new_dep.metadata.generation = None
            if new_dep.metadata.labels:
                new_dep.metadata.labels["app"] = new_name
                new_dep.metadata.labels["katakate.org/sandbox"] = new_name
            if new_dep.spec and new_dep.spec.selector and new_dep.spec.selector.match_labels:
                new_dep.spec.selector.match_labels["app"] = new_name
            if new_dep.spec and new_dep.spec.template and new_dep.spec.template.metadata:
                if new_dep.spec.template.metadata.labels is None:
                    new_dep.spec.template.metadata.labels = {}
                if hasattr(new_dep.spec.template.metadata, "managed_fields"):
                    new_dep.spec.template.metadata.managed_fields = None
                if hasattr(new_dep.spec.template.metadata, "creation_timestamp"):
                    new_dep.spec.template.metadata.creation_timestamp = None
                new_dep.spec.template.metadata.labels["app"] = new_name
                new_dep.spec.template.metadata.labels["katakate.org/sandbox"] = new_name
            new_dep.spec.replicas = 1

            if new_dep.spec and new_dep.spec.template and new_dep.spec.template.spec:
                vols = new_dep.spec.template.spec.volumes or []
                for v in vols:
                    pvc_ref = getattr(v, "persistent_volume_claim", None)
                    if pvc_ref and pvc_ref.claim_name == source_pvc_name:
                        pvc_ref.claim_name = target_pvc_name

            try:
                await apps_v1.create_namespaced_deployment(namespace=namespace, body=new_dep)
            except ApiException as e:
                if e.status == 409:
                    return OperationResult(success=False, error=f"Sandbox {new_name} already exists")
                raise

            # With WaitForFirstConsumer the cloned PVC stays Pending until the pod is
            # scheduled, so wait_for_pvc_bound MUST run after the Deployment exists.
            bound = await self._wait_for_pvc_bound(target_pvc_name, namespace=namespace)
            if not bound.success:
                try:
                    await apps_v1.delete_namespaced_deployment(name=new_name, namespace=namespace)
                except Exception:
                    pass
                try:
                    await v1.delete_namespaced_persistent_volume_claim(name=target_pvc_name, namespace=namespace)
                except Exception:
                    pass
                return bound

            # Spec 10e Option A: an auto-named fork snapshot has done its job once
            # the cloned PVC is Bound (Longhorn has finished provisioning the new
            # volume from the snapshot's data). Delete it inline so namespaces
            # don't accumulate stale ``-fork-`` snapshots. Best-effort: failures
            # are swallowed because the fork itself succeeded; the GC backstop
            # (Option C, snapshot-gc CronJob) will eventually catch any leak.
            if auto_temp_snapshot:
                try:
                    await self.delete_snapshot(snap_name, namespace=namespace)
                except Exception as e:
                    print(
                        f"⚠️  Failed to inline-delete fork snapshot {snap_name}: {e}",
                        file=sys.stderr,
                    )

            fork_elapsed = time.time() - fork_start
            return OperationResult(
                success=True,
                message=(
                    f"Forked sandbox {source_name} -> {new_name} with cloned disk "
                    f"(PVC {target_pvc_name} from snapshot {snap_name}) in {fork_elapsed:.2f}s"
                ),
            )
        except ApiException as e:
            return OperationResult(success=False, error=f"Kubernetes error while forking: {e}")
        except Exception as e:
            return OperationResult(success=False, error=str(e))

    async def _fork_sandbox_k7d(
        self,
        source_name: str,
        new_name: str,
        namespace: str,
        snapshot_name: str | None,
        fork_start: float,
    ) -> OperationResult:
        """k7d warm fork: create a copy of the source Deployment whose pod
        carries the ``k7d.katakate.org/fork-source-*`` annotations. The k7d
        shim resolves the source VM through the daemon and boots the new pod
        as a CoW disk+memory fork of it (k7d spec 17d).

        Limitations (fail loud, documented): single-workload sandboxes only
        (no sidecar), and the fork pod re-forks from the live source if its
        pod is ever restarted. Whole-cluster fork (forking a multi-VM inner
        k8s cluster as one unit) is a k7d-native feature, not a k7 verb —
        drive it through the k7d daemon API (https://github.com/katakate/k7d).
        """
        if snapshot_name:
            return OperationResult(
                success=False,
                error=f"--snapshot is not supported when forking a k7d sandbox: {K7D_SNAPSHOT_UNSUPPORTED}",
            )
        apps_v1 = await self._get_apps_v1_client()
        src = await apps_v1.read_namespaced_deployment(name=source_name, namespace=namespace)

        sidecar_ann = (src.metadata.annotations or {}).get("k7.katakate.org/sidecar")
        if sidecar_ann:
            return OperationResult(
                success=False,
                error=(
                    f"forking a k7d sandbox with a '{sidecar_ann}' sidecar is not supported: "
                    "the fork adopts the single workload container running inside the forked VM "
                    "(k7d spec 17d). Fork the sandbox without a sidecar, or use the kql backend "
                    "for sidecar forks."
                ),
            )

        # Spec 18g: the fork must run on the source VM's node (the daemon
        # socket is node-local; the fork pod is pinned there too). Forward
        # the WHOLE fork to that node's k7-agent when the source is remote.
        node = await self._k7d_sandbox_node(source_name, namespace)
        if node and node != self._k7d_local_node():
            return await self._k7d_forward_vm_op(
                node,
                "fork",
                {"name": source_name, "new_name": new_name, "namespace": namespace, "snapshot": snapshot_name},
                timeout=600,
            )

        # Resolve the live source VM up-front so a dead/unknown source fails
        # here with a clear message instead of leaving a CrashLooping fork pod.
        vm = await self._k7d_vm_for_sandbox(source_name, namespace)
        fork_annotations = {
            K7D_ANN_FORK_SOURCE_CLUSTER: vm.get("cluster_id") or vm["sandbox_id"],
            K7D_ANN_FORK_SOURCE_VM: vm["sandbox_id"],
        }

        new_dep = copy.deepcopy(src)
        new_dep.metadata.name = new_name
        new_dep.metadata.resource_version = None
        new_dep.metadata.uid = None
        for attr in ("managed_fields", "creation_timestamp", "generation"):
            if hasattr(new_dep.metadata, attr):
                setattr(new_dep.metadata, attr, None)
        if new_dep.metadata.labels:
            new_dep.metadata.labels["app"] = new_name
            new_dep.metadata.labels["katakate.org/sandbox"] = new_name
        if new_dep.spec and new_dep.spec.selector and new_dep.spec.selector.match_labels:
            new_dep.spec.selector.match_labels["app"] = new_name
        template = new_dep.spec.template if new_dep.spec else None
        if template and template.metadata:
            for attr in ("managed_fields", "creation_timestamp"):
                if hasattr(template.metadata, attr):
                    setattr(template.metadata, attr, None)
            if template.metadata.labels is None:
                template.metadata.labels = {}
            template.metadata.labels["app"] = new_name
            template.metadata.labels["katakate.org/sandbox"] = new_name
            template.metadata.annotations = {
                **(template.metadata.annotations or {}),
                **fork_annotations,
            }
        new_dep.spec.replicas = 1
        # The fork must land on the source VM's node — the k7d daemon socket
        # is node-local (cross-node fork is k7d spec 9a M12, not built yet).
        source_pod = await self._k7d_running_pod(source_name, namespace)
        new_dep.spec.template.spec.node_name = source_pod.spec.node_name

        try:
            await apps_v1.create_namespaced_deployment(namespace=namespace, body=new_dep)
        except ApiException as e:
            if e.status == 409:
                return OperationResult(success=False, error=f"Sandbox {new_name} already exists")
            raise

        pod_name = await self._wait_for_pod_container_started(
            sandbox_name=new_name,
            namespace=namespace,
            timeout_seconds=120,
        )
        if not pod_name:
            try:
                await apps_v1.delete_namespaced_deployment(name=new_name, namespace=namespace)
            except Exception:
                pass
            return OperationResult(
                success=False,
                error=(
                    f"forked sandbox {new_name} never reached Running — the k7d fork failed "
                    "(check `kubectl describe pod` events and the k7d daemon logs)"
                ),
            )

        fork_elapsed = time.time() - fork_start
        return OperationResult(
            success=True,
            message=(
                f"Forked sandbox {source_name} -> {new_name} as a k7d warm fork "
                f"(CoW disk+memory of VM {vm['vm_id']}) in {fork_elapsed:.2f}s"
            ),
        )

    # -------------------------------------------------------------------
    # Spec 10e: VolumeSnapshot lifecycle (list / get / create / delete / gc).
    # -------------------------------------------------------------------

    async def list_snapshots(
        self,
        namespace: str | None = "default",
        all_namespaces: bool = False,
        sandbox: str | None = None,
        kind: str | None = None,
    ) -> list[SnapshotInfo]:
        """List ``VolumeSnapshot`` objects, classified by k7 kind.

        ``all_namespaces=True`` returns every namespace; otherwise restricts to
        ``namespace``. ``sandbox`` filters to snapshots that target a sandbox's
        root PVC (by ``k7.io/source-sandbox`` annotation, with a name-pattern
        fallback for snapshots created before Spec 10e).
        """
        custom = await self._get_custom_objects_client()
        try:
            if all_namespaces:
                resp = await custom.list_cluster_custom_object(
                    group="snapshot.storage.k8s.io",
                    version="v1",
                    plural="volumesnapshots",
                )
            else:
                resp = await custom.list_namespaced_custom_object(
                    group="snapshot.storage.k8s.io",
                    version="v1",
                    namespace=namespace or "default",
                    plural="volumesnapshots",
                )
        except ApiException as e:
            print(f"⚠️  Failed to list VolumeSnapshots: {e}", file=sys.stderr)
            return []

        items = resp.get("items") or []
        out: list[SnapshotInfo] = []
        for item in items:
            info = self._snapshot_info_from_object(item)
            if kind and info.kind != kind:
                continue
            if sandbox:
                if info.source_sandbox:
                    if info.source_sandbox != sandbox:
                        continue
                else:
                    # Fallback: source PVC convention for kata-qemu-longhorn.
                    if info.source_pvc != self._root_pvc_name(sandbox):
                        continue
            out.append(info)
        return out

    async def get_snapshot(self, name: str, namespace: str = "default") -> SnapshotInfo | None:
        """Return a single ``SnapshotInfo`` or ``None`` if the object is absent."""
        custom = await self._get_custom_objects_client()
        try:
            snap = await custom.get_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                name=name,
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        return self._snapshot_info_from_object(snap)

    async def delete_snapshot(self, name: str, namespace: str = "default") -> OperationResult:
        """Delete a ``VolumeSnapshot`` by name.

        When the last surviving snapshot for an *orphaned* root PVC (one whose
        source sandbox was already deleted — kept around by
        :meth:`_delete_sandbox_resources` so Longhorn could still clone from
        it) is removed, the PVC is reaped here. That closes the spec-10f
        lifecycle: once you delete the last "save point", the disk goes away.
        """
        custom = await self._get_custom_objects_client()
        # Read the snapshot first so we know its source PVC after the delete.
        source_pvc: str | None = None
        try:
            snap_obj = await custom.get_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                name=name,
            )
            source_pvc = ((snap_obj.get("spec") or {}).get("source") or {}).get("persistentVolumeClaimName")
        except ApiException:
            pass
        try:
            await custom.delete_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                name=name,
            )
        except ApiException as e:
            if e.status == 404:
                return OperationResult(
                    success=False,
                    error=f"VolumeSnapshot {name} not found in namespace {namespace}",
                )
            return OperationResult(success=False, error=f"Snapshot delete failed: {e.reason or e.body}")

        if source_pvc:
            await self._maybe_reap_orphaned_pvc(source_pvc, namespace)
        return OperationResult(success=True, message=f"Snapshot {name} deleted")

    async def _maybe_reap_orphaned_pvc(self, pvc_name: str, namespace: str) -> None:
        """Delete an orphaned root PVC when its last referencing snapshot is gone.

        See :meth:`_delete_sandbox_resources` for context: when a sandbox is
        deleted while pause/named snapshots still reference its PVC, the PVC
        is kept around with the ``k7.io/orphaned-by`` annotation. When that
        last snapshot finally gets deleted, the PVC has no remaining
        purpose — clean it up here so we don't leak Longhorn volumes.
        """
        v1 = await self._get_core_v1_client()
        try:
            pvc = await v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
        except ApiException:
            return
        annotations = (pvc.metadata.annotations or {}) if pvc.metadata else {}
        if "k7.io/orphaned-by" not in annotations:
            return
        # Are there any other snapshots still referencing this PVC?
        try:
            custom = await self._get_custom_objects_client()
            snaps = await custom.list_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
            )
            for snap in snaps.get("items", []):
                src = ((snap.get("spec") or {}).get("source") or {}).get("persistentVolumeClaimName")
                if src == pvc_name:
                    return  # someone still holds a reference; keep the PVC.
        except Exception:
            return
        try:
            await v1.delete_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
        except ApiException:
            pass

    async def create_snapshot(
        self,
        sandbox_name: str,
        snapshot_name: str,
        namespace: str = "default",
    ) -> OperationResult:
        """Snapshot a running sandbox's root PVC without pausing it (kind=named)."""
        backend = await self._detect_backend(sandbox_name, namespace)
        if backend == "k7d":
            return OperationResult(
                success=False,
                error=f"`k7 snapshot create` does not support the k7d backend: {K7D_SNAPSHOT_UNSUPPORTED}",
            )
        return await self._create_volume_snapshot(
            pvc_name=self._root_pvc_name(sandbox_name),
            snapshot_name=snapshot_name,
            snapshot_class="longhorn",
            namespace=namespace,
            kind=SNAPSHOT_KIND_NAMED,
            source_sandbox=sandbox_name,
        )

    async def gc_snapshots(
        self,
        namespace: str = "default",
        all_namespaces: bool = False,
        keep_fork_for: timedelta = timedelta(minutes=10),
        dry_run: bool = False,
    ) -> OperationResult:
        """Sweep stale ``kind=fork`` snapshots older than ``keep_fork_for``.

        Pause and named snapshots are **never** touched — anything the user
        named survives. The data payload of the returned ``OperationResult``
        is a list of ``{name, namespace, age, deleted}`` records describing
        what was (or would be) removed.
        """
        candidates = await self.list_snapshots(
            namespace=namespace,
            all_namespaces=all_namespaces,
            kind=SNAPSHOT_KIND_FORK,
        )
        cutoff = datetime.now(timezone.utc) - keep_fork_for
        results: list[dict] = []
        for snap in candidates:
            try:
                created = datetime.fromisoformat((snap.creation_timestamp or "").replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except Exception:
                # Without a parseable timestamp we conservatively skip — leave it for the next sweep.
                continue
            if created > cutoff:
                continue
            record = {
                "name": snap.name,
                "namespace": snap.namespace,
                "age": snap.age,
                "deleted": False,
            }
            if not dry_run:
                deletion = await self.delete_snapshot(snap.name, namespace=snap.namespace)
                record["deleted"] = deletion.success
                if not deletion.success:
                    record["error"] = deletion.error
            results.append(record)
        verb = "would delete" if dry_run else "deleted"
        return OperationResult(
            success=True,
            message=f"GC {verb} {len(results)} fork snapshot(s)",
            data=results,
        )

    # -------------------------------------------------------------------
    # Spec 10f: restore a sandbox from a standalone VolumeSnapshot.
    # -------------------------------------------------------------------

    def _rehydrate_config_from_snapshot(
        self,
        annotations: dict[str, str],
        new_name: str,
        namespace: str,
        overrides: SandboxConfigOverrides | None,
    ) -> OperationResult:
        """Build a :class:`SandboxConfig` from a snapshot's ``k7.io/source-*``
        annotations, with optional per-call overrides.

        Returns the config in ``data`` on success. Returns success=False with a
        helpful error when neither annotation nor override supplies the
        required ``image`` field — that's the one value k7 can't safely guess.
        """
        ov = overrides or SandboxConfigOverrides()
        image = ov.image or annotations.get("k7.io/source-image")
        if not image:
            return OperationResult(
                success=False,
                error=(
                    "Snapshot lacks 'k7.io/source-image' annotation and no override supplied. "
                    "Pass --image explicitly to restore from this snapshot."
                ),
            )
        backend = self._canonicalize_backend(
            ov.backend or annotations.get("k7.io/source-backend", "kata-qemu-longhorn")
        )
        sidecar = ov.sidecar if ov.sidecar is not None else annotations.get("k7.io/source-sidecar")
        root_disk_size = ov.root_disk_size or annotations.get("k7.io/source-root-disk-size", "10Gi")
        limits = ov.limits
        if limits is None:
            raw_limits = annotations.get("k7.io/source-limits")
            if raw_limits:
                try:
                    parsed = json.loads(raw_limits)
                    if isinstance(parsed, dict):
                        limits = {k: str(v) for k, v in parsed.items()}
                except Exception:
                    limits = None
        return OperationResult(
            success=True,
            data=SandboxConfig(
                name=new_name,
                image=image,
                namespace=namespace,
                backend=backend,
                root_disk_size=root_disk_size,
                limits=limits,
                sidecar=sidecar or None,
                entrypoint=ov.entrypoint,
                cmd=ov.cmd,
                before_script=ov.before_script or "",
            ),
        )

    async def restore_sandbox(
        self,
        snapshot_name: str,
        new_sandbox_name: str,
        namespace: str = "default",
        overrides: SandboxConfigOverrides | None = None,
        keep_snapshot: bool = True,
    ) -> OperationResult:
        """Boot a brand-new sandbox from a standalone ``VolumeSnapshot``.

        The snapshot's ``k7.io/source-*`` annotations (stamped by
        :meth:`_create_volume_snapshot`) supply image / backend / sidecar /
        limits / root-disk-size; ``overrides`` (Spec 10f) can override any
        field. Restore is **kata-qemu-longhorn only** — there is no PVC to clone
        from in the kata-firecracker-devmapper backend.

        The cloned PVC is named ``_root_pvc_name(new_sandbox_name)`` and is
        pre-created from the snapshot before ``create_sandbox`` runs; the
        idempotent ``_ensure_root_pvc`` in ``create_sandbox`` then detects
        the existing PVC and skips its own creation step.

        ``keep_snapshot=False`` deletes the snapshot once the new sandbox
        reaches Ready — useful for "thaw and discard" flows.
        """
        # Look up the snapshot (typed view + raw annotations).
        snap_info = await self.get_snapshot(snapshot_name, namespace=namespace)
        if snap_info is None:
            return OperationResult(
                success=False,
                error=f"VolumeSnapshot {snapshot_name} not found in namespace {namespace}",
            )
        if not snap_info.ready_to_use:
            return OperationResult(
                success=False,
                error=(
                    f"VolumeSnapshot {snapshot_name} is not ready_to_use yet. "
                    f"Wait for the snapshot to settle before restoring."
                ),
            )
        custom = await self._get_custom_objects_client()
        try:
            snap_obj = await custom.get_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                name=snapshot_name,
            )
        except ApiException as e:
            return OperationResult(success=False, error=f"Failed to read snapshot {snapshot_name}: {e}")
        annotations = ((snap_obj.get("metadata") or {}).get("annotations") or {}) if isinstance(snap_obj, dict) else {}

        config_result = self._rehydrate_config_from_snapshot(
            annotations=annotations,
            new_name=new_sandbox_name,
            namespace=namespace,
            overrides=overrides,
        )
        if not config_result.success:
            return config_result
        config: SandboxConfig = config_result.data

        # Restore is kata-qemu-longhorn-only: kata-firecracker-devmapper has no PVC to
        # clone, so it has no snapshots either.
        if config.backend != "kata-qemu-longhorn":
            return OperationResult(
                success=False,
                error=(f"Restore requires the kata-qemu-longhorn backend; requested backend was '{config.backend}'"),
            )

        # Bail early if the new sandbox name is already taken (so we don't
        # half-create a PVC then fail at deployment time).
        apps_v1 = await self._get_apps_v1_client()
        try:
            await apps_v1.read_namespaced_deployment(name=new_sandbox_name, namespace=namespace)
            return OperationResult(success=False, error=f"Sandbox {new_sandbox_name} already exists")
        except ApiException as e:
            if e.status != 404:
                return OperationResult(success=False, error=f"Deployment lookup error: {e}")

        target_pvc = self._root_pvc_name(new_sandbox_name)
        pvc_clone = await self._create_pvc_from_snapshot(
            target_pvc_name=target_pvc,
            namespace=namespace,
            snapshot_name=snapshot_name,
            storage_size=config.root_disk_size or "10Gi",
        )
        if not pvc_clone.success:
            return pvc_clone

        # ``create_sandbox`` does the rest. ``_ensure_root_pvc`` is idempotent
        # and will detect our pre-created PVC; the Deployment, ConfigMap,
        # NetworkPolicy, and readiness wait all happen in the normal path.
        create_result = await self.create_sandbox(config)
        if not create_result.success:
            # Roll back the orphaned PVC so we don't leak storage.
            try:
                v1 = await self._get_core_v1_client()
                await v1.delete_namespaced_persistent_volume_claim(name=target_pvc, namespace=namespace)
            except Exception:
                pass
            return create_result

        if not keep_snapshot:
            try:
                await self.delete_snapshot(snapshot_name, namespace=namespace)
            except Exception as e:
                print(
                    f"⚠️  Failed to delete snapshot {snapshot_name} after restore: {e}",
                    file=sys.stderr,
                )

        return OperationResult(
            success=True,
            message=f"Sandbox {new_sandbox_name} restored from snapshot {snapshot_name}",
            data={"source_snapshot": snapshot_name, "new_sandbox_name": new_sandbox_name},
        )

    async def delete_sandbox(self, name: str, namespace: str = "default") -> OperationResult:
        """Delete a sandbox."""
        return await self._delete_sandbox_resources(name, namespace)

    async def delete_all_sandboxes(self, namespace: str = "default") -> OperationResult:
        """Delete all sandboxes in a namespace."""
        try:
            sandboxes = await self._get_kata_sandboxes(namespace)
            results = []

            for deployment in sandboxes:
                result = await self._delete_sandbox_resources(deployment.metadata.name, namespace)
                results.append(
                    {
                        "name": deployment.metadata.name,
                        "success": result.success,
                        "error": result.error if not result.success else None,
                    }
                )

            failed = [r for r in results if not r["success"]]
            if failed:
                return OperationResult(
                    success=False,
                    error=f"Failed to delete {len(failed)} sandboxes",
                    data=results,
                )

            return OperationResult(success=True, message=f"Deleted {len(results)} sandboxes", data=results)

        except Exception as e:
            return OperationResult(success=False, error=str(e))

    async def exec_command(self, sandbox_name: str, command: str, namespace: str = "default") -> ExecResult:
        """Execute a command in a sandbox and return the result."""
        start_time = time.time()

        try:
            apps_v1 = await self._get_apps_v1_client()
            v1 = await self._get_core_v1_client()

            try:
                await apps_v1.read_namespaced_deployment(name=sandbox_name, namespace=namespace)
            except ApiException as e:
                if e.status == 404:
                    raise Exception(f"Sandbox {sandbox_name} not found")
                raise Exception(f"Failed to get deployment: {e}")

            pods = await v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={sandbox_name}")

            # A Terminating pod (e.g. the old replica right after a
            # pause→resume cycle) still reports phase=Running and can keep a
            # stale Ready condition; exec'ing into it 500s or, worse, writes
            # into a dying VM's page cache that never reaches the disk.
            # Only pods without a deletionTimestamp are candidates.
            live = [p for p in pods.items if not p.metadata.deletion_timestamp]

            if not live:
                raise Exception(f"No pods found for sandbox {sandbox_name}")

            pod = live[0]
            if pod.status.phase != "Running":
                raise Exception(f"Pod is not running (status: {pod.status.phase})")

            pod_name = pod.metadata.name

            exec_cmd = ["/bin/sh", "-c", command]
            async with WsApiClient() as ws_api:
                v1_ws = client.CoreV1Api(api_client=ws_api)
                resp = await v1_ws.connect_get_namespaced_pod_exec(
                    pod_name,
                    namespace,
                    container="sandbox",
                    command=exec_cmd,  # ty: ignore[invalid-argument-type]
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )

            stdout_data = resp if isinstance(resp, str) else ""
            stderr_data = ""
            exit_code = 0

            duration_ms = int((time.time() - start_time) * 1000)

            return ExecResult(
                exit_code=exit_code,
                stdout=stdout_data,
                stderr=stderr_data,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return ExecResult(exit_code=1, stdout="", stderr=str(e), duration_ms=duration_ms)

    async def get_logs(
        self,
        sandbox_name: str,
        namespace: str = "default",
        container: str = "sandbox",
        tail_lines: int | None = 200,
        since_seconds: int | None = None,
    ) -> OperationResult:
        """Read pod logs for a sandbox (snapshot — no streaming/follow in V1).

        Resolves the pod by the ``app=<sandbox>`` label, then calls
        ``read_namespaced_pod_log``. ``OperationResult.data`` contains a
        ``{"logs": "..."}`` payload on success so the API handler can
        wrap it in the standard envelope. Follow / tail-and-stream is
        deliberately out of scope for Spec 10g; users who need it today
        can run ``k7 --core logs --follow``.
        """
        try:
            v1 = await self._get_core_v1_client()
            pods = await v1.list_namespaced_pod(namespace=namespace, label_selector=f"app={sandbox_name}")
            if not pods.items:
                return OperationResult(
                    success=False,
                    error=f"No pod found for sandbox {sandbox_name} in namespace {namespace}",
                )
            pod_name = pods.items[0].metadata.name
            kwargs: dict = {"name": pod_name, "namespace": namespace, "container": container}
            if tail_lines is not None and tail_lines > 0:
                kwargs["tail_lines"] = tail_lines
            if since_seconds is not None and since_seconds > 0:
                kwargs["since_seconds"] = since_seconds
            logs_text = await v1.read_namespaced_pod_log(**kwargs)
            return OperationResult(success=True, data={"logs": logs_text or ""})
        except ApiException as e:
            return OperationResult(success=False, error=f"Kubernetes error while reading logs: {e.reason or e}")
        except Exception as e:
            return OperationResult(success=False, error=str(e))

    async def get_sandbox_metrics(self, namespace: str | None = None) -> list[dict]:
        """Get resource usage metrics for sandboxes."""
        try:
            metrics_api = await self._get_metrics_client()
            v1 = await self._get_core_v1_client()
            sandboxes = await self._get_kata_sandboxes(namespace)

            metrics_list = []
            for deployment in sandboxes:
                sb_name = deployment.metadata.name
                sb_namespace = deployment.metadata.namespace

                try:
                    pods = await v1.list_namespaced_pod(namespace=sb_namespace, label_selector=f"app={sb_name}")
                    if not pods.items:
                        continue

                    pod = pods.items[0]
                    if pod.status.phase != "Running":
                        continue

                    pod_name = pod.metadata.name

                    metrics = await metrics_api.get_namespaced_custom_object(
                        group="metrics.k8s.io",
                        version="v1beta1",
                        namespace=sb_namespace,
                        plural="pods",
                        name=pod_name,
                    )

                    if "containers" in metrics and metrics["containers"]:
                        usage = metrics["containers"][0].get("usage", {})

                        metrics_list.append(
                            {
                                "name": sb_name,
                                "namespace": sb_namespace,
                                "cpu_usage": usage.get("cpu", "0n"),
                                "memory_usage": usage.get("memory", "0Ki"),
                            }
                        )

                except Exception:
                    continue

            return metrics_list

        except Exception:
            return []
