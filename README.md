<h1 align="center">k7</h1>

<p align="center">
  <b>Self-hosted secure VM sandboxes for AI compute at scale</b>
</p>


<p align="center">
  <a href="https://katakate.org"><img src="https://img.shields.io/badge/website-katakate.org-orange"></a>
  <a href="https://github.com/Katakate/k7/stargazers"><img src="https://img.shields.io/github/stars/Katakate/k7?style=social"></a>
  <a href="https://docs.katakate.org">
    <img src="https://img.shields.io/badge/docs-docs.katakate.org-orange" />
  </a>
</p>


<p align="center">
  <a href="https://news.ycombinator.com/item?id=45656952">
    <img src="https://img.shields.io/badge/Show%20HN-%231%20🔥-orange" alt="Show HN #1">
  </a>
  <a href="assets/show-hn_nb1_post-id-45656952.png" title="Screenshot proof">📸</a>
  <a href="https://console.dev">
    <img src="https://img.shields.io/badge/Featured%20on-Console.dev-blue" alt="Featured on Console.dev">
  </a>
  <a href="assets/k7-console-dev.png" title="Screenshot proof">📸</a>
  <a href="https://www.youtube.com/watch?v=2tgqzZvmbak">
    <img src="https://img.shields.io/badge/GitHub%20Trending-Oct%2023%2C%202025-black?logo=github" alt="GitHub Trending (Oct 23, 2025)">
  </a>
</p>


<p align="center">
  <img src="assets/k7-cover-upgrade.png" alt="Katakate Logo" width="3600" style="vertical-align: middle;"/>

</p>

<p align="center">
  <a href="https://deepwiki.com/Katakate/k7">
    <img src="https://deepwiki.com/badge.svg" />
  </a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg"></a>
  <img src="https://img.shields.io/badge/install%20with-apt-blue?logo=debian">
  <img src="https://img.shields.io/pypi/v/k7-sdk">
</p> 



<p align="center">
  <img src="assets/demo-k7.gif" alt="K7 Demo" width="900"/>
</p>





<i><b>Katakate</b></i> aims to make it easy to create, manage and orchestrate lightweight safe VM sandboxes for executing untrusted code, at scale. It is built on battle-tested VM isolation with Kata, Firecracker, QEMU, Longhorn, and Kubernetes — plus Katakate's own <i><b>k7d</b></i> runtime. It is orignally motivated by AI agents that need to run arbitrary code at scale but it is also great for:
- Custom serverless (like AWS Fargate, but yours)
- Hardened CI/CD runners (no Docker-in-Docker risks)
- Blockchain execution layers for AI dApps

> <b>100% open‑source</b> (Apache‑2.0). For technical support, write us at: hi@katakate.org

<h3 align="left">
The Tech Stack
</h3>

<i><b>Katakate</b></i> is built on:
- <i><b>Kubernetes</b></i> for orchestration, with K3s which is prod-ready and a great choice for edge nodes,
- <i><b>Kata</b></i> to encapsulate containers into light-weight virtual-machines,
- <i><b>Firecracker</b></i> (`kfd`) for super-fast boots, light footprints and minimal attack surface (with the jailer),
- <i><b>Devmapper Snapshotter</b></i> with <i><b>thin-pool provisioning of logical volumes</b></i> for efficient disk use across many Firecracker VMs per node,
- <i><b>QEMU</b></i> (`kql`) via Kata when you want a fuller VMM and durable sandbox disks,
- <i><b>Longhorn</b></i> for replicated PVC-backed root disks on the QEMU path — named snapshots, restore, disk-only fork, and cross-node mobility,
- <i><b>k7d</b></i> — Katakate's own microVM runtime daemon (<a href="https://github.com/Katakate/k7d">katakate/k7d</a>) with VM-level warm fork (CoW disk+memory) and in-place pause/resume.

<h3 align="left">
Sandbox backends
</h3>

`k7 install --backend <kfd|kql|k7d>` provisions one or more backends per node; `k7 create --backend …` picks one per sandbox. See [docs/BACKENDS.md](docs/BACKENDS.md) for the architecture and [PERFORMANCE.md](PERFORMANCE.md) for the full measurements (Hetzner AX41 node, medians).

| | `kfd` (kata-firecracker-devmapper) | `kql` (kata-qemu-longhorn) | `k7d` |
|---|---|---|---|
| VMM | Firecracker (Kata) | QEMU (Kata) | k7d (custom KVM VMM) |
| RuntimeClass | `kata` | `kata-qemu` | `k7` |
| Sandbox storage | devmapper thin-pool (needs a spare raw disk) | Longhorn PVC (replicated, persistent) | erofs images + reflink XFS + guest tmpfs |
| Create → Ready* | not re-measured† | 17.1s | **2.1s** |
| Named snapshot* | — | 6.5s (Longhorn, disk-only) | — (VM snapshot trees via the k7d API) |
| Fork → usable* | — | 46.7s (disk clone + cold boot) | **~5 ms VM CoW fork**; **~2.4 s** end-to-end via k7/k8s (pod Ready + exec) |
| Pause / resume* | scale to 0 / 1 | 1.3s / 4.1s (disk survives) | **0.2s / 0.3s (VM frozen in place, memory survives)** |
| Docker-in-VM sidecar | ✅ (ephemeral docker data) | ✅ (persistent docker data; fastest `docker pull`) | ✅ (VM-lifetime docker data) |
| Cross-pod persistence | ✗ | ✅ snapshots/restore | ✗ (fork carries state instead) |

\* medians of 3 on one Hetzner AX41 node — methodology, ranges, and the docker-sidecar
numbers are in [PERFORMANCE.md](PERFORMANCE.md).
† kfd needs a spare raw disk the benchmark node didn't have; its docker-workload numbers
are in the [PERFORMANCE.md](PERFORMANCE.md) spec-10b section.

<h3 align="left">
Also available today
</h3>

- 🛠️ Docker <code>build</code> / <code>run</code> inside VM sandboxes (docker sidecar on <b>kfd</b>, <b>kql</b>, and <b>k7d</b>; see [PERFORMANCE.md](PERFORMANCE.md))
- ⚡ <b>Warm VM fork</b> on the k7d backend: <code>k7 fork</code> CoW-clones a running sandbox's disk <i>and memory</i> in ~5&nbsp;ms at the VMM; end-to-end through k7/Kubernetes is ~2&nbsp;s to a Ready pod
- 🌐 Multi-node clusters (Ansible + Longhorn)
- 🔍 Cilium CNI with FQDN egress policies
- 📸 Pause / resume / fork / restore and <code>k7 snapshot</code> lifecycle
- 🐍 Python SDK: <code>pip install k7-sdk</code> (<code>katakate</code> package deprecated)

📋 **See [ROADMAP.md](ROADMAP.md) for upcoming work (GPU passthrough, …).**


<p align="left" style="margin-top: 40px;  font-size: 14px;">
   <strong>Note:</strong> Katakate is currently in <em>beta</em> and under security review. Use with caution for highly sensitive workloads.
</p>


# Usage

For usage you need:
- **Node(s)** that will host the VM sandboxes
- **Client** from where to send requests

We provide a:

- **CLI**: to use on the node(s) directly --> `apt install k7`
- **API**: deployed automatically by `k7 install` (toggle with `k7 api enable` / `k7 api disable`)
- **Python SDK**: HTTP client sync/async --> `pip install k7-sdk`

## Current requirements

### For the node(s)

- Ubuntu (amd64 or arm64) host.
  - **`k7d` backend is amd64 / x86_64 only** (same ISA; Debian calls it `amd64`,
    the release tarball is `*-x86_64-linux.tar.gz`). `kfd` and `kql` support
    amd64 and arm64.
- Hardware virtualization (KVM) available and accessible
  - Check: `ls /dev/kvm` should exist.
  - This is typically available on your own Linux machine.
  - On cloud providers, it varies. 
    - Hetzner (the only one I tested so far)  yes for their `Robot` instances only, i.e. "dedicated": robot.hetzner.com. 
    - AWS: only `.metal` EC2 instances. 
    - GCP: virtualization friendly, most instances, with `--enable-nested-virtualization` flag.
    - Azure: Dv3, Ev3, Dv4, Ev4, Dv5, Ev5 (Intel/AMD x86) or Dpdsv5, Dpldsv5, Epsv5 (ARM64).
    - DigitalOcean: Premium Intel and AMD droplets with nested virtualization enabled.
    - Others: in general, hardware virtualization is not exposed on cloud VPS, so you'll likely want a dedicated / bare metal.
- One raw disk (unformatted, unpartitioned) for the thin-pool that k7 will provision for efficient disk usage of sandboxes.
  - Use `./utils/wipe-disk.sh /your/disk` to wipe a disk clean before provisioning. DANGER: destructive - it will remove data/partitions/formatting/SWRAID.
- Ansible (for installer):
  ```bash
  sudo add-apt-repository universe -y
  sudo apt update
  sudo apt install -y ansible
  ```
- Docker and Docker Compose (for the API):
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```

Already tested setups:
  - Hetzner Robot dedicated with Ubuntu 24.04 and a **spare raw NVMe** for the `kfd` thin-pool. Dual-NVMe boxes (no third drive): install the OS on one disk only — see [tutorials/k7_hetzner_node_setup.md](tutorials/k7_hetzner_node_setup.md). (Older PDF that assumed an add-on third NVMe: [tutorials/k7_hetzner_node_setup.pdf](tutorials/k7_hetzner_node_setup.pdf).)

### For the client

Recent Python, or the **`k7`** CLI / **`k7-sdk`** from a Linux node or your laptop (API URL + key).

#### Development on macOS

The **`.deb` / PPA package is Linux-only** (amd64/arm64). On a MacBook:

- **CLI from source:** `./src/k7/cli/dev.sh` (same commands as `k7`; uses `uv` + `PYTHONPATH=src`)
- **API client from laptop:** set `K7_API_URL` and `K7_API_KEY`, then `dev.sh create` / `dev.sh list` (no `--core`)
- **`k7 install`** targets Linux servers with KVM — run on the node or via SSH, not on macOS locally
- **`pip install k7-sdk`** for Python scripts only

Do not install the Ubuntu `.deb` on macOS.

## Quick Start


### Get your node(s) ready

First install `k7` on your Linux server that will host the VMs:
```shell
sudo add-apt-repository ppa:katakate.org/k7
sudo apt update
sudo apt install k7
```


Then let `k7` get your node ready with everything:
```console
$  k7 install
Current task: Reminder about logging out and back in for group changes
  Installing K7 on 1 host(s)... ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:01:41
✅ Installation completed successfully!

```

Optionally pass `-v` for a verbose output.

> It will also tell you which raw disk was auto-selected for the LVM thin-pool. If you prefer, specify the disk explicitly (on a dual-NVMe Hetzner box this is usually the spare, e.g. `/dev/nvme1n1`):
> ```bash
> k7 install --disk /dev/nvme1n1
> ```

This will install and most importantly connect together the following components (depending on `--backend`):
- Kubernetes (K3s prod-ready distribution)
- Kata (for container virtualization)
- Firecracker + Jailer + devmapper thin-pool (`kfd`)
- QEMU via Kata + Longhorn PVC-backed roots (`kql`)
- k7d daemon + `containerd-shim-k7-v1` + RuntimeClass `k7` (`k7d`)


Careful design: config updates will not touch your existing Docker or containerd setups. We chose to use K3s' own containerd for minimal disruption. Installation may however overwrite existing installations of K3s, Kata, Firecracker, Jailer, QEMU/Kata config, or Longhorn. 

### CLI Usage

You can run workloads directly from the node(s) using the CLI. To create a sandbox, just create a yaml config for it. 

#### k7.yaml example:

```yaml
name: my-sandbox-123
image: alpine:latest
namespace: default

# Optional: restrict egress (safe pattern: whitelist only your own egress proxy IP)
egress_whitelist:
  - "10.0.0.5/32"     # Your private egress proxy/gateway

# Optional: resource limits
limits:
  cpu: "1"
  memory: "1Gi"
  ephemeral-storage: "2Gi"

# Optional: run before_script inside the container once at start. Network restrictions apply after the before-script, so you can install packages here, pull git repos, etc
before_script: |
  apk add --no-cache git curl

# Optional: load environment variables from a file. These will be available both during the before-script, and in the sandbox
env_file: path/to/your/secrets/.env
```


#### Running commands


```bash
# Create a sandbox (uses k7.yaml in the current directory by default, but you can also pass: -f myfile.yaml)
k7 create

# Or pick a backend explicitly (kfd | kql | k7d — aliases for the full names)
k7 create -f k7.yaml --backend k7d

# List sandboxes
k7 list

# Delete a sandbox
k7 delete my-sandbox-123

# Delete all sandboxes. You can also pass a namespace
k7 delete-all
```

#### Fork / pause / snapshot

```bash
# Warm CoW fork (disk + memory) — source must be a k7d sandbox
k7 create -f k7.yaml --backend k7d          # name from yaml, e.g. my-sandbox-123
k7 exec my-sandbox-123 sh -c 'echo hi > /tmp/state.txt'
k7 fork my-sandbox-123 branch-a
k7 exec branch-a cat /tmp/state.txt        # inherited memory + disk

# Disk-only fork (cold boot from cloned PVC) — kql / kata-qemu-longhorn
k7 create -f k7.yaml --backend kql
k7 fork my-sandbox-123 branch-b
# optional: pin the Longhorn VolumeSnapshot name used for the clone
k7 fork my-sandbox-123 branch-c --snapshot my-snap

# Parallel branches from one base
for i in $(seq 0 7); do k7 fork my-sandbox-123 exp-$i & done; wait

# Pause / resume (kql keeps the PVC; k7d freezes the live VM)
k7 pause my-sandbox-123
k7 resume my-sandbox-123

# Named disk snapshot without pausing (kql)
k7 snapshot create my-sandbox-123 my-named-snap
```

On **k7d**, the VMM fork itself is ~5&nbsp;ms; end-to-end through Kubernetes
to a Ready pod is ~2&nbsp;s. On **kql**, fork is a Longhorn snapshot + PVC
clone + cold boot (~45&nbsp;s). See [PERFORMANCE.md](PERFORMANCE.md) and
[docs/BACKENDS.md](docs/BACKENDS.md).

### API usage

The K7 API is deployed automatically by `k7 install` as the `k7-api`
Deployment in `kube-system`. K3s keeps it running on its own; there's no
separate "start" step.

```shell
# Check status + endpoint
k7 api status
k7 api endpoint

# Generate API key
k7 generate-api-key my-key1

# Temporarily disable / re-enable
k7 api disable
k7 api enable
```

Generating / listing / revoking keys talks to `/etc/k7/api_keys.json`, so
those subcommands need to run on the node (typically `sudo` or `root`).


### Python SDK Usage

After your k7 API is up, usage is very simple.

Install the Python SDK via:
```shell
pip install k7-sdk
```

Or if you want async support:
```shell
pip install "k7-sdk[async]"
```

The legacy `katakate` PyPI name remains as a one-release shim that re-exports `k7_sdk` with a deprecation warning.

Then use with:
```python
from k7_sdk import Client

k7 = Client(
  endpoint='https://<your-endpoint>', 
  api_key='your-key')

# Create sandbox (pick backend: kata-firecracker-devmapper | kata-qemu-longhorn | k7d)
sb = k7.create({
    "name": "base",
    "image": "alpine:latest",
    "backend": "k7d",
})

# Execute code
result = sb.exec('echo "Hello World" > /tmp/hi.txt && cat /tmp/hi.txt')
print(result['stdout'])

# Fork: k7d = warm CoW (disk + memory); kql = disk clone + cold boot
branch = sb.fork("branch-a")
print(branch.exec("cat /tmp/hi.txt")["stdout"])  # still there on k7d

# Parallel exploration
forks = [sb.fork(f"exp-{i}") for i in range(4)]

# List / delete
sandboxes = k7.list()
sb.delete()
```

#### Async variant
```python
import asyncio
from k7_sdk import AsyncClient

async def main():
    k7 = AsyncClient(
      endpoint='https://<your-endpoint>', 
      api_key='your-key'
    )
    print(await k7.list())
    await k7.aclose()

asyncio.run(main())
```

  
### Tutorials

- LangChain ReAct agent with a K7 sandbox tool
  - Path: tutorials/langchain-react-agent
  - Setup: copy .env.example to .env and fill K7_ENDPOINT/K7_API_KEY/OPENAI_API_KEY
  - Run: python agent.py
  - Try asking it anything! e.g. "List files from '/'"

## Build from source


First install make if not already available:
```bash
sudo add-apt-repository universe -y
sudo apt update
sudo apt install make
```


To build the `k7` CLI and API into `.deb` package:
```shell
make build
```

You can then install it with:
```shell
sudo make install
```

To uninstall later:
```shell
sudo make uninstall
```

Note: we recommend running `make uninstall` before reinstalling if it is not your first install, to avoid stale copies of cached files in the .deb package.


### Build and run the API container

Local dev image:
```bash
# Build the API image locally
make api-build-local

# Run API using local image (no pull)
make api-run-local
```


### Build the k7-sdk Python SDK from source

Preferred (uv):

```bash
# create env
uv venv .venv-build
. .venv-build/bin/activate

# install directly from source in editable mode
uv pip install -e .
```


## Security

K7 sandboxes are hardened by default with multiple layers of security:

- **VM isolation**: Kata Containers (Firecracker or QEMU) or the k7d RuntimeClass provide hardware-level isolation via lightweight VMs
  - On `kfd`, Firecracker processes are further restricted into a chroot using the Jailer
  - Kata's Seccomp restrictions are enabled on the Kata backends
  - `kql` uses QEMU + Longhorn for durable, cross-node-mobile disks; `k7d` has its own CoW-fork isolation trade-offs (see k7d `SECURITY.md`)

- **Linux capabilities**: All capabilities are dropped by default (`drop: ALL`) for defense-in-depth
  - Only explicitly add back capabilities you need via `cap_add` parameter
  - `allow_privilege_escalation` is always set to `false`
  - Seccomp profile: `RuntimeDefault`

- **Non-root execution**: Optionally run containers and pods as non-root user (UID 65532):
  - `container_non_root`: Run the main container as non-root and disable privilege escalation
  - `pod_non_root`: Run the entire pod as non-root with consistent filesystem ownership (UID/GID/FSGroup 65532)

- **API security**:
  - API keys stored as SHA256 hashes with timing-attack-resistant comparison
  - Expiry enforced; last-used timestamp recorded
  - File-based storage with 600 permissions (`/etc/k7/api_keys.json` by default)

- **Network policies**: Complete network isolation for VM sandboxes
  - **Ingress isolation**: All inter-VM communication is blocked by default to prevent sandbox-to-sandbox access
  - **Egress lockdown**: per-sandbox allowlists — CIDRs via Kubernetes NetworkPolicy, or **FQDN / domain** allowlists via Cilium (`CiliumNetworkPolicy`; default CNI)
  - **DNS is blocked** when egress is locked down; only entries in `egress_whitelist` (CIDR or domain) are reachable
  - Administrative access via `kubectl exec` and `k7 shell` is preserved (uses Kubernetes API, not pod networking)

More security features are on the roadmap (e.g. AppArmor).

## Packaging & Releases

- Layout uses `src/`:
  - CLI, API, core live under `src/k7/`
  - SDK under `src/k7_sdk/` (PyPI package `k7-sdk`; `src/katakate/` is a deprecation shim)
- Root `setup.py` publishes the SDK; assets under `src/k7/` belong to the Debian CLI / API image, not the PyPI wheel.
- User docs: `~/docs/k7/` (Mintlify). See `docs/README.md` in this repo.
- The CLI Debian package is built via `src/k7/cli/build.sh` and produces `dist/k7_<version>_amd64.deb` and `dist/k7_<version>_arm64.deb`.
- CI (tags `v*`) can publish the PyPI SDK and upload the `.deb` artifact.