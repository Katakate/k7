# Docker workload benchmark — Spec 10b

Quantifies the **storage tax** of running Docker inside a k7 sandbox vs
natively on the host. Output lands in [`PERFORMANCE.md`](../../PERFORMANCE.md)
under a dated section, formatted so the median + ratio numbers can be
cited verbatim from any blog post.

## What gets benched

| Label       | What it is |
|-------------|------------|
| `host`      | Native Docker on the Hetzner node (no k7 involved) |
| `k7-fd`     | k7 sandbox, `kata-firecracker-devmapper` (kfd) backend, `--sidecar docker` (docker daemon's `/var/lib/docker` is an emptyDir) |
| `k7-ql-r1`  | k7 sandbox, `kata-qemu-longhorn` (kql) backend, `--sidecar docker`, Longhorn `replicas=1` |
| `k7-ql-r2`  | Same as `k7-ql-r1` but Longhorn `replicas=2` (requires ≥ 2-node cluster) |

The four environments stack the storage path cleanly: `host` → no VM,
no Longhorn. `k7-fd` → VM but no Longhorn. `k7-ql-r1` → VM + one local
Longhorn replica. `k7-ql-r2` → VM + one local + one cross-node Longhorn
replica. Subtracting `k7-fd / host` from `k7-ql-r1 / host` isolates the
**Longhorn tax** from the **kata-qemu tax**.

## Workloads (5 runs each, after a discarded warm-up)

| Operation | Isolates |
|-----------|----------|
| `pull debian:12-slim` | Network + extract + write of an external image |
| `build (no-cache)` | The headline number: apt + pip + git + 256 MB fsync |
| `build (cached)` | Sanity — should be sub-second; non-zero on `k7-ql-*` would mean the bind mount of `/var/lib/docker` is broken |
| `run cpu (10s budget)` | Kata-qemu CPU overhead (Python busy-loop) |
| `run io (2k small + 512 MB fsync)` | Write path through the storage stack |
| `run read (venv tree cat)` | Read path (mostly page-cache hits after the first call) |

The Dockerfile that drives `build (no-cache)` is pinned at
[`bench/docker-perf/bench.Dockerfile`](bench.Dockerfile) — apt + pip
generate thousands of small files (metadata pressure), git clone is
inode-heavy, and `dd … conv=fsync` measures sync-write throughput.

## How to run it

The bench is a **pytest module** (not a shell script) — it reuses the
exact same `k7_core` / `test_namespace` fixtures that
`tests/integration/test_sidecar_docker.py` already uses to spin up a
`docker:27.5-cli` sandbox with `--sidecar docker`. The harness times
`k7_core.exec_command(...)` calls instead of doing anything new.

Run all three envs:

```bash
# On the cluster node (or any host that already runs the integration suite):
K7_BENCH_ENVS=host,k7-ql-r1,k7-ql-r2 \
uv run pytest -m bench tests/integration/bench_docker_perf.py -v -s
```

Pick a subset (useful for iterating on tooling):

```bash
K7_BENCH_ENVS=host             uv run pytest -m bench -v -s tests/integration/bench_docker_perf.py
K7_BENCH_ENVS=k7-ql-r1         uv run pytest -m bench -v -s tests/integration/bench_docker_perf.py
```

Switching `r1 → r2` is done **in-cluster** by patching Longhorn's
`default-replica-count` setting via `kubectl` — no `k7 install` reinstall,
no cluster-wide downtime, and the value is restored at session teardown.

### Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `K7_BENCH_ENVS` | `host,k7-ql-r1,k7-ql-r2` | Comma-separated subset of envs to run |
| `K7_BENCH_REPS` | `5` | Reps per cell |
| `K7_BENCH_WARMUP` | `1` | 0 to disable the warm-up |
| `K7_BENCH_OUT` | `/tmp` | Where logs + the aggregated CSV land |

### Output

Each leg writes a `bench-<label>-<ts>.log` of the same shape `render.py`
already parses:

```
# label=k7-ql-r1
# timestamp=20260529T120000Z
# kernel=...
# docker_version=...
# longhorn_replicas=1
# columns: OPERATION  RUN  SECONDS
pull                     1 3.142
pull                     2 3.098
build_nocache            1 60.42
...
```

When the pytest session ends, a session-scoped fixture aggregates all
logs from `bench_logs` into a single CSV at
`$K7_BENCH_OUT/bench-results-<ts>.csv`. Feed that to `render.py`:

```bash
uv run python bench/docker-perf/render.py \
    --input /tmp/bench-results-<ts>.csv \
    --title "Docker workloads inside sandboxes — 2026-05-29 (k7 <sha>)" \
    --hardware "Hardware: 2x Hetzner AX42, Ubuntu 24.04, kernel 6.8, Docker 27.5, Longhorn 1.7.x." \
    --output - >> PERFORMANCE.md
```

## Known confounds (acknowledge these honestly in any post)

1. **Page cache** — the harness drops `/proc/sys/vm/drop_caches` only on
   the host (where it has the privilege). Inside the sandbox we can't,
   so the `run read` leg is mostly a page-cache measurement, not a
   storage-stack one. Don't over-interpret that row.
2. **Network egress for `pull`** — Cilium FQDN egress + the sandbox's
   network path aren't the same as the host's. `pull` is here for
   context, not as a storage-tax signal.
3. **Kata-qemu CPU overhead** — the `run cpu` row reflects both CPU
   virtualization overhead and any scheduler differences. Reporting it
   explicitly lets readers do their own subtraction.
4. **Workload immutability** — the Dockerfile is committed and the base
   image (`debian:12-slim`) is captured by digest in each log header.
   The only real variable across re-runs is the k7 / Longhorn version.

## Statistical handling

- 5 reps per cell, first warm-up run is discarded.
- Reported as **median** + **min/max** (mean is too sensitive to outliers
  on storage benchmarks).
- Cells with `(max − min) / median > 0.30` are flagged with `⚠`. Treat
  flagged cells as "rerun with more reps before publishing" — don't
  quote them.

## Re-rendering without re-running

Already have a CSV (manually edited, or salvaged from a partial run)?
Re-render with:

```bash
python3 bench/docker-perf/render.py \
    --input /tmp/bench-results-<ts>.csv \
    --title "Docker workloads inside sandboxes — 2026-05-29 (k7 <sha>)" \
    --hardware "Hardware: ..." \
    --output /tmp/section.md
```

That same output is what `bench_docker_perf.py`'s teardown prints
instructions for — re-rendering is just the path for fixing typos in
the title / hardware line without re-running the bench.
