#!/usr/bin/env bash
# Run IMAGE on examples/job.json the way mmli-backend does, then check the output.
# Usage: scripts/smoke-test.sh IMAGE
set -euo pipefail

image="${1:?usage: scripts/smoke-test.sh IMAGE}"
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
# The container writes as root, so remove its output from inside a container
trap 'docker run --rm --user 0:0 -v "$work:/uws" --entrypoint rm "$image" -rf /uws/in /uws/out; rm -rf "$work"' EXIT

mkdir -p "$work/in" "$work/out"
cp "$root/examples/job.json" "$work/in/job.json"

# Same env vars and user (root) as the backend, so default cache paths under /root/.cache work.
# Explicit, because some base images (e.g. micromamba) default to a non-root USER.
docker run --rm \
  --user 0:0 \
  -e JOB_ID=smoke-test \
  -e JOB_TYPE=smoke-test \
  -e JOB_INPUT_DIR=/uws/in \
  -e JOB_OUTPUT_DIR=/uws/out \
  -v "$work:/uws" \
  "$image"

python3 "$root/scripts/validate.py" "$work/out"
