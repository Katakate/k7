"""In-sandbox network probes whose verdict is the probe command's exit status.

Probes used to print an ``&& echo OK || echo BLOCKED`` sentinel and parse
stdout. When an exec stream ends before the shell writes the sentinel, the
verdict is neither, and a policy that was enforced correctly reads as a
failure. The exit status comes from the Kubernetes exec status frame, which is
separate from the output streams. "No status" means the probe itself failed;
it never means the destination is blocked.
"""

from k7.core.core import EXEC_NO_STATUS, K7Core


def wget(url: str, timeout: int) -> str:
    return f"wget -q -T {timeout} -O /dev/null {url}"


async def reachable(k7_core: K7Core, sandbox: str, cmd: str, namespace: str) -> bool:
    r = await k7_core.exec_command(sandbox, cmd, namespace=namespace)
    assert r.exit_code != EXEC_NO_STATUS, f"probe in {sandbox} has no exit status ({cmd!r}): {r.stderr!r}"
    return r.exit_code == 0


async def verdict(k7_core: K7Core, sandbox: str, cmd: str, namespace: str) -> str:
    return "OK" if await reachable(k7_core, sandbox, cmd, namespace) else "BLOCKED"
