"""Unit tests for the k7_sdk client (sync + async).

These tests verify URL/body construction and response unwrapping for the
pause/resume/fork SDK additions from Spec 10a. The HTTP layer is mocked so
the tests run without a live API.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from k7_sdk.client import AsyncClient, Client, SandboxProxy


def _mock_response(payload: dict, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


# --- Client (sync) -----------------------------------------------------------


class TestSyncClientPause:
    def test_pause_default_args(self):
        c = Client(endpoint="http://api.test", api_key="k")
        with patch.object(
            c.session,
            "post",
            return_value=_mock_response({"data": {"message": "ok"}}),
        ) as post:
            out = c.pause("demo")
        assert out == {"message": "ok"}
        post.assert_called_once()
        url, kwargs = post.call_args[0][0], post.call_args[1]
        assert url == "http://api.test/api/v1/sandboxes/demo/pause"
        assert kwargs["json"] == {"namespace": "default"}

    def test_pause_with_snapshot(self):
        c = Client(endpoint="http://api.test", api_key="k")
        with patch.object(
            c.session,
            "post",
            return_value=_mock_response({"data": {"message": "ok"}}),
        ) as post:
            c.pause("demo", namespace="ns", snapshot="snap1")
        body = post.call_args[1]["json"]
        assert body == {"namespace": "ns", "snapshot": "snap1"}

    def test_pause_does_not_accept_legacy_kwargs(self):
        """Spec 10c: pvc and snapshot_class were removed from the SDK surface."""
        import inspect

        sig = inspect.signature(Client.pause)
        assert "pvc" not in sig.parameters
        assert "snapshot_class" not in sig.parameters


class TestSyncClientResume:
    def test_resume_posts_namespace(self):
        c = Client(endpoint="http://api.test", api_key="k")
        with patch.object(
            c.session,
            "post",
            return_value=_mock_response({"data": {"message": "ok"}}),
        ) as post:
            out = c.resume("demo", namespace="ns")
        assert out == {"message": "ok"}
        assert post.call_args[0][0] == "http://api.test/api/v1/sandboxes/demo/resume"
        assert post.call_args[1]["json"] == {"namespace": "ns"}


class TestSyncClientFork:
    def test_fork_returns_proxy(self):
        c = Client(endpoint="http://api.test", api_key="k")
        with patch.object(
            c.session,
            "post",
            return_value=_mock_response({"data": {"name": "child", "source": "demo"}}),
        ) as post:
            proxy = c.fork("demo", "child", namespace="ns", snapshot="snap1")
        assert isinstance(proxy, SandboxProxy)
        assert proxy.name == "child"
        assert proxy.namespace == "ns"
        assert proxy._client is c
        assert post.call_args[0][0] == "http://api.test/api/v1/sandboxes/demo/fork"
        assert post.call_args[1]["json"] == {
            "new_name": "child",
            "namespace": "ns",
            "snapshot": "snap1",
        }

    def test_fork_without_snapshot(self):
        c = Client(endpoint="http://api.test", api_key="k")
        with patch.object(
            c.session,
            "post",
            return_value=_mock_response({"data": {"name": "child"}}),
        ) as post:
            c.fork("demo", "child")
        body = post.call_args[1]["json"]
        assert body == {"new_name": "child", "namespace": "default"}
        assert "snapshot" not in body


# --- SandboxProxy ergonomics ------------------------------------------------


class TestSandboxProxyOps:
    def test_pause_delegates_to_client(self):
        c = Client(endpoint="http://api.test", api_key="k")
        proxy = SandboxProxy("demo", "ns", c)
        with patch.object(c, "pause", return_value={"message": "ok"}) as m:
            proxy.pause(snapshot="snap1")
        m.assert_called_once_with("demo", namespace="ns", snapshot="snap1")

    def test_resume_delegates_to_client(self):
        c = Client(endpoint="http://api.test", api_key="k")
        proxy = SandboxProxy("demo", "ns", c)
        with patch.object(c, "resume", return_value={"message": "ok"}) as m:
            proxy.resume()
        m.assert_called_once_with("demo", namespace="ns")

    def test_fork_delegates_and_returns_proxy(self):
        c = Client(endpoint="http://api.test", api_key="k")
        proxy = SandboxProxy("demo", "ns", c)
        returned_proxy = SandboxProxy("child", "ns", c)
        with patch.object(c, "fork", return_value=returned_proxy) as m:
            out = proxy.fork("child")
        assert out is returned_proxy
        m.assert_called_once_with("demo", "child", namespace="ns", snapshot=None)


# --- AsyncClient -------------------------------------------------------------


class TestAsyncClient:
    async def test_pause_builds_body(self):
        ac = AsyncClient(endpoint="http://api.test", api_key="k")
        try:
            post = AsyncMock(return_value=_mock_response({"data": {"message": "ok"}}))
            with patch.object(ac._client, "post", post):
                out = await ac.pause("demo", namespace="ns", snapshot="snap")
            assert out == {"message": "ok"}
            assert post.call_args[0][0] == "/api/v1/sandboxes/demo/pause"
            assert post.call_args[1]["json"] == {"namespace": "ns", "snapshot": "snap"}
        finally:
            await ac.aclose()

    async def test_resume(self):
        ac = AsyncClient(endpoint="http://api.test", api_key="k")
        try:
            post = AsyncMock(return_value=_mock_response({"data": {"message": "ok"}}))
            with patch.object(ac._client, "post", post):
                out = await ac.resume("demo")
            assert out == {"message": "ok"}
            assert post.call_args[0][0] == "/api/v1/sandboxes/demo/resume"
            assert post.call_args[1]["json"] == {"namespace": "default"}
        finally:
            await ac.aclose()

    async def test_fork_returns_data(self):
        ac = AsyncClient(endpoint="http://api.test", api_key="k")
        try:
            post = AsyncMock(return_value=_mock_response({"data": {"name": "child", "source": "demo"}}, 201))
            with patch.object(ac._client, "post", post):
                out = await ac.fork("demo", "child")
            assert out["name"] == "child"
            assert out["source"] == "demo"
            body = post.call_args[1]["json"]
            assert body == {"new_name": "child", "namespace": "default"}
        finally:
            await ac.aclose()


# --- Smoke: AsyncClient construction requires httpx --------------------------


def test_async_client_requires_httpx(monkeypatch):
    """When httpx is unavailable, AsyncClient raises RuntimeError loudly."""
    import k7_sdk.client as mod

    monkeypatch.setattr(mod, "httpx", None)
    with pytest.raises(RuntimeError, match="httpx is required"):
        AsyncClient(endpoint="http://api.test", api_key="k")
