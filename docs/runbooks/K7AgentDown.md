# K7AgentDown

The k7-agent on one node (`instance`) has not answered its `/metrics`
scrape for 5 minutes. k7d pause, resume and fork for sandboxes on that node
fail, and `GET /api/v1/nodes/storage` reports an error for it.

## First three commands

```bash
kubectl -n kube-system get pods -l app=k7-agent -o wide
kubectl -n kube-system describe pod -l app=k7-agent | grep -A10 Events
kubectl -n kube-system logs -l app=k7-agent --tail=50 --prefix
```

## Reading them

- No agent pod on the node: the DaemonSet does not tolerate a taint, or
  the node lacks a label it selects. Compare `kubectl describe node` with
  the DaemonSet's nodeSelector and tolerations.
- The pod is `Running` but the scrape fails: the scraper is not admitted
  by the `k7-metrics` policy, or the node's pod network is down. Look for
  Cilium drops with `cilium monitor --type drop` in the Cilium pod on that
  node.
- Crash loop: the last log line names the cause. A missing
  `/etc/k7/agent_token` means `k7 install` did not finish on that node.
