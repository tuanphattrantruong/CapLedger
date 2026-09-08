#!/usr/bin/env bash
# Portable macOS emulator suite. Sequential at the top level: each experiment
# runs its own process pool across the performance cores, so overlapping them
# would oversubscribe the machine and corrupt timing-sensitive measurements.
#
# FAST=1 passes --fast and writes below build/results-smoke by default.  Smoke
# runs therefore cannot overwrite the retained paper evidence.  E12/E13 have
# C repeat drivers; E18-E20 are focused/large runs; and E21-E23 require their
# dedicated Linux runners.  ARTIFACT.md gives those commands.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PY:-.venv/bin/python}"
PAPER_RESULTS="$ROOT/experiments/results"
if [ "${FAST:-0}" = "1" ]; then
    OUTDIR="${SMOKE_OUT:-$ROOT/build/results-smoke}"
    echo "[run_all] FAST=1: smoke mode; results are NOT paper numbers"
else
    OUTDIR="${RESULTS_OUT:-$PAPER_RESULTS}"
fi
mkdir -p "$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd -P)"
if [ "${FAST:-0}" = "1" ] && [ "$OUTDIR" = "$PAPER_RESULTS" ]; then
    echo "[run_all] refusing to write a smoke run into $PAPER_RESULTS" >&2
    exit 2
fi
if [ "${FAST:-0}" = "1" ]; then
    RUN_ARGS=(--fast --out "$OUTDIR")
else
    RUN_ARGS=(--out "$OUTDIR")
fi
echo "[run_all] output: $OUTDIR"

step() {
    local name="$1"; shift
    echo ""
    echo "=== [$name] $* @ $(date -u +%H:%M:%S)"
    local t0; t0=$(date +%s)
    local rc
    if "$@"; then
        rc=0
    else
        rc=$?
    fi
    echo "=== [$name] rc=$rc in $(( $(date +%s) - t0 ))s"
    return "$rc"
}

step "build"  make all
step "test"   ./build/test_core
step "pytest" "$PY" -m unittest discover -s tests
step "corpus" "$PY" workloads/gen_corpus.py

# E3/E4/E10 are timing-sensitive or memory-heavy and run their cells
# sequentially by construction. Every common-driver experiment receives an
# explicit output directory, including full runs, so callers can stage results
# safely with RESULTS_OUT=/path.
for e in e0_calibration e1_accuracy e2_frontier e3_phase_stress e4_overheads \
         e4_variance e5_multitenant e6_regime e7_realstate e8_detector \
         e9_strategic e10_scale e11_sensitivity e14_dependence \
         e15_drift_adversary e16_interface_model e17_reactive_baseline \
         e25_tracker_scale; do
    step "$e" "$PY" "experiments/$e.py" "${RUN_ARGS[@]}"
done

# E24 is derived from E14 and uses a file output rather than common.py's output
# directory convention.
step "e24_calibration_guard" "$PY" experiments/e24_calibration_guard.py \
    --input "$OUTDIR/e14_dependence.json" \
    --out "$OUTDIR/e24_calibration_guard.json"

# The claims audit is the last step on purpose: it reads the files the suite
# just wrote and checks the subset of the paper's argument exercised here.
# The strict, complete-evidence audit is reviews/verify_artifact.sh.
step "verify" "$PY" experiments/verify_claims.py --results-dir "$OUTDIR"

echo ""
echo "=== results ==="
ls -l "$OUTDIR"/*.json
echo "ALL DONE"
