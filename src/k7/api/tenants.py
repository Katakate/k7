"""Role lookups over the ``Tenant`` CRs, via ``core.tenancy``.

Both read live CRs, so a namespace added to a tenant applies on the next
request. A namespace claimed twice is a 500 ``TenantConflict`` naming both
tenants; a missing CRD is a loud error.
"""

from __future__ import annotations

from ..core import tenancy
from ..core.core import K7Core


async def list_tenants(core: K7Core) -> dict[str, list[str]]:
    """``{tenant: spec.namespaces}``."""
    return {
        tenancy.name_of(t): list(tenancy.spec_of(t).get("namespaces") or []) for t in await tenancy.list_tenants(core)
    }


async def namespace_tenant(core: K7Core, namespace: str) -> str | None:
    claims = sorted(t for t, nss in (await list_tenants(core)).items() if namespace in nss)
    if len(claims) > 1:
        raise tenancy.tenant_conflict(namespace, claims)
    return claims[0] if claims else None
