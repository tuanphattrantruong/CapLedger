#!/usr/bin/env python3
"""E3 — compressibility-crash phase stress.

Crash trace (TEXT -> RANDOM rewrite of the same lpns), 48 MiB pool, configs
{analytic+DENSITY_AGE, analytic+CLOCK_ONLY, optimistic, static 2.0,
analytic-ring+CLOCK, zswap-reactive+GLOBAL_LRU,
transparent-reserve+CLOCK} x 12 seeds.
Replay with per-op wall timing; full stats snapshot every 500 ops; cheap
per-op polling of LIE_FLAG / PRESSURE_LEVEL for exact first-crossing ops.

Derived per run:
  - detection lag (ops and wall ns) from the first phase-1 write to
    LIE_FLAG==1 and to the first PRESSURE_LEVEL>=1 after the flip
  - p99/p50 per-op stall, crash window vs steady window
  - bytes reclaimed per spill: measured SPILL_BYTES_W per spill event, plus a
    windowed Delta-PHYS_USED estimate per R3 (listed in modeled_fields: it
    assumes phase-1 writes add 4096 B each — formula in config)
  - recovery time: from the first post-flip ladder engagement until a
    >=2000-op ladder-quiescent stretch with u <= 0.85 (the ladder recovers
    synchronously inside the crossing write, so u-threshold crossings are
    invisible at snapshot granularity)
  - data.ocp_sensitivity: modeled device-time series = measured op counts x
    OCP latency parameter {150, 250, 500, 1000} ns (modeled, never measured;
    zlib emulation cost is reported separately from the tier's own
    COMP/DECOMP_NS counters so it can be factored out)

The trace uses n_pages=14336 (not DESIGN 4.2's sketch value 20480): measured
TEXT compressibility on this machine (~2217 B/page class-rounded) means 20480
pages would saturate the 48 MiB pool in phase 0 already; see
workloads/traces.py crash() docstring.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

POOL = 48 * 1024 * 1024
# Extent size sets how many extents the pool has, and the device dedicates
# whole extents to size classes. Too few and it starves on EXTENTS while
# its bytes are free, which moves recovery from a watermark the contract
# controls onto device exhaustion (E11 measures that boundary).
EXTENT = 1 << 20
MAX_PAGES = 32768
N_PAGES = 14336
OPP_FRAC = 0.125
N_SEEDS = 12
SNAP_EVERY = 500
OCP_NS = [150, 250, 500, 1000]
B_TOTAL = int(0.95 * POOL)

CONFIGS = [
    {"label": "analytic_density_age", "estimator": ffi.EST_ANALYTIC,
     "victim": ffi.VICTIM_DENSITY_AGE},
    {"label": "analytic_clock_only", "estimator": ffi.EST_ANALYTIC,
     "victim": ffi.VICTIM_CLOCK_ONLY},
    {"label": "optimistic", "estimator": ffi.EST_OPTIMISTIC,
     "victim": ffi.VICTIM_DENSITY_AGE},
    {"label": "static_2.0", "estimator": ffi.EST_STATIC,
     "victim": ffi.VICTIM_DENSITY_AGE},
    {"label": "analytic_ring_clock", "estimator": ffi.EST_ANALYTIC_RING,
     "victim": ffi.VICTIM_CLOCK_ONLY},
    # External baselines (see e5_multitenant.py CONFIGS for what they model).
    {"label": "zswap_reactive", "estimator": ffi.EST_ZSWAP,
     "victim": ffi.VICTIM_GLOBAL_LRU},
    {"label": "transparent_reserve", "estimator": ffi.EST_TRANSPARENT,
     "victim": ffi.VICTIM_CLOCK_ONLY},
]

SNAP_U64_TIER = {
    "phys_used": ffi.CTIER_STAT_PHYS_USED,
    "pages_present": ffi.CTIER_STAT_PAGES_PRESENT,
    "pages_spilled": ffi.CTIER_STAT_PAGES_SPILLED,
    "spill_bytes_w": ffi.CTIER_STAT_SPILL_BYTES_W,
    "writes": ffi.CTIER_STAT_WRITES,
    "reads": ffi.CTIER_STAT_READS,
    "promotes": ffi.CTIER_STAT_PROMOTES,
    "comp_ns": ffi.CTIER_STAT_COMP_NS_TOTAL,
    "decomp_ns": ffi.CTIER_STAT_DECOMP_NS_TOTAL,
}
SNAP_U64_CONTRACT = {
    "pressure_level": ffi.CSTAT_PRESSURE_LEVEL,
    "lie_flag": ffi.CSTAT_LIE_FLAG,
    "violations": ffi.CSTAT_VIOLATIONS,
    "r1_spills": ffi.CSTAT_R1_SPILLS,
    "r2_shed_calls": ffi.CSTAT_R2_SHED_CALLS,
    "r2_shed_bytes": ffi.CSTAT_R2_SHED_BYTES,
    "r3_forced_spills": ffi.CSTAT_R3_FORCED_SPILLS,
    "recovery_ns": ffi.CSTAT_RECOVERY_NS,
    "opp_drops": ffi.CSTAT_OPP_DROPS,
    "hard_failures": ffi.CSTAT_HARD_FAILURES,
}


def run_one(cfg: dict, seed: int, failures: list[str]) -> tuple[dict, dict]:
    trace = T.crash(n_pages=N_PAGES, seed=seed, opp_frac=OPP_FRAC)
    flip = trace["attrs"]["phase_flip_op"]
    guar_pages = trace["attrs"]["params"]["guar_pages"]
    opp_pages = trace["attrs"]["params"]["opp_pages"]
    rec: dict = {"label": cfg["label"], "seed": seed, "flip_op": flip,
                 "guar_pages": guar_pages, "opp_pages": opp_pages}
    ts: dict = {k: [] for k in ["op", "wall_ns", "cq_guar_pages", "invariant"]
                }
    for k in list(SNAP_U64_TIER) + list(SNAP_U64_CONTRACT):
        ts[k] = []

    with common.temp_spill() as sp:
        tier = ffi.Tier(POOL, MAX_PAGES, str(sp), zlib_level=1,
                        extent_bytes=EXTENT)
        contract = None
        try:
            contract = ffi.Contract(tier, cfg["estimator"], 0.99,
                                    headroom_frac=0.05, static_ratio=2.0,
                                    epoch_ops=1024, ntenants=1,
                                    victim_policy=cfg["victim"])
            # upfront admission of the working set; top up as the estimator
            # warms (cold start starts from the pessimistic prior)
            admitted = 0
            if contract.admit(0, ffi.CONTRACT_GUAR, guar_pages) == 0:
                admitted = guar_pages
            else:
                c = contract.capacity_pages(0, ffi.CONTRACT_GUAR)
                if c > 0 and contract.admit(0, ffi.CONTRACT_GUAR, min(c, guar_pages)) == 0:
                    admitted = min(c, guar_pages)
            contract.admit(0, ffi.CONTRACT_OPP, opp_pages)
            rec["admitted_guar_initial"] = admitted
            admit_state = {"admitted": admitted}

            first = {"lie_post_flip": None, "l1_post_flip": None,
                     "lie_any": None, "l1_any": None,
                     "ladder_post_flip": None, "ladder_any": None}
            wall = {"at_flip": None, "at_lie": None, "at_l1": None,
                    "at_ladder": None}

            def snapshot(i: int) -> None:
                # cheap per-op polling for exact first crossings. NOTE:
                # PRESSURE_LEVEL may read 0 here because the ladder recovers
                # synchronously inside the write; the robust engagement signal
                # is the first increase of the R1/R2/R3 counters.
                lie = contract.stat_u64(-1, ffi.CSTAT_LIE_FLAG)
                lvl = contract.stat_u64(-1, ffi.CSTAT_PRESSURE_LEVEL)
                ladder = (contract.stat_u64(-1, ffi.CSTAT_R1_SPILLS)
                          + contract.stat_u64(-1, ffi.CSTAT_R2_SHED_CALLS)
                          + contract.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS))
                now = time.perf_counter_ns()
                if i == flip - 1:
                    wall["at_flip"] = now
                if lie and first["lie_any"] is None:
                    first["lie_any"] = i
                if lvl >= 1 and first["l1_any"] is None:
                    first["l1_any"] = i
                if ladder > 0 and first["ladder_any"] is None:
                    first["ladder_any"] = i
                if i >= flip:
                    if lie and first["lie_post_flip"] is None:
                        first["lie_post_flip"] = i
                        wall["at_lie"] = now
                    if lvl >= 1 and first["l1_post_flip"] is None:
                        first["l1_post_flip"] = i
                        wall["at_l1"] = now
                    if ladder > 0 and first["ladder_post_flip"] is None:
                        first["ladder_post_flip"] = i
                        wall["at_ladder"] = now
                # estimator warm-up top-up every 512 ops
                if (i + 1) % 512 == 0 and admit_state["admitted"] < guar_pages:
                    deficit = guar_pages - admit_state["admitted"]
                    c = contract.capacity_pages(0, ffi.CONTRACT_GUAR)
                    n = min(deficit, c)
                    if n > 0 and contract.admit(0, ffi.CONTRACT_GUAR, n) == 0:
                        admit_state["admitted"] += n
                # full snapshot row every SNAP_EVERY ops
                if (i + 1) % SNAP_EVERY == 0:
                    ts["op"].append(i)
                    ts["wall_ns"].append(now)
                    for k, sid in SNAP_U64_TIER.items():
                        ts[k].append(tier.stat_u64(sid))
                    for k, sid in SNAP_U64_CONTRACT.items():
                        ts[k].append(contract.stat_u64(-1, sid))
                    ts["cq_guar_pages"].append(contract.stat_f64(0, ffi.CSTAT_CQ_GUAR_PAGES))
                    ts["invariant"].append(contract.check_invariant())

            res = T.replay(trace, contract, timing=True, snapshot_every=1,
                           snapshot_fn=snapshot)
            rec["admitted_guar_final"] = admit_state["admitted"]
            rec["replay_counts"] = {
                k: ({str(kk): vv for kk, vv in v.items()} if isinstance(v, dict) else v)
                for k, v in res.items() if k != "op_ns"}
            op_ns = res["op_ns"]

            # ---- stall percentiles (steady window excludes cold start)
            steady_ns = op_ns[2048:flip]
            crash_ns = op_ns[flip:]
            rec["stall_ns"] = {
                "steady_p50": float(np.percentile(steady_ns, 50)),
                "steady_p99": float(np.percentile(steady_ns, 99)),
                "steady_p999": float(np.percentile(steady_ns, 99.9)),
                "steady_max": int(steady_ns.max()),
                "crash_p50": float(np.percentile(crash_ns, 50)),
                "crash_p99": float(np.percentile(crash_ns, 99)),
                "crash_p999": float(np.percentile(crash_ns, 99.9)),
                "crash_max": int(crash_ns.max()),
            }

            # ---- detection lags
            rec["detect"] = {
                "first_lie_op_any": first["lie_any"],
                "first_l1_op_any": first["l1_any"],
                "first_ladder_op_any": first["ladder_any"],
                "lie_lag_ops": (first["lie_post_flip"] - flip
                                if first["lie_post_flip"] is not None else None),
                "l1_lag_ops": (first["l1_post_flip"] - flip
                               if first["l1_post_flip"] is not None else None),
                "ladder_lag_ops": (first["ladder_post_flip"] - flip
                                   if first["ladder_post_flip"] is not None else None),
                "lie_lag_wall_ns": (wall["at_lie"] - wall["at_flip"]
                                    if wall["at_lie"] and wall["at_flip"] else None),
                "l1_lag_wall_ns": (wall["at_l1"] - wall["at_flip"]
                                   if wall["at_l1"] and wall["at_flip"] else None),
                "ladder_lag_wall_ns": (wall["at_ladder"] - wall["at_flip"]
                                       if wall["at_ladder"] and wall["at_flip"] else None),
            }
            if first["lie_post_flip"] is None:
                failures.append(f"{cfg['label']}/seed{seed}: LIE_FLAG never rose after the flip")

            # ---- measured spill efficiency + windowed R3 reclaim estimate
            spills_total = (ts["r1_spills"][-1] + ts["r3_forced_spills"][-1]
                            if ts["op"] else 0)
            rec["spill_bytes_w_per_spill"] = (
                ts["spill_bytes_w"][-1] / spills_total if spills_total else None)
            r3_reclaim = []
            for w in range(1, len(ts["op"])):
                d_r3 = ts["r3_forced_spills"][w] - ts["r3_forced_spills"][w - 1]
                if d_r3 > 0 and ts["op"][w] >= flip:
                    d_phys = ts["phys_used"][w] - ts["phys_used"][w - 1]
                    d_writes = ts["writes"][w] - ts["writes"][w - 1]
                    d_r1 = ts["r1_spills"][w] - ts["r1_spills"][w - 1]
                    # phase-1 writes are RANDOM => +4096 B each (assumption)
                    est = (d_writes * 4096 - d_phys) / max(1, d_r3 + d_r1)
                    r3_reclaim.append({"op": ts["op"][w], "d_r3": d_r3,
                                       "d_r1": d_r1,
                                       "est_reclaim_per_spill": est})
            rec["r3_reclaim_windowed"] = r3_reclaim
            rec["r3_reclaim_windowed_mean"] = (
                float(np.mean([x["est_reclaim_per_spill"] for x in r3_reclaim]))
                if r3_reclaim else None)

            # ---- recovery time. The ladder recovers synchronously inside the
            # crossing write, so snapshot-granularity u rarely shows the
            # excursion; recovery is therefore defined as: from the first
            # post-flip ladder engagement until the start of a >=2000-op run
            # of windows with NO ladder activity and u <= 0.85 throughout.
            rec["recovery_ops"] = None
            rec["pressure_onset_op"] = first["ladder_post_flip"]
            ops_arr = np.array(ts["op"])
            u_arr = np.array(ts["phys_used"]) / B_TOTAL
            if first["ladder_post_flip"] is not None and len(ops_arr):
                act = (np.diff(np.array(ts["r1_spills"]), prepend=0)
                       + np.diff(np.array(ts["r2_shed_calls"]), prepend=0)
                       + np.diff(np.array(ts["r3_forced_spills"]), prepend=0)) > 0
                onset_op = first["ladder_post_flip"]
                for w in range(len(ops_arr)):
                    if ops_arr[w] <= onset_op or act[w] or u_arr[w] > 0.85:
                        continue
                    horizon = (ops_arr >= ops_arr[w]) & (ops_arr <= ops_arr[w] + 2000)
                    if np.all(~act[horizon]) and np.all(u_arr[horizon] <= 0.85):
                        rec["recovery_ops"] = int(ops_arr[w] - onset_op)
                        break
                if rec["recovery_ops"] is None:
                    rec["recovery_note"] = (
                        "no >=2000-op ladder-quiescent window with u <= 0.85 "
                        "after the first post-flip ladder engagement (writes "
                        "keep arriving until the trace ends)")
            else:
                rec["recovery_note"] = "ladder never engaged after the flip"

            # ---- final counters
            rec["final"] = {k: (ts[k][-1] if ts[k] else None)
                            for k in list(SNAP_U64_TIER) + list(SNAP_U64_CONTRACT)}
            rec["invariant_samples_failed"] = int(sum(1 for v in ts["invariant"] if v != 0))
            rec["invariant_failed_ops"] = [int(ts["op"][w]) for w, v
                                           in enumerate(ts["invariant"]) if v != 0]
            if rec["invariant_samples_failed"]:
                failures.append(
                    f"{cfg['label']}/seed{seed}: contract_check_invariant() "
                    f"nonzero in {rec['invariant_samples_failed']} of "
                    f"{len(ts['invariant'])} samples (ops "
                    f"{rec['invariant_failed_ops'][:6]}...)")
            rec["measured_wall_ns_total"] = int(op_ns.sum())
            rec["measured_zlib_ns_total"] = (
                (ts["comp_ns"][-1] + ts["decomp_ns"][-1]) if ts["op"] else None)
            return rec, ts
        finally:
            if contract is not None:
                contract.close()
            tier.close()


def main() -> int:
    ap = common.make_argparser("E3 phase-change (compressibility crash) stress")
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    seeds = [common.seed_for(i) for i in range(n_seeds)]
    out_path = Path(args.out) / "e3_phase_stress.json"
    config = {
        "pool_bytes": POOL, "extent_bytes": EXTENT, "max_pages": MAX_PAGES, "zlib_level": 1,
        "n_pages": N_PAGES, "opp_frac": OPP_FRAC, "n_seeds": n_seeds,
        "snapshot_every_ops": SNAP_EVERY, "configs": [c["label"] for c in CONFIGS],
        "q": 0.99, "b_total": B_TOTAL,
        "n_pages_deviation": "14336 instead of DESIGN 4.2 sketch 20480: measured "
                             "TEXT ~2217 B/page would saturate the 48 MiB pool "
                             "in phase 0 (see traces.crash docstring)",
        "ocp_sensitivity_formula": "modeled_ns(window) = (delta WRITES + delta READS "
                                   "+ delta PROMOTES) * ocp_ns for ocp_ns in "
                                   f"{OCP_NS}; zlib emulation cost is the tier's "
                                   "measured COMP_NS_TOTAL+DECOMP_NS_TOTAL and is "
                                   "excluded from the modeled series (factored out "
                                   "per the E0 calibration argument)",
        "r3_reclaim_formula": "(delta WRITES * 4096 - delta PHYS_USED) / "
                              "(delta R3 + delta R1) over 500-op phase-1 windows "
                              "with delta R3 > 0. CAVEAT: phase-1 writes are "
                              "REWRITES; each also frees the old in-pool copy "
                              "(TEXT, ~2.2 KB) unless it was already spilled, so "
                              "this estimate is biased UP by that freed-copy "
                              "volume. It is comparable across victim policies "
                              "(same bias); the clean measured per-spill metric "
                              "is spill_bytes_w_per_spill (SPILL_BYTES_W per "
                              "spill event)",
        "fast": bool(args.fast),
    }
    failures: list[str] = []
    runs, timeseries = [], []
    t0 = time.perf_counter_ns()
    for cfg in CONFIGS:
        for si in range(n_seeds):
            rec, ts = run_one(cfg, common.seed_for(si), failures)
            runs.append(rec)
            timeseries.append({"label": cfg["label"], "seed": rec["seed"], **ts})
            print(f"[e3] {cfg['label']}/seed{si} lie_lag="
                  f"{rec.get('detect', {}).get('lie_lag_ops')} "
                  f"viol={rec.get('final', {}).get('violations')}", flush=True)

    # modeled OCP sensitivity from measured windowed op counts
    ocp = []
    for ts in timeseries:
        w = np.array(ts["writes"]); r = np.array(ts["reads"]); p = np.array(ts["promotes"])
        dops = np.diff(np.concatenate([[0], w + r + p]))
        ocp.append({
            "label": ts["label"], "seed": ts["seed"], "op": ts["op"],
            "window_device_ops": dops.astype(int).tolist(),
            "modeled_window_ns": {str(n): (dops * n).astype(int).tolist()
                                  for n in OCP_NS},
            "modeled_total_ns": {str(n): int((w[-1] + r[-1] + p[-1]) * n)
                                 for n in OCP_NS} if len(w) else {},
        })

    data = {"runs": runs, "timeseries": timeseries, "ocp_sensitivity": ocp}
    wall = (time.perf_counter_ns() - t0) / 1e9
    common.write_result(
        out_path, "E3", "compressibility crash: detection, ladder, stalls",
        config, seeds, data,
        modeled_fields=["data.ocp_sensitivity",
                        "data.runs[*].r3_reclaim_windowed",
                        "data.runs[*].r3_reclaim_windowed_mean"],
        failures=failures, wall_seconds=wall)
    print(f"wrote {out_path} ({len(runs)} runs, {wall:.1f}s, "
          f"{len(failures)} failure(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
