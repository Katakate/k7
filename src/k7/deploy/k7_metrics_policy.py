"""Render the policies that keep the ``/metrics`` port (9464) scraper-only.

k7-api and k7-agent serve metrics on 9464. Only pods matching
the scraper selector (``--metrics-scraper ns:key=value[,key=value]``) may
reach it; with no selector, nothing can.

- Cilium: one CiliumNetworkPolicy. ``ingressDeny`` covers 9464 from every
  source except the scraper, without default-deny, so ports 8000/8443/80
  and the ``--api-allow-cidr`` policy are untouched.
- Other CNIs (flannel + the k3s policy controller): NetworkPolicies that
  admit the API ports from anywhere, as before, and 9464 from the scraper.

Stdlib only: the playbook runs it with the node's ``python3``. Output is
JSON, which ``kubectl apply`` reads as YAML.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

METRICS_PORT = 9464
CILIUM_FILE = "cilium/k7-metrics.yaml"
NETPOL_FILE = "k7-metrics-networkpolicy.yaml"
NS_LABEL = "k8s:io.kubernetes.pod.namespace"
# Sources a pod selector cannot name: everything outside the pod network.
DENIED_ENTITIES = ["world", "host", "remote-node", "kube-apiserver", "ingress", "health", "unmanaged", "init"]
_LABEL_KEY = re.compile(r"^([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?)?$")
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

Scraper = tuple[str, dict[str, str]]


def parse_scraper(spec: str) -> Scraper | None:
    """``monitoring:app.kubernetes.io/name=prometheus`` → namespace and pod labels; "" → None."""
    if not spec:
        return None
    namespace, sep, labels = spec.partition(":")
    if not sep or not _NAMESPACE.match(namespace) or not labels:
        raise ValueError(f"--metrics-scraper must be <namespace>:<key>=<value>[,<key>=<value>], got {spec!r}")
    pods: dict[str, str] = {}
    for pair in labels.split(","):
        key, eq, value = pair.partition("=")
        if not eq or not _LABEL_KEY.match(key) or not _LABEL_VALUE.match(value) or key in pods:
            raise ValueError(f"--metrics-scraper label {pair!r} is not a unique <key>=<value> label")
        pods[key] = value
    return namespace, pods


def _port() -> list[dict[str, Any]]:
    return [{"ports": [{"port": str(METRICS_PORT), "protocol": "TCP"}]}]


def cilium_policy(namespace: str, scraper: Scraper | None) -> dict[str, Any]:
    if scraper is None:
        deny: list[dict[str, Any]] = [{"fromEntities": ["all"], "toPorts": _port()}]
        allow: list[dict[str, Any]] = []
    else:
        ns, labels = scraper
        others: list[dict[str, Any]] = [{"matchExpressions": [{"key": NS_LABEL, "operator": "NotIn", "values": [ns]}]}]
        others += [
            {"matchLabels": {NS_LABEL: ns}, "matchExpressions": [{"key": k, "operator": "NotIn", "values": [v]}]}
            for k, v in sorted(labels.items())
        ]
        deny = [
            {"fromEntities": DENIED_ENTITIES, "toPorts": _port()},
            {"fromEndpoints": others, "toPorts": _port()},
        ]
        allow = [{"fromEndpoints": [{"matchLabels": {NS_LABEL: ns, **labels}}], "toPorts": _port()}]
    spec: dict[str, Any] = {
        "description": "Only the metrics scraper reaches k7-api / k7-agent :9464",
        "endpointSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["k7-api", "k7-agent"]}]},
        "enableDefaultDeny": {"ingress": False},
        "ingressDeny": deny,
    }
    if allow:
        spec["ingress"] = allow
    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "k7-metrics", "namespace": namespace},
        "spec": spec,
    }


def network_policies(namespace: str, scraper: Scraper | None, *, acme: bool) -> list[dict[str, Any]]:
    service_ports = {"k7-api": [8000, 8443, *([80] if acme else [])], "k7-agent": [8000]}
    out = []
    for app, ports in service_ports.items():
        ingress: list[dict[str, Any]] = [{"ports": [{"port": p, "protocol": "TCP"} for p in ports]}]
        if scraper is not None:
            ns, labels = scraper
            source = {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}},
                "podSelector": {"matchLabels": labels},
            }
            ingress.append({"from": [source], "ports": [{"port": METRICS_PORT, "protocol": "TCP"}]})
        out.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": f"{app}-metrics", "namespace": namespace},
                "spec": {"podSelector": {"matchLabels": {"app": app}}, "policyTypes": ["Ingress"], "ingress": ingress},
            }
        )
    return out


def write(manifest_dir: Path, *, cilium: bool, scraper: Scraper | None, namespace: str, acme: bool) -> Path:
    """Write the policy file for this CNI and remove the other one's."""
    keep, drop = (CILIUM_FILE, NETPOL_FILE) if cilium else (NETPOL_FILE, CILIUM_FILE)
    doc = (
        cilium_policy(namespace, scraper)
        if cilium
        else {"apiVersion": "v1", "kind": "List", "items": network_policies(namespace, scraper, acme=acme)}
    )
    path = manifest_dir / keep
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    (manifest_dir / drop).unlink(missing_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render the k7 /metrics network policies")
    p.add_argument("command", choices=["write"])
    p.add_argument("--manifest-dir", required=True)
    p.add_argument("--cni", choices=["cilium", "flannel"], required=True)
    p.add_argument("--scraper", required=True, help="<namespace>:<key>=<value>[,...], or '' for no scraper")
    p.add_argument("--namespace", required=True)
    p.add_argument("--acme", choices=["true", "false"], required=True)
    args = p.parse_args(argv)
    path = write(
        Path(args.manifest_dir),
        cilium=args.cni == "cilium",
        scraper=parse_scraper(args.scraper),
        namespace=args.namespace,
        acme=args.acme == "true",
    )
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
