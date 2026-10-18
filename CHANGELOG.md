# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] — 2026-10-11

### Added

- **Observability** (`docs/OBSERVABILITY.md`):
  - k7-api and k7-agent serve Prometheus `/metrics` on port 9464, with
    request, op-latency, auth, key-store, kube-API, k7d RPC, storage, VM
    and TLS-expiry metrics and no per-sandbox labels.
  - Only the pods named by `k7 install --metrics-scraper` (or the chart's
    `observability.scraperSelector`) can reach that port.
  - Pods log JSON lines with secrets scrubbed. A request id from
    `X-Request-ID` (or a minted UUIDv7) follows each request into
    k7-agent.
  - Four Grafana dashboards, eleven alerts tested with `promtool`, and a
    runbook per alert.
  - The chart gains `observability.*` values for a ServiceMonitor,
    PrometheusRule and dashboards.
  - `k7 install --monitoring` installs a pinned kube-prometheus-stack
    92.3.0 for single-box installs.
- **PR CI and a signed release pipeline.** GitHub Actions run lint,
  typecheck, unit tests and shellcheck on every PR (actions pinned by
  commit SHA). Tags build `ghcr.io/katakate/k7-api` and
  `ghcr.io/katakate/k7-node-installer`, scan them with Trivy, sign them
  with cosign keyless, and attach an SPDX SBOM and SLSA provenance; both
  digests go into `images.lock.yaml`. The `.deb` and wheel ship with
  `sha256sums.txt` and cosign bundles, next to `k7-node-installer.yaml`
  and `k7-node-cleanup.yaml` (the node-installer manifests with the
  image pinned by digest; the git tree carries a placeholder).
- **Helm chart for existing clusters** (`deploy/helm/k7`):
  k7-api, k7-agent, k7-node-installer, RuntimeClasses, snapshot-gc and the
  Cilium policies on a cluster you already run. `networkPolicy.provider`
  (`cilium`/`kubernetes`/`none`) and `tls.mode` are required; weaker
  providers need explicit acknowledgements, and kql needs your
  StorageClass and VolumeSnapshotClass. Hooks refuse a cluster that cannot
  enforce the values, create the agent-token and key Secrets, and undo
  node prep on uninstall; `helm test` creates, execs and forks one sandbox
  per backend. Validated on RKE2 v1.36.5 + Cilium 1.19.2
  (`deploy/helm/k7/ci/rke2-hetzner.md`).
- **Roles and OIDC login**. Four fixed K7 roles: `read-only`
  and `user` per tenant (`Tenant` CR), `operator` and `admin`
  cluster-wide; one `PERMISSIONS` table enforced on every route. Keys
  carry a role (`k7 generate-api-key NAME --role R [--tenant T]`).
  Optional OIDC (`k7 install --oidc-*`, chart `auth.oidc`): k7-api
  validates bearer JWTs statelessly (JWKS, RS256/ES256, iss/aud/exp) and
  maps groups, users or subjects to roles through ConfigMap
  `k7-role-bindings`, with `deny-subjects`. New: `k7 login` (device
  flow, `--browser` for PKCE), `k7 logout`, `k7 whoami`,
  `k7 role-bindings get|apply`, `--api` on the key and node-dedication
  commands, admin API routes for keys, role bindings and node
  dedication, and an SDK `token_provider`. K7 roles are not Kubernetes
  RBAC. See `docs/HARDENING.md` and `docs/THREAT-MODEL.md`.

- **Tenants and quotas**. A cluster-scoped `Tenant` CRD
  (`k7.katakate.org/v1alpha1`, playbook and chart) groups namespaces,
  dedicated nodes, a `defaultSize`, quotas (`sandboxes`, `vcpu`,
  `memory`, `disk`, `snapshotStorage`) and per-VM limits. `k7 tenant
  create/update/get/list/delete/adopt`, `k7 generate-api-key --role user|read-only --tenant`,
  `k7 nodes dedicate` records the node on the Tenant, and `/api/v1/tenants` routes
  (reads for `operator`/`admin`, management for `admin`). Tenant keys reach the tenant's namespaces
  (intersected with their own `-n`), and every created object carries
  the tenant and key-id labels. Quotas are checked under a per-tenant
  Lease at the Deployment and VolumeSnapshot creates (403 `QuotaExceeded`
  with the numbers; 503 if the Lease is not acquired in 5 s), and
  `Tenant.status.usage` is reconciled every 60 s. `limits.network` caps
  kfd/kql VM traffic with Kata's hypervisor rate limiters (`k7 create
  --egress-bps/--ingress-bps` may lower it); a limit a backend cannot
  enforce is refused with 400. See SECURITY.md, Tenants and quotas.
- **`k7_dns_upstreams` / `k7 install --dns-upstream`** pins CoreDNS's
  upstream resolvers (1 to 3 IPv4 addresses, validated before the install
  runs). It is applied through k3s's `--resolv-conf`, and re-running the
  install converges an existing cluster. Unset keeps the host's resolvers.
  The hardware CI uses 1.1.1.1 and 9.9.9.9, because Hetzner's 185.12.64.x
  resolvers timed out during suite runs. The Helm chart does not manage
  CoreDNS.

- **Metering and usage** (`docs/METERING.md`):
  - k7-agent samples every sandbox pod on its node (5-60 s) into
    CloudEvents `io.katakate.k7.sandbox.usage.v1`: seconds, reserved
    vCPU·s and GiB·s split by `running`/`paused` (paused k7d VMs accrue
    memory, not vCPU), plus egress bytes from CRI `PodSandboxStats`.
    Backends without network stats are reported `egress_unavailable`,
    never 0.
  - A leader-elected sampler in k7-api emits provisioned disk and
    VolumeSnapshot GiB·s, including stopped sandboxes.
  - Event ids are `sha256(cluster-id, uid, meter, window_start)`, with the
    cluster id in ConfigMap `k7-cluster-id`. Missed time becomes `gap`
    events, never extrapolated usage. The v1 JSON Schemas are frozen in
    `src/k7/metering/schemas/`.
  - Events go through a fsynced, capped hostPath spool
    (`/var/lib/k7/usage-spool`) to Prometheus counters (no sandbox label),
    an opt-in CloudEvents batch HTTP sink with an OpenMeter preset (which
    leaves out null `data` keys, since OpenMeter refuses them), and a
    stdout sink. A 4xx stops a sink loudly (`K7UsageSinkStopped`).
  - `k7 usage` / `GET /api/v1/usage` compute showback from Prometheus.
    `metering.requireDelivery` refuses creates (503 `UsageSpoolFull`) on
    nodes whose spool is above its high-water mark.
  - `k7 install --metering-*` flags and the chart's `metering.*` values;
    `--api-mode production` needs an explicit sample interval.
  - `k7 nodes storage` shows each node's usage spool.

### Changed

- **API keys need a role.** `k7 install` migrates the key store (or run
  `k7 api keys migrate`; chart installs: run it with the cluster's
  kubeconfig): `admin: true` becomes `admin`, namespace-scoped keys become `user`
  (still narrowed to their namespaces), and unscoped keys become `admin`
  flagged `unscoped_legacy` (`k7 list-api-keys` warns; re-issue them). A
  key without a role is refused with 403. `k7 generate-api-key` requires
  `--role`; `--admin` is gone. Audit lines gain `principal_type`,
  `subject`, `role`, `tenant` and `idp_issuer`.

- **`k7 install --api-mode production|development` is required** (chart:
  `api.mode`), with no default. `production` refuses to start with an
  unscoped API key in the store (and names it): a key with no role, a
  migrated `unscoped_legacy` admin, or a namespace-only key with no
  tenant. The same keys get 403 on every request in production;
  `development` still lets an `unscoped_legacy` key act as admin.
  Production also refuses to start with
  `--api-insecure-http`, or with remote install enabled; responses carry
  `X-K7-Mode`. **Migration:** re-run `k7 install` with `--api-mode
  development` to keep today's behaviour. To move to production, run
  `k7 tenant adopt --from-keys` (dry run: one tenant per connected group
  of key namespaces, plus the unscoped keys you must revoke or reissue
  with `--role user --tenant T`, or `--role admin` for a real admin), then `--apply`, then reinstall with
  `--api-mode production`. `k7 nodes dedicate --tenant T` now needs the
  Tenant to exist.

- **Cluster settings are explicit.** `K7_SYSTEM_NAMESPACE`,
  `K7_EGRESS_MODE` and `K7_KQL_STORAGE_CLASS`/`K7_KQL_SNAPSHOT_CLASS` come
  from pod env or `/etc/k7/config` (written by `k7 install`: kube-system,
  full, longhorn, longhorn); a missing one is an error. Creates with egress
  the cluster cannot enforce are refused (400): FQDNs under
  `networkPolicy.provider=kubernetes`, any rule under `none`. New
  `k7 --system-namespace`; `K7_KUBECONFIG` points host-side tooling at a
  non-K3s cluster.

- **Install playbook split into roles** (`k7_node_prep`, `k7_k3s`,
  `k7_cluster`) with a distro adapter (`roles/k7_node_prep/vars/<distro>.yml`)
  selected by the required `k7_distro`. `k7 install` passes `k7_distro=k3s`;
  the rendered K3s containerd template is byte-identical to 0.4.0. The
  `drop-in` containerd mode and the `kubeadm`/`eks-al2023` adapters are
  unverified groundwork for the Helm path; `rke2` is verified by the Helm chart.
- **K3s is pinned** (`k3s_version`, default `v1.36.5+k3s1`) and passed as
  `INSTALL_K3S_VERSION`; override with `k7 install --k3s-version`.
- **Disk source** is one variable: `k7_disk_source=spare-disk|device:<path>|loopback`
  (`--disk` maps to `device:<path>`; legacy `k7_devmapper_disk` still works;
  `loopback` with kfd fails).
- **The containerd socket is no longer hardcoded** in core, the API or the
  manifests: k7-api/k7-agent get `K7_CONTAINERD_SOCKET` from the adapter,
  and host-side core (`k7 --core`) reads `/etc/k7/containerd_socket`.
- **`k7 install` deploys `k7-api` by digest** from the `images.lock.yaml`
  packaged in the release, with `imagePullPolicy: IfNotPresent`. No
  checkout, no host Docker. From a source checkout pass
  `--api-image local` to build `k7-api:local` as before (unsigned; the
  playbook then installs a pinned Docker on the first master). There is
  no fallback from one to the other.
- **Every install download** (the K3s install script, CNI, Firecracker,
  Kata, k7d, Cilium CLI, Hubble, Longhorn) is checked against a pinned
  sha256. `--k7d-version` for a
  version without a pinned checksum needs `-e k7d_sha256=<hex>`.
- Caddy is pulled by digest by containerd instead of through Docker.
- **The `k7d` group is gid 770 on every node**, created by node prep
  before k7d's `install.sh`, so the `k7-agent` DaemonSet carries one
  static gid, including on nodes the k7-node-installer prepares. Node
  prep fails loud on a node whose `k7d` group already has another gid
  (an earlier install allocated one): migrate it as the error says, or
  reinstall the node.
- The playbook's `apt dist-upgrade` holds the `k7` package, so it no
  longer swaps a local `.deb` (or the CLI running the play) for the PPA
  build. An existing operator hold is kept.
- `utils/bootstrap.sh` installs the playbook's pinned, checksummed
  Docker instead of `get.docker.com` (Ubuntu 24.04 only) and restarts
  `docker.socket` with `docker.service`, which came up failed on a fresh
  node. The playbook enables that socket and restarts `docker.service`
  once (restarting the socket as well starts dockerd a second time).

### Fixed

- The RKE2 Helm install (`deploy/helm/k7/ci/rke2-cilium-values.yaml`,
  used by `rke2-node.sh chart`) sets `api.mode` and
  `observability.scraperSelector`. The chart still has no default for
  `api.mode`, metrics stay on, and `helm install` fails if either value
  is missing.

- The packaged CLI accepts a standalone `-c` (`k7 exec NAME -- sh -c '...'`,
  `ls -c`). The Nuitka onefile was exiting 2 with its self-execution
  guard before the CLI started.
- `POST /api/v1/sandboxes` returns 503 when the registry host does not
  resolve: that is the cluster's DNS failing, not a bad request (was 400).
- `POST /api/v1/sandboxes/{name}/snapshot` and `/pause` on a missing
  sandbox return 404, like fork (was 400).
- k7-node-installer no longer flushes the nftables ruleset, inserts `cni0`
  FORWARD rules or switches iptables to legacy on a running node: on RKE2
  the flush dropped Cilium's chains and cut pod traffic.
- Re-running `k7 install --api-image local` after Docker publishes a newer
  release no longer fails on `docker.service` (start limit hit): the pinned
  Docker packages are held, so the dist-upgrade leaves them alone.
- Re-running `k7 install --api-image local` within a minute no longer fails
  on `docker.service` (start-limit-hit). The local-build path restarts
  Docker once per run. Restarting `docker.socket` as well started dockerd
  a second time, and the next install inside 60s exceeded docker-ce's
  StartLimitBurst of 3.
- `k7 snapshot create` waits for the root snapshot to be ready on every
  backend (it only waited with a Docker volume). Deleting a snapshot that was
  still being cut left `pvc-as-source-protection` on the PVC and pinned the
  namespace in Terminating.

- Re-running `k7 install` on a Cilium node no longer races Cilium's CNI
  config rename (`99-loopback.conf`) and fails.
- **`exec` reports the command's real exit code and stderr.** `exec_command`
  (CLI `k7 exec`, `POST /api/v1/sandboxes/{name}/exec`, SDK) always returned
  `exit_code: 0` with stderr folded into stdout, so `k7 exec` never failed
  and the `sync` / `fsfreeze` guards around Kata snapshots could not trip.
  It now reads the exit status from the Kubernetes exec status channel and
  keeps stdout and stderr apart. When there is no status (the exec could not
  start, or the stream closed before the command finished) `exit_code` is
  `-1`; `k7 exec` then exits 255.
- **Forks no longer come up with a corrupt disk.** `fork_sandbox` deleted its
  temporary snapshot as soon as the cloned PVC was Bound, but Longhorn copies
  the snapshot into the clone after binding. About one fork in four had its
  snapshot purged mid-copy; its ext4 then failed `fsck` and the fork never went
  Ready. The snapshot is now deleted only after Longhorn reports the clone
  `completed`, and a failed or stalled clone fails the fork loudly. k7-api
  gets `get` on `volumes.longhorn.io` for this.
- **Docker-enabled Kata snapshots and forks work again.** The freeze ran the
  sandbox's glibc `fsfreeze` inside the musl `docker:dind` vehicle, where it
  cannot start (no ELF loader). The exec exit-code fix above made that
  failure visible. The staged `fsfreeze` now carries its shared libraries and
  loader, and runs through them in the vehicle.
- **Re-running `k7 install --api-image local` rolls out the rebuilt image.**
  The rebuild kept the `k7-api:local` tag and `imagePullPolicy: Never`, so the
  k7-api Deployment and k7-agent DaemonSet specs did not change and the old
  pods kept running the previous build. The install now stamps the image ID
  on both pod templates.
- **`k7 --core` k7d VM ops on another node fail with a reason.** On Cilium the
  k7-agent only accepts k7-api and its own host, so a host-side call for a
  sandbox on another node timed out after 120 s and reported an empty error.
  It now fails after 10 s, naming the agent and pointing at k7-api.
- **Sandbox create no longer blocks up to 30 s on a scheduled pod.** The
  scheduling check waited for the pod to leave Pending, which a bound pod only
  does once its Longhorn volume attaches (15–30 s on a fresh node), so a create
  could exceed a 30 s client timeout. It now returns once the pod is bound.

- The playbook raises `fs.inotify.max_user_instances` to 8192
  (`/etc/sysctl.d/90-k7-inotify.conf`, applied at install). At the
  kernel's 128 per user, sandbox creation failed with "too many open
  files" once a node ran about 50 pods.

### Security

- **Only k7 sets runtime pod annotations.** A tenant or namespace admin
  could put `k7d.katakate.org/cluster-id` on a Deployment and its k7d VM
  left as the node, bypassing Cilium egress; other forwarded keys forked
  any VM on the node (`fork-source-*`, `tree-*`), or chose any host Kata
  config (`io.katacontainers.config_path`), or re-disabled guest seccomp.
  The ValidatingAdmissionPolicy `k7-runtime-annotations` (playbook and
  chart) now denies any change to `k7d.katakate.org/` and
  `io.katacontainers.` annotations on pods and pod templates, except by
  k7-api, `system:masters` and kube-controller-manager. Containerd now
  forwards only the keys k7 sets, and Kata enables only `default_memory`
  and the two rate limiters (no `kernel_params` or `virtio_fs_extra_args`).
- **OIDC JWKS cache expires.** Cached signing keys are trusted for
  `K7_OIDC_JWKS_TTL_SECONDS` (default 3600, minimum 10;
  `k7 install --oidc-jwks-ttl`, chart `auth.oidc.jwksTtlSeconds`). A
  failed refresh is 503 for the next 10 s as well, and an expired cache
  is not used. When the username claim is `email`, `email_verified` must
  be true.
- **No DNS exfiltration from FQDN-allowlist sandboxes.** The per-sandbox
  Cilium DNS rule now carries the allowlist's own matchers instead of
  `matchPattern: "*"`. Other names get NXDOMAIN from Cilium's DNS proxy
  and never reach CoreDNS or its upstream. The install sets
  `dnsProxy.dnsRejectResponseCode=nameError`, and `k7 install` sets it on
  existing clusters through `cilium upgrade`. The chart's preflight hook
  requires it on install and upgrade: set it on your Cilium (then restart
  the cilium DaemonSet) before `helm upgrade`. k7-api rewrites older
  sandbox policies at startup. Without
  Cilium, a CIDR egress entry containing the cluster DNS server is refused.
- **k7-node-installer admission policies.** The cleanup ServiceAccount
  may delete only K7 sandbox pods on its own node
  (`k7-node-cleanup-own-node-pods`, bound-token node name). Node
  updates by either installer ServiceAccount may no longer change
  annotations, finalizers or ownerReferences.

- **`POST /api/v1/install` drives the k7-node-installer** and is off by
  default (404). The API never runs Ansible (its image has none): `k7
  install --api-remote-install` (chart: `api.remoteInstall`) deploys the
  installer DaemonSet in `k7-system`, configured from the first master's
  distro, backends and disk settings, with its image pinned at deploy
  time (`--node-installer-image local|name@sha256:`, default
  `images.lock.yaml`). A cluster-wide `admin` (key or OIDC) posts exactly `{"nodes": [...]}`; the
  API checks that the live DaemonSet runs the pinned image, records the
  operation, labels the nodes `k7.katakate.org/node-pool=true` and returns
  202 with an operation id. `GET /api/v1/install/{id}` reports each node
  `pending`/`running`/`succeeded`/`failed` (with the installer's own
  failure line). Any other body key (`playbook`, `image`, `inventory`,
  `config`) is a 400; an unknown node is 404; a K3s server node without
  `--node-installer-allow-server-restart` is 409; a drifted DaemonSet
  image is 500. The audit line carries `nodes` and `operation`. Up to
  0.4.0, any valid key could post playbook content. `k7_sdk.Client.install(nodes)`
  and `install_status(id)` replace the deprecated playbook form;
  `K7_API_IMAGE` is gone from the k7-api Deployment. Turning it off
  converges: `k7 install` without `--api-remote-install` removes the pool
  labels and deletes the whole installer stack (DaemonSet, ConfigMap,
  RBAC, admission policies, `k7-system` with the operation records)
  unless Helm owns it. The chart keeps its installer (core: `k7-agent`
  shares the pool); with `api.remoteInstall=false` Helm drops the API's
  Role and the pre-install/pre-upgrade hook removes the
  `k7.katakate.org/node-pool-added-by=remote-install` marker from the
  nodes the API added and deletes the records. Those nodes stay in the
  pool, so `k7-agent` (k7d pause/resume, storage) keeps serving them.
  Remote install is development-mode only: the chart (`api.mode=production`
  with `api.remoteInstall=true`), the playbook and `k7 install`
  (`--api-mode production --api-remote-install`) refuse the combination
  before deploying, instead of k7-api exiting at startup.
- **`core.install_node` no longer leaks temp files**: playbook, inventory
  and extra-vars live in one `TemporaryDirectory`, removed even when
  `ansible-playbook` is missing; extra-vars that cannot be written fail
  loud instead of being dropped.
- **Audit log.** `k7-api` writes one JSON line per mutating request
  (POST/PUT/PATCH/DELETE) and per 401/403 to stdout on the `k7.audit`
  logger: `ts, request_id, key_id, key_name, admin, method, route,
  namespace, sandbox, status, latency_ms, client_ip`. Tokens, request
  bodies and exec command text are never logged (exec logs the
  command's sha256 and length). A failure to emit returns 500. Every
  response carries `X-Request-ID`.
- **API keys move to a Kubernetes Secret; the API is HA.** Keys live in
  `kube-system/k7-api-keys` (one data entry per key id: name, sha256
  hash, created, expires, scopes, admin). The API serves auth from an
  in-memory snapshot re-read every 5 s and **never writes on
  authenticate**; expired keys are rejected, not purged; if the Secret
  cannot be read for 60 s, auth returns 503 (`key store stale`) instead
  of a stale allow. `last_used` is coarse (per replica, flushed every
  5 minutes into `k7-api-keys-usage`). New tokens are
  `k7_<id>_<secret>`; existing tokens keep working until they expire.
  `k7 install` runs `k7 api keys migrate`, which copies
  `/etc/k7/api_keys.json` into the Secret, verifies it and renames the
  file to `api_keys.json.migrated` (it fails loud if both exist and
  differ). `k7 generate-api-key` / `list-api-keys` / `revoke-api-key`
  now need kubectl on a node. `k7-api` runs **2 replicas** with a PDB
  and no first-master pin or `/etc/k7` hostPath (ACME TLS stays one
  replica on the first master). `k7 api enable` restores the replica
  count recorded by `k7 api disable` (or `--replicas N`). The
  `k7-api` ClusterRole no longer reads Secrets cluster-wide (create /
  delete only); the key Secrets are a namespaced Role.
  `K7_API_KEY_STORE=file` (read-only legacy file) remains for one
  minor.
- **`k7-agent` is no longer privileged** and has no `/dev` mount: all
  capabilities dropped, read-only root filesystem, `RuntimeDefault`
  seccomp; uid 0 plus the `k7d` group for socket access (the playbook
  fails loud if k7d nodes disagree on the `k7d` gid). kfd thin-pool stats
  (`k7 nodes storage`) are written by the new host timer
  `k7-storage-report.timer` to `/run/k7/storage.json` every 30 s; the
  agent returns 500 with the file age if it is missing or older than
  90 s. The image drops `lvm2`. `k7-api` runs as uid 1000 with no
  hostPath and always forwards k7d VM ops to `k7-agent`
  (`K7_K7D_REMOTE_ONLY=1`), including on the first master.

## [0.4.0] — 2026-09-19

Per-key node pins, k7d-fc pause/resume/exec, and HA-soak fixes. Playbook
pins k7d **0.7.0**. GitHub `.deb`, Launchpad PPA, and PyPI `k7-sdk` are
**0.4.0**.

### Added

- **Optional per-key node placement** for tenant isolation on the
  shared per-node k7d daemon. `k7 generate-api-key --node <node>`
  (repeatable) stores `"nodes"` on the key, analogous to `-n` for
  namespaces. Placement uses the Node's `kubernetes.io/hostname`
  label, not `spec.nodeName`. `k7 nodes dedicate NODE --tenant ID`
  writes `k7.katakate.org/tenant` as a label and a NoSchedule taint so
  other sandboxes cannot share that daemon. `k7 nodes list` shows the
  K3s Node names (Linux hostname / `kubernetes.io/hostname`) to pass
  to `--node`; inventory.ini does not write that label — `k7_backends`
  only stamps backend labels at install. A single-node key auto-pins
  creates/restores; a multi-node key requires an explicit `node_name`
  / `k7 create --node`. Forks must already sit on an allowed node.
  `GET /api/v1/nodes/storage` is 403 for a node-scoped key. Unscoped
  keys keep unrestricted placement. Root on `[k7_servers]` is still
  cluster-admin; pins do not contain a host compromise — tenant
  sandboxes belong on `[k7_agents]`. `k7-agent` has no kube SA token
  and no `remote-node` ingress, so a dedicated worker breakout is that
  tenant, not cluster-admin.
- **k7d-fc resume → exec**: pin Firecracker v1.16.2; pause/resume/exec
  integration row and filled `PERFORMANCE.md` lifecycle table.

### Fixed

- **`k7 api status` / `k7 api endpoint` from a laptop** no longer crash with
  `FileNotFoundError: kubectl`. With `k7 config set api.url` they print the
  configured URL and probe `GET /health`. `k7 api enable`/`disable` still
  need kubectl on a cluster node and now say so instead of tracebacking.
- **Kata memory below 256Mi** is rejected at create time instead of waiting
  300s for `FailedCreatePodSandBox` (docs `k7.yaml` used `128Mi`).
- **Create success text** after `--expose-port` points at `k7 list`, not a
  nonexistent `k7 list --name`.
- **Kata kql `--docker` live fork / snapshot** now `fsfreeze`s
  `/var/lib/docker` in the docker-vehicle around VolumeSnapshot create
  (thaw in `finally`). alpine dind has no `fsfreeze`; it is staged from
  the sandbox image via the shared `/tmp`. `sync` alone left overlay2
  crash-inconsistent on Longhorn r=3.
- **`k7 delete` does not wait for k7d VM teardown.** The docker-graph
  leftover assertion polls up to 60s instead of a 3s sleep.
- **Loud kfd fork reject**, cluster CA snippet in the SDK/docs path, and
  `K7Core` aiohttp session close on teardown.

### Changed

- **`k7 install --backend` is required.** There is no default runtime
  list (kfd needs a spare disk; installing all backends grows attack
  surface). Inventory hosts must set `k7_backends` (group vars are
  fine). `none` is the only empty set — scheduling-only masters use
  `[k7_servers:vars] k7_backends=none`. Empty/omitted is an error, not
  kfd.
- Playbook default `k7d_version` is **0.7.0** (was 0.6.0). README,
  Hetzner tutorial, and inventory examples target k7 **0.4.0** on
  GitHub, PPA, and PyPI.

## [0.3.1] — 2026-09-14

HA install and API-path `--docker` fixes for the 0.3.0 line. README
product name is **k7** (Katakate is the org).

### Fixed

- **`k7 install` copies Firecracker pins from the repo root**, so a
  3-node HA all-backends install no longer looks next to the tempfile
  playbook and fails to stage `k7d-fc/install-firecracker.sh` (#59).
- **API-path `--docker`** trusts a playbook-recorded k7d version
  `>= 0.6.0` instead of a host payload path the API container cannot
  see, so `--docker` works for CLI/API users (#59).
- **`k7 exec` takes one `sh -c` string.** Nested `sh -c` was joining
  wrong and writing a blank file, which made the README fork demo look
  like memory CoW was broken (#59).
- HA inventory example adds `ansible_ssh_common_args` so the first
  master can SSH peers on a first install (#59).

### Changed

- README and the Hetzner tutorial target **0.3.1** (GitHub `.deb`;
  Launchpad PPA is still 0.2.2 until this upload publishes). Product
  sentences say **k7**; Katakate stays on org URLs, email, and the
  deprecated PyPI shim. The PyPI badge links to `k7-sdk`.

## [0.3.0] — 2026-09-12

HTTPS-by-default for `k7-api`, cluster-wide Cilium isolation, first-class
`--docker` on Kata and k7d, and RuntimeClass `k7-fc`. The playbook now
pins k7d **0.6.0**.

### Changed

- **`k7 install` serves `k7-api` over HTTPS by default**.
  `/etc/k7/api_endpoint` is now `https://<first-master-ip>:31007`. A
  Caddy sidecar terminates TLS; the API container and its HTTP probes
  stay on `:8000`. The default cert is a playbook-minted cluster CA
  (`/etc/k7/tls/ca.crt`) — Let's Encrypt cannot issue for a bare IP.
  **Breaking for existing laptop clients:** `K7_API_URL=http://...` must
  switch to `https://` and, on the default track, install the CA
  (`k7 config set api.ca ./ca.crt`, or `--api-ca` / `K7_API_CA`).
  `verify=False` on an `https://` URL is an error. Optional
  `--api-hostname` (Let's Encrypt via Caddy, not Certbot) and
  `--api-tls-cert`/`--api-tls-key`. `--api-insecure-http` restores
  today's HTTP NodePort. Keys no longer travel in cleartext; there is
  still no rate limiting.

### Added

- **Cluster-wide sandbox platform isolation**. A deny-only
  `CiliumClusterwideNetworkPolicy` (`k7-sandbox-platform-deny`) stops every
  sandbox — in every egress mode, including `--egress-open` — from reaching
  the node it runs on, the other nodes, the Kubernetes API server, cloud
  metadata / link-local, and the `kube-system` + `longhorn-system` pods
  (CoreDNS excepted). Previously an `--egress-open` sandbox could dial the
  node's SSH/kubelet/k3s ports, `10.43.0.1:443`, `k7-api` and Longhorn.
  The policy sets `enableDefaultDeny: false`; on Cilium 1.19 an
  `egressDeny` section alone would otherwise switch the selected sandboxes
  to default-deny and break open egress. Cilium-only: `--cni flannel`
  clusters do not get this isolation (documented in `SECURITY.md`).
- **Opt-in sandbox ingress** — `--ingress-port` opens TCP ports on a
  sandbox and `--ingress-from` (`sandbox:<name>` / `namespace:<ns>` /
  `cidr:<cidr>`) scopes who may connect, as stock v1 `NetworkPolicy` rules
  (so it works on Flannel too). Default is unchanged: deny all ingress. An
  opened port with no source is reachable only from sandboxes in the same
  namespace; the internet needs `cidr:0.0.0.0/0` spelled out. `k7 create`
  warns that a `cidr:` source does not scope in-cluster peers on Cilium
  (an `ipBlock` peer is not evaluated for pod-to-pod traffic) — use
  `sandbox:` / `namespace:` for that.
- **`--expose-port`** — publishes a sandbox port outside the cluster through
  a `NodePort` Service with `externalTrafficPolicy: Local`, so the pod sees
  the real client IP instead of a SNAT'd one. Requires a matching
  `--ingress-port`, prints the resolved `http://<node-ip>:<nodeport>`,
  reports NodePorts in `k7 list`, and is removed by `k7 delete`.
- **`k7 install --api-allow-cidr`** (repeatable) — restricts the `k7-api`
  NodePort to the operator's source CIDRs via a `CiliumNetworkPolicy` on
  the API pod (`k7-api-ingress`) plus `externalTrafficPolicy: Local` so the
  pod sees the real client IP. Off by default: without the flag no policy
  is created and the Service is untouched. Cilium-only (`--cni flannel`
  fails at the CLI — the reserved `host` / `remote-node` / `health` /
  `kube-apiserver` peers the kubelet probes need are not expressible in a
  v1 `NetworkPolicy`); invalid CIDRs fail before Ansible runs, and
  `0.0.0.0/0` warns. This is defence in depth for operators who know their
  client CIDRs, **not** a secure API: the NodePort is HTTPS by default (see
  Changed above) and there is still no rate limiting. The policy selects a
  pod endpoint, never the host, so a wrong CIDR never affects SSH or the
  k3s API port.
- **`k7 install --hubble`** — opt-in Cilium Hubble flow observability
  (relay + pinned `hubble` CLI). Off by default; not a security control.
  Combined with `--cni flannel` it fails loudly. Hubble UI is not
  installed (no auth, public node IP).
- **`--docker`** — first-class Docker as a guest service. On `k7d` and
  `k7d-fc` the guest agent supervises a pinned `dockerd` with overlay2
  on a per-sandbox virtio-blk disk; forks stay overlay2. On Kata (`kfd`
  / `kql`) the same flag injects a privileged docker-vehicle with
  overlay2 on a block disk (`kql` persists/forks the graph; `kfd` is
  ephemeral). `--sidecar docker` remains a deprecated alias.
- **RuntimeClass `k7-fc`** (`--backend k7d-fc`) — Firecracker under the
  jailer next to the k7d daemon, CRI exec, and overlay2 on forked
  `--docker` graphs.
- Kata guest seccomp is on (`disable_guest_seccomp = false`).
- Playbook default `k7d_version` is **0.6.0** (was 0.5.0). `--docker` and
  `k7-fc` need that tarball; an older k7d fails loudly.

### Fixed

- **API sandbox delete returned 400 on every call** because
  `k7 delete` always removes the `{name}-expose` Service and the
  `k7-api` ClusterRole had no `services` verbs. Kubernetes answers
  403 (not 404) when the verb is missing, so even sandboxes that
  never used `--expose-port` failed to delete through the API.
- **`k7 snapshot gc` left orphan VolumeSnapshotContents behind**, so
  snapshot-lifecycle tests (and operator deletes) pinned namespaces in
  Terminating on `volumesnapshotcontent-bound-protection` /
  `pvc-as-source-protection`. GC now reaps contents whose
  VolumeSnapshot is already gone or deleting, then drops the PVC
  source-protection finalizer (merge-patch — strategic-merge leaves
  those CRD/PVC finalizers in place). Integration teardown calls that
  same sweep instead of a kubectl jsonpath that silently missed items.

- **`k7 install` treated a busy apiserver as "Cilium is missing"**. The
  DaemonSet check used `failed_when: false` and `rc != 0` as absent, so a
  brief `unable to handle the request` started `cilium install` on a
  cluster that already had Cilium. Presence is now present / absent /
  unknown: unknown retries, then fails loud, and never installs. The
  Hubble relay check got the same split (`cilium hubble enable`
  Helm-upgrades the agent — do not run it because kubectl blipped). A
  reinstall no longer fails `cilium status --wait` on warnings when the
  DaemonSet is Ready.
- **`GET /api/v1/nodes/storage` skipped namespace authorization**
  (CWE-862 / CWE-285). A leftover from the 0.2.1 scoping fix: this
  cluster-scoped route was guarded only by `verify_api_key`, so a
  namespace-scoped tenant key could read cluster-wide per-node storage
  topology (node names, pool utilization, per-agent error strings). The
  route now calls `authorize_namespace(..., all_namespaces=True)`;
  scoped keys get 403, unscoped keys are unchanged. Reported privately
  by **Ahmed Ibrahim** ([@skeletonsec](https://github.com/skeletonsec)),
  who held disclosure — thank you.
- `k7 install --cni flannel` failed at "Apply K7 API manifests" because the
  directory contained a `CiliumNetworkPolicy` and no `cilium.io` CRD
  exists on Flannel. Cilium-only manifests now live in
  `manifests/k7-api/cilium/` and are applied by a separate task gated on
  the CNI.
- **`k7 fork` produced a sandbox with no network policy at all** — neither
  the deny-ingress `NetworkPolicy` nor the egress policy was created, so a
  fork of a locked-down sandbox came up with unrestricted egress. Forks now
  inherit the source's egress configuration and are rolled back if their
  policy cannot be created.

## [0.2.2] — 2026-08-18

Docs and install-path release for a two-node `apt` install that ships
k7d 0.2.1, plus TWO_DISK documentation.

### Changed

- Two-node inventory / install docs and playbook extras so a Show HN
  reader can `apt install` k7 and bring up k7d 0.2.1 without guessing
  disk layout (`TWO_DISK`).

## [0.2.1] — 2026-08-15

Security release. Everyone running the `k7-api` control plane on 0.2.0 or
earlier should upgrade. Both issues were reported privately by
**Jirayu Thongchotchaung** ([@JirayuThongchotchaung](https://github.com/JirayuThongchotchaung)),
who held disclosure until this release was available — thank you.

### Fixed

- **Server-side request forgery via the sandbox `image` registry host**
  (CWE-918). A sandbox creation request could name a registry host that
  resolves to a loopback, link-local, or private address and make the
  control plane issue the OCI fetch on the caller's behalf. Registry hosts
  are now resolved and checked against public/allowlisted ranges *before*
  any fetch, the `localhost` → plain-HTTP downgrade is gone, and redirects
  are disabled so an allowlisted host cannot bounce the request inward.
  Resolution runs off the event loop, so the check cannot stall the API.

### Added

- **Optional per-key namespace authorization** (CWE-862 / CWE-285).
  API keys can be scoped to one or more namespaces with
  `k7 generate-api-key -n <namespace>`, enforced on every namespace-bearing
  endpoint; a scoped key cannot read or mutate another namespace and cannot
  perform all-namespaces operations. Keys without a scope keep their
  previous unrestricted behaviour, so this is backward compatible — scope
  your keys to benefit from it.

### Changed

- `SECURITY.md` states the supported release line accurately.
- Debian packaging targets `amd64` explicitly and no longer runs the test
  suite inside build chroots, which is what the Launchpad PPA needs.

## [0.2.0] — 2026-08-11

First public release. Ships the CLI/API deb and PyPI `k7-sdk`.
The API image is built on the node by the install playbook; a prebuilt GHCR
image and the apt/PPA story are fast follow-ups.

### Added

- **Multiple sandbox backends** on one install / cluster — pick per sandbox
  or specialize nodes:
  - `kata-firecracker-devmapper` (`kfd`) — Firecracker + jailer + LVM
    thin-pool
  - `kata-qemu-longhorn` (`kql`) — **QEMU** via Kata + Longhorn PVC root
    (named snapshots, restore, disk-only fork)
  - `k7d` — Katakate Rust VMM / `runtimeClassName: k7` (warm CoW fork;
    install via artifact URL / `--k7d-artifact` until the public
    `Katakate/k7d` release is live)
- Multi-node / HA install (Ansible inventory, Longhorn topology)
- Cilium CNI with FQDN egress (`CiliumNetworkPolicy`)
- API + SDK parity for pause / resume / fork
- Snapshot lifecycle + GC CronJob; restore from VolumeSnapshot (`kql`)
- CLI talks to the API by default (`k7 api`, `k7 dev api rebuild`)
- Docker-in-VM sidecar + performance bench harness
- Firecracker jailer integration
- Python SDK published as **`k7-sdk`** (`k7_sdk`; `katakate` deprecated)

### Changed

- Node-local ops removed from `K7Core` (API/agent split)

## [0.0.3]

- Debian package packaging fixes (GHCR image name casing)

## [0.0.1]

- Initial tagged release
