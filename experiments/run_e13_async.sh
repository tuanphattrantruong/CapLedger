#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
OUT=${E13_RESULTS_OUT:-$ROOT/experiments/results}
PY=${PY:-.venv/bin/python}
REPS=${REPS:-3}
RECORDS=${RECORDS:-100000}
GAP_NS=${GAP_NS:-5000}
mkdir -p "$OUT"
OUT=$(CDPATH= cd -- "$OUT" && pwd)
make build/feedback_async
set --
for r in $(seq 1 "$REPS"); do
  result="$OUT/e13_async_r${r}.json"
  build/feedback_async --records "$RECORDS" --producer-gap-ns "$GAP_NS" \
    --out "$result"
  set -- "$@" "$result"
done
"$PY" experiments/summarize_e13.py "$@" \
  > "$OUT/e13_async_summary.json"
echo "E13 summary: $OUT/e13_async_summary.json"
