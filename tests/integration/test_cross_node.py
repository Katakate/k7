"""Cross-node Longhorn replication integration tests (spec 3c).

These tests require a multi-node k7 cluster with topology affinity (specs 3a + 3b)
and Longhorn replicaCount >= 2. They are skipped on single-node setups.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Iterator

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = [pytest.mark.integration, pytest.mark.qemu, pytest.mark.multinode]

_K3S = "/usr/local/bin/k3s"


def _kubectl(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_K3S, "kubectl", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _wait_pod_ready(sandbox_name: str, namespace: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _kubectl(
            [
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                f"app={sandbox_name}",
                "-o",
                "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}",
            ],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "True":
            return
        time.sleep(2)
    raise TimeoutError(f"Pod for {sandbox_name} not Ready within {timeout}s")


def _pod_node(sandbox_name: str, namespace: str) -> str:
    result = _kubectl(
        [
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={sandbox_name}",
            "-o",
            "jsonpath={.items[0].spec.nodeName}",
        ]
    )
    node = result.stdout.strip()
    assert node, f"could not determine node for sandbox {sandbox_name}"
    return node


def _pvc_volume_name(pvc_name: str, namespace: str, timeout: int = 120) -> str:
    """Return the bound PV (Longhorn volume) name for a PVC. Waits until bound."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _kubectl(
            ["get", "pvc", pvc_name, "-n", namespace, "-o", "jsonpath={.spec.volumeName}"],
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        time.sleep(2)
    raise TimeoutError(f"PVC {pvc_name} did not bind to a PV within {timeout}s")


def _list_replicas_for_volume(volume_name: str) -> list[dict]:
    result = _kubectl(
        ["get", "replicas.longhorn.io", "-n", "longhorn-system", "-o", "json"],
    )
    items = json.loads(result.stdout).get("items", [])
    return [r for r in items if (r.get("spec") or {}).get("volumeName") == volume_name]


def _list_all_volumes() -> set[str]:
    result = _kubectl(
        [
            "get",
            "volumes.longhorn.io",
            "-n",
            "longhorn-system",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
        check=False,
    )
    return set(result.stdout.split()) if result.returncode == 0 and result.stdout.strip() else set()


def _list_all_replicas() -> list[dict]:
    result = _kubectl(
        ["get", "replicas.longhorn.io", "-n", "longhorn-system", "-o", "json"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return json.loads(result.stdout).get("items", [])


def _wait_replicas_on_distinct_nodes(volume_name: str, min_nodes: int = 2, timeout: int = 180) -> set[str]:
    """Poll until the Longhorn volume has replicas on at least `min_nodes` distinct nodes."""
    deadline = time.time() + timeout
    nodes: set[str] = set()
    while time.time() < deadline:
        replicas = _list_replicas_for_volume(volume_name)
        nodes = {(r.get("spec") or {}).get("nodeID") for r in replicas if (r.get("spec") or {}).get("nodeID")}
        if len(nodes) >= min_nodes:
            return nodes
        time.sleep(3)
    raise AssertionError(
        f"Volume {volume_name} did not get replicas on {min_nodes}+ nodes within {timeout}s; nodes seen: {nodes}"
    )


@pytest.fixture()
def cordoned_node() -> Iterator[list[str]]:
    """Track cordoned nodes; always uncordon on teardown."""
    cordoned: list[str] = []
    try:
        yield cordoned
    finally:
        for node in cordoned:
            subprocess.run(
                [_K3S, "kubectl", "uncordon", node],
                capture_output=True,
                text=True,
                check=False,
            )


class TestCrossNodeReplication:
    async def test_replica_spread(self, k7_core: K7Core, test_namespace: str) -> None:
        """Replicas of a sandbox volume must land on >=2 distinct nodes."""
        name = "integ-xn-spread"
        cfg = SandboxConfig(
            name=name,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(name, test_namespace)
            await k7_core.exec_command(name, "echo crossnode > /mnt/state/marker", namespace=test_namespace)

            pvc_name = k7_core._root_pvc_name(name)
            volume_name = _pvc_volume_name(pvc_name, test_namespace)
            nodes = _wait_replicas_on_distinct_nodes(volume_name, min_nodes=2)
            print(f"[replicas] volume {volume_name} replicas on nodes: {sorted(nodes)}")
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_data_survives_pod_migration(
        self,
        k7_core: K7Core,
        test_namespace: str,
        cordoned_node: list[str],
        node_count: int,
    ) -> None:
        """Cordoning the pod's node and deleting the pod reschedules elsewhere with data intact."""
        if node_count < 2:
            pytest.skip("requires >=2 nodes")
        name = "integ-xn-migrate"
        cfg = SandboxConfig(
            name=name,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_pod_ready(name, test_namespace)
            await k7_core.exec_command(name, "echo migrate-marker > /mnt/state/migrate-test", namespace=test_namespace)

            original_node = _pod_node(name, test_namespace)
            print(f"[migrate] original pod node: {original_node}")

            volume_name = _pvc_volume_name(k7_core._root_pvc_name(name), test_namespace)
            _wait_replicas_on_distinct_nodes(volume_name, min_nodes=2)

            _kubectl(["cordon", original_node])
            cordoned_node.append(original_node)

            pod_name = _kubectl(
                [
                    "get",
                    "pods",
                    "-n",
                    test_namespace,
                    "-l",
                    f"app={name}",
                    "-o",
                    "jsonpath={.items[0].metadata.name}",
                ]
            ).stdout.strip()
            assert pod_name, "could not determine pod name"
            _kubectl(["delete", "pod", pod_name, "-n", test_namespace, "--wait=false"])

            deadline = time.time() + 300
            new_node = ""
            while time.time() < deadline:
                node_result = _kubectl(
                    [
                        "get",
                        "pods",
                        "-n",
                        test_namespace,
                        "-l",
                        f"app={name}",
                        "-o",
                        "jsonpath={.items[0].spec.nodeName}",
                    ],
                    check=False,
                )
                candidate = node_result.stdout.strip()
                if candidate and candidate != original_node:
                    new_node = candidate
                    break
                time.sleep(3)
            assert new_node and new_node != original_node, (
                f"pod did not reschedule to a different node (still on {new_node or original_node})"
            )
            print(f"[migrate] pod rescheduled to: {new_node}")

            _wait_pod_ready(name, test_namespace, timeout=300)

            exec_result = await k7_core.exec_command(name, "cat /mnt/state/migrate-test", namespace=test_namespace)
            assert exec_result.exit_code == 0, f"exec failed after migration: {exec_result.stderr}"
            assert "migrate-marker" in exec_result.stdout, f"data not preserved after migration: {exec_result.stdout!r}"
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_fork_cross_node(self, k7_core: K7Core, test_namespace: str) -> None:
        """Fork a sandbox; if the fork lands on a different node, verify data and independence."""
        source = "integ-xn-fork-src"
        fork_name = "integ-xn-fork-dst"
        cfg = SandboxConfig(
            name=source,
            image="alpine:latest",
            namespace=test_namespace,
            backend="kata-qemu-longhorn",
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create source failed: {result.error}"

        try:
            _wait_pod_ready(source, test_namespace)
            await k7_core.exec_command(source, "echo xn-fork-marker > /mnt/state/xn-fork", namespace=test_namespace)
            source_node = _pod_node(source, test_namespace)
            print(f"[fork] source on node: {source_node}")

            fork_result = await k7_core.fork_sandbox(source, fork_name, namespace=test_namespace)
            assert fork_result.success, f"fork failed: {fork_result.error}"

            _wait_pod_ready(fork_name, test_namespace, timeout=300)
            fork_node = _pod_node(fork_name, test_namespace)
            print(f"[fork] fork on node: {fork_node}")

            read = await k7_core.exec_command(fork_name, "cat /mnt/state/xn-fork", namespace=test_namespace)
            assert read.exit_code == 0, f"exec on fork failed: {read.stderr}"
            assert "xn-fork-marker" in read.stdout, f"data not present on fork: {read.stdout!r}"

            await k7_core.exec_command(fork_name, "echo fork-write > /mnt/state/xn-fork", namespace=test_namespace)
            src_read = await k7_core.exec_command(source, "cat /mnt/state/xn-fork", namespace=test_namespace)
            assert src_read.exit_code == 0
            assert "xn-fork-marker" in src_read.stdout, f"source mutated by fork write: {src_read.stdout!r}"

            if fork_node == source_node:
                print(f"[fork] note: scheduler placed fork on same node ({source_node})")
        finally:
            await k7_core.delete_sandbox(fork_name, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_no_orphaned_replicas(self, k7_core: K7Core, test_namespace: str) -> None:
        """After create+delete cycles, every Longhorn replica must belong to an active volume."""
        names = [f"integ-xn-orphan-{i}" for i in range(2)]
        created: list[str] = []
        try:
            for name in names:
                cfg = SandboxConfig(
                    name=name,
                    image="alpine:latest",
                    namespace=test_namespace,
                    backend="kata-qemu-longhorn",
                )
                result = await k7_core.create_sandbox(cfg)
                assert result.success, f"create {name} failed: {result.error}"
                created.append(name)
                _wait_pod_ready(name, test_namespace)
        finally:
            for name in created:
                await k7_core.delete_sandbox(name, namespace=test_namespace)

        # Poll up to 90s for Longhorn GC.
        deadline = time.time() + 90
        last_orphans: list[str] = []
        last_stuck_pvcs: list[str] = []
        while time.time() < deadline:
            volumes = _list_all_volumes()
            replicas = _list_all_replicas()
            last_orphans = [
                r["metadata"]["name"] for r in replicas if (r.get("spec") or {}).get("volumeName") not in volumes
            ]

            pvc_result = _kubectl(
                [
                    "get",
                    "pvc",
                    "-n",
                    test_namespace,
                    "-o",
                    "jsonpath={range .items[*]}{.metadata.name}={.status.phase} {end}",
                ],
                check=False,
            )
            last_stuck_pvcs = []
            if pvc_result.stdout.strip():
                for entry in pvc_result.stdout.strip().split():
                    if "=" not in entry:
                        continue
                    pvc_name, phase = entry.rsplit("=", 1)
                    if phase in ("Pending", "Terminating"):
                        last_stuck_pvcs.append(f"{pvc_name}={phase}")

            if not last_orphans and not last_stuck_pvcs:
                print("[orphans] no orphaned replicas or stuck PVCs")
                return
            await asyncio.sleep(3)

        if last_stuck_pvcs:
            pytest.fail(f"PVCs stuck after 90s: {last_stuck_pvcs}")
        assert not last_orphans, f"Orphaned Longhorn replicas after 90s: {last_orphans}"
