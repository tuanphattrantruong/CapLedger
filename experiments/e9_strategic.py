#!/usr/bin/env python3
"""E9 — the strategic tenant, and whether repricing closes the window.

The paper names this attack in its discussion and never measures it. A tenant
that wants more of the pool than it should get does not have to inflate by
accident: it can SHAPE ITS HISTORY. Write highly compressible pages while the
telemetry window fills, so mu-hat collapses; buy a large guaranteed reservation
at the resulting cheap price; then write what it actually wanted to store. The
reservation was priced against evidence the tenant manufactured.

The exposure is not the violations the shaper eventually takes -- the ladder
charges those to it correctly. The exposure is the BUDGET IT HOLDS in the
meantime: reservation bytes booked against a price that no longer reflects its
data, which honest tenants are refused admissions for. E9 measures that
directly as the honest tenants' admission-rejection rate and granted pages.

The shaper must shape with data that is cheap but NOT free. Writing all-zero
pages drives mu-hat to ~0, which makes the reservation cost ~0 -- and a
reservation that costs nothing also HOLDS nothing, so it denies honest tenants
nothing. The effective attack shapes with ordinary compressible text
(ratio ~0.45), buys reservation priced at ~1.8 KB/page, and then stores
incompressible data at 4 KB/page: it holds real budget at a 2.2x discount to
what it consumes.

Arms:
  honest      every tenant writes what it says it will (the control)
  shaper      one tenant shapes its history, then inflates
  shaper+rx   the same attack against the repricing countermeasure, which
              marks open reservation groups to market on the lie-flag edge and
              revokes the unconsumed remainder the budget no longer backs

A working countermeasure should restore the honest tenants' admission success
toward the control while charging the shaper for what it actually used.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

# Sized so aggregate demand (~110 MiB) substantially exceeds the budget: the
# admission gate has to bind, or there is no scarcity for the shaper to exploit
# and the experiment is vacuous.
POOL = 56 * 1024 * 1024
MAX_PAGES = 65536
EXTENT = 256 << 10
N_TENANTS = 4                # tenant 3 is the (potential) shaper
SHAPER = 3
SHAPE_PAGES = 6000           # compressible pages written to bend mu-hat
BUY_BATCH = 256
BUY_ROUNDS = 40
HONEST_BATCH = 128
HONEST_ROUNDS = 120
INFLATE_PAGES = 6000
N_SEEDS = 10

ZERO = T.CONTENT_NAMES.index("ZERO")
TEXT = T.CONTENT_NAMES.index("TEXT")
JSON = T.CONTENT_NAMES.index("JSON")
RANDOM = T.CONTENT_NAMES.index("RANDOM")
HONEST_CLASS = [TEXT, JSON, TEXT]


def run_cell(arm: str, seed: int) -> dict:
    """One run of one arm. `arm` in {honest, shaper, shaper_reprice}."""
    shaping = arm != "honest"
    reprice = 1 if arm == "shaper_reprice" else 0
    spill = common.spill_path()
    tier = ffi.Tier(POOL, MAX_PAGES, str(spill), zlib_level=1,
                    extent_bytes=EXTENT, ring_capacity=4096)
    try:
        c = ffi.Contract(tier, ffi.EST_ANALYTIC_RING, 0.99, ntenants=N_TENANTS,
                         victim_policy=ffi.VICTIM_CLOCK_ONLY, reprice=reprice)
        try:
            base = [t * 12000 for t in range(N_TENANTS)]
            cursor = [0] * N_TENANTS

            def write(t, cls_content, npages) -> int:
                done = 0
                for _ in range(npages):
                    lpn = base[t] + cursor[t]
                    if cursor[t] >= 11500:
                        break
                    rc = c.write(t, ffi.CONTRACT_GUAR, lpn,
                                 T.page_bytes(cls_content, lpn, seed))
                    if rc != 0:
                        break
                    cursor[t] += 1
                    done += 1
                return done

            # ---- phase 0: every tenant earns 64 samples of history ----
            for t in range(N_TENANTS):
                for _ in range(80):
                    if c.admit(t, ffi.CONTRACT_GUAR, 1) != 0:
                        break
                    write(t, HONEST_CLASS[t] if t != SHAPER else TEXT, 1)

            # ---- phase 1: the shaper bends mu-hat with compressible pages ----
            # TEXT, not ZERO: see the module docstring -- a free reservation is
            # also an empty one, and steals nothing.
            shape_written = 0
            if shaping:
                for _ in range(SHAPE_PAGES // 64):
                    if c.admit(SHAPER, ffi.CONTRACT_GUAR, 64) != 0:
                        break
                    shape_written += write(SHAPER, TEXT, 64)
                # free the shaping pages: their purpose was the telemetry they
                # left behind, and freeing them returns their bytes so the
                # attack is about the PRICE, not about hoarding pages
                for i in range(shape_written):
                    c.free(SHAPER, base[SHAPER] + i)
                cursor[SHAPER] = 0
            mu_at_buy = c.stat_f64(SHAPER, ffi.CSTAT_MU_TENANT)

            # ---- phase 2: the shaper buys as much reservation as it can ----
            bought = 0
            r_before = c.stat_u64(-1, ffi.CSTAT_RESV_BYTES)
            for _ in range(BUY_ROUNDS):
                if c.admit(SHAPER, ffi.CONTRACT_GUAR, BUY_BATCH) != 0:
                    break
                bought += BUY_BATCH
            resv_after_buy = c.stat_u64(-1, ffi.CSTAT_RESV_BYTES)
            # Budget the shaper now holds, and what those pages will actually
            # cost once it stores incompressible data in them.
            budget_held = int(resv_after_buy) - int(r_before)
            budget_true = bought * 4096

            # ---- phase 3: honest tenants try to get their work admitted ----
            # Tenant 3 follows the SAME admission and write schedule in every
            # arm; only its content differs (TEXT in the control, RANDOM in the
            # attack arms). Otherwise the control would have tenant 3 hoarding a
            # reservation it never writes, which is its own denial of service
            # and would flatter the countermeasure.
            h_ok = h_rej = 0
            h_pages = 0
            t3_content = RANDOM if shaping else TEXT
            per_round = INFLATE_PAGES // (HONEST_ROUNDS // 4)
            for r in range(HONEST_ROUNDS):
                for t in range(N_TENANTS):
                    if t == SHAPER:
                        continue
                    if c.admit(t, ffi.CONTRACT_GUAR, HONEST_BATCH) == 0:
                        h_ok += 1
                        h_pages += write(t, HONEST_CLASS[t], HONEST_BATCH)
                    else:
                        h_rej += 1
                if r % 4 == 0:
                    write(SHAPER, t3_content, per_round)

            viol = {t: c.stat_u64(t, ffi.CSTAT_VIOLATIONS) for t in range(N_TENANTS)}
            return {
                "arm": arm, "seed": seed,
                "shape_pages": shape_written,
                "mu_at_buy": mu_at_buy,
                "shaper_bought_pages": bought,
                "resv_after_buy": int(resv_after_buy),
                "budget_held_bytes": budget_held,
                "budget_true_cost_bytes": budget_true,
                "discount": (budget_true / budget_held) if budget_held > 0 else None,
                "b_total": int(POOL * 0.95),
                "honest_admits_ok": h_ok, "honest_admits_rejected": h_rej,
                "honest_admit_rate": h_ok / max(1, h_ok + h_rej),
                "honest_pages_written": h_pages,
                # What honest tenants actually KEEP. Admission rate on its own
                # is a misleading headline: freeing reservation lets the gate
                # re-admit into space the ladder makes by spilling, so a high
                # admit rate can just mean a longer admit-and-spill treadmill.
                "honest_pages_resident": int(sum(
                    c.stat_u64(t, ffi.CSTAT_WRITTEN_GUAR) -
                    c.stat_u64(t, ffi.CSTAT_R3_FORCED_SPILLS)
                    for t in range(N_TENANTS) if t != SHAPER)),
                "shaper_pages_resident": int(
                    c.stat_u64(SHAPER, ffi.CSTAT_WRITTEN_GUAR) -
                    c.stat_u64(SHAPER, ffi.CSTAT_R3_FORCED_SPILLS)),
                "shaper_phys_bytes": int(max(0, c.stat_f64(
                    SHAPER, ffi.CSTAT_OVERDRAFT_BYTES))),
                "shaper_pages_written": cursor[SHAPER],
                "violations_shaper": int(viol[SHAPER]),
                "violations_honest": int(sum(v for t, v in viol.items()
                                             if t != SHAPER)),
                "reprice_events": int(c.stat_u64(-1, ffi.CSTAT_REPRICE_EVENTS)),
                "reprice_revoked": int(c.stat_u64(-1, ffi.CSTAT_REPRICE_REVOKED)),
                "hard_failures": int(c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES)),
                "invariant": int(c.check_invariant()),
                "overdraft_shaper": float(c.stat_f64(SHAPER,
                                                     ffi.CSTAT_OVERDRAFT_BYTES)),
                "reprice_debt_shaper": float(c.stat_f64(
                    SHAPER, ffi.CSTAT_REPRICE_DEBT_BYTES)),
            }
        finally:
            c.close()
    finally:
        tier.close()
        Path(spill).unlink(missing_ok=True)


def main() -> int:
    ap = common.make_argparser("E9 strategic tenant and repricing")
    ap.add_argument("--jobs", type=int, default=None)
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    out_path = Path(args.out) / "e9_strategic.json"
    arms = ["honest", "shaper", "shaper_reprice"]
    cellargs = [(a, common.seed_for(i)) for a in arms for i in range(n_seeds)]

    def _prog(i, total, a, r):
        print(f"[e9] {i}/{total} {a[0]:15s} bought={r['shaper_bought_pages']:6d} "
              f"h_resident={r['honest_pages_resident']:6d} "
              f"sh_resident={r['shaper_pages_resident']:6d} "
              f"v_h={r['violations_honest']:6d} v_sh={r['violations_shaper']:6d} "
              f"revoked={r['reprice_revoked']}", flush=True)

    t0 = time.perf_counter_ns()
    runs = common.run_cells(run_cell, cellargs,
                            jobs=1 if args.fast else args.jobs, progress=_prog)
    failures = [f"E9 invariant broke in {r['arm']}/{r['seed']}"
                for r in runs if r["invariant"] != 0]

    summary = []
    for a in arms:
        sel = [r for r in runs if r["arm"] == a]
        def m(k):
            return float(np.mean([r[k] for r in sel]))
        summary.append({
            "arm": a, "runs": len(sel),
            "mu_at_buy_mean": m("mu_at_buy"),
            "shaper_bought_pages_mean": m("shaper_bought_pages"),
            "budget_held_bytes_mean": m("budget_held_bytes"),
            "budget_held_frac_of_B": m("budget_held_bytes") / (POOL * 0.95),
            "discount_mean": float(np.mean([r["discount"] for r in sel
                                            if r["discount"] is not None]))
                             if any(r["discount"] for r in sel) else None,
            "honest_admit_rate_mean": m("honest_admit_rate"),
            "honest_admit_rate_min": float(np.min([r["honest_admit_rate"] for r in sel])),
            "honest_pages_written_mean": m("honest_pages_written"),
            "honest_pages_resident_mean": m("honest_pages_resident"),
            "shaper_pages_resident_mean": m("shaper_pages_resident"),
            "violations_shaper_mean": m("violations_shaper"),
            "violations_honest_mean": m("violations_honest"),
            "reprice_revoked_mean": m("reprice_revoked"),
            "overdraft_shaper_mean": m("overdraft_shaper"),
            "reprice_debt_shaper_mean": m("reprice_debt_shaper"),
            "hard_failures_total": int(sum(r["hard_failures"] for r in sel)),
        })

    config = {
        "pool_bytes": POOL, "max_pages": MAX_PAGES, "extent_bytes": EXTENT,
        "n_tenants": N_TENANTS, "shaper_tenant": SHAPER,
        "shape_pages": SHAPE_PAGES, "buy_batch": BUY_BATCH,
        "buy_rounds": BUY_ROUNDS, "honest_batch": HONEST_BATCH,
        "honest_rounds": HONEST_ROUNDS, "inflate_pages": INFLATE_PAGES,
        "n_seeds": n_seeds, "estimator": "analytic_ring", "q": 0.99,
        "attack": "write compressible TEXT to depress mu-hat, free it (the "
                  "telemetry is what the attack wanted), buy reservation at "
                  "the resulting price, then store RANDOM in those pages",
        "exposure_metric": "honest tenants' admission success rate and pages "
                           "written — what the shaper takes from them, not what "
                           "it is eventually charged",
        "countermeasure": "mark open reservation groups to market on the "
                          "lie-flag edge and revoke the unconsumed remainder "
                          "the budget no longer backs",
        "fast": bool(args.fast),
    }
    data = {"runs": runs, "summary": summary,
            "wall_s": (time.perf_counter_ns() - t0) / 1e9}
    common.write_result(
        out_path, "e9_strategic",
        "Strategic history shaping and the repricing countermeasure",
        config, [common.seed_for(i) for i in range(n_seeds)], data,
        modeled_fields=[], failures=failures, wall_seconds=data["wall_s"])
    print(f"[e9] wrote {out_path} ({len(failures)} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
