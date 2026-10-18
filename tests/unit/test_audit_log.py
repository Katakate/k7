"""Audit log: one JSON line per mutating / auth-failed request, no secrets, fail closed."""

import hashlib
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from k7 import logging as k7_logging
from k7.api.keystore import FileKeyStore
from k7.api.main import app, audit_log
from k7.core.models import ExecResult, OperationResult

TEST_KEY = "k7-audit-test-key-0123456789"
TEST_KEY_HASH = hashlib.sha256(TEST_KEY.encode()).hexdigest()
FIELDS = {
    "ts",
    "request_id",
    "key_id",
    "key_name",
    "principal_type",
    "subject",
    "role",
    "tenant",
    "idp_issuer",
    "method",
    "route",
    "namespace",
    "sandbox",
    "status",
    "latency_ms",
    "client_ip",
}


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(k7_logging.JsonFormatter().format(record))


@pytest.fixture()
def audit_lines():
    handler = _Capture()
    audit_log.addHandler(handler)
    try:
        yield handler.lines
    finally:
        audit_log.removeHandler(handler)


@pytest.fixture(autouse=True)
def _keys(tmp_path: Path):
    keys_file = tmp_path / "api_keys.json"
    entry = {"name": "auditor", "expires": int(time.time()) + 3600, "role": "admin"}
    keys_file.write_text(json.dumps({TEST_KEY_HASH: entry}))
    with patch("k7.api.main.STORE", FileKeyStore(keys_file)):
        yield


async def _request(method: str, url: str, peer: str = "127.0.0.1", **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, client=(peer, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


async def test_delete_emits_one_line_with_fields(audit_lines):
    with patch("k7.api.main.K7Core") as core_cls:
        core_cls.return_value.delete_sandbox = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        resp = await _request(
            "DELETE", "/api/v1/sandboxes/sb1", params={"namespace": "team-a"}, headers={"X-API-Key": TEST_KEY}
        )
    assert resp.status_code == 200
    assert len(audit_lines) == 1
    line = json.loads(audit_lines[0])
    assert set(line) >= FIELDS
    assert line["method"] == "DELETE"
    assert line["route"] == "/api/v1/sandboxes/{name}"
    assert line["namespace"] == "team-a"
    assert line["sandbox"] == "sb1"
    assert line["status"] == 200
    assert line["key_name"] == "auditor"
    assert line["key_id"] == line["subject"] == f"legacy-{TEST_KEY_HASH[:8]}"
    assert (line["principal_type"], line["role"], line["tenant"], line["idp_issuer"]) == ("key", "admin", None, None)
    assert "admin" not in line
    assert line["request_id"] == resp.headers["X-Request-ID"]


async def test_get_success_is_not_audited(audit_lines):
    with patch("k7.api.main.K7Core") as core_cls:
        core_cls.return_value.list_sandboxes = AsyncMock(return_value=[])
        resp = await _request("GET", "/api/v1/sandboxes", headers={"X-API-Key": TEST_KEY})
    assert resp.status_code == 200
    assert audit_lines == []


async def test_auth_failure_on_get_is_audited(audit_lines):
    resp = await _request("GET", "/api/v1/sandboxes", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401
    assert len(audit_lines) == 1
    line = json.loads(audit_lines[0])
    assert line["status"] == 401 and line["method"] == "GET"
    assert line["key_id"] is None


async def test_exec_logs_hash_never_command_or_token(audit_lines, capsys):
    cmd = "echo SECRET_PAYLOAD_42"
    with patch("k7.api.main.K7Core") as core_cls:
        core_cls.return_value.exec_command = AsyncMock(
            return_value=ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)
        )
        resp = await _request(
            "POST", "/api/v1/sandboxes/sb1/exec", json={"command": cmd}, headers={"X-API-Key": TEST_KEY}
        )
    assert resp.status_code == 200, resp.text
    line = json.loads(audit_lines[-1])
    assert line["command_sha256"] == hashlib.sha256(cmd.encode()).hexdigest()
    assert line["command_len"] == len(cmd)
    blob = "\n".join(audit_lines) + capsys.readouterr().out
    assert "SECRET_PAYLOAD_42" not in blob
    assert TEST_KEY not in blob


async def test_xff_trusted_only_from_localhost(audit_lines):
    resp = await _request("GET", "/api/v1/sandboxes", headers={"X-API-Key": "bad", "X-Forwarded-For": "6.6.6.6"})
    assert resp.status_code == 401
    # Peer 127.0.0.1 is the Caddy sidecar: its appended (last) XFF hop wins.
    assert json.loads(audit_lines[-1])["client_ip"] == "6.6.6.6"
    await _request(
        "GET", "/api/v1/sandboxes", peer="203.0.113.9", headers={"X-API-Key": "bad", "X-Forwarded-For": "6.6.6.6"}
    )
    assert json.loads(audit_lines[-1])["client_ip"] == "203.0.113.9"


async def test_emit_failure_fails_closed():
    with (
        patch("k7.api.main.K7Core") as core_cls,
        patch.object(audit_log, "info", side_effect=RuntimeError("disk full")),
    ):
        core_cls.return_value.delete_sandbox = AsyncMock(return_value=OperationResult(success=True, message="ok"))
        resp = await _request("DELETE", "/api/v1/sandboxes/sb1", headers={"X-API-Key": TEST_KEY})
    assert resp.status_code == 500
    assert "audit" in resp.json()["error"]["message"]


_FAIL = OperationResult(success=False, error="boom")
MUTATING = [
    ("POST", "/api/v1/sandboxes", {"name": "sb1", "image": "alpine"}, "/api/v1/sandboxes", {"sandbox": "sb1"}),
    ("DELETE", "/api/v1/sandboxes/sb1", None, "/api/v1/sandboxes/{name}", {"sandbox": "sb1"}),
    ("DELETE", "/api/v1/sandboxes", None, "/api/v1/sandboxes", {}),
    ("POST", "/api/v1/sandboxes/sb1/pause", {}, "/api/v1/sandboxes/{name}/pause", {"sandbox": "sb1"}),
    ("POST", "/api/v1/sandboxes/sb1/resume", {}, "/api/v1/sandboxes/{name}/resume", {"sandbox": "sb1"}),
    ("POST", "/api/v1/sandboxes/sb1/fork", {"new_name": "c"}, "/api/v1/sandboxes/{name}/fork", {"sandbox": "sb1"}),
    ("POST", "/api/v1/install", {"inventory": "x"}, "/api/v1/install", {}),
    (
        "POST",
        "/api/v1/sandboxes/sb1/snapshot",
        {"snapshot_name": "s1"},
        "/api/v1/sandboxes/{name}/snapshot",
        {"sandbox": "sb1", "snapshot": "s1"},
    ),
    ("DELETE", "/api/v1/snapshots/s1", None, "/api/v1/snapshots/{name}", {"snapshot": "s1"}),
    (
        "POST",
        "/api/v1/snapshots/s1/restore",
        {"new_sandbox_name": "r1"},
        "/api/v1/snapshots/{name}/restore",
        {"sandbox": "r1", "snapshot": "s1"},
    ),
    ("POST", "/api/v1/snapshots/gc", {}, "/api/v1/snapshots/gc", {}),
]


@pytest.mark.parametrize(("method", "url", "body", "route", "fields"), MUTATING)
async def test_every_mutating_route_emits_exactly_one_line(audit_lines, monkeypatch, method, url, body, route, fields):
    monkeypatch.delenv("K7_API_ENABLE_REMOTE_INSTALL", raising=False)
    with patch("k7.api.main.K7Core") as core_cls:
        for name in (
            "create_sandbox",
            "delete_sandbox",
            "delete_all_sandboxes",
            "pause_sandbox",
            "resume_sandbox",
            "fork_sandbox",
            "create_snapshot",
            "delete_snapshot",
            "restore_sandbox",
            "gc_snapshots",
        ):
            setattr(core_cls.return_value, name, AsyncMock(return_value=_FAIL))
        kwargs = {"json": body} if body is not None else {}
        resp = await _request(method, url, headers={"X-API-Key": TEST_KEY}, **kwargs)
    assert resp.status_code in (400, 404), resp.text
    assert len(audit_lines) == 1
    line = json.loads(audit_lines[0])
    assert (line["method"], line["route"], line["status"]) == (method, route, resp.status_code)
    assert line["key_name"] == "auditor" and line["role"] == "admin"
    assert {k: line[k] for k in fields} == fields
    assert TEST_KEY not in audit_lines[0]


async def test_forbidden_request_is_audited_with_the_key(audit_lines, tmp_path):
    keys_file = tmp_path / "scoped.json"
    entry = {"name": "scoped", "expires": int(time.time()) + 3600, "role": "user", "namespaces": ["team-a"]}
    keys_file.write_text(json.dumps({TEST_KEY_HASH: entry}))
    with patch("k7.api.main.STORE", FileKeyStore(keys_file)):
        resp = await _request(
            "GET", "/api/v1/sandboxes", params={"namespace": "team-b"}, headers={"X-API-Key": TEST_KEY}
        )
    assert resp.status_code == 403
    assert len(audit_lines) == 1
    line = json.loads(audit_lines[0])
    assert (line["status"], line["key_name"], line["namespace"]) == (403, "scoped", "team-b")


async def test_unhandled_exception_is_audited_as_500(audit_lines):
    with patch("k7.api.main.K7Core") as core_cls:
        core_cls.return_value.delete_sandbox = AsyncMock(side_effect=RuntimeError("kaboom"))
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/api/v1/sandboxes/sb1", headers={"X-API-Key": TEST_KEY})
    assert resp.status_code == 500
    line = json.loads(audit_lines[-1])
    assert (line["status"], line["route"], line["sandbox"]) == (500, "/api/v1/sandboxes/{name}", "sb1")
