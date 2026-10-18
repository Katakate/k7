"""Client side: `k7 login` (device flow and PKCE), the
0600 credentials cache, refresh, `k7 logout`, how the CLI picks a
credential, the admin CLI commands (`--api`, role-bindings, install
--oidc-*), and the SDK token provider."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from k7.api import oidc
from k7.cli import _oidc
from k7.cli._client import resolve_client
from k7.cli.k7 import app
from k7.core.models import OperationResult
from k7_sdk.client import AsyncClient, Client

runner = CliRunner()
API = "https://k7.example.com:31000"
ISSUER = "https://idp.example.com"
CFG = {"issuer": ISSUER, "client_id": "k7-cli", "audience": "k7-api"}
DOC = {
    "issuer": ISSUER,
    "token_endpoint": f"{ISSUER}/token",
    "device_authorization_endpoint": f"{ISSUER}/device/code",
    "authorization_endpoint": f"{ISSUER}/auth",
    "revocation_endpoint": f"{ISSUER}/revoke",
}


def unsigned_jwt(**claims) -> str:
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


def tokens(aud="k7-api", exp_in=3600, **extra) -> dict:
    exp = int(time.time()) + exp_in
    return {
        "access_token": unsigned_jwt(aud=aud, exp=exp, sub="alice"),
        "id_token": unsigned_jwt(aud="k7-cli", exp=exp, sub="alice"),
        "refresh_token": "r1",
        "expires_in": exp_in,
        **extra,
    }


class Resp:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("K7_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("K7_API_KEY", "K7_API_URL", "K7_API_CA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("k7.cli._client._ETC_API_ENDPOINT", tmp_path / "no-endpoint")
    monkeypatch.setattr("k7.cli._client._ETC_API_KEYS", tmp_path / "no-keys.json")
    return tmp_path / "cfg"


def _cached(**over) -> dict:
    entry = {**CFG, "token_endpoint": DOC["token_endpoint"], "revocation_endpoint": DOC["revocation_endpoint"]}
    entry.update(_oidc._stamp(tokens()))
    entry.update(over)
    _oidc._save({API: entry})
    return entry


# ── token selection, cache file ────────────────────────────────────


def test_pick_token_prefers_the_access_token_with_our_audience():
    t = tokens()
    assert _oidc.pick_token(t, "k7-api") == t["access_token"]
    assert _oidc.pick_token(t, "k7-cli") == t["id_token"]
    assert _oidc.pick_token({"access_token": unsigned_jwt(aud=["x", "k7-api"])}, "k7-api")
    with pytest.raises(RuntimeError, match="no JWT with audience 'nope'"):
        _oidc.pick_token(t, "nope")
    with pytest.raises(RuntimeError):
        _oidc.pick_token({"access_token": "opaque-token"}, "k7-api")


def test_credentials_file_is_0600_and_keyed_by_api_url(config_dir):
    _cached()
    p = config_dir / "credentials.json"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert list(json.loads(p.read_text())) == [API]


def test_discover_refuses_an_issuer_mismatch():
    with (
        patch("k7.cli._oidc.requests.get", return_value=Resp(body={**DOC, "issuer": "https://evil"})),
        pytest.raises(RuntimeError, match="evil"),
    ):
        _oidc.discover(ISSUER)


def test_discover_returns_the_matching_document():
    with patch("k7.cli._oidc.requests.get", return_value=Resp(body=DOC)) as get:
        assert _oidc.discover(ISSUER) == DOC
    assert get.call_args.args[0] == f"{ISSUER}/.well-known/openid-configuration"


# ── device flow ────────────────────────────────────────────────────


def test_device_flow_polls_through_pending_and_slow_down():
    start = {
        "device_code": "dc",
        "user_code": "ABCD-EFGH",
        "verification_uri": f"{ISSUER}/device",
        "interval": 1,
        "expires_in": 60,
    }
    post = MagicMock(
        side_effect=[
            Resp(body=start),
            Resp(400, {"error": "authorization_pending"}),
            Resp(400, {"error": "slow_down"}),
            Resp(body=tokens()),
        ]
    )
    sleeps: list[float] = []
    echoed: list[str] = []
    with patch("k7.cli._oidc.requests.post", post), patch("k7.cli._oidc.time.sleep", sleeps.append):
        got = _oidc.device_flow(DOC, "k7-cli", "openid groups", echoed.append)
    assert got["refresh_token"] == "r1"
    assert sleeps == [1, 1, 6], "slow_down adds 5s to the interval"
    assert any("ABCD-EFGH" in line for line in echoed)
    assert post.call_args_list[0].kwargs["data"] == {"client_id": "k7-cli", "scope": "openid groups"}
    assert post.call_args_list[1].kwargs["data"]["grant_type"] == _oidc.DEVICE_GRANT


@pytest.mark.parametrize(
    ("responses", "match"),
    [
        ([Resp(400, {"error": "invalid_client"})], "device authorization failed"),
        (
            [
                Resp(body={"device_code": "d", "user_code": "u", "verification_uri": "v", "interval": 1}),
                Resp(400, {"error": "access_denied"}),
            ],
            "access_denied",
        ),
        ([Resp(500, {})], "HTTP 500"),
    ],
)
def test_device_flow_failures_are_loud(responses, match):
    with (
        patch("k7.cli._oidc.requests.post", MagicMock(side_effect=responses)),
        patch("k7.cli._oidc.time.sleep"),
        pytest.raises(RuntimeError, match=match),
    ):
        _oidc.device_flow(DOC, "k7-cli", "openid", lambda _m: None)


def test_device_code_expiry_is_loud():
    start = {"device_code": "d", "user_code": "u", "verification_uri": "v", "interval": 1, "expires_in": 0}
    with (
        patch("k7.cli._oidc.requests.post", MagicMock(return_value=Resp(body=start))),
        patch("k7.cli._oidc.time.sleep"),
        pytest.raises(RuntimeError, match="expired before it was approved"),
    ):
        _oidc.device_flow(DOC, "k7-cli", "openid", lambda _m: None)


def test_device_flow_needs_the_endpoint():
    with pytest.raises(RuntimeError, match="--browser"):
        _oidc.device_flow({**DOC, "device_authorization_endpoint": None}, "k7-cli", "openid", lambda _m: None)


# ── browser flow (PKCE) ────────────────────────────────────────────


def _fake_browser(state_override: str | None = None, error: str | None = None):
    seen: dict = {}

    def open_url(url: str) -> bool:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen.update(q)
        params = {"error": error} if error else {"code": "auth-code", "state": state_override or q["state"]}
        target = f"{q['redirect_uri']}?{urllib.parse.urlencode(params)}"
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
        return True

    return open_url, seen


def test_browser_flow_uses_pkce_s256_and_localhost_redirect():
    open_url, seen = _fake_browser()
    post = MagicMock(return_value=tokens())
    with patch("k7.cli._oidc.webbrowser.open", open_url), patch("k7.cli._oidc._post", post):
        got = _oidc.browser_flow(DOC, "k7-cli", "openid", lambda _m: None)
    assert got["refresh_token"] == "r1"
    assert seen["code_challenge_method"] == "S256" and seen["response_type"] == "code"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:")
    sent = post.call_args.args[1]
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "auth-code"
    assert sent["redirect_uri"] == seen["redirect_uri"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == seen["code_challenge"]


@pytest.mark.parametrize(("state", "error"), [("forged-state", None), (None, "access_denied")])
def test_browser_flow_refuses_a_forged_state_or_an_idp_error(state, error):
    open_url, _ = _fake_browser(state_override=state, error=error)
    with (
        patch("k7.cli._oidc.webbrowser.open", open_url),
        patch("k7.cli._oidc._post") as post,
        pytest.raises(RuntimeError, match="browser login failed"),
    ):
        _oidc.browser_flow(DOC, "k7-cli", "openid", lambda _m: None)
    post.assert_not_called()


def test_browser_flow_code_exchange_failure_is_loud():
    open_url, _ = _fake_browser()
    with (
        patch("k7.cli._oidc.webbrowser.open", open_url),
        patch("k7.cli._oidc._post", return_value={"error": "invalid_grant"}),
        pytest.raises(RuntimeError, match="code exchange failed"),
    ):
        _oidc.browser_flow(DOC, "k7-cli", "openid", lambda _m: None)


# ── login / refresh / logout ───────────────────────────────────────


@pytest.mark.parametrize("browser", [False, True])
def test_login_caches_tokens_and_endpoints(browser, config_dir):
    flow = MagicMock(return_value=tokens())
    target = "browser_flow" if browser else "device_flow"
    with patch("k7.cli._oidc.discover", return_value=DOC), patch(f"k7.cli._oidc.{target}", flow):
        _oidc.login(API, CFG, browser=browser, scopes="openid", echo=lambda _m: None)
    entry = json.loads((config_dir / "credentials.json").read_text())[API]
    assert entry["token_endpoint"] == DOC["token_endpoint"] and entry["audience"] == "k7-api"
    assert entry["refresh_token"] == "r1" and entry["expires_at"] > time.time()


def test_login_refuses_tokens_without_our_audience(config_dir):
    with (
        patch("k7.cli._oidc.discover", return_value=DOC),
        patch("k7.cli._oidc.device_flow", return_value=tokens(aud="x")),
        pytest.raises(RuntimeError, match="audience"),
    ):
        _oidc.login(API, {**CFG, "audience": "k7-api"}, browser=False, scopes="openid", echo=lambda _m: None)
    assert not (config_dir / "credentials.json").exists()


def test_token_provider_without_a_login_is_none():
    assert _oidc.token_provider(API) is None


def test_token_provider_after_logout_requires_login():
    _cached()
    provider = _oidc.token_provider(API)
    assert provider is not None
    _oidc._save({})
    with pytest.raises(_oidc.LoginRequired, match="no cached login"):
        provider()


def test_token_provider_returns_a_fresh_token_without_network():
    entry = _cached()
    with patch("k7.cli._oidc.requests.post") as post:
        assert _oidc.token_provider(API)() == entry["access_token"]  # type: ignore[misc]
    post.assert_not_called()


def test_token_provider_refreshes_near_expiry_and_persists():
    _cached(**_oidc._stamp(tokens(exp_in=30)))
    new = tokens(refresh_token="r2")
    with patch("k7.cli._oidc.requests.post", return_value=Resp(body=new)) as post:
        got = _oidc.token_provider(API)()  # type: ignore[misc]
    assert got == new["access_token"]
    assert post.call_args.kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "r1",
        "client_id": "k7-cli",
    }
    assert _oidc._load()[API]["refresh_token"] == "r2"


@pytest.mark.parametrize(
    ("over", "resp", "match"),
    [
        ({"refresh_token": None}, None, "expired; run `k7 login`"),
        ({}, Resp(400, {"error": "invalid_grant"}), "invalid_grant"),
    ],
)
def test_token_provider_raises_login_required(over, resp, match):
    _cached(**{**_oidc._stamp(tokens(exp_in=-10)), **over})
    if over.get("refresh_token") is None and "refresh_token" in over:
        data = _oidc._load()
        data[API].pop("refresh_token")
        _oidc._save(data)
    with patch("k7.cli._oidc.requests.post", return_value=resp), pytest.raises(_oidc.LoginRequired, match=match):
        _oidc.token_provider(API)()  # type: ignore[misc]


def test_logout_deletes_the_cache_first_then_revokes(config_dir):
    _cached()
    calls: list[dict] = []

    def post(url, data, timeout):
        assert not (config_dir / "credentials.json").exists(), "cache must be gone before revocation"
        calls.append(data)
        return Resp()

    with patch("k7.cli._oidc.requests.post", post):
        msg = _oidc.logout(API)
    assert [c["token_type_hint"] for c in calls] == ["refresh_token", "access_token"]
    assert "revoked refresh_token, access_token" in msg


def test_logout_revocation_failure_is_loud_but_the_cache_is_gone(config_dir):
    _cached()
    with (
        patch("k7.cli._oidc.requests.post", return_value=Resp(503)),
        pytest.raises(RuntimeError, match="revoking the refresh_token failed"),
    ):
        _oidc.logout(API)
    assert not (config_dir / "credentials.json").exists()


def test_logout_keeps_other_apis_and_handles_no_login():
    _cached()
    data = _oidc._load()
    data["https://other"] = data[API]
    _oidc._save(data)
    with patch("k7.cli._oidc.requests.post", return_value=Resp()):
        _oidc.logout(API)
    assert list(_oidc._load()) == ["https://other"]
    assert _oidc.logout("https://nowhere") == "not logged in to https://nowhere"


# ── CLI: login/logout/whoami and credential choice ─────────────────


def test_k7_login_command_device_flow_then_whoami():
    me = {"type": "oidc", "subject": "alice", "grants": ["user on tenant acme"], "groups": ["acme-devs"]}
    with (
        patch("k7.cli.k7.requests.get", return_value=Resp(body={"data": CFG})) as get,
        patch("k7.cli._oidc.login") as login,
        patch("k7.cli.k7._oidc.token_provider", return_value=lambda: "jwt"),
        patch.object(Client, "whoami", return_value=me),
    ):
        result = runner.invoke(app, ["login", "--api", API])
    assert result.exit_code == 0, result.output
    assert get.call_args.args[0] == f"{API}/api/v1/auth/oidc"
    assert login.call_args.kwargs["browser"] is False and login.call_args.args[1] == CFG
    assert "Logged in to" in result.output and "user on tenant acme" in result.output


def test_k7_login_when_the_api_has_no_oidc_is_loud():
    with patch("k7.cli.k7.requests.get", return_value=Resp(404, {"error": {"message": "OIDC is not configured"}})):
        result = runner.invoke(app, ["login", "--api", API])
    assert result.exit_code == 1 and "OIDC is not configured" in result.output


def test_k7_login_needs_an_api_url():
    result = runner.invoke(app, ["login"])
    assert result.exit_code == 1 and "--api" in result.output


def test_k7_logout_command():
    _cached()
    with patch("k7.cli._oidc.requests.post", return_value=Resp()):
        result = runner.invoke(app, ["logout", "--api", API])
    assert result.exit_code == 0 and "logged out" in result.output


def test_explicit_key_wins_over_a_cached_login(monkeypatch):
    _cached()
    assert resolve_client(API, "k7_aaaaaaaa_x").session.headers["X-API-Key"] == "k7_aaaaaaaa_x"
    monkeypatch.setenv("K7_API_KEY", "k7_bbbbbbbb_y")
    assert resolve_client(API, None).session.headers["X-API-Key"] == "k7_bbbbbbbb_y"


def test_cached_login_is_used_when_no_key_is_set():
    entry = _cached()
    c = resolve_client(API, None)
    assert "X-API-Key" not in c.session.headers
    req = MagicMock(headers={})
    c.session.auth(req)  # type: ignore[misc, operator]
    assert req.headers["Authorization"] == f"Bearer {entry['access_token']}"


def test_expired_login_is_a_clean_cli_error():
    _cached(**_oidc._stamp(tokens(exp_in=-10)))
    with patch("k7.cli._oidc.requests.post", return_value=Resp(400, {"error": "invalid_grant"})):
        result = runner.invoke(app, ["--api-url", API, "whoami"])
    assert result.exit_code == 1 and "run `k7 login`" in result.output


def test_whoami_command():
    with patch.object(
        Client, "whoami", return_value={"type": "key", "subject": "abc", "grants": ["admin"], "groups": []}
    ):
        result = runner.invoke(app, ["--api-url", API, "--api-key", "k", "whoami"])
    assert result.exit_code == 0 and "key abc: admin" in result.output


# ── CLI: admin commands over the API ───────────────────────────────


def _api(*args: str):
    return runner.invoke(app, ["--api-url", API, "--api-key", "k7_aaaaaaaa_x", *args])


def test_generate_api_key_over_the_api():
    created = {"id": "abcdefgh", "token": "k7_abcdefgh_s", "role": "user", "tenant": "acme", "expires": 2_000_000_000}
    with patch.object(Client, "create_api_key", return_value=created) as create:
        result = _api("generate-api-key", "ci", "--role", "user", "--tenant", "acme", "--expires-days", "7", "--api")
    assert result.exit_code == 0, result.output
    create.assert_called_once_with("ci", role="user", tenant="acme", expires_days=7, namespaces=[], nodes=[])
    assert "k7_abcdefgh_s" in result.output and "Role: user on tenant acme" in result.output


def test_list_and_revoke_api_keys_over_the_api():
    rows = [
        {
            "id": "aaaaaaaa",
            "name": "old",
            "role": "admin",
            "unscoped_legacy": True,
            "created": 1,
            "expires": 2,
            "last_used": "3",
        }
    ]
    with (
        patch.object(Client, "list_api_keys", return_value=rows),
        patch("k7.cli.k7.Console", lambda: __import__("rich.console").console.Console(width=200)),
    ):
        listed = _api("list-api-keys", "--api")
    with patch.object(Client, "revoke_api_key", return_value={"revoked": ["aaaaaaaa"]}) as revoke:
        revoked = _api("revoke-api-key", "old", "--api")
    assert (
        listed.exit_code == 0
        and "unscoped key(s) from before roles (aaaaaaaa)" in listed.output
        and "admin" in listed.output
    )
    assert revoked.exit_code == 0 and "aaaaaaaa" in revoked.output
    revoke.assert_called_once_with("old")


def test_api_errors_surface_with_exit_1():
    import requests

    err = requests.HTTPError(
        response=MagicMock(status_code=403, json=lambda: {"error": {"message": "create_key needs role admin"}})
    )
    with patch.object(Client, "create_api_key", side_effect=err):
        result = _api("generate-api-key", "x", "--role", "admin", "--api")
    assert result.exit_code == 1


def test_nodes_dedicate_and_undedicate_over_the_api():
    with (
        patch.object(Client, "dedicate_node", return_value={"message": "dedicated n1"}) as ded,
        patch("k7.cli.k7.K7Core") as core_cls,
    ):
        d = _api("nodes", "dedicate", "n1", "--tenant", "acme", "--api")
    with patch.object(Client, "undedicate_node", return_value={"message": "freed n1"}):
        u = _api("nodes", "undedicate", "n1", "--api")
    assert d.exit_code == 0 and "dedicated n1" in d.output and u.exit_code == 0
    ded.assert_called_once_with("n1", "acme")
    core_cls.assert_not_called()


def test_role_bindings_get_and_apply(tmp_path: Path):
    data = {"bindings": [{"group": "sre", "role": "operator"}], "deny_subjects": ["bad"]}
    with patch.object(Client, "get_role_bindings", return_value=data):
        got = _api("role-bindings", "get")
    assert got.exit_code == 0 and "group: sre" in got.output
    f = tmp_path / "b.yaml"
    f.write_text(got.output)
    with patch.object(Client, "put_role_bindings", return_value=data) as put:
        applied = _api("role-bindings", "apply", "-f", str(f))
    assert applied.exit_code == 0 and "1 binding(s), 1 deny-subject(s)" in applied.output
    put.assert_called_once_with(data["bindings"], ["bad"])


@pytest.mark.parametrize("text", ["- just a list\n", "bindings: []\nextra: 1\n", "deny_subjects: []\n"])
def test_role_bindings_apply_rejects_bad_files(tmp_path: Path, text):
    f = tmp_path / "b.yaml"
    f.write_text(text)
    with patch.object(Client, "put_role_bindings") as put:
        result = _api("role-bindings", "apply", "-f", str(f))
    assert result.exit_code == 1
    put.assert_not_called()


# ── k7 install --oidc-* ────────────────────────────────────────────

OIDC_FLAGS = [
    "--oidc-issuer", ISSUER,
    "--oidc-audience", "k7-api",
    "--oidc-client-id", "k7-cli",
    "--oidc-groups-claim", "groups",
    "--oidc-username-claim", "email",
]  # fmt: skip


def _install(args: list[str]):
    with patch("k7.cli.k7.K7Core") as core_cls:
        install = MagicMock(return_value=OperationResult(success=True, message="ok"))
        core_cls.return_value.install_node = install
        result = runner.invoke(app, ["--core", "install", "--backend", "kql", "--api-mode", "development", *args])
    return result, install


def test_install_without_oidc_sends_empty_settings():
    result, install = _install([])
    assert result.exit_code == 0, result.output
    ev = install.call_args.kwargs["extra_vars"]
    assert {ev[f"k7_oidc_{k}"] for k in ("issuer", "audience", "client_id", "groups_claim", "username_claim")} == {""}
    assert ev["k7_oidc_jwks_ttl_seconds"] == str(oidc.JWKS_TTL_SECONDS)
    assert ev["k7_oidc_allow_http_issuer"] == "false"


def test_install_forwards_all_five_oidc_settings():
    result, install = _install(OIDC_FLAGS)
    assert result.exit_code == 0, result.output
    ev = install.call_args.kwargs["extra_vars"]
    assert (ev["k7_oidc_issuer"], ev["k7_oidc_client_id"], ev["k7_oidc_username_claim"]) == (ISSUER, "k7-cli", "email")


@pytest.mark.parametrize("drop", range(0, len(OIDC_FLAGS), 2))
def test_install_partial_oidc_is_refused_before_ansible(drop):
    flags = OIDC_FLAGS[:drop] + OIDC_FLAGS[drop + 2 :]
    result, install = _install(flags)
    assert result.exit_code != 0 and OIDC_FLAGS[drop] in result.output
    install.assert_not_called()


def test_install_forwards_and_rejects_the_jwks_ttl():
    result, install = _install([*OIDC_FLAGS, "--oidc-jwks-ttl", "30"])
    assert result.exit_code == 0, result.output
    assert install.call_args.kwargs["extra_vars"]["k7_oidc_jwks_ttl_seconds"] == "30"
    refused, install = _install(["--oidc-jwks-ttl", str(oidc.REFRESH_SECONDS - 1)])
    assert refused.exit_code != 0 and str(oidc.REFRESH_SECONDS) in refused.output
    install.assert_not_called()


def test_install_http_issuer_needs_the_hidden_test_flag():
    http = [*OIDC_FLAGS[:1], "http://10.43.0.5:5556/dex", *OIDC_FLAGS[2:]]
    refused, install = _install(http)
    assert refused.exit_code != 0 and "https://" in refused.output
    install.assert_not_called()
    ok, install = _install([*http, "--oidc-allow-http-issuer"])
    assert ok.exit_code == 0 and install.call_args.kwargs["extra_vars"]["k7_oidc_allow_http_issuer"] == "true"


def test_playbook_validates_and_publishes_oidc():
    from tests.unit.conftest import install_tree_text

    tree = install_tree_text()
    assert "Fail if OIDC is partially configured or the issuer is not https" in tree
    assert "Publish the k7-api OIDC settings (ConfigMap k7-oidc)" in tree
    assert "delete configmap k7-oidc --ignore-not-found" in tree
    assert "K7_OIDC_JWKS_TTL_SECONDS" in tree


# ── SDK token provider ─────────────────────────────────────────────


def test_sdk_needs_exactly_one_credential():
    with pytest.raises(ValueError, match="exactly one"):
        Client(API)
    with pytest.raises(ValueError, match="exactly one"):
        Client(API, api_key="k", token_provider=lambda: "t")
    with pytest.raises(ValueError, match="exactly one"):
        AsyncClient(API)


def test_sdk_calls_the_token_provider_on_every_request():
    calls = iter(["t1", "t2"])
    c = Client(API, token_provider=lambda: next(calls))
    seen: list[str] = []

    def send(request, **_kw):
        seen.append(request.headers["Authorization"])
        r = MagicMock(status_code=200)
        r.json.return_value = {"data": {"subject": "x"}}
        return r

    with patch.object(c.session, "send", side_effect=send):
        c.whoami()
        c.list_api_keys()
    assert seen == ["Bearer t1", "Bearer t2"]
    assert "X-API-Key" not in c.session.headers


async def test_async_sdk_token_provider_sets_bearer():
    import httpx

    calls = iter(["a1", "a2"])
    seen: list[str] = []
    c = AsyncClient(API, token_provider=lambda: next(calls))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": []})

    c._client._transport = httpx.MockTransport(handler)
    await c.list()
    await c.list()
    assert seen == ["Bearer a1", "Bearer a2"]


@pytest.mark.parametrize(
    ("method", "args", "kwargs", "http", "path", "body"),
    [
        ("whoami", (), {}, "GET", "/api/v1/auth/whoami", None),
        (
            "create_api_key",
            ("ci",),
            {"role": "user", "tenant": "acme", "expires_days": 3},
            "POST",
            "/api/v1/keys",
            {"name": "ci", "role": "user", "tenant": "acme", "expires_days": 3, "namespaces": [], "nodes": []},
        ),
        ("list_api_keys", (), {}, "GET", "/api/v1/keys", None),
        ("revoke_api_key", ("ci",), {}, "DELETE", "/api/v1/keys/ci", None),
        ("dedicate_node", ("n1", "acme"), {}, "POST", "/api/v1/nodes/n1/dedicate", {"tenant": "acme"}),
        ("undedicate_node", ("n1",), {}, "POST", "/api/v1/nodes/n1/undedicate", None),
        ("get_role_bindings", (), {}, "GET", "/api/v1/role-bindings", None),
        (
            "put_role_bindings",
            ([{"group": "g", "role": "admin"}], ["x"]),
            {},
            "PUT",
            "/api/v1/role-bindings",
            {"bindings": [{"group": "g", "role": "admin"}], "deny_subjects": ["x"]},
        ),
    ],
)
def test_sdk_identity_methods_hit_the_right_routes(method, args, kwargs, http, path, body):
    c = Client(API, api_key="k")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": {"ok": True}}
    with patch.object(c.session, "request", return_value=resp) as req:
        assert getattr(c, method)(*args, **kwargs) == {"ok": True}
    assert req.call_args.args == (http, f"{API}{path}")
    assert req.call_args.kwargs.get("json") == body
