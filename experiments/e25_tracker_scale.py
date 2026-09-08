#!/usr/bin/env python3
"""E25: drive the sharded many-tenant host completion tracker."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import common


def main() -> int:
    parser = common.make_argparser("E25 many-tenant host tracker")
    args = parser.parse_args()
    records = 100_000 if args.fast else 1_000_000
    repeats = 1 if args.fast else 3
    rows = []
    failures = []
    begin = time.perf_counter()
    for tenants in (8, 256, 4096):
        for queues in (1, 4, 8):
            for repeat in range(repeats):
                cmd = ["build/tracker_scale", "--tenants", str(tenants),
                       "--queues", str(queues), "--records", str(records)]
                run = subprocess.run(cmd, capture_output=True, text=True)
                if run.returncode:
                    failures.append(f"{tenants} tenants/{queues} queues/r{repeat}: {run.stderr}")
                    continue
                row = json.loads(run.stdout)
                row["repeat"] = repeat
                rows.append(row)
    summary = []
    for tenants in (8, 256, 4096):
        for queues in (1, 4, 8):
            cell = [r for r in rows if r["tenants"] == tenants and r["queues"] == queues]
            if not cell:
                continue
            rates = sorted(r["million_records_per_s"] for r in cell)
            cpus = sorted(r["cpu_ns_per_record"] for r in cell)
            summary.append({"tenants": tenants, "queues": queues,
                            "runs": len(cell), "median_million_records_per_s": rates[len(rates)//2],
                            "median_cpu_ns_per_record": cpus[len(cpus)//2],
                            "dropped": sum(r["dropped"] for r in cell),
                            "gaps": sum(r["gaps"] for r in cell)})
    common.write_result(args.out / "e25_tracker_scale.json", "E25",
                        "Sharded host tracker with many tenants and queues",
                        {"tenants": [8, 256, 4096], "queues": [1, 4, 8],
                         "records_per_run": records, "repeats": repeats,
                         "window_entries_per_tenant": 4096,
                         "fast": args.fast}, [],
                        {"summary": summary, "runs": rows,
                         "scope": ("C11 host data-plane prototype with direct-array "
                                   "token ownership, per-queue sequencing, and per-tenant "
                                   "locks; excludes policy/reclaim and is not a kernel result")},
                        modeled_fields=[], failures=failures,
                        wall_seconds=time.perf_counter() - begin)
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
