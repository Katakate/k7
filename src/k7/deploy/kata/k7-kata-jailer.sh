#!/bin/bash
# Kata 3.24.0 fcInit always passes --daemonize. Firecracker's jailer then
# double-forks: the parent exits (Kata records that PID), the grandchild
# execs Firecracker and is reparented to init. fcEnd's SIGTERM hits the
# already-dead jailer PID and returns success, so every kfd delete leaves
# the microVM running. Drop the flag. Without it the jailer execs
# Firecracker in this same PID, and the shim's SIGTERM reaches the VMM.
# No retry, no sleep: one exec.
set -euo pipefail

jailer=/usr/local/bin/jailer
if [[ ! -x "$jailer" ]]; then
    echo "k7-kata-jailer: $jailer is not executable" >&2
    exit 1
fi

args=()
for arg in "$@"; do
    if [[ "$arg" == "--daemonize" ]]; then
        continue
    fi
    args+=("$arg")
done

exec "$jailer" "${args[@]}"
