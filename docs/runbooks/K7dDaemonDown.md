# K7dDaemonDown

The k7-agent on `instance` cannot connect to the k7d control socket
(`/run/k7d/k7d.sock`). k7d sandboxes on that node cannot be created,
paused, resumed or forked.

## First three commands

```bash
ssh <node> systemctl status k7d --no-pager
ssh <node> journalctl -u k7d --since -30min --no-pager | tail -50
ssh <node> ls -l /run/k7d/
```

## Reading them

- `inactive` or `failed`: start it with `systemctl restart k7d` and read
  the journal for the cause. A version mismatch with `/etc/k7/k7d_version`
  means the node needs `k7 install` again.
- `active` but no socket: k7d is still starting, or it failed to bind. The
  journal says which.
- The socket exists and the alert still fires: the agent pod lacks the
  `/run/k7d` hostPath mount. Re-run `k7 install` or `helm upgrade`.
