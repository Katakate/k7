import shutil
import subprocess

import pytest


def _detect_backends() -> set[str]:
    """Return the set of backends supported by the live cluster.

    Sources, in order of preference:
    1. `k7.katakate.org/backend-<name>=true` labels on the cluster's nodes.
    2. `/etc/k7/backend` (legacy, single primary backend) — fallback for
       hosts where k3s isn't reachable.
    """
    if shutil.which("k3s"):
        try:
            result = subprocess.run(
                [
                    "k3s",
                    "kubectl",
                    "get",
                    "nodes",
                    "-o",
                    'jsonpath={range .items[*]}{.metadata.labels}{"\\n"}{end}',
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            legacy = {
                "firecracker-devmapper": "kata-firecracker-devmapper",
                "qemu-longhorn": "kata-qemu-longhorn",
            }
            backends: set[str] = set()
            for line in result.stdout.splitlines():
                for token in (
                    "kata-firecracker-devmapper",
                    "kata-qemu-longhorn",
                    "k7d",
                    "firecracker-devmapper",
                    "qemu-longhorn",
                ):
                    if f'"k7.katakate.org/backend-{token}":"true"' in line:
                        backends.add(legacy.get(token, token))
            if backends:
                return backends

    try:
        with open("/etc/k7/backend") as f:
            backend = f.read().strip()
            legacy = {
                "firecracker-devmapper": "kata-firecracker-devmapper",
                "qemu-longhorn": "kata-qemu-longhorn",
            }
            backend = legacy.get(backend, backend)
            if backend in ("kata-firecracker-devmapper", "kata-qemu-longhorn", "k7d"):
                return {backend}
    except FileNotFoundError:
        pass
    return {"kata-firecracker-devmapper"}


def _detect_node_count() -> int:
    """Count Ready k3s nodes; 0 when k3s is unavailable (e.g. local Mac)."""
    if not shutil.which("k3s"):
        return 0
    try:
        result = subprocess.run(
            ["k3s", "kubectl", "get", "nodes", "--no-headers"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if result.returncode != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if " Ready " in f" {line} ")


BACKENDS = _detect_backends()
NODE_COUNT = _detect_node_count()


@pytest.fixture()
def node_count() -> int:
    """Number of Ready nodes in the live k3s cluster."""
    return NODE_COUNT


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_firecracker = pytest.mark.skip(reason="cluster has no kata-firecracker-devmapper-capable node")
    skip_qemu = pytest.mark.skip(reason="cluster has no kata-qemu-longhorn-capable node")
    skip_k7d = pytest.mark.skip(reason="cluster has no k7d-capable node")
    skip_multinode = pytest.mark.skip(reason=f"requires >=2 Ready nodes (have {NODE_COUNT})")

    for item in items:
        if "firecracker" in item.keywords and "kata-firecracker-devmapper" not in BACKENDS:
            item.add_marker(skip_firecracker)
        if "qemu" in item.keywords and "kata-qemu-longhorn" not in BACKENDS:
            item.add_marker(skip_qemu)
        if "k7d" in item.keywords and "k7d" not in BACKENDS:
            item.add_marker(skip_k7d)
        if "multinode" in item.keywords and NODE_COUNT < 2:
            item.add_marker(skip_multinode)
