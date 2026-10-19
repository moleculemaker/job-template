#!/usr/bin/env bash
# Run IMAGE on examples/job.json the way mmli-backend does, then check the output.
# Usage: scripts/smoke-test.sh IMAGE
set -euo pipefail

image="${1:?usage: scripts/smoke-test.sh IMAGE}"
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/in" "$work/out"
cp "$root/examples/job.json" "$work/in/job.json"

# Same env vars the backend sets; run as the current user so the output is removable
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e JOB_ID=smoke-test \
  -e JOB_TYPE=smoke-test \
  -e JOB_INPUT_DIR=/uws/in \
  -e JOB_OUTPUT_DIR=/uws/out \
  -v "$work:/uws" \
  "$image"

python3 "$root/scripts/validate.py" "$work/out"
