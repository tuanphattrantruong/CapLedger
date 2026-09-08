#!/usr/bin/env bash
# One command that checks this package: native build, unit tests, and every
# stored-evidence claim. Needs no network, no privilege, and no container.
set -uo pipefail
cd "$(dirname "$0")"
PY="${PY:-python3}"
fail=0

run() {
    printf '%-30s' "$1"; shift
    if out=$("$@" 2>&1); then
        echo "ok    $(echo "$out" | tail -1 | cut -c1-70)"
    else
        echo "FAIL"; echo "$out" | tail -15; fail=1
    fi
}
run_expect() {
    local label="$1" expected="$2" out
    shift 2
    printf '%-30s' "$label"
    if out=$("$@" 2>&1) && grep -Eq "$expected" <<<"$out"; then
        echo "ok    $(echo "$out" | tail -1 | cut -c1-70)"
    else
        echo "FAIL"; echo "$out" | tail -15
        echo "expected output matching: $expected"; fail=1
    fi
}

run        "build"              make -s all
run_expect "C unit tests"       'all 16 tests passed' ./build/test_core
run_expect "Python unit tests"  '^Ran 37 tests' "$PY" -m unittest discover -s tests
run        "evidence + claims"  "$PY" experiments/verify_claims.py --strict --manifest manifest.json

echo
if [ "$fail" -eq 0 ]; then echo "all checks passed"; else echo "one or more checks failed"; fi
exit $fail
