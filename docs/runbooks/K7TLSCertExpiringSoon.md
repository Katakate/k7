# K7TLSCertExpiringSoon

The certificate k7-api serves has expired or will within
`tlsExpirySeconds` (default 14 days), and this has held for an hour. The
expiry comes from a TLS handshake with the Caddy sidecar, so it is the
certificate clients actually get. The label `source` says where the
certificate comes from:

| `source` | Install | Who renews it |
|---|---|---|
| `acme` | `k7 install` with a public hostname | Caddy, 30 days before expiry |
| `files` | `k7 install`: the k7 CA, or `--api-tls-cert`/`--api-tls-key` | `k7 install` |
| `certManager` | chart, `tls.mode: certManager` | cert-manager |
| `secret` | chart, `tls.mode: secret` | you |

## First three commands

```bash
kubectl -n kube-system logs -l app=k7-api -c caddy --since=24h | grep -iE 'renew|obtain|error' | tail -20
kubectl -n kube-system get secret k7-api-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -enddate -issuer
openssl s_client -connect <api-host>:<port> -servername <api-host> </dev/null 2>/dev/null | openssl x509 -noout -enddate
```

## Reading them

- `acme`: errors in the Caddy log mean port 80 or DNS no longer reaches
  this node. Fix that and Caddy retries on its own.
- `files`: re-run `k7 install` with the same flags (and the new
  `--api-tls-cert`/`--api-tls-key` if you supply your own).
- `certManager`: `kubectl describe certificate` in the release namespace
  shows why it did not renew.
- `secret`: replace the Secret, then
  `kubectl rollout restart deployment k7-api` in the release namespace.
- If the Secret is fresh but the handshake shows the old date, Caddy has
  not reloaded. Restart the k7-api Deployment.
