"""k7-agent is node-local only: no Kubernetes writes, fork refused."""

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from k7.api import agent as agent_mod
from k7.api.agent import app
from k7.metering import config as metering_config
from k7.metering.node import NodeMetering

TOKEN = "agent-secret"


def _client(tmp_path: Path, monkeypatch):
    token_file = tmp_path / "agent_token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN_FILE", str(token_file))
    cfg = metering_config.load()
    monkeypatch.setattr(
        agent_mod, "METERING", NodeMetering(cfg, metering_config.cluster_id(), "node-a", tmp_path / "spool")
    )
    return httpx.ASGITransport(app=app)


async def test_fork_refused(tmp_path: Path, monkeypatch):
    transport = _client(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        r = await client.post(
            "/agent/v1/vm/fork",
            json={"name": "src", "new_name": "dst"},
            headers={"X-K7-Agent-Token": TOKEN},
        )
    assert r.status_code == 400
    assert "control-plane" in r.json()["detail"]


async def test_pause_is_crictl_and_k7d_only(tmp_path: Path, monkeypatch):
    transport = _client(tmp_path, monkeypatch)
    vm = {"vm_id": "vm-1", "status": "sandbox_found", "sandbox_id": "cri"}
    with (
        patch("k7.api.agent._lookup_local_vm", new=AsyncMock(return_value=vm)),
        patch("k7.api.agent.K7Core") as core_cls,
    ):
        core_cls.return_value._k7d_request = AsyncMock(return_value={"status": "ok"})
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            r = await client.post(
                "/agent/v1/vm/pause",
                json={"name": "sb", "namespace": "ns", "pod_name": "sb-pod"},
                headers={"X-K7-Agent-Token": TOKEN},
            )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    core_cls.return_value._k7d_request.assert_awaited_once_with({"op": "pause_vm", "vm_id": "vm-1"})


async def test_pause_requires_pod_name(tmp_path: Path, monkeypatch):
    transport = _client(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        r = await client.post(
            "/agent/v1/vm/pause",
            json={"name": "sb"},
            headers={"X-K7-Agent-Token": TOKEN},
        )
    assert r.status_code == 400
    assert "pod_name" in r.json()["detail"]


_LVS = {
    "report": [
        {
            "lv": [
                {
                    "lv_name": "thin-pool",
                    "lv_attr": "twi-aotz--",
                    "lv_size": "1000",
                    "data_percent": "12.5",
                    "metadata_percent": "3.0",
                }
            ]
        }
    ]
}


async def _storage(tmp_path: Path, monkeypatch, *, age: float | None) -> httpx.Response:
    report = tmp_path / "storage.json"
    if age is not None:
        report.write_text(json.dumps(_LVS))
        os.utime(report, (time.time() - age, time.time() - age))
    monkeypatch.setattr(agent_mod, "STORAGE_REPORT", str(report))
    monkeypatch.setattr(agent_mod, "_k7d_disks", lambda: {"path": "/var/lib/k7d/disks"})
    transport = _client(tmp_path, monkeypatch)
    with patch("k7.api.agent.subprocess.run") as run:
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            r = await client.get("/agent/v1/storage", headers={"X-K7-Agent-Token": TOKEN})
    run.assert_not_called()  # never shells out to lvs
    return r


async def test_storage_reads_fresh_host_report(tmp_path: Path, monkeypatch):
    r = await _storage(tmp_path, monkeypatch, age=5)
    assert r.status_code == 200, r.text
    assert r.json()["kata_thinpool"] == {
        "vg": "kata-vg",
        "lv": "thin-pool",
        "size_bytes": 1000,
        "data_percent": 12.5,
        "metadata_percent": 3.0,
    }


async def test_stale_storage_report_is_500_with_age(tmp_path: Path, monkeypatch):
    r = await _storage(tmp_path, monkeypatch, age=300)
    assert r.status_code == 500
    msg = r.json()["detail"]
    assert "old" in msg and ("299s" in msg or "300s" in msg)


async def test_missing_storage_report_is_500(tmp_path: Path, monkeypatch):
    r = await _storage(tmp_path, monkeypatch, age=None)
    assert r.status_code == 500
    assert "k7-storage-report.timer" in r.json()["detail"]


async def _get_storage(tmp_path: Path, monkeypatch, token_text: str | None, header: str = TOKEN) -> httpx.Response:
    token_file = tmp_path / "agent_token"
    if token_text is not None:
        token_file.write_text(token_text)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN_FILE", str(token_file))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as client:
        return await client.get("/agent/v1/storage", headers={"X-K7-Agent-Token": header})


async def test_unreadable_or_empty_token_file_is_a_loud_500(tmp_path: Path, monkeypatch):
    r = await _get_storage(tmp_path, monkeypatch, None)
    assert r.status_code == 500 and "unreadable" in r.json()["detail"] and "k7 install" in r.json()["detail"]
    r = await _get_storage(tmp_path, monkeypatch, "  \n")
    assert r.status_code == 500 and "is empty" in r.json()["detail"]


async def test_wrong_or_missing_agent_token_is_401(tmp_path: Path, monkeypatch):
    assert (await _get_storage(tmp_path, monkeypatch, TOKEN, header="nope")).status_code == 401
    assert (await _get_storage(tmp_path, monkeypatch, TOKEN, header="")).status_code == 401


async def test_lookup_returns_the_cri_sandbox_and_resume_restarts_vcpus(tmp_path: Path, monkeypatch):
    transport = _client(tmp_path, monkeypatch)
    with patch("k7.api.agent.K7Core") as core_cls:
        core = core_cls.return_value
        core._k7d_cri_sandbox_id = AsyncMock(return_value="cri-1")
        core._k7d_request = AsyncMock(
            side_effect=lambda req: (
                {"status": "sandbox_found", "vm_id": "vm-1"} if req["op"] == "lookup_sandbox" else {}
            )
        )
        body = {"name": "sb", "namespace": "ns", "pod_name": "sb-pod"}
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            lookup = await client.post("/agent/v1/vm/lookup", json=body, headers={"X-K7-Agent-Token": TOKEN})
            resume = await client.post("/agent/v1/vm/resume", json=body, headers={"X-K7-Agent-Token": TOKEN})
    assert lookup.json() == {"status": "sandbox_found", "vm_id": "vm-1", "sandbox_id": "cri-1"}
    assert resume.status_code == 200 and resume.json()["success"] is True
    core._k7d_cri_sandbox_id.assert_awaited_with("sb-pod", "ns")
    assert [c.args[0]["op"] for c in core._k7d_request.await_args_list] == [
        "lookup_sandbox",
        "lookup_sandbox",
        "resume_vm",
    ]


async def test_unexpected_k7d_lookup_reply_is_500(tmp_path: Path, monkeypatch):
    _client(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with patch("k7.api.agent.K7Core") as core_cls:
        core_cls.return_value._k7d_cri_sandbox_id = AsyncMock(return_value="cri-1")
        core_cls.return_value._k7d_request = AsyncMock(return_value={"status": "sandbox_not_found"})
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            r = await client.post(
                "/agent/v1/vm/pause",
                json={"name": "sb", "pod_name": "sb-pod"},
                headers={"X-K7-Agent-Token": TOKEN},
            )
    assert r.status_code == 500
    assert "unexpected k7d lookup_sandbox response" in r.json()["error"]["message"]


async def test_report_without_a_thin_pool_is_500(tmp_path: Path, monkeypatch):
    report = tmp_path / "storage.json"
    report.write_text(json.dumps({"report": [{"lv": [{"lv_name": "root", "lv_attr": "-wi-ao----"}]}]}))
    monkeypatch.setattr(agent_mod, "STORAGE_REPORT", str(report))
    r = await _get_storage(tmp_path, monkeypatch, TOKEN)
    assert r.status_code == 500 and "no thin pool LV found in VG kata-vg" in r.json()["detail"]


def _df(monkeypatch, tmp_path: Path, returncode: int, stdout: str = "", stderr: str = "") -> None:
    monkeypatch.setattr(agent_mod, "K7D_DISKS_DIR", str(tmp_path))
    monkeypatch.setenv("K7_NODE_NAME", "n1")
    result = type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()
    monkeypatch.setattr(agent_mod.subprocess, "run", lambda *a, **kw: result)


def test_k7d_disks_parses_df(tmp_path: Path, monkeypatch):
    _df(monkeypatch, tmp_path, 0, "  1B-blocks  Used Avail\n 1000 250 750\n")
    assert agent_mod._k7d_disks() == {
        "path": str(tmp_path),
        "size_bytes": 1000,
        "used_bytes": 250,
        "avail_bytes": 750,
        "used_percent": 25.0,
    }
    _df(monkeypatch, tmp_path, 0, "h\n0 0 0\n")
    assert agent_mod._k7d_disks()["used_percent"] == 0.0


def test_k7d_disks_failures_are_loud(tmp_path: Path, monkeypatch):
    _df(monkeypatch, tmp_path, 1, stderr="df: boom\n")
    with pytest.raises(HTTPException, match="failed on node n1: df: boom"):
        agent_mod._k7d_disks()
    monkeypatch.setattr(agent_mod, "K7D_DISKS_DIR", str(tmp_path / "absent"))
    with pytest.raises(HTTPException) as exc:
        agent_mod._k7d_disks()
    assert exc.value.status_code == 500 and "is the k7d backend installed" in str(exc.value.detail)
