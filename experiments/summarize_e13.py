#!/usr/bin/env python3
import json
import statistics
import sys

runs = [json.load(open(path)) for path in sys.argv[1:]]
names = [c["name"] for c in runs[0]["cells"]]
cells = []
for name in names:
    group = [next(c for c in run["cells"] if c["name"] == name)
             for run in runs]
    cells.append({
        "name": name,
        "poll_ns": group[0]["poll_ns"],
        "batch": group[0]["batch"],
        "latency_ns_p50_median": statistics.median(
            c["latency_ns_p50"] for c in group),
        "latency_ns_p99_median": statistics.median(
            c["latency_ns_p99"] for c in group),
        "max_depth_worst": max(c["max_depth"] for c in group),
        "headroom_bytes_worst": max(c["pessimistic_headroom_bytes"]
                                    for c in group),
        "drops": sum(c["dropped"] for c in group),
        "gaps": sum(c["gap_records"] for c in group),
    })
print(json.dumps({"experiment": "E13_async_feedback", "runs": len(runs),
                  "records_per_cell_per_run": runs[0]["records_per_cell"],
                  "producer_gap_ns": runs[0]["producer_gap_ns"],
                  "cells": cells,
                  "pass": all(r["pass"] for r in runs)}, indent=2))
