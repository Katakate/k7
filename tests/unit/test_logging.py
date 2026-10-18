"""Structured logs: JSON shape, request ids, redaction, CLI golden output."""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from k7 import logging as k7_logging
from k7 import metrics
from k7.api.keystore import FileKeyStore
from k7.api.main import app
from k7.core.models import OperationResult

TOKEN = "k7_abcd1234_" + "S" * 32
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
LOGGERS = (
    "",
    "k7",
    "k7.core",
    "k7.api",
    "k7.agent",
    "k7.audit",
    "k7.op",
    "k7.keystore",
    "k7.hooks",
    "uvicorn.error",
    "httpx",
)


@pytest.fixture(autouse=True)
def _restore_json_logging():
    yield
    k7_logging.configure("json", "info")


def _json_lines(err: str) -> list[dict]:
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def test_json_line_has_every_field(capsys):
    k7_logging.configure("json", "info")
    k7_logging.new_context(request_id="req-12345678", tenant="acme", sandbox="sb1")
    logging.getLogger("k7.core").info("hello %s", "world", extra={"duration_ms": 12.5})
    (line,) = _json_lines(capsys.readouterr().err)
    assert set(line) >= {"ts", "level", "logger", "msg", *k7_logging.FIELDS}
    assert line["ts"].endswith("+00:00") and "T" in line["ts"]
    assert (line["level"], line["logger"], line["msg"]) == ("info", "k7.core", "hello world")
    assert (line["request_id"], line["tenant"], line["sandbox"], line["duration_ms"]) == (
        "req-12345678",
        "acme",
        "sb1",
        12.5,
    )
    assert line["backend"] is None and "exc" not in line


def test_uvicorn_color_message_is_dropped(capsys):
    k7_logging.configure("json", "info")
    logging.getLogger("uvicorn.error").info("Uvicorn running", extra={"color_message": "\x1b[1mUvicorn\x1b[0m"})
    (line,) = _json_lines(capsys.readouterr().err)
    assert line["msg"] == "Uvicorn running" and "color_message" not in line


def test_traceback_only_at_error(capsys):
    k7_logging.configure("json", "debug")
    log = logging.getLogger("k7.core")
    try:
        raise ValueError("boom")
    except ValueError:
        log.warning("warned", exc_info=True)
        log.exception("failed")
    warned, failed = _json_lines(capsys.readouterr().err)
    assert "exc" not in warned
    assert "ValueError: boom" in failed["exc"]


def test_level_and_format_are_required(monkeypatch):
    monkeypatch.delenv("K7_LOG_LEVEL")
    with pytest.raises(RuntimeError, match="K7_LOG_LEVEL"):
        k7_logging.configure_from_env()
    with pytest.raises(RuntimeError, match="K7_LOG_FORMAT"):
        k7_logging.configure("text", "info")
    with pytest.raises(RuntimeError, match="K7_LOG_LEVEL"):
        k7_logging.configure("json", "verbose")


def test_request_id_accepted_or_minted():
    assert k7_logging.start_request("abcd-1234-EFGH") == "abcd-1234-EFGH"
    for bad in (None, "", "short", "has space in it", "x" * 65, "semi;colon-12345"):
        minted = k7_logging.start_request(bad)
        assert uuid.UUID(minted).version == 7
    assert k7_logging.request_id() == minted


def test_uuid7_is_time_ordered():
    ids = [k7_logging.uuid7() for _ in range(50)]
    assert all(uuid.UUID(i).version == 7 and uuid.UUID(i).variant == uuid.RFC_4122 for i in ids)
    first, last = (uuid.UUID(i).int >> 80 for i in (ids[0], ids[-1]))
    assert 0 <= last - first < 1000 and abs(first - time.time() * 1000) < 5000


async def test_context_is_per_task(capsys):
    k7_logging.configure("json", "info")
    log = logging.getLogger("k7.core")

    async def request(rid: str, backend: str) -> None:
        k7_logging.start_request(rid)
        await asyncio.sleep(0)
        k7_logging.bind(backend=backend)
        await asyncio.sleep(0)
        log.info("op")

    await asyncio.gather(request("req-aaaaaaaa", "k7d"), request("req-bbbbbbbb", "kata-qemu-longhorn"))
    lines = _json_lines(capsys.readouterr().err)
    assert {(line["request_id"], line["backend"]) for line in lines} == {
        ("req-aaaaaaaa", "k7d"),
        ("req-bbbbbbbb", "kata-qemu-longhorn"),
    }


@pytest.mark.parametrize("fmt", ["json", "human"])
def test_tokens_are_scrubbed_through_every_logger(fmt, capsys):
    k7_logging.configure(fmt, "debug")
    k7_logging.new_context(request_id="req-12345678", note=f"ctx {TOKEN}")
    for name in LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.warning("token %s and bearer %s", TOKEN, JWT)
        lg.warning(f"inline {TOKEN}", extra={"field": JWT})
    out = capsys.readouterr()
    blob = out.err + out.out
    assert "[REDACTED]" in blob
    assert TOKEN not in blob and JWT not in blob and "S" * 32 not in blob
    for name in LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_cli_golden_output(capsys):
    """The CLI keeps the legacy ``print(..., file=sys.stderr)`` text, emoji included."""
    k7_logging.configure("human", "info")
    from k7.core.core import K7Core

    core = K7Core()
    core._config_loaded = True
    custom = MagicMock()
    err = ApiException(status=403, reason="Forbidden")
    custom.list_namespaced_custom_object = AsyncMock(side_effect=err)
    with patch.object(K7Core, "_get_custom_objects_client", AsyncMock(return_value=custom)):
        assert asyncio.run(core.list_snapshots(namespace="team-a")) == []
    logging.getLogger("k7.core").info("⏱️  PVC %s bound in %.2fs", "pvc-1", 1.234)
    logging.getLogger("k7.op").info("delete ok")  # per-op summaries are for collectors only
    logging.getLogger("httpx").info("HTTP Request: GET http://agent")  # libraries stay at last-resort
    logging.getLogger("k7.core").debug("never at info")
    assert capsys.readouterr().err == f"⚠️  Failed to list VolumeSnapshots: {err}\n⏱️  PVC pvc-1 bound in 1.23s\n"


async def test_api_request_id_in_header_log_and_audit(tmp_path: Path, capsys):
    k7_logging.configure("json", "info")
    key = "k7-logging-test-key-0123456789"
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({hashlib.sha256(key.encode()).hexdigest(): {"name": "logger", "role": "admin"}}))
    rid = "trace-0123456789"
    with patch("k7.api.main.STORE", FileKeyStore(keys)), patch("k7.api.main.K7Core") as core_cls:
        core_cls.return_value.delete_sandbox = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.delete(
                "/api/v1/sandboxes/sb1", params={"namespace": "team-a"}, headers={"X-API-Key": key, "X-Request-ID": rid}
            )
            minted = await client.get("/api/v1/sandboxes", headers={"X-API-Key": "wrong", "X-Request-ID": "bad id"})
    assert r.status_code == 200 and r.headers["X-Request-ID"] == rid
    minted_id = minted.headers["X-Request-ID"]
    assert uuid.UUID(minted_id).version == 7
    out = capsys.readouterr()
    audit = [json.loads(line) for line in out.out.splitlines()]
    assert audit[0]["logger"] == "k7.audit" and audit[0]["request_id"] == rid
    # A rejected GET is one audit line on stdout. The access line is a separate
    # k7.api record on stderr that shares the request id (kubectl shows both).
    (rejected,) = [line for line in audit if line["request_id"] == minted_id]
    assert (rejected["logger"], rejected["status"], rejected["method"]) == ("k7.audit", 401, "GET")
    (rejected_access,) = [
        line for line in _json_lines(out.err) if line["request_id"] == minted_id and line["logger"] == "k7.api"
    ]
    assert rejected_access["status"] == 401
    assert set(audit[0]) >= {"ts", "level", "msg", *k7_logging.FIELDS}
    assert (audit[0]["level"], audit[0]["msg"]) == ("info", "audit DELETE /api/v1/sandboxes/{name} 200")
    (access,) = [line for line in _json_lines(out.err) if line["logger"] == "k7.api" and line["request_id"] == rid]
    assert (access["route"], access["status"], access["namespace"], access["sandbox"]) == (
        "/api/v1/sandboxes/{name}",
        200,
        "team-a",
        "sb1",
    )
    assert access["client_ip"] == "127.0.0.1"
    assert key not in out.out + out.err


async def test_exec_logs_hash_not_command(tmp_path: Path, capsys):
    from k7.core.models import ExecResult

    k7_logging.configure("json", "debug")
    key = "k7-logging-test-key-0123456789"
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({hashlib.sha256(key.encode()).hexdigest(): {"name": "logger", "role": "admin"}}))
    cmd = "echo SECRET_PAYLOAD_99"
    with patch("k7.api.main.STORE", FileKeyStore(keys)), patch("k7.api.main.K7Core") as core_cls:
        core_cls.return_value.exec_command = AsyncMock(
            return_value=ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/v1/sandboxes/sb1/exec", json={"command": cmd}, headers={"X-API-Key": key})
    assert r.status_code == 200
    out = capsys.readouterr()
    (access,) = [line for line in _json_lines(out.err) if line["logger"] == "k7.api"]
    assert access["command_sha256"] == hashlib.sha256(cmd.encode()).hexdigest()
    assert access["command_len"] == len(cmd)
    assert "SECRET_PAYLOAD_99" not in out.out + out.err


async def test_agent_logs_forwarded_request_id(tmp_path: Path, monkeypatch, capsys):
    from k7.api import agent as agent_mod

    k7_logging.configure("json", "info")
    (tmp_path / "token").write_text("agent-secret")
    monkeypatch.setattr(agent_mod, "AGENT_TOKEN_FILE", str(tmp_path / "token"))
    vm = {"vm_id": "vm-1", "status": "sandbox_found", "sandbox_id": "cri"}
    with patch("k7.api.agent._lookup_local_vm", new=AsyncMock(return_value=vm)):
        transport = httpx.ASGITransport(app=agent_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
            r = await client.post(
                "/agent/v1/vm/lookup",
                json={"name": "sb", "namespace": "ns", "pod_name": "sb-pod"},
                headers={"X-K7-Agent-Token": "agent-secret", "X-Request-ID": "fork-across-nodes-1"},
            )
    assert r.status_code == 200, r.text
    (line,) = [line for line in _json_lines(capsys.readouterr().err) if line["logger"] == "k7.agent"]
    assert (line["request_id"], line["route"], line["sandbox"]) == ("fork-across-nodes-1", "/agent/v1/vm/lookup", "sb")


def test_core_forwards_request_id_to_agents():
    k7_logging.start_request("fork-across-nodes-2")
    from k7.core.core import K7Core

    assert K7Core._agent_headers("tok") == {"X-K7-Agent-Token": "tok", "X-Request-ID": "fork-across-nodes-2"}


def test_op_summary_line_in_json(capsys):
    k7_logging.configure("json", "info")
    k7_logging.new_context(request_id="req-12345678")

    @metrics.observed("pause")
    async def pause():
        k7_logging.bind(backend="k7d")
        return OperationResult(success=False, error="Sandbox x not found")

    asyncio.run(pause())
    (line,) = [line for line in _json_lines(capsys.readouterr().err) if line["logger"] == "k7.op"]
    assert (line["msg"], line["op"], line["backend"], line["error_code"]) == (
        "pause failed",
        "pause",
        "k7d",
        "NotFound",
    )
    assert isinstance(line["duration_ms"], float)


async def test_request_id_reaches_the_remote_agent_on_the_wire(monkeypatch):
    from k7.core import core as core_mod

    seen: list[tuple[str, str | None, str | None]] = []

    def agent(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("X-Request-ID"), request.headers.get("X-K7-Agent-Token")))
        return httpx.Response(200, json={"success": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(core_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(agent), **kw))
    monkeypatch.delenv("K7_AGENT", raising=False)
    core = core_mod.K7Core()
    core._k7d_agent_base_url = AsyncMock(return_value="http://10.42.1.7:8000")
    monkeypatch.setattr(core_mod.K7Core, "_k7d_agent_token", staticmethod(lambda: "agent-secret"))
    k7_logging.start_request("fork-across-nodes-3")
    assert (await core._k7d_forward_vm_op("node-b", "fork", {"name": "sb"}, timeout=5)).success
    nodes = MagicMock(items=[MagicMock(metadata=MagicMock())])
    nodes.items[0].metadata.name = "node-b"
    core._get_core_v1_client = AsyncMock(return_value=MagicMock(list_node=AsyncMock(return_value=nodes)))
    assert await core.nodes_storage() == {"node-b": {"success": True}}
    assert seen == [
        ("/agent/v1/vm/fork", "fork-across-nodes-3", "agent-secret"),
        ("/agent/v1/storage", "fork-across-nodes-3", "agent-secret"),
    ]


async def test_snapshot_gc_logs_json_lines(capsys):
    from k7.api import snapshot_gc

    record = {"namespace": "team-a", "name": "fork-snap-1", "age": "0:20:00", "deleted": True}
    core = MagicMock(aclose=AsyncMock())
    core.gc_snapshots = AsyncMock(return_value=OperationResult(success=True, message="1 deleted", data=[record]))
    with patch.object(snapshot_gc, "K7Core", return_value=core):
        assert await snapshot_gc._main() == 0
        core.gc_snapshots = AsyncMock(return_value=OperationResult(success=False, error="list failed"))
        assert await snapshot_gc._main() == 1
    lines = [line for line in _json_lines(capsys.readouterr().err) if line["logger"] == "k7.snapshot_gc"]
    assert [(line["level"], line["msg"]) for line in lines] == [
        ("info", "1 deleted"),
        ("info", "  [deleted] team-a/fork-snap-1 (age=0:20:00)"),
        ("error", "❌ snapshot-gc failed: list failed"),
    ]
    assert {line["op"] for line in lines} == {"snapshot-gc"} and len({line["request_id"] for line in lines}) == 2
    assert core.aclose.await_count == 2


def test_snapshot_gc_requires_log_env(monkeypatch):
    from k7.api import snapshot_gc

    monkeypatch.delenv("K7_LOG_FORMAT")
    with pytest.raises(RuntimeError, match="K7_LOG_FORMAT"):
        asyncio.run(snapshot_gc._main())


def test_chart_hooks_log_human_lines(capsys):
    from k7.api import chart_hooks

    async def run(command: str) -> None:
        logging.getLogger("k7.hooks").info(f"{command}: preflight ok")
        raise chart_hooks.HookError("Secret k7-system/k7-agent-token exists without an agent_token key")

    with patch.object(chart_hooks, "run", run):
        assert chart_hooks.main(["install"]) == 1
    assert capsys.readouterr().err == (
        "install: preflight ok\n"
        "k7 chart hook install: FAILED: Secret k7-system/k7-agent-token exists without an agent_token key\n"
    )
