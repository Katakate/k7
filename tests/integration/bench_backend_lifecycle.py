"""Backend lifecycle benchmark — kql (Longhorn disk snapshots) vs k7d (warm VM fork).

Measures, per backend, wall-clock time for:

- ``create``        — ``create_sandbox()`` call + pod Ready
- ``exec``          — ``echo`` round-trip in a Ready sandbox
- ``snapshot``      — named snapshot until ready (kql only; k7d rejects it)
- ``fork_call``     — the ``fork_sandbox()`` call itself
- ``fork_ready``    — fork call + forked pod Ready + exec answering
- ``pause``         — pause until effective (kql: pods gone; k7d: VM frozen)
- ``resume``        — resume until an exec answers again
- ``delete``        — ``delete_sandbox()`` call
- ``sidecar_*``     — docker-in-VM sidecar: create+docker-ready, docker pull, docker run

Same node, same images, interleaved runs. Invocation (on the k7 node):

    K7_BENCH_BACKENDS=kata-qemu-longhorn,k7d K7_BENCH_REPS=3 \
    uv run pytest -m bench tests/integration/bench_backend_lifecycle.py -v -s

Results land as a markdown table on stdout and JSON under ``$K7_BENCH_OUT``
(default ``/tmp``), one file per run: ``bench-backends-<ts>.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

pytestmark = pytest.mark.bench

REPS = int(os.environ.get("K7_BENCH_REPS", "3"))
EXEC_REPS = int(os.environ.get("K7_BENCH_EXEC_REPS", "10"))
OUT_DIR = Path(os.environ.get("K7_BENCH_OUT", "/tmp"))
BACKENDS = [b.strip() for b in os.environ.get("K7_BENCH_BACKENDS", "kata-qemu-longhorn,k7d").split(",") if b.strip()]

SANDBOX_IMAGE = "alpine:3.20"
SIDECAR_IMAGE = "docker:27.5-cli"
DOCKER_PULL_IMAGE = "alpine:3.21"


def _pod_ready(sandbox: str, namespace: str) -> bool:
    result = subprocess.run(
        [
            "k3s",
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={sandbox}",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        items = json.loads(result.stdout).get("items", [])
    except json.JSONDecodeError:
        return False
    for pod in items:
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if statuses and all(s.get("ready") for s in statuses):
            return True
    return False


def _wait(predicate, timeout: float, what: str, interval: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what} after {timeout}s")


def _no_pods(sandbox: str, namespace: str) -> bool:
    result = subprocess.run(
        ["k3s", "kubectl", "get", "pods", "-n", namespace, "-l", f"app={sandbox}", "--no-headers"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


async def _exec_ok(core: K7Core, sandbox: str, namespace: str, cmd: str = "echo bench-ok") -> bool:
    try:
        result = await core.exec_command(sandbox, cmd, namespace=namespace)
        return result.exit_code == 0
    except Exception:
        return False


async def _wait_exec(core: K7Core, sandbox: str, namespace: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _exec_ok(core, sandbox, namespace):
            return
        await asyncio.sleep(1)
    raise TimeoutError(f"exec in {sandbox} never answered within {timeout}s")


class Recorder:
    def __init__(self) -> None:
        self.samples: dict[tuple[str, str], list[float]] = {}

    def add(self, backend: str, metric: str, seconds: float) -> None:
        self.samples.setdefault((backend, metric), []).append(seconds)
        print(f"    [{backend}] {metric}: {seconds:.2f}s", flush=True)

    def table(self) -> str:
        metrics: list[str] = []
        for _, metric in self.samples:
            if metric not in metrics:
                metrics.append(metric)
        lines = [
            "| Metric | " + " | ".join(BACKENDS) + " |",
            "|--------|" + "|".join(["------"] * len(BACKENDS)) + "|",
        ]
        for metric in metrics:
            row = [metric]
            for backend in BACKENDS:
                vals = self.samples.get((backend, metric))
                if vals:
                    med = statistics.median(vals)
                    row.append(f"{med:.2f}s (n={len(vals)})")
                else:
                    row.append("n/a")
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    def dump(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = OUT_DIR / f"bench-backends-{ts}.json"
        payload = {
            "backends": BACKENDS,
            "reps": REPS,
            "sandbox_image": SANDBOX_IMAGE,
            "samples": {f"{b}/{m}": v for (b, m), v in self.samples.items()},
        }
        out.write_text(json.dumps(payload, indent=2))
        return out


async def _bench_lifecycle(core: K7Core, recorder: Recorder, backend: str, namespace: str, rep: int) -> None:
    name = f"bench-{backend.split('-')[-1][:4]}-{rep}"
    fork_name = f"{name}-fork"

    # --- create → Ready ---
    start = time.monotonic()
    result = await core.create_sandbox(
        SandboxConfig(name=name, image=SANDBOX_IMAGE, namespace=namespace, backend=backend)
    )
    assert result.success, f"create failed: {result.error}"
    _wait(lambda: _pod_ready(name, namespace), 300, f"{name} Ready")
    recorder.add(backend, "create_to_ready", time.monotonic() - start)

    try:
        # --- exec round-trips ---
        exec_times = []
        for _ in range(EXEC_REPS):
            t0 = time.monotonic()
            assert await _exec_ok(core, name, namespace)
            exec_times.append(time.monotonic() - t0)
        recorder.add(backend, "exec_median", statistics.median(exec_times))

        # seed state the fork should inherit
        await core.exec_command(name, "echo inherited > /tmp/bench-marker", namespace=namespace)

        # --- named snapshot (kql only — k7d rejects loudly by design) ---
        if backend == "kata-qemu-longhorn":
            snap_name = f"{name}-snap"
            t0 = time.monotonic()
            snap = await core.create_snapshot(name, snap_name, namespace=namespace)
            assert snap.success, f"snapshot failed: {snap.error}"
            ready = await core._wait_for_snapshot_ready(snap_name, namespace=namespace)
            assert ready.success, f"snapshot never ready: {ready.error}"
            recorder.add(backend, "snapshot_ready", time.monotonic() - t0)
            await core.delete_snapshot(snap_name, namespace=namespace)

        # --- fork ---
        t0 = time.monotonic()
        fork = await core.fork_sandbox(name, fork_name, namespace=namespace)
        assert fork.success, f"fork failed: {fork.error}"
        fork_call = time.monotonic() - t0
        recorder.add(backend, "fork_call", fork_call)
        _wait(lambda: _pod_ready(fork_name, namespace), 300, f"{fork_name} Ready")
        await _wait_exec(core, fork_name, namespace)
        recorder.add(backend, "fork_to_ready", time.monotonic() - t0)

        if backend == "k7d":
            inherited = await core.exec_command(fork_name, "cat /tmp/bench-marker", namespace=namespace)
            assert inherited.exit_code == 0 and "inherited" in inherited.stdout, (
                "k7d fork lost the source's in-memory state"
            )

        await core.delete_sandbox(fork_name, namespace=namespace)

        # --- pause / resume ---
        t0 = time.monotonic()
        pause = await core.pause_sandbox(name, namespace=namespace)
        assert pause.success, f"pause failed: {pause.error}"
        if backend != "k7d":
            _wait(lambda: _no_pods(name, namespace), 180, f"{name} pods gone")
        recorder.add(backend, "pause_effective", time.monotonic() - t0)

        t0 = time.monotonic()
        resume = await core.resume_sandbox(name, namespace=namespace)
        assert resume.success, f"resume failed: {resume.error}"
        await _wait_exec(core, name, namespace, timeout=300)
        recorder.add(backend, "resume_to_exec", time.monotonic() - t0)
    finally:
        t0 = time.monotonic()
        await core.delete_sandbox(name, namespace=namespace)
        recorder.add(backend, "delete", time.monotonic() - t0)


async def _bench_sidecar(core: K7Core, recorder: Recorder, backend: str, namespace: str, rep: int) -> None:
    name = f"bench-dind-{backend.split('-')[-1][:4]}-{rep}"

    start = time.monotonic()
    result = await core.create_sandbox(
        SandboxConfig(name=name, image=SIDECAR_IMAGE, namespace=namespace, backend=backend, sidecar="docker")
    )
    assert result.success, f"sidecar create failed: {result.error}"
    _wait(lambda: _pod_ready(name, namespace), 300, f"{name} Ready")

    async def docker_ready() -> bool:
        probe = await core.exec_command(name, "docker info >/dev/null 2>&1 && echo ok", namespace=namespace)
        return probe.exit_code == 0 and "ok" in probe.stdout

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if await docker_ready():
            break
        await asyncio.sleep(2)
    else:
        raise TimeoutError(f"docker daemon in {name} never became ready")
    recorder.add(backend, "sidecar_create_to_docker_ready", time.monotonic() - start)

    try:
        t0 = time.monotonic()
        pull = await core.exec_command(name, f"docker pull {DOCKER_PULL_IMAGE}", namespace=namespace)
        assert pull.exit_code == 0, f"docker pull failed: {pull.stderr}"
        recorder.add(backend, "sidecar_docker_pull", time.monotonic() - t0)

        t0 = time.monotonic()
        run = await core.exec_command(name, f"docker run --rm {DOCKER_PULL_IMAGE} echo dind-ok", namespace=namespace)
        assert run.exit_code == 0 and "dind-ok" in run.stdout, f"docker run failed: {run.stderr}"
        recorder.add(backend, "sidecar_docker_run", time.monotonic() - t0)
    finally:
        await core.delete_sandbox(name, namespace=namespace)


async def test_bench_backend_lifecycle(k7_core: K7Core, test_namespace: str):
    recorder = Recorder()
    print(f"\nBenchmarking backends {BACKENDS} — {REPS} rep(s), image {SANDBOX_IMAGE}", flush=True)
    for rep in range(REPS):
        for backend in BACKENDS:
            print(f"  rep {rep + 1}/{REPS} backend={backend}", flush=True)
            await _bench_lifecycle(k7_core, recorder, backend, test_namespace, rep)
    for backend in BACKENDS:
        print(f"  sidecar backend={backend}", flush=True)
        await _bench_sidecar(k7_core, recorder, backend, test_namespace, 0)

    out = recorder.dump()
    print("\n== Backend lifecycle benchmark (median) ==\n", flush=True)
    print(recorder.table(), flush=True)
    print(f"\nraw samples: {out}", flush=True)
