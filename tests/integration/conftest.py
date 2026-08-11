"""Integration test fixtures — requires a live k7 node."""

import subprocess
from uuid import uuid4

import pytest

from k7.core.core import K7Core

_K3S = "/usr/local/bin/k3s"


def _strip_finalizers(kind: str, name: str, namespace: str | None = None) -> None:
    cmd = [_K3S, "kubectl", "patch", kind, name, "--type=json", "-p", '[{"op":"remove","path":"/metadata/finalizers"}]']
    if namespace:
        cmd.extend(["-n", namespace])
    subprocess.run(cmd, capture_output=True)


def _force_delete_namespace(ns: str) -> None:
    """Delete a test namespace without blocking on stuck CSI/Longhorn finalizers."""
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
    vscs = subprocess.run(
        [
            _K3S,
            "kubectl",
            "get",
            "volumesnapshotcontent",
            "-o",
            f"jsonpath={{.items[?(@.spec.volumeSnapshotRef.namespace=='{ns}')].metadata.name}}",
        ],
        capture_output=True,
        text=True,
    )
    for vsc in vscs.stdout.split():
        _strip_finalizers("volumesnapshotcontent", vsc)
        subprocess.run(
            [_K3S, "kubectl", "delete", "volumesnapshotcontent", vsc, "--ignore-not-found", "--wait=false"],
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
