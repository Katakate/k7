"""k7-agent is node-local only: no Kubernetes writes, fork refused."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from k7.api import agent as agent_mod
from k7.api.agent import app

TOKEN = "agent-secret"


def _client(tmp_path: Path, monkeypatch):
    token_file = tmp_path / "agent_token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN_FILE", str(token_file))
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
