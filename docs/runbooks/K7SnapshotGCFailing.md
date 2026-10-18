# K7SnapshotGCFailing

The last `k7-snapshot-gc` Job failed (from kube-state-metrics'
`kube_job_status_failed`). Expired snapshots stay, and storage keeps
filling.

## First three commands

```bash
kubectl -n kube-system get jobs -l app=k7-snapshot-gc
kubectl -n kube-system logs -l app=k7-snapshot-gc --tail=50 | jq -c 'select(.level != "info")'
kubectl get volumesnapshots -A -l katakate.org/sandbox
```

## Reading them

- Every line of one run shares a `request_id`. Grep for it to see the
  whole run.
- `Forbidden`: the `k7-api` ServiceAccount lost snapshot RBAC. Re-run
  `k7 install` or `helm upgrade`.
- A snapshot that never becomes deletable (`finalizers` set) blocks the
  sweep. `kubectl describe volumesnapshot` shows which controller holds
  it. Fix that controller; do not strip the finalizer.
- The alert clears when a later run succeeds and the failed Job ages out
  (`ttlSecondsAfterFinished: 600`).
