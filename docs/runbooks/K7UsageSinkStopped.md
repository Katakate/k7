# K7UsageSinkStopped

A usage sink on one k7-agent answered a 4xx other than 429
(`k7_usage_sink_stopped{sink}`). The agent stopped sending to
that sink instead of retrying forever. Its events stay in the spool
until the agent restarts, and the spool overflows (K7UsageSpoolOverflow)
if that takes too long. Other sinks keep delivering.

## First three commands

```bash
kubectl -n kube-system logs -l app=k7-agent --since=1h --prefix | grep 'stopped:' | tail -5
kubectl -n kube-system get configmap k7-metering -o jsonpath='{.data.metering\.json}'; echo
kubectl -n kube-system get secret k7-metering-headers -o jsonpath='{.data}' | jq 'keys'
```

## Reading them

- `HTTP 401` or `403`: the sink's credentials are wrong or expired.
  Update the header JSON in Secret `k7-metering-headers` (the key named
  by the sink's `headersSecretRef`), then
  `kubectl -n kube-system rollout restart ds/k7-agent`.
- `HTTP 400` or `422` naming a field: the endpoint rejects the event
  schema. Check the sink's URL and preset (OpenMeter needs `preset:
  openmeter` and its base URL) and that its meters accept
  `io.katakate.k7.*` types.
- `HTTP 404`: the URL path is wrong. Fix it with
  `k7 install --metering-sinks-file` (or the chart's `metering.sinks`);
  the config change rolls the agents.
- After the restart the sink resumes from its saved offset, so nothing
  already in the spool is lost. Events re-sent after a crash carry the
  same ids, so the endpoint deduplicates them.
