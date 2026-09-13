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

install_docker_plugin() {
  src="$1"
  dst="$2"
  [ -x "$src" ] || {
    echo "k7-persist: missing docker plugin $src" >&2
    exit 1
  }
  mkdir -p "$(dirname "$dst")"
  # Docker CLI plugin discovery skips symlinks; copy a regular file.
  cp -a "$src" "$dst"
  chmod +x "$dst"
}

restore_k7_docker_cli() {
  # persist-bind overlays /usr and would hide the CLI subPath mounts.
  # Restage from the emptyDir at /run/k7/docker-cli (not under /usr).
  # Call after /usr and /root binds so these writes land on the PVC.
  if [ ! -x /run/k7/docker-cli/bin/docker ]; then
    return 0
  fi
  mkdir -p /usr/local/bin /usr/local/lib/docker/cli-plugins /root/.docker/cli-plugins
  cp -a /run/k7/docker-cli/bin/docker /usr/local/bin/docker
  chmod +x /usr/local/bin/docker
  install_docker_plugin /run/k7/docker-cli/cli-plugins/docker-compose \
    /usr/local/lib/docker/cli-plugins/docker-compose
  install_docker_plugin /run/k7/docker-cli/cli-plugins/docker-buildx \
    /usr/local/lib/docker/cli-plugins/docker-buildx
  install_docker_plugin /run/k7/docker-cli/cli-plugins/docker-compose \
    /root/.docker/cli-plugins/docker-compose
  install_docker_plugin /run/k7/docker-cli/cli-plugins/docker-buildx \
    /root/.docker/cli-plugins/docker-buildx
  if [ ! -f /root/.docker/config.json ]; then
    printf '%s\n' '{"cliPluginsExtraDirs":["/run/k7/docker-cli/cli-plugins"]}' >/root/.docker/config.json
  fi
  echo "k7-persist: docker CLI -> /run/k7/docker-cli" >&2
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
restore_k7_docker_cli

SCRIPT_END=$(date +%s)
SCRIPT_ELAPSED=$((SCRIPT_END - SCRIPT_START))
echo "k7-persist: script completed in ${SCRIPT_ELAPSED}s" >&2

# Directory socket share. Tools that ignore DOCKER_HOST and
# hardcode /var/run/docker.sock get a symlink; never overlay /var/run.
if [ -d /run/k7/docker ]; then
  mkdir -p /run
  ln -sfn /run/k7/docker/docker.sock /run/docker.sock
  if [ -d /var/run ]; then
    ln -sfn /run/k7/docker/docker.sock /var/run/docker.sock
  fi
  echo "k7-persist: docker.sock -> /run/k7/docker/docker.sock" >&2
fi

date > "${MIGRATION_MARK}" 2>/dev/null || true
if [ "$#" -eq 0 ]; then
  echo "k7-persist: no command provided; defaulting to keepalive sleep" >&2
  exec sleep 365d
fi
exec "$@"

