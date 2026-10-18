"""K7 application roles. Not Kubernetes RBAC: k7-api's
ServiceAccount stays the only Kubernetes actor.

``PERMISSIONS`` maps every FastAPI route name to the roles allowed on it
(``None`` = public). ``main.require`` runs on every route and refuses a
route that is missing here; ``test_authz`` fails on drift.

Checks compose in order: route role, then tenant for tenant
roles, then the key's own ``namespaces``/``nodes`` narrowing. The first
failure is a 403 naming the missing role or scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException

from ..core.core import K7Core
from ..deploy.k7_api_keys import CLUSTER_ROLES
from .tenants import namespace_tenant

RO, USER, OPERATOR, ADMIN = "read-only", "user", "operator", "admin"
_READ = frozenset({RO, USER, OPERATOR, ADMIN})
_TENANT_WRITE = frozenset({USER, ADMIN})
_OPS = frozenset({OPERATOR, ADMIN})
_ADMIN = frozenset({ADMIN})
_RANK = {RO: 0, USER: 1, OPERATOR: 2, ADMIN: 3}

PERMISSIONS: dict[str, frozenset[str] | None] = {
    "root": None,
    "health": None,
    "oidc_config": None,
    "whoami": _READ,
    "list_sandboxes": _READ,
    "get_sandbox": _READ,
    "get_sandbox_metrics": _READ,
    "list_snapshots": _READ,
    "get_snapshot": _READ,
    "create_sandbox": _TENANT_WRITE,
    "delete_sandbox": _TENANT_WRITE,
    "delete_all_sandboxes": _TENANT_WRITE,
    "pause_sandbox": _TENANT_WRITE,
    "resume_sandbox": _TENANT_WRITE,
    "fork_sandbox": _TENANT_WRITE,
    "get_sandbox_logs": _TENANT_WRITE,
    "exec_command": _TENANT_WRITE,
    "create_snapshot": _TENANT_WRITE,
    "delete_snapshot": _TENANT_WRITE,
    "restore_snapshot": _TENANT_WRITE,
    "gc_snapshots": _OPS,
    "get_nodes_storage": _OPS,
    "dedicate_node": _OPS,
    "undedicate_node": _OPS,
    "install_node": _ADMIN,
    "install_status": _ADMIN,
    "create_key": _ADMIN,
    "list_keys": _ADMIN,
    "delete_key": _ADMIN,
    "get_role_bindings": _ADMIN,
    "put_role_bindings": _ADMIN,
    "list_tenants_route": _OPS,
    "get_tenant_route": _OPS,
    "create_tenant_route": _ADMIN,
    "update_tenant_route": _ADMIN,
    "delete_tenant_route": _ADMIN,
    "get_usage": _READ,
}


def highest(roles: list[str]) -> str:
    return max(roles, key=_RANK.__getitem__)


@dataclass
class Principal:
    """Who is calling and what they hold.

    ``legacy_role`` is a key from before tenants: a tenant role with ``namespaces`` but
    no ``tenant``, narrowed by its namespace list only.
    """

    type: str  # "key" | "oidc"
    subject: str
    label: str
    cluster_role: str | None = None
    tenant_roles: dict[str, str] = field(default_factory=dict)
    legacy_role: str | None = None
    namespaces: list[str] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    issuer: str | None = None
    groups: list[str] = field(default_factory=list)
    allowed: frozenset[str] = frozenset()

    def grants(self) -> list[str]:
        out = [self.cluster_role] if self.cluster_role else []
        out += [f"{r} on tenant {t}" for t, r in sorted(self.tenant_roles.items())]
        if self.legacy_role:
            out.append(f"{self.legacy_role} on namespaces {', '.join(self.namespaces)}")
        return out or ["no role"]

    def check_route(self, route: str, allowed: frozenset[str]) -> None:
        self.allowed = allowed
        held = [self.cluster_role, self.legacy_role, *self.tenant_roles.values()]
        if not any(r in allowed for r in held if r):
            raise HTTPException(
                403,
                f"{route} needs role {' or '.join(sorted(allowed, key=_RANK.__getitem__))}; "
                f"{self.label} has {'; '.join(self.grants())}",
            )

    @property
    def cluster_allowed(self) -> bool:
        return self.cluster_role in self.allowed

    def narrow_namespace(self, namespace: str | None, all_namespaces: bool) -> None:
        if not self.namespaces:
            return
        if all_namespaces or namespace is None:
            raise HTTPException(
                403,
                f"{self.label} is namespace-scoped and not authorized for all-namespaces operations; "
                "pass an explicit allowed namespace",
            )
        if namespace not in self.namespaces:
            raise HTTPException(403, f"{self.label} is not authorized for namespace '{namespace}'")


async def authorize_namespace(
    p: Principal, core: K7Core, namespace: str | None, *, all_namespaces: bool = False
) -> tuple[str, str | None]:
    """Return ``(role, tenant)`` that authorizes ``namespace`` for the current route."""
    if p.cluster_allowed:
        p.narrow_namespace(namespace, all_namespaces)
        return p.cluster_role or "", None
    if all_namespaces or namespace is None:
        raise HTTPException(
            403, f"{p.label} holds tenant roles only and is not authorized for all-namespaces operations"
        )
    if p.legacy_role in p.allowed:
        p.narrow_namespace(namespace, all_namespaces)
        return p.legacy_role or "", None
    tenant = await namespace_tenant(core, namespace)
    role = p.tenant_roles.get(tenant or "")
    if role not in p.allowed:
        need = " or ".join(r for r in (RO, USER) if r in p.allowed)
        raise HTTPException(
            403,
            f"namespace {namespace} belongs to {f'tenant {tenant}' if tenant else 'no tenant'}; "
            f"this route needs {need} there and {p.label} has {'; '.join(p.grants())}",
        )
    p.narrow_namespace(namespace, all_namespaces)
    return role or "", tenant


def authorize_cluster(p: Principal) -> None:
    """Cluster-wide routes: a cluster role with no namespace/node narrowing."""
    if p.cluster_role not in CLUSTER_ROLES or not p.cluster_allowed:
        raise HTTPException(403, f"{p.label} is not authorized for cluster-wide operations")
    if p.namespaces:
        raise HTTPException(403, f"{p.label} is namespace-scoped and not authorized for all-namespaces operations")
    if p.nodes:
        raise HTTPException(403, f"{p.label} is node-scoped and not authorized for cluster-wide node operations")


def apply_node_scope(p: Principal, node_name: str | None) -> str | None:
    """Enforce optional per-key node placement and return the pin.

    No ``nodes`` on the key ⇒ unrestricted. A caller-supplied ``node_name`` is
    kept for operator/test pinning. A single-node scope auto-pins when
    ``node_name`` is omitted; a multi-node scope requires an explicit allowed
    node (fail loud, never silently pick).
    """
    allowed = [n for n in p.nodes if n]
    requested = node_name.strip() if isinstance(node_name, str) and node_name.strip() else None
    if not allowed:
        return requested
    if requested is None:
        if len(allowed) == 1:
            return allowed[0]
        raise HTTPException(status_code=403, detail="API key is node-scoped; pass an explicit allowed node")
    if requested not in allowed:
        raise HTTPException(status_code=403, detail=f"API key is not authorized for node '{requested}'")
    return requested
