# Performance

## Show HN cut — 2026-08-18 (apt `k7` 0.2.1, two-node, all backends)

Two Hetzner AX41-1-LTD boxes (k7-node-01 + k7-node-02). The third box
in the fleet was left untouched. Each node `TWO_DISK=1` (Ubuntu on one
NVMe, spare raw for the kfd thin-pool — NVMe names were **swapped**
across the two boxes, so auto-detect is mandatory).

Install path: `apt install k7` (PPA 0.2.1) on the first master, public
`Katakate/k7` v0.2.1 checkout as `k7_repo_root`, **one** command:

```bash
k7 install -i /root/k7-2n.ini --k7d-version 0.2.1
```

`failed=0` in 9m09s. Confirmed: PPA `k7` 0.2.1, PyPI `k7-sdk` 0.2.1,
`k7d` 0.2.1 on both nodes, RuntimeClasses `kata` / `kata-qemu` / `k7`,
thin-pool + `/var/lib/k7d/disks` on both. n=1 wall-clock via `k7 --core`
(create/resume/fork timed until an exec answers).

| Operation | kfd | kql | k7d |
|---|---:|---:|---:|
| create → exec answers | 3.74 s | 20.76 s | 3.83 s |
| pause (API) | 0.81 s | 0.82 s | 1.00 s |
| resume → exec answers | 4.48 s | 12.95 s | 1.95 s |
| fork → exec answers | n/a (rejected) | 83.30 s | **3.93 s** |
| fork inherits `/tmp` marker | n/a | no (disk-only clone) | **yes** (warm memory CoW) |
| sidecar create → `docker info` | 5.02 s | 21.02 s | 5.33 s |
| sidecar `docker run --rm hello-world` | 3.39 s | 4.01 s | 12.57 s |

kql fork is slower than the 2026-08-10 single-node r=1 median (~47 s)
because this cluster is two nodes / Longhorn r=2. k7d is the only backend
whose fork carries live memory. kfd has no fork by design. Docker-in-VM
sidecar (`--sidecar docker`) works on all three; k7d's `hello-world` pull
is the slow path (tmpfs + virtio + NAT), matching the 2026-08-10 note.

## Backend lifecycle: kql vs k7d — 2026-08-10 (spec 9a M11)

Hetzner AX41 dedicated node (Ryzen 5 3600, 64 GiB, NVMe), Ubuntu 24.04,
kernel 6.8.0-137, k3s v1.36.3, flannel CNI, Longhorn 1.10 (r=1), k7d 0.1.0.
Single node, interleaved runs, `alpine:3.20` sandboxes; median of 3
(sidecar legs n=1). Raw samples in the JSON the bench writes. Reproduce
on a node with both backends installed:

```bash
K7_BENCH_BACKENDS=kata-qemu-longhorn,k7d K7_BENCH_REPS=3 \
  uv run pytest -m bench tests/integration/bench_backend_lifecycle.py -v -s
```

| Operation | kql (kata-qemu-longhorn) | k7d | notes |
|-----------|--------------------------|-----|-------|
| create → pod Ready | 17.11 s (15.05–17.13) | **2.13 s** (2.13–2.16) | cold boot; kql pays Longhorn PVC provision + QEMU boot |
| exec round-trip | **0.04 s** (0.03–0.04) | 0.12 s (0.12–0.13) | k7d exec bridges through vsock |
| named snapshot ready | 6.51 s (2.72–6.52) | n/a | Longhorn-only by design; k7d rejects `k7 snapshot` loudly |
| fork (API call) | 7.78 s (7.77–7.80) | 2.15 s (2.14–2.16) | kql: Longhorn snapshot + PVC clone; k7d: CoW disk+memory `fork_vm` |
| **fork → forked pod Ready + exec** | 46.66 s (46.45–46.67) | **2.37 s** (2.37–2.38) | **~20×**: kql cold-boots a VM on the cloned disk; the k7d fork *inherits the source's live memory* (tmpfs, processes, page cache) |
| pause effective | 1.31 s (1.29–1.90) | **0.20 s** (0.20) | kql: scale-to-0, pods terminated; k7d: vCPUs frozen in place |
| resume → exec answers | 4.11 s (2.10–9.19) | **0.34 s** (0.33–0.38) | kql: reschedules pod + VM boot; k7d: restart vCPU loop, memory intact |
| delete | 0.05 s | 0.04 s | |
| docker sidecar: create → `docker info` | 21.11 s | **8.60 s** | dind in the same VM (k7d) / pod (kql) |
| docker sidecar: `docker pull alpine:3.21` | **2.35 s** | 8.21 s | **kql wins**: its dind unpacks onto the Longhorn ext4 volume, k7d's dind data dir sits on guest tmpfs behind virtio + NAT — pull streaming is the k7d sidecar's slow path today |
| docker sidecar: `docker run --rm alpine echo` | 0.91 s | **0.33 s** | |

Honest summary: k7d dominates every lifecycle operation (create ~8×,
fork-to-usable ~20×, pause ~6×, resume ~12×) and is the only backend whose
fork carries **memory state** — the forked sandbox resumes mid-thought
instead of cold-booting. kql keeps two real advantages: named snapshots
that persist after the sandbox dies (restore later, GC, cross-pod
persistence) and faster registry pulls inside the docker sidecar. Plain
exec is also ~3× faster on kql (80 ms absolute difference; both are
interactive-fast).

## Kata-qemu-longhorn backend baseline (2026-04-10)

Observed latencies on a single Hetzner dedicated node (3x NVMe, Ubuntu 24.04, Longhorn replicas=1).

| Operation | Latency | Limit |
|-----------|---------|-------|
| Cold create to pod ready | 15.20s | — |
| Snapshot ready | 6.98s | < 60s |
| Pause (scale to 0) | 0.13s | — |
| Resume (scale to 1 + pod ready) | 4.54s | < 60s |
| **Fork (total)** | **44.85s** | < 120s |
| — snapshot | 2.97s | |
| — clone PVC bound | 2.71s | |
| — deployment ready (pod + VM boot) | ~39.17s | |

Fork is ~3x slower than a cold create. The snapshot and clone PVC steps add ~6s, but the main cost is the forked deployment's VM boot (~39s vs ~15s for a fresh PVC) — likely due to Longhorn replaying cloned data on first attach.

Measured via integration tests (`tests/integration/test_qemu.py`) on 2026-04-10.

## Docker workloads inside sandboxes — 2026-05-29 (spec 10b)

Hetzner AX52 dedicated node (Ryzen 7 7700, 64 GiB, NVMe), Ubuntu 24.04,
kernel 6.8.0-100, k3s v1.35.5, docker host 29.5.2 / sandbox 27.5.1,
Longhorn 1.10. 3 runs per cell after one warm-up; median (range in parens).
⚠ marks cells whose (max−min)/median > 0.30 (range noise gate).

Workload is `bench/docker-perf/bench.Dockerfile`: pull `debian:12-slim`, a
no-cache build that does `apt install build-essential python3 git ca-certificates`,
`pip install numpy pandas requests pytest httpx pydantic`, a 256 MB
`dd ... conv=fsync`, and `pip check`. Run phase exercises a 10-s CPU loop,
a 2 000-small-files + 512 MB-fsync IO workload, and a cat-the-venv-tree
read workload. Reproduce with:

```bash
K7_BENCH_ENVS=host,k7-fd,k7-ql-r1,k7-ql-r2 K7_BENCH_REPS=3 \
  K7_BENCH_OUT=/tmp/bench-out \
  uv run pytest -m bench tests/integration/bench_docker_perf.py
uv run python bench/docker-perf/render.py -i /tmp/bench-out/bench-results-*.csv \
  --title "Docker workloads ..." --hardware "<one-line note>"
```

| Operation                 | host | k7-fd | k7-ql-r1 | k7-ql-r2 |
|---------------------------|------|-------|----------|----------|
| pull debian:12-slim       | 3.06 s (3.00 s–3.06 s) | 2.78 s (2.77 s–2.83 s) | 3.66 s (3.62 s–3.67 s) | 3.64 s (3.62 s–3.81 s) |
| build (no-cache)          | 53.2 s (53.2 s–53.8 s) | 26.0 s (25.0 s–30.2 s) | 72.3 s (72.0 s–72.8 s) | 68.7 s (68.6 s–71.6 s) |
| build (cached)            | ⚠ 714 ms (710 ms–936 ms) | 250 ms (250 ms–291 ms) | ⚠ 690 ms (469 ms–4.49 s) | ⚠ 448 ms (436 ms–663 ms) |
| run cpu (10s budget)      | 10.6 s (10.5 s–10.6 s) | 10.2 s (10.2 s–10.2 s) | 27.6 s (26.0 s–29.7 s) | 27.3 s (25.9 s–29.1 s) |
| run io (2k small + 512 MB)| ⚠ 1.14 s (1.08 s–1.68 s) | 485 ms (475 ms–593 ms) | 37.3 s (37.2 s–37.7 s) | 37.4 s (36.9 s–37.6 s) |
| run read (venv tree cat)  | 992 ms (989 ms–1.02 s) | 225 ms (213 ms–235 ms) | 16.4 s (16.3 s–16.9 s) | 16.4 s (16.4 s–16.9 s) |

Ratios to host (the number that matters for the sandbox tax story):

| Cell                      | k7-fd / host | k7-ql-r1 / host | k7-ql-r2 / host |
|---------------------------|--------------|-----------------|-----------------|
| build (no-cache) ratio    | 0.49× | 1.36× | 1.29× |
| run io ratio              | 0.42× | 32.6× | 32.7× |
| run cpu ratio             | 0.96× | 2.62× | 2.59× |

### What this says

**k7-fd (Firecracker, docker daemon on emptyDir) is *faster* than the
host** on every operation that touches disk — `build (no-cache)` 0.49×,
`run io` 0.42×, `run read` 0.23×. CPU is host-parity (0.96×).

Why faster, when the FD path strictly has more layers (Firecracker VM +
kata virtio-fs sharing the emptyDir from `/var/lib/kubelet/.../sidecar-data/`
into the guest, vs the host docker daemon going straight to ext4 on
`/dev/md2`)? Four contributing factors, in roughly decreasing order of
size:

1. **Storage driver delta.** The host docker daemon picks `overlayfs`
   (the legacy single-layer overlay implementation that ships with the
   host kernel). The FD sandbox's docker daemon picks `overlay2`. On
   metadata-heavy workloads — apt extracts ~5 600 files, pip+venv
   extracts another few thousand — overlay2 is significantly faster
   than overlayfs (different layer-handling code path, better
   d_type/userxattr behaviour). The whole "build (no-cache)" column is
   apt+pip metadata churn, which is exactly where this difference bites.
2. **Clean cache state.** The FD sandbox starts with an empty
   `/var/lib/docker`. The host docker daemon has accumulated state from
   prior `k7 install` builds, k3s-imported images, and the bench's own
   warm-up — manifest lookups, dangling layer GC, layer dedup all run
   on a populated tree.
3. **virtio-fs writeback caching.** Kata-fc shares the emptyDir into
   the guest via virtio-fs (the default for shared filesystem mounts;
   the alternative virtio-blk would require an explicit pod annotation
   for "direct block device" which we don't set). virtio-fs runs a
   writeback cache in virtiofsd on the host. Inside the guest VM,
   `dd if=/dev/zero of=/tmp/x bs=1M count=256 conv=fsync` measured
   1.3 GB/s — the fsync completes when virtiofsd acks, not necessarily
   when the data is durable on NVMe. This is a weaker fsync than the
   host gets directly on ext4. It's the same trade-off `cache=writeback`
   gets you in qemu/9pfs setups: faster, less crash-safe.
4. **Devmapper snapshotter on the VM rootfs.** The Firecracker VM's
   rootfs is a thin-provisioned LVM volume (containerd devmapper
   snapshotter). The `dd` and IO ops the bench runs go through the
   sandbox container's rootfs (devmapper) when writing inside the
   docker daemon's overlay2 upperdir, which itself is on the emptyDir
   (virtio-fs). LVM thin pools are read-cached aggressively at the
   page-cache layer — read-heavy ops like `run read` (cat the venv
   tree) benefit from page cache hits on the *host* even when the
   guest thinks it's doing fresh reads.

**Net of all of this:** k7-fd's 0.4–0.5× ratios *do* reflect a real
performance win for build/dev workloads on this hardware, but it would
be wrong to attribute the win to "VM is faster than bare metal".
Honest framing: *host docker is using a slow storage driver against a
populated daemon, and the FD sandbox is using a fast storage driver
against a clean daemon, with virtio-fs writeback caching softening the
guest's fsync semantics*. A cleaner future bench would (a) run host
docker with `--storage-driver=overlay2` and a fresh `/var/lib/docker`,
(b) note the virtio-fs cache mode explicitly, and (c) report the
device-level bandwidth so the absolute numbers are anchorable.

**k7-ql (qemu, docker daemon on a Longhorn PVC sub_path) pays a steep
storage tax** — `build (no-cache)` 1.3×, `run cpu` 2.6×, `run io` ~33×,
`run read` ~16×. Two things stacked here: (1) docker auto-picks the **vfs**
storage driver inside the qemu VM because Longhorn's iSCSI-attached block
device doesn't expose the filesystem features overlay2 wants; vfs copies
entire layer trees on every operation, so cached-build and image-layer
work get hit hard, and (2) reads/writes go disk → iSCSI → qemu virtio →
guest, where the host went directly to the page cache. The `run io`
column (~37 s for 2 000 small files plus a 512 MB fsync) is the worst-case
shape — pure disk-bound work with no compute.

**r=2 vs r=1 is statistically indistinguishable** (build 68.7 s vs 72.3 s,
io 37.4 s vs 37.3 s). The second Longhorn replica adds one cross-node
sync but the dominant cost on this hardware is vfs inside the guest, not
the replica copy on the wire. Useful negative result — picking r=2 for
durability does not double the cost on these workloads.

### Known confounds

- **Storage driver mismatch (the big one).** Host uses overlayfs; k7-fd
  uses overlay2 on a virtio-fs-shared emptyDir; k7-ql uses vfs. Every
  cross-env comparison is also a "different docker storage driver"
  comparison. We cannot cleanly isolate "VM tax" from "driver tax"
  without rebuilding the docker daemon image inside the guest with a
  matching driver, which the spec deliberately doesn't attempt.
- **Asymmetric daemon state.** Host docker has accumulated images,
  layers, and dangling refs from prior `k7 install` runs. Each sandbox
  starts with a fresh daemon. We're partly measuring "warm vs cold
  daemon".
- **virtio-fs writeback caching changes fsync semantics.** Guest fsync
  acks when virtiofsd has the data, not when NVMe has it. Compare the
  k7-fd dd fsync (1.3 GB/s) with the host's bare dd fsync (not
  benchmarked here — would need a follow-up rep).
- **Build noise.** `build (cached)` is sub-second everywhere and lands
  in the noise floor; the ⚠ flags on host and k7-ql-r1 are real
  variance (4.5 s outlier on k7-ql-r1 rep 3) but not a signal about the
  backend — they're "this op is too fast to time meaningfully with 3 reps."
- **Same physical disk for everything.** k7-fd's `/var/lib/docker`
  emptyDir (which lives as a regular directory under
  `/var/lib/kubelet/pods/.../volumes/kubernetes.io~empty-dir/` on the
  host ext4 root), k7-ql's Longhorn PV, and the host's docker root all
  live on the same `/dev/md2` (RAID1 NVMe pair). We're not measuring
  cross-disk effects. Note: emptyDir without `medium: Memory` is *not*
  tmpfs — it's a plain directory on the kubelet root FS.
- **Single-node sample.** Bench was driven on `k7-node-01` only. r=2's
  cross-node sync went to `k7-node-02` but the workload pod stayed on
  the primary. A future spec could pin pods to different nodes to also
  exercise the read-from-remote-replica path.

Raw per-leg logs and the aggregated CSV are under `bench/docker-perf/results/`
(gitignored — keep them in agent or local scratch space, paste into the
table above when adding a new run).

## Docker-in-VM under real Longhorn replica counts (2026-08, spec 18e/18h)

The "r=2 vs r=1 indistinguishable" result above is **invalid**: the old
bench patched a Longhorn *setting* that only affects newly-created volumes,
so both legs actually ran r=1 (CHALLENGES.md #2). `bench_docker_perf.py` now
sets real per-volume replica counts (`k7-ql-r3` leg). Host / kfd / r1 / r2
medians from the 18e HA run (5 reps); **k7-ql-r3 column re-filled in
spec 18h** after the virtiofsd wedge fix (5 reps, zero VM restarts):

| op | host | k7-fd | k7-ql-r1 | k7-ql-r2 | k7-ql-r3 |
|---|---|---|---|---|---|
| build no-cache | 76.4s | 52.5s | 108.2s | 270.9s | 292.5s |
| run io (2k files + 512MB fsync) | 1.43s | 1.02s | 41.4s | 68.4s | 88.3s |
| run read (venv tree cat) | — | — | — | — | 47.7s |
| run cpu (10s budget) | 10.5s | 10.4s | 41.5s | 47.8s | 56.4s |

Two takeaways (spec 18f issue 8 / 18h):

- **The r1→r2 jump dominates the redundancy cost** (build 108→271s; r2→r3
  adds only ~8% on build): the second replica forces synchronous
  cross-node writes, the third mostly parallelizes with them. Post-wedge
  r3 `run io` (88.3s) is higher than the old single-rep 64.5s sample —
  that sample was the lucky survivor of a ~50% kill rate, not a median.
- **kql-r3 IO wedge — root-caused and FIXED (spec 18g):** the "VM exec
  path dies after a run_io rep" wedge was not guest memory, not dockerd,
  and not Longhorn faulting — the guest was healthy (load 0.4, 1.5 GB
  free, zero dirty pages, clean dmesg) at the moment of death. The killer
  was the **kata shim**: kata's default virtiofsd runs with
  `--thread-pool-size=1`, so all virtio-fs IO (container rootfs + the
  Longhorn-PVC-backed `/var/lib/docker`) serializes through one thread.
  A `docker run` of the ~790 MB bench image makes vfs copy the whole
  rootfs and then fsync 512 MB through that single thread against an
  r=3 volume (~60–80 s saturated); any agent RPC touching virtio-fs
  blocks behind it, the shim's agent health ping (`CheckRequest`) times
  out, and the shim declares "Dead agent" and kills the healthy VM
  (`sandbox stopped unexpectedly`, pod sandbox recreated). Repro rate was
  ~50% per run_io rep. Fix: `k7 install` now sets
  `virtio_fs_extra_args = ["--thread-pool-size=16", ...]` in the
  kata-qemu config — 18h re-ran 5/5 `run_io` + 5/5 `run_read` with zero
  VM restarts on the same r=3 volume (`run_read` median 47.7s).
  Exec-probe pressure was reduced too (kubelet's default 1 s exec-probe
  timeout sprayed cancelled ttrpc execs — `docker info` legitimately
  takes >1 s while dockerd copies vfs layers), which cuts the
  `ttrpc: received message on inactive stream` noise but was NOT
  sufficient on its own.

## k7d docker-perf leg (2026-08-11, partial)

`test_bench_k7d` is wired (`K7_BENCH_ENVS=k7d`). Guest sized at **3Gi /
4 vCPU** — largest size that reliably reaches Ready on this k7d build;
≥~4Gi fails agent connect (`Connection timed out`, MMIO base moves past
4 GiB). At 3Gi the guest tmpfs upper is ~1.5 GiB, which is enough for
`docker pull debian:12-slim` but not for a sustained no-cache build of
`bench.Dockerfile` (apt+pip layers already ~1.4 GiB before the cpython
clone / 256 MB `dd`).

Measured on the 3-node HA cluster (pod on k7-node-02), 3 reps, no warmup:

| op | k7d (3Gi) | notes |
|---|---|---|
| pull debian:12-slim | **9.99 s** (9.88–10.19) | overlay2; slow vs kql's ~2.4 s lifecycle-bench pull — same NAT/tmpfs path |
| build / run_* | n/a | hits ENOSPC / guest wedge mid-build under the 1.5 GiB tmpfs ceiling |

One-shot smoke (same limits, single `docker build --no-cache`) completed in
~302 s earlier the same day — reproducible multi-rep builds did not.
Unblocking the full column needs a k7d fix for ≥4 GiB guests (or a larger
non-tmpfs docker data disk).
