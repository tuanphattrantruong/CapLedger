#!/usr/bin/env python3
"""E11 — parameter sensitivity: which constants are load-bearing?

The design carries a dozen constants -- watermarks 0.85/0.92/0.97, a 5%
headroom, 128 B size classes, a 4096-entry telemetry window, the 1.4 lie ratio
-- and the paper so far reports one value for each. E11 sweeps three selected
design axes on the entropy-flip trace and reports what moves.

Swept here:
  headroom      the fraction of the pool held back from the budget
  extent_bytes  the device's extent-allocation granularity; the 32 size
                classes and their 128 B spacing remain fixed, while extent
                size changes fragmentation and the available extent supply
  codec         zlib / lz4 / zstd, since every distributional claim in the
                paper is conditioned on one compressor

Reported per cell: forced spills, attribution (bomber vs victims), peak
utilisation, hard failures, invariant failures. A parameter that changes none
of these is not load-bearing and should be reported as such rather than tuned.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

POOL = 96 * 1024 * 1024
MAX_PAGES = 65536
N_TENANTS = 4
BOMBER = 3
SHARES = [0.2, 0.2, 0.2, 0.4]
FILL = 0.90
N_SEEDS = 10

HEADROOMS = [0.0, 0.02, 0.05, 0.10, 0.20]
EXTENTS = [128 << 10, 256 << 10, 1 << 20, 4 << 20]
CODECS = [("zlib", ffi.CODEC_ZLIB), ("lz4", ffi.CODEC_LZ4),
          ("zstd", ffi.CODEC_ZSTD)]
TEXT = T.CONTENT_NAMES.index("TEXT")
JSON = T.CONTENT_NAMES.index("JSON")
RANDOM = T.CONTENT_NAMES.index("RANDOM")
CLASSES = [TEXT, JSON, TEXT, TEXT]


def probe(codec: int, seed: int, n: int = 3000) -> float:
    spill = common.spill_path()
    t = ffi.Tier(64 << 20, n + 64, str(spill), codec=codec, zlib_level=1,
                 extent_bytes=(256 << 10), ring_capacity=8192)
    try:
        for i in range(n):
            t.write(i, T.page_bytes(CLASSES[i % N_TENANTS], i, seed))
        return t.ring_moments(0)[1]
    finally:
        t.close()
        Path(spill).unlink(missing_ok=True)


def run_cell(param: str, value, seed: int) -> dict:
    headroom = 0.05
    extent = 256 << 10
    codec_name, codec = "zlib", ffi.CODEC_ZLIB
    if param == "headroom":
        headroom = value
    elif param == "extent":
        extent = value
    elif param == "codec":
        codec_name, codec = value, dict(CODECS)[value]

    est_page = probe(codec, seed)
    b_total = int(POOL * (1.0 - headroom))
    ws = int(b_total * FILL / est_page)
    pert = [int(ws * f) for f in SHARES]
    spill = common.spill_path()
    tier = ffi.Tier(POOL, MAX_PAGES, str(spill), codec=codec, zlib_level=1,
                    extent_bytes=extent, ring_capacity=4096)
    try:
        c = ffi.Contract(tier, ffi.EST_ANALYTIC_RING, 0.99,
                         headroom_frac=headroom, ntenants=N_TENANTS,
                         victim_policy=ffi.VICTIM_CLOCK_ONLY)
        try:
            base, acc = [0] * N_TENANTS, 0
            for t in range(N_TENANTS):
                base[t] = acc
                acc += pert[t] + 16
            for t in range(N_TENANTS):
                i = 0
                while i < pert[t]:
                    n = min(2048, pert[t] - i)
                    if c.admit(t, ffi.CONTRACT_GUAR, n) != 0:
                        if c.admit(t, ffi.CONTRACT_GUAR, 1) != 0:
                            break
                        n = 1
                    for j in range(n):
                        lpn = base[t] + i + j
                        if c.write(t, ffi.CONTRACT_GUAR, lpn,
                                   T.page_bytes(CLASSES[t], lpn, seed)) != 0:
                            break
                    i += n
            u_load = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED) / POOL
            v0 = {t: c.stat_u64(t, ffi.CSTAT_VIOLATIONS) for t in range(N_TENANTS)}
            tier.cusum_reset()
            lie_op = cusum_op = None
            peak = 0
            for i in range(pert[BOMBER]):
                lpn = base[BOMBER] + i
                c.write(BOMBER, ffi.CONTRACT_GUAR, lpn,
                        T.page_bytes(RANDOM, lpn, seed))
                if lie_op is None and c.stat_u64(-1, ffi.CSTAT_LIE_FLAG):
                    lie_op = i
                if cusum_op is None and tier.cusum()[1]:
                    cusum_op = i
                if i % 512 == 0:
                    peak = max(peak, tier.stat_u64(ffi.CTIER_STAT_PHYS_USED))
            v1 = {t: c.stat_u64(t, ffi.CSTAT_VIOLATIONS) for t in range(N_TENANTS)}
            return {
                "param": param, "value": value, "seed": seed,
                "codec": codec_name, "headroom": headroom, "extent": extent,
                "probe_page_bytes": est_page, "working_set_pages": ws,
                "u_after_load": u_load,
                "u_peak": peak / POOL,
                "u_after_flip": tier.stat_u64(ffi.CTIER_STAT_PHYS_USED) / POOL,
                "lie_flag_op": lie_op, "cusum_op": cusum_op,
                "violations_bomber": int(v1[BOMBER] - v0[BOMBER]),
                "violations_victims": int(sum(v1[t] - v0[t]
                                              for t in range(N_TENANTS)
                                              if t != BOMBER)),
                "attribution_breaches": int(
                    c.stat_u64(-1, ffi.CSTAT_INVARIANT_BREACHES)),
                "hard_failures": int(c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES)),
                "invariant": int(c.check_invariant()),
                "guar_pages_written": int(sum(
                    c.stat_u64(t, ffi.CSTAT_WRITTEN_GUAR)
                    for t in range(N_TENANTS))),
            }
        finally:
            c.close()
    finally:
        tier.close()
        Path(spill).unlink(missing_ok=True)


def main() -> int:
    ap = common.make_argparser("E11 parameter sensitivity")
    ap.add_argument("--jobs", type=int, default=None)
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    out_path = Path(args.out) / "e11_sensitivity.json"
    hs = HEADROOMS[::2] if args.fast else HEADROOMS
    es = EXTENTS[::2] if args.fast else EXTENTS
    cs = [c[0] for c in (CODECS[::2] if args.fast else CODECS)]
    cellargs = ([("headroom", h, common.seed_for(i)) for h in hs
                 for i in range(n_seeds)] +
                [("extent", e, common.seed_for(i)) for e in es
                 for i in range(n_seeds)] +
                [("codec", c, common.seed_for(i)) for c in cs
                 for i in range(n_seeds)])

    def _prog(i, total, a, r):
        print(f"[e11] {i}/{total} {a[0]}={a[1]} u={r['u_after_load']:.2f}->"
              f"{r['u_after_flip']:.2f} v_b={r['violations_bomber']} "
              f"v_v={r['violations_victims']} hard={r['hard_failures']}",
              flush=True)

    t0 = time.perf_counter_ns()
    runs = common.run_cells(run_cell, cellargs, jobs=args.jobs, progress=_prog)
    failures = [f"E11 invariant broke: {r['param']}={r['value']}"
                for r in runs if r["invariant"] != 0]

    summary = []
    for param, values in (("headroom", hs), ("extent", es), ("codec", cs)):
        for v in values:
            sel = [r for r in runs if r["param"] == param and r["value"] == v]
            if not sel:
                continue
            def m(k):
                return float(np.mean([r[k] for r in sel]))
            summary.append({
                "param": param, "value": v, "runs": len(sel),
                "u_after_load": m("u_after_load"), "u_peak": m("u_peak"),
                "u_after_flip": m("u_after_flip"),
                "guar_pages_written": m("guar_pages_written"),
                "violations_bomber": m("violations_bomber"),
                "violations_victims": m("violations_victims"),
                "attribution_breaches": m("attribution_breaches"),
                "hard_failures": int(sum(r["hard_failures"] for r in sel)),
                "lie_detected": sum(1 for r in sel
                                    if r["lie_flag_op"] is not None),
                "cusum_detected": sum(1 for r in sel
                                      if r["cusum_op"] is not None),
                "cusum_op_median": float(np.median(
                    [r["cusum_op"] for r in sel if r["cusum_op"] is not None]))
                    if any(r["cusum_op"] is not None for r in sel) else None,
                "of": len(sel),
            })

    config = {
        "pool_bytes": POOL, "max_pages": MAX_PAGES, "n_tenants": N_TENANTS,
        "shares": SHARES, "bomber": BOMBER, "fill": FILL, "n_seeds": n_seeds,
        "headrooms": hs, "extents": es, "codecs": cs,
        "estimator": "analytic_ring", "q": 0.99,
        "baseline": {"headroom": 0.05, "extent": 256 << 10, "codec": "zlib"},
        "trace": "load to `fill` of budget with a 4-tenant content mix, then "
                 "the 40%-share tenant rewrites its whole working set as RANDOM",
        "fast": bool(args.fast),
    }
    data = {"runs": runs, "summary": summary,
            "wall_s": (time.perf_counter_ns() - t0) / 1e9}
    common.write_result(
        out_path, "e11_sensitivity",
        "Parameter sensitivity: headroom, extent granularity, codec",
        config, [common.seed_for(i) for i in range(n_seeds)], data,
        modeled_fields=[], failures=failures, wall_seconds=data["wall_s"])
    print(f"[e11] wrote {out_path} ({len(failures)} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
