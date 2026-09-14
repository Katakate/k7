# Challenges & solutions log

Tracking non-obvious bugs in k7 (the sandbox management layer) and how they
were solved. Same format as k7d-dev's `CHALLENGES.md`.

## 1. `k7 install --backend` extra-var silently clobbered inventory `k7_backends` (spec 18e)

**Symptom:** A 3-node HA install with an inventory declaring
`k7_backends=kfd,kql,k7d` per host would have provisioned only the two Kata
backends — the `k7d` backend would silently disappear from every node.

**Root cause:** `k7 install` always forwarded `k7_backends` as an Ansible
**extra-var** (built from the `--backend` option's *default* value even when
the user never passed `--backend`). Extra-vars have the highest precedence in
Ansible, so the CLI default overrode the per-host inventory var.

**Fix:** `install()` now checks `ctx.get_parameter_source("backend")`; when
the user supplied `-i <inventory>` without an explicit `--backend`, the
`k7_backends` extra-var is dropped so the inventory wins. An explicit
`--backend` alongside `-i` prints a warning that it overrides the inventory.

**Reference:** none (Ansible variable-precedence rules).

**Time lost:** caught in pre-flight review (spec 18e Phase 0), ~1 hour of
code reading. Would have cost a full reset+reinstall cycle if it had shipped.

---

## 2. Longhorn StorageClass `numberOfReplicas` parameter makes `default-replica-count` a no-op (spec 18e)

**Symptom:** `bench_docker_perf.py`'s r1/r2 legs patched Longhorn's
`default-replica-count` setting — but sandbox volumes kept the replica count
baked into the `longhorn` StorageClass (`numberOfReplicas: "3"` on the HA
cluster). The r1/r2/r3 benches would all have silently measured the same
replica count.

**Root cause:** Longhorn only consults the `default-replica-count` setting
when the StorageClass has **no** `numberOfReplicas` parameter. k7's install
playbook pins the parameter in the SC (topology-aware SC from the
`longhorn-storageclass` ConfigMap), so the setting never applies to k7
volumes.

**Fix:** the bench now patches `spec.numberOfReplicas` on the sandbox's
Longhorn **Volume CRs** after creation (`_set_sandbox_volume_replicas`),
waits until exactly N running replicas exist, and records the replica → node
placement in the log header as proof.

**Reference:** Longhorn docs (volume-level replica count update).

**Time lost:** ~1 hour. The old benches "passed" — the mismeasurement was
invisible without checking actual replica CRs.

---

## 3. k7d VM operations are node-local; multi-node scheduler breaks pause/fork tests (spec 18e)

**Symptom:** On the 3-node cluster, `test_k7d.py` pause/fork tests failed
with `sandbox X runs on node k7-node-02, but this k7 process runs on
k7-node-01; k7d VM operations must run on the pod's node`.

**Root cause:** k7d pause/resume/fork go through the **node-local**
`/run/k7d/k7d.sock`; cross-node VM ops are explicitly out of scope (k7d spec
9a M12) and core fails loudly on the mismatch. On a single-node cluster the
tests never noticed; with 3 schedulable nodes the sandbox lands anywhere.

**Fix:** tests that exercise VM ops pin their sandboxes with
`SandboxConfig(node_name=os.uname().nodename)` — the field that exists
precisely for host-side-inspection tests. The centralized-API implication
(k7-api pod can only pause/fork k7d sandboxes co-located on the first
master) is recorded as a release-readiness limitation.

**Reference:** k7d spec 9a M12 (cross-node fork out of scope).

**Time lost:** ~30 min (the loud error message made it easy).

---

## 4. kql fork loses guest writes made just before the fork (crash-consistency)

**Symptom:** `test_api.py::test_sdk_pause_resume_fork_round_trip` flaked on
the HA cluster: a file written via exec seconds before `fork()` did not
exist in the fork (`cat: can't open '/mnt/state/marker'`). The near-identical
`test_qemu.py::test_fork_clones_data` data check passed in the same run —
pure timing luck.

**Root cause:** the kql fork path cuts a **block-level** Longhorn
VolumeSnapshot of the source's root PVC. That is only crash-consistent: guest
writes still sitting in the VM's page cache are not on the block device yet
and are missing from the clone.

**Fix:** `fork_sandbox` now execs `sync` in the source sandbox before
creating the snapshot (only when the deployment has ready replicas — a
paused source has no writers), failing loudly if the flush fails.

**Reference:** none (standard crash-vs-application consistency).

**Time lost:** ~1 hour including re-runs. Note: *named* snapshots of live
sandboxes (`k7 snapshot`) remain crash-consistent by design — documented
behavior, unchanged.

---

## 5. NVMe enumeration swaps across reboots — hardcoded `k7_devmapper_disk` hit the OS disk

**Symptom:** The second full reset+reinstall loop of spec 18e failed on
k7-node-03: `Device '/dev/nvme1n1' has partitions; wipe it with
utils/wipe-disk.sh or choose another disk`. The identical inventory had just
worked on the first loop.

**Root cause:** Linux NVMe controller enumeration (`nvme0n1` vs `nvme1n1`)
is not stable across reboots. After the second reset, node-03 booted with
its OS on the disk now enumerated `nvme1n1`, and the raw spare as
`nvme0n1` — the inventory's hardcoded `k7_devmapper_disk=/dev/nvme1n1`
pointed at the OS disk. The playbook's safety checks caught it (fail-loud
worked as designed).

**Fix:** omit `k7_devmapper_disk` on identical dual-NVMe boxes — the
playbook's auto-detect ("first empty, non-removable, non-root whole disk")
is enumeration-proof. `inventory.ini.example` now documents this.

**Reference:** none (kernel device-naming behavior).

**Time lost:** ~30 min (one wasted install attempt + one extra
reset+reinstall loop of all three nodes).

---

## 6. Orphaned Firecracker microVMs leak on pod deletion and each burns a full CPU core

**Symptom:** During spec 18e Phase 3, the `k7-ql-r2` bench leg started
failing mid-run with `Pod is not running (status: Pending)` and the
`k7-ql-r3` leg failed entirely; longhorn-manager / cilium-envoy /
coredns readiness probes were flapping cluster-wide. `k7-node-03` had a
load average of ~21.

**Root cause:** 14 orphaned `/firecracker` processes (2 on node-01, 2 on
node-02, 10 on node-03) whose pods had been deleted hours earlier —
zero live Kata pods existed cluster-wide. Each orphan spun at ~97% CPU
(TIME ≈ ETIME in `ps`), starving Longhorn/Cilium/CoreDNS and the bench
sandbox itself. The kata-fc shim intermittently fails to kill the
microVM on pod deletion under parallel pod churn (~14 leaks over ~30
kfd pod deletions that day). Evidence:
`/tmp/leaked-firecracker-vms.txt` (agent run artifact).

**Fix (remediation):** verified no live Kata pods, then `pkill -9
firecracker` on all three nodes; loads recovered and the r2/r3 bench
legs were re-run green. **Root-cause fix still open** — tracked as a
release blocker in spec 18f-release-blockers (investigate
containerd-shim-kata-v2 / jailer cleanup path; add a leak-detection
integration test that asserts zero firecracker processes after suite
teardown).

**Reference:** none yet (kata-containers shim lifecycle).

**Time lost:** ~1.5 hours (failed bench legs + diagnosis + re-run).

---

## 7. Remote test loop tied to the SSH session died mid-run (Broken pipe)

**Symptom:** A multi-suite pytest loop launched over plain `ssh host 'for
f in ...; do pytest ...; done'` died silently when the SSH connection
dropped (`client_loop: send disconnect: Broken pipe`) — the remote shell got
SIGHUP'd between suites.

**Root cause:** the remote loop was a child of the SSH session; NAT idle
timeouts kill long-lived connections even with keepalives.

**Fix:** write the loop to a script on the node and launch it with
`setsid nohup ... < /dev/null &`, then poll a progress file. (Same class of
issue `utils/run-integration-tests.sh` already documents for its keepalive
settings.)

**Reference:** none.

**Time lost:** ~20 min (one interrupted suite sequence, `test_restore` had
finished right before the drop).

---

## 8. Firecracker leak root cause: `jailer --daemonize` makes the kata shim signal a dead PID (spec 18f)

**Symptom:** Follow-up to #6. Reproduced at will on the 18f run: create a
naked kfd pod (`sleep` workload), delete it — the pod terminates cleanly
but its `/firecracker` process survives with PPID 1 and climbs to ~100%
CPU. Two out of two attempts leaked. Shim logs at 18e leak time showed
`Agent did not stop sandbox: Dead agent` + `failed to ping agent:
CheckRequest timed out`.

**Root cause:** kata 3.24.0 `virtcontainers/fc.go`. When jailed (spec 8a
enabled the jailer), `fcInit` launches `jailer --daemonize`, which
double-forks — firecracker reparents to init immediately, and
`fc.info.PID = cmd.Process.Pid` records the **jailer's** PID, which is
already dead. `fcEnd()` then calls `WaitLocalProcess(pid, …, SIGTERM)` on
that stale PID: a no-op. The VMM normally exits because the in-guest agent
shuts the VM down; whenever that graceful path fails (dead/hung agent under
churn, wedged guest IO), nothing ever kills the firecracker process. The
`getting vm status failed … firecracker.socket: no such file or directory`
error seen at every kfd VM boot is a side effect of the same daemonize
handling (the shim polls the jailed API socket path before it exists) and
is harmless noise.

**Fix:** upstream fix belongs in kata (record the real VMM PID when
jailed). In k7: (a) `k7 install` now deploys a per-node systemd timer
`k7-vmm-reaper.timer` (1 min cadence) that SIGKILLs firecracker processes
whose 32-hex `--id` matches no live `containerd-shim-kata-v2 … -id`
(a live jailed firecracker always has PPID 1, so parentage cannot be used)
and qemu processes reparented to init; (b)
`tests/integration/test_zz_leaks.py` runs last in the suite and asserts
every node's VMM process count equals its live Kata pod count via hostPID
scan pods.

**Reference:** kata-containers `src/runtime/virtcontainers/fc.go`
(`fcInit`/`fcEnd`), firecracker jailer docs (`--daemonize`).

**Time lost:** ~1.5 h (live repro + kata source dive), on top of the ~1.5 h
in #6.

---

## 9. Cilium `matchPattern` `*` never crosses label boundaries — `*.docker.com` silently misses CDN blob hosts (spec 18f)

**Symptom:** `docker pull` inside a sandbox with
`--egress '*.docker.io' --egress '*.docker.com' --egress docker.io
--egress '*.cloudfront.net'` fetches the manifest fine but times out
downloading blobs (`dial tcp 108.156.22.x:443: i/o timeout`), even though
`cilium fqdn cache list` shows `production.cloudfront.docker.com` being
learned. Hubble showed the SYNs `Policy denied DROPPED` with the CloudFront
IPs still carrying identity `world`; `cilium ip list` had `fqdn:*.docker.io`
entries (single-label subdomain `registry-1`) but nothing for the blob host.

**Root cause:** in Cilium's FQDN `matchPattern` grammar
(`pkg/fqdn/matchpattern`), `*` expands to `[-a-zA-Z0-9_]*` — DNS characters
within a **single label**. `production.cloudfront.docker.com` therefore
does not match `*.docker.com` (and it is not under `cloudfront.net` at
all, so that entry never helped). The multi-label subdomain wildcard is the
non-obvious `**.` prefix form. Not a Cilium bug — a semantics trap between
k7's documented "wildcards like *.huggingface.co" UX and Cilium's grammar.

**Fix:** `K7Core._apply_cilium_egress_policy` now translates a leading `*.`
into `**.` (explicit `**.` and mid-label wildcards pass through). Verified
live: the same pull that timed out for 2m40s completes in ~8s. Also set
Cilium `dnsProxy.minTtl=3600` at install: CDN DNS TTLs are 30–60s while
dockerd's blob downloader keeps dialing its cached IP for minutes, so with
`minTtl=0` the learned FQDN→identity mapping can expire mid-download.
Integration coverage: `test_docker_pull_through_fqdn_whitelist`.

**Reference:** cilium `pkg/fqdn/matchpattern/matchpattern.go`
(`escapeRegexpCharacters`), Cilium docs "DNS based" policies.

**Time lost:** ~2 h (repro, hubble/ipcache/fqdn-cache spelunking, a wrong
first hypothesis on TTL expiry that the live test disproved).

## 10. kql-r3 dind IO wedge: single-threaded virtiofsd starves the kata-agent health ping (spec 18g)

**Symptom:** A kql (kata-qemu-longhorn) sandbox with the docker sidecar,
running the spec-10b `run_io` workload (2k files + 512 MB `dd conv=fsync`)
on an r=3 Longhorn volume, would intermittently (~50% per rep) "wedge":
exec 500s, both containers restarted, `Pod sandbox changed, it will be
killed and re-created`. First seen 2/2 in the spec-18e bench (`run_read`
unmeasurable on r3).

**Root cause:** Not the guest, not dockerd, not Longhorn. A `dmesg -c` +
`/proc/meminfo` stream running inside the guest right through the death
showed a healthy VM (load 0.4, 1.5 GB free, zero dirty/writeback, no OOM,
no hung tasks) — the last log lines were normal container veth setup. The
containerd log on the node had the smoking gun: floods of
`ttrpc: received message on inactive stream`, then
`failed to ping agent: CheckRequest timed out` → `Dead agent` →
`sandbox stopped unexpectedly` — the **kata shim killed a healthy VM**.
Kata's default virtiofsd runs `--thread-pool-size=1`, so ALL virtio-fs IO
(container rootfs + the Longhorn-PVC `/var/lib/docker`) serializes through
one thread. `docker run` on a vfs-driver dind copies the whole ~790 MB
image rootfs and then fsyncs 512 MB through that single thread against an
r=3 volume (60–80 s saturated). Agent RPCs that touch virtio-fs queue
behind the convoy; the shim's health ping starves and it declares the
agent dead. r≥2 matters only because Longhorn write amplification makes
the convoy long enough to exceed the ping deadline.

**Misdiagnoses ruled out on the way:** guest memory sizing (MemAvailable
1.8 GB throughout), vCPU count (4-vCPU guest wedged *faster*), Longhorn
backpressure/faults (volume `attached healthy`, no rebuilds), kubelet
exec-probe pressure (relaxing `docker info`/`true` probe timeouts from the
kubelet default 1 s reduced cancelled-ttrpc noise but did NOT stop the
kill).

**Fix:** playbook now sets
`virtio_fs_extra_args = ["--thread-pool-size=16", "--announce-submounts"]`
in `configuration-qemu.toml` (kata reads it per sandbox start, no restart
needed). Verified on the live 3-node HA cluster: 6/6 run_io reps +
run_read (~45 s) with zero VM restarts on the same r=3 volume. The probe
relaxations in `core.py` were kept as well (less cancelled-exec churn on
the shim↔agent ttrpc channel).

**Reference:** kata-containers virtiofsd integration (default
`--thread-pool-size=1`), virtiofsd docs on request queueing.

**Time lost:** ~3 h (bench-faithful repro, guest-side dmesg/meminfo
streaming, two disproven hypotheses, virtiofsd A/B).

## 11. Control-plane SSRF via sandbox `image` + missing API-key namespace authz (spec 10h)

**Symptom:** Authenticated callers of `k7-api` 0.2.0 could point the
control plane at internal/loopback/metadata addresses by supplying a
crafted container `image` (e.g. `169.254.169.254/...` or
`127.0.0.1:PORT/...`). Separately, any valid API key could operate on any
Kubernetes namespace — keys were authenticated but not authorized.

**Root cause:** `_get_registry_image_config` parsed the registry host
straight from the user-controlled image reference and issued `httpx`
GETs with no allowlist and no private/loopback/link-local rejection; the
`localhost` case even downgraded to plaintext `http`. On the authz side,
`verify_api_key` returned key metadata that no handler consulted, and
`namespace` was a free query/body parameter on every route.

**Fix:**
- `_assert_registry_host_allowed` — allowlist (default public registries +
  `K7_REGISTRY_ALLOWLIST`) plus resolve-and-deny for non-public addresses;
  called before any registry HTTP; `follow_redirects=False`; localhost→http
  downgrade removed. Also enforced early in `create_sandbox`.
- Optional `"namespaces": [...]` on API key records; CLI
  `generate-api-key -n`; `authorize_namespace` applied on every
  namespace-bearing endpoint. Absent/empty scope remains unrestricted.

**Reference:** Responsible disclosure against `k7-api` 0.2.0
(SSRF ≈ CVSS 7.1; missing namespace authz ≈ CVSS 9.1 in multi-tenant).
spec 10h-security-ssrf-and-namespace-authz.

**Time lost:** n/a (implemented from disclosure + spec).

---

## 12. `k7 exec` swallows `--rm` / `-c` (Show HN apt 0.2.1 smoke)

**Symptom:** `k7 --core exec NAME docker run --rm hello-world` returned in
~0.8 s on every backend with no `Hello from Docker`. Separately,
`k7 exec NAME sh -c 'echo x > /tmp/m'` aborted with PyInstaller's
`tried to call itself with '-c'`.

**Root cause:** Typer treats `--rm` as an option of `k7 exec`, so it never
reaches docker. The packaged CLI is a PyInstaller binary; a guest command
that includes `-c` trips its self-execution guard.

**Fix:** pass a separator and avoid `-c` in the guest command:
`k7 --core exec NAME -- docker run --rm hello-world` and
`k7 --core exec NAME -- 'echo x > /tmp/m'`. No product change this round.

**Reference:** none (Typer + PyInstaller).

**Time lost:** ~20 min (misread as sidecar/egress failure).

---

## 13. k7d `--docker`: BuildKit TLS vs dockerd pull; `-p` is guest-host netns (spec 37a-inc2)

**Symptom:** `docker run --rm hello-world` and `docker run alpine:3.21`
succeeded in a `--docker` k7d sandbox, but `docker build -t myapp .`
from alpine:3.21 failed with `x509: certificate signed by unknown
authority` on `auth.docker.io`, and `wget http://127.0.0.1:8080` from
`k7 exec` never saw an nginx published with `-p 8080:80`.
`K7Core.exec_command` also always reports `exit_code=0`, so a failed
`docker build` looked like a successful tag that `docker run myapp`
then could not find.

**Root cause:** dockerd's pull path uses the payload CA bundle
(`guest/docker/payload/etc/ssl/certs/ca-certificates.crt`). Default
BuildKit metadata fetch (docker driver, buildx 0.20 / buildkit v0.18)
does not, so `docker build` / compose `build:` fail TLS while `docker
pull` works. `docker run -p 8080:80` publishes in the **guest host**
netns (k7d inc1 probes it via agent vsock wget). `k7 exec` is the CRI
container netns, so localhost:8080 is the wrong place. `exec_command`
never reads the kubectl-exec exit code.

**Fix:** tests use `DOCKER_BUILDKIT=0 docker build` (classic builder →
dockerd pull + CA) and probe the inner nginx with `docker exec web wget
http://127.0.0.1:80`. Guest exit codes are asserted via an `__K7_EC:$?`
marker. Do not "fix" this by setting `DOCKER_BUILDKIT=0` as a product
default.

**Reference:** k7d `crates/k7d/tests/test_docker_service.rs`
`test_docker_warm_fork_nginx_survives` (`agent_sh` wget 8080);
k7d `guest/docker/payload/etc/ssl/certs/ca-certificates.crt`.

**Time lost:** ~40 min (first integration pass).

---

## 14. k3s containerd does not stamp the runtime-handler; `k7d_version` 0.5.0 would clobber the new shim (spec 38a-inc5)

**Symptom:** RuntimeClass `k7-fc` is not enough for the shim to see
handler `k7-fc` — this node's k3s 1.36 / containerd 2.3.3 never sets
`io.kubernetes.cri.runtime-handler` on the sandbox OCI spec (k7d
CHALLENGES #228). Also, `k7 install --backend k7d-fc` with the playbook
default `k7d_version: 0.5.0` would download the public GitHub tarball
and overwrite `/usr/local/bin/containerd-shim-k7-v1` / `k7d` with a
build that does not know ConfigPath.

**Root cause:** ConfigPath on `runtimes.k7-fc` is the working signal.
Kata Firecracker lives at `/opt/kata/bin` (playbook pin ~v1.14);
k7d-fc installs upstream v1.16.1 at `/usr/local/bin` — different
paths, do not share binaries. `grep runtimes.k7` matches `k7-fc`.

**Fix:** playbook writes `/etc/k7d/shim-k7-fc.toml` and a ConfigPath
`[options]` table **without** `BinaryName`. Install Firecracker via
vendored `src/k7/deploy/k7d-fc/install-firecracker.sh` (sha fatal).
Smoke-test the playbook with `--tags k7d-fc` (facts tagged `always`)
and `--k7d-artifact` pointing at a tarball built from the k7d tree
that just passed `make remote-check`, never the 0.5.0 GitHub URL.
`k7_has_k7d` is true for `k7d` **or** `k7d-fc`. The allowed-backend
`difference()` list must include `k7d-fc` or a k7d-fc-only install
fails validation before any task runs.
A fifth trap: the playbook **replaces** the whole containerd template
from the selected backends. A full `k7 install --backend k7d,k7d-fc`
on a mixed k7d-dev node would drop kata/nvidia/wasm runtime blocks.
`--tags k7d-fc` installs Firecracker + toml + RuntimeClass + labels
and does not rewrite the template (`runtimes.k7-fc` is also
self-patched by k7d's `ensure_k7_fc_runtime_registered`).

**Reference:** k7d CHALLENGES #228, `docs/backends.md`.

**Time lost:** named in the spec before implementation (~15 min
confirming live sandbox inspect on the node).

---

## 15. CRI exec into a k7-fc guest hung; kube Ready never flipped (spec 38a-inc5 / fixed 38a-inc6)

**Symptom:** `tests/integration/test_k7d_fc.py` created a Running
`runtimeClassName: k7-fc` pod (shim log `backend=Firecracker`) then
hung forever on `K7Core.exec_command`. `timeout 15 k3s kubectl exec
… -- echo hello` exited 124. The sandbox's exec readiness probe
(`/bin/sh -c true`, 5s) also timed out, so the container never
became Ready.

**Root cause:** Firecracker guests are reached over the jail vsock
UDS (`FcUds`), not host `AF_VSOCK`. The daemon publishes
`guest_cid = 0` (`NO_HOST_CID`). A host that dialled CID 0 blocked
in `connect()` with no timeout, so CRI Wait never saw an exit code.
This was not ConfigPath / RuntimeClass selection (k7d #228 / #230)
and not virtiofs (k7d-fc has none).

**Fix (inc6):** k7d refuses `AF_VSOCK` CIDs 0–2 and proves
`timeout 15 kubectl exec … -- echo hello` plus kube Ready on
`k7-fc` (`test_k7_fc_exec_probe_ready_and_native_exec`). Install that
artifact with `--k7d-artifact` (not GitHub `latest`). k7-fc tests
wait on Ready again; `test_k7d_fc_exec_echo_hello` and
`TestDockerK7dFc` exec into the guest. Ready is a valid signal.

**Reference:** k7d CHALLENGES #228 / #230 (selection) and #232 (CID 0).

**Time lost:** ~45 min in inc5 (pytest sat on exec until the SSH
session dropped; leftover ns `k7-test-fe13c2d7` had to be deleted
by hand).

---

## 16. Kata `--docker` vehicle: persist-bind hides CLI; two-PVC fork; shareProcessNamespace (spec 22a)

**Symptom / traps while bringing `--docker` to kfd/kql:**

1. **kql persist-bind overlays `/usr`.** The spec mounts the docker CLI via
   emptyDir `subPath` onto `/usr/local/bin/docker` and
   `/usr/local/lib/docker/cli-plugins/*`. kql's persist-bind then
   `mount --bind`s the PVC's `/usr` over that path, so compose/buildx
   vanish. Fix: also mount the CLI emptyDir at `/run/k7/docker-cli`
   (persist-bind does not overlay `/run`) and restage the canonical
   paths immediately after the `/usr` bind. Vehicle path-sharing
   fingerprints that staging file, not `/usr`.
2. **`hostPID` is the node, not the Kata guest.** Path sharing needs the
   vehicle to see the sandbox container's rootfs. `shareProcessNamespace:
   true` shares the *guest* PID namespace between the two CRI containers.
   `hostPID: true` would be the Kubernetes node (and is the wrong trust
   domain).
3. **Two Longhorn VolumeSnapshots are not atomic.** kql fork/restore
   snapshots the root PVC and the docker-graph Block PVC separately.
   Each is crash-consistent (`sync` in sandbox + vehicle first);
   containerd's boltdb recovers. Do not claim cross-volume atomicity.
4. **Never fall back to virtio-fs for the graph.** CHALLENGES #10's vfs
   tax and virtiofsd wedge are why the graph is `volumeDevices` + ext4 +
   overlay2 or fail loud. Keep the relaxed exec-probe timings (3/15/12/4).
5. **Alpine dind `VOLUME /var/lib/docker` is virtio-fs on Kata.** Unmount
   it, mkfs only when blkid is not already ext4 (busybox blkid ignores
   `-o value`), then `mount -t ext4`.
6. **`mount --bind` from `/proc/PID/root` is EINVAL on Kata.** Reading
   through that path works. Path sharing **symlinks** `/tmp` `/home`
   `/root` `/opt` `/workspace` in the vehicle onto the sandbox rootfs so
   `docker run -v /tmp/x:/x` resolves.
7. **OpenEBS LVM `GetCapacity` reports VG VFree, not thin-pool free.**
   `kata-vg` is almost entirely the thin-pool plus a few GiB leftover.
   With `storageCapacity: true` (chart default) the scheduler sees ~5Gi
   and never binds a 20Gi `k7-docker-lvm` PVC. Disable capacity tracking;
   thin LVs come from the pool (`thinProvision: yes`).
8. **Kata FC virtio-fs file `subPath` mounts are invisible to Docker
   plugin discovery.** `docker compose` is not a docker command on kfd
   unless compose/buildx are copied into `~/.docker/cli-plugins` from
   the directory-mounted emptyDir. kql persist-bind already restages
   them as regular files.

**Reference:** spec 22a-kata-docker-vehicle; CHALLENGES #10, #13.

**Time lost:** kql integration (vehicle CrashLoopBackOff on graph mount
and path-share).

---

## 17. k7-fc `--docker` Ready wait saw OutOfcpu corpses; CRI exec after pause hung

**Symptom (bench wait):** `test_bench_k7d_fc` at 4 CPU Guaranteed left a
Ready pod plus a pile of Failed/`OutOfcpu` siblings (`requested: 4050,
used: 9890, capacity: 12000` — the just-deleted k7d sandbox's 4 CPU
still counted). `_wait_all_containers_ready` only looked at `items[0]`,
which was a Failed pod, so the wait would have timed out at 300s while
one replica was already Ready.

**Fix:** wait until **any Running** pod has all containers Ready (same
shape as `bench_backend_lifecycle._pod_ready`). Forked-child `docker info`
asserts overlay2 on both k7d and k7d-fc.

**Symptom (lifecycle resume):** alpine k7d-fc pause returned in 0.18s
and k7d logged `resumed vm-… (guest_cid=0)`. `_wait_exec` after resume
never returned: kubelet `Readiness probe failed: "/bin/sh -c true"
timed out after 5s`. Create→Ready and exec *before* pause had worked
(2.14s / 0.18s). This is #15 again on the post-pause path, not a
docker-perf failure. Lifecycle was aborted; do not quote k7d-fc
resume→exec from that run.

**Reference:** CHALLENGES #15; k7d #232.

**Time lost:** ~15 min on the OutOfcpu wait; lifecycle resume hung until
the pytest process was killed (~8 min).

---

## 18. `k7 install --backend k7d-fc` cannot find vendored Firecracker installer

**Symptom:** A 3-node HA install with `k7_backends=kfd,kql,k7d,k7d-fc` (public
k7 0.3.0) failed at `K7d-fc — stage install-firecracker.sh and pins` on every
node:

```
Could not find or access 'k7d-fc/install-firecracker.sh'
Searched in:
  /tmp/files/k7d-fc/install-firecracker.sh
  /tmp/k7d-fc/install-firecracker.sh
  ... on the Ansible Controller.
```

K3s HA, Cilium, Longhorn, kfd thin-pool, and k7d itself had already succeeded.

**Root cause:** `k7 install` writes the embedded playbook to a tempfile
(`/tmp/tmp….yaml`) and runs `ansible-playbook` against that. Ansible `copy`
without `remote_src` looks up `src: k7d-fc/install-firecracker.sh` next to the
playbook file, i.e. `/tmp/k7d-fc/…`. The vendored files live at
`src/k7/deploy/k7d-fc/` in the source tree. The API-manifest copy already
documents this trap and uses `k7_repo_root`; k7d-fc did not.

**Fix:** copy from
`{{ k7_repo_root }}/src/k7/deploy/k7d-fc/{install-firecracker.sh,pins.env}`
on the controller (the checkout the CLI already requires for `k7-api:local`).

**Reference:** playbook comment on "Copy K7 API manifests on first master".

**Time lost:** one full 3-node TWO_DISK reset + 6 min install (~45 min).

---

## 19. `k7 create --docker` via the API always said "this k7d has no docker service"

**Symptom:** After a successful 3-node HA install of k7d 0.6.0 (payload at
`/usr/local/share/k7d/docker/bin/dockerd`, `/etc/k7/k7d_version` = `0.6.0`),
the docs path `k7 create --docker --backend k7d --egress-open builder ubuntu:24.04`
failed immediately with `this k7d has no docker service; upgrade`. The same
create via `k7 --core` (and the integration suite, which uses `--core`)
succeeded.

**Root cause:** `k7d_supports_docker()` treated a recorded version ≥ 0.6.0 as
"not old" and then still required `os.path.isfile` of the host dockerd
payload. k7-api hostPath-mounts `/etc/k7` (so it can read the version file)
but not `/usr/local/share/k7d`, so the payload check always failed inside the
API pod. Default CLI routing is the API, so every laptop/docs user hit this;
`--core` on the node never did.

**Fix:** if the playbook recorded a parseable version, that version is
authoritative (`>= 0.6.0` → supported). The payload path is only consulted
when the version file is missing (CLI `--core` / incomplete install).

**Reference:** `src/k7/deploy/manifests/k7-api/deployment.yaml` (`/etc/k7`
hostPath); `K7D_DOCKER_PAYLOAD_DOCKERD` in `src/k7/core/docker.py`.

**Time lost:** ~30 min diagnosing why the live cluster had dockerd but the
API refused `--docker`.
