# K7OperationSlow

The p95 of a successful lifecycle op (`k7_operation_duration_seconds`,
label `op`) has been over twice its budget for 15 minutes. The budgets come
from PERFORMANCE.md, and the chart's `observability.thresholds.opP95BudgetSeconds`
overrides them.

## First three commands

```bash
kubectl -n kube-system logs -l app=k7-api -c k7-api --since=30m | jq -c 'select(.logger == "k7.op") | {op, backend, duration_ms, sandbox, namespace}' | sort | tail -20
kubectl get pods -A -l katakate.org/sandbox -o wide | grep -v Running
kubectl -n longhorn-system get volumes.longhorn.io -o wide | grep -v healthy
```

## Reading them

- One backend is slow: `kata-qemu-longhorn` forks and creates wait on
  Longhorn snapshots and clones. Degraded or rebuilding volumes slow them
  down. `k7d` ops wait on the k7d daemon (`k7d_rpc_duration_seconds` on
  the k7-agent of that node).
- One node is slow: compare the `node` of the slow sandboxes. A full thin
  pool or a busy disk raises every op on that node; see
  [K7StoragePoolFilling](K7StoragePoolFilling.md).
- Every op is slow: the Kubernetes API is slow. Check
  `k7_kube_api_errors_total` and the apiserver's own latency.
