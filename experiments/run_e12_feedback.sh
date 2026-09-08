#!/bin/sh
# Repeat E12 sequentially so timing runs do not contend with one another, then
# regenerate the retained aggregate used by paper/data/tables.py.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
OUT=${E12_RESULTS_OUT:-$ROOT/experiments/results}
PY=${PY:-.venv/bin/python}
REPS=${REPS:-3}
RECORDS=${RECORDS:-1000000}
mkdir -p "$OUT"
OUT=$(CDPATH= cd -- "$OUT" && pwd)

make build/feedback
r=1
set --
while [ "$r" -le "$REPS" ]; do
  result="$OUT/e12_feedback_serial_r${r}.json"
  build/feedback --records "$RECORDS" \
    --out "$result"
  set -- "$@" "$result"
  r=$((r + 1))
done
"$PY" experiments/summarize_e12.py "$@" \
  > "$OUT/e12_feedback_summary.json"
echo "E12 summary: $OUT/e12_feedback_summary.json"
