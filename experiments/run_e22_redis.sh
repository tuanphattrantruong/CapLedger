#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
OUT=${E22_RESULTS_OUT:-$ROOT/experiments/results}
IMAGE=${E22_IMAGE:-redis@sha256:71da9275c5f3fcb97d0fa0c8c5b36cc995327265420f17a04bfd544f458059f7}
mkdir -p "$OUT"
OUT=$(CDPATH= cd -- "$OUT" && pwd)
docker run --rm --privileged --cgroupns=private --entrypoint sh \
  -v "$ROOT:/source:ro" -v "$OUT:/work/experiments/results" -w /work \
  "$IMAGE" \
  -c 'apt-get update -qq && apt-get install -y -qq python3 >/dev/null && python3 /source/experiments/e22_redis_memcg.py "$@"' sh "$@"
