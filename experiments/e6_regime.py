#!/usr/bin/env python3
"""E6 — the empirical operating regime of a plug-in C_q quote.

E2 found the q knob inert: sweeping q moved a 25 K-page advertisement by under
1%. That is not an accident, and E6 pins down why and where it stops being
true. The reservation the gate purchases for a batch of n pages is

    r(n) = n*mu_hat + z_q * sigma_hat * sqrt(n),

so the SAFETY MARGIN AS A FRACTION of the batch's expected size is

    m(n) = z_q * (sigma/mu) / sqrt(n) .

At n = 25 K and sigma/mu ~ 0.2 that is 0.3% -- swamped by the noise in mu_hat
itself, which is exactly what E1 and E2 measured. At the batch sizes a tiering
daemon actually admits in (tens to thousands of pages) and on content with a
wide size distribution, m(n) is tens of percent and the quantile is a live
knob. E6 measures that crossover directly.

The event measured is the exceedance objective attached to each admission:

    VIOLATION  <=>  realized class-rounded demand of the batch  >  r(n),

i.e. the batch's pages no longer fit the reservation that was purchased for
them. Operationally this is the moment a batch turns into overdraft and becomes
a forced-spill candidate. Pages are freed after every batch so the budget
returns to its starting point, but the 60 outcomes within a seed share an
evolving telemetry window and are not independent. The eight seed clusters are
the independent replication units; per-admission Wilson intervals retained in
the JSON are descriptive only.

Design: {batch size n} x {content coefficient of variation} x {estimator arm}
x {q} x seeds. Reported per cell: empirical violation rate with a Wilson 95%
interval against the target 1-q, the measured cv, the predicted margin m(n),
and the price paid -- reservation bytes per page, which is what a higher q
costs in admitted capacity.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

POOL = 512 * 1024 * 1024
MAX_PAGES = 262144
EXTENT = 1 << 20
RING = 16384
WARM = 8192                      # telemetry samples before any measured batch
BATCHES = 60                     # measured admissions per (cell, seed)
N_SEEDS = 8

BATCH_SIZES = [8, 32, 128, 512, 2048, 8192]
QS = [0.5, 0.9, 0.99]
ARMS = ["analytic", "analytic_ring"]

# Part 2: the window sweep. The main sweep shows that window moments materially
# improve on the short-memory EWMA. Moving W tests how descriptive exceedance
# rates vary with n/W. The retained result supports only a ratio-group mean-rate
# envelope through n/W <= 1/8: matched cells still vary, and E14/E24 reject
# transfer to ordered traces. Batch sizes straddle every window.
WINDOWS = [256, 1024, 4096, 16384]
WINDOW_NS = [128, 512, 2048, 8192, 32768]
WINDOW_Q = 0.99

# Content mixes chosen to span the coefficient of variation of the per-page
# compressed-size distribution, which is the other half of the margin formula.
MIXES = {
    "cv_low":  {T.CONTENT_NAMES.index("TEXT"): 1.0},
    "cv_med":  {T.CONTENT_NAMES.index("TEXT"): 0.5,
                T.CONTENT_NAMES.index("RANDOM"): 0.5},
    "cv_high": {T.CONTENT_NAMES.index("ZERO"): 0.5,
                T.CONTENT_NAMES.index("RANDOM"): 0.5},
}

ARM_EST = {"analytic": ffi.EST_ANALYTIC, "analytic_ring": ffi.EST_ANALYTIC_RING}


def _norminv(p: float) -> float:
    """Standard normal quantile, via the same C routine the gate uses."""
    return float(ffi._lib.norminv(p))


def _pages(mix: dict[int, float], n: int, seed: int, key0: int) -> list:
    """n pages drawn i.i.d. from `mix`, deterministic in (seed, key)."""
    rng = np.random.default_rng(seed ^ 0x5EED)
    ids = np.array(sorted(mix)), np.array([mix[k] for k in sorted(mix)])
    classes = rng.choice(ids[0], size=n, p=ids[1] / ids[1].sum())
    return [T.page_bytes(int(c), key0 + i, seed) for i, c in enumerate(classes)]


def run_window_cell(window: int, nbatch: int, seed: int) -> dict:
    """One (telemetry window W, batch size n) cell of the window sweep, at a
    fixed q. Uses the medium-cv mix; ring_window is passed to the contract, so
    the estimator sees exactly W samples regardless of ring capacity."""
    failures: list[str] = []
    r = run_cell(nbatch, "cv_med", "analytic_ring", WINDOW_Q, seed, failures,
                 ring_window=window)
    r["window"] = window
    r["_failures"] = failures
    return r


def run_cell_mp(nbatch: int, mixname: str, arm: str, q: float, seed: int) -> dict:
    """Pool-friendly wrapper: failures travel back inside the result dict."""
    failures: list[str] = []
    r = run_cell(nbatch, mixname, arm, q, seed, failures)
    r["_failures"] = failures
    return r


def run_cell(nbatch: int, mixname: str, arm: str, q: float, seed: int,
             failures: list[str], ring_window: int = 0) -> dict:
    mix = MIXES[mixname]
    spill = common.spill_path()
    tier = ffi.Tier(POOL, MAX_PAGES, str(spill), zlib_level=1,
                    extent_bytes=EXTENT, ring_capacity=RING)
    try:
        c = ffi.Contract(tier, ARM_EST[arm], q, headroom_frac=0.05,
                         ntenants=1, victim_policy=ffi.VICTIM_CLOCK_ONLY,
                         ring_window=ring_window)
        try:
            # --- warm the telemetry so the estimator is out of cold start ---
            warm = _pages(mix, WARM, seed, 0)
            lpn = 0
            for pg in warm:
                c.admit(0, ffi.CONTRACT_OPP, 1)
                c.write(0, ffi.CONTRACT_OPP, lpn, pg)
                lpn += 1
            for i in range(WARM):                # release the warm-up pages
                c.free(0, i)

            viol = 0
            done = 0
            resv_per_page: list[float] = []
            demand_per_page: list[float] = []
            rejects = 0
            base = MAX_PAGES // 2
            for b in range(nbatch and BATCHES):
                pages = _pages(mix, nbatch, seed + 1000 * b, base)
                r0 = c.stat_u64(-1, ffi.CSTAT_RESV_BYTES)
                if c.admit(0, ffi.CONTRACT_GUAR, nbatch) != 0:
                    rejects += 1
                    continue
                r1 = c.stat_u64(-1, ffi.CSTAT_RESV_BYTES)
                reservation = float(r1 - r0)
                # Exact demand measurement. The telemetry ring holds RING
                # samples, so a batch larger than that would overwrite its own
                # early writes before we could read them; drain in chunks of
                # RING/2 instead of assuming the batch fits.
                CHUNK = RING // 2
                w0 = tier.stat_u64(ffi.CTIER_STAT_WRITES)
                demand = 0.0
                seen = 0
                ok = True
                for i, pg in enumerate(pages):
                    if c.write(0, ffi.CONTRACT_GUAR, base + i, pg) != 0:
                        ok = False
                        break
                    if (i + 1) % CHUNK == 0:
                        got = tier.ring_snapshot(CHUNK)
                        if got.size < CHUNK:
                            ok = False
                            failures.append(
                                f"E6 demand drain short (want {CHUNK}, "
                                f"got {got.size})")
                            break
                        demand += float(got[:CHUNK].astype(np.float64).sum())
                        seen += CHUNK
                if not ok:
                    for i in range(nbatch):
                        c.free(0, base + i)
                    continue
                nw = tier.stat_u64(ffi.CTIER_STAT_WRITES) - w0
                tail = nbatch - seen
                if tail:
                    got = tier.ring_snapshot(tail)
                    if got.size < tail:
                        failures.append(
                            f"E6 demand tail short (want {tail}, got {got.size})")
                        for i in range(nbatch):
                            c.free(0, base + i)
                        continue
                    demand += float(got[:tail].astype(np.float64).sum())
                if nw != nbatch:
                    failures.append(
                        f"E6 write count mismatch (n={nbatch} writes={nw})")
                    for i in range(nbatch):
                        c.free(0, base + i)
                    continue
                if demand > reservation:
                    viol += 1
                done += 1
                resv_per_page.append(reservation / nbatch)
                demand_per_page.append(demand / nbatch)
                for i in range(nbatch):
                    c.free(0, base + i)

            n_ring, mu_ring, sg_ring = tier.ring_moments(0)
            return {
                "n": nbatch, "mix": mixname, "arm": arm, "q": q, "seed": seed,
                "batches": done, "violations": viol, "rejects": rejects,
                "resv_bytes_per_page": float(np.mean(resv_per_page)) if resv_per_page else None,
                "demand_bytes_per_page": float(np.mean(demand_per_page)) if demand_per_page else None,
                "mu_ring": mu_ring, "sigma_ring": sg_ring,
                "cv_measured": (sg_ring / mu_ring) if mu_ring > 0 else None,
                "ring_window": ring_window,
                "hard_failures": c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES),
                "invariant": c.check_invariant(),
            }
        finally:
            c.close()
    finally:
        tier.close()
        Path(spill).unlink(missing_ok=True)


def main() -> int:
    ap = common.make_argparser("E6 operating regime of the quantile knob")
    ap.add_argument("--jobs", type=int, default=None,
                    help="parallel cells (default: performance cores - 2); "
                         "cells are independent and counter-valued, so this "
                         "changes wall time only")
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    sizes = BATCH_SIZES[::2] if args.fast else BATCH_SIZES
    out_path = Path(args.out) / "e6_regime.json"

    config = {
        "pool_bytes": POOL, "max_pages": MAX_PAGES, "extent_bytes": EXTENT,
        "ring_capacity": RING, "warm_samples": WARM,
        "batches_per_cell": BATCHES, "batch_sizes": sizes, "qs": QS,
        "arms": ARMS, "mixes": {k: {T.CONTENT_NAMES[i]: w for i, w in v.items()}
                                for k, v in MIXES.items()},
        "n_seeds": n_seeds, "zlib_level": 1, "headroom_frac": 0.05,
        "violation_def": "realized class-rounded demand of an admitted batch > "
                         "the reservation bytes that batch purchased "
                         "(RESV_BYTES delta across contract_admit) — the "
                         "exceedance event targeted at 1-q by the plug-in "
                         "quote under its declared i.i.d. environment",
        "demand_measurement": "exact: telemetry ring drained immediately before "
                              "and after each batch (ring_capacity > max batch, "
                              "one sample per successful write, sole writer)",
        "trial_independence": "pages are freed after every batch and source "
                              "pages are i.i.d. draws, but outcomes within a "
                              "seed share an evolving telemetry window and are "
                              "not independent; seed clusters are independent",
        "margin_formula": "m(n) = z_q * (sigma/mu) / sqrt(n) — the predicted "
                          "fractional safety margin the quantile buys",
        "window_sweep": {"windows": WINDOWS, "batch_sizes": WINDOW_NS,
                         "q": WINDOW_Q, "mix": "cv_med",
                         "question": "how the ratio-group mean exceedance rate "
                                     "varies with n/W under the tested i.i.d. "
                                     "generator; individual cells and ordered "
                                     "traces need not follow a universal rule"},
        "fast": bool(args.fast),
    }

    failures: list[str] = []
    trials: list[dict] = []
    t0 = time.perf_counter_ns()
    cellargs = [(nbatch, mixname, arm, q, common.seed_for(si))
                for nbatch in sizes for mixname in MIXES for arm in ARMS
                for q in QS for si in range(n_seeds)]

    def _prog(i, total, a, r):
        print(f"[e6] {i}/{total} n={a[0]} {a[1]} {a[2]} q={a[3]} "
              f"viol={r['violations']}/{r['batches']}", flush=True)

    jobs = 1 if args.fast else args.jobs
    trials = common.run_cells(run_cell_mp, cellargs, jobs=jobs, progress=_prog)
    for t in trials:
        failures.extend(t.pop("_failures", []))

    cells = []
    for nbatch in sizes:
        for mixname in MIXES:
            for arm in ARMS:
                for q in QS:
                    sel = [t for t in trials
                           if t["n"] == nbatch and t["mix"] == mixname
                           and t["arm"] == arm and t["q"] == q]
                    k = sum(t["violations"] for t in sel)
                    n = sum(t["batches"] for t in sel)
                    if n == 0:
                        continue
                    lo, hi = common.wilson_ci(k, n)
                    cv = float(np.mean([t["cv_measured"] for t in sel
                                        if t["cv_measured"] is not None]))
                    z = float(_norminv(q))
                    cells.append({
                        "n": nbatch, "mix": mixname, "arm": arm, "q": q,
                        "target": round(1.0 - q, 6),
                        "trials": n, "violations": k,
                        "empirical": k / n, "wilson_lo": lo, "wilson_hi": hi,
                        "cv_measured": cv,
                        "margin_predicted": z * cv / np.sqrt(nbatch),
                        "resv_bytes_per_page": float(np.mean(
                            [t["resv_bytes_per_page"] for t in sel
                             if t["resv_bytes_per_page"] is not None])),
                        "demand_bytes_per_page": float(np.mean(
                            [t["demand_bytes_per_page"] for t in sel
                             if t["demand_bytes_per_page"] is not None])),
                    })

    # ---- part 2: telemetry-window sweep ----
    wsizes = WINDOW_NS[::2] if args.fast else WINDOW_NS
    wwins = WINDOWS[::2] if args.fast else WINDOWS
    wargs = [(w, n, common.seed_for(si))
             for w in wwins for n in wsizes for si in range(n_seeds)]

    def _wprog(i, total, a, r):
        print(f"[e6w] {i}/{total} W={a[0]} n={a[1]} "
              f"viol={r['violations']}/{r['batches']}", flush=True)

    wruns = common.run_cells(run_window_cell, wargs,
                             jobs=1 if args.fast else args.jobs, progress=_wprog)
    for t in wruns:
        failures.extend(t.pop("_failures", []))
    wcells = []
    for w in wwins:
        for n in wsizes:
            sel = [t for t in wruns if t["window"] == w and t["n"] == n]
            k = sum(t["violations"] for t in sel)
            nn = sum(t["batches"] for t in sel)
            if nn == 0:
                continue
            lo, hi = common.wilson_ci(k, nn)
            wcells.append({"window": w, "n": n, "q": WINDOW_Q,
                           "target": round(1.0 - WINDOW_Q, 6),
                           "trials": nn, "violations": k, "empirical": k / nn,
                           "wilson_lo": lo, "wilson_hi": hi,
                           "n_over_window": n / w})

    data = {"trials": trials, "cells": cells,
            "window_runs": wruns, "window_cells": wcells,
            "wall_s": (time.perf_counter_ns() - t0) / 1e9}
    common.write_result(
        out_path, "e6_regime",
        "Operating regime of the quantile knob: when C_q beats a mean reserve",
        config, [common.seed_for(i) for i in range(n_seeds)], data,
        modeled_fields=[], failures=failures,
        wall_seconds=data["wall_s"])
    print(f"[e6] wrote {out_path} ({len(failures)} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
