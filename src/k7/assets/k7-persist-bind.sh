#!/bin/sh
# shellcheck disable=SC3040  # pipefail: guarded probe, not assumed
set -eu
(set -o pipefail) 2>/dev/null && set -o pipefail || true

SCRIPT_START=$(date +%s)

STATE_MNT="/mnt/state"
SLOT="${K7_PERSIST_SLOT:?missing K7_PERSIST_SLOT}"

READY_FILE="${STATE_MNT}/.ready"
BASE="${STATE_MNT}/${SLOT}"
MIGRATION_MARK="${BASE}/.migrated_v1"

i=0
while [ ! -f "${READY_FILE}" ]; do
  i=$((i+1))
  if [ "$i" -gt 600 ]; then
    echo "k7-persist: timeout waiting for ${READY_FILE}" >&2
    exit 1
  fi
  sleep 0.1
done

mkdir -p "${BASE}"

is_symlink() { [ -L "$1" ]; }
is_dir() { [ -d "$1" ]; }

require_tool() {
  tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "k7-persist: missing required tool '$tool' in image" >&2
    exit 1
  fi
}

require_tool tar
require_tool mount
if [ ! -x /bin/sh ]; then
  echo "k7-persist: missing required shell /bin/sh in image" >&2
  exit 1
fi

copy_if_empty() {
  src="$1"
  dst="$2"
  mkdir -p "$dst"
  if [ -z "$(ls -A "$dst" 2>/dev/null || true)" ]; then
    echo "k7-persist: seeding $dst from $src (one-time)..."
    (cd "$src" && tar -cpf - .) | (cd "$dst" && tar -xpf -)
  fi
}

bind_mount_dir() {
  target="$1"
  name="$2"
  if [ ! -e "$target" ]; then
    return 0
  fi
  if is_symlink "$target"; then
    echo "k7-persist: skip symlink $target"
    return 0
  fi
  if ! is_dir "$target"; then
    echo "k7-persist: skip non-dir $target"
    return 0
  fi
  persist="${BASE}/${name}"
  copy_if_empty "$target" "$persist"
  mount --make-rprivate / || true
  mount --bind "$persist" "$target"
  echo "k7-persist: bound $target -> $persist"
}

bind_mount_dir /etc etc
bind_mount_dir /var var
bind_mount_dir /usr usr
bind_mount_dir /home home
bind_mount_dir /root root
bind_mount_dir /opt opt
bind_mount_dir /bin bin
bind_mount_dir /sbin sbin
bind_mount_dir /lib lib
bind_mount_dir /lib64 lib64

SCRIPT_END=$(date +%s)
SCRIPT_ELAPSED=$((SCRIPT_END - SCRIPT_START))
echo "k7-persist: script completed in ${SCRIPT_ELAPSED}s" >&2

date > "${MIGRATION_MARK}" 2>/dev/null || true
if [ "$#" -eq 0 ]; then
  echo "k7-persist: no command provided; defaulting to keepalive sleep" >&2
  exec sleep 365d
fi
exec "$@"

