# Sandbox backends: kfd, kql, k7d, k7d-fc

k7 turns every sandbox into a Kubernetes Deployment whose pod runs inside a
hardware-isolated microVM. *How* that VM is built, stored, snapshotted, and
forked is the backend's job. Four backends exist today; a node can install
any combination (`k7 install --backend kfd,kql,k7d,k7d-fc`) and each sandbox picks
one (`k7 create --backend …`, annotation `k7.katakate.org/backend`).
`--backend` / inventory `k7_backends` is **required** — there is no default
list. `none` means no sandbox runtime on that node (scheduling-only
master: `[k7_servers:vars] k7_backends=none`). Empty/omitted is an error,
not kfd.

| | `kfd` — kata-firecracker-devmapper | `kql` — kata-qemu-longhorn | `k7d` | `k7d-fc` |
|---|---|---|---|---|
| VMM | Firecracker via Kata Containers | QEMU via Kata Containers | [k7d](https://github.com/katakate/k7d) in-process rust-vmm | Same k7d daemon; stock Firecracker under the stock jailer |
| containerd RuntimeClass | `kata` | `kata-qemu` | `k7` | `k7-fc` |
| Rootfs / storage | devmapper LVM thin-pool (needs a spare raw disk) | overlayfs + Longhorn PVC mounted at `/mnt/state` | cached erofs images on virtio-blk, guest tmpfs upper, reflink-XFS volume images | same as k7d (erofs/scratch virtio-blk; **no virtiofs / hostPath**) |
| Named snapshots (`k7 snapshot`) | ✗ | ✅ Longhorn VolumeSnapshot (disk-only, crash-consistent) | ✗ by design — see below | ✗ (same as k7d) |
| `k7 fork` | ✗ | disk clone: snapshot → new PVC → cold boot | **warm fork: CoW disk *and* memory of the live VM** | same daemon socket / `fork-source-*` annotations |
| `k7 pause` / `resume` | scale to 0/1 (state lost) | scale to 0/1, disk persists on the PVC | **VM frozen in place: vCPUs stop, memory stays** | same `/run/k7d/k7d.sock` path |
| `k7 restore` | ✗ | ✅ boot a new sandbox from any named snapshot | ✗ (fork the live sandbox instead) | ✗ |
| Docker | **`--docker`**: privileged `docker-vehicle`, overlay2 on ephemeral `k7-docker-lvm` block LV | **`--docker`**: privileged `docker-vehicle`, overlay2 on Longhorn Block PVC (pause/fork/restore) | **`--docker`**: agent-supervised dockerd | **`--docker`** (same guest service) |
| hostPath | Kata virtiofs | Kata virtiofs | k7d virtiofs fallback | **refused up front** (Firecracker has no virtiofs) |

Measured numbers for all of this live in [PERFORMANCE.md](../PERFORMANCE.md):
on the same node, k7d creates in ~2.1s vs kql's ~17.1s, forks to a usable
sandbox in ~2.4s vs ~46.7s (and the k7d fork inherits the source's memory),
and pauses/resumes in ~0.2s/~0.3s vs ~1.3s/~4.1s — while kql keeps named
persistent snapshots and overlay2 Docker on a Longhorn block volume.

`k7 create --docker` is one user contract on every backend (`docker` /
`compose` / `buildx` on PATH, `overlay2` on a **block** disk, socket shared
as a directory). k7d/k7d-fc run dockerd as a guest agent service (no extra
CRI container). Kata (kfd/kql) injects one privileged **docker-vehicle**
container in the same VM; the sandbox container's security context is
unchanged. The graph is never virtio-fs / emptyDir. kql's graph is a
second Longhorn Block PVC (`<name>-docker-lh`) included in pause / fork /
restore (two VolumeSnapshots are crash-consistent per volume, not
cross-volume atomic). kfd's graph is a generic ephemeral volume from
StorageClass `k7-docker-lvm` (OpenEBS LVM LocalPV over `kata-vg/thin-pool`)
and is deleted with the pod; `k7 fork` of a kfd `--docker` sandbox is
rejected. `--sidecar docker` is a deprecated alias of `--docker`. Inside
one Kata VM the vehicle and the sandbox are a single trust domain; the
isolation boundary is the VM. No hostPath is used for the payload or the
CLI.

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
- **k7d-fc** (`k7-fc`) — pick this when you want k7d's fork/pause daemon
  **and** Firecracker's jailer (separate VMM process, per-VM uid/chroot/seccomp).
  You lose virtiofs/hostPath and time-warp. See k7d
  `SECURITY.md` "Firecracker profile" and `docs/backends.md`. Kata's
  Firecracker (`kfd`) is a different binary (`/opt/kata/bin`, older pin);
  k7d-fc installs upstream v1.16.2 at `/usr/local/bin`.

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
3. downloads the k7d release tarball (`k7d_artifact_url`, default the
   public `Katakate/k7d` GitHub release for `k7d_version`, currently
   **0.7.0**) and runs the bundled `install.sh`, which installs `k7d` +
   `containerd-shim-k7-v1` into `/usr/local/bin`, guest kernel/initramfs
   into `/usr/local/share/k7d`, and starts `k7d.service` (control socket
   `/run/k7d/k7d.sock`). Override with `k7 install --k7d-version <ver>`
   (same GitHub URL, other tag) or `--k7d-artifact <path>` /
   `k7d_artifact_local_path` (local tarball). `--docker` needs a k7d that
   ships the guest docker service (payload at
   `/usr/local/share/k7d/docker` or `/root/k7d/guest/docker/payload`); an
   older k7d fails loudly.
4. registers the `k7` runtime in the k3s containerd template **with**
   `pod_annotations = ["k7d.katakate.org/*"]` and **without** a `BinaryName`
   option (containerd resolves the shim from
   `runtime_type = "io.containerd.k7.v1"`), restarts k3s, and creates
   RuntimeClass `k7`;
5. labels the node `k7.katakate.org/backend-k7d=true`.

### What k7 installs (`k7 install --backend k7d-fc`)

Same daemon/shim as `k7d`, plus:

1. pinned Firecracker + jailer via `src/k7/deploy/k7d-fc/install-firecracker.sh`
   (pins from `pins.env`; sha mismatch fails the playbook);
2. `/etc/k7d/shim-k7-fc.toml` (`backend = "firecracker"`) and containerd
   `runtimes.k7-fc` with `ConfigPath` and **no** `BinaryName`;
3. RuntimeClass `k7-fc` with PodOverhead `32Mi` / `50m`;
4. node label `k7.katakate.org/backend-k7d-fc=true` (and `backend-k7d`
   so native k7 pods still schedule).

Do not point `--k7d-artifact` at an older GitHub tarball after developing
the shim — 0.5.0 does not know ConfigPath. The playbook
renders the whole containerd template from the selected backends; on a
node that already has other runtimes (kata, nvidia, wasm, …) use
`--tags k7d-fc` (Firecracker install + RuntimeClass + labels) rather
than a full `k7 install` that would drop those blocks.

### How a sandbox maps to a VM

One pod = one k7d microVM. The pod's sandbox container runs as a runc
container *inside* that VM. ``k7 create --docker`` does **not** add a
second CRI container: the guest agent (PID 1) supervises a pinned
`dockerd` with its graph on a per-sandbox virtio-blk scratch disk.
Inner containers share the guest kernel, network namespace, and the
exported sandbox paths (`/home`, `/root`, `/tmp`, `/opt`, `/workspace`).
VM size follows the pod's CPU/memory limits.

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

- Warm fork works for **single-workload sandboxes**, including
  `--docker` (dockerd is not a CRI sidecar). Forking a sandbox with a
  **real CRI sidecar** is rejected (no reliable container mapping — use
  kql for that).
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
containerd socket, and `crictl`. This is handled by the `k7-agent`
DaemonSet (kube-system, same `k7-api:local` image running
`k7.api.agent:app`), so VM ops work **through the API for a sandbox on any
node**:

- A VM op on a sandbox co-located with the k7-api pod runs directly (the
  deployment mounts both sockets and ships `crictl`).
- A VM op on a sandbox on any OTHER node is forwarded to the k7-agent pod
  on that node (`POST /agent/v1/vm/{pause,resume,lookup}` on the pod
  IP). The agent does **not** create Deployments or patch nodes
  (no ServiceAccount token). Fork Kubernetes writes stay on `k7-api`.
  Forwarding authenticates with the token the install playbook
  writes to `/etc/k7/agent_token` (root, 0600) on every node; a
  CiliumNetworkPolicy allows agent ingress from the k7-api pod and
  local `host` only (`remote-node` denied). No Ready agent / missing
  token → loud error, never a silent no-op.
- **CLI on a node** talks to the local daemon for local sandboxes.
  Remote VM ops go through `k7-api` (default CLI path). `k7 --core`
  cannot host-forward to another node's agent.
- A fork still **lands on the source's node** (`spec.nodeName` on the
  fork Deployment). k7-api creates that Deployment after an agent
  lookup; CoW is node-local on that k7d daemon. Cross-node fork *data
  path* is future k7d work (daemon side, not built yet).

k7d is one daemon per node. Namespace scoping on API keys does not stop
two tenants sharing that daemon. Pin a tenant with
`k7 generate-api-key tenant-a -n tenant-a --node <node>` so creates
cannot *choose* another node. `<node>` is the Kubernetes Node name
(`k7 nodes list` / `kubectl get nodes`) — the Linux hostname K3s
registered. The pin is a `nodeSelector` on `kubernetes.io/hostname`
(kubelet stamps that label; `inventory.ini` does not). Inventory
`k7_backends` only stamps `k7.katakate.org/backend-*` at install.
To keep **other** sandboxes off that node, also run
`k7 nodes dedicate <node> --tenant <id>` (label + NoSchedule taint
`k7.katakate.org/tenant`). See `SECURITY.md`.

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

A sandbox has exactly one of three egress modes:

| Mode | CLI | YAML / API | Result |
|---|---|---|---|
| **block-all** (CLI default) | `k7 create …` (no egress flag) | `egress_whitelist: []` | deny-all egress NetworkPolicy |
| **whitelist** | `--egress <entry>` (repeatable) | `egress_whitelist: [<entries>]` | only listed CIDRs/domains |
| **open** | `--egress-open` | omit `egress_whitelist` (or `null`) | no per-sandbox egress policy; internet reachable, platform is not (see below) |

In every mode, on a Cilium cluster, the cluster-wide
`k7-sandbox-platform-deny` policy additionally denies sandbox egress to
the node, the other nodes, the Kubernetes API server, link-local / cloud
metadata, and the `kube-system` + `longhorn-system` pods except CoreDNS.
It is deny-only, so it never narrows what a whitelist allows beyond those
targets. `--cni flannel` clusters do **not** get this protection.

`--egress-open` and `--egress` are mutually exclusive. Note the asymmetric
defaults, kept for backward compatibility: the CLI without flags is
**block-all**, while an API/YAML request that omits `egress_whitelist`
entirely is **open**.

Wildcard semantics: `--egress '*.docker.com'` covers subdomains at **any
depth** (`registry.docker.com` *and* `production.cloudfront.docker.com`) but
not the apex `docker.com` itself — add it as its own entry. Under the hood
k7 translates a leading `*.` into Cilium's multi-label `**.` matchPattern; a
bare Cilium `*` never crosses label boundaries, which used to silently break
CDN-backed registries. The install also sets Cilium `dnsProxy.minTtl=3600`
so clients that cache a resolved IP longer than the CDN's 30–60s DNS TTL
(dockerd's blob downloader does) keep their learned FQDN→IP allowance for an
hour.

## Ingress

Ingress is denied by default and opt-in per sandbox. A sandbox with no
ingress flags gets the same deny-all `{name}-deny-ingress` NetworkPolicy
k7 has always created; `--ingress-port` adds allow rules to that same
object.

| Setting | CLI | YAML / API | Result |
|---|---|---|---|
| **deny-all** (default) | `k7 create …` (no ingress flag) | omit `ingress_ports` | deny-all ingress NetworkPolicy |
| **open TCP ports** | `--ingress-port <port>` (repeatable) | `ingress_ports: [<ports>]` | listed TCP ports reachable from sandboxes in the same namespace |
| **scope the sources** | `--ingress-from <source>` (repeatable) | `ingress_from: [<sources>]` | only the listed sources reach those ports |

Sources:

| Entry | Matches |
|---|---|
| `sandbox:<name>` | that sandbox, same namespace (`podSelector` on `katakate.org/sandbox`) |
| `namespace:<ns>` | every pod in that namespace (`namespaceSelector` on `kubernetes.io/metadata.name`) |
| `cidr:<cidr>` | clients in that CIDR (`ipBlock`) |

Anything else is a hard error at create time — an unreadable source in a
security rule is never guessed at. `--ingress-from` without
`--ingress-port` is an error too: sources alone open nothing.

`--ingress-port` with no `--ingress-from` means "other sandboxes in this
namespace", never the world. Reaching a sandbox from anywhere requires
`--ingress-from cidr:0.0.0.0/0`, typed out in full.

These are stock v1 `NetworkPolicy` rules, which Cilium enforces natively,
so ingress works on `--cni flannel` as well.

**`cidr:` does not scope in-cluster peers on Cilium.** Cilium does not
evaluate an `ipBlock` peer for traffic where both ends are Cilium-managed
([Layer 3 policy docs][cilium-l3]), so a rule whose peers are all `cidr:`
degrades to a *port-only* allow inside the cluster: the port is reachable
from **any sandbox in the cluster**, even when the CIDR contains no cluster
IP. Measured on this cluster — a sandbox opened with
`--ingress-from cidr:198.51.100.0/24` answered an unrelated sandbox on the
opened port, while an unlisted port stayed closed. Adding a `sandbox:` peer
alongside the `cidr:` does not close it.

`k7 create` warns on stderr whenever `--ingress-from` contains a `cidr:`
entry. Use `cidr:` for **external** clients (that is what `--expose-port`
needs it for) and `sandbox:` / `namespace:` to scope in-cluster access —
those are evaluated by pod identity and were verified to deny precisely.

Two further reasons not to reach for `cidr:` in-cluster: pod IPs are
reassigned on every restart, so a rule written against one silently stops
matching; and the NetworkPolicy spec leaves `ipBlock`-vs-pod-traffic up to
the CNI, so such a rule is not portable between CNIs either.

[cilium-l3]: https://docs.cilium.io/en/stable/security/policy/layer3/

`kubectl exec` and `k7 shell` are unaffected either way: they go through
the Kubernetes API and the CRI, not the pod network.

A sandbox is a bare Deployment with no Service, so ingress rules only cover
in-cluster traffic. To reach a sandbox from outside the cluster, see
"Exposing a sandbox" below.

## Exposing a sandbox outside the cluster

`--expose-port <port>` (repeatable, `expose_ports` in YAML/API) creates a
`NodePort` Service named `{name}-expose` in the sandbox's namespace. `k7
create` prints the resolved `http://<node-ip>:<nodeport>` for each port, and
`k7 list` shows the allocated NodePorts.

- **Every `--expose-port` needs a matching `--ingress-port`.** k7 refuses
  otherwise: a public NodePort in front of a deny-all policy is a confusing
  no-op, and one in front of an accidentally-open policy is a breach.
- The Service uses `externalTrafficPolicy: Local`, so the client's real
  source IP reaches the pod and `--ingress-from cidr:…` rules can mean
  something. With the default `Cluster` the source would be SNAT'd to an
  address on the pod network and any CIDR allowlist would be decoration.
- **A `cidr:` allowlist does not constrain clients on the cluster's own
  nodes.** As in-cluster (above), Cilium does not evaluate an `ipBlock`
  peer against a node IP without `policy-cidr-match-mode=nodes`, so a
  request from a node answers even when the CIDR excludes it. Verify a
  `cidr:` allowlist from a genuinely external client — `curl` from the node
  passes no matter how the rule is written.
- Consequence of `Local`: the NodePort **only answers on the node running
  the sandbox** (`k7 list` shows which one).
- On a bare-metal host (Hetzner) the node IP is public, so
  `--expose-port` with `--ingress-from cidr:0.0.0.0/0` publishes an
  untrusted sandbox to the internet. k7 warns on stderr on every such
  create.
- `k7 delete` removes the Service; a leaked NodePort is both a resource leak
  and an open port.
- A `k7 fork` inherits the source's ingress and egress **policy** but not its
  Service — a fork is not reachable from outside until you expose it.

```bash
k7 create web python:3.12-slim \
  --ingress-port 8000 --ingress-from cidr:203.0.113.0/24 --expose-port 8000
# 🌐 Port 8000 exposed at http://<node-ip>:31234
```

## TLS for `k7-api`

Default `k7 install` serves the NodePort on `31007` over HTTPS. The
`k7-api` container stays HTTP on `:8000` (kubelet probes unchanged); a
Caddy sidecar in the same pod terminates TLS on `:8443`. API keys no
longer travel in cleartext. This is not rate limiting and not a claim
that the API is safe to expose.

| Flag | Cert |
|---|---|
| (none) | Playbook cluster CA at `/etc/k7/tls/ca.crt`. Laptop: copy that file and `k7 config set api.ca ./ca.crt` plus `api.url` to `https://<first-master-ip>:31007`. |
| `--api-hostname NAME` | Let's Encrypt via Caddy (HTTP-01 on host port 80). `NAME` must be a DNS name whose A record is the first-master IP; a bare IP is refused. The CLI uses the system trust store (no `api.ca`). |
| `--api-tls-cert` + `--api-tls-key` | Operator-supplied PEM pair. Incompatible with `--api-hostname`. |
| `--api-insecure-http` | Today's HTTP NodePort, bit for bit. Keys travel in cleartext. |

`--api-insecure-http` cannot be combined with the hostname or cert
flags. It can be combined with `--api-allow-cidr` (the allowlist still
applies; the warning is that keys are still cleartext).

## Restricting who can reach `k7-api`

`k7-api` is a `NodePort` Service on `31007`, and on a dedicated box the
node IP is public. `k7 install --api-allow-cidr 203.0.113.4/32
--api-allow-cidr 198.51.100.0/24` (repeatable, Cilium only — with
`--cni flannel` it fails at the CLI) renders a `CiliumNetworkPolicy`
`k7-api-ingress` on the API pod and flips the Service to
`externalTrafficPolicy: Local`. Both are skipped entirely when the flag
is unset, which is the default.

This is **defence in depth for operators who know their client CIDRs**.
It stacks with TLS and does not replace it. There is still no rate
limiting. See `SECURITY.md`.

Measured behaviour worth knowing before you debug it:

- The policy's `ingress:` section makes the API pod default-deny for
  ingress. The `fromEntities` block (`host`, `remote-node`, `health`,
  `kube-apiserver`) is what keeps the kubelet liveness/readiness probes
  alive — probes arrive from the node's `cilium_host` address and match
  `host`. Remove it and the pod enters `CrashLoopBackOff`.
- `externalTrafficPolicy: Local` is what preserves the client's source
  IP; without it every external caller would arrive SNAT'd as a node IP.
  The trade-off: the NodePort then answers **only** on the node running
  the `k7-api` pod, which is the first master
  (`nodeSelector: k7.katakate.org/first-master: "true"`) — exactly the
  address the playbook writes into `/etc/k7/api_endpoint`. Operators
  hitting other node IPs directly must switch to that one.
- **A curl from the node is not a test of the allowlist.** Node-local
  traffic matches the `host` entity, and node-local NodePort traffic is
  SNAT'd to the pod network even under `Local` (measured: the pod sees
  the node's `cilium_host` address), so it succeeds however wrong the
  CIDR list is. Test from off-cluster.
- Nor is a curl from **another Cilium node that runs its own `k7-api`**:
  with `kubeProxyReplacement=true` its socket load-balancer translates a
  host-originated connection to *any* address on that NodePort to its own
  local backend. Measured: such a client got a healthy `200` while
  `tcpdump` on the target node saw no packet at all. Use a client that is
  not a Cilium node, or confirm with `tcpdump` that the packets arrive.
- A `fromCIDR` peer is not evaluated when the source is Cilium-managed,
  so allowlisting a pod's own /32 does not admit that pod. In-cluster
  callers are governed by `fromEndpoints` rules
  (`agent-networkpolicy.yaml`), not by this allowlist.
- `hubble observe --verdict DROPPED` shows the drops (see below).

Rollback — re-running `k7 install` without the flag removes both pieces,
or by hand:

```bash
k3s kubectl -n kube-system delete ciliumnetworkpolicy k7-api-ingress
k3s kubectl -n kube-system patch svc k7-api \
  -p '{"spec":{"externalTrafficPolicy":"Cluster"}}'
```

A wrong CIDR only costs API access: the policy selects the `k7-api` pod
endpoint, never the host, so SSH, `kubectl` and
`src/k7/cli/dev.sh --core` on the node keep working. The Cilium host
firewall would not have that property, which is why it was rejected.

## Debugging policy drops (Hubble)

Hubble is **opt-in observability**, not enforcement. It is off by
default and is not a security control. Enable it with
`k7 install --hubble` (Cilium CNI only — `--hubble --cni flannel` fails
loudly). The Hubble UI is not installed: it has no authentication and
the node IP is public. Rollback: `cilium hubble disable`.

`hubble observe` talks to Hubble Relay. On the node (verified: Relay
is ClusterIP `:80`; this forwards it to `127.0.0.1:4245`, the Hubble
CLI default):

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cilium hubble port-forward &     # kube-system/hubble-relay → 127.0.0.1:4245
hubble status                    # Healthcheck (via localhost:4245): Ok
hubble observe --verdict DROPPED --last 50
```

When a sandbox with an FQDN whitelist cannot reach a host, the symptom
is always a hang or timeout — policy drop, DNS, MTU, and a dead
upstream look the same from inside the VM. The CHALLENGES.md #9 recipe
tells them apart:

```bash
hubble observe --namespace <ns> --verdict DROPPED --last 50
cilium fqdn cache list
cilium ip list
```

A `Policy denied DROPPED` verdict with the destination still labelled
`world` means the FQDN→identity mapping missed (wildcard too narrow, or
the learned IP expired). `cilium fqdn cache list` shows what Cilium
learned; `cilium ip list` shows which IPs carry an `fqdn:` identity.

## Disk pool sizing (kfd + k7d)

Both node-local storage pools have fixed sizes chosen at install
time — set them per node in the inventory:

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
  Docker-heavy kfd nodes share this pool with `--docker` graph LVs
  (`k7-docker-lvm`); raise `kata_thinpool_pv_size` rather than silently
  resizing.
- **k7d pool full**: writable volume images and fork reflink clones fail;
  the k7d daemon rejects new sandboxes/forks loudly (`no space left on
  device`). The image is sparse, so `ls -l` shows the virtual size —
  use `du`/`df` for actual usage.

Utilization for both pools on every node is surfaced through the API and
CLI/SDK: `GET /api/v1/nodes/storage`, `k7 nodes storage` (`--json` for
raw), and `Client.nodes_storage()`
return a per-node map of `kata_thinpool` (`lvs` size/data%/metadata%)
and `k7d_disks` (`df` size/used/avail), collected from the k7-agent
DaemonSet. A node whose agent is unreachable gets a loud
`{"error": ...}` entry.

## Memory limits (`--memory`)

All three backends honour `k7 create --memory <qty>` (Kubernetes quantity
like `2Gi` / `4096Mi`):

| Backend | Mechanism |
|---|---|
| **kfd** | Kata stamps `io.katacontainers.config.hypervisor.default_memory` (MiB). containerd forwards `io.katacontainers.*` (`pod_annotations` on `runtimes.kata`) and `configuration-fc.toml` allowlists `default_memory` (before that the annotation was silently ignored and the VM stayed at the 2048 MiB default). |
| **kql** | Same annotation path via `runtimes.kata-qemu` + `configuration-qemu.toml`. |
| **k7d** | No Kata annotation — the k7d shim sizes the VM straight from the pod's CRI CPU/memory limits. |

## Known issues

- **kata-fc VMM leak under churn**: kata 3.24.0 with the jailer records the
  `--daemonize`d jailer's PID, so its SIGTERM fallback at pod deletion
  signals a dead PID. When the graceful in-guest shutdown fails (dead agent
  under parallel churn), the firecracker process is orphaned and spins at
  100% CPU. `k7 install` deploys a per-node systemd timer
  (`k7-vmm-reaper.timer`, 1-minute cadence) that kills VMM processes whose
  kata shim is gone, and the integration suite asserts zero orphans cluster
  wide after teardown (`tests/integration/test_zz_leaks.py`).
- **kql docker-in-VM wedge under heavy fsync + replicas ≥ 2 — FIXED**:
  kata's default single-threaded virtiofsd serialized all virtio-fs IO;
  under a sustained fsync burst against an r≥2 Longhorn volume the
  kata-agent's health ping starved behind the IO convoy and the shim
  killed the healthy VM. `k7 install` now widens the virtiofsd thread
  pool (`--thread-pool-size=16`); see PERFORMANCE.md for the root-cause
  narrative and post-fix numbers.
