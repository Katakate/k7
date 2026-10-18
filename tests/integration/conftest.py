"""Integration test fixtures — requires a live k7 node."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from k7.core.core import K7Core

from .cluster import API_KEYS_AT_START, HAS_CLUSTER, KUBECTL, NAMESPACES_AT_START, api_keys, namespaces

_DEV_SH = Path(__file__).resolve().parents[2] / "src" / "k7" / "cli" / "dev.sh"


def _strip_finalizers(kind: str, name: str, namespace: str | None = None) -> None:
    # Merge-patch is what actually clears CSI/Longhorn finalizers on these CRs.
    cmd = [
        *KUBECTL,
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
        [*KUBECTL, "get", "volumesnapshotcontent", "-o", "json"],
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


# Upper bound for a namespace's pods, PVs and snapshot contents to be released.
# A sandbox pod alone can take its 30 s default grace period to stop.
_TEARDOWN_TIMEOUT = 300


def _namespace_storage(ns: str) -> list[str]:
    """Cluster-scoped storage that outlives ``ns``: its PVs and VolumeSnapshotContents."""
    raw = subprocess.run([*KUBECTL, "get", "pv", "-o", "json"], capture_output=True, text=True, check=True)
    pvs = [
        f"pv/{pv['metadata']['name']}"
        for pv in json.loads(raw.stdout).get("items") or []
        if ((pv.get("spec") or {}).get("claimRef") or {}).get("namespace") == ns
    ]
    return pvs + [f"volumesnapshotcontent/{n}" for n in _vsc_names_for_namespace(ns)]


def _teardown_namespace(ns: str) -> None:
    """Delete ``ns`` and block until everything it held is actually released.

    Deletes are asynchronous. Without this barrier the next test starts while
    the previous test's Kata VMs are still stopping and its Longhorn volumes
    and snapshots are still being deleted, so a test's speed depends on what
    ran before it. Resources are released through their own finalizers
    (pods stop, CSI deletes the volumes and snapshots), never stripped. If they
    don't converge in time, the leftovers are force-cleaned so later tests are
    not polluted, and the owning test fails loudly.
    """
    gc = subprocess.run(
        [str(_DEV_SH), "--core", "snapshot", "gc", "-n", ns, "--keep-fork-for=0s"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if gc.returncode != 0:
        print(f"gc_snapshots teardown failed: {gc.stderr or gc.stdout}", file=sys.stderr)

    targets = [f"namespace/{ns}", *_namespace_storage(ns)]
    subprocess.run([*KUBECTL, "delete", "namespace", ns, "--wait=false"], capture_output=True, check=True)
    t0 = time.time()
    wait = subprocess.run(
        [*KUBECTL, "wait", "--for=delete", *targets, f"--timeout={_TEARDOWN_TIMEOUT}s"],
        capture_output=True,
        text=True,
    )
    print(f"[teardown] {ns}: {len(targets)} object(s) released in {time.time() - t0:.1f}s", file=sys.stderr)
    if wait.returncode == 0:
        return
    # Finalizers name the controller that did not let go; the force-clean below erases them.
    cols = "KIND:.kind,NAME:.metadata.name,DELETING:.metadata.deletionTimestamp,FINALIZERS:.metadata.finalizers"
    left = "".join(
        subprocess.run(
            [*KUBECTL, "get", *objs, "--ignore-not-found", "-o", f"custom-columns={cols}"],
            capture_output=True,
            text=True,
        ).stdout
        for objs in (["pods,pvc,volumesnapshot", "-n", ns], targets[1:])
        if objs
    )
    _force_delete_namespace(ns)
    pytest.fail(
        f"namespace {ns} not released within {_TEARDOWN_TIMEOUT}s: {wait.stderr.strip()}\n{left}", pytrace=False
    )


def _force_delete_namespace(ns: str) -> None:
    """Last resort: strip CSI/Longhorn finalizers so a stuck namespace goes away."""
    snaps = subprocess.run(
        [*KUBECTL, "get", "volumesnapshot", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True,
        text=True,
    )
    for snap in snaps.stdout.split():
        _strip_finalizers("volumesnapshot", snap, ns)
        subprocess.run(
            [*KUBECTL, "delete", "volumesnapshot", snap, "-n", ns, "--ignore-not-found", "--wait=false"],
            capture_output=True,
        )
    for vsc in _vsc_names_for_namespace(ns):
        _strip_finalizers("volumesnapshotcontent", vsc)
        subprocess.run(
            [*KUBECTL, "delete", "volumesnapshotcontent", vsc, "--ignore-not-found", "--wait=false"],
            capture_output=True,
        )
    pvcs = subprocess.run(
        [*KUBECTL, "get", "pvc", "-n", ns, "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True,
        text=True,
    )
    for pvc in pvcs.stdout.split():
        _strip_finalizers("pvc", pvc, ns)
        subprocess.run(
            [*KUBECTL, "delete", "pvc", pvc, "-n", ns, "--ignore-not-found", "--wait=false"],
            capture_output=True,
        )
    subprocess.run(
        [*KUBECTL, "delete", "namespace", ns, "--ignore-not-found", "--wait=false"],
        capture_output=True,
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Destructive tests take the stack down; they run only when asked for."""
    if os.environ.get("K7_DESTRUCTIVE") == "1":
        return
    skip = pytest.mark.skip(reason="destructive: set K7_DESTRUCTIVE=1 and run -m destructive")
    for item in items:
        if "destructive" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _record_namespaces_at_start() -> None:
    if HAS_CLUSTER:
        NAMESPACES_AT_START.update(namespaces())
        API_KEYS_AT_START.update(api_keys())


@pytest.fixture()
def k7_core() -> K7Core:
    return K7Core()


@pytest.fixture()
def make_namespace():
    """Create extra uniquely named test namespaces; each is torn down like ``test_namespace``."""
    made: list[str] = []

    def _make(suffix: str) -> str:
        ns = f"k7-test-{uuid4().hex[:8]}-{suffix}"
        subprocess.run([*KUBECTL, "create", "namespace", ns], check=True, capture_output=True)
        made.append(ns)
        return ns

    try:
        yield _make
    finally:
        failures = []
        for ns in made:
            try:
                _teardown_namespace(ns)
            except pytest.fail.Exception as e:
                failures.append(str(e))
        if failures:
            pytest.fail("\n".join(failures), pytrace=False)


@pytest.fixture()
def test_namespace() -> str:
    ns = f"k7-test-{uuid4().hex[:8]}"
    subprocess.run(
        [*KUBECTL, "create", "namespace", ns],
        check=True,
        capture_output=True,
    )
    try:
        yield ns
    finally:
        _teardown_namespace(ns)
