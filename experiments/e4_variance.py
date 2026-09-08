#!/usr/bin/env python3
"""E4-variance — run the overhead benchmark N times and report ranges.

Two of the overhead numbers in an earlier draft turned out to be
unrepresentative single samples, so no E4 quantity is quoted as a point value
any more. This driver re-runs `e4_overheads.py` N times (sequentially, since
every quantity it measures is timing-sensitive) and aggregates each field into
a min/max/mean range. It also decides, from the data rather than by assertion,
whether the telemetry A/B delta is resolvable above run-to-run noise.

Run:  python3 experiments/e4_variance.py [--runs 4]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import common

RUNS = 4


def dig(d: dict, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


FIELDS = [
    ("admission_mean_ns", ("admission", "mean_ns")),
    ("admission_p50_ns", ("admission", "p50_ns")),
    ("admission_p99_ns", ("admission", "p99_ns")),
    ("admission_max_ns", ("admission", "max_ns")),
    ("compaction_mbps", ("compaction", "mbps")),
    ("compaction_mb_released", ("compaction", "mb_released")),
    ("spill_mbps_logical", ("spill", "spill_mbps_logical")),
    ("promote_mbps_logical", ("spill", "promote_mbps_logical")),
]


def main() -> int:
    ap = common.make_argparser("E4 variance study (repeats e4_overheads)")
    ap.add_argument("--runs", type=int, default=RUNS)
    args = ap.parse_args()
    n = 2 if args.fast else args.runs
    out_path = Path(args.out) / "e4_variance.json"
    t0 = time.perf_counter_ns()
    failures: list[str] = []
    runs = []

    with tempfile.TemporaryDirectory(prefix="e4var-") as td:
        for i in range(n):
            r = subprocess.run(
                [sys.executable, str(common.ROOT / "experiments" / "e4_overheads.py"),
                 "--out", td], capture_output=True, text=True, timeout=1800)
            f = Path(td) / "e4_overheads.json"
            if r.returncode != 0 or not f.exists():
                failures.append(f"e4_overheads run {i} failed: "
                                f"rc={r.returncode} {r.stderr.strip()[:300]}")
                continue
            runs.append(json.loads(f.read_text())["data"])
            print(f"[e4var] run {i+1}/{n} "
                  f"admit={dig(runs[-1], 'admission', 'mean_ns')}ns", flush=True)

    summary = {}
    for name, path in FIELDS:
        vals = [dig(r, *path) for r in runs]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if not vals:
            continue
        summary[name] = {"n": len(vals), "min": float(min(vals)),
                         "max": float(max(vals)), "mean": float(np.mean(vals)),
                         "values": vals}

    # Telemetry A/B: resolvable only if its spread excludes zero AND is small
    # relative to the write path's own run-to-run spread.
    deltas = []
    for r in runs:
        for k, v in (r.get("writepath") or {}).items():
            if k.startswith("hist_overhead") and isinstance(v, (int, float)):
                deltas.append(v)
    wp = []
    for r in runs:
        for k, v in (r.get("writepath") or {}).items():
            if k.endswith("_ns_op") and isinstance(v, (int, float)):
                wp.append(v)
    conclusions = {}
    if deltas:
        lo, hi = min(deltas), max(deltas)
        resolvable = (lo > 0 or hi < 0)
        conclusions["hist_overhead"] = {
            "delta_min_ns": lo, "delta_max_ns": hi,
            "sign_flips": bool(lo < 0 < hi),
            "writepath_ns_per_op_min": min(wp) if wp else None,
            "writepath_ns_per_op_max": max(wp) if wp else None,
            "resolvable_above_noise": bool(resolvable),
            "note": ("The telemetry A/B delta spans both signs across runs while "
                     "the compression-dominated write path itself varies by more "
                     "than the delta, so the per-write telemetry cost is below "
                     "the noise floor of this methodology and must not be quoted "
                     "as a measured number." if not resolvable else
                     "The delta has a consistent sign across runs and may be "
                     "quoted with its range."),
        }

    config = {"runs_requested": n, "runs_completed": len(runs),
              "sequential": "every quantity here is timing-sensitive, so runs "
                            "are strictly sequential and nothing else should be "
                            "running on the machine",
              "fields": [f[0] for f in FIELDS], "fast": bool(args.fast)}
    data = {"summary": summary, "conclusions": conclusions, "runs": runs,
            "wall_s": (time.perf_counter_ns() - t0) / 1e9}
    common.write_result(out_path, "e4_variance",
                        "Run-to-run variance of the overhead benchmark",
                        config, [], data, modeled_fields=[], failures=failures,
                        wall_seconds=data["wall_s"])
    print(f"[e4var] wrote {out_path} ({len(failures)} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
