#!/bin/sh
# Docker vehicle for Kata backends. Overlay2 on a block
# device or fail loud — no driver auto-selection, no virtio-fs graph.
set -eu

DEVICE="/dev/k7docker"
GRAPH="/var/lib/docker"
SOCK_DIR="/run/k7/docker"
SOCK="${SOCK_DIR}/docker.sock"

log() { echo "k7-docker-vehicle: $*" >&2; }

die() {
  log "$*"
  exit 1
}

i=0
while [ ! -b "${DEVICE}" ]; do
  i=$((i + 1))
  if [ "${i}" -gt 60 ]; then
    ls -l "${DEVICE}" /dev 2>&1 || true
    die "${DEVICE} is not a block device"
  fi
  sleep 0.5
done

# The dind image VOLUME /var/lib/docker is virtio-fs on Kata. Overlay2
# cannot live there — unmount it, then put ext4 on the block device.
i=0
while grep -q " ${GRAPH} " /proc/mounts; do
  i=$((i + 1))
  [ "${i}" -le 8 ] || die "could not unmount existing filesystem from ${GRAPH}"
  log "unmounting existing mount from ${GRAPH}: $(grep " ${GRAPH} " /proc/mounts || true)"
  umount "${GRAPH}" || umount -l "${GRAPH}" || die "umount ${GRAPH} failed"
done
mkdir -p "${GRAPH}"
# Try mounting first. A restore/fork clone already has ext4; mkfs would
# wipe it. Longhorn clones can take a moment to present the superblock.
mounted=0
i=0
while [ "${i}" -lt 60 ]; do
  if mount -t ext4 "${DEVICE}" "${GRAPH}"; then
    mounted=1
    break
  fi
  i=$((i + 1))
  sleep 0.5
done
if [ "${mounted}" -eq 0 ]; then
  has_fs=0
  blkid_out=""
  if command -v blkid >/dev/null 2>&1; then
    blkid_out=$(blkid "${DEVICE}" 2>/dev/null || true)
    case "${blkid_out}" in
      *TYPE=\"ext4\"* | *TYPE=ext4*) has_fs=1 ;;
    esac
  fi
  [ "${has_fs}" -eq 0 ] || die "ext4 on ${DEVICE} but mount failed (blkid='${blkid_out}')"
  command -v mkfs.ext4 >/dev/null 2>&1 || die "mkfs.ext4 not found in vehicle image"
  log "formatting ${DEVICE} as ext4 (blkid='${blkid_out:-empty}')"
  mkfs.ext4 -F -m 0 "${DEVICE}"
  mount -t ext4 "${DEVICE}" "${GRAPH}" || {
    log "mount -t ext4 failed after mkfs; blkid=$(blkid "${DEVICE}" 2>&1 || true)"
    die "mount ${DEVICE} on ${GRAPH} failed"
  }
fi

assert_ext4() {
  fstype=""
  src=""
  if command -v findmnt >/dev/null 2>&1; then
    fstype=$(findmnt -no FSTYPE "${GRAPH}")
    src=$(findmnt -no SOURCE "${GRAPH}")
  else
    # /proc/mounts is the same assertion without findmnt — not a driver fallback.
    while read -r dev mnt fs _rest; do
      if [ "${mnt}" = "${GRAPH}" ]; then
        fstype=${fs}
        src=${dev}
        break
      fi
    done </proc/mounts
  fi
  [ "${fstype}" = "ext4" ] || die "${GRAPH} fstype is '${fstype}', want ext4 (not virtiofs/tmpfs)"
  case "${src}" in
    "${DEVICE}" | "${DEVICE} "* | *"${DEVICE}") ;;
    *)
      real=$(readlink -f "${DEVICE}" 2>/dev/null || echo "${DEVICE}")
      case "${src}" in
        "${real}" | "${real} "*) ;;
        *) die "${GRAPH} source is '${src}', want block device ${DEVICE}" ;;
      esac
      ;;
  esac
}
assert_ext4

mkdir -p "${SOCK_DIR}"

# Path sharing: /tmp is a per-pod emptyDir mounted in both containers
# (Kata rejects mount --bind from /proc/PID/root). Persist dirs are
# bind-mounted from the kql root PVC when present.
if grep -q " /tmp " /proc/mounts; then
  log "/tmp is a shared volume"
else
  die "/tmp is not a mount; want path-share-tmp emptyDir in both containers"
fi

SLOT="${K7_PERSIST_SLOT:-main}"
if [ -d "/mnt/state/${SLOT}" ]; then
  for d in home root opt workspace; do
    persist="/mnt/state/${SLOT}/${d}"
    [ -d "${persist}" ] || continue
    if grep -q " /${d} " /proc/mounts; then
      umount "/${d}" 2>/dev/null || umount -l "/${d}" 2>/dev/null || true
    fi
    mkdir -p "/${d}"
    if mount --bind "${persist}" "/${d}"; then
      log "shared /${d} <- ${persist}"
    else
      log "bind ${persist} -> /${d} failed"
    fi
  done
fi

command -v dockerd >/dev/null 2>&1 || die "dockerd not found in vehicle image"
exec dockerd --host="unix://${SOCK}" --tls=false --storage-driver=overlay2
