# Hetzner Robot node setup (dual NVMe)

Goal: Ubuntu on **one** NVMe with `/dev/kvm`, and a **second raw NVMe** for the
`kfd` (kata-firecracker-devmapper) LVM thin-pool.

> The older PDF next to this file
> ([`k7_hetzner_node_setup.pdf`](k7_hetzner_node_setup.pdf)) assumed you could
> order a **third** empty NVMe and leave it unformatted. Hetzner often ships
> dual-NVMe boxes now **without** an add-on disk option, and the default
> `installimage` layout puts the OS on **software RAID across both drives**.
> Use this guide instead.

> **Automated alternative:** if you have Hetzner Robot API credentials
> (`ROBOT_USER`/`ROBOT_PASS`) and your SSH key registered in Robot,
> `TWO_DISK=1 HOSTNAME_VALUE=<name> utils/reset_robot_node.sh <server-number> <ip>`
> automates steps 2–3 end-to-end: rescue boot, wipe of **both** NVMes,
> single-disk installimage (`SWRAID 0`), reboot into Ubuntu. Because it
> pre-wipes the spare disk in rescue, step 3 below can be skipped. See
> `editor-config/skills/hetzner-k7-node-reset/SKILL.md` for details.

## 1. Pick a dedicated (Robot) server

- Must expose hardware virtualization: `ls /dev/kvm` after install.
- Robot dedicated only (not Cloud CX/CPX VPS).
- Prefer Intel/AMD x86 with **2× NVMe** included (e.g. AX/EX matrix).
- You do **not** need a third drive slot if the box already has two NVMes.

## 2. Rescue → installimage on a single disk

1. In [Robot](https://robot.hetzner.com): **Rescue** → Linux 64-bit → activate → reboot.
2. SSH into rescue and check disks:

   ```bash
   lsblk -d -o NAME,SIZE,MODEL,TRAN
   ```

3. Run `installimage`, pick Ubuntu 24.04 (Noble), then edit the config so only
   **one** drive is used and software RAID is off:

   ```text
   DRIVE1 /dev/nvme0n1
   # DRIVE2 /dev/nvme1n1    # omit / keep commented — thin-pool disk

   SWRAID 0
   SWRAIDLEVEL 0

   BOOTLOADER grub
   HOSTNAME k7-node

   PART /boot ext4 1024M
   PART swap swap 8G
   PART / ext4 all

   IMAGE /root/images/Ubuntu-2404-noble-amd64-base.tar.gz
   ```

   Adjust `DRIVE1` / image path to match what `installimage` shows. Save, quit,
   confirm the wipe, wait for install, then `reboot`.

## 3. Wipe leftover RAID metadata on the spare disk

After first boot, the unused NVMe often still has old `md` / LVM signatures
from a previous RAID layout. Wipe it **before** `k7 install`:

```bash
lsblk -f
# example: spare is /dev/nvme1n1
sudo mdadm --stop --scan || true
sudo ./utils/wipe-disk.sh /dev/nvme1n1
lsblk -f   # spare should show no FSTYPE / no RAID members
```

`wipe-disk.sh` is destructive — only point it at the spare disk.

## 4. Install k7 and point at the raw disk

```bash
sudo add-apt-repository ppa:katakate.org/k7
sudo apt update
sudo apt install k7

# dual-NVMe box: let the playbook auto-detect the raw spare disk.
# PPA k7 0.2.1 still defaults k7d to 0.1.0 — pin the public v0.2.1 release.
# Run from a checkout of this repo (or Katakate/k7) so k7-api:local can build.
sudo k7 install --backend kfd,kql,k7d --k7d-version 0.2.1
```

`k7 install` provisions the LVM thin-pool on that disk for the `kfd` backend.
Other backends (`kql`, `k7d`) do not need this spare disk, but keeping one raw
NVMe lets you compare all backends on the same node.

Two-node (one server + one agent, no `--ha`) is the same idea: write an
inventory with `k7_backends=kfd,kql,k7d` on both hosts, omit
`k7_devmapper_disk`, and run **one** `k7 install -i inventory.ini --k7d-version 0.2.1`
from the first master. See `src/k7/deploy/inventory.ini.example`.

> **Do NOT pin the disk on dual-NVMe boxes.** NVMe enumeration
> (`nvme0n1` vs `nvme1n1`) is **not stable across reboots**, so a hardcoded
> `--disk /dev/nvme1n1` (or `k7_devmapper_disk` in a multi-node inventory)
> can point at the OS disk after a reboot. The playbook auto-detects the
> empty non-root whole disk, which is enumeration-proof. Pin a device only
> when a node has several spare disks and you must pick a specific one.
> The same applies to multi-node installs — see the dual-NVMe note in
> `src/k7/deploy/inventory.ini.example`.
>
> Sizing: only the first `kata_thinpool_pv_size` (default 100G) of the spare
> disk is used for the kfd thin-pool, and the k7d volume pool defaults to a
> 32G sparse image (`k7d_disks_image_size`). Override both per node in the
> inventory — see "Disk pool sizing" in `docs/BACKENDS.md`.

## Checklist

- [ ] `ls /dev/kvm` exists
- [ ] OS root is on a single disk (`findmnt /` → `/dev/nvme0n1p…`, not `/dev/md…`)
- [ ] Spare disk has no filesystem (`lsblk -f` empty FSTYPE)
- [ ] `k7 install --backend kfd,kql,k7d --k7d-version 0.2.1` succeeds (auto-detects the spare disk — do not pin `--disk` on dual-NVMe)
- [ ] `k7 create --backend kfd …` can start a sandbox
