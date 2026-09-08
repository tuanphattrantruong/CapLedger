#!/usr/bin/env python3
"""E1 — contract accuracy: calibration of advertised C_q (the money figure).

For workload in {steady-mixed, drift} x estimator in {analytic (tenant EWMA),
analytic_ring (the same closed form fed by the telemetry window), bootstrap} x
q in {0.5, 0.8, 0.9, 0.95, 0.99} x 60 seeds:

  fresh 64 MiB tier (zlib level 1) + contract; warm 4096 GUAR pages of
  workload content (admitted first); read the advertised C_q (analytic:
  contract_capacity_pages, the C hot path; bootstrap: cq_bootstrap on the
  telemetry ring with budget = 0.95*pool - PHYS_USED - RESV_BYTES); admit
  exactly that many GUAR pages (EXTERNAL estimator's gate always accepts for
  the bootstrap arm) and write them with workload-distributed content.

  Violation is measured two ways, both recorded:

  * violation (primary, demand-based): the realized demand of the fill —
    the exact sum of class-rounded compressed sizes of the C_q written pages,
    measured by draining the tier's 4096-entry telemetry ring every 4096
    successful writes — exceeded the budget the estimator was given, or any
    hard failure occurred. This is precisely the event P(sum > B) <= 1-q that
    C_q bounds, so it is the calibration-curve event.
  * violation_ladder (secondary, as described in DESIGN's experiment matrix):
    (VIOLATIONS delta > 0) or (HARD_FAILURES delta > 0) during the fill.
    This SATURATES near 1 for every q by construction: the advertisement
    targets the full budget B_total while the R3 rung fires at 0.97*B_total,
    so filling exactly C_q pages crosses the forced-spill watermark almost
    surely regardless of q. It measures the ladder/advertisement interaction,
    not the advertisement's calibration; reported alongside as a finding.

  Per cell: empirical violation fraction vs target 1-q with Wilson 95% CI,
  overflow magnitudes (demand - budget, and the spec'd peak PHYS_USED -
  0.95*pool which a working ladder keeps at ~0), advertised pages, and the
  analytic-vs-bootstrap capacity gap computed on identical telemetry
  snapshots (bootstrap arm: cq_bootstrap vs cq_normal on the same instant's
  tenant mu/sigma; analytic arm: C value vs Python cq_normal).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T
from bootstrap import cq_bootstrap, cq_normal

POOL = 64 * 1024 * 1024
MAX_PAGES = 262144
WARM = 4096
N_SEEDS = 60
QS = [0.5, 0.8, 0.9, 0.95, 0.99]
WORKLOADS = ["steady_mixed", "drift"]
ESTIMATORS = ["analytic", "analytic_ring", "bootstrap"]
FILL_CAP = MAX_PAGES - WARM - 64
DRIFT_RAMP_OPS = 20000
BOOT_K = 200


def content_seq(workload: str, n: int, seed: int) -> np.ndarray:
    if workload == "steady_mixed":
        return T.steady_mixed_content(n, seed)
    if workload == "drift":
        return T.drift_content(n, seed, r0=0.0, r1=0.8, ramp_ops=DRIFT_RAMP_OPS)
    raise ValueError(workload)


def run_trial_mp(workload: str, estimator: str, q: float, seed: int) -> dict:
    """Pool-friendly wrapper: failures travel back inside the result dict."""
    failures: list[str] = []
    r = run_trial(workload, estimator, q, seed, failures)
    r["_failures"] = failures
    return r


def run_trial(workload: str, estimator: str, q: float, seed: int,
              failures: list[str]) -> dict:
    contents = content_seq(workload, WARM + FILL_CAP, seed)
    est_id = {"analytic": ffi.EST_ANALYTIC,
              "analytic_ring": ffi.EST_ANALYTIC_RING}.get(estimator,
                                                          ffi.EST_EXTERNAL)
    rec: dict = {"workload": workload, "estimator": estimator, "q": q, "seed": seed}
    with common.temp_spill() as sp:
        tier = ffi.Tier(POOL, MAX_PAGES, str(sp), zlib_level=1, ring_capacity=4096)
        contract = None
        try:
            contract = ffi.Contract(tier, est_id, q, headroom_frac=0.05,
                                    static_ratio=2.0, epoch_ops=1024,
                                    ntenants=1,
                                    victim_policy=ffi.VICTIM_DENSITY_AGE)
            # ---- warm phase (cold-start pitfall: >=64 samples; we use 4096)
            rc = contract.admit(0, ffi.CONTRACT_GUAR, WARM)
            rec["warm_admit_rc"] = rc
            if rc != 0:
                failures.append(f"trial {rec}: warm admission rejected (rc={rc})")
                rec["aborted"] = "warm_admit_rejected"
                return rec
            warm_fail = 0
            for i in range(WARM):
                page = T.page_bytes(int(contents[i]), i, seed, 0)
                if contract.write(0, ffi.CONTRACT_GUAR, i, page) != 0:
                    warm_fail += 1
            rec["warm_write_failures"] = warm_fail

            # ---- identical-snapshot capacity estimates
            phys = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
            resv = contract.stat_u64(-1, ffi.CSTAT_RESV_BYTES)
            budget = int(0.95 * POOL) - phys - resv
            mu_t = contract.stat_f64(0, ffi.CSTAT_MU_TENANT)
            sigma_t = contract.stat_f64(0, ffi.CSTAT_SIGMA_TENANT)
            ring = tier.ring_snapshot(4096)
            rec.update(phys_after_warm=phys, resv_after_warm=resv,
                       budget_bytes=budget, mu_tenant=mu_t, sigma_tenant=sigma_t,
                       ring_len=int(ring.size))
            cq_norm_py = cq_normal(mu_t, sigma_t, budget, q)
            rec["cq_normal_py"] = cq_norm_py
            if estimator in ("analytic", "analytic_ring"):
                advertised = contract.capacity_pages(0, ffi.CONTRACT_GUAR)
                rec["cq_analytic_c"] = advertised
                # Acklam (C) vs NormalDist (py) cross-check on the same state
                rec["analytic_c_vs_py_gap"] = advertised - cq_norm_py
            else:
                t_b0 = time.perf_counter_ns()
                advertised = cq_bootstrap(
                    ring, budget, q, K=BOOT_K,
                    rng=np.random.default_rng(T._hash3(seed, 0xB007, 0)))
                rec["bootstrap_ms"] = (time.perf_counter_ns() - t_b0) / 1e6
                rec["cq_bootstrap"] = advertised
                # analytic-vs-bootstrap gap on the identical snapshot
                rec["analytic_vs_bootstrap_gap"] = cq_norm_py - advertised
            advertised = int(advertised)
            rec["advertised_pages"] = advertised
            n_fill = min(advertised, FILL_CAP)
            rec["fill_capped"] = n_fill < advertised
            if rec["fill_capped"]:
                failures.append(
                    f"trial {rec['workload']}/{estimator}/q={q}/seed={seed}: "
                    f"advertised {advertised} capped to {n_fill} by max_pages")
            rec["n_fill"] = n_fill

            # ---- admit exactly the advertised count, then fill
            admit_rc = contract.admit(0, ffi.CONTRACT_GUAR, n_fill)
            rec["fill_admit_rc"] = admit_rc
            n_admitted = n_fill
            retries = 0
            while admit_rc != 0 and retries < 8 and n_admitted > 0:
                retries += 1
                n_admitted = max(0, int(n_admitted * 0.98) - 1)
                admit_rc = contract.admit(0, ffi.CONTRACT_GUAR, n_admitted)
            if retries:
                rec["fill_admit_retries"] = retries
                rec["fill_admitted"] = n_admitted
                failures.append(
                    f"trial {rec['workload']}/{estimator}/q={q}/seed={seed}: "
                    f"advertised C_q={n_fill} was not admittable as-is "
                    f"(final rc={admit_rc}, admitted {n_admitted})")
            if admit_rc != 0:
                rec["aborted"] = "fill_admit_rejected"
                return rec
            n_fill = n_admitted

            v0 = contract.stat_u64(-1, ffi.CSTAT_VIOLATIONS)
            h0 = contract.stat_u64(-1, ffi.CSTAT_HARD_FAILURES)
            r3_0 = contract.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS)
            peak_phys = phys
            consec_enospace = 0
            fill_rcs = {"ok": 0, "enospace": 0, "other": 0}
            # exact realized demand via ring drains: with ring_capacity == 4096
            # and this contract as the only writer, snapshotting after exactly
            # 4096 successful writes yields precisely those writes' sizes
            demand_bytes = 0
            since_snap = 0
            t_f0 = time.perf_counter_ns()
            for i in range(n_fill):
                lpn = WARM + i
                page = T.page_bytes(int(contents[WARM + i]), lpn, seed, 0)
                rc = contract.write(0, ffi.CONTRACT_GUAR, lpn, page)
                if rc == 0:
                    fill_rcs["ok"] += 1
                    consec_enospace = 0
                    since_snap += 1
                    if since_snap == 4096:
                        demand_bytes += int(tier.ring_snapshot(4096).sum())
                        since_snap = 0
                elif rc == ffi.CONTRACT_ENOSPACE:
                    fill_rcs["enospace"] += 1
                    consec_enospace += 1
                    if consec_enospace > 64:
                        rec["fill_break_early_at"] = i
                        break
                else:
                    fill_rcs["other"] += 1
                p = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
                if p > peak_phys:
                    peak_phys = p
            if since_snap:
                # Order-free tail drain: the ABI does not specify ring order,
                # so pad with zero-page tier writes (telemetry records 0, no
                # physical cost, estimator no longer read) until the ring
                # holds exactly the uncounted tail plus zeros, then sum all.
                zpage = np.zeros(ffi.PAGE, dtype=np.uint8)
                pad_ok = True
                for _ in range(4096 - since_snap):
                    if tier.write(MAX_PAGES - 1, zpage) < 0:
                        pad_ok = False
                        break
                tail = tier.ring_snapshot(4096)
                if pad_ok:
                    demand_bytes += int(tail.sum())
                else:  # fall back to newest-last slice assumption
                    demand_bytes += int(tail[-since_snap:].sum())
                    failures.append(
                        f"trial {rec['workload']}/{estimator}/q={q}/seed={seed}: "
                        f"zero-page ring padding failed; tail demand used "
                        f"newest-last ordering assumption")
            rec["fill_wall_s"] = (time.perf_counter_ns() - t_f0) / 1e9
            rec["fill_rcs"] = fill_rcs
            rec["violations_delta"] = contract.stat_u64(-1, ffi.CSTAT_VIOLATIONS) - v0
            rec["hard_failures_delta"] = contract.stat_u64(-1, ffi.CSTAT_HARD_FAILURES) - h0
            rec["r3_delta"] = contract.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS) - r3_0
            rec["demand_bytes"] = demand_bytes
            rec["demand_overflow_bytes"] = max(0, demand_bytes - budget)
            rec["violation"] = bool(demand_bytes > budget
                                    or rec["hard_failures_delta"] > 0)
            rec["violation_ladder"] = bool(rec["violations_delta"] > 0
                                           or rec["hard_failures_delta"] > 0)
            rec["peak_phys_used"] = int(peak_phys)
            rec["overflow_bytes"] = max(0, int(peak_phys) - int(0.95 * POOL))
            rec["final_phys_used"] = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
            return rec
        finally:
            if contract is not None:
                contract.close()
            tier.close()


def main() -> int:
    ap = common.make_argparser("E1 contract accuracy / calibration curve")
    ap.add_argument("--jobs", type=int, default=None,
                    help="parallel cells (default: performance cores - 2). "
                         "Cells are independent and counter-valued; this "
                         "changes wall time only.")
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    out_path = Path(args.out) / "e1_accuracy.json"
    seeds = [common.seed_for(i) for i in range(n_seeds)]
    config = {
        "pool_bytes": POOL, "max_pages": MAX_PAGES, "zlib_level": 1,
        "warm_pages": WARM, "qs": QS, "workloads": WORKLOADS,
        "estimators": ESTIMATORS, "n_seeds": n_seeds,
        "steady_mix": {T.CONTENT_NAMES[k]: v for k, v in T.E1_STEADY_MIX.items()},
        "drift": {"r0": 0.0, "r1": 0.8, "ramp_ops": DRIFT_RAMP_OPS},
        "bootstrap_K": BOOT_K, "headroom_frac": 0.05,
        "budget_formula": "0.95*pool_bytes - PHYS_USED - RESV_BYTES (at snapshot)",
        "violation_def": "PRIMARY (demand-based): sum of class-rounded sizes "
                         "of the fill's written pages (exact, via telemetry-"
                         "ring drains every 4096 successful writes) > budget, "
                         "or HARD_FAILURES delta > 0 — exactly the event "
                         "P(sum > B) <= 1-q that C_q bounds",
        "violation_ladder_def": "SECONDARY: VIOLATIONS delta > 0 or "
                                "HARD_FAILURES delta > 0 during fill; saturates "
                                "~1 for all q because the advertisement targets "
                                "B_total while R3 fires at 0.97*B_total (ladder/"
                                "advertisement interaction, reported as such)",
        "demand_measurement_assumption": "tier telemetry records class-rounded "
                                         "size once per successful write only; "
                                         "ring_capacity=4096 and this contract "
                                         "is the sole writer",
        "overflow_def": "demand_overflow = max(0, demand_bytes - budget); "
                        "overflow_bytes = max(0, peak PHYS_USED during fill - "
                        "0.95*pool_bytes) (spec'd; ~0 whenever the ladder works)",
        "fast": bool(args.fast),
    }
    failures: list[str] = []
    trials: list[dict] = []
    t0 = time.perf_counter_ns()
    total = len(WORKLOADS) * len(ESTIMATORS) * len(QS) * n_seeds
    done = 0
    cellargs = [(w, e, q, common.seed_for(si))
                for w in WORKLOADS for e in ESTIMATORS for q in QS
                for si in range(n_seeds)]

    def _prog(i, tot, a, r):
        print(f"[e1] {i}/{tot} {a[0]}/{a[1]}/q={a[2]} "
              f"viol={r.get('violation')}", flush=True)

    trials = common.run_cells(run_trial_mp, cellargs, jobs=args.jobs,
                              progress=_prog)
    for t in trials:
        failures.extend(t.pop("_failures", []))

    curve = []
    for workload in WORKLOADS:
        for estimator in ESTIMATORS:
            for q in QS:
                cell = [t for t in trials
                        if t["workload"] == workload and t["estimator"] == estimator
                        and t["q"] == q and "violation" in t]
                k = sum(1 for t in cell if t["violation"])
                k_ladder = sum(1 for t in cell if t["violation_ladder"])
                n = len(cell)
                lo, hi = common.wilson_ci(k, n)
                overflow = [t["overflow_bytes"] for t in cell]
                d_over = [t["demand_overflow_bytes"] for t in cell]
                adv = [t["advertised_pages"] for t in cell]
                entry = {
                    "workload": workload, "estimator": estimator, "q": q,
                    "target_violation_rate": round(1.0 - q, 6),
                    "n_trials": n,
                    "violations": k,
                    "empirical_violation_rate": (k / n) if n else None,
                    "wilson95_lo": lo, "wilson95_hi": hi,
                    "violations_ladder": k_ladder,
                    "empirical_ladder_violation_rate": (k_ladder / n) if n else None,
                    "demand_overflow_bytes_mean": float(np.mean(d_over)) if d_over else None,
                    "demand_overflow_bytes_max": int(np.max(d_over)) if d_over else None,
                    "overflow_bytes_mean": float(np.mean(overflow)) if overflow else None,
                    "overflow_bytes_max": int(np.max(overflow)) if overflow else None,
                    "r3_delta_mean": float(np.mean([t["r3_delta"] for t in cell])) if cell else None,
                    "advertised_pages_mean": float(np.mean(adv)) if adv else None,
                }
                if estimator == "bootstrap":
                    gaps = [t["analytic_vs_bootstrap_gap"] for t in cell
                            if "analytic_vs_bootstrap_gap" in t]
                    entry["analytic_vs_bootstrap_gap_mean"] = (
                        float(np.mean(gaps)) if gaps else None)
                else:
                    gaps = [t["analytic_c_vs_py_gap"] for t in cell
                            if "analytic_c_vs_py_gap" in t]
                    entry["analytic_c_vs_py_gap_mean"] = (
                        float(np.mean(gaps)) if gaps else None)
                curve.append(entry)

    data = {
        "calibration_curve": curve,
        "trials": trials,
        "note": (
            f"Primary 'violation' is demand-based (realized fill bytes > "
            f"advertised budget) — the exact event C_q bounds; "
            f"'violation_ladder' additionally reports the R3/hard-failure "
            f"definition, which saturates near 1 for every q because the "
            f"advertisement targets B_total while the R3 watermark sits at "
            f"0.97*B_total (see config.violation_ladder_def). "
            f"With {n_seeds} seeds per cell the resolvable violation "
            f"probability is coarse: at q=0.99 the target rate 0.01 cannot be "
            f"distinguished from 0 with {n_seeds} Bernoulli trials (Wilson 95% "
            f"CI for 0/{n_seeds} spans up to "
            f"~{common.wilson_ci(0, max(1, n_seeds))[1]:.3f}). Violation "
            f"magnitude (overflow bytes) is reported alongside to strengthen "
            f"the calibration story."),
    }
    wall = (time.perf_counter_ns() - t0) / 1e9
    common.write_result(out_path, "E1",
                        "calibration of advertised C_q vs empirical violation rate",
                        config, seeds, data, modeled_fields=[],
                        failures=failures, wall_seconds=wall)
    print(f"wrote {out_path} ({len(trials)} trials, {wall:.1f}s, "
          f"{len(failures)} failure(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
