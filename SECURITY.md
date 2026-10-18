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
- The control plane API uses API keys with hashed storage and expiry,
  stored in the Kubernetes Secret `kube-system/k7-api-keys` (protect
  etcd: enable K3s `--secrets-encryption` or KMS envelope encryption on
  managed clusters). Tokens are `k7_<id>_<secret>`; the store holds
  `sha256(secret)`. A plain SHA-256 is deliberate: the secret is 256
  random bits, so the threat is store disclosure, not guessing, and a
  slow KDF adds nothing. The `<id>` is safe to log and makes leaked
  keys greppable. The API never writes the key store on authenticate;
  it serves a snapshot refreshed every 5 s and returns 503 rather than
  trusting a snapshot older than 60 s. Revocation reaches every replica
  within one refresh. The `k7-api` ServiceAccount can only `get` and
  `patch` the key Secret (namespaced Role; `patch` is for the admin key
  routes); it cannot read Secrets elsewhere. Every key carries a **role**
  (see *Roles and OIDC* below). Keys may additionally be narrowed to one or more namespaces
  (`k7 generate-api-key -n <ns>`); they may also be scoped to one or more
  Kubernetes nodes (`k7 generate-api-key --node <node>`). Absent/empty
  narrowing leaves the role's own scope (its tenant, or the cluster).
  Narrowed keys are enforced on every namespace-bearing and cluster-scoped
  control-plane route — they cannot list across all namespaces, touch
  namespaces outside their list, or read cluster-wide topology (e.g.
  ``GET /api/v1/nodes/storage``). A **node-scoped** key may only
  create/restore/fork sandboxes onto its listed nodes (a single-node
  scope auto-pins; several nodes require an explicit ``node_name``).
  Namespace scope is the control-plane tenancy boundary; node scope is
  the placement boundary (API-key `--node` is a Kubernetes node name
  from `k7 nodes list` / `kubectl get nodes` — the Linux hostname K3s
  registered — joined via the kubelet-stamped `kubernetes.io/hostname`
  label; inventory.ini does not write that label). Together they
  keep a tenant off another tenant's k7d daemon **only if that node is
  also dedicated** (`k7 nodes dedicate NODE --tenant ID` writes
  `k7.katakate.org/tenant` as a label and a NoSchedule taint). Without
  the taint, an unscoped key can still land on the same node. An
  `operator` or `admin` key without narrowing has cross-namespace
  control-plane access and unrestricted placement.
- **Roles and OIDC. K7 roles are not Kubernetes RBAC.** They
  are application roles checked inside k7-api; the k7-api
  ServiceAccount stays the only Kubernetes actor, and nobody gets a
  Kubernetes identity through K7. Four fixed roles:
  `read-only` (list/get sandboxes and snapshots) and `user` (plus
  create, exec, logs, pause/resume, fork, snapshots) are granted **per
  tenant** (the `Tenant` CR's `spec.namespaces`); `operator`
  (cluster-wide read, snapshot GC, node storage, node dedication; no
  exec, no keys) and `admin` (everything, including keys and role
  bindings) are cluster-wide. One table, `PERMISSIONS` in
  `src/k7/api/authz.py`, maps every route to its roles; a route missing
  from it answers 500 and fails CI. Checks compose: route role, then
  tenant, then the key's namespace/node narrowing; the first failure is a
  403 that names the missing role or scope. `k7 api keys migrate` gives
  keys from before roles a role: `admin: true` → `admin`, namespace-scoped →
  `user` (still narrowed to those namespaces), unscoped → `admin` flagged
  `unscoped_legacy` (`list-api-keys` warns; re-issue them with a real
  role). A key without a role is refused (403) until migrated.
  **OIDC** (`k7 install --oidc-issuer/--oidc-audience/--oidc-client-id/
  --oidc-groups-claim/--oidc-username-claim`, optional `--oidc-jwks-ttl`,
  or chart `auth.oidc`; all five or none; `https://` issuers only) lets
  humans and workloads send
  `Authorization: Bearer <JWT>`. k7-api validates statelessly: signature
  against the issuer's JWKS (cached for `K7_OIDC_JWKS_TTL_SECONDS`, default
  3600, minimum 10; also refetched on an unknown `kid` at most every 10 s).
  A failed refresh is 503, including for the next 10 s, and an expired
  cache is not used. `alg` is RS256 or ES256 only and must match the key
  type (`none`, HS256 and key confusion are 401). `iss`, `aud`, `exp`
  (60 s leeway) and `sub` are required. When the username claim is
  `email`, `email_verified` must be boolean true. Tokens starting
  `k7_` and the `X-API-Key` header always take the key path; a bad key is
  never retried as OIDC. Groups, usernames or subjects map to roles
  through ConfigMap `k7-role-bindings` (`k7 role-bindings get|apply`,
  `PUT /api/v1/role-bindings`, admin only), read on every request, so an
  edit or a `deny-subjects` entry applies on the next request on every
  replica. Several matching bindings combine as the highest role per
  tenant plus the highest cluster role. An unbound subject gets a 403
  that echoes its groups (to debug claim shapes); bindings validate
  tenants, role/tenant pairs and duplicates, and a write that leaves no
  `admin` binding while no unexpired `admin` key exists is refused.
  k7-api stores no sessions and never sees refresh tokens: `k7 login`
  (device flow, or `--browser` for Authorization Code + PKCE) caches them
  in `~/.config/k7/credentials.json` (0600) and refreshes them itself;
  `k7 logout` deletes the cache, then revokes at the IdP. Configure the
  IdP for short access tokens (1 h or less): a JWT stays valid until it
  expires, and `deny-subjects` is the immediate stop. Keep an `admin`
  **break-glass key** offline: keys keep working during an IdP outage,
  and the server-node bootstrap path (`k7 generate-api-key` with the
  node's kubeconfig) always works. Reinstalling without the `--oidc-*`
  flags turns OIDC off (like `--api-allow-cidr`); so does the deprecated
  remote `POST /api/v1/install`.
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
- **Audit log.** Every mutating API request and every 401/403 produces
  one JSON line on the API pod's stdout (`k7.audit` logger) with the
  principal type (`key`/`oidc`), subject (key id or OIDC `sub`), role,
  tenant, IdP issuer, key id and name, route template, namespace,
  sandbox, status, latency and client IP. `X-Forwarded-For` is trusted
  only from the localhost Caddy sidecar. Tokens, emails, groups, bodies
  and exec command text are never logged (exec:
  sha256 + length). Emission failure fails the request closed (500).
  Ship stdout with your collector (Fluent Bit, CloudWatch, Loki); on
  exec-heavy workloads expect one line per exec and size log rotation
  accordingly.
- **Least-privilege control-plane pods.** `k7-agent` is not privileged:
  uid 0 plus the `k7d` group, pinned to gid 770 on every node (for the
  root-owned containerd socket and the `k7d:k7d` k7d socket), all
  capabilities dropped, no privilege escalation, read-only root
  filesystem, `RuntimeDefault` seccomp. Its only host mounts are the
  containerd socket directory, `/run/k7d`, `/var/lib/k7d` (ro) and
  `/run/k7` (ro); `/dev` is not mounted. kfd thin-pool stats come from
  the host timer `k7-storage-report.timer` (`/run/k7/storage.json`,
  500 if older than 90 s). `k7-api` runs as uid 1000 with no hostPath
  and forwards every k7d VM op to the node's `k7-agent`. No K7 pod
  opens `/dev/kvm` (the shims and VMMs run on the host), so there is no
  KVM device plugin.
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
  allowlist when Cilium is the CNI (default). DNS follows the egress mode:
  - **Blocked** and **CIDR-only** sandboxes have no DNS at all.
  - **FQDN** sandboxes resolve **only their allowlisted names**. Cilium's
    DNS proxy forwards those to CoreDNS and answers every other query
    itself with NXDOMAIN (`dnsProxy.dnsRejectResponseCode=nameError`), so
    a sandbox cannot exfiltrate data by encoding it into queries for
    `<data>.attacker.tld`. k7-api rewrites policies created by older
    versions (which allowed every name) when it starts.
  - **Open** sandboxes resolve anything, as they reach anything.

  Residual channels inside an allowlist are the allowlist's own:
  - A broad wildcard over a zone someone else controls (`*.nip.io`, a
    delegated subdomain, `*.amazonaws.com`) admits names that party
    chooses, and HTTPS to them.
  - Allowlisting a public resolver (`dns.google`, `1.1.1.1/32`) gives
    that resolver directly, DoH included.
  - An allowlisted name on a shared CDN IP can front for other tenants of
    that CDN.

  Without Cilium (`--cni flannel`, chart `networkPolicy.provider=kubernetes`),
  domain entries are refused. A CIDR entry containing the cluster DNS server
  (the `kube-dns` Service or CoreDNS pod IPs, checked at create and fork) is
  refused too: those engines match pod IPs, so it would give a recursive
  resolver. The check is against current IPs only. A CoreDNS pod rescheduled
  into an allowlisted pod-network range later re-opens the path, so do not
  allowlist pod or service ranges there.
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
- **Root on a K3s server is full cluster control.** Nodes in
  `[k7_servers]` (including the 3-node `--ha` example, where every
  host is a server) have `/etc/rancher/k3s/k3s.yaml` as **cluster-admin**
  (playbook `--write-kubeconfig-mode 644`), the join token, and etcd.
  The first master also has `/etc/k7/agent_token`. Node
  pins and `k7 nodes dedicate` do **not** contain that: they stop two
  tenants sharing a k7d daemon, they do not survive a VM escape onto
  a master. Put tenant sandboxes on `[k7_agents]`; do not dedicate a
  tenant onto a server running `k7-api`. Root on an **agent** is not
  the admin kubeconfig. `k7-agent` has no ServiceAccount token and
  ingress from `remote-node` is denied, so that breakout owns **that
  node's** k7d (that tenant, if dedicated) — not other agents and not
  the Kubernetes API. Per-node isolation is the blast radius for a
  worker compromise; it is not useless. The shared agent token remains
  on every node as defence-in-depth after the CNP.
- **`k7-node-installer` is root on every opted-in node, by design**
  (kata-deploy pattern; `src/k7/deploy/manifests/k7-node-installer/`).
  It is privileged, `hostPID`, mounts `/` at `/host` and runs Ansible in
  PID 1's namespaces. It runs only on nodes labelled
  `k7.katakate.org/node-pool=true` and is separate from `k7-agent`.
  Its image carries every release artifact, checksummed at build time;
  it downloads nothing at runtime (OS packages still come from the
  node's own package repos). Node labelling: RBAC cannot scope
  `patch nodes` to one node, so the ServiceAccount gets `get`/`patch`
  on all nodes and a ValidatingAdmissionPolicy
  (`k7-node-installer-own-node`) denies any patch that is not to the
  node in the caller's bound token (`authentication.kubernetes.io/node-name`,
  Kubernetes >= 1.32), touches the node spec, annotations, finalizers or
  ownerReferences, or changes a label outside `k7.katakate.org/`. That is
  defence in depth, not a boundary: root on the node can read that
  node's kubelet credentials. It does stop a leaked installer token from
  relabelling other nodes, for example into a tenant's pool. The cleanup
  ServiceAccount adds `list`/`delete` on pods (to drain sandboxes with
  `--force`) and is bound only by the cleanup DaemonSet; a second policy
  (`k7-node-cleanup-own-node-pods`) lets it delete only K7 sandbox pods
  (RuntimeClass `k7`, `k7-fc`, `kata`, `kata-qemu`) on its own node. Its
  `list pods` stays cluster-wide (admission does not see reads), so a
  leaked cleanup token can read every pod spec in the cluster.
  The release publishes the installer image signed, scanned and pinned
  by digest in `k7-node-installer.yaml`; the git-tree manifests carry a
  placeholder.

- **Runtime annotations are k7's alone.** containerd hands pod
  annotations to the k7d shim and to Kata, which act on them:
  `k7d.katakate.org/cluster-id` skips the pod netns (egress leaves as the
  node, outside Cilium), `fork-source-*` / `tree-*` clone any VM on the
  node by id, `io.katacontainers.config_path` and
  `io.katacontainers.config.runtime.*` override the hardened Kata
  config. The ValidatingAdmissionPolicy `k7-runtime-annotations` denies
  adding, changing or removing any `k7d.katakate.org/` or
  `io.katacontainers.` annotation on Pods and built-in pod templates
  (Deployment, ReplicaSet, StatefulSet, DaemonSet, Job, CronJob,
  ReplicationController) unless the caller is the k7-api ServiceAccount,
  in `system:masters`, or kube-controller-manager. Grant tenants
  namespace RBAC freely; this is what keeps them on the pod network.
  Behind it, containerd forwards only the keys k7 sets (`docker`,
  `docker-disk`, `fork-source-cluster`, `fork-source-vm`, Kata
  `default_memory` and the tenant rate limiters), and Kata enables only
  those three hypervisor annotations. Workloads
  created through other controllers (CRDs) are denied if their pods carry
  these keys.

See also the docs: security model, networking, and backends comparison.

### Tenants and quotas

- **`api.mode` is chosen at install, never defaulted** (`k7 install
  --api-mode production|development`, chart `api.mode`). `production`
  refuses to start while the key store holds an unscoped key and names
  each one: no role (from before roles), a migrated `unscoped_legacy` admin, or a
  namespace-only key with no tenant. It refuses `--api-insecure-http`
  (`K7_API_TRANSPORT`) and `K7_API_ENABLE_REMOTE_INSTALL=1`, and returns
  403 to those keys on every route at request time (including key
  minting, role bindings and `/install`), with the commands that
  re-scope them. Every response carries `X-K7-Mode`; `/health` reports
  it. `development` keeps them working (`unscoped_legacy` as admin).
- **A `Tenant`** (`tenants.k7.katakate.org/v1alpha1`, cluster-scoped)
  claims namespaces (label `k7.katakate.org/tenant`), dedicated nodes,
  a `defaultSize`, quotas and per-VM limits. A `--tenant` key reaches the
  tenant's namespaces intersected with its own `-n` list, read from the
  Tenant on every request, so adding or removing a namespace applies at
  once and a key whose tenant is deleted reaches nothing. Cross-tenant
  requests are 403 before any cluster call, on every namespaced route.
  Fork and restore never leave their namespace. A namespace claimed by
  two Tenants (only possible by writing CRs by hand) fails every request
  there with 500 `TenantConflict` naming both. Creates carry the tenant
  and `k7.katakate.org/api-key-id` labels on every object; tenants with
  `spec.nodes` land only there, through a selector and toleration.
  Tenant routes (`/api/v1/tenants`): `operator` and `admin` read,
  only `admin` creates, updates and deletes (`PERMISSIONS`).
- **Quotas** (`sandboxes`, `vcpu`, `memory`, `disk`, `snapshotStorage`)
  are checked at K7's two choke points, the sandbox Deployment create and
  the VolumeSnapshot create, so create, fork, restore, pause-with-snapshot
  and named snapshots are all covered. The check and the create run under
  a per-tenant `coordination.k8s.io` Lease (`k7-quota-<tenant>`, TTL
  10 s), so concurrent creates on any number of API replicas cannot
  overshoot. A Lease not acquired within 5 s is 503 `QuotaLockTimeout`
  and creates nothing; a Lease lost mid-check is 503 `QuotaLockLost`.
  Over-quota is 403 `QuotaExceeded` with the dimension, limit, usage and
  request, and is audit-logged. Usage is **reserved, not used**: a paused
  sandbox keeps its vCPU, memory and disk until it is deleted. kfd counts
  its 10 GB devmapper root; kql counts the PVCs its Deployment mounts.
  The leader replica writes `Tenant.status.usage` every 60 s.
- **Enforcement is at the K7 API.** `kubectl apply` by a cluster admin
  bypasses it. For a single-namespace tenant a `ResourceQuota`
  (`k7-tenant-quota`: `count/deployments.apps`, `requests.storage`) is the
  Kubernetes-side backstop; multi-namespace tenants have none, since a
  ResourceQuota cannot span namespaces.
- **Per-VM limits fail closed.** `limits.network` (`egressBps`,
  `ingressBps`, bits/s) is set as Kata's `tx_/rx_rate_limiter_max_rate`
  annotations (Firecracker token buckets on kfd, tc on the tap for
  kata-qemu). On kfd, K7 encodes the value for Kata's Firecracker unit
  conversion, which would otherwise run 4.8% fast. Firecracker's bucket
  starts full, so the first second can burst to twice the cap; the
  30-second average stays within 10%. This replaces the spec's Cilium
  Bandwidth Manager, which exists only under Cilium (not `--cni flannel`
  or a chart install on another CNI) and is aimed at pod egress; the
  hypervisor limiter caps both directions per VM whatever the CNI. A
  request may lower a cap, never raise it. A backend that cannot enforce
  a configured limit refuses the create with 400 `LimitNotEnforceable`
  instead of running unlimited: `diskIO` on every backend (Kata 3.24's runtime has
  no block rate-limiter annotation, and k7d has no disk limiter yet),
  `network` on k7d and k7d-fc, and a `disk` quota or
  `maxSandboxSize.disk` on k7d and k7d-fc (K7 cannot read a k7d VM's
  writable-disk size). `maxSandboxSize` is 400; production mode needs a
  CPU and memory limit on every sandbox, filled from `defaultSize`.

### Known limitations (pre-1.0)

- Default `k7 install` serves `k7-api` over HTTPS with a playbook-minted
  cluster CA, so API keys no longer travel in cleartext. There is still
  no rate limiting. `--api-insecure-http` is the escape hatch back to a
  plain HTTP NodePort and must be treated as such. `--api-allow-cidr`
  is defence in depth on who can connect; it is not a secure-API claim.
- No rate limiting or abuse protection at the API layer yet.
- API keys are only as safe as the cluster's Secrets: anyone with
  `get secrets` in `kube-system` (or root on a server) reads the hashes.
  Tenant roles are the tenancy boundary on top of that model. Migrated
  `unscoped_legacy` keys are cluster `admin` (key minting, role bindings,
  remote install) only in `development` mode; `production` mode refuses
  them at startup and on every request until they are re-scoped.
  Node scoping is the matching opt-in **placement** boundary: without
  `--node` on the key, two namespaces can still share a node's k7d
  daemon. Pair `-n` and `--node`, and run `k7 nodes dedicate` on that
  node, for tenant isolation on k7d. The dedicate step is a label +
  taint (`k7.katakate.org/tenant`); a hostname pin alone does not keep
  other sandboxes off the node. Neither contains a host compromise:
  root on `[k7_servers]` is cluster-admin.
- Until the release after 0.4.0, `POST /api/v1/install` accepted any
  valid key and request-supplied playbook content. It is now 404 unless
  the cluster was installed with `--api-remote-install`, cluster-wide `admin` only,
  and takes only node names: it labels them into the k7-node-installer
  pool, whose image and config are fixed at deploy time. An admin can
  therefore make the installer root-prepare any existing node it names
  (servers only with `--node-installer-allow-server-restart`). Turning it
  off removes that path: a playbook re-install deletes the installer
  stack and pool labels; on the chart, whose installer is core, Helm
  drops the API's Role and the pre-install/pre-upgrade hook, which may
  patch nodes while it runs, removes the remote-install marker and the
  records. The nodes the API added stay in the pool and stay prepared.
- An OIDC token is valid until it expires; there is no revocation list
  beyond `deny-subjects`. Entra ID group overage (more than ~200 groups
  replaced by a link) is not followed: bind such users by `user` or
  `subject`, or emit group ids in the token.
- Young project; no independent security audit yet.
- The `k7d` backend has a different isolation trade-off for CoW sibling
  forks — see k7d's `SECURITY.md`.
- Prefer a dedicated RBAC-restricted kubeconfig for the API rather than
  cluster-admin credentials in production.
- **Host compromise is cluster-wide on servers.** `--node` / `dedicate`
  isolate k7d placement, not a root shell on `[k7_servers]`. The HA
  inventory example makes every node a server. Tenant boxes belong in
  `[k7_agents]`. `k7-agent` has no ServiceAccount token and no
  `remote-node` ingress, so root on an agent is that node (that tenant
  if dedicated), not k7-api RBAC on the rest of the cluster.

## Responsible Disclosure

Do not publicly disclose vulnerabilities before we have had a reasonable
time to investigate and release fixes. We appreciate coordinated
disclosure and will credit reporters unless anonymity is requested.
