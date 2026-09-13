#!/bin/sh
# kfd: Kata Firecracker virtio-fs subPath file mounts are not visible to
# Docker CLI plugin discovery. Stage compose/buildx from the directory
# emptyDir at /run/k7/docker-cli (kql persist-bind already does this).
set -eu

STAGING="${K7_DOCKER_CLI_STAGING:-/run/k7/docker-cli}"
mkdir -p /root/.docker/cli-plugins
if [ -d "${STAGING}/cli-plugins" ]; then
  for p in "${STAGING}/cli-plugins"/*; do
    [ -e "${p}" ] || continue
    dest="/root/.docker/cli-plugins/$(basename "${p}")"
    cp -a "${p}" "${dest}"
    chmod +x "${dest}"
  done
fi
if [ ! -f /root/.docker/config.json ]; then
  printf '%s\n' "{\"cliPluginsExtraDirs\":[\"${STAGING}/cli-plugins\"]}" >/root/.docker/config.json
fi
if [ "$#" -eq 0 ]; then
  exec sleep 365d
fi
exec "$@"
