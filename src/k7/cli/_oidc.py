"""``k7 login`` / ``k7 logout``: OIDC device flow (default) or Authorization
Code + PKCE (``--browser``), with tokens cached per API URL in
``~/.config/k7/credentials.json`` (mode 0600) and refreshed with the
refresh token when the IdP issued one.

The issuer, client id and audience come from the API's public
``GET /api/v1/auth/oidc``, so the CLI needs only the API URL.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from pathlib import Path

import requests

from ._config import _config_dir

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_SCOPES = "openid profile email groups offline_access"
REFRESH_MARGIN = 60


class LoginRequired(RuntimeError):
    """The cached login is missing, expired without a refresh token, or refused."""


def credentials_path() -> Path:
    return _config_dir() / "credentials.json"


def _load() -> dict:
    p = credentials_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save(data: dict) -> None:
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(p, 0o600)


def _post(url: str, data: dict) -> dict:
    r = requests.post(url, data=data, timeout=15)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 and "error" not in body:
        raise RuntimeError(f"{url} returned HTTP {r.status_code}: {r.text[:300]}")
    return body


def discover(issuer: str) -> dict:
    r = requests.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=15)
    r.raise_for_status()
    doc = r.json()
    if doc.get("issuer") != issuer:
        raise RuntimeError(f"IdP discovery names issuer {doc.get('issuer')!r}, not {issuer!r}")
    return doc


def _claims(token: str | None) -> dict:
    """Unverified JWT payload (only to pick the token whose ``aud`` is the API's)."""
    if not token or token.count(".") != 2:
        return {}
    seg = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def pick_token(tokens: dict, audience: str) -> str:
    for kind in ("access_token", "id_token"):
        aud = _claims(tokens.get(kind)).get("aud")
        if audience == aud or (isinstance(aud, list) and audience in aud):
            return tokens[kind]
    raise RuntimeError(f"the IdP issued no JWT with audience {audience!r} (auth.oidc.audience)")


def device_flow(doc: dict, client_id: str, scopes: str, echo: Callable[[str], None]) -> dict:
    endpoint = doc.get("device_authorization_endpoint")
    if not endpoint:
        raise RuntimeError("the IdP does not support the device flow; use `k7 login --browser`")
    start = _post(endpoint, {"client_id": client_id, "scope": scopes})
    if "error" in start:
        raise RuntimeError(f"device authorization failed: {start}")
    echo(f"Open {start.get('verification_uri_complete') or start['verification_uri']}")
    echo(f"and enter code {start['user_code']} (expires in {start.get('expires_in', '?')}s)")
    interval = int(start.get("interval") or 5)
    deadline = time.monotonic() + int(start.get("expires_in", 600))
    while time.monotonic() < deadline:
        time.sleep(interval)
        tok = _post(
            doc["token_endpoint"],
            {"grant_type": DEVICE_GRANT, "device_code": start["device_code"], "client_id": client_id},
        )
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            raise RuntimeError(f"device login failed: {err}: {tok.get('error_description', '')}")
        return tok
    raise RuntimeError("device code expired before it was approved")


def browser_flow(doc: dict, client_id: str, scopes: str, echo: Callable[[str], None]) -> dict:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"k7 login complete; you can close this tab.")

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    url = (
        doc["authorization_endpoint"]
        + "?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "scope": scopes,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    echo(f"Opening {url}")
    webbrowser.open(url)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()
    if got.get("state") != state or "code" not in got:
        raise RuntimeError(f"browser login failed: {got.get('error') or 'no authorization code received'}")
    tok = _post(
        doc["token_endpoint"],
        {
            "grant_type": "authorization_code",
            "code": got["code"],
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if "error" in tok:
        raise RuntimeError(f"code exchange failed: {tok}")
    return tok


def login(api_url: str, cfg: dict, *, browser: bool, scopes: str, echo: Callable[[str], None]) -> None:
    doc = discover(cfg["issuer"])
    tokens = (browser_flow if browser else device_flow)(doc, cfg["client_id"], scopes, echo)
    pick_token(tokens, cfg["audience"])
    data = _load()
    data[api_url] = {
        **cfg,
        "token_endpoint": doc["token_endpoint"],
        "revocation_endpoint": doc.get("revocation_endpoint"),
        **_stamp(tokens),
    }
    _save(data)


def _stamp(tokens: dict) -> dict:
    keep = {k: tokens[k] for k in ("access_token", "id_token", "refresh_token") if tokens.get(k)}
    return {**keep, "expires_at": int(time.time()) + int(tokens.get("expires_in") or 0)}


def token_provider(api_url: str) -> Callable[[], str] | None:
    """A provider for the SDK when ``k7 login`` cached tokens for ``api_url``."""
    if api_url not in _load():
        return None

    def provide() -> str:
        data = _load()
        entry = data.get(api_url)
        if entry is None:
            raise LoginRequired(f"no cached login for {api_url}; run `k7 login`")
        token = pick_token(entry, entry["audience"])
        exp = _claims(token).get("exp") or entry["expires_at"]
        if exp - REFRESH_MARGIN > time.time():
            return token
        if not entry.get("refresh_token"):
            raise LoginRequired(f"OIDC login for {api_url} expired; run `k7 login`")
        tok = _post(
            entry["token_endpoint"],
            {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"], "client_id": entry["client_id"]},
        )
        if "error" in tok:
            raise LoginRequired(f"token refresh failed ({tok['error']}); run `k7 login`")
        entry.update(_stamp({"refresh_token": entry["refresh_token"], **tok}))
        _save(data)
        return pick_token(entry, entry["audience"])

    return provide


def logout(api_url: str) -> str:
    data = _load()
    entry = data.pop(api_url, None)
    if entry is None:
        return f"not logged in to {api_url}"
    if data:
        _save(data)
    else:
        credentials_path().unlink()
    revoked = []
    if entry.get("revocation_endpoint"):
        for kind in ("refresh_token", "access_token"):
            if entry.get(kind):
                r = requests.post(
                    entry["revocation_endpoint"],
                    data={"token": entry[kind], "token_type_hint": kind, "client_id": entry["client_id"]},
                    timeout=15,
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"revoking the {kind} failed: HTTP {r.status_code} {r.text[:200]}")
                revoked.append(kind)
    return f"logged out of {api_url}" + (f" (revoked {', '.join(revoked)})" if revoked else "")
