<p align="center">
<span style="font-family: 'Georgia', sans-serif; font-weight: bold; font-size: 48px; font-style: italic; color: #ef672b; vertical-align: middle; margin-left: 10px;">
    KATAKATE
  </span>
</p>


<p align="center" style="font-weight: bold; font-size: 20px; ">
  Self-hosted secure VM sandboxes for AI compute at scale
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
    <img src="https://img.shields.io/badge/Show%20HN-%231%20🔥-FF6600" alt="Show HN #1">
  </a>
  <a href="assets/show-hn_nb1_post-id-45656952.png" title="Screenshot proof">📸</a>
  <a href="https://console.dev">
    <img src="https://img.shields.io/badge/Featured-Console.dev-4F39F5" alt="Featured on Console.dev">
  </a>
  <a href="assets/k7-console-dev.png" title="Screenshot proof">📸</a>
  <a href="https://changelog.com/news/169">
    <img src="https://img.shields.io/badge/Featured-Changelog-59B287" alt="Featured on Changelog">
  </a>
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
  <img src="https://img.shields.io/pypi/v/katakate">
</p> 



<p align="center">
  <img src="assets/demo-k7.gif" alt="K7 Demo" width="900"/>
</p>





<i><b>Katakate</b></i> aims to make it easy to create, manage and orchestrate lightweight safe VM sandboxes for executing untrusted code, at scale. It is built on battle-tested VM isolation with Kata, Firecracker and Kubernetes. It is orignally motivated by AI agents that need to run arbitrary code at scale but it is also great for:
- Custom serverless (like AWS Fargate, but yours)
- Hardened CI/CD runners (no Docker-in-Docker risks)
- Blockchain execution layers for AI dApps

> <b>100% open‑source</b> (Apache‑2.0). For technical support, write us at: hi@katakate.org</b>

<h3 align="left">
The Tech Stack
</h3>

<i><b>Katakate</b></i> is built on:
- <i><b>Kubernetes</b></i> for orchestration, with K3s which is prod-ready and a great choice for edge nodes,
- <i><b>Kata</b></i> to encapsulate containers into light-weight virtual-machines,
- <i><b>Firecracker</b></i> as the chosen VM, for super-fast boots, light footprints and minimal attack surface,
- <i><b>Devmapper Snapshotter</b></i> with <i><b>thin-pool provisioning of logical volumes</b></i> for efficient use of disk space shared by dozens of VMs per node.


<h3 align="left">
Coming Soon
</h3>


- 🛠️ Docker <code>build</code> / <code>run</code> / <code>compose</code> support <b><i>inside the VM sandbox</i></b>
- 🌐 Multi-node cluster capabilities for distributed workloads
- 🔍 Cilium FQDN-based DNS resolution to safely whitelist domains, not just IP blocks
- ⚙️ Support other VMM such as Qemu for GPU workloads

📋 **See [ROADMAP.md](ROADMAP.md) for the complete feature roadmap and project priorities.**


<p align="left" style="margin-top: 40px;  font-size: 14px;">
   <strong>Note:</strong> Katakate is currently in <em>beta</em> and under security review. Use with caution for highly sensitive workloads.
</p>


# Usage

For usage you need:
- **Node(s)** that will host the VM sandboxes
- **Client** from where to send requests

We provide a:

- **CLI**: to use on the node(s) directly --> `apt install k7`
- **API**: deployed on the (master) node(s) --> `k7 start-api`
- **Python SDK**: Python client sync/async talking to API --> `pip install katakate`

## Current requirements

### For the node(s)

- Ubuntu (amd64 or arm64) host.
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
  - Hetzner Robot instance with Ubuntu 24.04, x86_64 or ARM64 arch, booked with 1 extra empty disk `nvme2n1` for the thin-pool provisioning. See the setup guide (PDF): [tutorials/k7_hetzner_node_setup.pdf](tutorials/k7_hetzner_node_setup.pdf).

### For the client

Just recent Python.

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

> It will also tell you which raw disk was auto-selected for the LVM thin-pool. If you prefer, specify the disk explicitly:
> ```bash
> k7 install --disk /dev/nvme2n1
> ```

This will install and most importantly connect together the following components:
- Kubernetes (K3s prod-ready distribution)
- Kata (for container virtualization)
- Firecracker (as Virtual Machine Manager)
- Jailer (to secure Firecracker VMs further into a chroot)
- devmapper snapshotter with thin-pool provisioning of logical volumes for VM efficient disk memory usage


Careful design: config updates will not touch your existing Docker or containerd setups. We chose to use K3s' own containerd for minimal disruption. Installation may however overwrite existing installations of K3s, Kata, Firecracker, Jailer. 

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

# List sandboxes
k7 list

# Delete a sandbox
k7 delete my-sandbox-123

# Delete all sandboxes. You can also pass a namespace
k7 delete-all
```

### API usage

If you'd like to manage workloads remotely, just use the API:

```shell
# Start API server (containerized and SSL support with Cloudflared)
k7 start-api

# Generate API key
k7 generate-api-key my-key1
```

Make sure your user is in the `Docker` group to be allowed to start or stop the API.

As for generating / listing / revoking keys, you might need `sudo` or `root`. 

### Python SDK Usage

After your k7 API is up, usage is very simple.

Install the Python SDK via:
```shell
pip install katakate
```

Or if you want async support:
```shell
pip install "katakate[async-sdk]"
```

Then use with:
```python
from katakate import Client

k7 = Client(
  endpoint='https://<your-endpoint>', 
  api_key='your-key')

# Create sandbox
sb = k7.create({
    "name": "my-sandbox",
    "image": "alpine:latest"
})

# Execute code
result = sb.exec('echo "Hello World"')
print(result['stdout'])

# List all sandboxes
sandboxes = k7.list()

# Delete sandbox
sb.delete()
```

#### Async variant
```python
import asyncio
from katakate import AsyncClient

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

### Fast development workflow

For faster development iterations when working on the CLI, you can use `dev.sh` to run `k7` commands directly without rebuilding the binary:

```shell
# Basic commands
./src/k7/cli/dev.sh install
./src/k7/cli/dev.sh list
./src/k7/cli/dev.sh create

# Install with options
./src/k7/cli/dev.sh install -v
./src/k7/cli/dev.sh install --disk /dev/nvme2n1


# Create sandbox with options
./src/k7/cli/dev.sh create --name test --image alpine:latest
```

This script uses `uv run` to execute the CLI with all dependencies, so you can test changes immediately without running `make build` every time. This is especially useful when iterating on CLI code changes.

### Build and run the API container

Local dev image:
```bash
# Build the API image locally
make api-build-local

# Run API using local image (no pull)
make api-run-local
```


### Build the katakate Python SDK from source

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

- **VM isolation**: Kata Containers provide hardware-level isolation via lightweight VMs with Firecracker
  - VMs are further restricted into a chroot using Jailer
  - Kata's Seccomp restrictions are enabled

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
  - **Egress lockdown**: Control outbound traffic with CIDR-based restrictions using Kubernetes NetworkPolicies
  - **DNS is blocked** when egress is locked down; only IPs/CIDRs in `egress_whitelist` are reachable
  - Administrative access via `kubectl exec` and `k7 shell` is preserved (uses Kubernetes API, not pod networking)
  - Soon to come: Cilium integration for domain name whitelisting

More security features are currently on the roadmap, including integrating AppArmor.

## Packaging & Releases

- Layout uses `src/`:
  - CLI, API, core live under `src/k7/`
  - SDK under `src/katakate/`
- Root packaging targets the `katakate` SDK only; assets under `src/k7/` are not part of the PyPI distribution.
- `MANIFEST.in` (for the `katakate` SDK) should include essentials like `LICENSE` and `README.md` only; deploy assets from `src/k7/deploy/*` belong to the Debian/CLI packaging flow, not to the PyPI package.
- `setup.py` for `katakate` lives at repo root; packages from `src/`.
- The CLI Debian package is built via `src/k7/cli/build.sh` and produces `dist/k7_<version>_amd64.deb` and `dist/k7_<version>_arm64.deb`.
- CI (tags `v*`) can publish the PyPI SDK and upload the `.deb` artifact.



## Known issues

- Jailer seems to be currently ignored by Kata despite being passed correctly into its configuration, and despite the Jailer process being started. The use of Kubernetes secrets could be a reason of incompatibility. This is under investigation.  