#!/bin/sh
# Build the portable C11 completion queue and run E23 in a disposable,
# privileged Linux cgroup-v2 namespace.  The image contains Redis itself; the
# package install provides only Python and a C compiler for the queue library.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
OUT=${E23_RESULTS_OUT:-$ROOT/experiments/results}
IMAGE=${E23_IMAGE:-redis@sha256:71da9275c5f3fcb97d0fa0c8c5b36cc995327265420f17a04bfd544f458059f7}
mkdir -p "$OUT"
OUT=$(CDPATH= cd -- "$OUT" && pwd)
docker run --rm --privileged --cgroupns=private --entrypoint sh \
  -v "$ROOT:/source:ro" -v "$OUT:/results" -w /source \
  "$IMAGE" \
  -c 'apt-get update -qq && apt-get install -y -qq python3 gcc libc6-dev >/dev/null && \
      gcc -std=c11 -O2 -fPIC -shared -pthread -Isrc/include \
          src/ctier/feedback_queue.c -o /tmp/libfeedback.so && \
      python3 experiments/e23_end_to_end_redis.py "$@" \
          --feedback-lib /tmp/libfeedback.so \
          --out /results/e23_end_to_end_redis.json' sh "$@"
