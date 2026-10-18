# Observability

k7-api and k7-agent export Prometheus metrics and log JSON lines. One
request id follows a request from k7-api into the k7-agent that runs its
VM op. K7 ships Grafana dashboards, alert rules and a
[runbook](runbooks/) per alert, and `k7 install --monitoring` can install a
monitoring stack for a single-box install.

## Metrics

Both processes serve `/metrics` (and nothing else) as plain HTTP on port
9464 (`K7_METRICS_PORT`; `off` disables the listener). Only the pods you
name may reach that port:

| Install | Setting | Default |
|---|---|---|
| `k7 install` | `--metrics-scraper <namespace>:<key>=<value>[,...]` | none: nobody reaches 9464 |
| `k7 install --monitoring` | the stack's Prometheus pods (`monitoring:app.kubernetes.io/name=prometheus`) | |
| chart | `observability.scraperSelector` (`namespace`, `podLabels`) | required while `observability.metrics: true` |

On Cilium this is the CiliumNetworkPolicy `k7-metrics`. It only denies
9464 to other sources, so the API ports and `--api-allow-cidr` behave as
before. Other CNIs get the NetworkPolicies `k7-api-metrics` and
`k7-agent-metrics`. On Cilium the host and other nodes are refused too.

k7-api (each replica):

| Metric | Labels |
|---|---|
| `k7_http_requests_total`, `k7_http_request_duration_seconds` | `route` (the template, e.g. `/api/v1/sandboxes/{name}`), `method`, `status` (counter only) |
| `k7_operation_duration_seconds` | `op`, `backend`, `outcome` |
| `k7_operation_errors_total` | `op`, `backend`, `error_code` |
| `k7_auth_failures_total` | `reason`: `missing`, `invalid`, `expired`, `forbidden`, `oidc_invalid` |
| `k7_key_store_last_sync_timestamp_seconds` | |
| `k7_kube_api_errors_total` | `verb`, `resource`, `status` |
| `k7_quota_rejections_total` | `tenant`, `dimension`: one per create refused with `QuotaExceeded` |
| `k7_rate_limited_total` | `class` (incremented when rate limiting lands) |
| `k7_sandboxes` | `tenant`, `backend`, `state`. Only the leader replica (the Ready pod with the smallest name) exports it, so `sum()` is the cluster count |
| `k7_api_info` | `version`, `mode` (`production` or `development`) |
| `k7_tls_cert_expiry_timestamp_seconds` | `source`, read from a TLS handshake with the Caddy sidecar |

k7-agent (each node): `k7d_rpc_duration_seconds` and `k7d_rpc_errors_total`
(`op`), `k7d_daemon_up`, `k7_storage_pool_bytes` (`pool`, `kind`),
`k7_storage_report_age_seconds` and `k7_vm_count` (`backend`, `state`).

Both also export the standard `process_*` and `python_*` series. One
bucket set covers every op, from a 5 ms k7d VMM fork to a 10-minute kql
restore: 5 ms to 600 s.

### Cardinality

No series carries a sandbox name, uid or pod (a unit test enforces this).
For per-sandbox data use `k7 top` and the usage events (`docs/METERING.md`). Upper bounds,
counting `_bucket`, `_sum` and `_count` (20 series per histogram child):

| Source | Series |
|---|---|
| HTTP, per k7-api replica | 22 routes × ~8 statuses + 22 × 20 ≈ 620 |
| Ops, per replica | 9 ops × 5 backends × (2 outcomes × 20 + 8 error codes) ≈ 2,200 |
| Kube API errors, per replica | 5 verbs × ~10 resources × ~6 statuses ≈ 300 |
| `k7_sandboxes`, leader only | tenants × 5 backends × 3 states = 15 T |
| Quota and rate limits | tenants × (dimensions + classes) |
| Per node (k7-agent) | ~7 RPC ops × 21 + ~25 gauges ≈ 175 |
| Usage counters, per node (metering) | tenant namespaces on the node × backends × (2 states × 3 + 1) |

So 2 replicas, 20 nodes and 500 tenants come to at most
2 × 3,100 + 15 × 500 + 20 × 175 ≈ 17,200 series, plus the usage counters
of the tenant namespaces each node actually runs. In practice the count is
much lower, because only label sets that occur are exported. Tenant
counts are bounded by the operator; the dashboards use `topk` for tenant
panels.

## Logs

k7-api, k7-agent and the snapshot-gc Job write one JSON object per line on
stderr. `K7_LOG_FORMAT` and `K7_LOG_LEVEL` are required, and the manifests
set them (the chart takes `observability.logLevel`). Every line has `ts`,
`level`, `logger` and `msg`, plus `request_id`, `tenant`, `namespace`,
`sandbox`, `backend`, `op`, `duration_ms` and `error_code`, which are null
when they do not apply. API key tokens and JWTs are replaced with
`[REDACTED]`, and no level logs request bodies.

- **Request ids**: k7-api accepts a well-formed `X-Request-ID` (8-64
  characters of `[A-Za-z0-9-]`) or mints a UUIDv7. It returns the id in
  the response and sends it to k7-agent with every VM op. Each
  snapshot-gc run logs under its own id.
- **Per request**: one access line, an audit line for mutating requests
  (`logger: k7.audit`), and a `k7.op` summary line per lifecycle op. Exec
  output is never logged.
- **Volume and rotation**: a line is about 400-600 bytes. kubelet rotates
  container logs. K3s keeps the kubelet defaults of 10 MiB × 5 files per
  container, so at 10 requests/s a node keeps about three hours of k7-api
  logs. Ship them with your collector (they are JSON already), or raise
  `container-log-max-size` and `container-log-max-files` in the K3s kubelet
  args.

The CLI keeps its human output, unchanged.

## Dashboards and alerts

Sources live in `src/k7/deploy/observability/`:

- `dashboards/`: API overview, Sandbox lifecycle, Nodes and storage, and
  Usage by tenant (the metering usage counters, `docs/METERING.md`).
- `rules/k7.rules.yaml`: the alerts, with each alert's thresholds stored
  as `k7:threshold:*` recording rules. `promtool test rules` covers one
  firing and one quiet case per alert (`tests/`).

Every alert links to `docs/runbooks/<alert>.md`, which starts with the
first three commands to run.

## Helm chart values

| Value | Default | Meaning |
|---|---|---|
| `observability.metrics` | `true` | Serve 9464. Needs `networkPolicy.provider` `cilium` or `kubernetes`, and `scraperSelector` |
| `observability.scraperSelector` | `{}` | `{namespace, podLabels}` of the pods that may scrape |
| `observability.logLevel` | `info` | `debug`, `info`, `warning` or `error` |
| `observability.serviceMonitor` | `false` | Service `k7-api-metrics`, ServiceMonitor `k7-api` and PodMonitor `k7-agent`. Rendering fails without the monitoring.coreos.com CRDs |
| `observability.prometheusRule` | `false` | PrometheusRule `k7` with the thresholds below |
| `observability.monitorLabels` | `{}` | Labels on those objects, for a Prometheus that selects by label |
| `observability.grafanaDashboards` | `false` | Dashboard ConfigMaps labelled `grafana_dashboard: "1"` |
| `observability.thresholds.*` | see `values.yaml` | Alert thresholds and per-op p95 budgets |

## `k7 install --monitoring`

Monitoring is opt-in and meant for single-box installs; existing clusters
and EKS keep their own monitoring and use the chart values above. It
installs, on the first master:

- kube-prometheus-stack 92.3.0 (pinned and checksummed) in namespace
  `monitoring`, with 7 days of retention and K3s's embedded components
  disabled.
- The K7 ServiceMonitor and PodMonitor, the PrometheusRule `k7` (in
  `kube-system`) and the dashboard ConfigMaps.
- A `/metrics` policy that admits that Prometheus. `--metrics-scraper`
  cannot be combined with `--monitoring`, and `--no-api` is refused.

Grafana's admin password is random and stored in a Secret:

```bash
kubectl -n monitoring get secret kube-prometheus-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
```

Running `k7 install` again without `--monitoring` leaves the stack
installed, but the `/metrics` policy goes back to `--metrics-scraper` (by
default nobody), so its K7 targets go down. To remove the stack:

```bash
kubectl -n kube-system delete prometheusrule k7
kubectl delete -f /etc/k7/observability/k7-monitors.yaml
helm uninstall kube-prometheus-stack -n monitoring
kubectl delete namespace monitoring
kubectl get crd -o name | grep monitoring.coreos.com | xargs kubectl delete
```

## Not yet

- Tracing (OpenTelemetry). Request ids are the correlation mechanism
  until then.
- `k7_rate_limited_total` stays at zero until rate limiting lands.
