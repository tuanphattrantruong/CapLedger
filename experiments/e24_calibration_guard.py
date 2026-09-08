#!/usr/bin/env python3
"""E24: a conservative, deliberately non-certifying eligibility screen.

E14 shows that none of its empirical estimators has distribution-free
coverage on ordered/dependent inputs. This replay makes the safe fallback
executable: a stratum is charged at the deterministic 4 KiB/page cap unless a
*predeclared* estimator passes a deliberately strict diagnostic screen. The
screen requires 299 earlier zero-exceedance batches, E14's stationary label,
and low measured page-size autocorrelation. Those diagnostics do not prove
independence, stationarity, representativeness, or future coverage, so passing
would enable only an experimental branch, never a statistical certificate.

The first 299 batches are always accounted at the raw-page cap. A later
empirical exceedance immediately returns the stratum to that cap. Only the
4 KiB/page branch is guaranteed under arbitrary dependence. This script
consumes E14's frozen q=0.99 ordered batches and makes no new measurement.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import time
from pathlib import Path


FROZEN_Q = 0.99
IID_SCREEN_CONFIDENCE = 0.95
PREDECLARED_METHOD = "iid_normal"


def iid_zero_event_sample_floor(q: float, confidence: float) -> int:
    """IID Bernoulli reference size; this is a screen, not a certificate."""
    return math.ceil(math.log(1.0 - confidence) / math.log(q))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path,
                        default=Path("experiments/results/e14_dependence.json"))
    parser.add_argument("--out", type=Path,
                        default=Path("experiments/results/e24_calibration_guard.json"))
    parser.add_argument("--max-tau", type=float, default=1.10)
    args = parser.parse_args()
    begin = time.perf_counter()
    source = json.loads(args.input.read_text())
    source_q = float(source["config"]["q"])
    if not math.isclose(source_q, FROZEN_Q):
        raise SystemExit(
            f"E24 is defined only for E14's frozen q={FROZEN_Q}; got {source_q}"
        )
    rows = source["data"]["rows"]
    screen_batches = iid_zero_event_sample_floor(
        FROZEN_Q, IID_SCREEN_CONFIDENCE
    )
    groups: dict[tuple, list[dict]] = collections.defaultdict(list)
    for row in rows:
        groups[(row["kind"], row["source"], row["n"])].append(row)

    details = []
    total_eval = total_eval_pages = total_violations = 0
    raw_cap_violations = raw_batches = empirical_batches = 0
    reserve_bytes = demand_bytes = 0.0
    for (kind, name, batch), group in sorted(groups.items()):
        # These are only conservative diagnostics. In particular, the E14
        # kind label and page-size tau do not establish independent batch
        # exceedance outcomes.
        enough = len(group) > screen_batches
        prefix = group[:screen_batches] if enough else []
        tau = sum(row["tau"] for row in prefix) / len(prefix) if prefix else None
        labelled_stationary = kind == "stationary_ordered"
        low_page_size_tau = tau is not None and tau <= args.max_tau
        zero_prefix_exceedances = bool(prefix) and not any(
            row["violations"][PREDECLARED_METHOD] for row in prefix
        )
        eligible = (enough and labelled_stationary and low_page_size_tau
                    and zero_prefix_exceedances)
        selected = PREDECLARED_METHOD if eligible else "raw_page_cap"
        initial = selected
        tripped = False
        violations = raw_violations = 0
        group_raw = group_empirical = 0

        # Screening observations are paid for pessimistically. If the screen
        # passes, only the later suffix can use the experimental estimate.
        for index, row in enumerate(group):
            method = "raw_page_cap" if index < screen_batches else selected
            reserve = min(row["reserve"][method], row["reserve"]["raw_page_cap"])
            violated = row["demand"] > reserve
            violations += int(violated)
            raw_violations += int(violated and method == "raw_page_cap")
            if method == "raw_page_cap":
                group_raw += 1
            else:
                group_empirical += 1
            reserve_bytes += reserve
            demand_bytes += row["demand"]
            if violated and method != "raw_page_cap":
                selected = "raw_page_cap"
                tripped = True

        total_eval += len(group)
        total_eval_pages += sum(row["n"] for row in group)
        total_violations += violations
        raw_cap_violations += raw_violations
        raw_batches += group_raw
        empirical_batches += group_empirical
        details.append({
            "kind": kind, "source": name, "batch_pages": batch,
            "available_batches": len(group),
            "screen_batches_required": screen_batches,
            "screen_batches_available": len(prefix),
            "screen_tau_mean": tau,
            "screen_stationary_label": labelled_stationary,
            "screen_low_page_size_tau": low_page_size_tau,
            "screen_zero_iid_normal_exceedances": zero_prefix_exceedances,
            "screen_passed": eligible,
            "post_screen_initial_policy": initial,
            "final_policy": selected,
            "tripped_after_exceedance": tripped,
            "evaluated_batches": len(group),
            "empirical_batches": group_empirical,
            "raw_cap_batches": group_raw,
            "violations": violations,
            "raw_cap_violations": raw_violations,
        })

    result = {
        "schema": "ccap-results-v1", "experiment": "E24",
        "title": "Fail-closed eligibility screen on ordered/dependent batches",
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_seconds": round(time.perf_counter() - begin, 3),
        "config": {
            "frozen_q": FROZEN_Q,
            "predeclared_method": PREDECLARED_METHOD,
            "iid_reference_confidence": IID_SCREEN_CONFIDENCE,
            "screen_batches": screen_batches,
            "max_page_size_integrated_autocorrelation_time": args.max_tau,
            "input": str(args.input),
        },
        "summary": {
            "strata": len(details), "evaluated_batches": total_eval,
            "violations": total_violations,
            "raw_cap_violations": raw_cap_violations,
            "violation_rate": total_violations / max(1, total_eval),
            "empirical_batches": empirical_batches,
            "raw_cap_batches": raw_batches,
            "mean_reserved_bytes_per_page": reserve_bytes /
                max(1, total_eval_pages),
            "aggregate_demand_over_reserve": demand_bytes /
                max(1.0, reserve_bytes),
        },
        "strata": details,
        "guarantee": (
            "Only the 4096-byte/page branch is deterministic under arbitrary "
            "dependence. The stationary label, page-size autocorrelation, and "
            "299 prior batches with no i.i.d.-normal exceedance are conservative "
            "diagnostics, not a test of "
            "independent representative batch outcomes and not a coverage "
            "certificate. A passing stratum would remain experimental."
        ),
        "metric_classes": {
            "counted": ["batches, selected branch, exceedances"],
            "derived": ["rates and reserved bytes from frozen E14"],
            "measured": [], "modeled": [],
        },
        "failures": ([] if raw_cap_violations == 0 else [
            f"raw-page cap was exceeded by {raw_cap_violations} batches"
        ]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
