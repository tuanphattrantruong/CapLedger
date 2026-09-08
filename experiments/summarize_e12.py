#!/usr/bin/env python3
"""Aggregate sequential E12 repeats into the retained summary JSON."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", type=Path)
    args = parser.parse_args()
    runs = [json.loads(path.read_text()) for path in args.results]
    records = {run["throughput"]["records"] for run in runs}
    record_bytes = {run["meta"]["record_bytes"] for run in runs}
    forced_loss = {run["loss"]["gap_records"] for run in runs}
    forced_debt = {run["loss"]["debt_before_resync"] for run in runs}
    if any(len(values) != 1 for values in
           (records, record_bytes, forced_loss, forced_debt)):
        raise SystemExit("E12 repeats disagree on fixed configuration")
    if any(run["loss"]["debt_after_resync"] != 0 for run in runs):
        raise SystemExit("E12 repeat retained debt after resynchronization")

    rates = [run["throughput"]["million_records_per_s"] for run in runs]
    bandwidth = [run["throughput"]["mib_per_s"] for run in runs]
    rejected = all(run["placeability"]["raw_page_reservation_code"] != 0 and
                   run["placeability"]["matching_class_reservation_code"] == 0
                   for run in runs)
    summary = {
        "experiment": "E12_feedback_placeability_repeat_summary",
        "runs": len(runs),
        "records_per_run": records.pop(),
        "record_bytes": record_bytes.pop(),
        "million_records_per_s": {
            "median": statistics.median(rates),
            "min": min(rates), "max": max(rates)},
        "mib_per_s": {
            "median": statistics.median(bandwidth),
            "min": min(bandwidth), "max": max(bandwidth)},
        "dropped": sum(run["throughput"]["dropped"] for run in runs),
        "forced_loss_records": forced_loss.pop(),
        "forced_loss_debt_bytes": forced_debt.pop(),
        "placeability_counterexample_rejected": rejected,
    }
    if summary["dropped"] or not rejected:
        raise SystemExit("E12 retained run failed loss-free/placeability checks")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
