"""OIDC token validation against an in-process fake IdP
(discovery + JWKS over httpx.MockTransport, locally generated RSA/EC keys),
group/user/subject bindings, deny-subjects, the bindings API and its
lock-out guard, and how OIDC principals mix with API keys and tenants."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException
from kubernetes_asyncio.client.rest import ApiException

from k7.api import oidc
from k7.api.oidc import OIDCConfig, OIDCVerifier, principal_from_claims, validate_bindings
from k7.core.models import OperationResult
from tests.unit.auth_fixtures import CFG, ISSUER, api

REAL_MONOTONIC = time.monotonic
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
ROGUE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: Any, kid: str) -> dict:
    alg = jwt.algorithms.RSAAlgorithm if isinstance(key, rsa.RSAPrivateKey) else jwt.algorithms.ECAlgorithm
    d = alg.to_jwk(key.public_key(), as_dict=True)
    return {**d, "kid": kid, "use": "sig"}


class FakeIdP:
    """Discovery + JWKS; ``jwks`` can be swapped (key rotation) and ``down`` simulates an outage."""

    def __init__(self, issuer: str = ISSUER, discovery_issuer: str | None = None):
        self.issuer = issuer
        self.discovery_issuer = discovery_issuer or issuer
        self.jwks = {"keys": [_jwk(RSA_KEY, "rsa1"), _jwk(EC_KEY, "ec1")]}
        self.down = False
        self.hits: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hits.append(request.url.path)
        if self.down:
            return httpx.Response(503)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": self.discovery_issuer, "jwks_uri": f"{self.issuer}/keys"})
        if request.url.path == "/keys":
            return httpx.Response(200, json=self.jwks)
        return httpx.Response(404)

    def verifier(self, cfg: OIDCConfig = CFG) -> OIDCVerifier:
        return OIDCVerifier(cfg, transport=httpx.MockTransport(self.handler))


def claims(**over: Any) -> dict:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": "k7-api",
        "sub": "user-1",
        "email": "alice@example.com",
        "email_verified": True,
        "groups": ["acme-devs"],
        "iat": now,
        "exp": now + 300,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def sign(payload: dict | None = None, *, key: Any = RSA_KEY, alg: str = "RS256", kid: str | None = "rsa1") -> str:
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload or claims(), key, algorithm=alg, headers=headers)


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


async def _status(verifier: OIDCVerifier, token: str) -> tuple[int, str]:
    try:
        await verifier.verify(token)
    except HTTPException as e:
        return e.status_code, str(e.detail)
    return 200, ""


# ── config ─────────────────────────────────────────────────────────

ENV = {
    "K7_OIDC_ISSUER": ISSUER,
    "K7_OIDC_AUDIENCE": "k7-api",
    "K7_OIDC_CLIENT_ID": "k7-cli",
    "K7_OIDC_GROUPS_CLAIM": "groups",
    "K7_OIDC_USERNAME_CLAIM": "email",
}


def test_config_unset_is_none(monkeypatch):
    for k in [*ENV, "K7_OIDC_ALLOW_HTTP_ISSUER"]:
        monkeypatch.delenv(k, raising=False)
    assert OIDCConfig.from_env() is None


def test_config_all_five(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert OIDCConfig.from_env() == CFG


@pytest.mark.parametrize("missing", sorted(ENV))
def test_config_partial_is_loud(monkeypatch, missing):
    for k, v in ENV.items():
        monkeypatch.setenv(k, "" if k == missing else v)
    with pytest.raises(RuntimeError, match=missing):
        OIDCConfig.from_env()


def test_jwks_ttl_defaults_and_rejects_a_short_or_garbage_value(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("K7_OIDC_JWKS_TTL_SECONDS", raising=False)
    cfg = OIDCConfig.from_env()
    assert cfg is not None and cfg.jwks_ttl_seconds == oidc.JWKS_TTL_SECONDS
    monkeypatch.setenv("K7_OIDC_JWKS_TTL_SECONDS", str(oidc.REFRESH_SECONDS))
    cfg = OIDCConfig.from_env()
    assert cfg is not None and cfg.jwks_ttl_seconds == oidc.REFRESH_SECONDS
    for raw in ("0", str(oidc.REFRESH_SECONDS - 1), "-1", "10.5", "abc"):
        monkeypatch.setenv("K7_OIDC_JWKS_TTL_SECONDS", raw)
        with pytest.raises(RuntimeError, match="K7_OIDC_JWKS_TTL_SECONDS"):
            OIDCConfig.from_env()


def test_jwks_ttl_pin_matches_the_install_options():
    root = Path(__file__).resolve().parents[2]
    play = yaml.safe_load((root / "src/k7/deploy/k7-install-node.yaml").read_text())[0]["vars"]
    values = yaml.safe_load((root / "deploy/helm/k7/values.yaml").read_text())
    schema = json.loads((root / "deploy/helm/k7/values.schema.json").read_text())
    validate = (root / "src/k7/deploy/roles/k7_cluster/tasks/validate.yml").read_text()
    assert play["k7_oidc_jwks_ttl_seconds"] == oidc.JWKS_TTL_SECONDS
    assert values["auth"]["oidc"]["jwksTtlSeconds"] == oidc.JWKS_TTL_SECONDS
    prop = schema["properties"]["auth"]["properties"]["oidc"]["properties"]["jwksTtlSeconds"]
    assert prop["minimum"] == oidc.REFRESH_SECONDS
    assert f">= {oidc.REFRESH_SECONDS}" in validate


def test_config_http_issuer_needs_the_test_flag(monkeypatch):
    for k, v in {**ENV, "K7_OIDC_ISSUER": "http://dex.local:5556/dex"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("K7_OIDC_ALLOW_HTTP_ISSUER", raising=False)
    with pytest.raises(RuntimeError, match="https"):
        OIDCConfig.from_env()
    monkeypatch.setenv("K7_OIDC_ALLOW_HTTP_ISSUER", "1")
    cfg = OIDCConfig.from_env()
    assert cfg is not None and cfg.issuer == "http://dex.local:5556/dex"


# ── token validation ───────────────────────────────────────────────


@pytest.mark.parametrize(("key", "alg", "kid"), [(RSA_KEY, "RS256", "rsa1"), (EC_KEY, "ES256", "ec1")])
async def test_valid_tokens_pass(key, alg, kid):
    v = FakeIdP().verifier()
    got = await v.verify(sign(key=key, alg=alg, kid=kid))
    assert got["sub"] == "user-1" and got["email"] == "alice@example.com"


@pytest.mark.parametrize(
    ("name", "token_fn", "status", "message"),
    [
        ("expired", lambda: sign(claims(exp=int(time.time()) - 120)), 401, "expired"),
        ("not yet valid", lambda: sign(claims(nbf=int(time.time()) + 120)), 401, "not yet valid"),
        ("issued in the future", lambda: sign(claims(iat=int(time.time()) + 3600)), 401, "invalid OIDC token"),
        ("wrong audience", lambda: sign(claims(aud="some-other-app")), 401, "Audience doesn't match"),
        ("audience list without ours", lambda: sign(claims(aud=["a", "b"])), 401, "Audience doesn't match"),
        ("wrong issuer", lambda: sign(claims(iss="https://evil.example.com")), 401, "issuer"),
        ("no exp", lambda: sign(claims(exp=None)), 401, "exp"),
        ("no sub", lambda: sign(claims(sub=None)), 401, "sub"),
        ("no aud", lambda: sign(claims(aud=None)), 401, "aud"),
        ("no iss", lambda: sign(claims(iss=None)), 401, "iss"),
        ("alg none", lambda: f"{_b64({'alg': 'none', 'kid': 'rsa1'})}.{_b64(claims())}.", 401, "'none' is not allowed"),
        (
            "HS256 signed with the RSA public key",
            lambda: _hs256(
                RSA_KEY.public_key().public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
            ),
            401,
            "'HS256' is not allowed",
        ),
        (
            "HS256 shared secret",
            lambda: jwt.encode(claims(), "s" * 32, algorithm="HS256", headers={"kid": "rsa1"}),
            401,
            "HS256",
        ),
        ("RS512 not on the allow-list", lambda: sign(alg="RS512"), 401, "RS512"),
        ("ES256 header on an RSA key", lambda: sign(key=EC_KEY, alg="ES256", kid="rsa1"), 401, "is a RSA key"),
        ("RS256 header on an EC key", lambda: sign(kid="ec1"), 401, "is a EC key"),
        ("no kid", lambda: sign(kid=None), 401, "no kid"),
        (
            "alg is not a string",
            lambda: f"{_b64({'alg': ['RS256'], 'kid': 'rsa1'})}.{_b64(claims())}.c2ln",
            401,
            "not allowed",
        ),
        (
            "kid is not a string",
            lambda: f"{_b64({'alg': 'RS256', 'kid': 1})}.{_b64(claims())}.c2ln",
            401,
            "Key ID header parameter must be a string",
        ),
        ("rogue key reusing a known kid", lambda: sign(key=ROGUE_KEY), 401, "Signature verification failed"),
        ("tampered payload", lambda: _tamper(sign()), 401, "invalid OIDC token"),
        ("garbage", lambda: "not.a.jwt", 401, "malformed"),
    ],
)
async def test_bad_tokens_are_401(name, token_fn, status, message):
    got = await _status(FakeIdP().verifier(), token_fn())
    assert got[0] == status, (name, got)
    assert message in got[1], (name, got)


def _hs256(secret: bytes) -> str:
    """The alg-confusion forgery pyjwt refuses to produce: HMAC keyed with the IdP's public key."""
    import hashlib
    import hmac

    signing_input = f"{_b64({'alg': 'HS256', 'kid': 'rsa1', 'typ': 'JWT'})}.{_b64(claims())}"
    mac = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def _tamper(token: str) -> str:
    h, p, s = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(p + "=="))
    body["groups"] = ["platform-admins"]
    return f"{h}.{_b64(body)}.{s}"


async def test_leeway_accepts_small_clock_skew():
    v = FakeIdP().verifier()
    assert (await _status(v, sign(claims(exp=int(time.time()) - 30))))[0] == 200
    assert (await _status(v, sign(claims(nbf=int(time.time()) + 30))))[0] == 200


async def test_unknown_kid_refreshes_jwks_once_and_rate_limits(monkeypatch):
    idp = FakeIdP()
    v = idp.verifier()
    assert (await _status(v, sign()))[0] == 200
    assert idp.hits == ["/.well-known/openid-configuration", "/keys"]
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.jwks = {"keys": [*idp.jwks["keys"], _jwk(rotated, "rsa2")]}
    monkeypatch.setattr(oidc.time, "monotonic", lambda: REAL_MONOTONIC() + oidc.REFRESH_SECONDS + 1)
    assert (await _status(v, sign(key=rotated, kid="rsa2")))[0] == 200
    assert idp.hits[-1] == "/keys" and idp.hits.count("/.well-known/openid-configuration") == 1
    hits = len(idp.hits)
    assert (await _status(v, sign(kid="ghost")))[0] == 401
    assert len(idp.hits) == hits, "a second unknown kid inside REFRESH_SECONDS must not hit the IdP"
    assert (await _status(v, sign()))[0] == 200, "known kids never trigger a refresh"
    assert len(idp.hits) == hits


async def test_jwks_unreachable_with_no_cached_key_is_503():
    idp = FakeIdP()
    idp.down = True
    status, msg = await _status(idp.verifier(), sign())
    assert status == 503 and "cannot fetch the OIDC signing keys" in msg


async def test_cached_keys_keep_working_during_an_idp_outage():
    idp = FakeIdP()
    v = idp.verifier()
    assert (await _status(v, sign()))[0] == 200
    idp.down = True
    assert (await _status(v, sign()))[0] == 200


def _clock(monkeypatch, start: float = 1_000_000.0) -> dict[str, float]:
    now = {"t": start}
    monkeypatch.setattr(oidc.time, "monotonic", lambda: now["t"])
    return now


async def test_jwks_ttl_drops_a_retired_key(monkeypatch):
    now = _clock(monkeypatch)
    idp = FakeIdP()
    v = idp.verifier()
    token = sign()
    assert (await _status(v, token))[0] == 200
    idp.jwks = {"keys": [_jwk(EC_KEY, "ec1")]}
    now["t"] += v.cfg.jwks_ttl_seconds - 1
    assert (await _status(v, token))[0] == 200, "inside the TTL a retired key is still the cached one"
    hits = len(idp.hits)
    now["t"] += 1
    status, msg = await _status(v, token)
    assert status == 401 and "rsa1" in msg
    assert len(idp.hits) > hits and idp.hits[-1] == "/keys"


async def test_configured_ttl_is_what_expires_the_cache(monkeypatch):
    now = _clock(monkeypatch)
    idp = FakeIdP()
    v = idp.verifier(replace(CFG, jwks_ttl_seconds=30))
    token = sign()
    assert (await _status(v, token))[0] == 200
    idp.jwks = {"keys": [_jwk(EC_KEY, "ec1")]}
    now["t"] += 29
    assert (await _status(v, token))[0] == 200
    now["t"] += 1
    assert (await _status(v, token))[0] == 401


async def test_expired_cache_is_not_used_when_refresh_fails(monkeypatch):
    now = _clock(monkeypatch)
    idp = FakeIdP()
    v = idp.verifier()
    assert (await _status(v, sign()))[0] == 200
    idp.down = True
    now["t"] += v.cfg.jwks_ttl_seconds
    status, msg = await _status(v, sign())
    assert status == 503 and "cannot fetch the OIDC signing keys" in msg
    hits = len(idp.hits)
    status, msg = await _status(v, sign())
    assert status == 503 and "cannot fetch the OIDC signing keys" in msg and len(idp.hits) == hits


async def test_failed_refresh_stays_503_through_the_rate_limit(monkeypatch):
    now = _clock(monkeypatch)
    idp = FakeIdP()
    idp.down = True
    v = idp.verifier()
    status, msg = await _status(v, sign())
    assert status == 503 and "cannot fetch the OIDC signing keys" in msg
    hits = len(idp.hits)
    now["t"] += oidc.REFRESH_SECONDS - 1
    idp.down = False
    status, msg = await _status(v, sign(kid="ghost"))
    assert status == 503 and "cannot fetch the OIDC signing keys" in msg and len(idp.hits) == hits
    now["t"] += 1
    assert (await _status(v, sign()))[0] == 200


async def test_unknown_kid_refresh_failure_is_503_and_known_keys_still_work(monkeypatch):
    now = _clock(monkeypatch)
    idp = FakeIdP()
    v = idp.verifier()
    assert (await _status(v, sign()))[0] == 200
    idp.down = True
    now["t"] += oidc.REFRESH_SECONDS
    status, msg = await _status(v, sign(kid="ghost"))
    assert status == 503 and "cannot fetch the OIDC signing keys" in msg
    hits = len(idp.hits)
    now["t"] += oidc.REFRESH_SECONDS - 1
    status, _ = await _status(v, sign(kid="other"))
    assert status == 503 and len(idp.hits) == hits
    assert (await _status(v, sign()))[0] == 200, "a fresh cache still verifies keys it already holds"


async def test_requests_during_a_ttl_refresh_wait_for_it(monkeypatch):
    now = _clock(monkeypatch)
    idp = FakeIdP()
    gate = asyncio.Event()

    async def slow_keys(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/keys" and "/keys" in idp.hits:
            await gate.wait()
        return idp.handler(request)

    v = OIDCVerifier(CFG, transport=httpx.MockTransport(slow_keys))
    assert (await _status(v, sign()))[0] == 200
    now["t"] += v.cfg.jwks_ttl_seconds
    first = asyncio.create_task(_status(v, sign()))
    second = asyncio.create_task(_status(v, sign()))
    for _ in range(20):  # the patched clock also stops asyncio timers
        await asyncio.sleep(0)
    assert not second.done(), "a request during the refresh must wait for it"
    gate.set()
    assert [(await first)[0], (await second)[0]] == [200, 200]
    assert idp.hits.count("/keys") == 2


async def test_discovery_naming_another_issuer_is_refused():
    idp = FakeIdP(discovery_issuer="https://evil.example.com")
    status, msg = await _status(idp.verifier(), sign())
    assert status == 503 and "evil.example.com" in msg


async def test_tokens_from_another_issuer_signed_by_our_idp_keys_are_401():
    v = FakeIdP().verifier()
    assert (await _status(v, sign(claims(iss=ISSUER + "/"))))[0] == 401


# ── bindings → principal ───────────────────────────────────────────

BINDINGS = [
    {"group": "platform-admins", "role": "admin"},
    {"group": "sre", "role": "operator"},
    {"group": "acme-devs", "role": "user", "tenant": "acme"},
    {"group": "acme-auditors", "role": "read-only", "tenant": "acme"},
    {"user": "auditor@example.com", "role": "read-only", "tenant": "globex"},
    {"subject": "repo:org/app:ref:refs/heads/main", "role": "user", "tenant": "globex"},
]


def _principal(c: dict, bindings=BINDINGS, deny=()):
    return principal_from_claims(CFG, c, list(bindings), list(deny))


def test_group_binding():
    p = _principal(claims(groups=["acme-devs"]))
    assert (p.type, p.subject, p.cluster_role, p.tenant_roles) == ("oidc", "user-1", None, {"acme": "user"})
    assert p.issuer == ISSUER and p.groups == ["acme-devs"]


def test_user_binding_matches_the_username_claim_exactly():
    assert _principal(claims(email="auditor@example.com", groups=[])).tenant_roles == {"globex": "read-only"}
    with pytest.raises(HTTPException):
        _principal(claims(email="AUDITOR@example.com", groups=[]))


def test_subject_binding_for_workload_identity():
    p = _principal(claims(sub="repo:org/app:ref:refs/heads/main", email="ci", groups=[]))
    assert p.tenant_roles == {"globex": "user"}
    with pytest.raises(HTTPException):
        _principal(claims(sub="repo:org/app:ref:refs/heads/feature", email="ci", groups=[]))


def test_union_takes_the_highest_role_per_tenant_and_cluster():
    p = _principal(claims(email="auditor@example.com", groups=["acme-auditors", "acme-devs", "sre"]))
    assert p.cluster_role == "operator"
    assert p.tenant_roles == {"acme": "user", "globex": "read-only"}
    assert _principal(claims(groups=["sre", "platform-admins"])).cluster_role == "admin"


def test_unbound_subject_403_names_subject_user_and_groups():
    with pytest.raises(HTTPException) as e:
        _principal(claims(groups=["marketing", "everyone"]))
    assert e.value.status_code == 403
    msg = str(e.value.detail)
    assert "'user-1'" in msg and "alice@example.com" in msg and "['everyone', 'marketing']" in msg


@pytest.mark.parametrize("deny", [["user-1"], ["alice@example.com"]])
def test_deny_subjects_beat_every_binding(deny):
    with pytest.raises(HTTPException) as e:
        _principal(claims(groups=["platform-admins"]), deny=deny)
    assert e.value.status_code == 403 and "deny-subjects" in str(e.value.detail)


def test_missing_username_claim_is_401():
    with pytest.raises(HTTPException) as e:
        _principal(claims(email=None))
    assert e.value.status_code == 401 and "usernameClaim" in str(e.value.detail)


@pytest.mark.parametrize("verified", [False, "true", 1, None])
def test_email_username_requires_email_verified_true(verified):
    with pytest.raises(HTTPException) as e:
        _principal(claims(email_verified=verified))
    assert e.value.status_code == 401 and "email_verified" in str(e.value.detail)
    assert _principal(claims(email_verified=True)).subject == "user-1"


def test_other_username_claims_do_not_require_email_verified():
    cfg = replace(CFG, username_claim="preferred_username")
    got = principal_from_claims(
        cfg, claims(preferred_username="auditor@example.com", email_verified=None, groups=[]), BINDINGS, []
    )
    assert got.tenant_roles == {"globex": "read-only"}


def test_groups_claim_shapes():
    assert _principal(claims(groups="acme-devs")).tenant_roles == {"acme": "user"}
    assert _principal(claims(groups=[{"id": "platform-admins"}, 7, "acme-devs"])).cluster_role is None
    with pytest.raises(HTTPException):
        _principal(claims(groups=None))


@pytest.mark.parametrize(
    ("bindings", "deny", "message"),
    [
        ({"group": "g"}, [], "must be lists"),
        ([], "user-1", "must be lists"),
        ([], [""], "non-empty strings"),
        (["g"], [], "must be an object"),
        ([{"role": "admin"}], [], "exactly one of"),
        ([{"group": "g", "user": "u", "role": "admin"}], [], "exactly one of"),
        ([{"group": "", "role": "admin"}], [], "exactly one of"),
        ([{"group": "g", "role": "admin", "extra": 1}], [], "exactly one of"),
        ([{"group": "g", "role": "god"}], [], "role must be one of"),
        ([{"group": "g", "role": "user"}], [], "tenant-scoped"),
        ([{"group": "g", "role": "operator", "tenant": "acme"}], [], "cluster-scoped"),
        ([{"group": "g", "role": "user", "tenant": "ghost"}], [], "does not exist"),
        (
            [{"group": "g", "role": "user", "tenant": "acme"}, {"group": "g", "role": "read-only", "tenant": "acme"}],
            [],
            "duplicates",
        ),
    ],
)
def test_validate_bindings_rejects(bindings, deny, message):
    with pytest.raises(ValueError, match=message):
        validate_bindings(bindings, deny, {"acme", "globex"})


def test_validate_bindings_accepts_the_spec_example():
    rows, deny = validate_bindings(BINDINGS, ["bad@example.com"], {"acme", "globex"})
    assert rows == BINDINGS and deny == ["bad@example.com"]
    same_group_two_tenants = [
        {"group": "g", "role": "user", "tenant": "acme"},
        {"group": "g", "role": "read-only", "tenant": "globex"},
    ]
    assert validate_bindings(same_group_two_tenants, [], {"acme", "globex"})[0] == same_group_two_tenants


def test_parse_bindings():
    assert oidc.parse_bindings({}) == ([], [])
    assert oidc.parse_bindings({"bindings": "- {group: g, role: admin}\n", "deny-subjects": "- x\n"}) == (
        [{"group": "g", "role": "admin"}],
        ["x"],
    )


async def test_read_and_write_bindings_configmap():
    v1 = AsyncMock()
    core = AsyncMock()
    core._get_core_v1_client.return_value = v1
    v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    assert await oidc.read_bindings(core, "kube-system") == ([], [])
    v1.read_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        await oidc.read_bindings(core, "kube-system")
    v1.replace_namespaced_config_map.side_effect = ApiException(status=404)
    await oidc.write_bindings(core, "kube-system", [{"group": "g", "role": "admin"}], ["x"])
    body = v1.create_namespaced_config_map.await_args.args[1]
    assert body.metadata.name == "k7-role-bindings"
    assert oidc.parse_bindings(body.data) == ([{"group": "g", "role": "admin"}], ["x"])
    v1.replace_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        await oidc.write_bindings(core, "kube-system", [], [])


# ── through the API: real verifier, fake IdP, bindings in the ConfigMap ──


def _real_oidc(a, idp: FakeIdP | None = None):
    return patch("k7.api.main.OIDC", (idp or FakeIdP()).verifier())


async def _get(a, token: str, path: str = "/api/v1/auth/whoami", **kw):
    return await a.call("GET", path, headers={"Authorization": f"Bearer {token}"}, **kw)


async def test_oidc_unset_rejects_jwts_with_401_and_keys_still_work():
    with api(oidc=False) as a:
        a.add_key("a", role="admin")
        r = await _get(a, sign())
        assert r.status_code == 401 and "OIDC is not configured" in r.json()["error"]["message"]
        assert (await a.call("GET", "/api/v1/auth/whoami", "a")).status_code == 200
        assert (await a.call("GET", "/api/v1/auth/oidc")).status_code == 404


async def test_public_oidc_config_for_the_cli():
    with api() as a:
        r = await a.call("GET", "/api/v1/auth/oidc")
        health = await a.call("GET", "/health")
    assert r.status_code == 200
    assert r.json()["data"] == {"issuer": ISSUER, "client_id": "k7-cli", "audience": "k7-api"}
    assert health.json()["oidc_issuer"] == ISSUER


@pytest.mark.parametrize(
    "token",
    [
        lambda: sign(claims(exp=int(time.time()) - 600)),
        lambda: sign(claims(aud="other")),
        lambda: sign(claims(iss="https://evil.example.com")),
        lambda: f"{_b64({'alg': 'none', 'kid': 'rsa1'})}.{_b64(claims())}.",
        lambda: jwt.encode(claims(), "s" * 32, algorithm="HS256", headers={"kid": "rsa1"}),
    ],
    ids=["expired", "wrong-aud", "wrong-iss", "alg-none", "hs256"],
)
async def test_bad_jwts_are_401_through_the_api(token):
    before = _oidc_invalid()
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        r = await _get(a, token())
    assert r.status_code == 401
    assert _oidc_invalid() == before + 1


def _oidc_invalid() -> float:
    from k7 import metrics

    return metrics.API.get_sample_value("k7_auth_failures_total", {"reason": "oidc_invalid"}) or 0.0


async def test_idp_outage_is_not_counted_as_an_invalid_token():
    before = _oidc_invalid()
    idp = FakeIdP()
    idp.down = True
    with api() as a, _real_oidc(a, idp):
        r = await _get(a, sign())
    assert r.status_code == 503 and _oidc_invalid() == before


async def test_unverified_email_is_401_through_the_api():
    with api() as a:
        a.add_oidc("alice", groups=["acme-devs"], email_verified=False)
        a.bindings = list(BINDINGS)
        r = await _get(a, a.tokens["alice"])
    assert r.status_code == 401 and "email_verified" in r.json()["error"]["message"]


async def test_bound_group_grants_user_on_one_tenant_only():
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        tok = sign(claims(groups=["acme-devs"]))
        a.core.list_sandboxes.return_value = []
        a.core.create_sandbox.return_value = OperationResult(success=True)
        who = (await _get(a, tok)).json()["data"]
        assert who == {
            "type": "oidc",
            "subject": "user-1",
            "grants": ["user on tenant acme"],
            "issuer": ISSUER,
            "groups": ["acme-devs"],
        }
        h = {"Authorization": f"Bearer {tok}"}
        ok = await a.call(
            "POST", "/api/v1/sandboxes", json={"name": "t", "image": "alpine", "namespace": "acme-prod"}, headers=h
        )
        other = await a.call(
            "POST", "/api/v1/sandboxes", json={"name": "t", "image": "alpine", "namespace": "globex"}, headers=h
        )
        keys = await a.call("POST", "/api/v1/keys", json={"name": "x", "role": "admin", "expires_days": 1}, headers=h)
    assert ok.status_code == 201
    assert other.status_code == 403 and keys.status_code == 403


async def test_unbound_subject_gets_403_naming_its_groups_through_the_api():
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        r = await _get(a, sign(claims(groups=["interns"])))
    assert r.status_code == 403 and "['interns']" in r.json()["error"]["message"]


async def test_deny_subjects_block_on_the_very_next_request():
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        tok = sign(claims(groups=["platform-admins"]))
        assert (await _get(a, tok)).status_code == 200
        a.deny = ["user-1"]
        r = await _get(a, tok)
    assert r.status_code == 403 and "deny-subjects" in r.text


async def test_removing_a_binding_applies_on_the_next_request():
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        tok = sign(claims(groups=["sre"]))
        assert (await _get(a, tok, "/api/v1/nodes/storage")).status_code != 403
        a.bindings = [b for b in BINDINGS if b.get("group") != "sre"]
        assert (await _get(a, tok, "/api/v1/nodes/storage")).status_code == 403


async def test_unknown_kid_refresh_through_the_api():
    idp = FakeIdP()
    with api() as a, _real_oidc(a, idp):
        a.bindings = list(BINDINGS)
        assert (await _get(a, sign(claims(groups=["sre"])))).status_code == 200
        rotated = ec.generate_private_key(ec.SECP256R1())
        idp.jwks = {"keys": [_jwk(rotated, "ec2")]}
        with patch.object(oidc.time, "monotonic", lambda: REAL_MONOTONIC() + 60):
            r = await _get(a, sign(claims(groups=["sre"]), key=rotated, alg="ES256", kid="ec2"))
    assert r.status_code == 200 and idp.hits.count("/keys") == 2


async def test_bearer_routing_between_keys_and_jwts():
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        key = a.add_key("k", role="operator")
        jwt_tok = sign(claims(groups=["acme-devs"]))
        as_bearer = (await _get(a, key)).json()["data"]
        x_api_key_wins = (
            await a.call("GET", "/api/v1/auth/whoami", headers={"X-API-Key": key, "Authorization": f"Bearer {jwt_tok}"})
        ).json()["data"]
        jwt_in_x_api_key = await a.call("GET", "/api/v1/auth/whoami", headers={"X-API-Key": jwt_tok})
        lower_bearer = await a.call("GET", "/api/v1/auth/whoami", headers={"Authorization": f"bearer {jwt_tok}"})
    assert as_bearer["type"] == "key" and as_bearer["grants"] == ["operator"]
    assert x_api_key_wins["type"] == "key"
    assert jwt_in_x_api_key.status_code == 401, "X-API-Key never takes the OIDC path"
    assert lower_bearer.json()["data"]["type"] == "oidc"


async def test_oidc_admin_mints_a_key_that_then_works_with_its_own_role():
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        h = {"Authorization": f"Bearer {sign(claims(groups=['platform-admins']))}"}
        r = await a.call(
            "POST",
            "/api/v1/keys",
            json={"name": "ci", "role": "read-only", "tenant": "acme", "expires_days": 1},
            headers=h,
        )
        assert r.status_code == 201, r.text
        data = r.json()["data"]
        a.store.keys[data["id"]] = json.loads(a.core.patch_secret_data.await_args.args[2][data["id"]])
        a.tokens["ci"] = data["token"]
        a.core.list_sandboxes.return_value = []
        assert (await a.call("GET", "/api/v1/sandboxes", "ci", params={"namespace": "default"})).status_code == 200
        assert (
            await a.call(
                "POST", "/api/v1/sandboxes/sb/exec", "ci", params={"namespace": "default"}, json={"command": "id"}
            )
        ).status_code == 403


async def test_audit_lines_carry_oidc_identity_and_never_the_token(capsys):
    import logging

    from k7 import logging as k7_logging
    from k7.api.main import audit_log

    lines: list[str] = []
    handler = logging.Handler()
    handler.setFormatter(k7_logging.JsonFormatter())
    handler.emit = lambda rec: lines.append(handler.format(rec))  # type: ignore[method-assign]
    audit_log.addHandler(handler)
    tok = sign(claims(groups=["acme-devs"]))
    try:
        with api() as a, _real_oidc(a):
            a.bindings = list(BINDINGS)
            a.core.delete_sandbox.return_value = OperationResult(success=True, message="ok")
            await a.call(
                "DELETE",
                "/api/v1/sandboxes/sb1",
                params={"namespace": "default"},
                headers={"Authorization": f"Bearer {tok}"},
            )
            await _get(a, sign(claims(groups=["nobody"])))
    finally:
        audit_log.removeHandler(handler)
    ok, denied = (json.loads(x) for x in lines)
    assert ok["principal_type"] == "oidc" and ok["subject"] == "user-1" and ok["role"] == "user"
    assert ok["tenant"] == "acme" and ok["idp_issuer"] == ISSUER and ok["key_id"] is None
    assert denied["status"] == 403 and denied["subject"] == "user-1" and denied["principal_type"] == "oidc"
    blob = "\n".join(lines) + capsys.readouterr().out
    assert tok not in blob and "alice@example.com" not in blob and "acme-devs" not in blob


# ── PUT /api/v1/role-bindings ──────────────────────────────────────


async def _put(a, label: str, body: dict):
    return await a.call("PUT", "/api/v1/role-bindings", label, json=body)


@pytest.fixture()
def write_bindings():
    with patch("k7.api.main.write_bindings", AsyncMock()) as w:
        yield w


def _keys_secret(a, entries: dict[str, dict]):
    a.core.read_secret_data.return_value = {k: json.dumps(v) for k, v in entries.items()}


async def test_put_bindings_validates_and_writes(write_bindings):
    with api() as a:
        a.add_key("a", role="admin")
        _keys_secret(a, {"k1": {"name": "a", "hash": "h", "role": "admin", "expires": int(time.time()) + 60}})
        r = await _put(a, "a", {"bindings": BINDINGS, "deny_subjects": ["bad"]})
        assert r.status_code == 200, r.text
        write_bindings.assert_awaited_once()
        assert write_bindings.await_args.args[2:] == (BINDINGS, ["bad"])
        got = await a.call("GET", "/api/v1/role-bindings", "a")
    assert got.status_code == 200


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"bindings": [{"group": "g", "role": "user", "tenant": "ghost"}]}, "does not exist"),
        ({"bindings": [{"group": "g", "role": "admin"}, {"group": "g", "role": "operator"}]}, "duplicates"),
        ({"bindings": [{"group": "g", "role": "user"}]}, "tenant-scoped"),
        ({}, "must be lists"),
    ],
)
async def test_put_bindings_400s(write_bindings, body, message):
    with api() as a:
        a.add_key("a", role="admin")
        r = await _put(a, "a", body)
    assert r.status_code == 400 and message in r.json()["error"]["message"]
    write_bindings.assert_not_awaited()


@pytest.mark.parametrize(
    ("stored", "locked_out"),
    [
        ({}, True),
        ({"k1": {"name": "op", "hash": "h", "role": "operator", "expires": 9_999_999_999}}, True),
        ({"k1": {"name": "old", "hash": "h", "role": "admin", "expires": 1}}, True),
        ({"k1": {"name": "root", "hash": "h", "role": "admin", "expires": 9_999_999_999}}, False),
        ({"k1": {"name": "root", "hash": "h", "role": "admin"}}, False),
    ],
    ids=["no-keys", "only-operator-key", "expired-admin-key", "admin-key", "admin-key-no-expiry"],
)
async def test_lock_out_guard(write_bindings, stored, locked_out):
    with api() as a, _real_oidc(a):
        a.bindings = list(BINDINGS)
        _keys_secret(a, stored)
        h = {"Authorization": f"Bearer {sign(claims(groups=['platform-admins']))}"}
        body = {"bindings": [{"group": "sre", "role": "operator"}]}
        r = await a.call("PUT", "/api/v1/role-bindings", json=body, headers=h)
        keep_admin = await a.call("PUT", "/api/v1/role-bindings", json={"bindings": BINDINGS}, headers=h)
    assert keep_admin.status_code == 200
    if locked_out:
        assert r.status_code == 400 and "last admin binding" in r.json()["error"]["message"]
        assert write_bindings.await_count == 1
    else:
        assert r.status_code == 200 and write_bindings.await_count == 2
