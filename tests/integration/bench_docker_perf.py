"""Docker workload benchmark — host vs k7-fd vs k7-ql vs k7d vs k7d-fc.

Why a pytest module rather than a separate bash harness:
``tests/integration/test_docker.py`` already pioneered the pattern
of spinning up a sandbox with Docker and running docker commands via
``k7_core.exec_command``. This benchmark times that same pattern.

k7d and k7d-fc use first-class ``--docker`` (guest agent dockerd, overlay2 on a
virtio-blk graph disk, forkable). Kata backends use the same ``--docker``
flag via a privileged docker-vehicle container and a block graph
(overlay2): Longhorn Block PVC on kql, ephemeral LVM thin LV on kfd.

Why these environments are valid:
``test_sidecar_docker.py`` has a note claiming Docker-DinD doesn't work on
the Firecracker backend. That note is historical — ``k7 create --docker``
now lands on the vehicle, not ``docker:27.5-dind``. The isolation we want:

  - ``k7-fd``  — docker-in-VM without Longhorn in the storage path.
  - ``k7-ql-r1`` — adds Longhorn r=1 (one local replica).
  - ``k7-ql-r2`` — adds Longhorn r=2 (one local + one cross-node sync).
  - ``k7-ql-r3`` — Longhorn r=3 (replicas on all three nodes of an HA
    cluster — the full redundancy write-amplification cost).
  - ``k7d`` — first-class ``--docker``: dockerd is an agent child, graph
    on a per-sandbox virtio-blk scratch disk (overlay2, not tmpfs).
    Needs enough ``--memory`` for the engine; layers live on the graph
    disk. Fork of a warm engine is in-scope.
  - ``k7d-fc`` — same guest docker service as ``k7d``, RuntimeClass
    ``k7-fc`` (stock Firecracker + jailer). Overlay2 and warm fork of
    a running inner engine must match native k7d.

Marker / invocation:
This file uses ``@pytest.mark.bench`` so it doesn't run with the rest of
``-m integration``. Drive it explicitly:

    K7_BENCH_ENVS=host,k7-fd,k7-ql-r1,k7-ql-r2,k7-ql-r3,k7d,k7d-fc \\
    K7_BENCH_OUT=/tmp/bench-out \\
    uv run pytest -m bench tests/integration/bench_docker_perf.py -v

Replica counts are applied per sandbox volume by patching the Longhorn
Volume CR's ``spec.numberOfReplicas`` after creation (the ``longhorn``
StorageClass pins ``numberOfReplicas`` as a provisioning parameter, so
patching Longhorn's ``default-replica-count`` setting has no effect on
these volumes). Each ql log header records the actual replica placement.

Output: one ``bench-<label>-<ts>.log`` per env (same format ``render.py``
already parses), written under ``$K7_BENCH_OUT`` (default ``/tmp``).
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from k7.core.core import K7Core, _is_k7d_family
from k7.core.models import SandboxConfig

pytestmark = pytest.mark.bench

# How many reps per cell + warm-up. Override via env for ad-hoc shorter runs.
REPS = int(os.environ.get("K7_BENCH_REPS", "5"))
WARMUP = int(os.environ.get("K7_BENCH_WARMUP", "1"))
OUT_DIR = Path(os.environ.get("K7_BENCH_OUT", "/tmp"))

# Pinned image + tag for reproducibility — captured in each log's header.
BENCH_IMAGE = "debian:12-slim"
SANDBOX_IMAGE = "docker:27.5-cli"

# The Dockerfile content used by the build-no-cache leg. We write it into
# the sandbox at /tmp/bench/Dockerfile via exec_command so we don't depend
# on ``kubectl cp``.
BENCH_DOCKERFILE = (Path(__file__).resolve().parents[2] / "bench" / "docker-perf" / "bench.Dockerfile").read_text()

# Operations in stable order. Each value is a callable returning the shell
# command (so we can interpolate per-iteration state like the small-files
# count for run_io).
Operation = tuple[str, str]  # (op_name, shell_command)


def _build_cmd(args: str, classic: bool) -> str:
    # k7d guest BuildKit lacks the payload CA bundle (CHALLENGES #13).
    prefix = "DOCKER_BUILDKIT=0 " if classic else ""
    return f"{prefix}docker build {args}"


def _ops(*, classic_build: bool = False) -> list[Operation]:
    return [
        ("pull", f"docker rmi -f {BENCH_IMAGE} 2>/dev/null; docker pull {BENCH_IMAGE}"),
        # The big one: apt + pip + git + 256 MB fsync.
        # ``--network=host`` works around the same Docker default-bridge DNS
        # flake seen when building the API image: with default bridge
        # networking, ``apt-get update`` inside the build container intermittently
        # fails with "Try again" / "failed to lookup address information". Host
        # networking shares the daemon's already-working resolver — no measurement
        # impact for our purposes (the storage path is the same either way).
        (
            "build_nocache",
            f"cd /tmp/bench && {_build_cmd('--network=host --no-cache -t bench:nocache -f Dockerfile .', classic_build)}",
        ),
        # Should be sub-second; non-zero would indicate a broken /var/lib/docker bind.
        (
            "build_cached",
            f"cd /tmp/bench && {_build_cmd('--network=host -t bench:nocache -f Dockerfile .', classic_build)}",
        ),
        # 10s busy-loop — measures kata-qemu CPU overhead (mostly noise on host).
        (
            "run_cpu",
            "docker run --rm bench:nocache /opt/venv/bin/python -c "
            "'import time; end=time.time()+10; n=0\n"
            "while time.time()<end: n+=1\n"
            'print("iters", n)\'',
        ),
        # Pure write path: 2k small files + 512 MB fsync.
        (
            "run_io",
            "docker run --rm bench:nocache sh -c 'set -e; mkdir /tmp/many; "
            'i=1; while [ "$i" -le 2000 ]; do echo "$i" > /tmp/many/f"$i"; i=$((i+1)); done; '
            "sync; dd if=/dev/zero of=/tmp/big bs=1M count=512 conv=fsync 2>&1 | tail -1; "
            "rm -rf /tmp/many /tmp/big'",
        ),
        # Read path. Page-cache-dominated inside the sandbox (can't drop_caches
        # without privs) — flagged as a known confound in bench/docker-perf/README.md.
        (
            "run_read",
            "docker run --rm bench:nocache sh -c 'find /opt/venv -type f -exec cat {} + > /dev/null'",
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers shared by the env-specific runners.
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _drop_host_page_cache() -> None:
    """Best-effort: drop OS page cache on the host before timing.

    Inside a sandbox we lack the privilege; the read-path measurement
    inside the sandbox is therefore page-cache-dominated. Documented as
    a known confound in bench/docker-perf/README.md.
    """
    if not os.access("/proc/sys/vm/drop_caches", os.W_OK):
        return
    try:
        subprocess.run(["sync"], check=False, timeout=10)
        Path("/proc/sys/vm/drop_caches").write_text("3\n")
    except (OSError, subprocess.SubprocessError):
        pass


def _write_log_header(log: Path, *, label: str, extra: dict[str, str]) -> None:
    parts = [
        f"label={label}",
        f"timestamp={_ts()}",
        f"host={platform.node()}",
        f"kernel={platform.release()}",
        f"python={platform.python_version()}",
        f"reps={REPS}",
        f"warmup={WARMUP}",
        f"bench_image={BENCH_IMAGE}",
        f"sandbox_image={SANDBOX_IMAGE}",
    ]
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    log.write_text("\n".join(f"# {p}" for p in parts) + "\n# columns: OPERATION  RUN  SECONDS\n")


def _append_row(log: Path, op: str, run: int, seconds: float) -> None:
    with log.open("a") as f:
        f.write(f"{op:<22} {run} {seconds:.3f}\n")


# ---------------------------------------------------------------------------
# Host runner — subprocess against the node's docker daemon, no k7 at all.
# ---------------------------------------------------------------------------


def _host_docker_reset() -> None:
    for args in (
        ["docker", "rm", "-f", "bench-cpu", "bench-io", "bench-read"],
        ["docker", "rmi", "-f", "bench:nocache"],
        ["docker", "builder", "prune", "-af"],
        ["docker", "image", "prune", "-af"],
    ):
        subprocess.run(args, capture_output=True, text=True, timeout=120)


def _host_run_once(op: str, command: str) -> float:
    _drop_host_page_cache()
    start = _now()
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=1800)
    elapsed = _now() - start
    if result.returncode != 0:
        # Don't poison the log — but raise so the test goes red.
        raise AssertionError(f"host op {op!r} failed (rc={result.returncode}): {result.stderr[-500:]}")
    return elapsed


def _bench_host(out_dir: Path) -> Path:
    if not shutil.which("docker"):
        pytest.skip("host: docker binary not available")
    docker_version = (
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        or "unknown"
    )
    storage_driver = subprocess.run(
        ["sh", "-c", "docker info 2>/dev/null | grep 'Storage Driver' | head -1"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    label = "host"
    log = out_dir / f"bench-{label}-{_ts()}.log"
    _write_log_header(
        log,
        label=label,
        extra={"docker_version": docker_version, "storage_driver": storage_driver},
    )

    bench_dir = Path("/tmp/k7-bench-host")
    bench_dir.mkdir(parents=True, exist_ok=True)
    (bench_dir / "Dockerfile").write_text(BENCH_DOCKERFILE)

    ops = _ops()
    # Warm-up — pull + a single build so the first measurement isn't dominated
    # by daemon spin-up or initial layer extract.
    if WARMUP:
        subprocess.run(["docker", "pull", BENCH_IMAGE], capture_output=True, timeout=300)
        subprocess.run(
            # ``--network=host`` for the same DNS reason as the timed builds below.
            ["docker", "build", "--network=host", "--no-cache", "-t", "bench:nocache", "-f", "Dockerfile", "."],
            cwd=bench_dir,
            capture_output=True,
            timeout=1800,
        )

    for op, command in ops:
        # Only ``pull`` and ``build_nocache`` get a between-iteration reset:
        # ``pull`` so each rep is a real pull (no local cache), ``build_nocache``
        # so each rep is genuinely cache-less. ``build_cached`` needs the prior
        # build's layers. ``run_cpu`` / ``run_io`` / ``run_read`` all need the
        # ``bench:nocache`` image the build phase produced — resetting between
        # them would ``docker rmi`` that image and break the next op.
        for i in range(1, REPS + 1):
            if op in ("pull", "build_nocache"):
                _host_docker_reset()
            # Rewrite the Dockerfile for each build iteration — _host_docker_reset
            # nukes the image but leaves the on-disk Dockerfile untouched.
            (bench_dir / "Dockerfile").write_text(BENCH_DOCKERFILE)
            # The build commands assume cwd=/tmp/bench. Patch the command for
            # the host case so it uses our local dir.
            run_cmd = command.replace("/tmp/bench", str(bench_dir))
            secs = _host_run_once(op, run_cmd)
            _append_row(log, op, i, secs)

    _host_docker_reset()
    return log


# ---------------------------------------------------------------------------
# Sandbox runner — reuses K7Core.exec_command, mirrors test_sidecar_docker.py.
# ---------------------------------------------------------------------------


async def _wait_docker_ready(k7_core: K7Core, name: str, namespace: str, timeout: int = 180) -> None:
    deadline = _now() + timeout
    while _now() < deadline:
        r = await k7_core.exec_command(name, "docker info >/dev/null 2>&1 && echo ok", namespace=namespace)
        if r.exit_code == 0 and "ok" in r.stdout:
            return
        await asyncio.sleep(3)
    raise TimeoutError(f"Docker not ready in {name} within {timeout}s")


def _wait_all_containers_ready(name: str, namespace: str, timeout: int = 300) -> None:
    """Same shape as test_sidecar_docker.py — block until every container is Ready."""
    import json as _json

    deadline = _now() + timeout
    while _now() < deadline:
        r = subprocess.run(
            ["k3s", "kubectl", "get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            try:
                items = _json.loads(r.stdout).get("items", [])
                for pod in items:
                    if pod.get("status", {}).get("phase") != "Running":
                        continue
                    statuses = pod.get("status", {}).get("containerStatuses", [])
                    if statuses and all(s.get("ready") for s in statuses):
                        return
            except (_json.JSONDecodeError, KeyError, IndexError):
                pass
        time.sleep(2)
    raise TimeoutError(f"containers not ready for {name}: {timeout}s")


async def _sandbox_run_once(k7_core: K7Core, name: str, namespace: str, command: str, timeout: int = 1800) -> float:
    """Time a guest command. Kata CRI exec returns after the first stdout chunk
    (CHALLENGES #13), so we launch with no streaming stdout and poll a done-file.
    """
    token = uuid.uuid4().hex[:12]
    out = f"/tmp/k7-bench-{token}.out"
    ec = f"/tmp/k7-bench-{token}.ec"
    launch = f"trap '' HUP; rm -f {ec} {out}; ( sh -c {shlex.quote(command)} >{out} 2>&1; echo $? >{ec} ) &"
    start = _now()
    await k7_core.exec_command(name, launch, namespace=namespace)
    deadline = start + timeout
    last = ""
    while _now() < deadline:
        result = await k7_core.exec_command(name, f"[ -f {ec} ] && echo __K7_EC:$(cat {ec})", namespace=namespace)
        last = (result.stdout or "") + (f"\n{result.stderr}" if result.stderr else "")
        match = re.search(r"__K7_EC:(\d+)", last)
        if match:
            elapsed = _now() - start
            code = int(match.group(1))
            body_res = await k7_core.exec_command(name, f"tail -c 800 {out}; rm -f {out} {ec}", namespace=namespace)
            if code != 0:
                body = (body_res.stdout or "").strip()
                raise AssertionError(f"sandbox op failed (rc={code}): {body[-500:]}")
            return elapsed
        await asyncio.sleep(2)
    raise AssertionError(f"sandbox op timed out after {timeout}s: {command!r} last={last!r}")


async def _sandbox_reset(k7_core: K7Core, name: str, namespace: str) -> None:
    await k7_core.exec_command(
        name,
        (
            "docker rm -f bench-cpu bench-io bench-read 2>/dev/null; "
            "docker rmi -f bench:nocache 2>/dev/null; "
            "docker builder prune -af 2>/dev/null; "
            "docker image prune -af 2>/dev/null; true"
        ),
        namespace=namespace,
    )


async def _bench_sandbox(
    k7_core: K7Core,
    label: str,
    namespace: str,
    backend: str,
    backend_extra: dict[str, str],
    out_dir: Path,
    longhorn_replicas: int | None = None,
    limits: dict[str, str] | None = None,
    *,
    docker: bool = False,
) -> Path:
    name = f"bench-{label.replace('_', '-')}"
    cfg = SandboxConfig(
        name=name,
        image="ubuntu:24.04" if docker else SANDBOX_IMAGE,
        namespace=namespace,
        backend=backend,
        docker=docker,
        sidecar=None if docker else "docker",
        # The sandbox needs room for the full bench image + its build cache
        # + the 2k-files / 512 MB workloads. Default 10Gi is the minimum;
        # keep it explicit for reproducibility — and only meaningful when
        # the root disk is a Longhorn PVC (kata-qemu-longhorn). On k7d
        # ``--docker`` the graph is a virtio-blk scratch disk (default 20Gi).
        root_disk_size="20Gi",
        limits=limits,
        node_name=platform.node(),
    )
    create = await k7_core.create_sandbox(cfg)
    assert create.success, f"create {name}: {create.error}"

    log = out_dir / f"bench-{label}-{_ts()}.log"
    try:
        _wait_all_containers_ready(name, namespace)
        await _wait_docker_ready(k7_core, name, namespace)

        if longhorn_replicas is not None:
            placement = _set_sandbox_volume_replicas(namespace, name, longhorn_replicas)
            backend_extra = {
                **backend_extra,
                "longhorn_replica_nodes": ";".join(f"{v}={','.join(nodes)}" for v, nodes in placement.items()),
            }
            print(f"\n[bench] {label} Longhorn replica placement: {placement}")

        # Push the Dockerfile in via a single exec. The previous version of
        # this used ``printf '%s' '<escaped>'`` and a literal-quote escape
        # dance, which silently mangled the file (single quotes inside the
        # Dockerfile content broke the surrounding shell quoting and ended
        # up writing a near-empty file — docker build then "succeeded" in
        # ~80 ms against the broken Dockerfile, which is what produced the
        # impossibly-fast first-run numbers).
        #
        # Heredoc with a *quoted* sentinel (``<<'BENCH_EOF'``) is the right
        # primitive — shell does no expansion / interpolation between the
        # quoted sentinel lines, so the Dockerfile's $vars, backslashes, and
        # single quotes all land verbatim. ``k7_core.exec_command`` wraps
        # this string in ``/bin/sh -c`` so the heredoc is interpreted
        # server-side by the sandbox's shell, not by the local shell that
        # never sees it.
        write_cmd = (
            "mkdir -p /tmp/bench && cat > /tmp/bench/Dockerfile <<'BENCH_EOF'\n"
            + BENCH_DOCKERFILE
            + "\nBENCH_EOF\necho WROTE"
        )
        write_res = await k7_core.exec_command(name, write_cmd, namespace=namespace)
        assert write_res.exit_code == 0 and "WROTE" in write_res.stdout, (
            f"write Dockerfile: exit={write_res.exit_code} stdout={write_res.stdout!r} stderr={write_res.stderr!r}"
        )
        # Pin the file size so a future shell-quoting regression fails loudly
        # instead of silently producing impossibly-fast build numbers.
        expected_lines = BENCH_DOCKERFILE.count("\n") + 1
        verify = await k7_core.exec_command(name, "wc -l /tmp/bench/Dockerfile", namespace=namespace)
        actual_lines = int(verify.stdout.strip().split()[0]) if verify.stdout.strip() else 0
        assert actual_lines >= expected_lines - 2, (
            f"Dockerfile was truncated on the sandbox side: "
            f"expected ~{expected_lines} lines, got {actual_lines}. "
            f"This is the 'silent build failure' regression — see commit msg."
        )

        # Capture docker version + storage driver from inside the sandbox.
        ver = await k7_core.exec_command(
            name,
            "docker version --format '{{.Server.Version}}' 2>/dev/null",
            namespace=namespace,
        )
        sd = await k7_core.exec_command(
            name,
            "docker info 2>/dev/null | grep 'Storage Driver' | head -1",
            namespace=namespace,
        )
        _write_log_header(
            log,
            label=label,
            extra={
                "docker_version": ver.stdout.strip() or "unknown",
                "storage_driver": sd.stdout.strip(),
                **backend_extra,
            },
        )

        # Warm-up: one pull + one build (discarded).
        if WARMUP:
            await k7_core.exec_command(name, f"docker pull {BENCH_IMAGE}", namespace=namespace)
            await k7_core.exec_command(
                name,
                f"cd /tmp/bench && {_build_cmd('--network=host --no-cache -t bench:nocache -f Dockerfile .', docker)}",
                namespace=namespace,
            )

        for op, command in _ops(classic_build=docker):
            for i in range(1, REPS + 1):
                # Same reset policy as the host leg — see _bench_host for rationale.
                if op in ("pull", "build_nocache"):
                    await _sandbox_reset(k7_core, name, namespace)
                # Fault-tolerant: one wedged op (vfs-driven docker daemon
                # going zombie after a multi-minute no-cache build is the
                # observed failure mode on k7-ql) shouldn't kill the whole
                # leg. Record the failure as a sentinel row so render.py
                # can show "n/a" instead of silently dropping a cell, and
                # carry on with the remaining ops.
                try:
                    secs = await _sandbox_run_once(k7_core, name, namespace, command)
                    _append_row(log, op, i, secs)
                except AssertionError as e:
                    with log.open("a") as f:
                        f.write(f"# {op} rep {i} FAILED: {str(e)[:300]}\n")
                    _append_row(log, op, i, float("nan"))

        await _sandbox_reset(k7_core, name, namespace)

        if docker and _is_k7d_family(backend):
            # Fork a warm engine; child must stay overlay2.
            fork_name = f"{name}-fork"
            t0 = time.monotonic()
            fork = await k7_core.fork_sandbox(name, fork_name, namespace=namespace)
            elapsed = time.monotonic() - t0
            assert fork.success, f"{backend} --docker fork failed: {fork.error}"
            _append_row(log, "fork_warm_engine", 1, elapsed)
            try:
                _wait_all_containers_ready(fork_name, namespace, timeout=180)
                await _wait_docker_ready(k7_core, fork_name, namespace)
                child_sd = await k7_core.exec_command(
                    fork_name,
                    "docker info 2>/dev/null | grep 'Storage Driver' | head -1",
                    namespace=namespace,
                )
                driver = child_sd.stdout.strip()
                assert "overlay2" in driver.lower(), f"{backend} forked child storage driver: {driver!r}"
                assert "vfs" not in driver.lower(), f"{backend} forked child fell back to vfs: {driver!r}"
                with log.open("a") as f:
                    f.write(f"# storage_driver_child={driver}\n")
                _append_row(log, "fork_to_ready", 1, time.monotonic() - t0)
            finally:
                await k7_core.delete_sandbox(fork_name, namespace=namespace)
    finally:
        await k7_core.delete_sandbox(name, namespace=namespace)
    return log


# ---------------------------------------------------------------------------
# Longhorn replica-count helpers. The ``longhorn`` StorageClass pins
# ``numberOfReplicas`` as a provisioning parameter, which takes precedence
# over Longhorn's ``default-replica-count`` setting — so patching the
# setting (the old approach) silently had no effect on SC-provisioned
# sandbox volumes. The only reliable per-bench knob is the Longhorn Volume
# CR itself: ``spec.numberOfReplicas`` can be changed on a live volume
# (Longhorn adds/removes replicas to converge). We patch the sandbox's
# volumes right after creation and wait for convergence, recording the
# actual replica placement in the log header as proof.
# ---------------------------------------------------------------------------


def _kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["k3s", "kubectl", *args], capture_output=True, text=True, check=False)


def _sandbox_longhorn_volumes(namespace: str, sandbox: str) -> list[str]:
    """Return Longhorn volume (= PV) names backing the sandbox's bound PVCs."""
    r = _kubectl("get", "pvc", "-n", namespace, "-o", "json")
    assert r.returncode == 0, f"get pvc: {r.stderr}"
    vols: list[str] = []
    for item in json.loads(r.stdout).get("items", []):
        if sandbox in item["metadata"]["name"] and item["spec"].get("storageClassName") == "longhorn":
            vol = item["spec"].get("volumeName")
            if vol:
                vols.append(vol)
    return vols


def _set_sandbox_volume_replicas(namespace: str, sandbox: str, count: int, timeout: int = 600) -> dict[str, list[str]]:
    """Set ``spec.numberOfReplicas`` on every Longhorn volume of a sandbox.

    Returns ``{volume: [replica node IDs]}`` once exactly ``count`` running
    replicas exist per volume. Fails loudly on timeout — a bench leg whose
    replica count silently didn't apply would be worse than no bench at all.
    """
    vols = _sandbox_longhorn_volumes(namespace, sandbox)
    assert vols, f"no bound Longhorn volumes found for sandbox {sandbox!r} in {namespace!r}"
    for vol in vols:
        r = _kubectl(
            "-n",
            "longhorn-system",
            "patch",
            "volumes.longhorn.io",
            vol,
            "--type=merge",
            "-p",
            f'{{"spec":{{"numberOfReplicas":{count}}}}}',
        )
        assert r.returncode == 0, f"patch volume {vol}: {r.stderr}"
    placement: dict[str, list[str]] = {}
    deadline = time.time() + timeout
    for vol in vols:
        while True:
            r = _kubectl(
                "-n",
                "longhorn-system",
                "get",
                "replicas.longhorn.io",
                "-l",
                f"longhornvolume={vol}",
                "-o",
                "json",
            )
            items = json.loads(r.stdout).get("items", []) if r.returncode == 0 else []
            running = [i for i in items if i.get("status", {}).get("currentState") == "running"]
            if len(running) == count and len(items) == count:
                placement[vol] = sorted(i["spec"].get("nodeID") or "?" for i in running)
                break
            assert time.time() < deadline, (
                f"volume {vol}: wanted {count} running replicas, have "
                f"{len(running)} running / {len(items)} total after {timeout}s"
            )
            time.sleep(5)
    return placement


# ---------------------------------------------------------------------------
# Aggregation + final CSV emission.
# ---------------------------------------------------------------------------


def _logs_to_csv(logs: list[Path], csv_path: Path) -> None:
    rows: list[dict[str, str]] = []
    for log in logs:
        if not log.exists():
            continue
        # The label is encoded in the filename: bench-<label>-<ts>.log
        name = log.name
        assert name.startswith("bench-") and name.endswith(".log"), name
        label = name.removeprefix("bench-").rsplit("-", 1)[0]
        for line in log.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 3:
                continue
            op, run, secs = parts
            rows.append({"label": label, "operation": op, "run": run, "seconds": secs})
    with csv_path.open("w") as f:
        w = csv.DictWriter(f, fieldnames=["label", "operation", "run", "seconds"])
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------
# Tests — one per environment + a final aggregation.
# ---------------------------------------------------------------------------


def _envs_requested() -> set[str]:
    raw = os.environ.get("K7_BENCH_ENVS", "host,k7-fd,k7-ql-r1,k7-ql-r2,k7-ql-r3")
    return {e.strip() for e in raw.split(",") if e.strip()}


@pytest.fixture(scope="session")
def bench_out_dir() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR


@pytest.fixture(scope="session")
def bench_logs() -> list[Path]:
    return []


@pytest.fixture(scope="session", autouse=True)
def _emit_csv_on_session_finish(bench_out_dir: Path, bench_logs: list[Path]):
    yield
    if not bench_logs:
        return
    csv_path = bench_out_dir / f"bench-results-{_ts()}.csv"
    _logs_to_csv(bench_logs, csv_path)
    print(f"\n[bench] aggregated CSV → {csv_path}")
    print(f"[bench] render with: uv run python bench/docker-perf/render.py --input {csv_path} --title '...'")


def _maybe_skip(env: str) -> None:
    if env not in _envs_requested():
        pytest.skip(f"env '{env}' not in K7_BENCH_ENVS")


def test_bench_host(bench_out_dir: Path, bench_logs: list[Path]):
    _maybe_skip("host")
    log = _bench_host(bench_out_dir)
    bench_logs.append(log)
    print(f"\n[bench] host log → {log}")


@pytest.mark.firecracker
async def test_bench_k7_fd(
    k7_core: K7Core,
    test_namespace: str,
    bench_out_dir: Path,
    bench_logs: list[Path],
):
    _maybe_skip("k7-fd")
    log = await _bench_sandbox(
        k7_core,
        label="k7-fd",
        namespace=test_namespace,
        backend="kata-firecracker-devmapper",
        backend_extra={"docker_data_path": "k7-docker-lvm-overlay2"},
        out_dir=bench_out_dir,
        docker=True,
    )
    bench_logs.append(log)
    print(f"\n[bench] k7-fd log → {log}")


async def _bench_ql(
    k7_core: K7Core,
    namespace: str,
    out_dir: Path,
    bench_logs: list[Path],
    replicas: int,
) -> None:
    label = f"k7-ql-r{replicas}"
    log = await _bench_sandbox(
        k7_core,
        label=label,
        namespace=namespace,
        backend="kata-qemu-longhorn",
        backend_extra={"longhorn_replicas": str(replicas), "docker_data_path": "longhorn-block-overlay2"},
        out_dir=out_dir,
        longhorn_replicas=replicas,
        docker=True,
    )
    bench_logs.append(log)
    print(f"\n[bench] {label} log → {log}")


@pytest.mark.qemu
async def test_bench_k7_ql_r1(
    k7_core: K7Core,
    test_namespace: str,
    bench_out_dir: Path,
    bench_logs: list[Path],
):
    _maybe_skip("k7-ql-r1")
    await _bench_ql(k7_core, test_namespace, bench_out_dir, bench_logs, replicas=1)


@pytest.mark.qemu
async def test_bench_k7_ql_r2(
    k7_core: K7Core,
    test_namespace: str,
    bench_out_dir: Path,
    bench_logs: list[Path],
):
    _maybe_skip("k7-ql-r2")
    await _bench_ql(k7_core, test_namespace, bench_out_dir, bench_logs, replicas=2)


@pytest.mark.qemu
async def test_bench_k7_ql_r3(
    k7_core: K7Core,
    test_namespace: str,
    bench_out_dir: Path,
    bench_logs: list[Path],
):
    """Longhorn r=3 — the full redundancy cost on a 3-node HA cluster."""
    _maybe_skip("k7-ql-r3")
    await _bench_ql(k7_core, test_namespace, bench_out_dir, bench_logs, replicas=3)


@pytest.mark.k7d
async def test_bench_k7d(
    k7_core: K7Core,
    test_namespace: str,
    bench_out_dir: Path,
    bench_logs: list[Path],
):
    """k7d first-class --docker — overlay2 on a virtio-blk graph disk.

    Graph is no longer guest tmpfs, so the 3Gi memory ceiling that blocked
    no-cache builds is gone. Fork of a warm engine is timed after run_io.
    """
    _maybe_skip("k7d")
    log = await _bench_sandbox(
        k7_core,
        label="k7d",
        namespace=test_namespace,
        backend="k7d",
        backend_extra={"docker_data_path": "virtio-blk-scratch"},
        out_dir=bench_out_dir,
        limits={"memory": "3Gi", "cpu": "4"},
        docker=True,
    )
    bench_logs.append(log)
    print(f"\n[bench] k7d log → {log}")


@pytest.mark.k7d
async def test_bench_k7d_fc(
    k7_core: K7Core,
    test_namespace: str,
    bench_out_dir: Path,
    bench_logs: list[Path],
):
    """k7d-fc --docker — same guest service, RuntimeClass k7-fc."""
    _maybe_skip("k7d-fc")
    present = subprocess.run(
        ["k3s", "kubectl", "get", "runtimeclass", "k7-fc"],
        capture_output=True,
        text=True,
        check=False,
    )
    if present.returncode != 0:
        pytest.skip("RuntimeClass k7-fc not registered")
    log = await _bench_sandbox(
        k7_core,
        label="k7d-fc",
        namespace=test_namespace,
        backend="k7d-fc",
        backend_extra={"docker_data_path": "virtio-blk-scratch", "runtime_class": "k7-fc"},
        out_dir=bench_out_dir,
        limits={"memory": "3Gi", "cpu": "4"},
        docker=True,
    )
    bench_logs.append(log)
    print(f"\n[bench] k7d-fc log → {log}")
