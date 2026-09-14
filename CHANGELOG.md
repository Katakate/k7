# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
