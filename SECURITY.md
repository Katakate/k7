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
- Sandbox containers run as non-root with restricted capabilities on top
  of the VM boundary.
- The control plane API uses API keys with hashed storage and expiry
  (file-backed by default at `/etc/k7/api_keys.json` — rotate and protect
  that file). Keys may optionally be scoped to one or more namespaces
  (`k7 generate-api-key -n <ns>`); absent/empty scope keeps the historical
  unrestricted behaviour (backward compatible). Scoped keys are enforced
  on every namespace-bearing endpoint — they cannot list across all
  namespaces or touch namespaces outside their list.
- Control-plane OCI registry inspection (used to resolve image
  entrypoint/cmd) rejects registry hosts that are not on an allowlist
  (default: `registry-1.docker.io`, `ghcr.io`, `quay.io`, `public.ecr.aws`;
  extend via `K7_REGISTRY_ALLOWLIST`) and rejects any host that resolves
  to loopback/private/link-local/metadata/reserved addresses. Redirect
  following is disabled. The previous `localhost`→`http` downgrade path
  has been removed.
- **Ingress** to sandboxes is denied by default (NetworkPolicy).
  **Egress** is per-sandbox: open, blocked, CIDR allowlist, or **FQDN**
  allowlist when Cilium is the CNI (default). DNS is blocked by default
  when egress is locked down.
- **Multi-node** clusters are supported (Ansible inventory; Longhorn for
  the QEMU/`kql` path). Cilium FQDN egress applies cluster-wide.

See also the docs: security model, networking, and backends comparison.

### Known limitations (pre-1.0)

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
