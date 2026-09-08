#!/usr/bin/env python3
"""Reviewer-requested cost mapping and exact finite-sample detector intervals."""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "experiments/results"


def binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p**i * (1 - p)**(n - i)
               for i in range(k + 1))


def cp95(x: int, n: int) -> list[float]:
    """Two-sided exact Clopper--Pearson interval, alpha=.05."""
    if n == 0:
        return [0.0, 1.0]
    if x == 0:
        lo = 0.0
    else:
        a, b = 0.0, 1.0
        target = .975
        for _ in range(90):
            m = (a + b) / 2
            if binom_cdf(x - 1, n, m) > target:
                a = m
            else:
                b = m
        lo = (a + b) / 2
    if x == n:
        hi = 1.0
    else:
        a, b = 0.0, 1.0
        target = .025
        for _ in range(90):
            m = (a + b) / 2
            if binom_cdf(x, n, m) > target:
                a = m
            else:
                b = m
        hi = (a + b) / 2
    return [lo, hi]


def main() -> int:
    e7 = json.loads((RES / "e7_realstate.json").read_text())
    e8 = json.loads((RES / "e8_detector.json").read_text())
    e4 = json.loads((RES / "e4_overheads.json").read_text())
    promote_pps = e4["data"]["spill"]["promote_pages_per_s"]

    costs = []
    for r in e7["data"]["transitions"]:
        v = r["violations"]
        reuse = r["sample_promote_frac"]
        ns_page = r["sample_read_ns_per_page"]
        costs.append({
            "transition": r["transition"], "codec": r["codec"],
            "violations": v, "sampled_post_transition_reuse_fraction": reuse,
            "measured_sample_read_ns_per_page": ns_page,
            "modeled_expected_refaults": v * reuse,
            "modeled_expected_added_read_ms": v * reuse * ns_page / 1e6,
            "modeled_expected_promotion_ms_at_e4_rate": v * reuse / promote_pps * 1000,
            "one_refault_per_violation_upper_ms": v * ns_page / 1e6,
        })

    intervals = []
    for s in e8["data"]["summary"]:
        n = s["runs"]
        x = s["cusum"]["detected"]
        fa = s["cusum"]["false_positive_runs"]
        intervals.append({"drift_rate": s["drift_rate"], "detector": "device_cusum",
                          "detections": x, "runs": n,
                          "detection_rate_cp95": cp95(x, n),
                          "false_alarm_runs": fa,
                          "false_alarm_rate_cp95": cp95(fa, n)})
        for threshold, d in s["ratio"].items():
            x = d["detected"]
            # Summary retained a rate, while raw runs retain exact booleans.
            fa = sum(1 for r in e8["data"]["runs"]
                     if r["drift_rate"] == s["drift_rate"] and
                     r["thresholds"][threshold]["false_positive_ops"] > 0)
            intervals.append({"drift_rate": s["drift_rate"],
                              "detector": f"ratio_{threshold}",
                              "detections": x, "runs": n,
                              "detection_rate_cp95": cp95(x, n),
                              "false_alarm_runs": fa,
                              "false_alarm_rate_cp95": cp95(fa, n)})

    result = {
        "experiment": "E19_review_requested_analysis",
        "application_cost_mapping": costs,
        "application_cost_scope": "counterfactual mapping using E7 sampled post-transition reuse and measured read latency; not an end-to-end application SLO",
        "e4_measured_promotion_pages_per_s": promote_pps,
        "detector_exact_intervals": intervals,
        "interval_method": "two-sided 95% Clopper-Pearson exact binomial",
        "interpretation": "for 0 events in 12 runs, the exact upper bound is about 26.5%; zero observed is not evidence of zero population risk",
    }
    out = RES / "e19_review_analysis.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(out), "cost_rows": len(costs),
                      "interval_rows": len(intervals),
                      "zero_of_12_cp95": cp95(0, 12)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
