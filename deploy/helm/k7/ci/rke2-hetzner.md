# Target A: RKE2 + Cilium + Longhorn on one Hetzner node

The infra-as-code for the Helm chart's validation cluster. `rke2-node.sh` (next
to this file) holds every command; each section below explains one of its
steps. `rke2-node.sh all` runs them in order, as root, on a freshly reset
Ubuntu 24.04 node (two disks: OS on `nvme0n1`, `nvme1n1` left raw for kfd). The chart installs none of this —
it is the customer's cluster. Versions are the playbook's pins
(`src/k7/deploy/k7-install-node.yaml`) where one exists.

| Component | Version |
|-----------|---------|
| RKE2 | `v1.36.5+rke2r1` (same Kubernetes minor as the playbook's K3s) |
| Cilium (via cilium CLI `v0.19.2`) | `1.19.2`, the playbook's flags |
| Longhorn | `v1.10.1` |
| CSI snapshot CRDs + controller | bundled with RKE2 (`rke2-snapshot-controller`) |
| OpenEBS LVM LocalPV (kfd `--docker`) | `1.10.1` |
| helm | `v3.16.4` |

## 1. Host packages

```bash
deploy/helm/k7/ci/rke2-node.sh packages
```

## 2. RKE2 server, no CNI, no kube-proxy

Cilium replaces both; K7 needs no ingress controller. RKE2 bundles the
CSI snapshot CRDs and snapshot-controller (step 4 uses them). The
`node-pool` label opts the node into
`k7-node-installer`. The `localhost:5000` mirror is validation-only (it
lets the node pull the test images of step 7); a real cluster pulls the
released digests from ghcr.io.

```bash
deploy/helm/k7/ci/rke2-node.sh rke2
```

## 3. Cilium

Same flags as `roles/k7_cluster/tasks/cilium.yml`; `k8sServiceHost` is
this node because there is no kube-proxy to serve the kubernetes Service.

```bash
deploy/helm/k7/ci/rke2-node.sh cilium
```

## 4. Longhorn, CSI snapshots, VolumeSnapshotClass (kql)

```bash
deploy/helm/k7/ci/rke2-node.sh longhorn
```

## 5. OpenEBS LVM LocalPV (kfd `--docker` graph volumes)

Same values as `roles/k7_cluster/tasks/api.yml`. Its node plugin waits
for the `backend-kata-firecracker-devmapper` label, which
`k7-node-installer` sets once `kata-vg` exists.

```bash
deploy/helm/k7/ci/rke2-node.sh openebs
```

## 6. Namespace and API TLS certificate

The release namespace runs the privileged installer, so it needs the
`privileged` Pod Security level (the pre-install hook checks). `tls.mode=secret`
with a self-signed CA here; use cert-manager (`tls.mode=certManager`) on a
real cluster.

```bash
deploy/helm/k7/ci/rke2-node.sh tls
```

## 7. Test images (validation only, never published)

The chart takes image digests. Until the release pipeline publishes them, build both
images from the checkout at `/root/k7` into a registry bound to
`127.0.0.1:5000`. Docker runs without iptables so it cannot touch
Cilium's datapath.

```bash
deploy/helm/k7/ci/rke2-node.sh images
```

## 8. Install the chart

`ci/rke2-cilium-values.yaml` is the values file; the digests come from
step 7. The chart has no default for `api.mode` and refuses to render
while `observability.metrics` is on without `observability.scraperSelector`.
This file sets `api.mode=development` (this validation cluster; `production`
is the other choice) and the kube-prometheus-stack scraper
(`monitoring`, `app.kubernetes.io/name=prometheus`). Metrics stay on, and
only those pods may reach port 9464. Host-side tooling (the CLI, the
integration suite) reads `/etc/k7/config`, `/etc/k7/api_endpoint`,
`/etc/k7/tls/ca.crt` and `/etc/k7/agent_token` (root-only, from Secret
`k7-agent-token`).

```bash
deploy/helm/k7/ci/rke2-node.sh chart test
```

Then, from the workstation:
`K7_KUBECONFIG=/etc/rancher/rke2/rke2.yaml K7_SYSTEM_NAMESPACE=k7-system make test-integration-remote`.

## 9. `kubernetes` provider, upgrade, uninstall

Automated as the opt-in destructive integration phase
(`tests/integration/test_destructive.py`, `pytest -m destructive` with
`K7_DESTRUCTIVE=1`; the hardware CI runs it as phase `destructive` after
`rke2 test`):

- an upgrade that leaves the installer config alone restarts neither
  containerd nor the installer, and `helm test` still passes;
- `networkPolicy.provider=kubernetes` is refused without
  `acknowledgeNoPlatformDeny`; with it the Cilium policies go and the API
  runs `cidr-only`, without a containerd restart; switching back restores them;
- `helm uninstall --wait` with `forceCleanup` and a live sandbox: the
  cleanup ServiceAccount's admission policy refuses non-sandbox and
  other-node pods, lets cleanup delete the sandbox pod, and no K7 object,
  node label or `/var/lib/k7` survives.

After the uninstall, remove the host files step 8 wrote: `rm -rf /etc/k7`.
