#!/usr/bin/env bash
# Install prerequisites for k7 on Ubuntu: Ansible, Docker (get.docker.com), and the k7 CLI from the PPA.
# Does NOT run `k7 install` — that requires the user to choose inventory, disk, backends, etc.
set -euo pipefail

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
[[ "${ID:-}" == "ubuntu" ]] || {
  echo "This script supports Ubuntu only (ID=${ID:-unknown})" >&2
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

curl -fsSL https://get.docker.com -o "$tmpdir/get-docker.sh"
sh "$tmpdir/get-docker.sh"

echo "==> Bootstrap finished."
echo "    Ansible, Docker, and the k7 CLI are installed."
echo "    Next: run k7 install (with your inventory, --disk, backends, etc.) when ready."
