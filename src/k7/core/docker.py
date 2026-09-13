"""User-facing Docker pins and --docker helpers.

k7d's source of truth is ``guest/docker/pins.env`` in the pinned k7d
tree. The constants below are what k7 displays and what the Kata
vehicle consumes. ``tests/unit/test_docker_pins.py`` fails loud if they
drift from the checked-in snapshot of that file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# User-facing pins. Bump as a set with k7d's guest/docker/pins.env.
DOCKER_VERSION = "27.5.1"
COMPOSE_VERSION = "2.32.4"
BUILDX_VERSION = "0.20.0"
IPTABLES_VERSION = "1.8.11"
IPTABLES_BACKEND = "nft"

DEFAULT_DOCKER_DISK = "20Gi"

# Daemon args as displayed to users. k7d's agent adds data-root / exec-root
# / pidfile / host path under /run/k7d/docker/; overlay2 is mandatory.
DAEMON_ARGS = "--host=unix:///run/k7d/docker/docker.sock --tls=false --storage-driver=overlay2"

CLI_PATH = "/usr/local/bin/docker"
CLI_PLUGINS_DIR = "/usr/local/lib/docker/cli-plugins"
# Per-pod emptyDir staging; kql persist-bind overlays /usr, so the sandbox
# also mounts this path (not under /usr) and restages the canonical CLI.
CLI_STAGING_DIR = "/run/k7/docker-cli"

# Kata vehicle: directory socket, never /var/run or /run/k7d.
KATA_SOCKET_DIR = "/run/k7/docker"
DOCKER_HOST_URL = "unix:///run/k7/docker/docker.sock"
KATA_DAEMON_ARGS = "--host=unix:///run/k7/docker/docker.sock --tls=false --storage-driver=overlay2"
GRAPH_DEVICE_PATH = "/dev/k7docker"
VEHICLE_CONTAINER_NAME = "docker-vehicle"
KFD_DOCKER_STORAGE_CLASS = "k7-docker-lvm"
# linux/amd64 digest of docker:27.5.1-dind (Docker Hub, 2026-09).
DIND_IMAGE = "docker:27.5.1-dind@sha256:f649ef046008ca7f926a2571c32b0ac22e5c59eb61b959617f9acc2a4c638cf5"
PATH_SHARE_DIRS = ("/home", "/root", "/tmp", "/opt", "/workspace")

# Pod-template annotations the k7d shim reads.
ANN_K7D_DOCKER = "k7d.katakate.org/docker"
ANN_K7D_DOCKER_DISK = "k7d.katakate.org/docker-disk"
# Deployment bookkeeping for k7 (not a sidecar).
ANN_K7_DOCKER = "k7.katakate.org/docker"
ANN_DOCKER_PVC = "k7.katakate.org/docker-pvc-name"

KATA_FORK_GRAPH_REJECT = (
    "k7 fork of a kfd --docker sandbox is not supported: the docker graph cannot be cloned (ephemeral LV)"
)

# Playbook records the installed k7d version here. Guest dockerd
# and k7-fc shipped in public k7d 0.6.0; a node whose recorded version
# is older, or that has no docker payload, fails loud.
K7D_VERSION_FILE = "/etc/k7/k7d_version"
K7D_DOCKER_MIN_VERSION = (0, 6, 0)
K7D_DOCKER_PAYLOAD_DOCKERD = (
    "/usr/local/share/k7d/docker/bin/dockerd",
    "/root/k7d/guest/docker/payload/bin/dockerd",
)
DOCKER_UNSUPPORTED_K7D = "this k7d has no docker service; upgrade"

_SIZE_RE = re.compile(r"^[0-9]+(?:[KMGT]i)?$")


def parse_docker_disk(raw: str) -> str:
    """Validate a ``--docker-disk`` quantity. Loud on garbage."""
    value = (raw or "").strip()
    if not value or not _SIZE_RE.match(value):
        raise ValueError(f"invalid --docker-disk {raw!r}: use a Kubernetes quantity like 20Gi or 40Gi")
    return value


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    text = raw.strip()
    if text.lower().startswith("k7d "):
        text = text[4:].strip()
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2].split("-")[0].split("+")[0]) if len(parts) > 2 else 0
    except ValueError:
        return None
    return major, minor, patch


def recorded_k7d_version(version_file: str = K7D_VERSION_FILE) -> str | None:
    """Return the k7d version the playbook recorded, or None if missing."""
    try:
        text = Path(version_file).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return text or None


def k7d_docker_payload_present() -> bool:
    return any(os.path.isfile(p) for p in K7D_DOCKER_PAYLOAD_DOCKERD)


def k7d_supports_docker(version_file: str = K7D_VERSION_FILE) -> bool:
    """True when the installed k7d can run ``--docker``.

    Detects via the version the playbook records (and the docker payload
    the shim execs). Does not create a VM and wait for a timeout.
    """
    recorded = recorded_k7d_version(version_file)
    if recorded:
        parsed = _parse_version(recorded)
        if parsed is not None and parsed < K7D_DOCKER_MIN_VERSION:
            return False
    return k7d_docker_payload_present()


def pins_from_env_text(text: str) -> dict[str, str]:
    """Parse a k7d ``guest/docker/pins.env`` body into name → value."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def user_facing_pins() -> dict[str, str]:
    return {
        "DOCKER_VERSION": DOCKER_VERSION,
        "COMPOSE_VERSION": COMPOSE_VERSION,
        "BUILDX_VERSION": BUILDX_VERSION,
        "IPTABLES_VERSION": IPTABLES_VERSION,
        "IPTABLES_BACKEND": IPTABLES_BACKEND,
    }
