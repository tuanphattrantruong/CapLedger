#!/usr/bin/env python3
"""E18: focused replication of cooperative R2 callback behavior.

Measures both application pages freed and additional pool bytes released after
R1; the retained experiment is a negative result for additional byte release.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import common
import ctier_ffi as ffi
import e5_multitenant as e5


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "experiments/results/e18_r2_callback.json")
    failures: list[str] = []
    runs = []
    t0 = time.perf_counter()
    cfg = {"label": "analytic_ring_q0.99", "estimator": ffi.EST_ANALYTIC_RING}
    for i in range(10):
        r = e5.run_one(cfg, common.seed_for(i), failures)
        logical_pages = sum(x["freed"] for x in r["shed"].values())
        runs.append({
            "seed": r["seed"], "r2_calls": r["global"]["r2_shed_calls"],
            "application_pages_freed": logical_pages,
            "pool_bytes_released": r["global"]["r2_shed_bytes"],
            "violations": r["global"]["violations"],
            "invariant_breaches": r["global"]["invariant_breaches"],
        })
        print(f"[e18-r2] {i + 1}/10 pages={logical_pages} "
              f"bytes={runs[-1]['pool_bytes_released']}", flush=True)
    b = [r["pool_bytes_released"] for r in runs]
    p = [r["application_pages_freed"] for r in runs]
    result = {
        "experiment": "E18_R2_real_byte_reclamation", "runs": runs,
        "summary": {"runs": len(runs), "pages_freed_min": min(p),
                    "pages_freed_median": statistics.median(p),
                    "pages_freed_max": max(p), "bytes_released_min": min(b),
                    "bytes_released_median": statistics.median(b),
                    "bytes_released_max": max(b),
                    "all_runs_released_pool_bytes": all(x > 0 for x in b)},
        "callback_policy": "delete the 512 newest live opportunistic pages; R1 already targets the coldest",
        "scope": "cooperative application callback in the software model",
        "failures": failures, "wall_s": time.perf_counter() - t0,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
