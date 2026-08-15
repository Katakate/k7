# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
