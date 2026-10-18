"""exec returns a command's complete output on every backend.

Over the v4 exec subprotocol, Kata sandboxes lost everything after the first
read of a large output (often exactly 1024 bytes) while still reporting exit 0:
20-95% of 10 kB execs on kfd and kql. See ``EXEC_WS_PROTOCOL`` in core.py.
"""

import asyncio
import os
import subprocess
import time

import pytest

from k7.core.core import K7Core
from k7.core.models import SandboxConfig

from .cluster import KUBECTL

pytestmark = pytest.mark.integration

_LOCAL_NODE = os.uname().nodename
_BACKENDS = ["kata-firecracker-devmapper", "kata-qemu-longhorn", "k7d", "k7d-fc"]
_EXECS = 50
# ~11 kB per stream, byte-exact so a dropped or reordered chunk shows.
_SEQ = 2500
_EXPECTED = "".join(f"{i}\n" for i in range(1, _SEQ + 1))


def _node_has_backend(backend: str) -> bool:
    label = f"jsonpath={{.metadata.labels.k7\\.katakate\\.org/backend-{backend}}}"
    out = subprocess.run([*KUBECTL, "get", "node", _LOCAL_NODE, "-o", label], capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == "true"


async def _wait_ready(k7_core: K7Core, name: str, namespace: str, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(s.name == name and s.ready == "True" for s in await k7_core.list_sandboxes(namespace=namespace)):
            return
        await asyncio.sleep(3)
    raise AssertionError(f"Sandbox {name} never became Ready within {timeout}s")


@pytest.mark.parametrize("backend", _BACKENDS)
async def test_large_exec_output_is_complete(k7_core: K7Core, test_namespace: str, backend: str):
    if not _node_has_backend(backend):
        pytest.skip(f"node {_LOCAL_NODE} lacks label k7.katakate.org/backend-{backend}=true")
    short = {"kata-firecracker-devmapper": "kfd", "kata-qemu-longhorn": "kql"}.get(backend, backend)
    name = f"exo-{short}"
    cfg = SandboxConfig(
        name=name, image="alpine:3.20", namespace=test_namespace, backend=backend, node_name=_LOCAL_NODE
    )
    result = await k7_core.create_sandbox(cfg)
    assert result.success, f"create {name} failed: {result.error}"
    try:
        await _wait_ready(k7_core, name, test_namespace)
        bad = []
        for i in range(_EXECS):
            # Alternate stdout-only and stdout+stderr so both streams are covered.
            both = i % 2 == 1
            cmd = f"seq {_SEQ}" + (f"; seq {_SEQ} >&2" if both else "")
            r = await k7_core.exec_command(name, cmd, namespace=test_namespace)
            if r.exit_code != 0 or r.stdout != _EXPECTED or (both and r.stderr != _EXPECTED):
                bad.append((i, r.exit_code, len(r.stdout), len(r.stderr), r.stderr[-200:] if not both else ""))
        assert not bad, f"[{backend}] {len(bad)}/{_EXECS} execs incomplete (i, exit, len out, len err, err): {bad}"
    finally:
        await k7_core.delete_sandbox(name, namespace=test_namespace)
