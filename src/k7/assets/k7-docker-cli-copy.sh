#!/bin/sh
# Copy docker CLI + compose/buildx into a per-pod emptyDir.
# Fail loud if a source path is missing — plugin layout differs by image.
set -eu

DEST="${1:-/cli}"
mkdir -p "${DEST}/bin" "${DEST}/cli-plugins"

src_docker=""
for p in /usr/local/bin/docker /usr/bin/docker; do
  if [ -x "${p}" ]; then
    src_docker=${p}
    break
  fi
done
[ -n "${src_docker}" ] || {
  echo "k7-docker-cli-copy: docker CLI not found in vehicle image" >&2
  exit 1
}
cp -a "${src_docker}" "${DEST}/bin/docker"

for name in docker-compose docker-buildx; do
  found=""
  for p in \
    "/usr/local/libexec/docker/cli-plugins/${name}" \
    "/usr/libexec/docker/cli-plugins/${name}" \
    "/usr/local/lib/docker/cli-plugins/${name}" \
    "/usr/lib/docker/cli-plugins/${name}"; do
    if [ -e "${p}" ]; then
      found=${p}
      break
    fi
  done
  [ -n "${found}" ] || {
    echo "k7-docker-cli-copy: ${name} not found in dind image (searched libexec/lib plugin dirs)" >&2
    ls -la /usr/local/libexec/docker/cli-plugins /usr/libexec/docker/cli-plugins \
      /usr/local/lib/docker/cli-plugins /usr/lib/docker/cli-plugins 2>&1 || true
    exit 1
  }
  cp -a "${found}" "${DEST}/cli-plugins/${name}"
done
