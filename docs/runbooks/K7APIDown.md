# K7APIDown

No k7-api replica answered its `/metrics` scrape for 2 minutes, or
Prometheus has no `k7-api` target at all. Clients get connection errors.

Commands use `kube-system`, the playbook's namespace; on a chart install
use the release namespace.

## First three commands

```bash
kubectl -n kube-system get pods -l app=k7-api -o wide
kubectl -n kube-system describe deployment k7-api | tail -20
kubectl -n kube-system logs -l app=k7-api -c k7-api --tail=50
```

## Reading them

- Pods `CrashLoopBackOff`: the last log line names the cause. The usual one
  is a missing `K7_LOG_FORMAT`, `K7_LOG_LEVEL` or `K7_METRICS_PORT`, which
  the process refuses to start without. Re-run `k7 install` or
  `helm upgrade` to restore the manifests.
- Pods `Running` but the alert still fires: the scrape is refused. Check
  that the `k7-metrics` CiliumNetworkPolicy, or the `k7-api-metrics`
  NetworkPolicy, names your Prometheus pods (`--metrics-scraper` or
  `observability.scraperSelector`). Also check that the target exists in
  Prometheus under job `k7-api`.
- No target: the ServiceMonitor `k7-api` or the Service `k7-api-metrics`
  is missing, or Prometheus does not select it. `--monitoring` and the
  chart's `observability.monitorLabels` handle the selection.
