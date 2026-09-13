#!/usr/bin/env bash
# Classify a namespaced Kubernetes object's presence.
#
# Three states:
#   present — kubectl get succeeded
#   absent  — kubectl succeeded at talking to the apiserver and the object
#             is NotFound
#   unknown — any other kubectl failure (timeout, 500, "unable to handle
#             the request"). Retried, then exit 2. Never treat as absent.
#
# Usage: k8s-object-presence.sh <namespace> <kind> <name>
# stdout (exit 0): present | absent
# exit 2: unknown after retries
#
# K7_KUBECTL   — kubectl argv prefix (default: k3s kubectl)
# K7_PRESENCE_RETRIES — attempts (default: 5)
# K7_PRESENCE_DELAY   — seconds between attempts (default: 5)
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <namespace> <kind> <name>" >&2
  exit 2
fi

namespace=$1
kind=$2
name=$3
# Intentionally unquoted: K7_KUBECTL is an argv prefix ("k3s kubectl").
# shellcheck disable=SC2206
kubectl=(${K7_KUBECTL:-k3s kubectl})
retries=${K7_PRESENCE_RETRIES:-5}
delay=${K7_PRESENCE_DELAY:-5}

attempt=1
while [ "$attempt" -le "$retries" ]; do
  set +e
  out=$("${kubectl[@]}" -n "$namespace" get "$kind" "$name" 2>&1)
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    echo present
    exit 0
  fi
  if printf '%s' "$out" | grep -qiE 'notfound|not found'; then
    echo absent
    exit 0
  fi
  echo "kubectl error (attempt ${attempt}/${retries}): $out" >&2
  if [ "$attempt" -eq "$retries" ]; then
    echo unknown >&2
    exit 2
  fi
  sleep "$delay"
  attempt=$((attempt + 1))
done
exit 2
