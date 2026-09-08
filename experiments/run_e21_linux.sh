#!/bin/sh
# Run E21 in a disposable privileged Linux cgroup namespace.  The privileged
# scope is needed only to create nested cgroup-v2 memory-controller groups.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
OUT=${E21_RESULTS_OUT:-$ROOT/experiments/results}
IMAGE=${E21_IMAGE:-python@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc}
mkdir -p "$OUT"
OUT=$(CDPATH= cd -- "$OUT" && pwd)
docker run --rm --privileged --cgroupns=private \
  -v "$ROOT:/source:ro" -v "$OUT:/results" -w /source \
  "$IMAGE" \
  python experiments/e21_linux_memcg.py "$@" \
    --out /results/e21_linux_memcg.json
