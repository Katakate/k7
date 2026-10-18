"""The packaged Nuitka onefile must accept a standalone ``-c``.

Nuitka's self-execution guard is on unless the build passes
``--no-deployment-flag=self-execution``. It exits 2, before Python
starts, when argv contains a standalone ``-c`` — ``k7 exec NAME -- sh
-c '...'`` and ``-- ls -c /``. The source CLI does not.

This runs the binary ``make build`` writes to ``dist/k7`` (or
``K7_PACKAGED_BIN``). It skips when that binary has not been built.
``K7_REQUIRE_PACKAGED_CLI=1`` fails instead of skipping.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_FLAG = "--no-deployment-flag=self-execution"
_SELF_EXEC = "tried to call itself with '-c'"


def test_nuitka_builds_pass_no_self_execution() -> None:
    """Both Nuitka entry points pass the flag. The binary test below is the acceptance check."""
    dockerfile = (REPO / "src/k7/cli/Dockerfile.cli").read_text()
    rules = (REPO / "debian/rules").read_text()
    assert _FLAG in dockerfile
    assert _FLAG in rules
    # The docker path is what `make build` runs; the local path is the fallback.
    assert dockerfile.count(_FLAG) == 1
    assert rules.count(_FLAG) == 1


def _packaged_binary() -> Path:
    override = os.environ.get("K7_PACKAGED_BIN")
    path = Path(override) if override else REPO / "dist" / "k7"
    require = bool(override) or os.environ.get("K7_REQUIRE_PACKAGED_CLI") == "1"
    if not path.is_file():
        reason = f"packaged Nuitka onefile not at {path} (make build, or set K7_PACKAGED_BIN)"
        if require:
            pytest.fail(reason)
        pytest.skip(reason)
    with path.open("rb") as fh:
        magic = fh.read(4)
    if not os.access(path, os.X_OK) or magic != b"\x7fELF":
        pytest.fail(f"{path} is not an executable ELF (expected the Nuitka onefile)")
    return path


@pytest.mark.parametrize(
    "guest",
    (
        ["sh", "-c", "exit 7"],
        ["ls", "-c", "/"],
    ),
    ids=["sh-c", "ls-c"],
)
def test_packaged_binary_accepts_standalone_dash_c(guest: list[str], tmp_path: Path) -> None:
    """``-c`` reaches the CLI. A dead local API is the proof; the guard never gets that far."""
    binary = _packaged_binary()
    env = os.environ.copy()
    for key in ("K7_API_URL", "K7_API_KEY", "K7_API_CA"):
        env.pop(key, None)
    env["K7_CONFIG_DIR"] = str(tmp_path)
    proc = subprocess.run(
        [
            str(binary),
            "--api-url",
            "http://127.0.0.1:9",
            "--api-key",
            "k7-not-a-key",
            "exec",
            "x",
            "--",
            *guest,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert _SELF_EXEC not in output, output
    assert proc.returncode == 1, output
    assert "Could not reach the K7 API" in output, output
