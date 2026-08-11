#!/usr/bin/env bash
set -euo pipefail

# wipe-disk.sh — aggressively and safely wipe a single block device
# Usage: ./wipe-disk.sh /dev/sdX_or_nvmeXnY
#
# After you type YES, this will:
# - Unmount anything on the device/partitions and disable swap
# - Close dm-crypt mappings (best effort)
# - Stop mdadm arrays using its partitions and zero md superblocks
# - Deactivate LVM VGs using it, then wipe PV/FS signatures
# - Zap partition tables and signatures
# - Zero first/last regions and try blkdiscard (if supported)

die() { echo "Error: $*" >&2; exit 1; }
run() { echo "+ $*"; "$@"; }

[[ $# -ge 1 ]] || { echo "Usage: $0 /dev/sdX_or_nvmeXnY"; exit 1; }

DEVICE_RAW="$1"
DEVICE="$(readlink -f "$DEVICE_RAW" || echo "$DEVICE_RAW")"
[[ -b "$DEVICE" ]] || die "$DEVICE is not a block device"

# Accept only whole-disk nodes (not partitions)
# Use -d to show only the device itself (no children), -n to suppress header
dtype="$(lsblk -dn -o TYPE "$DEVICE" 2>/dev/null || echo "")"
case "$dtype" in
  disk|mpath) : ;;        # ok
  *) die "You gave a partition node ($DEVICE). Please provide the WHOLE DISK (e.g., /dev/nvme0n1 or /dev/sda).";;
esac

echo "=============================="
echo " YOU ARE ABOUT TO WIPE: $DEVICE"
echo "=============================="
echo "This will destroy ALL partitions, RAID/LVM/crypto metadata, and filesystems."
confirm="${WIPE_CONFIRM:-}"
if [[ "$confirm" != "YES" ]]; then
  read -r -p "Type YES to proceed: " confirm
fi

[[ "$confirm" == "YES" ]] || { echo "Aborted."; exit 0; }

# Helper: enumerate child partitions of the device
partitions() {
  # Prints /dev/* entries for child partitions (if any)
  lsblk -rno PATH "$DEVICE" | tail -n +2 || true
}

echo "[1/7] Unmounting and disabling swap on the target device..."
# Unmount anything mounted from the device or any of its partitions
while read -r src; do
  [[ -z "$src" ]] && continue
  # Unmount all mountpoints that have this source
  while read -r mnt; do
    [[ -n "$mnt" ]] && run umount -R "$mnt" || true
  done < <(findmnt -rno TARGET -S "$src" 2>/dev/null || true)
done < <(printf "%s\n" "$DEVICE" "$(partitions)")

# Disable any swap that points to the device/partitions
if [[ -r /proc/swaps ]]; then
  while read -r swdev _; do
    for src in "$DEVICE" $(partitions); do
      if [[ "$swdev" == "$src" ]]; then run swapoff "$swdev" || true; fi
    done
  done < <(tail -n +2 /proc/swaps | awk '{print $1" "$2}')
fi

echo "[2/7] Closing dm-crypt (LUKS) mappings on top of this device (best effort)..."
if command -v lsblk >/dev/null 2>&1; then
  # Find any device-mapper names that depend on our base device or its parts
  while read -r dmname dtype _pk; do
    [[ "$dtype" != "crypt" ]] && continue
    # If this crypt mapper ultimately uses our device or its partitions, close it
    if lsblk -rno PKNAME "/dev/$dmname" 2>/dev/null | grep -Eq "$(basename "$DEVICE")(p[0-9]+)?"; then
      if command -v cryptsetup >/dev/null 2>&1; then
        run cryptsetup luksClose "$dmname" || true
      fi
      if command -v dmsetup >/dev/null 2>&1; then
        run dmsetup remove "/dev/$dmname" || true
      fi
    fi
  done < <(lsblk -rno NAME,TYPE,PKNAME | awk '{print $1" "$2" "$3}')
fi

echo "[3/7] Stopping mdraid arrays that include this disk..."
if command -v mdadm >/dev/null 2>&1; then
  # Stop any /dev/md* that lists one of our partitions as a member
  for md in /dev/md/* /dev/md*; do
    [[ -e "$md" ]] || continue
    if mdadm --detail "$md" >/dev/null 2>&1; then
      if mdadm --detail "$md" 2>/dev/null | grep -qE "$(basename "$DEVICE")(p[0-9]+)?"; then
        run mdadm --stop "$md" || true
      fi
    fi
  done
fi

echo "[4/7] Deactivating LVM that sits on this disk (if any)..."
if command -v pvs >/dev/null 2>&1 && command -v vgchange >/dev/null 2>&1; then
  # Find VGs that have PVs on our device or its partitions and deactivate them
  while read -r vg pv; do
    for src in "$DEVICE" $(partitions); do
      if [[ "$pv" == "$src" ]]; then
        run vgchange -an "$vg" || true
      fi
    done
  done < <(pvs --noheadings -o vg_name,pv_name 2>/dev/null | awk '{$1=$1;print}')
fi

echo "[5/7] Clearing md superblocks and LVM/FS signatures on partitions..."
for p in $(partitions); do
  if command -v mdadm >/dev/null 2>&1; then
    run mdadm --zero-superblock "$p" || true
  fi
  if command -v wipefs >/dev/null 2>&1; then
    run wipefs -fa "$p" || true
  fi
done

# Let udev settle and drop partition caches before touching the whole disk
command -v udevadm >/dev/null 2>&1 && run udevadm settle || true
run blockdev --rereadpt "$DEVICE" || true

echo "[6/7] Zapping partition tables and signatures on the whole disk..."
if command -v wipefs >/dev/null 2>&1; then
  run wipefs -fa "$DEVICE" || true
fi

if command -v sgdisk >/dev/null 2>&1; then
  run sgdisk --zap-all "$DEVICE" || true
else
  echo "sgdisk not found; zeroing first/last 1MiB as a fallback..."
  run dd if=/dev/zero of="$DEVICE" bs=1M count=1 conv=fsync || true
  if command -v blockdev >/dev/null 2>&1; then
    SECTORS=$(blockdev --getsz "$DEVICE")
    SEEK=$(( SECTORS / 2048 - 1 ))   # 2048 sectors ≈ 1 MiB at 512-byte sectors
    (( SEEK > 0 )) && run dd if=/dev/zero of="$DEVICE" bs=1M seek="$SEEK" count=1 conv=fsync || true
  fi
fi

echo "[7/7] Overwriting start/end of the disk and attempting secure discard..."
# Zero first 100MiB to clear lingering metadata quickly
run dd if=/dev/zero of="$DEVICE" bs=1M count=100 status=progress conv=fsync || true

# Zero last ~100MiB
if command -v blockdev >/dev/null 2>&1; then
  SECTORS=$(blockdev --getsz "$DEVICE")
  SEEK_VAL=$(( SECTORS / 2048 - 100 ))
  if (( SEEK_VAL > 0 )); then
    run dd if=/dev/zero of="$DEVICE" bs=1M seek="$SEEK_VAL" count=100 status=progress conv=fsync || true
  fi
fi

# Discard entire device if supported (SSD/NVMe)
if command -v blkdiscard >/dev/null 2>&1; then
  run blkdiscard "$DEVICE" || echo "(blkdiscard not supported or failed; non-fatal)"
fi

# Final reread to clear any lingering kernel views
run blockdev --rereadpt "$DEVICE" || true
command -v partprobe >/dev/null 2>&1 && run partprobe "$DEVICE" || true

echo
echo "DONE: $DEVICE should now be blank. Current view:"
lsblk -o NAME,TYPE,SIZE,MOUNTPOINTS "$DEVICE" || true
