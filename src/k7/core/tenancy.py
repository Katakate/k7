"""Tenants: the Tenant CRD, key binding, quotas and per-VM limits.

A Tenant (cluster-scoped ``tenants.k7.katakate.org``) groups namespaces,
dedicated nodes, a default sandbox size, quotas and per-VM caps. The k7-api
resolves the tenant that owns a request's namespace and hands K7Core an
:class:`Admission`. Every sandbox Deployment then goes through
:func:`admit_deployment` (identity labels, tenant nodes, per-VM limits and,
under the per-tenant Lease, the quotas) and every user VolumeSnapshot through
:func:`admit_snapshot`. ``Tenant.status`` is display only, never enforcement.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from kubernetes_asyncio import client
from kubernetes_asyncio.client.exceptions import ApiException

from .. import metrics
from ..deploy.k7_api_keys import CLUSTER_ROLES
from . import cluster_config

log = logging.getLogger("k7.tenancy")

GROUP, VERSION, PLURAL, KIND = "k7.katakate.org", "v1alpha1", "tenants", "Tenant"
TENANT_LABEL = "k7.katakate.org/tenant"
KEY_ID_LABEL = "k7.katakate.org/api-key-id"
SANDBOX_LABEL = "katakate.org/sandbox"
BACKEND_ANN = "k7.katakate.org/backend"
DIMENSIONS = ("sandboxes", "vcpu", "memory", "disk", "snapshotStorage")
SIZE_KEYS = ("cpu", "memory", "disk")
DISK_IO_KEYS = ("readBps", "writeBps", "readIops", "writeIops")
NETWORK_KEYS = ("egressBps", "ingressBps")
KFD = "kata-firecracker-devmapper"
K7D_FAMILY = frozenset({"k7d", "k7d-fc"})
BACKENDS = (KFD, "kata-qemu-longhorn", "k7d", "k7d-fc")
# devmapper `base_image_size = "10GB"` (k7-runtimes.toml.j2): every kfd root.
KFD_ROOT_DISK = Decimal(10 * 1000**3)
# Kata 3.x hypervisor rate limiters, bits/s: Firecracker's own token buckets
# on kfd, tc on the tap for kata-qemu. tx = guest egress, rx = guest ingress.
NETWORK_ANNOTATIONS = {
    "egressBps": "io.katacontainers.config.hypervisor.tx_rate_limiter_max_rate",
    "ingressBps": "io.katacontainers.config.hypervisor.rx_rate_limiter_max_rate",
}
RESOURCE_QUOTA = "k7-tenant-quota"
# kubernetes_asyncio sends a dict patch as JSON Patch unless told otherwise.
MERGE_PATCH = "application/merge-patch+json"
STATUS_LEASE = "k7-tenant-status"
LEASE_TTL = 10
LEASE_TIMEOUT = 5.0
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

_UNENFORCEABLE: dict[str, tuple[frozenset[str], str]] = {
    "diskIO": (
        frozenset(BACKENDS),
        "no backend enforces per-VM disk I/O caps yet (Kata 3.24's Go runtime has no block "
        "rate-limiter annotation; k7d needs its disk-limit release)",
    ),
    "network": (K7D_FAMILY, "k7d has no tap rate limiter yet (needs the k7d network-limit release)"),
    "disk": (
        K7D_FAMILY,
        "K7 cannot read a k7d VM's writable-disk size, so a disk quota or maxSandboxSize.disk cannot be accounted",
    ),
}


class AdmissionError(Exception):
    """A tenancy refusal with its HTTP status, error code and structured details."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


_UNITS = {
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal(10**3),
    "M": Decimal(10**6),
    "G": Decimal(10**9),
    "T": Decimal(10**12),
    "P": Decimal(10**15),
    "E": Decimal(10**18),
}
_QTY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")


def quantity(value: Any, what: str = "quantity") -> Decimal:
    """Exact Kubernetes quantity (``500m``, ``2Gi``, ``1.5``, ``500M``); anything else raises."""
    if isinstance(value, bool):
        raise ValueError(f"invalid {what} {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    m = _QTY.match(str(value).strip())
    if not m:
        raise ValueError(f"invalid {what} {value!r}: expected a Kubernetes quantity such as 500m, 2, 4Gi or 500M")
    return Decimal(m.group(1)) * _UNITS[m.group(2) or ""]


def fmt(dim: str, value: Decimal) -> str:
    if dim == "sandboxes":
        return str(int(value))
    if dim in ("vcpu", "cpu"):
        return str(int(value)) if value == value.to_integral_value() else f"{int(value * 1000)}m"
    for unit in ("Ti", "Gi", "Mi", "T", "G", "M", "Ki", "k"):
        if value and value % _UNITS[unit] == 0:
            return f"{int(value / _UNITS[unit])}{unit}"
    return str(int(value))


def name_of(tenant: dict) -> str:
    return tenant["metadata"]["name"]


def spec_of(tenant: dict | None) -> dict:
    return (tenant or {}).get("spec") or {}


def validate_spec(name: str, spec: dict) -> None:
    """Raise ValueError on anything the CRD schema would reject, with the field named."""
    if not _DNS_LABEL.match(name or ""):
        raise ValueError(f"tenant name {name!r} must be a DNS-1123 label (lowercase alphanumerics and '-', ≤63)")
    unknown = set(spec) - {"namespaces", "nodes", "defaultSize", "quotas", "limits"}
    if unknown:
        raise ValueError(f"unknown Tenant spec field(s): {', '.join(sorted(unknown))}")
    namespaces = spec.get("namespaces")
    if not isinstance(namespaces, list) or not namespaces:
        raise ValueError("spec.namespaces needs at least one namespace")
    for field, values in (("namespaces", namespaces), ("nodes", spec.get("nodes") or [])):
        if len(set(values)) != len(values) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f"spec.{field} must be unique non-empty strings")
    for ns in namespaces:
        if not _DNS_LABEL.match(ns):
            raise ValueError(f"spec.namespaces: {ns!r} is not a valid namespace name")

    def check(path: str, section: Any, keys: tuple[str, ...]) -> None:
        if not isinstance(section, dict):
            raise ValueError(f"{path} must be a mapping")
        bad = set(section) - set(keys)
        if bad:
            raise ValueError(f"{path}: unknown key(s) {', '.join(sorted(bad))}; allowed: {', '.join(keys)}")
        for k, v in section.items():
            if k == "sandboxes" and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise ValueError(f"{path}.sandboxes must be a non-negative integer, got {v!r}")
            quantity(v, f"{path}.{k}")

    check("spec.defaultSize", spec.get("defaultSize") or {}, SIZE_KEYS)
    check("spec.quotas", spec.get("quotas") or {}, DIMENSIONS)
    limits = spec.get("limits") or {}
    check("spec.limits", {k: 0 for k in limits}, ("maxSandboxSize", "diskIO", "network"))
    check("spec.limits.maxSandboxSize", limits.get("maxSandboxSize") or {}, SIZE_KEYS)
    check("spec.limits.diskIO", limits.get("diskIO") or {}, DISK_IO_KEYS)
    check("spec.limits.network", limits.get("network") or {}, NETWORK_KEYS)
    default, mx = spec.get("defaultSize") or {}, limits.get("maxSandboxSize") or {}
    for k in set(default) & set(mx):
        if quantity(default[k]) > quantity(mx[k]):
            raise ValueError(f"spec.defaultSize.{k} {default[k]} is above spec.limits.maxSandboxSize.{k} {mx[k]}")


@dataclass
class Admission:
    """Who is creating (API key) and under which tenant/mode; set on K7Core by k7-api only."""

    key_id: str | None
    admin: bool
    tenant: dict | None
    mode: str

    def labels(self) -> dict[str, str]:
        out = {KEY_ID_LABEL: self.key_id} if self.key_id else {}
        if self.tenant:
            out[TENANT_LABEL] = name_of(self.tenant)
        return out


# ── Tenant lookup and key scope ─────────────────────────────────────


async def list_tenants(core: Any) -> list[dict]:
    custom = await core._get_custom_objects_client()
    try:
        return (await custom.list_cluster_custom_object(GROUP, VERSION, PLURAL))["items"]
    except ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                f"the Tenant CRD {PLURAL}.{GROUP} is not installed; re-run `k7 install` (or upgrade the chart)"
            ) from e
        raise


async def get_tenant(core: Any, name: str) -> dict | None:
    custom = await core._get_custom_objects_client()
    try:
        return await custom.get_cluster_custom_object(GROUP, VERSION, PLURAL, name)
    except ApiException as e:
        if e.status == 404:
            await list_tenants(core)  # a missing CRD is a loud error, not "no such tenant"
            return None
        raise


def tenant_conflict(namespace: str, names: list[str]) -> AdmissionError:
    return AdmissionError(
        500,
        "TenantConflict",
        f"namespace {namespace} is claimed by tenants {' and '.join(names)}; K7 refuses every request "
        f"there until one claim is removed (`k7 tenant update {names[0]} --namespace ...`)",
        {"namespace": namespace, "tenants": names},
    )


def tenant_of_namespace(namespace: str, tenants: list[dict]) -> dict | None:
    owners = [t for t in tenants if namespace in spec_of(t).get("namespaces", [])]
    if len(owners) > 1:
        raise tenant_conflict(namespace, sorted(name_of(t) for t in owners))
    return owners[0] if owners else None


def key_namespaces(key: dict, tenants: list[dict]) -> list[str] | None:
    """Namespaces a key may touch (None = unrestricted): tenant namespaces ∩ the key's own list."""
    bound = key.get("tenant")
    own = key.get("namespaces") or []
    if not bound:
        return own or None
    tenant = next((t for t in tenants if name_of(t) == bound), None)
    if tenant is None:
        raise AdmissionError(
            403, "Forbidden", f"API key is bound to tenant {bound}, which does not exist", {"tenant": bound}
        )
    allowed = spec_of(tenant)["namespaces"]
    return [ns for ns in allowed if ns in own] if own else list(allowed)


# ── Per-sandbox policy ──────────────────────────────────────────────


def unenforceable(backend: str, limit: str) -> str | None:
    backends, why = _UNENFORCEABLE[limit]
    if backend not in backends:
        return None
    return f"backend {backend} cannot enforce {limit}: {why}. K7 never runs a sandbox without a configured limit"


def _refuse(backend: str, limit: str) -> None:
    msg = unenforceable(backend, limit)
    if msg:
        raise AdmissionError(400, "LimitNotEnforceable", msg, {"backend": backend, "limit": limit})


def apply_policy(config: Any, backend: str, tenant: dict | None, mode: str) -> None:
    """Create-time request policy: fill ``defaultSize`` and keep request caps under the tenant's."""
    spec = spec_of(tenant)
    who = f"tenant {name_of(tenant)}" if tenant else "this namespace (no tenant)"
    limits = dict(config.limits or {})
    default = spec.get("defaultSize") or {}
    for k in ("cpu", "memory"):
        if k not in limits and k in default:
            limits[k] = str(default[k])
    if config.root_disk_size is None and "disk" in default:
        config.root_disk_size = str(default["disk"])
    config.limits = limits
    if mode == "production" and not {"cpu", "memory"} <= limits.keys():
        raise AdmissionError(
            400,
            "BadRequest",
            f"production mode requires cpu and memory on every sandbox; pass --cpu/--memory or set "
            f"defaultSize on {who}",
        )
    caps = (spec.get("limits") or {}).get("network") or {}
    for k, v in (config.network_limits or {}).items():
        if k not in NETWORK_KEYS:
            raise AdmissionError(
                400, "BadRequest", f"network_limits: unknown key {k!r}; allowed: egressBps, ingressBps"
            )
        if k in caps and quantity(v, k) > quantity(caps[k]):
            raise AdmissionError(
                400, "BadRequest", f"{k} {v} is above {who}'s cap {caps[k]}; a request may lower a cap, not raise it"
            )


def kata_rate(backend: str, bps: int) -> int:
    """Annotation value that caps the VM at ``bps`` bits/s.

    kata-qemu hands bits/s to tc unchanged. Kata 3.24's Firecracker driver
    (fc.go) sizes the bucket as ``RevertBytes(value / 8)``, which reads the
    byte count's base-1000 digits as base-1024 (6,250,000 B/s becomes
    6,547,456): write the target's base-1024 digits in base 1000, flooring.
    """
    if backend != KFD:
        return bps
    digits, rest = [], bps // 8
    while rest:
        rest, d = divmod(rest, 1024)
        digits.append(d)
    out, clamp = 0, False
    for d in reversed(digits):
        clamp = clamp or d > 999
        out = out * 1000 + (999 if clamp else d)
    return out * 8


def network_annotations(limits: dict, backend: str) -> dict[str, str]:
    bad = set(limits) - set(NETWORK_KEYS)
    if bad:
        raise ValueError(f"unknown key(s) {', '.join(sorted(bad))}; allowed: {', '.join(NETWORK_KEYS)}")
    return {NETWORK_ANNOTATIONS[k]: str(kata_rate(backend, int(quantity(v, k)))) for k, v in limits.items()}


def _backend(dep: Any) -> str:
    return (dep.metadata.annotations or {}).get(BACKEND_ANN) or ""


def _limits(container: Any) -> dict:
    return (container.resources.limits if container.resources else None) or {}


def _claims(dep: Any) -> set[str]:
    vols = dep.spec.template.spec.volumes or []
    return {v.persistent_volume_claim.claim_name for v in vols if v.persistent_volume_claim}


def deployment_usage(dep: Any, pvc_sizes: dict[str, Decimal]) -> dict[str, Decimal]:
    """What one sandbox reserves: container limits, its PVCs (sizes given), ephemeral claims, kfd root."""
    pod = dep.spec.template.spec
    u = {"sandboxes": Decimal(1), "vcpu": Decimal(0), "memory": Decimal(0), "disk": Decimal(0)}
    for c in pod.containers:
        lim = _limits(c)
        for dim, key in (("vcpu", "cpu"), ("memory", "memory"), ("disk", "ephemeral-storage")):
            if key in lim:
                u[dim] += quantity(lim[key])
    for v in pod.volumes or []:
        if v.persistent_volume_claim:
            u["disk"] += pvc_sizes.get(v.persistent_volume_claim.claim_name, Decimal(0))
        elif v.ephemeral:
            u["disk"] += quantity(v.ephemeral.volume_claim_template.spec.resources.requests["storage"])
    if _backend(dep) == KFD:
        u["disk"] += KFD_ROOT_DISK
    return u


def stamp(dep: Any, adm: Admission) -> None:
    """Identity labels, tenant node placement and the foreign-dedicated-node check."""
    labels = adm.labels()
    for meta in (dep.metadata, dep.spec.template.metadata):
        meta.labels = {k: v for k, v in (meta.labels or {}).items() if k not in (TENANT_LABEL, KEY_ID_LABEL)}
        meta.labels.update(labels)
    pod = dep.spec.template.spec
    mine = name_of(adm.tenant) if adm.tenant else None
    foreign = {t.value for t in pod.tolerations or [] if t.key == TENANT_LABEL} - {mine}
    if foreign and not adm.admin:
        raise AdmissionError(
            403, "Forbidden", f"node is dedicated to tenant {', '.join(sorted(foreign))}; pin a node of your own"
        )
    nodes = spec_of(adm.tenant).get("nodes") or []
    if not nodes:
        return
    pinned = (pod.node_selector or {}).get("kubernetes.io/hostname") or pod.node_name
    if pinned and pinned not in nodes:
        raise AdmissionError(
            400, "BadRequest", f"node {pinned} is not one of tenant {mine}'s nodes ({', '.join(nodes)})"
        )
    pod.node_selector = {**(pod.node_selector or {}), TENANT_LABEL: mine}
    if not any(t.key == TENANT_LABEL and t.value == mine for t in pod.tolerations or []):
        tol = client.V1Toleration(key=TENANT_LABEL, operator="Equal", value=mine, effect="NoSchedule")
        pod.tolerations = [*(pod.tolerations or []), tol]


def enforce_vm_limits(dep: Any, tenant: dict) -> None:
    """Tenant per-VM caps: refuse what the backend cannot enforce, else cap the Kata annotations."""
    lim = spec_of(tenant).get("limits") or {}
    backend = _backend(dep)
    if lim.get("diskIO"):
        _refuse(backend, "diskIO")
    net = lim.get("network") or {}
    if not net:
        return
    _refuse(backend, "network")
    meta = dep.spec.template.metadata
    anns = meta.annotations = meta.annotations if meta.annotations is not None else {}
    for key, cap_value in net.items():
        cap = kata_rate(backend, int(quantity(cap_value)))
        ann = NETWORK_ANNOTATIONS[key]
        if ann not in anns or int(anns[ann]) > cap:
            anns[ann] = str(cap)


def check_sandbox(dep: Any, tenant: dict | None, mode: str, requested: dict[str, Decimal]) -> None:
    sandbox = next(c for c in dep.spec.template.spec.containers if c.name == "sandbox")
    lim = _limits(sandbox)
    if mode == "production" and not {"cpu", "memory"} <= lim.keys():
        raise AdmissionError(400, "BadRequest", "production mode requires cpu and memory limits on every sandbox")
    spec = spec_of(tenant)
    quotas = spec.get("quotas") or {}
    for dim, key in (("vcpu", "cpu"), ("memory", "memory")):
        if dim in quotas and key not in lim:
            raise AdmissionError(
                400,
                "BadRequest",
                f"tenant {name_of(tenant or {})} has a {dim} quota, so every sandbox needs a {key} limit "
                f"(pass --{key}, or set the tenant's defaultSize.{key})",
            )
    mx = (spec.get("limits") or {}).get("maxSandboxSize") or {}
    for key, dim in (("cpu", "vcpu"), ("memory", "memory"), ("disk", "disk")):
        if key in mx and requested[dim] > quantity(mx[key]):
            raise AdmissionError(
                400,
                "BadRequest",
                f"sandbox {key} {fmt(dim, requested[dim])} is above tenant {name_of(tenant or {})}'s "
                f"maxSandboxSize.{key} {mx[key]}",
                {"limit": f"maxSandboxSize.{key}", "max": str(mx[key]), "requested": fmt(dim, requested[dim])},
            )
    if "disk" in quotas or "disk" in mx:
        _refuse(_backend(dep), "disk")


def check_quota(tenant: dict, usage: dict[str, Decimal], requested: dict[str, Decimal]) -> None:
    quotas = spec_of(tenant).get("quotas") or {}
    for dim in DIMENSIONS:
        if dim in quotas and requested.get(dim):
            limit = quantity(quotas[dim])
            if usage[dim] + requested[dim] > limit:
                details = {
                    "tenant": name_of(tenant),
                    "dimension": dim,
                    "limit": fmt(dim, limit),
                    "usage": fmt(dim, usage[dim]),
                    "requested": fmt(dim, requested[dim]),
                }
                metrics.QUOTA_REJECTIONS.labels(details["tenant"], dim).inc()
                raise AdmissionError(
                    403,
                    "QuotaExceeded",
                    f"tenant {details['tenant']} {dim} quota exceeded: limit {details['limit']}, "
                    f"usage {details['usage']}, requested {details['requested']}",
                    details,
                )


# ── Usage from live objects ─────────────────────────────────────────


async def _pvc_sizes(core: Any, namespace: str) -> dict[str, Decimal]:
    v1 = await core._get_core_v1_client()
    pvcs = await v1.list_namespaced_persistent_volume_claim(namespace)
    return {p.metadata.name: quantity(p.spec.resources.requests["storage"]) for p in pvcs.items}


def _add(total: dict[str, Decimal], more: dict[str, Decimal]) -> None:
    for k, v in more.items():
        total[k] = total.get(k, Decimal(0)) + v


async def tenant_usage(core: Any, tenant: dict) -> dict[str, Decimal]:
    """sandboxes/vcpu/memory/disk of every sandbox Deployment in the tenant's namespaces.

    Disk counts the PVCs a sandbox references, not every PVC in the namespace:
    a racing create's PVC exists before its Deployment is admitted, and must
    not count until then (it is that create's own ``requested``).
    """
    apps = await core._get_apps_v1_client()

    async def one(ns: str) -> dict[str, Decimal]:
        deps, sizes = await asyncio.gather(
            apps.list_namespaced_deployment(ns, label_selector=SANDBOX_LABEL), _pvc_sizes(core, ns)
        )
        u = {d: Decimal(0) for d in DIMENSIONS[:4]}
        for d in deps.items:
            _add(u, deployment_usage(d, sizes))
        return u

    total = {d: Decimal(0) for d in DIMENSIONS[:4]}
    for u in await asyncio.gather(*(one(ns) for ns in spec_of(tenant)["namespaces"])):
        _add(total, u)
    return total


async def snapshot_usage(core: Any, tenant: dict) -> Decimal:
    """VolumeSnapshot restoreSize, or the source PVC size until the snapshot is cut."""
    custom = await core._get_custom_objects_client()
    total = Decimal(0)
    for ns in spec_of(tenant)["namespaces"]:
        try:
            snaps = (
                await custom.list_namespaced_custom_object("snapshot.storage.k8s.io", "v1", ns, "volumesnapshots")
            )["items"]
        except ApiException as e:
            if e.status == 404:  # no snapshot CRDs on this cluster: no snapshots
                return total
            raise
        sizes: dict[str, Decimal] | None = None
        for s in snaps:
            size = (s.get("status") or {}).get("restoreSize")
            if size is not None:
                total += quantity(size)
                continue
            src = ((s.get("spec") or {}).get("source") or {}).get("persistentVolumeClaimName")
            sizes = sizes if sizes is not None else await _pvc_sizes(core, ns)
            if src not in sizes:
                raise AdmissionError(
                    500,
                    "QuotaAccounting",
                    f"cannot size VolumeSnapshot {ns}/{s['metadata']['name']}: no restoreSize and source PVC "
                    f"{src} is gone; delete the snapshot",
                )
            total += sizes[src]
    return total


# ── Quota lease ─────────────────────────────────────────────────────


def _micro(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _expired(spec: Any) -> bool:
    renewed = spec.renew_time or spec.acquire_time
    if renewed is None or not spec.lease_duration_seconds:
        return True
    if renewed.tzinfo is None:
        renewed = renewed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > renewed + timedelta(seconds=spec.lease_duration_seconds)


def quota_lease_name(tenant: str, suffix: str = "") -> str:
    return f"k7-quota-{tenant}{suffix}"


def _lease_body(name: str, holder: str, ttl: int, rv: str | None = None) -> dict:
    now = _micro(datetime.now(timezone.utc))
    meta: dict = {"name": name, "labels": {"app.kubernetes.io/managed-by": "k7"}}
    if rv:
        meta["resourceVersion"] = rv
    spec = {"holderIdentity": holder, "leaseDurationSeconds": ttl, "acquireTime": now, "renewTime": now}
    return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": meta, "spec": spec}


async def _take(api: Any, namespace: str, name: str, holder: str, ttl: int) -> str | None:
    """Create the Lease, or take it over when expired or already ours; resourceVersion or None."""
    try:
        return (await api.create_namespaced_lease(namespace, _lease_body(name, holder, ttl))).metadata.resource_version
    except ApiException as e:
        if e.status != 409:
            raise
    try:
        cur = await api.read_namespaced_lease(name, namespace)
        if cur.spec.holder_identity != holder and not _expired(cur.spec):
            return None
        body = _lease_body(name, holder, ttl, cur.metadata.resource_version)
        return (await api.replace_namespaced_lease(name, namespace, body)).metadata.resource_version
    except ApiException as e:
        if e.status in (404, 409):
            return None
        raise


async def try_lead(core: Any, name: str, holder: str, ttl: int) -> bool:
    """One leadership attempt on Lease ``name`` (kept, not released: renewed by the next attempt)."""
    api = client.CoordinationV1Api(await core._get_api_client())
    return await _take(api, cluster_config.system_namespace(), name, holder, ttl) is not None


class QuotaLease:
    """Exclusive coordination.k8s.io Lease, held while usage is computed and the object created.

    Never proceeds unchecked: no lease within ``timeout`` is 503
    ``QuotaLockTimeout``; a lease lost mid-hold is 503 ``QuotaLockLost``.
    """

    def __init__(self, core: Any, name: str, *, ttl: int = LEASE_TTL, timeout: float = LEASE_TIMEOUT):
        self.core, self.name, self.ttl, self.timeout = core, name, ttl, timeout
        self.namespace = cluster_config.system_namespace()
        self.holder = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.rv: str | None = None
        self.lost: str | None = None
        self.renewed = 0.0
        self.acquire_ms = 0.0
        self._renew: asyncio.Task | None = None

    async def _try(self, api: Any) -> bool:
        self.rv = await _take(api, self.namespace, self.name, self.holder, self.ttl)
        return self.rv is not None

    async def __aenter__(self) -> QuotaLease:
        api = client.CoordinationV1Api(await self.core._get_api_client())
        start = time.monotonic()
        while not await self._try(api):
            if time.monotonic() - start >= self.timeout:
                raise AdmissionError(
                    503,
                    "QuotaLockTimeout",
                    f"could not acquire quota lease {self.namespace}/{self.name} within {self.timeout:g}s "
                    "(another create for this tenant holds it); nothing was created, retry",
                    {"lease": self.name},
                )
            await asyncio.sleep(0.05 + random.random() * 0.05)
        self.renewed = time.monotonic()
        self.acquire_ms = (self.renewed - start) * 1000
        self._renew = asyncio.create_task(self._renew_loop(api))
        return self

    async def _renew_loop(self, api: Any) -> None:
        while True:
            await asyncio.sleep(self.ttl / 3)
            try:
                body = _lease_body(self.name, self.holder, self.ttl, self.rv)
                lease = await api.replace_namespaced_lease(self.name, self.namespace, body)
            except Exception as e:
                self.lost = str(e)
                return
            self.rv, self.renewed = lease.metadata.resource_version, time.monotonic()

    def assert_held(self) -> None:
        if self.lost or time.monotonic() - self.renewed >= self.ttl:
            raise AdmissionError(
                503,
                "QuotaLockLost",
                f"quota lease {self.namespace}/{self.name} was lost before the create ({self.lost or 'not renewed'}); "
                "nothing was created, retry",
            )

    async def __aexit__(self, *_exc: object) -> None:
        held_ms = (time.monotonic() - self.renewed) * 1000
        if self._renew:
            self._renew.cancel()
            await asyncio.gather(self._renew, return_exceptions=True)
        api = client.CoordinationV1Api(await self.core._get_api_client())
        # Release only our own lease: a renew may have landed as it was
        # cancelled (409 on the precondition), so re-read before giving up.
        for _ in range(2):
            opts = client.V1DeleteOptions(preconditions=client.V1Preconditions(resource_version=self.rv))
            try:
                await api.delete_namespaced_lease(self.name, self.namespace, body=opts)
                break
            except ApiException as e:
                if e.status == 404:
                    break
                if e.status != 409:
                    log.warning("⚠️  quota lease %s release failed (expires in %ss): %s", self.name, self.ttl, e)
                    break
                cur = await api.read_namespaced_lease(self.name, self.namespace)
                if cur.spec.holder_identity != self.holder:
                    break
                self.rv = cur.metadata.resource_version
        log.info("⏱️  Quota lease %s: acquired in %.1f ms, held %.1f ms", self.name, self.acquire_ms, held_ms)


# ── Admission choke points (called by K7Core) ───────────────────────


async def admit_deployment(
    core: Any, adm: Admission, namespace: str, dep: Any, create: Callable[[], Awaitable[Any]]
) -> Any:
    stamp(dep, adm)
    tenant = adm.tenant
    if tenant:
        enforce_vm_limits(dep, tenant)
    claims = _claims(dep)
    requested = deployment_usage(dep, await _pvc_sizes(core, namespace) if claims else {})
    check_sandbox(dep, tenant, adm.mode, requested)
    if not tenant or not spec_of(tenant).get("quotas"):
        return await create()
    async with QuotaLease(core, quota_lease_name(name_of(tenant))) as lease:
        check_quota(tenant, await tenant_usage(core, tenant), requested)
        lease.assert_held()
        return await create()


async def admit_snapshot(
    core: Any, adm: Admission, namespace: str, pvc: str, create: Callable[[], Awaitable[Any]]
) -> Any:
    tenant = adm.tenant
    if "snapshotStorage" not in (spec_of(tenant).get("quotas") or {}):
        return await create()
    assert tenant is not None
    sizes = await _pvc_sizes(core, namespace)
    if pvc not in sizes:
        raise AdmissionError(404, "NotFound", f"PVC {namespace}/{pvc} not found; nothing to snapshot")
    async with QuotaLease(core, quota_lease_name(name_of(tenant), "-snapshots")) as lease:
        usage = {"snapshotStorage": await snapshot_usage(core, tenant)}
        check_quota(tenant, usage, {"snapshotStorage": sizes[pvc]})
        lease.assert_held()
        return await create()


# ── Tenant lifecycle (CLI and admin API) ────────────────────────────


def _claim_conflicts(name: str, namespaces: list[str], tenants: list[dict]) -> None:
    for t in tenants:
        if name_of(t) == name:
            continue
        taken = sorted(set(namespaces) & set(spec_of(t).get("namespaces", [])))
        if taken:
            raise ValueError(f"namespace(s) {', '.join(taken)} already belong to tenant {name_of(t)}")


async def _label_namespace(core: Any, ns: str, tenant: str | None, existing: set[str]) -> bool:
    """Set (or with ``tenant=None`` drop) the tenant label; create a missing namespace. True if created."""
    v1 = await core._get_core_v1_client()
    try:
        obj = await v1.read_namespace(ns)
    except ApiException as e:
        if e.status != 404:
            raise
        if tenant is None:
            return False
        await v1.create_namespace({"metadata": {"name": ns, "labels": {TENANT_LABEL: tenant}}})
        return True
    owner = (obj.metadata.labels or {}).get(TENANT_LABEL)
    if tenant and owner and owner != tenant and owner in existing:
        raise ValueError(f"namespace {ns} is labelled for tenant {owner}")
    await v1.patch_namespace(ns, {"metadata": {"labels": {TENANT_LABEL: tenant}}})
    return False


async def _dedicate(core: Any, node: str, tenant: str | None) -> None:
    r = await (core.dedicate_node(node, tenant) if tenant else core.undedicate_node(node))
    if not r.success:
        raise RuntimeError(r.error)


async def create_tenant(core: Any, name: str, spec: dict) -> dict:
    validate_spec(name, spec)
    tenants = await list_tenants(core)
    if any(name_of(t) == name for t in tenants):
        raise ValueError(f"tenant {name} already exists")
    _claim_conflicts(name, spec["namespaces"], tenants)
    existing = {name_of(t) for t in tenants}
    custom = await core._get_custom_objects_client()
    body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": KIND, "metadata": {"name": name}, "spec": spec}
    created_cr = await custom.create_cluster_custom_object(GROUP, VERSION, PLURAL, body)
    labelled: list[tuple[str, bool]] = []
    dedicated: list[str] = []
    try:
        for ns in spec["namespaces"]:
            labelled.append((ns, await _label_namespace(core, ns, name, existing)))
        for node in spec.get("nodes") or []:
            await _dedicate(core, node, name)
            dedicated.append(node)
    except BaseException:
        v1 = await core._get_core_v1_client()
        for node in dedicated:
            await _dedicate(core, node, None)
        for ns, created in labelled:
            await (v1.delete_namespace(ns) if created else _label_namespace(core, ns, None, existing))
        await custom.delete_cluster_custom_object(GROUP, VERSION, PLURAL, name)
        raise
    return created_cr


async def update_tenant(core: Any, name: str, changes: dict) -> dict:
    """Replace the given top-level spec sections (``None`` removes one); re-label namespaces and nodes."""
    tenant = await get_tenant(core, name)
    if tenant is None:
        raise LookupError(f"tenant {name} not found")
    old = spec_of(tenant)
    new = {k: v for k, v in {**old, **changes}.items() if v is not None}
    validate_spec(name, new)
    tenants = await list_tenants(core)
    _claim_conflicts(name, new["namespaces"], tenants)
    existing = {name_of(t) for t in tenants}
    tenant["spec"] = new
    custom = await core._get_custom_objects_client()
    updated = await custom.replace_cluster_custom_object(GROUP, VERSION, PLURAL, name, tenant)
    for ns in new["namespaces"]:
        if ns not in old.get("namespaces", []):
            await _label_namespace(core, ns, name, existing)
    v1 = await core._get_core_v1_client()
    for ns in old.get("namespaces", []):
        if ns not in new["namespaces"]:
            await _label_namespace(core, ns, None, existing)
            await _delete_ignoring_404(v1.delete_namespaced_resource_quota(RESOURCE_QUOTA, ns))
    old_nodes, new_nodes = old.get("nodes") or [], new.get("nodes") or []
    for node in new_nodes:
        if node not in old_nodes:
            await _dedicate(core, node, name)
    for node in old_nodes:
        if node not in new_nodes:
            await _dedicate(core, node, None)
    return updated


async def tenant_objects(core: Any, tenant: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(sandboxes, snapshots) as (namespace, name) in the tenant's namespaces."""
    apps = await core._get_apps_v1_client()
    sandboxes, snapshots = [], []
    for ns in spec_of(tenant)["namespaces"]:
        deps = await apps.list_namespaced_deployment(ns, label_selector=SANDBOX_LABEL)
        sandboxes += [(ns, d.metadata.name) for d in deps.items]
        snapshots += [(ns, s.name) for s in await core.list_snapshots(namespace=ns)]
    return sandboxes, snapshots


async def delete_tenant(core: Any, name: str, cascade: bool) -> list[str]:
    """Delete a tenant; with ``cascade`` its snapshots and sandboxes first. Returns each object deleted."""
    tenant = await get_tenant(core, name)
    if tenant is None:
        raise LookupError(f"tenant {name} not found")
    sandboxes, snapshots = await tenant_objects(core, tenant)
    if (sandboxes or snapshots) and not cascade:
        listed = ", ".join([f"sandbox {ns}/{n}" for ns, n in sandboxes] + [f"snapshot {ns}/{n}" for ns, n in snapshots])
        raise ValueError(f"tenant {name} still has {listed}; delete them or pass --cascade")
    deleted: list[str] = []
    for kind, items, op in (("snapshot", snapshots, core.delete_snapshot), ("sandbox", sandboxes, core.delete_sandbox)):
        for ns, n in items:
            r = await op(n, namespace=ns)
            if not r.success:
                raise RuntimeError(
                    f"cascade stopped: deleting {kind} {ns}/{n} failed: {r.error} (deleted so far: {deleted})"
                )
            deleted.append(f"{kind} {ns}/{n}")
    spec = spec_of(tenant)
    existing = {name_of(t) for t in await list_tenants(core)}
    for ns in spec["namespaces"]:
        await _label_namespace(core, ns, None, existing)
        deleted.append(f"label {TENANT_LABEL} on namespace {ns}")
    for node in spec.get("nodes") or []:
        await _dedicate(core, node, None)
        deleted.append(f"dedication of node {node}")
    v1 = await core._get_core_v1_client()
    coord = client.CoordinationV1Api(await core._get_api_client())
    sysns = cluster_config.system_namespace()
    for ns in spec["namespaces"]:
        await _delete_ignoring_404(v1.delete_namespaced_resource_quota(RESOURCE_QUOTA, ns))
    for lease in (quota_lease_name(name), quota_lease_name(name, "-snapshots")):
        await _delete_ignoring_404(coord.delete_namespaced_lease(lease, sysns))
    custom = await core._get_custom_objects_client()
    await custom.delete_cluster_custom_object(GROUP, VERSION, PLURAL, name)
    deleted.append(f"tenant {name}")
    return deleted


async def _delete_ignoring_404(call: Awaitable[Any]) -> None:
    try:
        await call
    except ApiException as e:
        if e.status != 404:
            raise


async def dedicate(core: Any, node: str, tenant_name: str) -> None:
    """`k7 nodes dedicate N --tenant T`: label + taint N and record it in Tenant/T.spec.nodes."""
    tenants = await list_tenants(core)
    target = next((t for t in tenants if name_of(t) == tenant_name), None)
    if target is None:
        raise LookupError(f"tenant {tenant_name} does not exist; create it with `k7 tenant create {tenant_name}`")
    custom = await core._get_custom_objects_client()
    await _dedicate(core, node, tenant_name)
    for t in tenants:
        nodes = spec_of(t).get("nodes") or []
        want = [*nodes, node] if t is target and node not in nodes else [n for n in nodes if t is target or n != node]
        if want != nodes:
            await custom.patch_cluster_custom_object(
                GROUP, VERSION, PLURAL, name_of(t), {"spec": {"nodes": want}}, _content_type=MERGE_PATCH
            )


async def undedicate(core: Any, node: str) -> None:
    await _dedicate(core, node, None)
    custom = await core._get_custom_objects_client()
    for t in await list_tenants(core):
        nodes = spec_of(t).get("nodes") or []
        if node in nodes:
            body = {"spec": {"nodes": [n for n in nodes if n != node]}}
            await custom.patch_cluster_custom_object(
                GROUP, VERSION, PLURAL, name_of(t), body, _content_type=MERGE_PATCH
            )


def adopt_plan(keys: dict[str, dict], tenants: list[dict]) -> tuple[list[dict], list[str]]:
    """One proposed tenant per connected group of key namespace sets; plus the unscoped key ids.

    Keys keep their ``namespaces`` (now a narrowing), so no key's reach changes.
    """
    groups: list[tuple[set[str], list[str]]] = []
    unscoped: list[str] = []
    for key_id, entry in sorted(keys.items()):
        if entry.get("tenant") or entry.get("role") in CLUSTER_ROLES and not entry.get("unscoped_legacy"):
            continue
        nss = set(entry.get("namespaces") or [])
        if not nss:
            unscoped.append(key_id)
            continue
        merged = [g for g in groups if g[0] & nss]
        groups = [g for g in groups if not g[0] & nss]
        groups.append((nss.union(*(g[0] for g in merged)), [key_id, *(k for g in merged for k in g[1])]))
    taken = {name_of(t) for t in tenants}
    plan = []
    for nss, ids in sorted(groups, key=lambda g: sorted(g[0])):
        owned = sorted({name_of(t) for ns in nss for t in tenants if ns in spec_of(t).get("namespaces", [])})
        if owned:
            raise ValueError(f"namespaces {', '.join(sorted(nss))} overlap existing tenant(s) {', '.join(owned)}")
        base = sorted(nss)[0]
        name, i = base, 2
        while name in taken:
            name, i = f"{base}-{i}", i + 1
        taken.add(name)
        plan.append({"tenant": name, "namespaces": sorted(nss), "keys": sorted(ids)})
    return plan, unscoped


# ── Status and ResourceQuota backstop (leader replica) ──────────────


def resource_quota_hard(tenant: dict) -> dict[str, str]:
    """Kubernetes-side backstop for single-namespace tenants (count and PVC storage only)."""
    quotas = spec_of(tenant).get("quotas") or {}
    hard = {}
    if "sandboxes" in quotas:
        hard["count/deployments.apps"] = str(quotas["sandboxes"])
    if "disk" in quotas:
        hard["requests.storage"] = str(quotas["disk"])
    return hard


async def reconcile_tenant(core: Any, tenant: dict) -> None:
    spec = spec_of(tenant)
    usage = await tenant_usage(core, tenant)
    usage["snapshotStorage"] = await snapshot_usage(core, tenant)
    custom = await core._get_custom_objects_client()
    status = {
        "usage": {d: fmt(d, usage[d]) for d in DIMENSIONS},
        "observedGeneration": tenant["metadata"].get("generation"),
    }
    await custom.patch_cluster_custom_object_status(
        GROUP, VERSION, PLURAL, name_of(tenant), {"status": status}, _content_type=MERGE_PATCH
    )
    v1 = await core._get_core_v1_client()
    hard = resource_quota_hard(tenant)
    namespaces = spec["namespaces"]
    for ns in namespaces:
        if len(namespaces) == 1 and hard:
            body = {
                "metadata": {"name": RESOURCE_QUOTA, "labels": {TENANT_LABEL: name_of(tenant)}},
                "spec": {"hard": hard},
            }
            try:
                await v1.replace_namespaced_resource_quota(RESOURCE_QUOTA, ns, body)
            except ApiException as e:
                if e.status != 404:
                    raise
                await v1.create_namespaced_resource_quota(ns, body)
        else:
            await _delete_ignoring_404(v1.delete_namespaced_resource_quota(RESOURCE_QUOTA, ns))
