# Security Policy

## Supported Versions

This project is pre-1.0 and under active development; breaking changes may
occur until 1.0.0. Security fixes land on the latest release line only.

| Version | Supported |
|---------|-----------|
| 0.2.1 and later | Yes |
| 0.2.0 and earlier | No — upgrade to 0.2.1 |

## Reporting a Vulnerability

If you believe you have found a security vulnerability, please email:

- **security@katakate.org** (preferred)
- Or open a private security advisory via GitHub
  (Security → Advisories → Report a vulnerability)

Please include:

- A detailed description of the issue and potential impact
- Steps to reproduce or proof-of-concept
- Affected versions / commit SHAs and environment details

We aim to acknowledge reports within 72 hours and provide a remediation
plan or mitigation timeline when applicable.

Do **not** open a public issue for security-sensitive reports.

## Scope and current model

- Nodes run **K3s**. A cluster (or a single node) can install **multiple
  sandbox backends**; each sandbox picks one:
  - **`kfd`** (`kata-firecracker-devmapper`) — Firecracker via Kata. The
    Firecracker process runs inside the **jailer** (chroot + dropped
    capabilities + seccomp). An integration test asserts the jailer is
    active after install.
  - **`kql`** (`kata-qemu-longhorn`) — **QEMU** via Kata with a Longhorn
    PVC root (durable disk, named snapshots / restore / disk-only fork).
  - **`k7d`** — [Katakate/k7d](https://github.com/Katakate/k7d)
    (`runtimeClassName: k7`); CoW sibling-fork isolation differs — see
    k7d's `SECURITY.md`.
  - **`k7d-fc`** (`runtimeClassName: k7-fc`) — same daemon and shim;
    stock Firecracker under the stock jailer. What that buys (and does
    not) is k7d's `SECURITY.md` **Firecracker profile** table. hostPath
    volumes are refused before the pod is created.
- Sandbox containers run as non-root with restricted capabilities on top
  of the VM boundary.
- The control plane API uses API keys with hashed storage and expiry
  (file-backed by default at `/etc/k7/api_keys.json` — rotate and protect
  that file). Keys may optionally be scoped to one or more namespaces
  (`k7 generate-api-key -n <ns>`); absent/empty scope keeps the historical
  unrestricted behaviour (backward compatible). Scoped keys are enforced
  on every namespace-bearing and cluster-scoped control-plane route —
  they cannot list across all namespaces, touch namespaces outside their
  list, or read cluster-wide topology (e.g. ``GET /api/v1/nodes/storage``).
- **The control-plane API is on a public NodePort (`31007`) on a public
  node, over HTTPS by default**. A Caddy sidecar terminates TLS; the API
  container and its probes stay HTTP on `:8000`. The default cert is a
  playbook-minted cluster CA at `/etc/k7/tls/ca.crt` (Let's Encrypt cannot
  issue for a bare IP). Clients trust it with `k7 config set api.ca`,
  `--api-ca`, or `K7_API_CA`. `--api-hostname` (Let's Encrypt via Caddy)
  and `--api-tls-cert`/`--api-tls-key` are the other tracks;
  `--api-insecure-http` restores a plain HTTP NodePort and must be treated
  as such (`verify=False` on an `https://` URL is an error). `k7 install
  --api-allow-cidr <cidr>` (repeatable, Cilium only) restricts *who can
  reach* it: a `CiliumNetworkPolicy` on the `k7-api` pod allows the
  operator's CIDRs plus the reserved `host` / `remote-node` / `health` /
  `kube-apiserver` peers (the kubelet probe path), and the Service switches
  to `externalTrafficPolicy: Local` so the pod sees the real client IP.
  This is **defence in depth for operators who know their client CIDRs, not
  a secure API**: there is still no rate limiting. The flag is off by
  default and affects nothing when unset. It is a pod-level policy — SSH
  and the k3s API port are never touched, so a wrong CIDR costs API access
  from your workstation and nothing else (`kubectl -n kube-system delete
  ciliumnetworkpolicy k7-api-ingress` undoes it). A `fromCIDR` peer is not
  evaluated for Cilium-managed sources, so the allowlist constrains
  external clients only; in-cluster access is governed by the pod policies.
- Control-plane OCI registry inspection (used to resolve image
  entrypoint/cmd) rejects registry hosts that are not on an allowlist
  (default: `registry-1.docker.io`, `ghcr.io`, `quay.io`, `public.ecr.aws`;
  extend via `K7_REGISTRY_ALLOWLIST`) and rejects any host that resolves
  to loopback/private/link-local/metadata/reserved addresses. Redirect
  following is disabled. The previous `localhost`→`http` downgrade path
  has been removed.
- **Ingress** to sandboxes is denied by default (NetworkPolicy) and opt-in
  per sandbox: `--ingress-port` opens TCP ports, `--ingress-from`
  (`sandbox:` / `namespace:` / `cidr:`) scopes who may connect. With no
  source given, an opened port is reachable only from sandboxes in the
  same namespace — never from the internet. A `cidr:` source does **not**
  scope in-cluster peers on Cilium: an `ipBlock` peer is not evaluated for
  pod-to-pod traffic, so such a rule leaves the opened port reachable from
  any sandbox in the cluster. `k7 create` warns when one is used; scope
  in-cluster access with `sandbox:` / `namespace:` instead. See
  `docs/BACKENDS.md` for the measured behaviour. `--expose-port`
  additionally publishes a port outside the cluster (NodePort with
  `externalTrafficPolicy: Local`, so the pod sees the real client IP); it
  requires a matching `--ingress-port`, and on a public node it puts the
  sandbox on a public IP. A `cidr:` allowlist there does not constrain
  clients on the cluster's own nodes.
  **Egress** is per-sandbox: open, blocked, CIDR allowlist, or **FQDN**
  allowlist when Cilium is the CNI (default). DNS is blocked by default
  when egress is locked down.
- **Sandboxes never reach the platform.** With Cilium (default) a
  cluster-wide deny policy (`k7-sandbox-platform-deny`) stops every
  sandbox — in **every** egress mode, including `--egress-open` — from
  dialing the node it runs on, the other nodes, the Kubernetes API
  (node IP or `10.43.0.1`), cloud metadata (`169.254.0.0/16`), and the
  pods of `kube-system` / `longhorn-system` (`k7-api`, Longhorn, ...),
  CoreDNS excepted. "Open" egress means *the internet*, not *the
  cluster*. This is deny-only and composes with the per-sandbox egress
  policies; it does not defend against a VM escape. **Caveat:**
  `k7 install --cni flannel` clusters do **not** get this isolation
  (no Cilium policy engine) — an `--egress-open` sandbox there can reach
  the node and the API server.
- **Multi-node** clusters are supported (Ansible inventory; Longhorn for
  the QEMU/`kql` path). Cilium FQDN egress applies cluster-wide.

See also the docs: security model, networking, and backends comparison.

### Known limitations (pre-1.0)

- Default `k7 install` serves `k7-api` over HTTPS with a playbook-minted
  cluster CA, so API keys no longer travel in cleartext. There is still
  no rate limiting. `--api-insecure-http` is the escape hatch back to a
  plain HTTP NodePort and must be treated as such. `--api-allow-cidr`
  is defence in depth on who can connect; it is not a secure-API claim.
- No rate limiting or abuse protection at the API layer yet.
- API key storage is local file-backed; treat the API host as trusted.
  Namespace scoping is an opt-in tenancy boundary on top of that model —
  unscoped keys still have full cross-namespace control-plane access.
- Young project; no independent security audit yet.
- The `k7d` backend has a different isolation trade-off for CoW sibling
  forks — see k7d's `SECURITY.md`.
- Prefer a dedicated RBAC-restricted kubeconfig for the API rather than
  cluster-admin credentials in production.

## Responsible Disclosure

Do not publicly disclose vulnerabilities before we have had a reasonable
time to investigate and release fixes. We appreciate coordinated
disclosure and will credit reporters unless anonymity is requested.
