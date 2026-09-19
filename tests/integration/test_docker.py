"""Integration tests for first-class ``--docker``.

k7d runs dockerd as a guest agent service (no sidecar, no privileged).
Kata backends use a privileged docker-vehicle container + block graph.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from k7.core.core import K7Core
from k7.core.docker import VEHICLE_CONTAINER_NAME
from k7.core.models import SandboxConfig

pytestmark = pytest.mark.integration

_K7D_DISKS = Path("/var/lib/k7d/disks")


def _wait_all_containers_ready(sandbox_name: str, namespace: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            [
                "k3s",
                "kubectl",
                "get",
                "pods",
                "-n",
                namespace,
                "-l",
                f"app={sandbox_name}",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            time.sleep(2)
            continue
        try:
            data = json.loads(result.stdout)
            items = data.get("items", [])
            for pod in items:
                if pod.get("status", {}).get("phase") != "Running":
                    continue
                statuses = pod.get("status", {}).get("containerStatuses", [])
                if statuses and all(s.get("ready") for s in statuses):
                    return
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
        time.sleep(2)
    raise TimeoutError(f"Not all containers ready for {sandbox_name} within {timeout}s")


# Classic builder uses dockerd's pull path (CA bundle in the payload).
# Default BuildKit metadata fetch does not (CHALLENGES #13).
_DOCKER_BUILD = "DOCKER_BUILDKIT=0 docker build"

_LOCAL_NODE = os.uname().nodename


async def _wait_docker_ready(k7_core: K7Core, name: str, namespace: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await k7_core.exec_command(name, "docker info >/dev/null 2>&1 && echo ok", namespace=namespace)
        if "ok" in (result.stdout or ""):
            return
        await asyncio.sleep(3)
    raise TimeoutError(f"Docker daemon not ready in {name} within {timeout}s")


async def _sh(k7_core: K7Core, name: str, namespace: str, command: str, timeout: int = 180) -> str:
    """Run a command in the sandbox and require a zero guest exit code.

    Kata CRI exec returns after the first stdout chunk and kills the rest
    of the script (CHALLENGES #13). Launch with no streaming stdout, then
    poll a done-file over short execs.
    """
    token = uuid.uuid4().hex[:12]
    out = f"/tmp/k7-{token}.out"
    ec = f"/tmp/k7-{token}.ec"
    launch = f"trap '' HUP; rm -f {ec} {out}; ( sh -c {shlex.quote(command)} >{out} 2>&1; echo $? >{ec} ) &"
    await k7_core.exec_command(name, launch, namespace=namespace)
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        poll = f"[ -f {ec} ] && echo __K7_EC:$(cat {ec})"
        result = await k7_core.exec_command(name, poll, namespace=namespace)
        last = (result.stdout or "") + (f"\n{result.stderr}" if result.stderr else "")
        match = re.search(r"__K7_EC:(\d+)", last)
        if match:
            code = int(match.group(1))
            body_res = await k7_core.exec_command(name, f"tail -c 8000 {out}; rm -f {out} {ec}", namespace=namespace)
            body = (body_res.stdout or "").strip("\n")
            assert code == 0, f"{command!r} exited {code}:\n{body}"
            return body
        await asyncio.sleep(2)
    raise AssertionError(f"timeout waiting for {command!r}: {last!r}")


async def _wait_nginx_via_docker_exec(k7_core: K7Core, name: str, namespace: str, timeout: int = 90) -> str:
    """Probe the inner nginx through ``docker exec``, not CRI localhost.

    ``docker run -p 8080:80`` publishes in the guest host netns (k7d
    agent's wget). ``k7 exec`` is the CRI container netns.
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        result = await k7_core.exec_command(
            name,
            "docker exec web wget -qO- http://127.0.0.1:80 2>&1 | head -c 200",
            namespace=namespace,
        )
        last = result.stdout or ""
        if "nginx" in last.lower() or "html" in last.lower() or "Welcome" in last:
            return last
        await asyncio.sleep(2)
    raise TimeoutError(f"nginx did not serve in {name} within {timeout}s: {last!r}")


def _assert_overlay2(docker_info: str) -> None:
    """Overlay2 is the graph driver; vfs on that line is a ready failure."""
    assert "Storage Driver: overlay2" in docker_info, docker_info
    driver_line = docker_info.split("Storage Driver:")[-1].splitlines()[0].lower()
    assert "vfs" not in driver_line, f"storage driver line is vfs-shaped: {driver_line!r}"


def _k7d_docker_disk_names() -> set[str]:
    if not _K7D_DISKS.is_dir():
        return set()
    return {p.name for p in _K7D_DISKS.iterdir() if "docker" in p.name}


async def _wait_k7d_docker_disks_gone(before: set[str], timeout: float = 60.0) -> None:
    """k7 delete returns when K8s objects are gone; k7d unlinks the graph later.

    ``delete_sandbox`` does not wait for VM teardown (API latency must not
    couple to shim Delete). Poll here; a leftover after ``timeout`` is a leak.
    """
    started = time.time()
    leftover: set[str] = set()
    while time.time() - started < timeout:
        leftover = _k7d_docker_disk_names() - before
        if not leftover:
            return
        await asyncio.sleep(1)
    raise AssertionError(f"leaked k7d docker volume images after {time.time() - started:.1f}s: {leftover}")


def _pod_annotations(name: str, namespace: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "k3s",
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={name}",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout).get("items", [])
    assert items, f"no pod for {name}"
    return items[0].get("metadata", {}).get("annotations") or {}


# ===========================================================================
# k7d — first-class --docker
# ===========================================================================


@pytest.mark.k7d
class TestDockerK7d:
    backend = "k7d"

    def _name(self, stem: str) -> str:
        return stem if self.backend == "k7d" else f"{stem}-fc"

    async def test_docker_engine_cli_path_sharing_not_privileged(self, k7_core: K7Core, test_namespace: str):
        """Overlay2, hello-world, compose/buildx, -v, CapBnd, graph disk."""
        name = self._name("docker-engine-k7d")
        before_disks = _k7d_docker_disk_names()
        cfg = SandboxConfig(
            name=name,
            image="ubuntu:24.04",
            namespace=test_namespace,
            backend=self.backend,
            docker=True,
            node_name=_LOCAL_NODE,
            limits={"memory": "3Gi", "cpu": "2"},
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)

            ann = _pod_annotations(name, test_namespace)
            assert ann.get("k7d.katakate.org/docker") == "true"
            assert "k7.katakate.org/sidecar" not in ann
            if self.backend == "k7d-fc":
                pod = _pod_json(name, test_namespace)
                assert pod.get("spec", {}).get("runtimeClassName") == "k7-fc"

            info = await k7_core.exec_command(name, "docker info", namespace=test_namespace)
            assert info.exit_code == 0, info.stderr
            _assert_overlay2(info.stdout)

            during_disks = _k7d_docker_disk_names() - before_disks
            assert during_disks, "k7d docker graph disk missing from /var/lib/k7d/disks (graph would be tmpfs)"

            hello = await k7_core.exec_command(name, "docker run --rm hello-world", namespace=test_namespace)
            assert hello.exit_code == 0, hello.stderr
            assert "Hello from Docker!" in hello.stdout

            compose = await k7_core.exec_command(name, "docker compose version", namespace=test_namespace)
            assert compose.exit_code == 0, compose.stderr
            buildx = await k7_core.exec_command(name, "docker buildx version", namespace=test_namespace)
            assert buildx.exit_code == 0, buildx.stderr

            await k7_core.exec_command(name, "mkdir -p /tmp/x && echo f > /tmp/x/f", namespace=test_namespace)
            share = await k7_core.exec_command(
                name, "docker run --rm -v /tmp/x:/x alpine:3.21 ls /x", namespace=test_namespace
            )
            assert share.exit_code == 0, share.stderr
            assert "f" in share.stdout

            cap = await k7_core.exec_command(name, "grep '^CapBnd:' /proc/self/status", namespace=test_namespace)
            assert cap.exit_code == 0, cap.stderr
            capbnd = cap.stdout.split()[-1].lower()
            assert capbnd != "000001ffffffffff", f"sandbox CapBnd is full: {capbnd}"
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)
            await _wait_k7d_docker_disks_gone(before_disks)

    async def test_docker_build_and_compose(self, k7_core: K7Core, test_namespace: str):
        """docker build + compose of a one-service fixture."""
        name = self._name("docker-build-k7d")
        cfg = SandboxConfig(
            name=name,
            image="ubuntu:24.04",
            namespace=test_namespace,
            backend=self.backend,
            docker=True,
            node_name=_LOCAL_NODE,
            limits={"memory": "3Gi", "cpu": "2"},
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            await _sh(
                k7_core,
                name,
                test_namespace,
                "mkdir -p /tmp/app && "
                "printf 'FROM alpine:3.21\\nCMD echo built-ok\\n' > /tmp/app/Dockerfile && "
                "printf 'services:\\n  app:\\n    image: myapp\\n    command: echo compose-ok\\n' "
                "> /tmp/app/compose.yaml",
            )
            build = await _sh(
                k7_core,
                name,
                test_namespace,
                f"cd /tmp/app && {_DOCKER_BUILD} -t myapp .",
            )
            assert (
                "Successfully tagged" in build
                or "Successfully built" in build
                or "naming to docker.io/library/myapp" in build
            ), build
            run = await _sh(k7_core, name, test_namespace, "docker run --rm myapp")
            assert "built-ok" in run
            compose = await _sh(
                k7_core,
                name,
                test_namespace,
                "cd /tmp/app && DOCKER_BUILDKIT=0 docker compose run --rm app",
            )
            assert "compose-ok" in compose
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_docker_fork_keeps_running_inner_container(self, k7_core: K7Core, test_namespace: str):
        """k7 fork of --docker keeps a running inner container; not a sidecar reject."""
        source = self._name("docker-fork-src")
        fork_name = self._name("docker-fork-dst")
        cfg = SandboxConfig(
            name=source,
            image="ubuntu:24.04",
            namespace=test_namespace,
            backend=self.backend,
            docker=True,
            node_name=os.uname().nodename,
            limits={"memory": "3Gi", "cpu": "2"},
        )
        result = await k7_core.create_sandbox(cfg)
        assert result.success, f"create failed: {result.error}"

        try:
            _wait_all_containers_ready(source, test_namespace)
            await _wait_docker_ready(k7_core, source, test_namespace)
            await _sh(
                k7_core,
                source,
                test_namespace,
                "docker run -d --name web -p 8080:80 nginx:1.27-alpine",
            )
            await _wait_nginx_via_docker_exec(k7_core, source, test_namespace)

            fork_result = await k7_core.fork_sandbox(source, fork_name, namespace=test_namespace)
            assert fork_result.success, f"fork failed (must not trip sidecar reject): {fork_result.error}"
            assert "sidecar" not in (fork_result.error or "").lower()

            _wait_all_containers_ready(fork_name, test_namespace, timeout=180)
            await _wait_docker_ready(k7_core, fork_name, test_namespace)
            child_info = await k7_core.exec_command(fork_name, "docker info", namespace=test_namespace)
            assert child_info.exit_code == 0, child_info.stderr
            _assert_overlay2(child_info.stdout)

            ps = await _sh(
                k7_core,
                fork_name,
                test_namespace,
                "docker ps --format '{{.Names}}'",
            )
            assert "web" in ps
            child = await _wait_nginx_via_docker_exec(k7_core, fork_name, test_namespace)
            assert "Welcome to nginx" in child or "nginx" in child.lower() or "html" in child.lower()

            parent = await _wait_nginx_via_docker_exec(k7_core, source, test_namespace)
            assert "nginx" in parent.lower() or "html" in parent.lower() or "Welcome" in parent
        finally:
            await k7_core.delete_sandbox(fork_name, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)


@pytest.mark.k7d
class TestDockerK7dFc(TestDockerK7d):
    """The full --docker suite on RuntimeClass k7-fc."""

    backend = "k7d-fc"

    @pytest.fixture(autouse=True)
    def _require_k7_fc(self):
        result = subprocess.run(
            ["k3s", "kubectl", "get", "runtimeclass", "k7-fc"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("RuntimeClass k7-fc not registered")


def _pod_json(name: str, namespace: str) -> dict:
    result = subprocess.run(
        ["k3s", "kubectl", "get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout).get("items", [])
    assert items, f"no pod for {name}"
    return items[0]


def _sandbox_security_context(name: str, namespace: str) -> dict:
    pod = _pod_json(name, namespace)
    sandbox = next(c for c in pod["spec"]["containers"] if c["name"] == "sandbox")
    return sandbox.get("securityContext") or {}


def _privileged_container_names(name: str, namespace: str) -> list[str]:
    pod = _pod_json(name, namespace)
    names = []
    for c in pod["spec"]["containers"]:
        if (c.get("securityContext") or {}).get("privileged"):
            names.append(c["name"])
    return names


async def _assert_overlay2_engine_cli_share(k7_core: K7Core, name: str, namespace: str) -> None:
    info = await _sh(k7_core, name, namespace, "docker info -f '{{.Driver}} {{.DriverStatus}}'")
    assert "overlay2" in info, info
    assert "vfs" not in info.lower(), f"vfs storage driver: {info}"
    hello = await _sh(k7_core, name, namespace, "docker run --rm hello-world")
    assert "Hello from Docker!" in hello
    compose = await _sh(k7_core, name, namespace, "docker compose version")
    assert "v" in compose.lower() or "compose" in compose.lower()
    buildx = await _sh(k7_core, name, namespace, "docker buildx version")
    assert "buildx" in buildx.lower() or "github.com/docker/buildx" in buildx.lower()
    await _sh(k7_core, name, namespace, "mkdir -p /tmp/x && echo f > /tmp/x/f")
    share = await _sh(k7_core, name, namespace, "docker run --rm -v /tmp/x:/x alpine:3.21 ls /x")
    assert "f" in share


async def _assert_build_and_compose(k7_core: K7Core, name: str, namespace: str) -> None:
    await _sh(
        k7_core,
        name,
        namespace,
        "mkdir -p /tmp/app && "
        "printf 'FROM alpine:3.21\\nCMD echo built-ok\\n' > /tmp/app/Dockerfile && "
        "printf 'services:\\n  app:\\n    image: myapp\\n    command: echo compose-ok\\n' "
        "> /tmp/app/compose.yaml",
    )
    build = await _sh(k7_core, name, namespace, f"cd /tmp/app && {_DOCKER_BUILD} -t myapp .")
    assert (
        "Successfully tagged" in build or "Successfully built" in build or "naming to docker.io/library/myapp" in build
    ), build
    run = await _sh(k7_core, name, namespace, "docker run --rm myapp")
    assert "built-ok" in run
    compose = await _sh(
        k7_core,
        name,
        namespace,
        "cd /tmp/app && DOCKER_BUILDKIT=0 docker compose run --rm app",
    )
    assert "compose-ok" in compose


def _remaining_pvcs(namespace: str, prefix: str) -> list[str]:
    pvc_check = subprocess.run(
        ["k3s", "kubectl", "get", "pvc", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [p for p in pvc_check.stdout.strip().split() if p.startswith(prefix)]


def _remaining_configmaps(namespace: str, prefix: str) -> list[str]:
    check = subprocess.run(
        ["k3s", "kubectl", "get", "cm", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [p for p in check.stdout.strip().split() if p.startswith(prefix)]


class _DockerKataMixin:
    backend: str
    limits = {"memory": "3Gi", "cpu": "2"}

    def _cfg(self, name: str, namespace: str, **kwargs) -> SandboxConfig:
        return SandboxConfig(
            name=name,
            image="ubuntu:24.04",
            namespace=namespace,
            backend=self.backend,
            docker=True,
            limits=dict(self.limits),
            **kwargs,
        )

    async def test_docker_engine_cli_path_sharing(self, k7_core: K7Core, test_namespace: str):
        """Overlay2, hello-world, compose/buildx, -v."""
        name = f"docker-engine-{self.backend.split('-')[1][:2]}"
        result = await k7_core.create_sandbox(self._cfg(name, test_namespace))
        assert result.success, f"create failed: {result.error}"
        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            ann = _pod_annotations(name, test_namespace)
            assert ann.get("k7.katakate.org/docker") == "true"
            assert "k7.katakate.org/sidecar" not in ann
            await _assert_overlay2_engine_cli_share(k7_core, name, test_namespace)
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_docker_build_and_compose(self, k7_core: K7Core, test_namespace: str):
        """docker build + compose of a one-service fixture."""
        name = f"docker-build-{self.backend.split('-')[1][:2]}"
        result = await k7_core.create_sandbox(self._cfg(name, test_namespace))
        assert result.success, f"create failed: {result.error}"
        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            await _assert_build_and_compose(k7_core, name, test_namespace)
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_sandbox_security_context_unchanged(self, k7_core: K7Core, test_namespace: str):
        """--docker does not change the sandbox security context."""
        docker_name = f"docker-sec-{self.backend.split('-')[1][:2]}"
        plain_name = f"plain-sec-{self.backend.split('-')[1][:2]}"
        docker_cfg = self._cfg(docker_name, test_namespace)
        plain = SandboxConfig(
            name=plain_name,
            image="ubuntu:24.04",
            namespace=test_namespace,
            backend=self.backend,
            docker=False,
            limits=dict(self.limits),
        )
        docker_res = await k7_core.create_sandbox(docker_cfg)
        assert docker_res.success, docker_res.error
        try:
            plain_res = await k7_core.create_sandbox(plain)
            assert plain_res.success, plain_res.error
            try:
                _wait_all_containers_ready(docker_name, test_namespace)
                _wait_all_containers_ready(plain_name, test_namespace)
                assert _sandbox_security_context(docker_name, test_namespace) == _sandbox_security_context(
                    plain_name, test_namespace
                )
                priv = _privileged_container_names(docker_name, test_namespace)
                if self.backend == "kata-qemu-longhorn":
                    assert "sandbox" in priv
                    assert VEHICLE_CONTAINER_NAME in priv
                    assert priv.count(VEHICLE_CONTAINER_NAME) == 1
                else:
                    assert priv == [VEHICLE_CONTAINER_NAME]
            finally:
                await k7_core.delete_sandbox(plain_name, namespace=test_namespace)
        finally:
            await k7_core.delete_sandbox(docker_name, namespace=test_namespace)

    async def test_delete_leaves_no_docker_resources(self, k7_core: K7Core, test_namespace: str):
        """Delete leaves no docker resources behind."""
        name = f"docker-clean-{self.backend.split('-')[1][:2]}"
        result = await k7_core.create_sandbox(self._cfg(name, test_namespace))
        assert result.success, result.error
        _wait_all_containers_ready(name, test_namespace)
        await k7_core.delete_sandbox(name, namespace=test_namespace)
        await asyncio.sleep(5)
        leftover_pvc = _remaining_pvcs(test_namespace, name)
        leftover_cm = _remaining_configmaps(test_namespace, name)
        assert not leftover_pvc, f"leaked PVCs: {leftover_pvc}"
        assert not leftover_cm, f"leaked ConfigMaps: {leftover_cm}"


@pytest.mark.qemu
class TestDockerKQL(_DockerKataMixin):
    backend = "kata-qemu-longhorn"

    async def test_pause_resume_keeps_image(self, k7_core: K7Core, test_namespace: str):
        """Pause/resume keeps the pulled image."""
        name = "docker-pr-ql"
        result = await k7_core.create_sandbox(self._cfg(name, test_namespace))
        assert result.success, result.error
        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            await _sh(k7_core, name, test_namespace, "docker pull alpine:3.21")
            pause = await k7_core.pause_sandbox(name, namespace=test_namespace)
            assert pause.success, pause.error
            await asyncio.sleep(5)
            resume = await k7_core.resume_sandbox(name, namespace=test_namespace)
            assert resume.success, resume.error
            _wait_all_containers_ready(name, test_namespace, timeout=180)
            await _wait_docker_ready(k7_core, name, test_namespace)
            has = await _sh(k7_core, name, test_namespace, "docker image inspect alpine:3.21 >/dev/null && echo yes")
            assert "yes" in has
            run = await _sh(k7_core, name, test_namespace, "docker run --rm alpine:3.21 echo survived-pause")
            assert "survived-pause" in run
        finally:
            await k7_core.delete_sandbox(name, namespace=test_namespace)

    async def test_fork_clones_both_pvcs(self, k7_core: K7Core, test_namespace: str):
        """Fork clones both the root and the docker graph PVC."""
        source = "docker-fork-src-ql"
        fork_name = "docker-fork-dst-ql"
        result = await k7_core.create_sandbox(self._cfg(source, test_namespace))
        assert result.success, result.error
        try:
            _wait_all_containers_ready(source, test_namespace)
            await _wait_docker_ready(k7_core, source, test_namespace)
            await _sh(k7_core, source, test_namespace, "docker pull alpine:3.21")
            fork = await k7_core.fork_sandbox(source, fork_name, namespace=test_namespace)
            assert fork.success, fork.error
            # Same 600s bound as test_qemu.test_fork_clones_data: on HA
            # longhorn_replicas=3 the cloned volumes hydrate before first
            # attach ("not ready for workloads"). 240s is a false timeout.
            _wait_all_containers_ready(fork_name, test_namespace, timeout=600)
            await _wait_docker_ready(k7_core, fork_name, test_namespace)
            has = await _sh(
                k7_core, fork_name, test_namespace, "docker image inspect alpine:3.21 >/dev/null && echo yes"
            )
            assert "yes" in has
            run = await _sh(k7_core, fork_name, test_namespace, "docker run --rm alpine:3.21 echo fork-works")
            assert "fork-works" in run
            parent = await _sh(k7_core, source, test_namespace, "docker run --rm alpine:3.21 echo parent-ok")
            assert "parent-ok" in parent
            pvcs = _remaining_pvcs(test_namespace, "")
            assert f"{source}-docker-lh" in pvcs
            assert f"{fork_name}-docker-lh" in pvcs
            assert f"{source}-root-lh" in pvcs
            assert f"{fork_name}-root-lh" in pvcs
        finally:
            await k7_core.delete_sandbox(fork_name, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)

    async def test_snapshot_restore_keeps_image(self, k7_core: K7Core, test_namespace: str):
        """Snapshot + restore keeps the pulled image."""
        source = "docker-snap-src-ql"
        restored = "docker-snap-dst-ql"
        snap = "docker-snap-ql"
        result = await k7_core.create_sandbox(self._cfg(source, test_namespace))
        assert result.success, result.error
        try:
            _wait_all_containers_ready(source, test_namespace)
            await _wait_docker_ready(k7_core, source, test_namespace)
            await _sh(k7_core, source, test_namespace, "docker pull alpine:3.21")
            snap_res = await k7_core.create_snapshot(source, snap, namespace=test_namespace)
            assert snap_res.success, snap_res.error
            restore = await k7_core.restore_sandbox(snap, restored, namespace=test_namespace)
            assert restore.success, restore.error
            _wait_all_containers_ready(restored, test_namespace, timeout=240)
            await _wait_docker_ready(k7_core, restored, test_namespace)
            has = await _sh(
                k7_core, restored, test_namespace, "docker image inspect alpine:3.21 >/dev/null && echo yes"
            )
            assert "yes" in has
        finally:
            await k7_core.delete_sandbox(restored, namespace=test_namespace)
            await k7_core.delete_sandbox(source, namespace=test_namespace)
            await k7_core.delete_snapshot(snap, namespace=test_namespace)


@pytest.mark.firecracker
class TestDockerKFD(_DockerKataMixin):
    backend = "kata-firecracker-devmapper"

    async def test_graph_is_ephemeral_block_fork_rejected(self, k7_core: K7Core, test_namespace: str):
        """The graph is an ephemeral block device, so fork is rejected."""
        name = "docker-ephem-kfd"
        result = await k7_core.create_sandbox(self._cfg(name, test_namespace))
        assert result.success, result.error
        try:
            _wait_all_containers_ready(name, test_namespace)
            await _wait_docker_ready(k7_core, name, test_namespace)
            mnt = await k7_core.exec_command(
                name,
                "findmnt -no FSTYPE,SOURCE /var/lib/docker || cat /proc/mounts",
                namespace=test_namespace,
                container="docker-vehicle",
            )
            text = (mnt.stdout or "") + (mnt.stderr or "")
            assert "ext4" in text.lower() or "ext4" in text
            assert "virtiofs" not in text.lower()
            assert "tmpfs" not in text.split("/var/lib/docker")[-1].splitlines()[0].lower()
            fork = await k7_core.fork_sandbox(name, "docker-kfd-fork", namespace=test_namespace)
            assert not fork.success
            assert "cannot be cloned" in (fork.error or "").lower()
        finally:
            before = subprocess.run(["lvs", "--noheadings", "-o", "lv_name", "kata-vg"], capture_output=True, text=True)
            await k7_core.delete_sandbox(name, namespace=test_namespace)
            await asyncio.sleep(8)
            leftover_pvc = _remaining_pvcs(test_namespace, name)
            leftover_cm = _remaining_configmaps(test_namespace, name)
            assert not leftover_pvc, f"leaked PVCs: {leftover_pvc}"
            assert not leftover_cm, f"leaked ConfigMaps: {leftover_cm}"
            after = subprocess.run(["lvs", "--noheadings", "-o", "lv_name", "kata-vg"], capture_output=True, text=True)
            # thin-pool remains; docker graph LVs must be gone.
            leftover_lvs = [
                ln.strip()
                for ln in (after.stdout or "").splitlines()
                if ln.strip() and ln.strip() != "thin-pool" and name.replace("_", "-") in ln
            ]
            assert not leftover_lvs, f"leaked LVs: {leftover_lvs} (before={before.stdout!r})"
