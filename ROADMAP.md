# Project Roadmap

Where **K7** is headed — for contributors and operators.

---

## Current focus

Release engineering: keep the apt/PPA, GHCR `k7-api`, and PyPI `k7-sdk`
pipelines current with `main` (PPA, GitHub `.deb`, and PyPI `k7-sdk` are **0.4.0**).

---

## Recently shipped

- [x] Multi-node Ansible, Longhorn topology, HA / cross-node tests
- [x] Cilium CNI + FQDN egress
- [x] API + SDK parity: pause / resume / fork
- [x] Snapshot lifecycle + GC; restore from VolumeSnapshot
- [x] CLI → API by default; `k7 api` / `k7 dev api rebuild`
- [x] Docker-in-VM sidecar + bench harness
- [x] First-class `--docker` (guest dockerd on k7d / k7d-fc; docker-vehicle on Kata)
- [x] Firecracker jailer
- [x] `k7d-fc` backend (k7d driving stock Firecracker + jailer)
- [x] Python SDK as **`k7-sdk`** / `k7_sdk` (`katakate` deprecated)
- [x] `k7d` backend install path (public `Katakate/k7d` GitHub Releases; playbook pin 0.7.0)
- [x] HTTPS-by-default for `k7-api` (Caddy sidecar, cluster CA)
- [x] Network security hardening: cluster-wide sandbox→platform
  isolation, opt-in sandbox ingress / `--expose-port`, Hubble, and a
  source-CIDR allowlist for the API NodePort. Tetragon was evaluated and
  rejected

---

## Next goals

- [x] PPA / GHCR / PyPI cut of the unreleased work (HTTPS, `--docker`, `k7d-fc`)
- [ ] Optional macOS CLI artifacts (tarball / Homebrew) — after the above

---

## Future work

- [ ] GPU passthrough support
- [ ] Cross-node mobility of snapshots / forks for the `k7d` backend
  (`kql` already moves state across nodes via Longhorn)
- [ ] AppArmor integration
- [ ] CI/CD deployment tests on every public tag
- [ ] TEE support; custom rootfs; persistent in-API interpreter

---

## How to contribute

1. Open a [Discussion](https://github.com/Katakate/k7/discussions) or
   [Issue](https://github.com/Katakate/k7/issues)
2. Reference the roadmap item
3. See [`CONTRIBUTING.md`](CONTRIBUTING.md)

---

*Last updated: September 2026*
