"""Container images ``k7 install`` and the node-installer manifests deploy.

Release builds package ``images.lock.yaml`` (image key → ``name@sha256:…``)
next to this module; the git tree carries none, so a tag never has to
contain its own digest. ``--api-image local`` / ``--node-installer-image local``
build ``k7-api:local`` / ``k7-node-installer:local`` from a checkout instead and
are unsigned. The release also attaches the k7-node-installer manifests
rendered with the locked digest.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from importlib import resources
from pathlib import Path

import yaml

LOCK_FILE = "images.lock.yaml"
API_IMAGE_KEY = "k7-api"
NODE_INSTALLER_IMAGE_KEY = "k7-node-installer"
API_IMAGE_LOCAL = "local"
LOCAL_API_IMAGE_REF = "k7-api:local"
LOCAL_NODE_INSTALLER_IMAGE_REF = "k7-node-installer:local"
DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")

# Manifest placeholder → image key in the lock file.
PLACEHOLDERS = {"__K7_API_IMAGE__": API_IMAGE_KEY, "__K7_NODE_INSTALLER_IMAGE__": NODE_INSTALLER_IMAGE_KEY}
PULL_POLICY_PLACEHOLDER = "__K7_API_IMAGE_PULL_POLICY__"
# Release asset → manifests (relative to manifests/k7-node-installer), in apply order.
NODE_INSTALLER_BUNDLES = {"k7-node-installer.yaml": "*.yaml", "k7-node-cleanup.yaml": "cleanup/*.yaml"}


def parse_lock(text: str) -> dict[str, str]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{LOCK_FILE} must be a non-empty mapping of image key to name@sha256:digest")
    for key, ref in data.items():
        if not isinstance(ref, str) or not DIGEST_REF.match(ref):
            raise ValueError(f"{LOCK_FILE}: {key!r} is not pinned by digest: {ref!r}")
    return data


def load_lock() -> dict[str, str] | None:
    """Packaged lock file, or None for a source tree / unreleased build."""
    ref = resources.files("k7.deploy").joinpath(LOCK_FILE)
    if not ref.is_file():
        return None
    return parse_lock(ref.read_text())


def resolve_image(key: str, flag: str | None) -> str:
    """``local``, an explicit digest ref, or the packaged lock entry for ``key``. Never a guess."""
    option = f"--{key.removeprefix('k7-')}-image"
    if flag == API_IMAGE_LOCAL:
        return API_IMAGE_LOCAL
    if flag:
        if not DIGEST_REF.match(flag):
            raise ValueError(f"{option} must be 'local' or name@sha256:<64 hex>, got {flag!r}")
        return flag
    lock = load_lock()
    if lock is None:
        raise LookupError(
            f"This k7 build has no packaged {LOCK_FILE} (source checkout or unreleased .deb), so there is "
            f"no signed {key} image to deploy. Pass {option} local to build {key}:local from this "
            f"checkout (unsigned), or {option} ghcr.io/katakate/{key}@sha256:<digest>."
        )
    if key not in lock:
        raise LookupError(f"{LOCK_FILE} has no {key!r} entry")
    return lock[key]


def resolve_api_image(flag: str | None) -> str:
    return resolve_image(API_IMAGE_KEY, flag)


def api_image_ref_and_policy(api_image: str) -> tuple[str, str]:
    if api_image == API_IMAGE_LOCAL:
        return LOCAL_API_IMAGE_REF, "Never"
    return api_image, "IfNotPresent"


# Pod-template hash of "no OIDC settings" (chart and playbook agree on it).
OIDC_OFF_HASH = hashlib.sha256(b"").hexdigest()[:16]


def render_manifests(
    src: Path, dst: Path, api_image: str, containerd_socket: str, api_mode: str, metering_json: str
) -> None:
    """Copy the k7-api manifests to ``dst`` with every ``__K7_*`` placeholder filled in.

    No TLS overlay is applied here, so the transport is plain HTTP (which
    production mode refuses at startup). Remote install is rendered off: only
    ``k7 install --api-remote-install`` deploys its stack. ``metering_json`` is
    the k7-metering ConfigMap's ``metering.json``; its hash matches the playbook's.
    """
    ref, policy = api_image_ref_and_policy(api_image)
    subs = {
        PULL_POLICY_PLACEHOLDER: policy,
        "__K7_API_IMAGE__": ref,
        "__K7_API_MODE__": api_mode,
        "__K7_API_TRANSPORT__": "insecure-http",
        "__K7_CONTAINERD_SOCKET_DIR__": str(Path(containerd_socket).parent),
        "__K7_CONTAINERD_SOCKET__": containerd_socket,
        # Pods still read any existing k7-oidc ConfigMap; the hash only decides rollouts.
        "__K7_OIDC_CONFIG_HASH__": OIDC_OFF_HASH,
        "__K7_METERING_CONFIG_HASH__": hashlib.sha256(metering_json.encode()).hexdigest()[:16],
        "__K7_API_REMOTE_INSTALL__": "0",
        "__K7_NODE_INSTALLER_IMAGE__": "",
        "__K7_NODE_INSTALLER_NAMESPACE__": "",
    }
    shutil.copytree(src, dst, dirs_exist_ok=True)
    for f in dst.rglob("*.yaml"):
        text = f.read_text()
        for placeholder, value in subs.items():
            text = text.replace(placeholder, value)
        if "__K7_" in text:
            raise ValueError(f"{f.name}: unrendered __K7_ placeholder")
        f.write_text(text)


def render_node_installer_bundles(src: Path, dst: Path, lock: dict[str, str]) -> list[Path]:
    """Write one ``kubectl apply -f`` file per NODE_INSTALLER_BUNDLES entry, image by locked digest."""
    ref = lock[NODE_INSTALLER_IMAGE_KEY]
    out = []
    for name, pattern in NODE_INSTALLER_BUNDLES.items():
        text = "---\n".join(f.read_text() for f in sorted(src.glob(pattern)))
        text = text.replace("__K7_NODE_INSTALLER_IMAGE__", ref)
        if "__K7_" in text or ref not in text:
            raise ValueError(f"{name}: unrendered __K7_ placeholder or no installer image")
        out.append(dst / name)
        out[-1].write_text(text)
    return out
