"""``metering.requireDelivery`` (for the hosted plane).

k7-api refuses a create on a node whose usage spool is above its high-water
mark (503 naming the node). An unpinned create is steered off such nodes,
and refused only when every candidate node is over. A node whose agent does
not answer counts as over: delivery cannot be proven there.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kubernetes_asyncio import client

from ..core import tenancy
from . import config

HOSTNAME = "kubernetes.io/hostname"


async def _over(core: Any, node: str) -> str | None:
    try:
        status = await core._agent_request(node, "GET", "/agent/v1/usage/status", timeout=5)
    except RuntimeError as e:
        return f"usage spool state unknown ({e})"
    spool = status["spool"]
    if spool["above_high_water"]:
        return f"usage spool {spool['bytes']} of {spool['max_bytes']} bytes, above its {spool['high_water_percent']}% high-water mark"
    return None


def _exclude(pod: Any, nodes: list[str]) -> None:
    expr = client.V1NodeSelectorRequirement(key=HOSTNAME, operator="NotIn", values=sorted(nodes))
    pod.affinity = pod.affinity or client.V1Affinity()
    na = pod.affinity.node_affinity = pod.affinity.node_affinity or client.V1NodeAffinity()
    req = na.required_during_scheduling_ignored_during_execution
    if req is None:
        na.required_during_scheduling_ignored_during_execution = client.V1NodeSelector(
            node_selector_terms=[client.V1NodeSelectorTerm(match_expressions=[expr])]
        )
        return
    for term in req.node_selector_terms:
        term.match_expressions = [*(term.match_expressions or []), expr]


async def require(core: Any, dep: Any, tenant: dict | None) -> None:
    if not config.load().require_delivery:
        return
    pod = dep.spec.template.spec
    pinned = (pod.node_selector or {}).get(HOSTNAME) or pod.node_name
    if pinned:
        nodes = [pinned]
    else:
        nodes = tenancy.spec_of(tenant).get("nodes") or [
            n.metadata.name for n in (await (await core._get_core_v1_client()).list_node()).items
        ]
    reasons = dict(zip(nodes, await asyncio.gather(*(_over(core, n) for n in nodes)), strict=True))
    over = {n: r for n, r in reasons.items() if r}
    if over and (pinned or len(over) == len(nodes)):
        raise tenancy.AdmissionError(
            503,
            "UsageSpoolFull",
            "metering.requireDelivery refuses the create: "
            + "; ".join(f"node {n}: {r}" for n, r in sorted(over.items()))
            + ". Fix the usage sink (k7-agent /health) so the spool drains.",
            {"nodes": sorted(over)},
        )
    if over:
        _exclude(pod, list(over))
