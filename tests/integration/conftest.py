"""Integration test fixtures — requires a live k7 node."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from k7.core.core import K7Core

_K3S = "/usr/local/bin/k3s"
_DEV_SH = Path(__file__).resolve().parents[2] / "src" / "k7" / "cli" / "dev.sh"


def _strip_finalizers(kind: str, name: str, namespace: str | None = None) -> None:
    # Merge-patch is what actually clears CSI/Longhorn finalizers on these CRs.
    cmd = [
        _K3S,
        "kubectl",
        "patch",
        kind,
        name,
        "--type=merge",
        "-p",
        '{"metadata":{"finalizers":[]}}',
    ]
    if namespace:
        cmd.extend(["-n", namespace])
    subprocess.run(cmd, capture_output=True)


def _vsc_names_for_namespace(ns: str) -> list[str]:
    """List VolumeSnapshotContents that point at ``ns``.

    kubectl jsonpath filters on ``volumeSnapshotRef.namespace`` silently
    miss items; parse the JSON list instead.
    """
    raw = subprocess.run(
        [_K3S, "kubectl", "get", "volumesnapshotcontent", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if raw.returncode != 0 or not raw.stdout.strip():
        return []
    try:
        items = json.loads(raw.stdout).get("items") or []
    except json.JSONDecodeError:
        return []
    names: list[str] = []
    for item in items:
        ref = (item.get("spec") or {}).get("volumeSnapshotRef") or {}
        if ref.get("namespace") == ns:
            name = (item.get("metadata") or {}).get("name")
            if name:
                names.append(name)
    return names


def _force_delete_namespace(ns: str) -> None:
    """Delete a test namespace without blocking on stuck CSI/Longhorn finalizers.

    Prefer the product GC (orphan VolumeSnapshotContents + stuck PVC
    source-protection) so tests exercise the same path as the CronJob.
    kubectl leftover stripping is a fallback when the API is unhappy.
    """
    gc = subprocess.run(
        [str(_DEV_SH), "--core", "snapshot", "gc", "-n", ns, "--keep-fork-for=0s"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if gc.returncode != 0:
        print(f"gc_snapshots teardown failed: {gc.stderr or gc.stdout}", file=sys.stderr)

    snaps = subprocess.run(
        [_K3S, "kubectl", "get", "volumesnapshot", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True,
        text=True,
    )
    for snap in snaps.stdout.split():
        _strip_finalizers("volumesnapshot", snap, ns)
        subprocess.run(
            [_K3S, "kubectl", "delete", "volumesnapshot", snap, "-n", ns, "--ignore-not-found", "--wait=false"],
            capture_output=True,
        )
    for vsc in _vsc_names_for_namespace(ns):
        _strip_finalizers("volumesnapshotcontent", vsc)
        subprocess.run(
            [_K3S, "kubectl", "delete", "volumesnapshotcontent", vsc, "--ignore-not-found", "--wait=false"],
            capture_output=True,
        )
    pvcs = subprocess.run(
        [_K3S, "kubectl", "get", "pvc", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True,
        text=True,
    )
    for pvc in pvcs.stdout.split():
        _strip_finalizers("pvc", pvc, ns)
        subprocess.run(
            [_K3S, "kubectl", "delete", "pvc", pvc, "-n", ns, "--ignore-not-found", "--wait=false"],
            capture_output=True,
        )
    subprocess.run(
        [_K3S, "kubectl", "delete", "namespace", ns, "--ignore-not-found", "--wait=false"],
        capture_output=True,
    )


@pytest.fixture()
def k7_core() -> K7Core:
    return K7Core()


@pytest.fixture()
def test_namespace() -> str:
    ns = f"k7-test-{uuid4().hex[:8]}"
    subprocess.run(
        [_K3S, "kubectl", "create", "namespace", ns],
        check=True,
        capture_output=True,
    )
    try:
        yield ns
    finally:
        _force_delete_namespace(ns)
