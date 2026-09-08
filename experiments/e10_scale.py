#!/usr/bin/env python3
"""E10 — do selected emulator properties survive a 256x pool-size sweep?

Every earlier result came from pools of 48-256 MiB and working sets under
30 K pages, on a technology whose entire economic case is TB-scale multi-tenant
capacity pools. E10 runs the same mechanisms from 256 MiB up to 64 GiB -- a
256x span, ending two orders of magnitude above anything previously reported --
and asks whether four measured properties remain stable over that tested span:

  * the safety invariant still holds and no write hard-fails
  * the detection lag stays a property of the ESTIMATOR (a fixed number of
    operations) rather than growing with the pool
  * attribution still lands on the tenant whose data inflated
  * the admission decision stays constant-time, and metadata stays a fixed
    fraction of the pool

The codec is LZ4 here, not zlib: at 64 GiB each run performs about 26 M page
operations (roughly 18.9 M initial writes plus 7.6 M rewrites), and a
25-65 us/page software DEFLATE would put the sweep out of reach. LZ4 is also
the closer analogue of what a CXL controller actually runs (E7 measures the
distributional consequences of that choice).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

# (pool bytes, label). The largest is bounded by the measurement machine's
# 512 GiB of RAM: the pool is an anonymous mapping and is really touched.
SCALES = [(256 << 20, "256MiB"), (1 << 30, "1GiB"), (4 << 30, "4GiB"),
          (16 << 30, "16GiB"), (64 << 30, "64GiB")]
FILL = 0.90                  # of budget, before the entropy flip
FLIP_FRAC = 1.0              # the bomber flips its WHOLE working set
# Uneven tenant shares: with four equal tenants the bomber is only a quarter of
# the pool, and flipping it from lz4-TEXT (~2.8 KiB) to RANDOM (4 KiB) moves
# utilisation by 11 points -- not enough to reach the forced-spill watermark, so
# the attribution claim would never be exercised at all.
SHARES = [0.2, 0.2, 0.2, 0.4]
# Extent size must scale with the pool: the device dedicates whole extents to
# size classes, so a fixed 4 MiB extent leaves a 256 MiB pool with only 64 of
# them and it starves on extents (with most of its bytes free) while a 64 GiB
# pool would have 16 K tiny ones. ~1024 extents at every scale.
def extent_for(pool: int) -> int:
    e = pool // 1024
    e = max(256 << 10, min(4 << 20, e))
    return e - (e % 4096)
N_TENANTS = 4
BOMBER = 3
N_SEEDS = 3

TEXT = T.CONTENT_NAMES.index("TEXT")
JSON = T.CONTENT_NAMES.index("JSON")
RANDOM = T.CONTENT_NAMES.index("RANDOM")
CLASSES = [TEXT, JSON, TEXT, TEXT]


def probe_page_bytes(seed: int, n: int = 4000) -> float:
    """Mean class-rounded cost of one page of the tenant content mix, measured
    on a scratch tier with the same codec."""
    spill = common.spill_path()
    t = ffi.Tier(64 << 20, n + 64, str(spill), codec=ffi.CODEC_LZ4,
                 zlib_level=1, extent_bytes=(256 << 10), ring_capacity=8192)
    try:
        for i in range(n):
            t.write(i, T.page_bytes(CLASSES[i % N_TENANTS], i, seed))
        return t.ring_moments(0)[1]
    finally:
        t.close()
        Path(spill).unlink(missing_ok=True)


def run_cell(pool: int, label: str, seed: int) -> dict:
    b_total = int(pool * 0.95)
    # Measure the per-page cost rather than assuming it: a hard-coded estimate
    # is wrong per codec and per content mix, and getting it wrong here means
    # the load itself overshoots into the forced-spill watermark, so the
    # attribution measurement is contaminated before the bomb even fires.
    est_page = probe_page_bytes(seed)
    ws = int(b_total * FILL / est_page)
    ws -= ws % N_TENANTS
    pert = [int(ws * f) for f in SHARES]
    per = max(pert)
    max_pages = ws + (ws // 4) + 1024
    spill = common.spill_path()
    t_create0 = time.perf_counter_ns()
    tier = ffi.Tier(pool, max_pages, str(spill), codec=ffi.CODEC_LZ4,
                    zlib_level=1, extent_bytes=extent_for(pool),
                    ring_capacity=4096)
    try:
        c = ffi.Contract(tier, ffi.EST_ANALYTIC_RING, 0.99, ntenants=N_TENANTS,
                         victim_policy=ffi.VICTIM_CLOCK_ONLY)
        create_ns = time.perf_counter_ns() - t_create0
        try:
            base = [0] * N_TENANTS
            acc = 0
            for t in range(N_TENANTS):
                base[t] = acc
                acc += pert[t] + 16
            inv_fail = 0

            # ---- load ----
            t0 = time.perf_counter_ns()
            written = 0
            for t in range(N_TENANTS):
                i = 0
                while i < pert[t]:
                    n = min(4096, pert[t] - i)
                    if c.admit(t, ffi.CONTRACT_GUAR, n) != 0:
                        n = 1
                        if c.admit(t, ffi.CONTRACT_GUAR, 1) != 0:
                            break
                    for j in range(n):
                        lpn = base[t] + i + j
                        if c.write(t, ffi.CONTRACT_GUAR, lpn,
                                   T.page_bytes(CLASSES[t], lpn, seed)) != 0:
                            break
                        written += 1
                    i += n
            load_ns = time.perf_counter_ns() - t0
            phys_load = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
            if c.check_invariant() != 0:
                inv_fail += 1

            # ---- admission cost at this scale (the hot path) ----
            NA = 20000
            ta = time.perf_counter_ns()
            for _ in range(NA):
                c.capacity_pages(BOMBER, ffi.CONTRACT_GUAR)
            cq_ns = (time.perf_counter_ns() - ta) / NA

            # ---- the entropy bomb, at scale ----
            tier.cusum_reset()
            v_before = {t: c.stat_u64(t, ffi.CSTAT_VIOLATIONS)
                        for t in range(N_TENANTS)}
            nflip = int(pert[BOMBER] * FLIP_FRAC)
            lie_op = cusum_op = None
            t0 = time.perf_counter_ns()
            for i in range(nflip):
                lpn = base[BOMBER] + i
                c.write(BOMBER, ffi.CONTRACT_GUAR, lpn,
                        T.page_bytes(RANDOM, lpn, seed))
                if lie_op is None and c.stat_u64(-1, ffi.CSTAT_LIE_FLAG):
                    lie_op = i
                if cusum_op is None and tier.cusum()[1]:
                    cusum_op = i
                if i % 100000 == 0 and c.check_invariant() != 0:
                    inv_fail += 1
            flip_ns = time.perf_counter_ns() - t0
            v_after = {t: c.stat_u64(t, ffi.CSTAT_VIOLATIONS)
                       for t in range(N_TENANTS)}
            if c.check_invariant() != 0:
                inv_fail += 1

            meta = tier.stat_u64(ffi.CTIER_STAT_META_BYTES)
            return {
                "pool_bytes": pool, "label": label, "seed": seed,
                "working_set_pages": ws, "pages_written": written,
                "probe_page_bytes": est_page,
                "max_pages": max_pages,
                "phys_after_load": int(phys_load),
                "u_after_load": phys_load / pool,
                "u_after_flip": tier.stat_u64(ffi.CTIER_STAT_PHYS_USED) / pool,
                "load_ns_per_page": load_ns / max(1, written),
                "flip_ns_per_page": flip_ns / max(1, nflip),
                "create_ns": create_ns,
                # Measured from Python, so it INCLUDES a ctypes round trip
                # (~0.5-1 us) and is an upper bound, not the C-side cost. E4
                # measures the hot path in C. What matters here is that it does
                # not grow with the pool.
                "capacity_call_ns_ffi_inclusive": cq_ns,
                "lie_flag_op": lie_op, "cusum_op": cusum_op,
                "flipped_pages": nflip,
                "violations_bomber": int(v_after[BOMBER] - v_before[BOMBER]),
                "violations_victims": int(sum(v_after[t] - v_before[t]
                                              for t in range(N_TENANTS)
                                              if t != BOMBER)),
                "hard_failures": int(c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES)),
                # Forced spills taken from a tenant that was NOT in overdraft:
                # pressure that no lie explains (device fragmentation, ordinary
                # capacity pressure) has no attributable owner, and the ladder
                # falls back to an arbitrary victim. Counted, not hidden.
                "attribution_breaches": int(
                    c.stat_u64(-1, ffi.CSTAT_INVARIANT_BREACHES)),
                "invariant_failures": inv_fail,
                "meta_bytes": int(meta),
                "meta_frac_of_pool": meta / pool,
                "spilled_pages": int(tier.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED)),
            }
        finally:
            c.close()
    finally:
        tier.close()
        Path(spill).unlink(missing_ok=True)


def main() -> int:
    ap = common.make_argparser("E10 scale sweep")
    ap.add_argument("--max-pool-gib", type=float, default=64.0,
                    help="cap the sweep (the 64 GiB cell really touches "
                         "64 GiB of anonymous memory)")
    args = ap.parse_args()
    scales = [s for s in SCALES if s[0] <= args.max_pool_gib * (1 << 30)]
    if args.fast:
        scales = scales[:2]
    n_seeds = 1 if args.fast else N_SEEDS
    out_path = Path(args.out) / "e10_scale.json"

    failures: list[str] = []
    runs = []
    t0 = time.perf_counter_ns()
    # Deliberately sequential: the large cells allocate tens of GiB and the
    # per-page timings would be meaningless under contention.
    for pool, label in scales:
        for si in range(n_seeds):
            r = run_cell(pool, label, common.seed_for(si))
            runs.append(r)
            print(f"[e10] {label:7s} seed{si} ws={r['working_set_pages']:>9d} "
                  f"u={r['u_after_load']:.2f}->{r['u_after_flip']:.2f} "
                  f"lie@{r['lie_flag_op']} cusum@{r['cusum_op']} "
                  f"v_bomber={r['violations_bomber']} "
                  f"v_victims={r['violations_victims']} "
                  f"cq={r['capacity_call_ns_ffi_inclusive']:.0f}ns "
                  f"meta={r['meta_frac_of_pool']*100:.2f}% "
                  f"breach={r['attribution_breaches']} "
                  f"inv_fail={r['invariant_failures']} "
                  f"hard={r['hard_failures']}", flush=True)
            if r["invariant_failures"]:
                failures.append(f"E10 invariant failed at {label}/seed{si}")
            if r["violations_victims"]:
                failures.append(f"E10 victims charged at {label}/seed{si}")

    summary = []
    for pool, label in scales:
        sel = [r for r in runs if r["label"] == label]
        if not sel:
            continue
        summary.append({
            "label": label, "pool_bytes": pool, "runs": len(sel),
            "working_set_pages": sel[0]["working_set_pages"],
            "u_after_load_mean": float(np.mean([r["u_after_load"] for r in sel])),
            "u_after_flip_mean": float(np.mean([r["u_after_flip"] for r in sel])),
            "lie_flag_op_median": float(np.median(
                [r["lie_flag_op"] for r in sel if r["lie_flag_op"] is not None]))
                if any(r["lie_flag_op"] is not None for r in sel) else None,
            "cusum_op_median": float(np.median(
                [r["cusum_op"] for r in sel if r["cusum_op"] is not None]))
                if any(r["cusum_op"] is not None for r in sel) else None,
            "capacity_call_ns_ffi_inclusive_mean": float(np.mean(
                [r["capacity_call_ns_ffi_inclusive"] for r in sel])),
            "load_ns_per_page_mean": float(np.mean(
                [r["load_ns_per_page"] for r in sel])),
            "violations_bomber_mean": float(np.mean(
                [r["violations_bomber"] for r in sel])),
            "violations_victims_total": int(sum(
                r["violations_victims"] for r in sel)),
            "meta_frac_of_pool": float(np.mean(
                [r["meta_frac_of_pool"] for r in sel])),
            "hard_failures_total": int(sum(r["hard_failures"] for r in sel)),
            "attribution_breaches_total": int(sum(
                r["attribution_breaches"] for r in sel)),
            "invariant_failures_total": int(sum(
                r["invariant_failures"] for r in sel)),
        })

    config = {
        "scales": [s[1] for s in scales], "fill": FILL,
        "flip_frac": FLIP_FRAC,
        "extent_bytes": {l: extent_for(p) for p, l in scales},
        "n_tenants": N_TENANTS, "bomber": BOMBER, "shares": SHARES,
        "n_seeds": n_seeds,
        "codec": "lz4", "level": 1, "estimator": "analytic_ring", "q": 0.99,
        "codec_rationale": "at 64 GiB each run performs about 26 M page "
                           "operations (about 18.9 M initial writes plus 7.6 M rewrites); software "
                           "DEFLATE at 25-65 us/page would put that out of "
                           "reach, and lz4 is the closer analogue of a CXL "
                           "controller's engine anyway",
        "sequential": "cells run one at a time: the large ones allocate tens of "
                      "GiB and the per-page timings would be meaningless under "
                      "contention",
        "fast": bool(args.fast),
    }
    data = {"runs": runs, "summary": summary,
            "wall_s": (time.perf_counter_ns() - t0) / 1e9}
    common.write_result(
        out_path, "e10_scale",
        "Scale sweep: 256 MiB to 64 GiB pools",
        config, [common.seed_for(i) for i in range(n_seeds)], data,
        modeled_fields=[], failures=failures, wall_seconds=data["wall_s"])
    print(f"[e10] wrote {out_path} ({len(failures)} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
