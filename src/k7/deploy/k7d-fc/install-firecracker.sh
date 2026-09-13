#!/usr/bin/env bash
# Vendored from k7d utils/install-firecracker.sh.
# Pins live next to this script (not k7d's guest/fc/pins.env).
#
# Installs stock Firecracker + jailer into /usr/local/bin (NOT Kata's
# /opt/kata/bin). A sha mismatch is fatal, never a warning.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINS="${HERE}/pins.env"
BIN_DIR="/usr/local/bin"
CACHE="/var/cache/k7d-firecracker"

die() { echo "install-firecracker: $*" >&2; exit 1; }

[[ -f "${PINS}" ]] || die "missing pin file ${PINS}"
# shellcheck source=/dev/null
source "${PINS}"
for var in FIRECRACKER_VERSION FIRECRACKER_RELEASE_URL FIRECRACKER_TGZ_SHA256 \
           FIRECRACKER_SHA256 JAILER_SHA256; do
    [[ -n "${!var:-}" ]] || die "${PINS} does not set ${var}"
done

[[ "$(id -u)" -eq 0 ]] || die "must run as root (installs into ${BIN_DIR})"
command -v curl >/dev/null || die "curl is required"
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v tar >/dev/null || die "tar is required"

FC_DST="${BIN_DIR}/firecracker-${FIRECRACKER_VERSION}"
JAILER_DST="${BIN_DIR}/jailer-${FIRECRACKER_VERSION}"

digest() { sha256sum "$1" | cut -d' ' -f1; }

if [[ -x "${FC_DST}" && -x "${JAILER_DST}" \
      && "$(digest "${FC_DST}")" == "${FIRECRACKER_SHA256}" \
      && "$(digest "${JAILER_DST}")" == "${JAILER_SHA256}" \
      && "$(readlink -f "${BIN_DIR}/firecracker" 2>/dev/null)" == "${FC_DST}" \
      && "$(readlink -f "${BIN_DIR}/jailer" 2>/dev/null)" == "${JAILER_DST}" ]]; then
    echo "install-firecracker: already ${FIRECRACKER_VERSION} at ${FC_DST}"
    exit 0
fi

mkdir -p "${CACHE}"
tgz="${CACHE}/firecracker-${FIRECRACKER_VERSION}-x86_64.tgz"
if [[ -f "${tgz}" && "$(digest "${tgz}")" != "${FIRECRACKER_TGZ_SHA256}" ]]; then
    echo "install-firecracker: cached tarball digest is stale, refetching"
    rm -f "${tgz}"
fi
if [[ ! -f "${tgz}" ]]; then
    echo "install-firecracker: downloading ${FIRECRACKER_RELEASE_URL}"
    curl -fsSL -o "${tgz}.part" "${FIRECRACKER_RELEASE_URL}"
    mv "${tgz}.part" "${tgz}"
fi
got="$(digest "${tgz}")"
[[ "${got}" == "${FIRECRACKER_TGZ_SHA256}" ]] \
    || die "tarball sha256 ${got}, pinned ${FIRECRACKER_TGZ_SHA256}"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
rel="release-${FIRECRACKER_VERSION}-x86_64"
tar -xzf "${tgz}" -C "${work}" \
    "${rel}/firecracker-${FIRECRACKER_VERSION}-x86_64" \
    "${rel}/jailer-${FIRECRACKER_VERSION}-x86_64"

install_one() {
    local src="$1" dst="$2" want="$3" link="$4"
    local got
    got="$(digest "${src}")"
    [[ "${got}" == "${want}" ]] || die "$(basename "${dst}") sha256 ${got}, pinned ${want}"
    install -m 755 "${src}" "${dst}.new"
    mv -f "${dst}.new" "${dst}"
    ln -sfn "${dst}" "${link}"
}

install_one "${work}/${rel}/firecracker-${FIRECRACKER_VERSION}-x86_64" \
            "${FC_DST}" "${FIRECRACKER_SHA256}" "${BIN_DIR}/firecracker"
install_one "${work}/${rel}/jailer-${FIRECRACKER_VERSION}-x86_64" \
            "${JAILER_DST}" "${JAILER_SHA256}" "${BIN_DIR}/jailer"

reported="$("${FC_DST}" --version | head -1)"
[[ "${reported}" == *"${FIRECRACKER_VERSION#v}"* ]] \
    || die "installed binary reports ${reported}, want ${FIRECRACKER_VERSION}"
echo "install-firecracker: ${FIRECRACKER_VERSION} at ${FC_DST} (${reported})"
