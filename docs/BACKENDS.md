# Sandbox backends: kfd, kql, k7d

k7 turns every sandbox into a Kubernetes Deployment whose pod runs inside a
hardware-isolated microVM. *How* that VM is built, stored, snapshotted, and
forked is the backend's job. Three backends exist today; a node can install
any combination (`k7 install --backend kfd,kql,k7d`) and each sandbox picks
one (`k7 create --backend …`, annotation `k7.katakate.org/backend`).

| | `kfd` — kata-firecracker-devmapper | `kql` — kata-qemu-longhorn | `k7d` |
|---|---|---|---|
| VMM | Firecracker via Kata Containers | QEMU via Kata Containers | [k7d](https://github.com/katakate/k7d), Katakate's purpose-built KVM VMM |
| containerd RuntimeClass | `kata` | `kata-qemu` | `k7` |
| Rootfs / storage | devmapper LVM thin-pool (needs a spare raw disk) | overlayfs + Longhorn PVC mounted at `/mnt/state` | cached erofs images on virtio-blk, guest tmpfs upper, reflink-XFS volume images |
| Named snapshots (`k7 snapshot`) | ✗ | ✅ Longhorn VolumeSnapshot (disk-only, crash-consistent) | ✗ by design — see below |
| `k7 fork` | ✗ | disk clone: snapshot → new PVC → cold boot | **warm fork: CoW disk *and* memory of the live VM** |
| `k7 pause` / `resume` | scale to 0/1 (state lost) | scale to 0/1, disk persists on the PVC | **VM frozen in place: vCPUs stop, memory stays** |
| `k7 restore` | ✗ | ✅ boot a new sandbox from any named snapshot | ✗ (fork the live sandbox instead) |
| Docker-in-VM sidecar | ✅ (emptyDir data) | ✅ (PVC-persistent data) | ✅ (VM-lifetime data) |

Measured numbers for all of this live in [PERFORMANCE.md](../PERFORMANCE.md):
on the same node, k7d creates in ~2.1s vs kql's ~17.1s, forks to a usable
sandbox in ~2.4s vs ~46.7s (and the k7d fork inherits the source's memory),
and pauses/resumes in ~0.2s/~0.3s vs ~1.3s/~4.1s — while kql keeps named
persistent snapshots and the faster in-sidecar `docker pull`.

## Which backend when?

- **kfd** — smallest attack surface and fast boots, when you don't need any
  snapshot/fork lifecycle. Requires a dedicated raw disk for the thin-pool.
- **kql** — durable sandboxes. The root disk is a replicated Longhorn volume:
  it survives pod restarts and node reboots, can be snapshotted by name,
  restored into brand-new sandboxes, and forked (at the cost of a full VM
  boot on the cloned disk).
- **k7d** — ephemeral-but-forkable compute, e.g. agent/RL rollouts. The whole
  VM (filesystem *and* RAM: running processes, page cache, sockets) can be
  forked in seconds, and pause/resume is instantaneous because nothing is
  torn down. State does not survive pod deletion — persistence is "fork it
  again", not "write it to a disk".

## The k7d backend

**Architecture:** Linux **amd64 / x86_64** only (Debian `amd64` ≡ tarball
`x86_64`). Unlike `kfd` / `kql`, there is no arm64 build yet.

### What k7 installs (`k7 install --backend k7d`)

The Ansible playbook:

1. checks `/dev/kvm` exists (loud failure otherwise) and loads
   `vhost_vsock` + `tun`;
2. installs `erofs-utils`, `xfsprogs`, `virtiofsd`, and provisions a sparse
   reflink-capable XFS image loop-mounted at `/var/lib/k7d/disks` (warm forks
   clone writable volume images with `FICLONE` reflinks);
3. downloads the k7d release tarball
   (`k7d_artifact_url`, default the public `Katakate/k7d` GitHub release
   for `k7d_version`, currently **0.2.1**) and runs the bundled
   `install.sh`, which installs `k7d` + `containerd-shim-k7-v1` into
   `/usr/local/bin`, guest kernel/initramfs into `/usr/local/share/k7d`,
   and starts `k7d.service` (control socket `/run/k7d/k7d.sock`).
   Override with `k7 install --k7d-version <ver>` (same GitHub URL, other
   tag) or `--k7d-artifact <path>` / `k7d_artifact_local_path` (local
   tarball). The PPA `k7` 0.2.1 playbook still defaults to k7d 0.1.0 —
   pass `--k7d-version 0.2.1` on that package until the next k7 cut.
4. registers the `k7` runtime in the k3s containerd template **with**
   `pod_annotations = ["k7d.katakate.org/*"]` and **without** a `BinaryName`
   option (containerd resolves the shim from
   `runtime_type = "io.containerd.k7.v1"`), restarts k3s, and creates
   RuntimeClass `k7`;
5. labels the node `k7.katakate.org/backend-k7d=true`.

### How a sandbox maps to a VM

One pod = one k7d microVM. The pod's containers (sandbox + optional docker
sidecar) all run as runc containers *inside* that single VM — multi-container
pods share the guest kernel, network namespace, and tmpfs. VM size follows the
pod's CPU/memory limits (the shim reads the CRI sandbox annotations, so
`k7 create --cpu 2 --memory 1Gi` gives a 2-vCPU/1 GiB VM with a host-side CFS
cap).

### The fork story

`k7 fork src dst` on a k7d sandbox does **not** copy a disk. Instead the new
Deployment's pod carries two annotations:

```yaml
k7d.katakate.org/fork-source-cluster: <source CRI sandbox id>
k7d.katakate.org/fork-source-vm:      <source CRI sandbox id>
```

The pod's containerd shim resolves the source VM through the k7d daemon and
issues a `fork_vm`: the daemon briefly pauses the source, captures dirty
pages, and builds the child from a `MAP_PRIVATE` CoW mapping of the source's
memory plus reflink clones of its disk overlays. The child inherits
*everything* — files, tmpfs, running processes, page cache — and diverges
independently from that point. The source keeps running (its vsock identity
is preserved). The fork pod then *adopts* the workload container already
running inside the forked guest, so `kubectl exec` / `k7 exec` transparently
target the inherited state.

Properties and limits (fail loudly, never silently):

- Warm fork works for **single-workload sandboxes**; forking a sandbox with a
  sidecar is rejected (no reliable container mapping — use kql for that).
- The fork is pinned to the source's node (the k7d daemon is node-local;
  cross-node fork is k7d's M12 roadmap item).
- If the fork pod is ever restarted by Kubernetes it re-forks from the (then
  current) source — a fork is a live branch, not a stored artifact.
- `k7 snapshot`/`k7 restore` are rejected on k7d: named, storable snapshots
  are a Longhorn/kql feature. k7d has its own richer VM snapshot **trees**
  (fork/rollback/suspend of whole VM states, including multi-VM clusters)
  driven through the k7d daemon API — see the
  [k7d project](https://github.com/katakate/k7d). Whole-cluster fork
  (forking an inner k8s cluster of VMs as one unit) is deliberately a
  k7d-level feature, not a k7 verb.

### k7d VM ops work on any node (per-node k7-agent)

k7d pause/resume/fork need three things that only exist **on the node
hosting the sandbox**: the k7d daemon socket (`/run/k7d/k7d.sock`), the k3s
containerd socket, and `crictl`. Since spec 18g this is handled by the
`k7-agent` DaemonSet (kube-system, same `k7-api:local` image running
`k7.api.agent:app`), so VM ops work **through the API for a sandbox on any
node**:

- A VM op on a sandbox co-located with the k7-api pod runs directly (the
  deployment mounts both sockets and ships `crictl`).
- A VM op on a sandbox on any OTHER node is forwarded to the k7-agent pod
  on that node (`POST /agent/v1/vm/{pause,resume,fork,lookup}` on the pod
  IP). Forwarding authenticates with the shared token the install playbook
  writes to `/etc/k7/agent_token` (root, 0600) on every node; a
  CiliumNetworkPolicy limits pod-originated agent ingress to the k7-api
  pod. No Ready agent on the node / missing token → loud error, never a
  silent no-op.
- **CLI on a node** also works for any sandbox: local sandboxes talk to the
  local daemon, remote ones are forwarded the same way (root can read the
  token).
- A fork still **lands on the source's node** — the agent proxies to the
  node-local daemon; the cross-node fork *data path* is k7d spec 9a M12
  (daemon side, not built yet). When it lands, only the forwarding target
  changes.

kql pause/resume/fork have none of these constraints (they are pure
Kubernetes/Longhorn operations) and work through the API for any node.

### Pause / resume

`k7 pause` on k7d asks the daemon (over `/run/k7d/k7d.sock`) to stop the VM's
vCPU threads and park its device workers: RAM, devices, and the vsock CID all
stay. The pod object remains scheduled (annotated
`k7.katakate.org/k7d-paused=true`), so `k7 resume` is just "restart the vCPU
loop" — sub-second, and every in-memory byte survives. Compare kql, where
pause scales the Deployment to zero (the VM is destroyed; only the Longhorn
disk survives) and resume pays a full VM boot.

## Egress modes

A sandbox has exactly one of three egress modes (spec 18f issue 4):

| Mode | CLI | YAML / API | Result |
|---|---|---|---|
| **block-all** (CLI default) | `k7 create …` (no egress flag) | `egress_whitelist: []` | deny-all egress NetworkPolicy |
| **whitelist** | `--egress <entry>` (repeatable) | `egress_whitelist: [<entries>]` | only listed CIDRs/domains |
| **open** | `--egress-open` | omit `egress_whitelist` (or `null`) | no egress policy at all |

`--egress-open` and `--egress` are mutually exclusive. Note the asymmetric
defaults, kept for backward compatibility: the CLI without flags is
**block-all**, while an API/YAML request that omits `egress_whitelist`
entirely is **open**.

Wildcard semantics: `--egress '*.docker.com'` covers subdomains at **any
depth** (`registry.docker.com` *and* `production.cloudfront.docker.com`) but
not the apex `docker.com` itself — add it as its own entry. Under the hood
k7 translates a leading `*.` into Cilium's multi-label `**.` matchPattern; a
bare Cilium `*` never crosses label boundaries, which used to silently break
CDN-backed registries (spec 18f issue 3). The install also sets Cilium
`dnsProxy.minTtl=3600` so clients that cache a resolved IP longer than the
CDN's 30–60s DNS TTL (dockerd's blob downloader does) keep their learned
FQDN→IP allowance for an hour.

## Disk pool sizing (kfd + k7d)

Both node-local storage pools have fixed sizes chosen at install time
(spec 18f issue 5) — set them per node in the inventory:

| Pool | Backend | Default | Inventory knob | Utilization |
|---|---|---|---|---|
| `kata-vg/thin-pool` (LVM, on the spare disk) | kfd | 100G PV | `kata_thinpool_pv_size` | `lvs kata-vg` (`Data%`/`Meta%`) |
| `/var/lib/k7d/disks` (sparse XFS loopback) | k7d | 32G image | `k7d_disks_image_size` | `df -h /var/lib/k7d/disks` |

Failure modes when a pool fills — both are **invisible to kubelet** (no
disk-pressure eviction, the pools are not part of the root filesystem):

- **kfd thin-pool full**: LVM autoextend (`thin_pool_autoextend_threshold=80`)
  grows the pool within the PV; once the PV itself is exhausted writes inside
  sandboxes start failing with I/O errors and new kfd pods fail to create
  their devmapper snapshots (`CreateContainerError`). Only the first
  `kata_thinpool_pv_size` of the spare disk is used — size it generously.
- **k7d pool full**: writable volume images and fork reflink clones fail;
  the k7d daemon rejects new sandboxes/forks loudly (`no space left on
  device`). The image is sparse, so `ls -l` shows the virtual size —
  use `du`/`df` for actual usage.

Utilization for both pools on every node is surfaced through the API
(spec 18g) and CLI/SDK (spec 18h): `GET /api/v1/nodes/storage`,
`k7 nodes storage` (`--json` for raw), and `Client.nodes_storage()`
return a per-node map of `kata_thinpool` (`lvs` size/data%/metadata%)
and `k7d_disks` (`df` size/used/avail), collected from the k7-agent
DaemonSet. A node whose agent is unreachable gets a loud
`{"error": ...}` entry.

## Memory limits (`--memory`)

All three backends honour `k7 create --memory <qty>` (Kubernetes quantity
like `2Gi` / `4096Mi`):

| Backend | Mechanism |
|---|---|
| **kfd** | Kata stamps `io.katacontainers.config.hypervisor.default_memory` (MiB). containerd forwards `io.katacontainers.*` (`pod_annotations` on `runtimes.kata`) and `configuration-fc.toml` allowlists `default_memory` (spec 18h — before that the annotation was silently ignored and the VM stayed at the 2048 MiB default). |
| **kql** | Same annotation path via `runtimes.kata-qemu` + `configuration-qemu.toml` (spec 18g). |
| **k7d** | No Kata annotation — the k7d shim sizes the VM straight from the pod's CRI CPU/memory limits. |

## Known issues

- **kata-fc VMM leak under churn** (spec 18f issue 1b): kata 3.24.0 with the
  jailer records the `--daemonize`d jailer's PID, so its SIGTERM fallback at
  pod deletion signals a dead PID. When the graceful in-guest shutdown fails
  (dead agent under parallel churn), the firecracker process is orphaned and
  spins at 100% CPU. `k7 install` deploys a per-node systemd timer
  (`k7-vmm-reaper.timer`, 1-minute cadence) that kills VMM processes whose
  kata shim is gone, and the integration suite asserts zero orphans cluster
  wide after teardown (`tests/integration/test_zz_leaks.py`).
- **kql docker-in-VM wedge under heavy fsync + replicas ≥ 2 — FIXED**
  (spec 18f issue 8 / spec 18g): kata's default single-threaded virtiofsd
  serialized all virtio-fs IO; under a sustained fsync burst against an
  r≥2 Longhorn volume the kata-agent's health ping starved behind the IO
  convoy and the shim killed the healthy VM. `k7 install` now widens the
  virtiofsd thread pool (`--thread-pool-size=16`); see PERFORMANCE.md for
  the root-cause narrative and post-fix numbers.
