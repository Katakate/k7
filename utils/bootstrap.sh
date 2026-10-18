#!/usr/bin/env bash
# Install prerequisites for k7 on Ubuntu 24.04: Ansible, the k7 CLI from the PPA, and
# Docker (for `make build` and `--api-image local`; a release `k7 install` needs none).
# Does NOT run `k7 install` — that requires the user to choose inventory, disk, backends, etc.
set -euo pipefail

# Same pins as the playbook's docker_* vars (k7-install-node.yaml; a unit test
# keeps them equal). Docker's apt repo keeps old versions.
DOCKER_APT_KEY_SHA256="1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570"
DOCKER_CE_VERSION="5:29.8.2-1~ubuntu.24.04~noble"
DOCKER_CONTAINERD_VERSION="2.3.6-1~ubuntu.24.04~noble"
DOCKER_BUILDX_VERSION="0.37.1-1~ubuntu.24.04~noble"

if [[ "${EUID:-}" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

[[ -r /etc/os-release ]] || {
  echo "Cannot read /etc/os-release" >&2
  exit 1
}
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_CODENAME:-}" == "noble" ]] || {
  echo "This script supports Ubuntu 24.04 (noble) only (ID=${ID:-unknown} VERSION_CODENAME=${VERSION_CODENAME:-unknown})" >&2
  exit 1
}

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1" >&2
    exit 1
  }
}

need_cmd apt-get
need_cmd curl

export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get install -y ca-certificates curl software-properties-common

add-apt-repository universe -y
add-apt-repository ppa:katakate.org/k7 -y

apt-get update -y
apt-get install -y ansible k7

curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$tmpdir/docker.asc"
echo "$DOCKER_APT_KEY_SHA256  $tmpdir/docker.asc" | sha256sum -c -
install -d -m 0755 /etc/apt/keyrings
install -m 0644 "$tmpdir/docker.asc" /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y --allow-downgrades --allow-change-held-packages \
  "docker-ce=$DOCKER_CE_VERSION" "docker-ce-cli=$DOCKER_CE_VERSION" \
  "containerd.io=$DOCKER_CONTAINERD_VERSION" "docker-buildx-plugin=$DOCKER_BUILDX_VERSION"
# Seen on a fresh node: docker.service came up failed after the package
# install (socket activation) until docker.socket was restarted with it.
# `docker version` fails the script if the daemon is still down.
systemctl enable docker.socket docker.service
systemctl restart docker.socket docker.service
docker version >/dev/null

echo "==> Bootstrap finished."
echo "    Ansible, Docker, and the k7 CLI are installed."
echo "    Next: run k7 install (with your inventory, --disk, backends, etc.) when ready."
