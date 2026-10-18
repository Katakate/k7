# Metering and usage

K7 measures what each tenant and sandbox uses and emits it as usage events
and Prometheus counters. It does not price anything: rating, invoices and
billing belong to whatever consumes the events (OpenMeter, a data
warehouse, the hosted control plane).

## Meters

| Meter | Unit | Measured by | Dimensions |
|---|---|---|---|
| `sandbox_seconds` | s | node sampler (k7-agent) | tenant, namespace, sandbox, sandbox_uid, backend, state, size class |
| `sandbox_vcpu_seconds` | reserved vCPU·s | node sampler | same |
| `sandbox_memory_gib_seconds` | reserved GiB·s | node sampler | same |
| `sandbox_disk_gib_seconds` | provisioned GiB·s | storage sampler (k7-api leader) | tenant, namespace, sandbox, backend, state |
| `snapshot_gib_seconds` | GiB·s | storage sampler | tenant, namespace, snapshot, kind, location |
| `sandbox_egress_bytes` | bytes | node sampler | tenant, namespace, sandbox, backend, scope |

- **Reserved, not used.** vCPU and memory are the sandbox container's
  limits, stamped on the Deployment at create, fork and restore
  (`k7.katakate.org/vcpu`, `k7.katakate.org/memory-bytes`). Actual CPU and
  memory stay in `k7 top`; they are not meters.
- **Size class** is `<cpu>c-<memGi>g` (for example `2c-4g`, `0.5c-0.5g`),
  or `unsized` for a sandbox created without both limits.
- **sandbox_uid** is a fresh UUID per sandbox
  (`k7.katakate.org/sandbox-uid`). A fork or restore gets its own, so it
  never shares windows with its source.

### States

| State | What it is | Accrues |
|---|---|---|
| `running` | the pod runs and the VM is not paused | seconds, vCPU·s, GiB·s, disk |
| `paused` | a k7d VM frozen in place, memory resident | seconds and GiB·s, **0 vCPU·s**, disk |
| `stopped` | a kql or kfd sandbox scaled to 0 (no pod) | disk only (storage sampler) |

## How it is measured

### Node sampler (k7-agent, every node)

Every `sampleInterval` seconds (5-60; 15 in development, explicit in
production) k7-agent lists the node's ready sandbox pods with
`crictl pods`, and their network counters with `crictl statsp`.

- **Windows.** Each sample closes `[t_prev, t_now)` for every pod.
  Seconds come from the monotonic clock and stamps from the wall clock, so
  an NTP step moves `time` but not `seconds`. NTP is still required: it
  keeps windows comparable across nodes.
- **State changes.** k7-agent (or `k7 --core` on the node) records the time
  of every k7d pause and resume in `pause-transitions.json` in the spool
  directory. A window that spans a recorded change is split there exactly.
  A change with no record is split at the midpoint, and both halves carry
  `approximate: true`.
- **Deleted pods.** A pod gone since the last sample is charged up to the
  midpoint of the two samples, flagged `approximate`.
- **Gaps.** Time no sampler saw is never filled in. On the first sample
  after metering starts, every pod already running gets a
  `sandbox.usage.gap` event (`reason: metering-start`) from its creation.
  After an agent restart, each pod gets a gap (`agent-restart`) from the
  last saved sample to now, for `sandbox` and, where it was counted,
  `egress`. No window is emitted twice.
- **Egress.** `txBytes` of the pod netns default interface (eth0) from CRI
  `PodSandboxStats`. A Kata netns also holds the VM's tap device, whose
  traffic crosses eth0 too, so only eth0 counts. A counter that goes
  backwards (pod restart) is counted from zero and flagged
  `counter_reset`; a new pod sandbox id starts from zero. A pod whose stats
  come back empty marks its backend `egress_unavailable` in k7-agent
  `/health` and in `k7_usage_egress_unavailable{backend}`. It is never
  reported as 0 bytes.
- **Egress counts frames, headers included.** How much the headers add
  depends on the segment size the interface sees. Kata and k7d-fc pods
  hand eth0 64 KiB GSO segments, so for a bulk TCP transfer the meter is
  about 0.1% above the payload. k7d pods send MTU-sized frames (66 header
  bytes per 1448 payload bytes), so it is about 4.6% above the payload.
- **Scope.** Only `scope: total` is emitted. `external` (traffic leaving
  the cluster CIDRs) is reserved in the schema for Hubble flow metrics and
  is not emitted yet.

### Storage sampler (k7-api, one leader)

The k7-api replica holding Lease `k7-metering-leader` runs every
`storageInterval` seconds (30-600, default 60). Windows are aligned to
multiples of the interval, so a new leader computes the same event ids
for the same window. The last emitted boundary is kept in ConfigMap
`k7-metering-state`; windows skipped during a leader change become
`leader-change` gap events.

- **Disk** is what each sandbox has provisioned: PVC capacity (kql),
  ephemeral volume claims, `ephemeral-storage` limits, and the 10 GB
  devmapper root of a running kfd sandbox. A stopped sandbox keeps its
  PVC, so it keeps accruing disk.
- **Snapshots**: every VolumeSnapshot, sized by `status.restoreSize` (or
  its source PVC until that is set), `kind: volume`, `location: local`.
- An object that cannot be sized (missing PVC) is an error in k7-api
  `/health` (`metering.error`); the other objects' windows are still
  emitted.
- The events go to the spool of the k7-agent on the leader's node, which
  delivers them with the same sinks.

Not metered yet, because their sources are not in K7 yet: k7d writable
disks (reported in `/health` as `disk_unavailable`, never as 0), k7d
snapshots (`kind: k7d`) and exported bundles (`location: exported`).

## Events

Every event is a CloudEvents 1.0 JSON object, structured mode:

| Field | Value |
|---|---|
| `specversion` | `1.0` |
| `type` | one of the types below |
| `source` | `k7://<cluster-id>/node/<node>` (node sampler) or `k7://<cluster-id>/storage` |
| `subject` | `<tenant>/<namespace>/<sandbox or snapshot>`; `-` for a sandbox outside every tenant |
| `id` | `sha256(cluster-id, uid, meter, window_start)`, hex: the idempotency key |
| `time` | window end (`gap_end` for gaps), RFC 3339 UTC, milliseconds |
| `datacontenttype` | `application/json` |
| `data` | per type, below |

`cluster-id` is the UUID in ConfigMap `k7-cluster-id` (key `id`), written
once at install (by the playbook, or the chart's pre-install hook) and
never regenerated. Keep it in your backups: a new id makes every event id
new. Because ids are deterministic, a replay after a crash produces the
same ids and deduplicates downstream.

The JSON Schemas live in `src/k7/metering/schemas/`. `v1` is frozen (a unit
test pins each file's hash); a changed field means a new `v2` type.

| Type | `data` |
|---|---|
| `io.katakate.k7.sandbox.usage.v1` | tenant, namespace, sandbox, sandbox_uid, api_key_id, backend, state (`running`/`paused`), size_class, window_start, window_end, seconds, vcpu, memory_gib, approximate |
| `io.katakate.k7.sandbox.usage.gap.v1` | meter (`sandbox`/`egress`/`disk`/`snapshot`), tenant, namespace, sandbox, sandbox_uid, api_key_id, backend (null for storage gaps), gap_start, gap_end, reason (`metering-start`/`agent-restart`/`leader-change`) |
| `io.katakate.k7.egress.usage.v1` | tenant, namespace, sandbox, sandbox_uid, api_key_id, backend, scope, window_start, window_end, bytes, counter_reset |
| `io.katakate.k7.disk.usage.v1` | tenant, namespace, sandbox, sandbox_uid, api_key_id, backend, state (`running`/`paused`/`stopped`), window_start, window_end, seconds, bytes, gib_seconds |
| `io.katakate.k7.snapshot.usage.v1` | tenant, namespace, snapshot, snapshot_uid, source_sandbox, kind, location, window_start, window_end, seconds, bytes, gib_seconds |

`vcpu` and `memory_gib` are the reserved size during the window (`vcpu` is
0 while paused); multiply by `seconds` for vCPU·s and GiB·s. `api_key_id`
is the key that created the sandbox (null for OIDC users and `--core`).

## Spool and delivery

Events are appended to `/var/lib/k7/usage-spool/` on each node (hostPath)
as JSON-lines segments, fsynced before the sample is acknowledged. Each
sink keeps its own offset and resumes from it after a restart, so
delivery is **at least once**: duplicates are possible, losses are
counted.

- The spool is capped (`spool.maxBytes`, default 1 GiB). Past it the
  oldest segment is dropped and its undelivered events are added to
  `k7_usage_events_dropped_total`; k7-agent `/health` reports `degraded`
  for 10 minutes and `K7UsageSpoolOverflow` fires.
- `k7 nodes storage` shows each node's spool fill and dropped events.
- Sandboxes are never blocked by metering unless you ask for it:
  `requireDelivery: true` makes k7-api steer creates off nodes whose spool
  is above `spool.highWaterPercent` (or whose agent does not answer), and
  refuse them with 503 `UsageSpoolFull`, naming the nodes, when the
  sandbox is pinned to such a node or every candidate node is over.

## Sinks

**Prometheus** is always on. k7-agent exports, at tenant × namespace ×
backend × state (egress: × scope):

- `k7_usage_sandbox_seconds_total`, `k7_usage_vcpu_seconds_total`,
  `k7_usage_memory_gib_seconds_total`, `k7_usage_egress_bytes_total`
- `k7_usage_events_dropped_total`, `k7_usage_spool_bytes`,
  `k7_usage_sink_stopped{sink}`, `k7_usage_sink_pending_bytes{sink}`,
  `k7_usage_gaps_total{meter,reason}`, `k7_usage_sample_failures_total`,
  `k7_usage_egress_unavailable{backend}`

The k7-api leader exports the gauges `k7_usage_disk_bytes` (tenant,
namespace, backend) and `k7_usage_snapshot_bytes` (tenant, namespace,
kind, location). No usage series carries a sandbox label (a unit test
fails on one); per-sandbox detail is in the events. The Grafana board
"K7 / Usage by tenant" reads these counters.

Opt-in sinks are a YAML list (`k7 install --metering-sinks-file`, or the
chart's `metering.sinks`):

```yaml
- name: openmeter            # DNS label, at most 32 characters
  type: http
  url: http://openmeter.openmeter.svc:8888   # base URL with the preset
  preset: openmeter          # POST <url>/api/v1/events
  mode: batch                # application/cloudevents-batch+json
  batchSize: 100             # events per request, 1-1000
  headersSecretRef: {name: k7-metering-headers, key: openmeter}
- name: logs
  type: stdout               # one event per line on the k7.usage logger
```

- **http** posts CloudEvents batches. Transport errors, 429 and 5xx back
  off exponentially (1 s to 5 min) while the spool keeps the events. Any
  other 4xx **stops that sink**: k7-agent logs an error with the status and
  body, sets `k7_usage_sink_stopped{sink}` (alert `K7UsageSinkStopped`)
  and   reports it in `/health`. Its events stay in the spool. Fix the cause,
  then `kubectl -n <system namespace> rollout restart ds/k7-agent`.
- **The `openmeter` preset** leaves out `data` keys whose value is `null`
  (such as `api_key_id` with no key): OpenMeter's ingest API refuses null
  values. Ids and every other field are unchanged.
- **Auth headers** come from Secret `k7-metering-headers` in the K7 system
  namespace (`kube-system` for `k7 install`, the release namespace for the
  chart). That is the only Secret k7-agent mounts. Each key holds a JSON
  object of header name to value. The file is read on every request, so
  a rotated Secret applies without a restart:

  ```bash
  kubectl -n kube-system create secret generic k7-metering-headers \
    --from-literal=openmeter='{"Authorization": "Bearer om_..."}'
  ```

- **stdout** writes each event as one JSON line on k7-agent stdout (logger
  `k7.usage`, no other fields), for Fluent Bit or Loki pipelines.

## Configuration

`k7 install` and the Helm chart render the same document into ConfigMap
`k7-metering` (key `metering.json`), mounted into k7-api and k7-agent.
Every key is required and validated at startup; an unknown key or bad
value stops the pod. Changing it rolls both (annotation
`k7.katakate.org/metering-config`).

| Key | `k7 install` flag | Default |
|---|---|---|
| `sampleInterval` | `--metering-sample-interval` | none in production; 15 in development |
| `storageInterval` | `--metering-storage-interval` | 60 |
| `spool.maxBytes` | `--metering-spool-size` | `1Gi` |
| `spool.highWaterPercent` | `--metering-spool-high-water` | 80 |
| `requireDelivery` | `--metering-require-delivery` | false |
| `prometheusUrl` | `--metering-prometheus-url` (`--monitoring` sets its own) | unset |
| `sinks` | `--metering-sinks-file` | `[]` |

## Showback

```bash
k7 usage --from 7d --group-by tenant,state
k7 usage --tenant acme --from 2026-01-01T00:00:00Z --to 2026-02-01T00:00:00Z --json
```

`k7 usage` and `GET /api/v1/usage?from=&to=&tenant=&group_by=` read
Prometheus at `metering.prometheusUrl`; without it they answer 400 naming
the key. K7 keeps no usage database. `from`/`to` are unix seconds or
RFC 3339 with an offset; `group_by` is a subset of
`tenant,namespace,backend,state` (a meter without one of those labels
reports `*`). A tenant key sees only its own tenant; all tenants need an
operator or admin key without namespace scope.

Counters are summed exactly from raw samples, counter resets included, so
`k7 usage` matches the sum of the events to within one scrape interval
per series, for windows inside Prometheus retention (7 days with
`--monitoring`). Disk and snapshot GiB·s integrate the gauges at scrape
resolution. For anything billable, consume the events.

## Approximations, in one place

- `approximate: true`: a state change with no recorded time, or a pod
  deleted between two samples (charged to the midpoint).
- Gap events: time no sampler covered. Nothing is extrapolated.
- Storage windows are at most `storageInterval` coarse: an object created
  inside a window counts from its creation, one deleted inside it is not
  counted for that window.
- `k7 usage` is Prometheus-resolution showback; the events are the record.
