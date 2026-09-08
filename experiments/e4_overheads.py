#!/usr/bin/env python3
"""E4 — overheads (wraps build/overhead) + Python-side bootstrap timing.

Runs the C overhead benchmark binary (admission latency percentiles, write
path hist-on/off, compaction & spill throughput, thread scaling) with
default arguments and wraps its stdout JSON with run metadata.

Additionally times the Python cq_bootstrap estimator on a realistic
4096-entry ring (built by zlib-level-1-compressing steady-mixed pages in
Python — same system zlib the device links — and class-rounding exactly like
the device telemetry: 0 for zero pages, 4096 for incompressible, else
128*ceil(size/128)) at K=200 for three budgets. Bootstrap numbers are labeled
python-side/amortized: the estimator is a periodic computation, not a hot
path (see DESIGN.md's scope and optional-estimator sections).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import zlib as _zlib
from pathlib import Path

import numpy as np

import common
import traces as T
from bootstrap import cq_bootstrap

RING_N = 4096
BOOT_K = 200
BUDGETS = [16 * 1024 * 1024, 64 * 1024 * 1024, 256 * 1024 * 1024]
REPS = 5


def class_rounded(page: np.ndarray) -> int:
    b = page.tobytes()
    if not any(b):
        return 0
    c = len(_zlib.compress(b, 1))
    return 4096 if c >= 4096 else 128 * ((c + 127) // 128)


def bootstrap_timing(fast: bool) -> dict:
    reps = max(2, REPS // 2) if fast else REPS
    contents = T.steady_mixed_content(RING_N, common.BASE_SEED)
    ring = np.array([class_rounded(T.page_bytes(int(c), k, common.BASE_SEED, 0))
                     for k, c in enumerate(contents)], dtype=np.uint16)
    out = {
        "label": "python-side, amortized (periodic estimator, not a hot path)",
        "ring_entries": int(ring.size),
        "ring_mean_bytes": float(ring.mean()),
        "K": BOOT_K,
        "reps": reps,
        "per_budget": [],
    }
    for budget in BUDGETS:
        times_ms = []
        result = None
        for rep in range(reps):
            rng = np.random.default_rng(common.BASE_SEED + rep)
            t0 = time.perf_counter_ns()
            result = cq_bootstrap(ring, budget, 0.99, K=BOOT_K, rng=rng)
            times_ms.append((time.perf_counter_ns() - t0) / 1e6)
        out["per_budget"].append({
            "budget_bytes": budget,
            "q": 0.99,
            "cq_pages": int(result),
            "ms_mean": float(np.mean(times_ms)),
            "ms_min": float(np.min(times_ms)),
            "ms_per_rep": times_ms,
        })
    return out


# The scaling sweep stops at SCALE_MAX_THREADS. Beyond it, the C benchmark
# becomes unreliable on this machine -- one run reported zero throughput for
# the 24-thread cell and a rerun did not terminate. A separate driver
# (24 threads, 3 s, no stuck threads) confirms the DEVICE does not deadlock,
# so the fault is in the benchmark harness and we have not isolated it. We
# therefore report the sweep only as far as it is trustworthy rather than
# publishing a number we cannot reproduce or a device limit we have not shown.
SCALE_MAX_THREADS = 16


def _threads_list() -> str:
    """1,2,4,... up to min(performance cores, SCALE_MAX_THREADS)."""
    try:
        n = int(subprocess.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                               capture_output=True, text=True,
                               timeout=10).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        n = 8
    n = min(max(1, n), SCALE_MAX_THREADS)
    out, t = [], 1
    while t <= n and len(out) < 16:
        out.append(t)
        t *= 2
    if out[-1] != n and n > 1:
        out.append(n)
    return ",".join(str(x) for x in out)


THREADS_LIST = _threads_list()


def main() -> int:
    ap = common.make_argparser("E4 overheads (wraps build/overhead)")
    args = ap.parse_args()
    out_path = Path(args.out) / "e4_overheads.json"
    overhead = common.ROOT / "build" / "overhead"
    config = {
        "binary": str(overhead), "args": [],
        "bootstrap_timing": {"ring_entries": RING_N, "K": BOOT_K,
                             "budgets": BUDGETS, "q": 0.99},
        "fast": bool(args.fast),
    }
    failures: list[str] = []
    data: dict = {}
    t0 = time.perf_counter_ns()

    if not overhead.exists():
        failures.append(f"overhead binary missing: {overhead} (build with `make bench`)")
    else:
        try:
            # Scale the thread sweep to the machine: the benchmark's built-in
            # 1/2/4/8 list was chosen for an 8-core laptop and says nothing
            # about a 24-performance-core part.
            r = subprocess.run([str(overhead), "--threads-list", THREADS_LIST],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                failures.append(
                    f"build/overhead exited {r.returncode}; stderr: "
                    f"{r.stderr.strip()[:2000]}")
            else:
                try:
                    parsed = json.loads(r.stdout)
                    if isinstance(parsed, dict):
                        data.update(parsed)
                    else:
                        data["overhead"] = parsed
                except json.JSONDecodeError as e:
                    failures.append(
                        f"build/overhead stdout was not valid JSON ({e}); "
                        f"first 500 chars: {r.stdout[:500]!r}")
        except subprocess.TimeoutExpired:
            failures.append("build/overhead timed out after 600 s")
        except OSError as e:
            failures.append(f"failed to run build/overhead: {e}")

    try:
        data["bootstrap_python"] = bootstrap_timing(args.fast)
    except Exception as e:  # corpus missing etc.
        failures.append(f"python bootstrap timing failed: {e}")

    wall = (time.perf_counter_ns() - t0) / 1e9
    common.write_result(out_path, "E4",
                        "contract/tier overheads and scalability",
                        config, seeds=[common.BASE_SEED], data=data,
                        modeled_fields=[], failures=failures, wall_seconds=wall)
    print(f"wrote {out_path}" + (f" with {len(failures)} failure(s)" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
