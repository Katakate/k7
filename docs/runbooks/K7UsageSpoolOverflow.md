# K7UsageSpoolOverflow

A k7-agent dropped usage events in the last 10 minutes
(`k7_usage_events_dropped_total`). Its spool at
`/var/lib/k7/usage-spool` reached `metering.spool.maxBytes`, so the
oldest segment was deleted before every sink had read it. Usage for
those windows is lost downstream, and the agent's `/health` reports
`usage.status: degraded` for 10 minutes.

## First three commands

```bash
kubectl -n kube-system logs -l app=k7-agent --since=30m --prefix | grep -E '"k7.metering|sink ' | tail -20
k7 nodes storage
kubectl -n kube-system exec ds/k7-agent -- python -c "import json,urllib.request as u;print(json.dumps(json.load(u.urlopen('http://127.0.0.1:8000/health'))['usage'],indent=1))"
```

## Reading them

- A sink with `stopped` set, or `last_error` repeating: that sink is not
  draining the spool. See K7UsageSinkStopped, or fix the endpoint; the
  spool drains on its own once the sink accepts events again.
- `pending_bytes` near `max_bytes` for every sink, with no errors: the
  sinks keep up too slowly for the event volume. Raise
  `--metering-spool-size` (`metering.spool.maxBytes`), raise the sink's
  `batchSize`, or scale the sink.
- The lost windows are not re-created. Downstream totals for the dropped
  period are low by the dropped events' seconds; `last_drop` in the
  spool status bounds when it happened.
