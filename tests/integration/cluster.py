"""How the suite reaches the cluster under test: the K3s playbook node, or a
chart install on another distro.

- ``HAS_CLUSTER``: a K3s node or ``K7_KUBECONFIG`` (False on a dev laptop).
- ``KUBECTL``: ``kubectl --kubeconfig=$K7_KUBECONFIG`` when set, else ``k3s kubectl``.
- ``system_ns()``: where k7-api, k7-agent and the key Secrets live
  (``K7_SYSTEM_NAMESPACE`` or ``/etc/k7/config``; kube-system on the playbook).
- ``API_CA``: CA bundle of the k7-api certificate (``K7_API_CA`` or the playbook's).
"""

from __future__ import annotations

import base64
import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

from k7.core import cluster_config

KUBECTL = cluster_config.kubectl_cmd()
HAS_CLUSTER = bool(os.environ.get("K7_KUBECONFIG")) or shutil.which("k3s") is not None
KUBECONFIG = os.environ.get("K7_KUBECONFIG") or "/etc/rancher/k3s/k3s.yaml"
API_CA = Path(os.environ.get("K7_API_CA") or "/etc/k7/tls/ca.crt")


@functools.cache
def system_ns() -> str:
    return cluster_config.system_namespace()


def kubectl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*KUBECTL, *args], capture_output=True, text=True, check=check)


# Filled once per session by conftest; test_zz_leaks fails on anything the run added and kept.
NAMESPACES_AT_START: set[str] = set()
API_KEYS_AT_START: dict[str, str] = {}


def namespaces() -> set[str]:
    out = kubectl("get", "namespaces", "-o", "jsonpath={.items[*].metadata.name}", check=True).stdout
    return set(out.split())


def api_keys() -> dict[str, str]:
    """``{key id: name}`` in the k7-api-keys Secret ({} before k7-api is installed)."""
    from k7.deploy.k7_api_keys import KEYS_SECRET, Kubectl, decode_keys

    keys = decode_keys(Kubectl(KUBECTL, system_ns()).read(KEYS_SECRET) or {})
    return {key_id: str(entry.get("name")) for key_id, entry in keys.items()}


def server_version() -> tuple[int, int]:
    v = json.loads(kubectl("version", "-o", "json", check=True).stdout)["serverVersion"]
    return int(v["major"]), int(v["minor"].rstrip("+"))


def bound_token_kubeconfig(pod: dict, path: Path) -> Path:
    """Write a kubeconfig that authenticates as ``pod``'s ServiceAccount with a token
    bound to that pod. From Kubernetes 1.32 the token carries the pod's node name,
    which is what the K7 admission policies compare against."""
    token = kubectl(
        "create",
        "token",
        pod["spec"]["serviceAccountName"],
        "-n",
        pod["metadata"]["namespace"],
        "--bound-object-kind=Pod",
        f"--bound-object-name={pod['metadata']['name']}",
        "--duration=10m",
        check=True,
    ).stdout.strip()
    admin = json.loads(kubectl("config", "view", "--raw", "--minify", "-o", "json", check=True).stdout)
    cfg = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c", "cluster": admin["clusters"][0]["cluster"]}],
        "users": [{"name": "sa", "user": {"token": token}}],
        "contexts": [{"name": "sa", "context": {"cluster": "c", "user": "sa"}}],
        "current-context": "sa",
    }
    path.write_text(yaml.safe_dump(cfg))
    path.chmod(0o600)
    return path


def as_token(cfg: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run kubectl ``args`` as ``cfg``'s identity with ``--dry-run=server``: admission runs, nothing persists."""
    # The last --kubeconfig wins over the one KUBECTL may already carry.
    return subprocess.run([*KUBECTL, f"--kubeconfig={cfg}", *args, "--dry-run=server"], capture_output=True, text=True)


def agent_token() -> str:
    """The shared k7-agent token, from Secret k7-agent-token (no host file on a chart cluster)."""
    out = kubectl(
        "-n", system_ns(), "get", "secret", "k7-agent-token", "-o", "jsonpath={.data.agent_token}", check=True
    )
    token = base64.b64decode(out.stdout).decode().strip()
    assert token, f"Secret {system_ns()}/k7-agent-token has an empty agent_token"
    return token
