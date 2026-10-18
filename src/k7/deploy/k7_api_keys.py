"""API key schema, token format, and kubectl-backed key Secret writers.

Stdlib only: the install playbook runs ``python3 k7_api_keys.py migrate``
on the first master before the k7-api Deployment rolls, and the CLI key
commands call the same functions.

Secret ``k7-api-keys`` (system namespace): data key = key id, value =
JSON ``{name, hash, created, expires, role, tenant, namespaces, nodes}``.
``role`` is one of ``ROLES``; ``read-only``/``user`` are scoped
to a ``tenant``, ``operator``/``admin`` to the cluster.
Secret ``k7-api-keys-usage``: data key = key id, value = last-used epoch.

Tokens are ``k7_<id>_<secret>``: ``<id>`` is 8 base32 chars (lookup key,
safe to log), ``<secret>`` is ``secrets.token_urlsafe(32)``; the store
holds ``sha256(secret)``. Legacy bare tokens are stored under
``legacy-<hash[:8]>`` with ``hash = sha256(token)`` and matched by scan.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import time
from pathlib import Path

KEYS_SECRET = "k7-api-keys"
USAGE_SECRET = "k7-api-keys-usage"
LEGACY_FILE = Path(os.environ.get("K7_API_KEYS_FILE", "/etc/k7/api_keys.json"))
LEGACY_PREFIX = "legacy-"
_TOKEN_RE = re.compile(r"k7_([a-z2-7]{8})_([A-Za-z0-9_-]{43})")
TENANT_ROLES = ("read-only", "user")
CLUSTER_ROLES = ("operator", "admin")
ROLES = TENANT_ROLES + CLUSTER_ROLES
TENANT_RESOURCE = "tenants.k7.katakate.org"


def validate_scope(role: str, tenant: str | None) -> None:
    """Tenant roles need a tenant; cluster roles forbid one."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}, got {role!r}")
    if role in TENANT_ROLES and not tenant:
        raise ValueError(f"role {role} is tenant-scoped: pass a tenant")
    if role in CLUSTER_ROLES and tenant:
        raise ValueError(f"role {role} is cluster-scoped: a tenant is not allowed")


def new_entry(
    name: str, *, role: str, tenant: str | None, expires_days: int, namespaces: list[str], nodes: list[str]
) -> tuple[str, str, dict]:
    """Mint a token and its store entry; return ``(token, key_id, entry)``."""
    validate_scope(role, tenant)
    token, key_id, digest = mint_token()
    now = int(time.time())
    entry: dict = {"name": name, "hash": digest, "created": now, "expires": now + expires_days * 86400, "role": role}
    if tenant:
        entry["tenant"] = tenant
    if namespaces:
        entry["namespaces"] = namespaces
    if nodes:
        entry["nodes"] = nodes
    return token, key_id, entry


def migrate_roles(keys: dict[str, dict]) -> tuple[dict[str, str], list[str]]:
    """Give every key from before roles a ``role``; return ``(Secret patch, one line per change)``.

    ``admin: true`` becomes ``admin``; a tenant- or namespace-scoped key
    becomes ``user``; an unscoped non-admin key becomes ``admin`` flagged
    ``unscoped_legacy`` (development mode only; ``list-api-keys`` warns).
    """
    patch: dict[str, str] = {}
    lines: list[str] = []
    for key_id, entry in sorted(keys.items()):
        if "role" in entry:
            continue
        new = {k: v for k, v in entry.items() if k != "admin"}
        if entry.get("admin") is True:
            new["role"], why = "admin", "admin: true"
        elif entry.get("tenant") or entry.get("namespaces"):
            new["role"], why = "user", f"scoped to {entry.get('tenant') or entry.get('namespaces')}"
        else:
            new["role"], new["unscoped_legacy"], why = "admin", True, "unscoped non-admin key (development mode only)"
        patch[key_id] = json.dumps(new)
        lines.append(f"key {key_id} ({entry.get('name')}): {why} -> role {new['role']}")
    return patch, lines


def unscoped_keys(keys: dict[str, dict]) -> dict[str, dict]:
    """Production mode refuses these keys: no role (from before roles), a migrated
    ``unscoped_legacy`` admin, or a tenant role with no tenant (a namespace-only
    key from before tenants)."""
    return {
        k: e
        for k, e in keys.items()
        if e.get("unscoped_legacy") or e.get("role") not in CLUSTER_ROLES and not e.get("tenant")
    }


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def mint_token() -> tuple[str, str, str]:
    """Return ``(token, key_id, sha256(secret))``."""
    key_id = base64.b32encode(secrets.token_bytes(5)).decode().lower()
    secret = secrets.token_urlsafe(32)
    return f"k7_{key_id}_{secret}", key_id, sha256_hex(secret)


def token_key_id(token: str) -> str | None:
    m = _TOKEN_RE.fullmatch(token)
    return m.group(1) if m else None


def match_token(keys: dict[str, dict], token: str) -> tuple[str, dict] | None:
    """Return ``(key_id, entry)`` for a valid token (expiry not checked)."""
    m = _TOKEN_RE.fullmatch(token)
    if m:
        entry = keys.get(m.group(1))
        if entry and secrets.compare_digest(str(entry.get("hash", "")), sha256_hex(m.group(2))):
            return m.group(1), entry
        return None
    digest = sha256_hex(token)
    for key_id, entry in keys.items():
        if key_id.startswith(LEGACY_PREFIX) and secrets.compare_digest(str(entry.get("hash", "")), digest):
            return key_id, entry
    return None


def legacy_entries(file_data: dict) -> dict[str, dict]:
    """Convert ``/etc/k7/api_keys.json`` (``{sha256(token): entry}``) to Secret entries."""
    out: dict[str, dict] = {}
    for digest, entry in file_data.items():
        key_id = LEGACY_PREFIX + digest[:8]
        if key_id in out:
            raise ValueError(f"legacy key id collision on {key_id}; revoke one of the colliding keys first")
        out[key_id] = {**{k: v for k, v in entry.items() if k != "last_used"}, "hash": digest}
    return out


def decode_keys(data: dict[str, str]) -> dict[str, dict]:
    return {k: json.loads(v) for k, v in data.items()}


class Kubectl:
    """Minimal Secret access via ``kubectl`` (the node's admin kubeconfig)."""

    def __init__(self, argv: list[str], namespace: str):
        self.argv = argv
        self.namespace = namespace

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.argv + args + ["-n", self.namespace], capture_output=True, text=True, check=False)

    def _check(self, args: list[str]) -> None:
        r = self._run(args)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(self.argv + args)} failed: {r.stderr.strip()}")

    def read(self, name: str) -> dict[str, str] | None:
        r = self._run(["get", "secret", name, "-o", "json"])
        if r.returncode != 0:
            if "NotFound" in r.stderr:
                return None
            raise RuntimeError(f"reading Secret {self.namespace}/{name} failed: {r.stderr.strip()}")
        data = json.loads(r.stdout).get("data") or {}
        return {k: base64.b64decode(v).decode() for k, v in data.items()}

    def ensure(self, name: str) -> None:
        if self.read(name) is None:
            self._check(["create", "secret", "generic", name])

    def patch(self, name: str, data: dict[str, str | None]) -> None:
        """JSON merge patch of ``data``; ``None`` deletes a key."""
        body = {"data": {k: None if v is None else base64.b64encode(v.encode()).decode() for k, v in data.items()}}
        self._check(["patch", "secret", name, "--type", "merge", "-p", json.dumps(body)])

    def require_tenant(self, tenant: str) -> list[str]:
        """Return the tenant's ``spec.namespaces``; loud when it is missing or unreadable."""
        r = subprocess.run(self.argv + ["get", TENANT_RESOURCE, tenant, "-o", "json"], capture_output=True, text=True)
        if r.returncode == 0:
            return list(json.loads(r.stdout)["spec"]["namespaces"])
        if "NotFound" in r.stderr:
            raise RuntimeError(f"tenant {tenant!r} does not exist")
        if "doesn't have a resource type" in r.stderr:
            raise RuntimeError(f"the Tenant CRD ({TENANT_RESOURCE}) is not installed")
        raise RuntimeError(f"reading tenant {tenant!r} failed: {r.stderr.strip()}")


def generate(
    kc: Kubectl,
    name: str,
    *,
    role: str,
    tenant: str | None,
    expires_days: int,
    namespaces: list[str],
    nodes: list[str],
    legacy_file: Path | None = None,
) -> tuple[str, str, dict]:
    """Create a key in the Secret; return ``(token, key_id, entry)``."""
    legacy_file = legacy_file or LEGACY_FILE
    if legacy_file.exists():
        raise RuntimeError(f"{legacy_file} has not been migrated; run `k7 api keys migrate` first")
    token, key_id, entry = new_entry(
        name, role=role, tenant=tenant, expires_days=expires_days, namespaces=namespaces, nodes=nodes
    )
    if tenant:
        allowed = kc.require_tenant(tenant)
        outside = [ns for ns in namespaces if ns not in allowed]
        if outside:
            raise RuntimeError(
                f"namespace(s) {', '.join(outside)} are not in tenant {tenant}; the key could never reach them"
            )
    kc.ensure(KEYS_SECRET)
    kc.patch(KEYS_SECRET, {key_id: json.dumps(entry)})
    return token, key_id, entry


def revoke(kc: Kubectl, name: str) -> list[str]:
    """Delete every key named ``name``; return the revoked ids."""
    keys = decode_keys(kc.read(KEYS_SECRET) or {})
    ids = [k for k, e in keys.items() if e.get("name") == name]
    if ids:
        kc.patch(KEYS_SECRET, dict.fromkeys(ids))
    return ids


def bind_tenant(kc: Kubectl, key_ids: list[str], tenant: str) -> None:
    """Bind existing keys to ``tenant`` (``k7 tenant adopt --apply``); their ``namespaces`` stay as a narrowing.

    A key keeps a tenant role it already has (``read-only``/``user``); anything else becomes ``user``.
    """
    keys = decode_keys(kc.read(KEYS_SECRET) or {})
    missing = [k for k in key_ids if k not in keys]
    if missing:
        raise RuntimeError(f"key(s) {', '.join(missing)} not found in Secret {kc.namespace}/{KEYS_SECRET}")
    kc.require_tenant(tenant)
    patch = {}
    for k in key_ids:
        entry = {f: v for f, v in keys[k].items() if f not in ("admin", "unscoped_legacy")}
        entry["role"] = entry.get("role") if entry.get("role") in TENANT_ROLES else "user"
        patch[k] = json.dumps({**entry, "tenant": tenant})
    kc.patch(KEYS_SECRET, patch)


def migrate(kc: Kubectl, legacy_file: Path | None = None) -> str:
    """Move the legacy key file into the Secret, then give every key a role. Idempotent."""
    lines = [_migrate_file(kc, legacy_file or LEGACY_FILE)]
    patch, changes = migrate_roles(decode_keys(kc.read(KEYS_SECRET) or {}))
    if patch:
        kc.patch(KEYS_SECRET, dict(patch))
    return "\n".join(lines + (changes or ["every key already has a role"]))


def _migrate_file(kc: Kubectl, legacy_file: Path) -> str:
    """Copy the legacy key file into the Secret, verify, then rename it."""
    kc.ensure(USAGE_SECRET)
    current = kc.read(KEYS_SECRET)
    if not legacy_file.exists():
        if current is None:
            kc.ensure(KEYS_SECRET)
        return f"no {legacy_file}; Secret {kc.namespace}/{KEYS_SECRET} is the key store"
    file_data = json.loads(legacy_file.read_text())
    wanted = legacy_entries(file_data)
    have = decode_keys(current or {})
    if have and any(have.get(k) != v for k, v in wanted.items()):
        raise RuntimeError(
            f"both {legacy_file} and Secret {kc.namespace}/{KEYS_SECRET} exist with different content; "
            "reconcile by hand (move one aside), then re-run `k7 api keys migrate`"
        )
    kc.ensure(KEYS_SECRET)
    if wanted:
        kc.patch(KEYS_SECRET, {k: json.dumps(v) for k, v in wanted.items()})
    usage = {
        LEGACY_PREFIX + d[:8]: str(e["last_used"]) for d, e in file_data.items() if isinstance(e.get("last_used"), int)
    }
    if usage:
        kc.patch(USAGE_SECRET, dict(usage))
    stored = decode_keys(kc.read(KEYS_SECRET) or {})
    if any(stored.get(k) != v for k, v in wanted.items()):
        raise RuntimeError(f"Secret {kc.namespace}/{KEYS_SECRET} does not match {legacy_file} after copy; file kept")
    migrated = legacy_file.with_name(legacy_file.name + ".migrated")
    legacy_file.rename(migrated)
    return f"migrated {len(wanted)} key(s) from {legacy_file} to Secret {kc.namespace}/{KEYS_SECRET}; renamed to {migrated}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="k7 API key store")
    p.add_argument("command", choices=["migrate"])
    p.add_argument("--file", default=str(LEGACY_FILE))
    p.add_argument("--kubectl", default="k3s kubectl")
    p.add_argument("--namespace", required=True)
    args = p.parse_args(argv)
    print(migrate(Kubectl(shlex.split(args.kubectl), args.namespace), Path(args.file)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
