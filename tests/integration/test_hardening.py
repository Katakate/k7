"""Unprivileged k7-agent on a live cluster, host-free
k7-api, and the host storage report the agent serves."""

import asyncio
import os
import subprocess
import time

import pytest

from k7.core.core import K7Core

from .cluster import system_ns
from .test_k7d_api import _kubectl_json

pytestmark = pytest.mark.integration

REPORT = "/run/k7/storage.json"


def _pod_spec(label: str) -> dict:
    pods = _kubectl_json("get", "pods", "-n", system_ns(), "-l", f"app={label}")["items"]
    assert pods, f"no {label} pods"
    return pods[0]["spec"]


def test_agent_pod_is_unprivileged():
    spec = _pod_spec("k7-agent")
    sc = spec["containers"][0]["securityContext"]
    assert not sc.get("privileged"), sc
    assert sc["capabilities"] == {"drop": ["ALL"]} and sc["readOnlyRootFilesystem"] is True
    paths = {v["hostPath"]["path"] for v in spec["volumes"] if "hostPath" in v}
    assert "/dev" not in paths, paths


def test_api_pod_has_no_hostpath_and_runs_as_1000():
    spec = _pod_spec("k7-api")
    assert not [v for v in spec["volumes"] if "hostPath" in v]
    api = next(c for c in spec["containers"] if c["name"] == "k7-api")
    assert api["securityContext"]["runAsUser"] == 1000


K7D_GID = 770
K7D_SOCK = "/run/k7d/k7d.sock"


@pytest.mark.skipif(
    subprocess.run(["getent", "group", "k7d"], capture_output=True).returncode != 0, reason="k7d not installed here"
)
def test_k7d_group_and_socket_use_the_pinned_gid():
    """The agent reaches k7d.sock only through the pinned gid in its supplementalGroups."""
    gid = int(subprocess.run(["getent", "group", "k7d"], capture_output=True, text=True).stdout.split(":")[2])
    assert gid == K7D_GID
    if os.path.exists(K7D_SOCK):
        st = os.stat(K7D_SOCK)
        assert st.st_gid == K7D_GID and st.st_mode & 0o060 == 0o060, oct(st.st_mode)


def test_agent_pod_carries_the_k7d_gid():
    assert K7D_GID in _pod_spec("k7-agent")["securityContext"]["supplementalGroups"]


def test_api_is_replicated_behind_a_pdb():
    dep = _kubectl_json("get", "deployment", "k7-api", "-n", system_ns())
    pdb = _kubectl_json("get", "pdb", "k7-api", "-n", system_ns())["spec"]
    assert pdb["selector"]["matchLabels"] == {"app": "k7-api"}
    if dep["spec"]["replicas"] > 1:
        assert pdb.get("minAvailable") == 1, pdb
    else:
        assert pdb.get("maxUnavailable") == 1, pdb


def _nodes_storage() -> dict:
    return asyncio.run(K7Core().nodes_storage())


@pytest.mark.skipif(not os.path.exists(REPORT), reason="kfd backend not installed on this node")
def test_storage_report_fresh_and_stale_is_loud():
    node = subprocess.run(["hostname"], capture_output=True, text=True, check=True).stdout.strip()
    assert "kata_thinpool" in _nodes_storage()[node], "fresh report should serve thin-pool stats"
    # Age the report; the timer rewrites it within 30 s, so try/finally only
    # needs to force that refresh.
    old = time.time() - 600
    try:
        os.utime(REPORT, (old, old))
        entry = _nodes_storage()[node]
        assert "old" in entry.get("error", ""), entry
    finally:
        subprocess.run(["systemctl", "start", "k7-storage-report.service"], check=True)
    assert "kata_thinpool" in _nodes_storage()[node]
