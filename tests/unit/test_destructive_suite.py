"""The opt-in destructive integration phase and the RKE2 node script, checked
without a cluster: the gate, the playbook tasks it kills at, the host files it
expects the chart uninstall to remove, and rke2-node.sh's step plumbing."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.integration import test_destructive as destructive

REPO = Path(__file__).resolve().parents[2]
ROLES = REPO / "src/k7/deploy/roles"
CI = REPO / "deploy/helm/k7/ci"
RKE2_NODE = CI / "rke2-node.sh"


def test_destructive_tests_skip_unless_asked_for():
    env = {k: v for k, v in os.environ.items() if k != "K7_DESTRUCTIVE"}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "destructive", "-q", "--no-cov", "-p", "no:cacheprovider", "-rs"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^(\d+) skipped", r.stdout.splitlines()[-1]), r.stdout
    assert "passed" not in r.stdout.splitlines()[-1] and "failed" not in r.stdout.splitlines()[-1]
    assert "destructive: set K7_DESTRUCTIVE=1" in r.stdout


def test_destructive_tests_are_also_integration_tests():
    assert {m.name for m in destructive.pytestmark} == {"integration", "destructive"}


def _task_names(role: str) -> set[str]:
    names = set()
    for f in (ROLES / role / "tasks").glob("*.yml"):
        names |= {t["name"] for t in yaml.safe_load(f.read_text()) or [] if "name" in t}
    return names


def test_install_kill_points_name_real_playbook_tasks():
    source = Path(destructive.__file__).read_text()
    points = re.findall(r'r"\^TASK \\\[(\w+) : ([^"]+)"', source)
    assert len(points) == 3, points
    for role, prefix in points:
        assert any(n.startswith(prefix) for n in _task_names(role)), f"no task {prefix!r} in role {role}"


def test_uninstall_host_checks_are_what_the_cleanup_removes():
    cleanup = (ROLES / "k7_node_prep/tasks/cleanup.yml").read_text()
    rke2 = yaml.safe_load((ROLES / "k7_node_prep/vars/rke2.yml").read_text())
    entrypoint = (REPO / "src/k7/deploy/node_installer/entrypoint.py").read_text()
    for path in destructive.HOST_LEFTOVERS:
        removed = path in cleanup or path == rke2["k7_containerd_config_path"]
        removed |= path == "/var/lib/k7/installer-state.json" and '"var/lib/k7/installer-state.json"' in entrypoint
        assert removed, f"{path} is not removed by node cleanup"


def _rke2_node(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(RKE2_NODE), *args], capture_output=True, text=True)


@pytest.mark.parametrize(("args", "msg"), [([], "usage:"), (["chart", "deploy"], "unknown step: deploy")])
def test_rke2_node_refuses_bad_steps_before_running_any(args: list[str], msg: str):
    r = _rke2_node(*args)
    assert r.returncode == 2 and msg in r.stderr
    assert "==> rke2-node.sh" not in r.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="as root the step would run")
def test_rke2_node_needs_root():
    r = _rke2_node("packages")
    assert r.returncode == 1 and "run as root" in r.stderr


def _steps() -> list[str]:
    m = re.search(r"^STEPS=\(([^)]*)\)$", RKE2_NODE.read_text(), re.M)
    assert m
    return m.group(1).split()


def test_rke2_node_defines_every_step_and_the_runbook_runs_each():
    script = RKE2_NODE.read_text()
    runbook = (CI / "rke2-hetzner.md").read_text()
    ran = {s for line in re.findall(r"^deploy/helm/k7/ci/rke2-node\.sh (.+)$", runbook, re.M) for s in line.split()}
    for step in _steps():
        assert re.search(rf"^step_{step}\(\) {{$", script, re.M), step
    assert ran == set(_steps())


def test_rke2_node_downloads_are_checksummed():
    script = re.sub(r"\\\n\s*", "", RKE2_NODE.read_text())
    assert len(re.findall(r"\bcurl -", script)) == 1, "download only through fetch(), which checks the sha256"
    fetches = re.findall(r"^\s*fetch (\S+) (\S+) \"\$(\w+)\"$", script, re.M)
    assert len(fetches) == script.count("\n    fetch "), fetches
    for url, _, var in fetches:
        assert re.search(rf"^{var}=[0-9a-f]{{64}}$", script, re.M), var
        assert "latest" not in url, url
