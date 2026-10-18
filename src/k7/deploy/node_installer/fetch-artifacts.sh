#!/usr/bin/env bash
# Build-time only: fetch artifacts.txt into DEST and verify every sha256.
# Usage: fetch-artifacts.sh <artifacts.txt> <dest-dir>
set -euo pipefail

list="${1:?usage: fetch-artifacts.sh <artifacts.txt> <dest-dir>}"
dest="${2:?usage: fetch-artifacts.sh <artifacts.txt> <dest-dir>}"
mkdir -p "${dest}"

while read -r file sha url; do
    [[ -z "${file}" || "${file}" == \#* ]] && continue
    [[ -n "${sha}" && -n "${url}" ]] || { echo "malformed line for ${file}" >&2; exit 1; }
    echo "fetch-artifacts: ${url}"
    curl -fsSL --retry 5 --retry-delay 10 -o "${dest}/${file}" "${url}"
    echo "${sha}  ${dest}/${file}" | sha256sum -c -
done < "${list}"
