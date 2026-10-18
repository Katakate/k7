"""Cluster-level settings: env in pods, ``/etc/k7/config`` on playbook nodes.

The k7-api, k7-agent and snapshot-gc pods get every setting as env (the
playbook manifests and the Helm chart set them). Host-side callers (`k7
--core`, integration tests) read ``KEY=value`` lines from ``/etc/k7/config``,
which `k7 install` writes. A setting found in neither is a loud error, never a
guess: a chart cluster has no ``kube-system`` K7 objects and no ``longhorn``
StorageClass.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CONFIG_FILE = Path("/etc/k7/config")

SYSTEM_NAMESPACE = "K7_SYSTEM_NAMESPACE"
EGRESS_MODE = "K7_EGRESS_MODE"
KQL_STORAGE_CLASS = "K7_KQL_STORAGE_CLASS"
KQL_SNAPSHOT_CLASS = "K7_KQL_SNAPSHOT_CLASS"
API_MODE = "K7_API_MODE"
API_MODES = ("production", "development")

# full: Cilium FQDN + CIDR policies. cidr-only: v1 NetworkPolicy, no FQDN
# (chart networkPolicy.provider=kubernetes). none: no egress policy at all.
EGRESS_MODES = ("full", "cidr-only", "none")


def _read_config_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, sep, value = line.partition("=")
            if not sep:
                raise RuntimeError(f"{path}: malformed line {line!r}; expected KEY=value")
            out[key.strip()] = value.strip()
    return out


def setting(key: str, config_file: Path = CONFIG_FILE) -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    if config_file.is_file():
        value = _read_config_file(config_file).get(key, "")
        if value:
            return value
    raise RuntimeError(
        f"{key} is not set: pods get it from the k7 manifests or Helm chart; on a node "
        f"`k7 install` writes it to {config_file} (re-run it after upgrading), or export {key}."
    )


def system_namespace() -> str:
    """Namespace of k7-api, k7-agent and the key-store Secrets."""
    return setting(SYSTEM_NAMESPACE)


def egress_mode() -> str:
    mode = setting(EGRESS_MODE)
    if mode not in EGRESS_MODES:
        raise RuntimeError(f"{EGRESS_MODE}={mode!r}; allowed: {', '.join(EGRESS_MODES)}")
    return mode


def api_mode() -> str:
    """``production`` or ``development``; chosen at install, never defaulted."""
    mode = setting(API_MODE)
    if mode not in API_MODES:
        raise RuntimeError(f"{API_MODE}={mode!r}; allowed: {', '.join(API_MODES)}")
    return mode


def egress_mode_error(entries: list[str] | None, fqdns: list[str]) -> str | None:
    """Create-time refusal for egress the cluster cannot enforce, else None."""
    if entries is None:
        return None
    mode = egress_mode()
    if mode == "none":
        return (
            f"egress rules cannot be enforced on this cluster ({EGRESS_MODE}=none: the k7 chart runs "
            "with networkPolicy.provider=none). Omit egress, or install with "
            "networkPolicy.provider=kubernetes (CIDR only) or cilium."
        )
    if mode == "cidr-only" and fqdns:
        return (
            f"Domain-based egress ({', '.join(fqdns)}) cannot be enforced on this cluster "
            f"({EGRESS_MODE}=cidr-only: the k7 chart runs with networkPolicy.provider=kubernetes). "
            "Use CIDR entries, or install the chart with networkPolicy.provider=cilium."
        )
    return None


def kql_storage_class() -> str:
    return setting(KQL_STORAGE_CLASS)


def kql_snapshot_class() -> str:
    return setting(KQL_SNAPSHOT_CLASS)


def kubectl_cmd() -> list[str]:
    """kubectl prefix for host-side callers: ``K7_KUBECONFIG`` if set, else the node's K3s."""
    kubeconfig = os.environ.get("K7_KUBECONFIG")
    if kubeconfig:
        return ["kubectl", f"--kubeconfig={kubeconfig}"]
    return ["k3s", "kubectl"] if shutil.which("k3s") else ["kubectl"]
