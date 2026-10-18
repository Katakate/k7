# K7StorageReportStale

`/run/k7/storage.json` on `instance` is older than
`storageReportStaleSeconds` (default 90 s). The storage gauges and
`GET /api/v1/nodes/storage` for that node show old numbers, so
[K7StoragePoolFilling](K7StoragePoolFilling.md) cannot fire.

## First three commands

```bash
ssh <node> systemctl status k7-storage-report.timer k7-storage-report.service --no-pager
ssh <node> journalctl -u k7-storage-report.service --since -15min --no-pager | tail -30
ssh <node> ls -l --time-style=+%T /run/k7/storage.json
```

## Reading them

- The timer is inactive: `systemctl enable --now k7-storage-report.timer`,
  then re-run `k7 install` so the playbook owns it again.
- The service fails: the journal shows the `lvs` error. A missing volume
  group means the node's thin pool is gone; reinstall the node.
- The file is fresh but the gauge is old: the k7-agent cannot read
  `/run/k7`. Check the DaemonSet's hostPath mount.
