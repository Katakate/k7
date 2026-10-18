# K7APIHighErrorRate

More than `observability.thresholds.apiErrorRatio` (default 5%) of k7-api
requests returned 5xx over 10 minutes.

## First three commands

```bash
kubectl -n kube-system logs -l app=k7-api -c k7-api --since=15m | jq -c 'select(.status >= 500 or .level == "error")' | tail -20
kubectl -n kube-system get events --sort-by=.lastTimestamp | tail -20
kubectl -n kube-system top pods -l app=k7-api
```

## Reading them

- Each JSON line carries `request_id`, `route`, `op`, `backend` and
  `error_code`. Group by `route` and `error_code` to find the failing path.
  Pass one `request_id` to `grep` across the k7-api and k7-agent logs to
  follow one request.
- `KubernetesError` codes, or a rising `k7_kube_api_errors_total`: the
  Kubernetes API is refusing k7-api (RBAC, throttling, or a down
  apiserver).
- 503s from every route: the key store is stale. See
  [K7KeyStoreStale](K7KeyStoreStale.md).
