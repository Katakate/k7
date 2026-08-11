# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
