# K7KeyStoreStale

No k7-api replica has read the `k7-api-keys` Secret for longer than
`observability.thresholds.keyStoreStaleSeconds` (default 60 s). After 60 s
without a read, a replica answers 503 rather than trusting an old key list,
so authentication fails.

## First three commands

```bash
kubectl -n kube-system logs -l app=k7-api -c k7-api --since=10m | jq -c 'select(.logger == "k7.keystore")' | tail -20
kubectl -n kube-system get secret k7-api-keys -o jsonpath='{.metadata.resourceVersion}{"\n"}'
kubectl auth can-i get secret/k7-api-keys -n kube-system --as=system:serviceaccount:kube-system:k7-api
```

## Reading them

- `Forbidden` in the log, or `no` from `can-i`: the k7-api Role lost
  `get` on the Secret. Re-run `k7 install` or `helm upgrade`.
- `NotFound`: the Secret was deleted. `k7 install` recreates it empty;
  re-issue keys with `k7 generate-api-key`.
- Timeouts: the replica cannot reach the apiserver. Check the pod's node
  and the apiserver health.
