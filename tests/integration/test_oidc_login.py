"""End to end: k7 roles and OIDC login against a real k7-api.

A pinned Dex (static users with groups, memory storage) is the issuer; a
second "rogue" Dex with its own issuer and signing keys stands in for a
foreign IdP. The test points k7-api at Dex through ConfigMap ``k7-oidc``,
rolls it with the ``k7.katakate.org/oidc-config`` pod annotation (the
playbook's mechanism), creates ``Tenant`` CRs (and the Tenant CRD when it is
absent), and restores every object in ``finally``. Everything it creates
carries the ``k7.katakate.org/itest=oidc`` label so ``test_zz_leaks`` can
find leftovers.

Tests run in file order: the last one restarts Dex, which drops every
refresh token issued before it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import subprocess
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml

from k7.api.keystore import POLL_SECONDS

from .cluster import KUBECTL, system_ns
from .conftest import _teardown_namespace
from .test_api import _api_pod_ready, _get_api_base_url, _tls_verify, _wait_for_sandbox_ready, _wait_key_live
from .test_observability import _scrape_app, _sum

pytestmark = pytest.mark.integration

DEX_IMAGE = "ghcr.io/dexidp/dex:v2.46.0@sha256:933fcd3f523338c847b88ef84a7145fb2fcb7b9917f08418612afeaaa2001777"
DEX_NS = "k7-itest-dex"
ITEST_LABEL = {"k7.katakate.org/itest": "oidc"}
PASSWORD = "password"
PASSWORD_HASH = "$2a$10$2b2cU8CPhOTaGrs1HRQuAueS7JTT5ZHsHSzYiFPm1leZck7Mc8T4W"  # bcrypt("password")
TOKEN_TTL = 70  # Dex idTokens expiry; k7-api adds LEEWAY on top
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ACME, GLOBEX = "itest-acme", "itest-globex"
USERS = {
    "alice": ["itest-acme-devs"],  # user on acme
    "bob": ["itest-acme-viewers"],  # read-only on acme
    "carol": ["itest-sre"],  # operator
    "dave": ["itest-k7-admins"],  # admin
    "eve": ["itest-contractors"],  # unbound
    "frank": ["itest-acme-viewers", "itest-globex-devs"],  # read-only on acme + user on globex
}
BINDINGS = [
    {"group": "itest-acme-devs", "role": "user", "tenant": ACME},
    {"group": "itest-acme-viewers", "role": "read-only", "tenant": ACME},
    {"group": "itest-globex-devs", "role": "user", "tenant": GLOBEX},
    {"group": "itest-sre", "role": "operator"},
    {"group": "itest-k7-admins", "role": "admin"},
]
_DEV_SH = Path(__file__).resolve().parents[2] / "src" / "k7" / "cli" / "dev.sh"
_TENANT_CRD = "tenants.k7.katakate.org"
_OIDC_ANNOTATION = "k7.katakate.org/oidc-config"


def _k(*args: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run([*KUBECTL, *args], input=stdin, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)} failed: {r.stderr}")
    return r


def _apply(*objs: dict) -> None:
    _k("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objs)}))


def _meta(name: str, namespace: str | None = None) -> dict:
    return {"name": name, "labels": dict(ITEST_LABEL), **({"namespace": namespace} if namespace else {})}


# ── Dex ─────────────────────────────────────────────────────────────


def _dex_objects(name: str, issuer: str) -> list[dict]:
    config = {
        "issuer": issuer,
        "storage": {"type": "memory"},
        "web": {"http": "0.0.0.0:5556"},
        # Dex v2.46 rotating a refresh token re-reads the local password DB while
        # holding the memory storage lock and deadlocks; inside reuseInterval it
        # returns the same refresh token instead.
        "expiry": {"idTokens": f"{TOKEN_TTL}s", "refreshTokens": {"reuseInterval": "24h"}},
        "oauth2": {
            "skipApprovalScreen": True,
            "passwordConnector": "local",
            "grantTypes": ["authorization_code", "refresh_token", "password", DEVICE_GRANT],
        },
        "enablePasswordDB": True,
        "staticClients": [
            # Dex v2.46 sends the device callback as the issuer path plus /device/callback,
            # which a public client accepts only when it is registered.
            {
                "id": "k7-cli",
                "name": "k7 CLI",
                "public": True,
                "redirectURIs": [f"{urllib.parse.urlparse(issuer).path}/device/callback"],
            },
            {"id": "other-app", "name": "Some other app", "public": True},
        ],
        "staticPasswords": [
            {"email": f"{u}@example.com", "hash": PASSWORD_HASH, "username": u, "userID": f"{u}-id", "groups": g}
            for u, g in USERS.items()
        ]
        + [
            # Admin group, so a missing email_verified check would be 200, not 403.
            # Dex defaults a nil emailVerified to true; false is what we want rejected.
            {
                "email": "ivan@example.com",
                "hash": PASSWORD_HASH,
                "username": "ivan",
                "userID": "ivan-id",
                "groups": ["itest-k7-admins"],
                "emailVerified": False,
            }
        ],
    }
    labels = {"app": name}
    container = {
        "name": "dex",
        "image": DEX_IMAGE,
        "args": ["dex", "serve", "/etc/dex/k7/config.yaml"],
        "ports": [{"containerPort": 5556}],
        "readinessProbe": {"httpGet": {"path": "/dex/healthz", "port": 5556}, "periodSeconds": 2},
        "volumeMounts": [{"name": "config", "mountPath": "/etc/dex/k7"}],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1001,
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": _meta(name, DEX_NS),
            "data": {"config.yaml": yaml.safe_dump(config)},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": _meta(name, DEX_NS),
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [container],
                        "volumes": [{"name": "config", "configMap": {"name": name}}],
                    },
                },
            },
        },
    ]


def _start_dex(name: str) -> str:
    """Create Service + Deployment ``name`` in DEX_NS; return its issuer URL once discovery answers."""
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": _meta(name, DEX_NS),
            "spec": {"selector": {"app": name}, "ports": [{"port": 5556, "targetPort": 5556}]},
        }
    )
    ip = _k("-n", DEX_NS, "get", "svc", name, "-o", "jsonpath={.spec.clusterIP}").stdout.strip()
    issuer = f"http://{ip}:5556/dex"
    _apply(*_dex_objects(name, issuer))
    _k("-n", DEX_NS, "rollout", "status", f"deployment/{name}", "--timeout=180s")
    _wait_discovery(issuer)
    return issuer


def _wait_discovery(issuer: str, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=3).json()["issuer"] == issuer:
                return
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise AssertionError(f"Dex at {issuer} did not serve discovery within {timeout}s")


def _token(issuer: str, user: str, client_id: str = "k7-cli") -> str:
    """A real Dex access token (a JWT with the ID token's claims) via the password grant."""
    r = httpx.post(
        f"{issuer}/token",
        data={
            "grant_type": "password",
            "username": f"{user}@example.com",
            "password": PASSWORD,
            "client_id": client_id,
            "scope": "openid email profile groups",
        },
        timeout=15,
    )
    assert r.status_code == 200, f"Dex password grant for {user} failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _segment(token: str, i: int) -> dict:
    seg = token.split(".")[i]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def _claims(token: str) -> dict:
    return _segment(token, 1)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _approve_device(issuer: str, user_code: str, user: str) -> None:
    """What a human does in the browser: enter the code, then log in to Dex's local connector."""
    with httpx.Client(follow_redirects=True, timeout=15) as c:
        r = c.post(f"{issuer}/device/auth/verify_code", data={"user_code": user_code})
        assert 'name="password"' in r.text, f"Dex did not show its login form: {r.status_code} {r.text[:500]}"
        action = re.search(r'<form method="post" action="([^"]*)"', r.text)
        assert action, r.text[:500]
        target = urllib.parse.urljoin(str(r.url), html.unescape(action.group(1)))
        r = c.post(target, data={"login": f"{user}@example.com", "password": PASSWORD})
        assert "Login Successful" in r.text, f"Dex did not approve the device: {r.status_code} {r.text[:500]}"


# ── the environment ─────────────────────────────────────────────────


@dataclass
class Env:
    url: str
    verify: bool | str
    issuer: str
    rogue_issuer: str
    admin_key: str
    acme_ns: str
    globex_ns: str

    def call(self, method: str, path: str, *, token: str | None = None, key: str | None = None, **kw):
        headers = kw.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["X-API-Key"] = key
        return httpx.request(method, f"{self.url}{path}", headers=headers, verify=self.verify, timeout=60, **kw)

    def as_user(self, user: str, method: str, path: str, **kw):
        return self.call(method, path, token=_token(self.issuer, user), **kw)

    def admin(self, method: str, path: str, **kw):
        return self.call(method, path, key=self.admin_key, **kw)

    def put_bindings(self, bindings: list[dict], deny: list[str] | None = None) -> None:
        r = self.admin("PUT", "/api/v1/role-bindings", json={"bindings": bindings, "deny_subjects": deny or []})
        assert r.status_code == 200, r.text

    def cli(self, *args: str, config_dir: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(_DEV_SH), "--api-url", self.url, *args],
            env=_cli_env(config_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def _cli_env(config_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("K7_API_KEY", "K7_API_URL")}
    return {**env, "K7_CONFIG_DIR": str(config_dir)}


def _read(kind: str, name: str, namespace: str) -> dict | None:
    r = _k("-n", namespace, "get", kind, name, "-o", "json", check=False)
    if r.returncode != 0:
        assert "NotFound" in r.stderr, r.stderr
        return None
    obj = json.loads(r.stdout)
    return {
        "apiVersion": obj["apiVersion"],
        "kind": obj["kind"],
        "metadata": {k: obj["metadata"][k] for k in ("name", "namespace", "labels") if k in obj["metadata"]},
        "data": obj.get("data") or {},
    }


def _restore(kind: str, name: str, namespace: str, saved: dict | None) -> None:
    _k("-n", namespace, "delete", kind, name, "--ignore-not-found")
    if saved is not None:
        _k("-n", namespace, "create", "-f", "-", stdin=json.dumps(saved))


def _publish_oidc(data: dict[str, str]) -> None:
    """Replace ConfigMap k7-oidc and roll k7-api so every replica reads it."""
    _apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta("k7-oidc", system_ns()), "data": data})
    _roll_api(hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16])


def _api_replica_urls() -> list[str]:
    """Each Running k7-api pod. The JWKS cache is per process, and the Service does not pin a client to one pod."""
    pods = json.loads(_k("-n", system_ns(), "get", "pods", "-l", "app=k7-api", "-o", "json").stdout)["items"]
    urls = []
    for pod in pods:
        if pod["status"].get("phase") != "Running" or pod["metadata"].get("deletionTimestamp"):
            continue
        ip = pod["status"].get("podIP")
        if not ip:
            continue
        caddy = next((c for c in pod["spec"]["containers"] if c["name"] == "caddy"), None)
        if caddy:
            urls.append(f"https://{ip}:{caddy['ports'][0]['containerPort']}")
        else:
            urls.append(f"http://{ip}:8000")
    assert len(urls) >= 2, f"both k7-api replicas must be Running, found {urls}"
    return urls


def _whoami_replicas(token: str) -> list[httpx.Response]:
    # Pod IPs are not in the API certificate's SANs.
    return [
        httpx.request(
            "GET",
            f"{url}/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {token}"},
            verify=False,
            timeout=60,
        )
        for url in _api_replica_urls()
    ]


def _roll_api(annotation: str | None) -> None:
    patch = {"spec": {"template": {"metadata": {"annotations": {_OIDC_ANNOTATION: annotation}}}}}
    _k("-n", system_ns(), "patch", "deployment", "k7-api", "--type=merge", "-p", json.dumps(patch))
    _k("-n", system_ns(), "rollout", "status", "deployment/k7-api", "--timeout=300s")


def _wait_oidc_served(env: Env, expect: int, timeout: int = 120) -> None:
    """Both replicas answer GET /api/v1/auth/oidc with ``expect`` (10 in a row)."""
    deadline, streak = time.monotonic() + timeout, 0
    while time.monotonic() < deadline and streak < 10:
        try:
            ok = env.call("GET", "/api/v1/auth/oidc").status_code == expect
        except httpx.HTTPError:
            ok = False
        streak = streak + 1 if ok else 0
        time.sleep(0.3 if ok else 2)
    assert streak >= 10, f"k7-api did not serve /api/v1/auth/oidc with {expect} within {timeout}s"


def _ensure_tenant_crd() -> bool:
    """Create a minimal Tenant CRD when none is installed; return whether this test owns it."""
    if _k("get", "crd", _TENANT_CRD, check=False).returncode == 0:
        return False
    _apply(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": _meta(_TENANT_CRD),
            "spec": {
                "group": "k7.katakate.org",
                "names": {"kind": "Tenant", "plural": "tenants", "singular": "tenant", "listKind": "TenantList"},
                "scope": "Cluster",
                "versions": [
                    {
                        "name": "v1alpha1",
                        "served": True,
                        "storage": True,
                        "schema": {
                            "openAPIV3Schema": {
                                "type": "object",
                                "properties": {
                                    "spec": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
                                    "status": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
                                },
                            }
                        },
                    }
                ],
            },
        }
    )
    _k("wait", "--for=condition=Established", f"crd/{_TENANT_CRD}", "--timeout=60s")
    return True


@pytest.fixture(scope="module")
def env() -> Iterator[Env]:
    if not _api_pod_ready():
        pytest.skip("k7-api deployment not ready")
    url, verify, ns = _get_api_base_url(), _tls_verify(), system_ns()
    suffix = uuid4().hex[:6]
    acme_ns, globex_ns = f"k7-test-oidc-acme-{suffix}", f"k7-test-oidc-globex-{suffix}"
    saved_oidc = _read("configmap", "k7-oidc", ns)
    saved_bindings = _read("configmap", "k7-role-bindings", ns)
    saved_annotation = json.loads(
        _k("-n", ns, "get", "deployment", "k7-api", "-o", "jsonpath={.spec.template.metadata.annotations}").stdout
        or "{}"
    ).get(_OIDC_ANNOTATION)
    owns_crd = rolled = False
    admin_name = f"itest-oidc-admin-{suffix}"
    try:
        _apply({"apiVersion": "v1", "kind": "Namespace", "metadata": _meta(DEX_NS)})
        issuer, rogue = _start_dex("dex"), _start_dex("rogue-dex")
        for n in (acme_ns, globex_ns):
            _k("create", "namespace", n)
        owns_crd = _ensure_tenant_crd()
        _apply(
            *(
                {"apiVersion": "k7.katakate.org/v1alpha1", "kind": "Tenant", "metadata": _meta(t), "spec": spec}
                for t, spec in ((ACME, {"namespaces": [acme_ns]}), (GLOBEX, {"namespaces": [globex_ns]}))
            )
        )
        from k7.deploy.k7_api_keys import Kubectl, generate

        admin_key = generate(
            Kubectl(KUBECTL, ns), admin_name, role="admin", tenant=None, expires_days=1, namespaces=[], nodes=[]
        )[0]
        _wait_key_live(admin_key)
        oidc_data = {
            "K7_OIDC_ISSUER": issuer,
            "K7_OIDC_AUDIENCE": "k7-cli",
            "K7_OIDC_CLIENT_ID": "k7-cli",
            "K7_OIDC_GROUPS_CLAIM": "groups",
            "K7_OIDC_USERNAME_CLAIM": "email",
            "K7_OIDC_ALLOW_HTTP_ISSUER": "1",
        }
        _apply({"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta("k7-oidc", ns), "data": oidc_data})
        rolled = True
        _roll_api(hashlib.sha256(json.dumps(oidc_data, sort_keys=True).encode()).hexdigest()[:16])
        e = Env(url, verify, issuer, rogue, admin_key, acme_ns, globex_ns)
        _wait_oidc_served(e, 200)
        e.put_bindings(BINDINGS)
        yield e
    finally:
        from k7.deploy.k7_api_keys import Kubectl, revoke

        steps = [
            lambda: _restore("configmap", "k7-role-bindings", ns, saved_bindings),
            lambda: _restore("configmap", "k7-oidc", ns, saved_oidc),
            lambda: _roll_api(saved_annotation) if rolled else None,
            lambda: revoke(Kubectl(KUBECTL, ns), admin_name),
            *(lambda n=n: _teardown_namespace(n) for n in (acme_ns, globex_ns) if _ns_exists(n)),
            lambda: _k("delete", _TENANT_CRD, ACME, GLOBEX, "--ignore-not-found") if _crd_exists() else None,
            lambda: _k("delete", "crd", _TENANT_CRD, "--ignore-not-found") if owns_crd else None,
            lambda: _k("delete", "namespace", DEX_NS, "--ignore-not-found", "--wait=true", "--timeout=180s"),
        ]
        errors = []
        for step in steps:
            try:
                step()
            except Exception as e:  # finish every restore step, then fail loudly
                errors.append(e)
        if errors:
            raise AssertionError(f"OIDC test teardown failed: {errors}")


def _ns_exists(name: str) -> bool:
    return _k("get", "namespace", name, check=False).returncode == 0


def _crd_exists() -> bool:
    return _k("get", "crd", _TENANT_CRD, check=False).returncode == 0


def _message(r: httpx.Response) -> str:
    return r.json()["error"]["message"]


def _assert_status(r: httpx.Response, want: str, what: str) -> None:
    if want == "deny":
        assert r.status_code == 403, f"{what}: expected 403, got {r.status_code} {r.text}"
    else:
        assert r.status_code not in (401, 403, 500), f"{what}: expected to pass authz, got {r.status_code} {r.text}"


# ── B: k7 login (device flow) ───────────────────────────────────────


def _login(env: Env, user: str, config_dir: Path) -> str:
    """Run `k7 login` (device flow) and approve it through Dex; return the CLI output."""
    proc = subprocess.Popen(
        [str(_DEV_SH), "login", "--api", env.url],
        env=_cli_env(config_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            out.append(line)
            m = re.search(r"enter code (\S+)", line)
            if m:
                _approve_device(env.issuer, m.group(1), user)
                break
        rest, _ = proc.communicate(timeout=90)
        out.append(rest)
    finally:
        if proc.poll() is None:
            proc.kill()
    text = "".join(out)
    assert proc.returncode == 0, f"k7 login failed:\n{text}"
    return text


class TestOidcLogin:
    def test_device_flow_login_and_group_binding_grants_user_on_one_tenant(self, env: Env, tmp_path: Path):
        out = _login(env, "alice", tmp_path)
        assert f"Logged in to {env.url}" in out and f"user on tenant {ACME}" in out, out
        creds = tmp_path / "credentials.json"
        assert oct(creds.stat().st_mode & 0o777) == "0o600"
        entry = json.loads(creds.read_text())[env.url]
        assert entry["issuer"] == env.issuer and entry["refresh_token"]

        me = env.cli("whoami", config_dir=tmp_path)
        assert me.returncode == 0, me.stderr
        assert f"user on tenant {ACME}" in me.stdout and "groups: itest-acme-devs" in me.stdout, me.stdout

        assert env.cli("list", "-n", env.acme_ns, config_dir=tmp_path).returncode == 0
        other = env.cli("list", "-n", env.globex_ns, config_dir=tmp_path)
        assert other.returncode != 0 and f"belongs to tenant {GLOBEX}" in other.stdout + other.stderr, other
        everywhere = env.cli("list", config_dir=tmp_path)
        assert everywhere.returncode != 0 and "all-namespaces" in everywhere.stdout + everywhere.stderr
        mint = env.cli("generate-api-key", "itest-escalate", "--role", "admin", "--api", config_dir=tmp_path)
        assert mint.returncode != 0 and "create_key needs role admin" in mint.stdout + mint.stderr, mint

        bye = env.cli("logout", config_dir=tmp_path)
        assert bye.returncode == 0 and "logged out" in bye.stdout, bye
        assert not creds.exists()

    def test_k7_login_refuses_a_token_for_another_audience(self, env: Env, tmp_path: Path):
        """The CLI checks the IdP issued a JWT for k7-api's audience before caching it."""
        from k7.cli import _oidc

        with pytest.raises(RuntimeError, match="audience"):
            _oidc.pick_token({"access_token": _token(env.issuer, "alice", client_id="other-app")}, "k7-cli")

    def test_expired_token_is_refused_and_the_cli_refreshes(self, env: Env, tmp_path: Path):
        token = _token(env.issuer, "alice")
        _login(env, "alice", tmp_path)
        assert env.call("GET", "/api/v1/auth/whoami", token=token).status_code == 200
        wait = _claims(token)["exp"] + 60 + 3 - time.time()  # k7-api LEEWAY is 60 s
        time.sleep(max(wait, 0))
        r = env.call("GET", "/api/v1/auth/whoami", token=token)
        assert r.status_code == 401 and "expired" in _message(r), r.text

        before = json.loads((tmp_path / "credentials.json").read_text())[env.url]
        me = env.cli("whoami", config_dir=tmp_path)
        assert me.returncode == 0 and f"user on tenant {ACME}" in me.stdout, me
        after = json.loads((tmp_path / "credentials.json").read_text())[env.url]
        assert after["expires_at"] > before["expires_at"] and after["access_token"] != before["access_token"]


# ── B: token refusals against the real API ──────────────────────────


def _oidc_invalid() -> float:
    return _sum(_scrape_app("k7-api"), "k7_auth_failures_total", reason="oidc_invalid")


class TestOidcTokenRefusals:
    def test_wrong_audience_is_401(self, env: Env):
        r = env.call("GET", "/api/v1/auth/whoami", token=_token(env.issuer, "dave", client_id="other-app"))
        assert r.status_code == 401 and "audience" in _message(r).lower(), r.text

    def test_unverified_email_is_401(self, env: Env):
        token = _token(env.issuer, "ivan")
        assert _claims(token).get("email_verified") is False, _claims(token)
        r = env.call("GET", "/api/v1/auth/whoami", token=token)
        assert r.status_code == 401 and "email_verified" in _message(r), r.text

    def test_foreign_issuer_is_401(self, env: Env):
        """A token from another IdP (its own issuer and keys) is never trusted, even for an admin group."""
        rogue = _token(env.rogue_issuer, "dave")
        assert _claims(rogue)["iss"] == env.rogue_issuer
        r = env.call("GET", "/api/v1/auth/whoami", token=rogue)
        assert r.status_code == 401, r.text

    def test_forged_tokens_are_401(self, env: Env):
        real = _token(env.issuer, "alice")
        header, payload, sig = real.split(".")
        hdr = _segment(real, 0)
        escalated = _b64(json.dumps({**_claims(real), "groups": ["itest-k7-admins"]}).encode())
        jwks_uri = httpx.get(f"{env.issuer}/.well-known/openid-configuration", timeout=10).json()["jwks_uri"]
        jwks = httpx.get(jwks_uri, timeout=10).json()
        n = next(k["n"] for k in jwks["keys"] if k["kid"] == hdr["kid"]).encode()
        hs_header = _b64(json.dumps({**hdr, "alg": "HS256"}).encode())
        hs_sig = _b64(hmac.new(n, f"{hs_header}.{payload}".encode(), hashlib.sha256).digest())
        forged = {
            "alg none": f"{_b64(json.dumps({**hdr, 'alg': 'none'}).encode())}.{payload}.",
            "HS256 keyed with the public key": f"{hs_header}.{payload}.{hs_sig}",
            "groups edited": f"{header}.{escalated}.{sig}",
            "signature stripped": f"{header}.{payload}.",
            "garbage": "a.b.c",
        }
        before = _oidc_invalid()
        for what, token in forged.items():
            r = env.call("GET", "/api/v1/auth/whoami", token=token)
            assert r.status_code == 401, f"{what}: {r.status_code} {r.text}"
        assert _oidc_invalid() == before + len(forged), "each JWT-shaped refusal counts once as oidc_invalid"

    def test_unbound_subject_is_403_naming_its_groups(self, env: Env):
        r = env.as_user("eve", "GET", "/api/v1/auth/whoami")
        assert r.status_code == 403 and "itest-contractors" in _message(r) and "eve@example.com" in _message(r)

    def test_deny_subjects_block_immediately_by_user_and_by_subject(self, env: Env):
        sub = _claims(_token(env.issuer, "alice"))["sub"]
        try:
            for deny in ("alice@example.com", sub):
                env.put_bindings(BINDINGS, [deny])
                for _ in range(6):  # both replicas, no restart
                    r = env.as_user("alice", "GET", "/api/v1/sandboxes", params={"namespace": env.acme_ns})
                    assert r.status_code == 403 and "deny-subjects" in _message(r), r.text
                assert env.as_user("bob", "GET", "/api/v1/sandboxes", params={"namespace": env.acme_ns}).is_success
        finally:
            env.put_bindings(BINDINGS)
        assert env.as_user("alice", "GET", "/api/v1/sandboxes", params={"namespace": env.acme_ns}).is_success

    def test_removing_a_binding_revokes_access_on_the_next_request(self, env: Env):
        try:
            env.put_bindings([b for b in BINDINGS if b["group"] != "itest-acme-devs"])
            r = env.as_user("alice", "GET", "/api/v1/sandboxes", params={"namespace": env.acme_ns})
            assert r.status_code == 403 and "no role binding" in _message(r)
        finally:
            env.put_bindings(BINDINGS)


# ── A + B: the role matrix over OIDC ────────────────────────────────

ALLOW, DENY = "allow", "deny"
_ROLES = ("alice", "bob", "carol", "dave")  # user@acme, read-only@acme, operator, admin


def _matrix(env: Env) -> list[tuple[str, str, dict, tuple[str, str, str, str]]]:
    a, g = env.acme_ns, env.globex_ns
    sb = "/api/v1/sandboxes/itest-missing"
    return [
        ("GET", "/api/v1/sandboxes", {"params": {"namespace": a}}, (ALLOW, ALLOW, ALLOW, ALLOW)),
        ("GET", "/api/v1/sandboxes", {"params": {"namespace": g}}, (DENY, DENY, ALLOW, ALLOW)),
        ("GET", "/api/v1/sandboxes", {}, (DENY, DENY, ALLOW, ALLOW)),
        ("GET", sb, {"params": {"namespace": a}}, (ALLOW, ALLOW, ALLOW, ALLOW)),
        ("GET", f"{sb}/logs", {"params": {"namespace": a}}, (ALLOW, DENY, DENY, ALLOW)),
        ("POST", f"{sb}/exec", {"params": {"namespace": a}, "json": {"command": "true"}}, (ALLOW, DENY, DENY, ALLOW)),
        ("POST", f"{sb}/pause", {"json": {"namespace": a}}, (ALLOW, DENY, DENY, ALLOW)),
        ("POST", f"{sb}/pause", {"json": {"namespace": g}}, (DENY, DENY, DENY, ALLOW)),
        ("DELETE", sb, {"params": {"namespace": a}}, (ALLOW, DENY, DENY, ALLOW)),
        ("DELETE", sb, {"params": {"namespace": g}}, (DENY, DENY, DENY, ALLOW)),
        ("GET", "/api/v1/snapshots", {"params": {"namespace": a}}, (ALLOW, ALLOW, ALLOW, ALLOW)),
        ("GET", "/api/v1/nodes/storage", {}, (DENY, DENY, ALLOW, ALLOW)),
        ("POST", "/api/v1/nodes/itest-no-such-node/dedicate", {"json": {"tenant": ACME}}, (DENY, DENY, ALLOW, ALLOW)),
        ("GET", "/api/v1/keys", {}, (DENY, DENY, DENY, ALLOW)),
        (
            "POST",
            "/api/v1/keys",
            {"json": {"name": "itest-x", "role": "bogus", "expires_days": 1}},
            (DENY,) * 3 + (ALLOW,),
        ),
        ("DELETE", "/api/v1/keys/itest-no-such-key", {}, (DENY, DENY, DENY, ALLOW)),
        ("GET", "/api/v1/role-bindings", {}, (DENY, DENY, DENY, ALLOW)),
        ("PUT", "/api/v1/role-bindings", {"json": {"bindings": "not-a-list"}}, (DENY, DENY, DENY, ALLOW)),
    ]


class TestRoleMatrixOverOidc:
    def test_every_role_on_every_route_class(self, env: Env):
        for method, path, kw, expect in _matrix(env):
            tokens = {u: _token(env.issuer, u) for u in _ROLES}  # fresh: some routes are slow
            for user, want in zip(_ROLES, expect, strict=True):
                r = env.call(method, path, token=tokens[user], **kw)
                _assert_status(r, want, f"{user} {method} {path} {kw.get('params')}")

    def test_spec_named_refusals_name_the_missing_role(self, env: Env):
        a = env.acme_ns
        r = env.as_user("bob", "POST", "/api/v1/sandboxes/x/exec", params={"namespace": a}, json={"command": "true"})
        assert r.status_code == 403 and "exec_command needs role user or admin" in _message(r)
        assert f"read-only on tenant {ACME}" in _message(r)
        r = env.as_user("carol", "POST", "/api/v1/sandboxes/x/exec", params={"namespace": a}, json={"command": "true"})
        assert r.status_code == 403 and "has operator" in _message(r)
        r = env.as_user("carol", "POST", "/api/v1/keys", json={"name": "x", "role": "user", "expires_days": 1})
        assert r.status_code == 403 and "create_key needs role admin" in _message(r)
        r = env.as_user("alice", "GET", "/api/v1/sandboxes", params={"namespace": env.globex_ns})
        assert r.status_code == 403 and f"belongs to tenant {GLOBEX}" in _message(r)

    def test_bindings_union_across_tenants(self, env: Env):
        """frank: read-only on acme and user on globex from two groups."""
        me = env.as_user("frank", "GET", "/api/v1/auth/whoami").json()["data"]
        assert me["grants"] == [f"read-only on tenant {ACME}", f"user on tenant {GLOBEX}"]
        r = env.as_user("frank", "DELETE", "/api/v1/sandboxes/x", params={"namespace": env.acme_ns})
        assert r.status_code == 403
        r = env.as_user("frank", "DELETE", "/api/v1/sandboxes/x", params={"namespace": env.globex_ns})
        _assert_status(r, ALLOW, "frank DELETE in globex")

    def test_tenant_user_runs_a_sandbox_only_in_its_tenant(self, env: Env):
        name = f"itest-oidc-{uuid4().hex[:6]}"
        a = env.acme_ns
        body = {"name": name, "image": "alpine:3.21", "namespace": a}
        try:
            r = env.as_user("bob", "POST", "/api/v1/sandboxes", json=body)
            assert r.status_code == 403, r.text
            r = env.as_user("alice", "POST", "/api/v1/sandboxes", json={**body, "namespace": env.globex_ns})
            assert r.status_code == 403, r.text
            r = env.as_user("alice", "POST", "/api/v1/sandboxes", json=body)
            assert r.status_code == 201, r.text
            _wait_for_sandbox_ready(env.url, {"X-API-Key": env.admin_key}, name, a)
            got = env.as_user("bob", "GET", f"/api/v1/sandboxes/{name}", params={"namespace": a})
            assert got.status_code == 200, got.text
            run = env.as_user(
                "alice", "POST", f"/api/v1/sandboxes/{name}/exec", params={"namespace": a}, json={"command": "echo hi"}
            )
            assert run.status_code == 200 and "hi" in json.dumps(run.json()["data"]), run.text
            r = env.as_user("bob", "DELETE", f"/api/v1/sandboxes/{name}", params={"namespace": a})
            assert r.status_code == 403
            r = env.as_user("alice", "DELETE", f"/api/v1/sandboxes/{name}", params={"namespace": a})
            assert r.status_code == 200, r.text
        finally:
            env.admin("DELETE", f"/api/v1/sandboxes/{name}", params={"namespace": a})


# ── escalation and the interaction with API keys and tenants ───────


class TestEscalationAndKeys:
    def test_no_role_can_grant_itself_more(self, env: Env):
        grab = [*BINDINGS, {"user": "alice@example.com", "role": "admin"}]
        for user in ("alice", "bob", "carol"):
            r = env.as_user(user, "PUT", "/api/v1/role-bindings", json={"bindings": grab, "deny_subjects": []})
            assert r.status_code == 403, f"{user}: {r.text}"
            r = env.as_user(
                user, "POST", "/api/v1/keys", json={"name": "itest-esc", "role": "admin", "expires_days": 1}
            )
            assert r.status_code == 403, f"{user}: {r.text}"
            r = env.as_user(user, "DELETE", "/api/v1/keys/itest-any-key")
            assert r.status_code == 403, f"{user}: {r.text}"
        stored = env.admin("GET", "/api/v1/role-bindings").json()["data"]
        assert stored == {"bindings": BINDINGS, "deny_subjects": []}
        r = env.as_user("alice", "GET", "/api/v1/auth/whoami")
        assert r.json()["data"]["grants"] == [f"user on tenant {ACME}"]

    def test_binding_validation_over_the_api(self, env: Env):
        for bad, msg in (
            ([{"group": "g", "role": "user", "tenant": "itest-ghost"}], "does not exist"),
            ([{"group": "g", "role": "admin", "tenant": ACME}], "cluster-scoped"),
            ([{"group": "g", "role": "user"}], "tenant-scoped"),
            ([{"group": "g", "role": "root"}], "role must be one of"),
            ([{"group": "g", "role": "admin"}, {"group": "g", "role": "admin"}], "duplicates"),
        ):
            r = env.admin("PUT", "/api/v1/role-bindings", json={"bindings": bad})
            assert r.status_code == 400 and msg in _message(r), f"{bad}: {r.text}"
        assert env.admin("GET", "/api/v1/role-bindings").json()["data"]["bindings"] == BINDINGS

    def test_oidc_admin_mints_scoped_keys_that_keep_their_scope(self, env: Env):
        names = [f"itest-oidc-user-{uuid4().hex[:6]}", f"itest-oidc-op-{uuid4().hex[:6]}"]
        try:
            for body, msg in (
                ({"name": "itest-bad", "role": "admin", "tenant": ACME, "expires_days": 1}, "cluster-scoped"),
                ({"name": "itest-bad", "role": "user", "expires_days": 1}, "tenant-scoped"),
                ({"name": "itest-bad", "role": "user", "tenant": "itest-ghost", "expires_days": 1}, "does not exist"),
            ):
                r = env.as_user("dave", "POST", "/api/v1/keys", json=body)
                assert r.status_code == 400 and msg in _message(r), r.text

            r = env.as_user(
                "dave",
                "POST",
                "/api/v1/keys",
                json={"name": names[0], "role": "user", "tenant": ACME, "expires_days": 1},
            )
            assert r.status_code == 201 and r.json()["data"]["tenant"] == ACME, r.text
            user_key = r.json()["data"]["token"]
            r = env.as_user(
                "dave", "POST", "/api/v1/keys", json={"name": names[1], "role": "operator", "expires_days": 1}
            )
            assert r.status_code == 201, r.text
            op_key = r.json()["data"]["token"]
            _wait_key_live(user_key)
            _wait_key_live(op_key)

            listed = {k["name"]: k for k in env.as_user("dave", "GET", "/api/v1/keys").json()["data"]}
            assert listed[names[0]]["role"] == "user" and listed[names[0]]["tenant"] == ACME
            assert all("hash" not in k and "token" not in k for k in listed.values())

            a, g = env.acme_ns, env.globex_ns
            assert env.call("GET", "/api/v1/sandboxes", key=user_key, params={"namespace": a}).status_code == 200
            assert env.call("GET", "/api/v1/sandboxes", key=user_key, params={"namespace": g}).status_code == 403
            assert env.call("GET", "/api/v1/sandboxes", token=user_key, params={"namespace": a}).status_code == 200
            r = env.call("POST", "/api/v1/keys", key=user_key, json={"name": "x", "role": "admin", "expires_days": 1})
            assert r.status_code == 403
            assert env.call("GET", "/api/v1/sandboxes", key=op_key).status_code == 200
            r = env.call("POST", "/api/v1/sandboxes/x/exec", key=op_key, params={"namespace": a}, json={"command": "1"})
            assert r.status_code == 403

            # X-API-Key wins over a valid bearer JWT: a bad key is 401, never a fallback to OIDC.
            dave = _token(env.issuer, "dave")
            r = env.call("GET", "/api/v1/auth/whoami", token=dave, key="k7_aaaaaaaa_" + "A" * 43)
            assert r.status_code == 401
            assert env.call("GET", "/api/v1/auth/whoami", token=dave).json()["data"]["type"] == "oidc"

            assert env.as_user("dave", "DELETE", f"/api/v1/keys/{names[0]}").status_code == 200
            time.sleep(2 * POLL_SECONDS)  # every replica's key-store poll
            for _ in range(6):
                assert env.call("GET", "/api/v1/sandboxes", key=user_key, params={"namespace": a}).status_code == 401
        finally:
            for n in names:
                env.admin("DELETE", f"/api/v1/keys/{n}")

    def test_keys_are_unaffected_by_bindings(self, env: Env):
        """An admin key keeps working while no binding matches anyone (keys are the break-glass path)."""
        try:
            env.put_bindings([])
            assert env.as_user("dave", "GET", "/api/v1/keys").status_code == 403
            assert env.admin("GET", "/api/v1/keys").status_code == 200
        finally:
            env.put_bindings(BINDINGS)


# ── audit ───────────────────────────────────────────────────────────


def _audit_lines(request_id: str) -> list[dict]:
    logs = _k("-n", system_ns(), "logs", "-l", "app=k7-api", "-c", "k7-api", "--tail=3000").stdout
    lines = [json.loads(line) for line in logs.splitlines() if request_id in line]
    return [line for line in lines if line["logger"] == "k7.audit"]


class TestOidcAudit:
    def test_mutating_and_refused_requests_carry_subject_and_role(self, env: Env):
        token = _token(env.issuer, "alice")
        sub = _claims(token)["sub"]
        r = env.call("DELETE", "/api/v1/sandboxes/itest-audit-missing", token=token, params={"namespace": env.acme_ns})
        _assert_status(r, ALLOW, "alice DELETE in acme")
        denied = env.as_user("eve", "DELETE", "/api/v1/sandboxes/x", params={"namespace": env.acme_ns})
        assert denied.status_code == 403
        time.sleep(1)
        (line,) = _audit_lines(r.headers["X-Request-ID"])
        assert line["principal_type"] == "oidc" and line["subject"] == sub
        assert (line["role"], line["tenant"], line["namespace"]) == ("user", ACME, env.acme_ns)
        assert line["idp_issuer"] == env.issuer and line["key_id"] is None
        assert "alice@example.com" not in json.dumps(line) and "itest-acme-devs" not in json.dumps(line)
        (refused,) = _audit_lines(denied.headers["X-Request-ID"])
        assert refused["status"] == 403 and refused["principal_type"] == "oidc" and refused["subject"]
        logs = _k("-n", system_ns(), "logs", "-l", "app=k7-api", "-c", "k7-api", "--tail=3000").stdout
        assert token not in logs and token.split(".")[2] not in logs


# ── JWKS refresh and IdP outage (restarts Dex: keep last) ───────────


class TestZzSigningKeys:
    def test_idp_outage_keeps_keys_working_and_rotation_refreshes_jwks(self, env: Env):
        old = _token(env.issuer, "alice")
        assert env.call("GET", "/api/v1/auth/whoami", token=old).status_code == 200
        _k("-n", DEX_NS, "scale", "deployment/dex", "--replicas=0")
        _k("-n", DEX_NS, "wait", "--for=delete", "pod", "-l", "app=dex", "--timeout=120s")
        time.sleep(11)  # past the per-replica JWKS refresh rate limit
        rogue = _token(env.rogue_issuer, "alice")  # unknown kid: k7-api must refetch the JWKS and cannot
        r = env.call("GET", "/api/v1/auth/whoami", token=rogue)
        assert r.status_code == 503 and "cannot fetch the OIDC signing keys" in _message(r), r.text
        for _ in range(4):  # a failed refresh stays 503 through the rate limit, never 401 or 200
            got = env.call("GET", "/api/v1/auth/whoami", token=rogue)
            assert got.status_code == 503 and "cannot fetch the OIDC signing keys" in _message(got), got.text
        assert env.admin("GET", "/api/v1/keys").status_code == 200
        assert env.call("GET", "/api/v1/auth/whoami", token=old).status_code == 200  # cached keys still verify

        _k("-n", DEX_NS, "scale", "deployment/dex", "--replicas=1")  # memory storage: new signing keys
        _k("-n", DEX_NS, "rollout", "status", "deployment/dex", "--timeout=180s")
        _wait_discovery(env.issuer)
        new = _token(env.issuer, "alice")
        assert _segment(new, 0)["kid"] != _segment(old, 0)["kid"]
        time.sleep(11)
        for _ in range(6):  # each replica refreshes on the unknown kid
            r = env.call("GET", "/api/v1/auth/whoami", token=new)
            assert r.status_code == 200, r.text
        time.sleep(11)
        for _ in range(6):
            r = env.call("GET", "/api/v1/auth/whoami", token=old)
            assert r.status_code == 401 and "unknown key id" in _message(r), r.text

    def test_retired_signing_key_stops_being_trusted_after_the_ttl(self, env: Env):
        """A key Dex no longer publishes stays trusted until the JWKS TTL, then is refused.

        The TTL here is 120s (the pin is 3600) so the test can outlast a Dex
        restart and still finish inside the module. Restarting Dex rotates the
        signing key; requests keep presenting the old kid, so nothing refreshes
        the cache until it expires. Each replica is addressed by pod IP: a
        Service request can land on a process that never cached the key, and
        that process fetches Dex's new JWKS and answers 401 inside the TTL.
        """
        ttl = 120
        ns = system_ns()
        saved = _read("configmap", "k7-oidc", ns)
        assert saved is not None and saved["data"].get("K7_OIDC_ISSUER")
        try:
            _publish_oidc({**saved["data"], "K7_OIDC_JWKS_TTL_SECONDS": str(ttl)})
            old = _token(env.issuer, "alice")
            first = time.monotonic()
            for r in _whoami_replicas(old):  # warm every replica while Dex still publishes this kid
                assert r.status_code == 200, r.text
            last = time.monotonic()
            _k("-n", DEX_NS, "scale", "deployment/dex", "--replicas=0")
            _k("-n", DEX_NS, "wait", "--for=delete", "pod", "-l", "app=dex", "--timeout=120s")
            _k("-n", DEX_NS, "scale", "deployment/dex", "--replicas=1")
            _k("-n", DEX_NS, "rollout", "status", "deployment/dex", "--timeout=180s")
            _wait_discovery(env.issuer)
            assert time.monotonic() < first + ttl, "Dex restart ran past the JWKS TTL; raise ttl"
            for r in _whoami_replicas(old):
                assert r.status_code == 200, f"retired key was dropped inside the TTL: {r.text}"
            time.sleep(last + ttl + 3 - time.monotonic())
            new = _token(env.issuer, "alice")
            assert _segment(new, 0)["kid"] != _segment(old, 0)["kid"]
            for r in _whoami_replicas(old):
                assert r.status_code == 401 and "unknown key id" in _message(r), r.text
            for r in _whoami_replicas(new):
                assert r.status_code == 200, r.text
        finally:
            if saved is not None:
                _publish_oidc(saved["data"])
