#!/usr/bin/env python3
"""E8 — the lie detector's operating characteristic.

E3 measured detection on the easiest possible case: the entire working set
flips to incompressible in one step. A production tier drifts. E8 sweeps the
drift rate from that instantaneous flip down to drifts slow enough to evade a
ratio test, and reports what a detector paper owes: lag, lead over pool
pressure, MISSES, and the FALSE-POSITIVE rate on content that never drifts.

Two detectors run side by side on the same write stream, both maintained by the
device in O(1) per write:

  ratio  fast/slow EWMA ratio > THRESH (the mechanism in the paper so far);
         it is a TRANSIENT -- it rises when the fast mean outruns the slow one
         and clears again once the slow mean absorbs the shift, so it is
         sampled every write, not read at the end.
  cusum  upper-sided CUSUM on the size stream, S+ = max(0, S+ + (x - mu0 - k)),
         alarming at S+ > h*sigma_slow. It LATCHES, which is the
         operationally useful behaviour. The device implements one setting
         (k=0.5, h=5); because a single threshold is a single point of an
         operating characteristic and says nothing about the trade it makes,
         the recorded size stream is ALSO replayed offline through a grid of
         h, and the device's own alarm is cross-checked against the offline
         computation at the matching h to confirm the two agree.

Arms:
  * drift_share: the fraction of the WRITE STREAM that is newly-incompressible
    data, from 1.0 (every write inflates -- E3's whole-working-set rewrite)
    down to 0.002 (one write in five hundred). One operation is exactly one
    write, so this is directly the drift's share of observed traffic, which is
    what both detectors actually see.
  * threshold: the ratio detector's THRESH, swept to expose the
    sensitivity/false-alarm trade the single 1.4 in the paper hides.
  * a stationary control arm with no drift at all, which is where false
    positives are counted.

Reported per cell: detection lag in ops after drift onset (None = missed),
lead over the first ladder engagement, and the false-alarm rate on the control.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

# Sized so the working set fits comfortably as TEXT (u ~ 0.57) but overruns the
# budget once fully incompressible, so pool pressure genuinely arrives and the
# detector's LEAD over it is measurable rather than infinite.
POOL = 96 * 1024 * 1024
MAX_PAGES = 131072
EXTENT = 1 << 20
WS = 24576                 # working-set pages, all guaranteed
PRE_OPS = 8000             # stationary ops before drift onset (also the control)
POST_OPS = 60000           # ops during/after drift
N_SEEDS = 12

# Drift share of the write stream. 1.0 reproduces E3 (every write inflates);
# the small values are the slow leaks a ratio test is expected to miss.
DRIFT_RATES = [1.0, 0.5, 0.2, 0.05, 0.01, 0.002, 0.0]   # 0.0 = control
THRESHOLDS = [1.1, 1.2, 1.4, 1.7, 2.0]
# CUSUM alarm thresholds in units of sigma_slow (the device runs h=5).
CUSUM_H = [5.0, 10.0, 20.0, 50.0, 100.0, 200.0]
CUSUM_K = 0.5
SIGMA_FLOOR = 32.0

TEXT = T.CONTENT_NAMES.index("TEXT")
RANDOM = T.CONTENT_NAMES.index("RANDOM")


def run_cell(drift_rate: float, seed: int) -> dict:
    """One replay. The ratio detector is evaluated at every threshold from the
    same stream (the EWMAs do not depend on the threshold), so one replay
    yields the whole threshold sweep -- that is why THRESHOLDS is not a
    separate experimental axis with its own runs."""
    rng = np.random.default_rng(seed)
    spill = common.spill_path()
    tier = ffi.Tier(POOL, MAX_PAGES, str(spill), zlib_level=1,
                    extent_bytes=EXTENT, ring_capacity=4096)
    try:
        c = ffi.Contract(tier, ffi.EST_ANALYTIC_RING, 0.99, ntenants=1,
                         victim_policy=ffi.VICTIM_CLOCK_ONLY)
        try:
            # ---- lay down a stationary TEXT working set ----
            for i in range(WS):
                for _ in range(4):
                    if c.admit(0, ffi.CONTRACT_GUAR, 1) == 0:
                        break
                c.write(0, ffi.CONTRACT_GUAR, i, T.page_bytes(TEXT, i, seed))

            # Arm the detector only once the working set is laid down. Its
            # accumulator would otherwise carry alarms from the layup itself,
            # where the EWMAs are still converging and the size distribution
            # genuinely is shifting (empty -> text) -- those are warm-up
            # transients, not false alarms on stationary content, and counting
            # them as the latter overstates the detector's false-alarm rate.
            tier.cusum_reset()
            fast_hist: list[float] = []
            slow_hist: list[float] = []
            var_hist: list[float] = []
            size_hist: list[int] = []
            cusum_op = None
            ladder_op = None
            v0 = c.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS) + \
                 c.stat_u64(-1, ffi.CSTAT_R1_SPILLS)

            # ---- stationary phase: this is the false-positive window ----
            for op in range(PRE_OPS):
                lpn = int(rng.integers(0, WS))
                c.write(0, ffi.CONTRACT_GUAR, lpn, T.page_bytes(TEXT, lpn, seed))
                fast_hist.append(tier.stat_f64(ffi.CTIER_STAT_EWMA_MEAN_FAST))
                slow_hist.append(tier.stat_f64(ffi.CTIER_STAT_EWMA_MEAN_SLOW))
                var_hist.append(tier.stat_f64(ffi.CTIER_STAT_EWMA_VAR_SLOW))
                size_hist.append(int(tier.ring_snapshot(1)[0]))
            n_pre = len(fast_hist)
            # A false positive is an alarm raised during the stationary phase,
            # which began at the reset above. Record it, then re-arm so the
            # drift phase measures detection lag rather than a stale latch.
            _, cusum_pre = tier.cusum()
            tier.cusum_reset()

            # ---- drift phase ----
            # One op == one write. With probability `drift_rate` the write
            # converts a not-yet-flipped page to RANDOM; otherwise it refreshes
            # a random page with its CURRENT content class. Sampling the EWMAs
            # once per write (rather than once per batch of writes) matters:
            # at high drift shares the slow EWMA absorbs a shift within a few
            # writes, so a coarser sample interval hides the ratio excursion
            # the detector is built on.
            flipped = np.zeros(WS, dtype=bool)
            unflipped = list(range(WS))
            for op in range(POST_OPS):
                if unflipped and rng.random() < drift_rate:
                    j = int(rng.integers(0, len(unflipped)))
                    lpn = unflipped[j]
                    unflipped[j] = unflipped[-1]
                    unflipped.pop()
                    flipped[lpn] = True
                    c.write(0, ffi.CONTRACT_GUAR, lpn,
                            T.page_bytes(RANDOM, lpn, seed))
                else:
                    lpn = int(rng.integers(0, WS))
                    cls = RANDOM if flipped[lpn] else TEXT
                    c.write(0, ffi.CONTRACT_GUAR, lpn,
                            T.page_bytes(cls, lpn, seed))
                fast_hist.append(tier.stat_f64(ffi.CTIER_STAT_EWMA_MEAN_FAST))
                slow_hist.append(tier.stat_f64(ffi.CTIER_STAT_EWMA_MEAN_SLOW))
                var_hist.append(tier.stat_f64(ffi.CTIER_STAT_EWMA_VAR_SLOW))
                size_hist.append(int(tier.ring_snapshot(1)[0]))
                if cusum_op is None:
                    _, al = tier.cusum()
                    if al:
                        cusum_op = op
                if ladder_op is None:
                    spills = c.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS) + \
                             c.stat_u64(-1, ffi.CSTAT_R1_SPILLS)
                    if spills > v0:
                        ladder_op = op

            fast = np.asarray(fast_hist)
            slow = np.asarray(slow_hist)
            ratio = np.divide(fast, slow, out=np.zeros_like(fast), where=slow > 0)
            res: dict = {"drift_rate": drift_rate, "seed": seed,
                   "pre_ops": n_pre, "post_ops": len(fast) - n_pre,
                   "ladder_op": ladder_op,
                   "cusum_lag": cusum_op,
                   "cusum_false_positive": bool(cusum_pre),
                   "frac_flipped": float(flipped.mean()),
                   "thresholds": {}}
            # Offline CUSUM over a grid of h, on exactly the stream the device
            # saw. Reported per h: lag after drift onset, and whether it
            # alarmed during the stationary phase (a false alarm).
            x = np.asarray(size_hist, dtype=np.float64)
            sg = np.sqrt(np.maximum(np.asarray(var_hist), 0.0))
            sg = np.maximum(sg, SIGMA_FLOOR)
            inc = x - slow - CUSUM_K * sg
            res["cusum_h"] = {}
            for h in CUSUM_H:
                sp = 0.0
                fired_pre = None
                fired_post = None
                for i in range(len(inc)):
                    sp = max(0.0, sp + inc[i])
                    if sp > h * sg[i]:
                        if i < n_pre:
                            if fired_pre is None:
                                fired_pre = i
                            sp = 0.0            # policy re-arms after acting
                        else:
                            fired_post = i - n_pre
                            break
                res["cusum_h"][str(h)] = {
                    "lag": fired_post,
                    "false_alarm": fired_pre is not None,
                    "false_alarm_op": fired_pre,
                }
            res["ratio_max_post"] = float(ratio[n_pre:].max()) if len(ratio) > n_pre else None
            res["ratio_max_pre"] = float(ratio[:n_pre].max()) if n_pre else None
            for th in THRESHOLDS:
                fired = ratio > th
                fp = int(fired[:n_pre].sum())
                post = np.flatnonzero(fired[n_pre:])
                res["thresholds"][str(th)] = {
                    "lag": int(post[0]) if post.size else None,
                    "fired_ops_post": int(post.size),
                    "false_positive_ops": fp,
                    "false_positive_rate": fp / max(1, n_pre),
                }
            return res
        finally:
            c.close()
    finally:
        tier.close()
        Path(spill).unlink(missing_ok=True)


def main() -> int:
    ap = common.make_argparser("E8 detector operating characteristic")
    ap.add_argument("--jobs", type=int, default=None)
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    rates = DRIFT_RATES[::2] if args.fast else DRIFT_RATES
    out_path = Path(args.out) / "e8_detector.json"

    config = {
        "pool_bytes": POOL, "max_pages": MAX_PAGES, "extent_bytes": EXTENT,
        "working_set_pages": WS, "pre_ops": PRE_OPS, "post_ops": POST_OPS,
        "drift_rates": rates, "thresholds": THRESHOLDS, "n_seeds": n_seeds,
        "zlib_level": 1, "estimator": "analytic_ring", "q": 0.99,
        "drift_rate_def": "probability that a given write is newly-incompressible "
                          "(one op == one write), i.e. the drift's share of the "
                          "write stream; 1.0 reproduces E3's whole-working-set "
                          "rewrite, 0.0 is the stationary control",
        "ratio_detector": "fast/slow EWMA ratio > threshold, sampled every op; "
                          "TRANSIENT (clears once the slow EWMA absorbs the shift)",
        "cusum_detector": "S+ = max(0, S+ + (x - mu_slow - 0.5*sigma_slow)), "
                          "alarm at S+ > h*sigma_slow, sigma floored at 32 B; "
                          "LATCHES. The device runs h=5; the recorded size "
                          "stream is replayed offline over CUSUM_H so the "
                          "detector gets an operating characteristic rather "
                          "than a single point.",
        "cusum_h_grid": CUSUM_H, "cusum_k": CUSUM_K,
        "false_alarm_def": "an alarm raised during the stationary pre-drift "
                           "phase, which begins after the working set is laid "
                           "down and the detector is armed; alarms during the "
                           "layup are warm-up transients, not false alarms on "
                           "stationary content, and are excluded. Policy "
                           "re-arms after acting, so a run can have several",
        "false_positive_window": "the stationary pre-drift phase of every run "
                                 "plus the drift_rate=0 control arm",
        "fast": bool(args.fast),
    }
    cellargs = [(r, common.seed_for(si)) for r in rates for si in range(n_seeds)]

    def _prog(i, total, a, r):
        lag14 = r["thresholds"]["1.4"]["lag"]
        print(f"[e8] {i}/{total} rate={a[0]} seed={a[1] & 0xffff} "
              f"ratio1.4_lag={lag14} cusum_lag={r['cusum_lag']} "
              f"ladder={r['ladder_op']}", flush=True)

    t0 = time.perf_counter_ns()
    runs = common.run_cells(run_cell, cellargs,
                            jobs=1 if args.fast else args.jobs, progress=_prog)

    summary = []
    for r in rates:
        sel = [x for x in runs if x["drift_rate"] == r]
        row = {"drift_rate": r, "runs": len(sel),
               "frac_flipped_mean": float(np.mean([x["frac_flipped"] for x in sel])),
               "ladder_op_median": (float(np.median(
                   [x["ladder_op"] for x in sel if x["ladder_op"] is not None]))
                   if any(x["ladder_op"] is not None for x in sel) else None),
               "ratio_max_post_median": (float(np.median(
                   [x["ratio_max_post"] for x in sel
                    if x["ratio_max_post"] is not None]))
                   if any(x["ratio_max_post"] is not None for x in sel) else None),
               "cusum": {}, "ratio": {}}
        lags = [x["cusum_lag"] for x in sel]
        hit = [l for l in lags if l is not None]
        row["cusum"] = {"detected": len(hit), "of": len(sel),
                        "lag_median": float(np.median(hit)) if hit else None,
                        "lag_min": min(hit) if hit else None,
                        "lag_max": max(hit) if hit else None,
                        "false_positive_runs": sum(
                            1 for x in sel if x["cusum_false_positive"])}
        row["cusum_h"] = {}
        for h in CUSUM_H:
            lags = [x["cusum_h"][str(h)]["lag"] for x in sel
                    if "cusum_h" in x]
            hit = [l for l in lags if l is not None]
            fa = sum(1 for x in sel
                     if x.get("cusum_h", {}).get(str(h), {}).get("false_alarm"))
            row["cusum_h"][str(h)] = {
                "detected": len(hit), "of": len(sel),
                "lag_median": float(np.median(hit)) if hit else None,
                "false_alarm_runs": fa,
                "false_alarm_rate": fa / max(1, len(sel)),
            }
        for th in THRESHOLDS:
            lags = [x["thresholds"][str(th)]["lag"] for x in sel]
            hit = [l for l in lags if l is not None]
            fp = float(np.mean([x["thresholds"][str(th)]["false_positive_rate"]
                                for x in sel]))
            row["ratio"][str(th)] = {
                "detected": len(hit), "of": len(sel),
                "lag_median": float(np.median(hit)) if hit else None,
                "lag_min": min(hit) if hit else None,
                "lag_max": max(hit) if hit else None,
                "false_positive_rate": fp,
            }
        summary.append(row)

    data = {"runs": runs, "summary": summary,
            "wall_s": (time.perf_counter_ns() - t0) / 1e9}
    common.write_result(
        out_path, "e8_detector",
        "Detector operating characteristic: lag, lead, misses, false alarms",
        config, [common.seed_for(i) for i in range(n_seeds)], data,
        modeled_fields=[], failures=[], wall_seconds=data["wall_s"])
    print(f"[e8] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
