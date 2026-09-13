"""Unit tests for ``k8s-object-presence.sh``.

Unknown (timeout / 503 / unable to handle) must never classify as absent,
or the playbook would run ``cilium install`` on a live cluster.
"""

import os
import stat
import subprocess
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2] / "src/k7/deploy/k8s-object-presence.sh"
PLAYBOOK = Path(__file__).resolve().parents[2] / "src/k7/deploy/k7-install-node.yaml"


def _run(kubectl: Path, retries: str = "3", delay: str = "0") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["K7_KUBECTL"] = str(kubectl)
    env["K7_PRESENCE_RETRIES"] = retries
    env["K7_PRESENCE_DELAY"] = delay
    return subprocess.run(
        ["bash", str(HELPER), "kube-system", "daemonset", "cilium"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write_kubectl(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "kubectl"
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_helper_is_executable_script():
    assert HELPER.is_file()
    assert HELPER.stat().st_mode & stat.S_IXUSR


def test_present_on_success(tmp_path: Path):
    kubectl = _write_kubectl(tmp_path, "echo NAME READY; exit 0\n")
    result = _run(kubectl)
    assert result.returncode == 0
    assert result.stdout.strip() == "present"


def test_absent_on_not_found(tmp_path: Path):
    kubectl = _write_kubectl(
        tmp_path,
        "echo 'Error from server (NotFound): daemonsets.apps \"cilium\" not found' >&2; exit 1\n",
    )
    result = _run(kubectl)
    assert result.returncode == 0
    assert result.stdout.strip() == "absent"


def test_unknown_on_unable_to_handle_does_not_look_absent(tmp_path: Path):
    kubectl = _write_kubectl(
        tmp_path,
        'echo "The connection to the server localhost:6443 was refused" >&2; exit 1\n',
    )
    result = _run(kubectl, retries="2", delay="0")
    assert result.returncode == 2
    assert "present" not in result.stdout
    assert "absent" not in result.stdout
    assert "unknown" in result.stderr


def test_unknown_on_timeout(tmp_path: Path):
    kubectl = _write_kubectl(
        tmp_path, 'echo "Unable to connect to the server: net/http: request canceled" >&2; exit 1\n'
    )
    result = _run(kubectl, retries="2", delay="0")
    assert result.returncode == 2
    assert result.stdout.strip() != "absent"


def test_unknown_on_503(tmp_path: Path):
    kubectl = _write_kubectl(
        tmp_path,
        'echo "Error from server (ServiceUnavailable): the server is currently unable to handle the request" >&2; exit 1\n',
    )
    result = _run(kubectl, retries="2", delay="0")
    assert result.returncode == 2
    assert result.stdout.strip() != "absent"


def test_retries_then_present(tmp_path: Path):
    state = tmp_path / "n"
    state.write_text("0")
    kubectl = _write_kubectl(
        tmp_path,
        f"""
n=$(cat '{state}')
n=$((n + 1))
echo "$n" > '{state}'
if [ "$n" -lt 3 ]; then
  echo "unable to handle the request" >&2
  exit 1
fi
exit 0
""",
    )
    result = _run(kubectl, retries="5", delay="0")
    assert result.returncode == 0
    assert result.stdout.strip() == "present"


def test_playbook_installs_cilium_only_when_absent():
    text = PLAYBOOK.read_text()
    assert "cilium_installed_check.rc != 0" not in text
    assert 'cilium_presence.stdout == "absent"' in text
    assert 'hubble_presence.stdout == "absent"' in text
    assert "cilium hubble enable" in text


def test_playbook_does_not_fail_reinstall_on_status_warnings_alone():
    text = PLAYBOOK.read_text()
    assert "Confirm Cilium agent is Ready (reinstall)" in text
    assert 'cilium_presence.stdout == "present"' in text
    # First-install wait is still the strict --wait path.
    assert "Wait for Cilium to report ready (first install)" in text
