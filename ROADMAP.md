# Project Roadmap

Where **K7** is headed — for contributors and operators.

---

## Current focus

Release engineering: apt/PPA, GHCR `k7-api`, and PyPI `k7-sdk`, after the
sibling [`Katakate/k7d`](https://github.com/Katakate/k7d) v0.1.0 artifact
exists (default install URL depends on it).

---

## Recently shipped

- [x] Multi-node Ansible, Longhorn topology, HA / cross-node tests
- [x] Cilium CNI + FQDN egress
- [x] API + SDK parity: pause / resume / fork
- [x] Snapshot lifecycle + GC; restore from VolumeSnapshot
- [x] CLI → API by default; `k7 api` / `k7 dev api rebuild`
- [x] Docker-in-VM sidecar + bench harness
- [x] Firecracker jailer
- [x] Python SDK as **`k7-sdk`** / `k7_sdk` (`katakate` deprecated)
- [x] `k7d` backend install path (artifact URL / local override)

---

## Next goals

- [ ] PPA (`apt install k7`), GHCR `k7-api`, PyPI `k7-sdk`
- [ ] Default `k7d` install from public `Katakate/k7d` GitHub Releases
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

*Last updated: August 2026*
