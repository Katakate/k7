# K7StoragePoolFilling

A node's storage pool has been above its threshold for 10 minutes:

| Pool | Threshold (chart value) |
|---|---|
| `thinpool_data` | 80% (`thinpoolDataRatio`) |
| `thinpool_metadata` | 70% (`thinpoolMetadataRatio`) |
| `k7d_disks` | 85% (`k7dDisksRatio`) |

A full thin pool stops every kata-firecracker-devmapper sandbox on the
node, and a full k7d disk pool fails k7d creates and forks.

## First three commands

```bash
ssh <node> lvs -o lv_name,lv_size,data_percent,metadata_percent
ssh <node> df -h /var/lib/k7d/disks
k7 list
```

## Reading them

- Data filling: delete sandboxes you no longer need (`k7 delete`), or
  remove their snapshots (`k7 snapshot delete`). Snapshot GC removes
  expired ones every 10 minutes; see
  [K7SnapshotGCFailing](K7SnapshotGCFailing.md) if it does not.
- Metadata filling while data is low: many small thin volumes. Extend the
  metadata LV (`lvextend --poolmetadatasize`). It is the one manual
  storage action the playbook does not take for you.
- k7d disks: each fork shares its parent's disk until written, so
  long-lived forks of a busy parent grow. Delete idle forks first.
