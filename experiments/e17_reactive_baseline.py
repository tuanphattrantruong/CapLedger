#!/usr/bin/env python3
"""E17 - exact-size, tenant-aware reactive baseline on the E5 trace.

This runs only the added baseline so the audited 20-seed E5 result file remains
unchanged. The device, trace, page order, and victim implementation are shared
with E5; the arm removes admission pricing and reservations while retaining
tenant-aware owned-minus-purchased-byte overdraft.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import e5_multitenant as e5

N_SEEDS = 5


def main() -> int:
    ap = common.make_argparser("E17 tenant-aware exact-accounting baseline")
    args = ap.parse_args()
    n = common.fast_trials(N_SEEDS, args.fast)
    cfg = {"label": "reactive_tenant", "estimator": ffi.EST_REACTIVE_TENANT}
    failures: list[str] = []
    t0 = time.perf_counter_ns()
    runs = []
    for i in range(n):
        r = e5.run_one(cfg, common.seed_for(i), failures)
        runs.append(r)
        iso = r["isolation"]
        print(f"[e17] seed{i} bomber={iso['bomber_violations']} "
              f"victims={iso['total_victim_violations']}", flush=True)
    summary = {
        "label": cfg["label"], "runs": n,
        "bomber_violations_mean": float(np.mean(
            [r["isolation"]["bomber_violations"] for r in runs])),
        "victim_violations_mean": float(np.mean(
            [r["isolation"]["total_victim_violations"] for r in runs])),
        "forced_spills_mean": float(np.mean(
            [r["global"]["r3_forced_spills"] for r in runs])),
        "hard_failures_total": int(sum(
            r["isolation"]["hard_failures"] for r in runs)),
        "invariant_breaches_total": int(sum(
            r["isolation"]["invariant_breaches"] for r in runs)),
    }
    common.write_result(Path(args.out) / "e17_reactive_baseline.json", "E17",
                        "Tenant-aware exact-accounting reactive baseline",
                        {"trace": "identical E5 80k-op entropy bomb",
                         "pool_bytes": e5.POOL, "extent_bytes": e5.EXTENT,
                         "n_tenants": e5.NTENANTS, "seeds": n,
                         "admission": "always open", "reservation": "none",
                         "victim_order": "owned-minus-purchased byte overdraft, then CLOCK"},
                        [common.seed_for(i) for i in range(n)],
                        {"summary": summary, "runs": runs},
                        modeled_fields=[], failures=failures,
                        wall_seconds=(time.perf_counter_ns() - t0) / 1e9)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
