#!/usr/bin/env python3
"""E2 — utilization vs. safety frontier.

Steady-mixed content with mild drift (RANDOM share 0.1 -> 0.3), 30k ops,
64 MiB pool, 20 seeds x 17 configs:
  ANALYTIC q in {0.5, 0.8, 0.9, 0.95, 0.99}
  STATIC ratio in {1.5, 2.0, 2.5, 3.0}
  ANALYTIC_RING q in {0.5, 0.9, 0.99}
  OPTIMISTIC, PESSIMISTIC
  ZSWAP_REACTIVE, REACTIVE_TENANT, TRANSPARENT_RESERVE

Driver (advertisement-honoring client): after a 256-page warm, read the
advertised guaranteed capacity ONCE, admit exactly that many GUAR pages, and
write them; GUAR demand beyond the advertisement is by definition rejected,
so the remaining ops fill OPP (OPP writes may EAGAIN = drops, not
violations). A continuously re-querying client was tried first and erases
all differentiation: as recovery spills drain the pool, every estimator
re-admits the freed bytes, so all configs converge to the same terminal
state — the advertisement is the contract, and the frontier measures it.

Metrics per run:
  U      = peak (WRITTEN_GUAR + WRITTEN_OPP) * 4096 / pool_bytes
           (counters are monotone, so peak == final)
  U_guar = WRITTEN_GUAR * 4096 / pool_bytes (guaranteed utilization — the
           frontier axis that distinguishes estimators)
  V      = R3_FORCED_SPILLS per 10k GUAR writes
plus OPP_DROPS, hard failures, and a residency-based peak.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

POOL = 64 * 1024 * 1024
MAX_PAGES = 65536
N_OPS = 30000
N_SEEDS = 20
WARM = 256
BATCH = 256

CONFIGS = (
    [{"label": f"analytic_q{q}", "estimator": "analytic", "q": q} for q in
     (0.5, 0.8, 0.9, 0.95, 0.99)]
    + [{"label": f"static_{r}", "estimator": "static", "ratio": r} for r in
       (1.5, 2.0, 2.5, 3.0)]
    + [{"label": f"analytic_ring_q{q}", "estimator": "analytic_ring", "q": q}
       for q in (0.5, 0.9, 0.99)]
    + [{"label": "optimistic", "estimator": "optimistic"},
       {"label": "pessimistic", "estimator": "pessimistic"},
       # External baselines (see e5_multitenant.py CONFIGS for what they model).
       {"label": "zswap_reactive", "estimator": "zswap"},
       {"label": "reactive_tenant", "estimator": "reactive_tenant"},
       {"label": "transparent_reserve", "estimator": "transparent"}]
)

_EST_IDS = {"analytic": ffi.EST_ANALYTIC, "static": ffi.EST_STATIC,
            "optimistic": ffi.EST_OPTIMISTIC, "pessimistic": ffi.EST_PESSIMISTIC,
            "analytic_ring": ffi.EST_ANALYTIC_RING, "zswap": ffi.EST_ZSWAP,
            "reactive_tenant": ffi.EST_REACTIVE_TENANT,
            "transparent": ffi.EST_TRANSPARENT}


def run_one(cfg: dict, seed: int, failures: list[str]) -> dict:
    contents = T.drift_content(N_OPS + WARM, seed, r0=0.1, r1=0.3,
                               ramp_ops=N_OPS)
    rec = {"label": cfg["label"], "seed": seed}
    with common.temp_spill() as sp:
        tier = ffi.Tier(POOL, MAX_PAGES, str(sp), zlib_level=1)
        contract = None
        try:
            contract = ffi.Contract(
                tier, _EST_IDS[cfg["estimator"]], cfg.get("q", 0.99),
                headroom_frac=0.05, static_ratio=cfg.get("ratio", 2.0),
                epoch_ops=1024, ntenants=1,
                victim_policy=ffi.VICTIM_DENSITY_AGE)
            lpn = 0
            ops = 0
            peak_present = 0

            def write_page(cls: int) -> int:
                nonlocal lpn, ops, peak_present
                page = T.page_bytes(int(contents[ops]), lpn, seed, 0)
                rc = contract.write(0, cls, lpn, page)
                if rc == 0:
                    lpn += 1
                ops += 1
                if ops % 64 == 0:
                    pres = tier.stat_u64(ffi.CTIER_STAT_PAGES_PRESENT)
                    if pres > peak_present:
                        peak_present = pres
                return rc

            # warm phase (cold-start pitfall; counted toward the 30k ops)
            rc = contract.admit(0, ffi.CONTRACT_GUAR, WARM)
            if rc != 0:
                failures.append(f"{cfg['label']}/seed{seed}: warm admit rc={rc}")
                rec["aborted"] = "warm_admit"
                return rec
            for _ in range(WARM):
                write_page(ffi.CONTRACT_GUAR)

            # one-shot advertisement-honoring GUAR provisioning
            advertised = contract.capacity_pages(0, ffi.CONTRACT_GUAR)
            n_guar = max(0, min(int(advertised), N_OPS - ops))
            rec["advertised_guar_pages"] = int(advertised)
            admit_rc = contract.admit(0, ffi.CONTRACT_GUAR, n_guar) if n_guar else 0
            rec["guar_admit_rc"] = admit_rc
            if admit_rc != 0:
                failures.append(f"{cfg['label']}/seed{seed}: advertised "
                                f"{n_guar} GUAR pages not admittable (rc={admit_rc})")
                n_guar = 0
            for _ in range(n_guar):
                write_page(ffi.CONTRACT_GUAR)
            rec["guar_phase_end_op"] = ops
            rec["r3_at_guar_phase_end"] = contract.stat_u64(
                -1, ffi.CSTAT_R3_FORCED_SPILLS)

            # remaining demand is opportunistic
            while ops < N_OPS:
                batch = min(BATCH, N_OPS - ops)
                contract.admit(0, ffi.CONTRACT_OPP, batch)  # always accepted
                for _ in range(batch):
                    write_page(ffi.CONTRACT_OPP)

            written_guar = contract.stat_u64(-1, ffi.CSTAT_WRITTEN_GUAR)
            written_opp = contract.stat_u64(-1, ffi.CSTAT_WRITTEN_OPP)
            pres = tier.stat_u64(ffi.CTIER_STAT_PAGES_PRESENT)
            peak_present = max(peak_present, pres)
            r3 = contract.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS)
            rec.update({
                "ops": ops,
                "written_guar": written_guar,
                "written_opp": written_opp,
                "U": (written_guar + written_opp) * 4096 / POOL,
                "U_guar": written_guar * 4096 / POOL,
                "U_resident_peak": peak_present * 4096 / POOL,
                "V_r3_per_10k_guar": (r3 * 10000 / written_guar) if written_guar else None,
                "r3_forced_spills": r3,
                "violations": contract.stat_u64(-1, ffi.CSTAT_VIOLATIONS),
                "opp_drops": contract.stat_u64(-1, ffi.CSTAT_OPP_DROPS),
                "r1_spills": contract.stat_u64(-1, ffi.CSTAT_R1_SPILLS),
                "hard_failures": contract.stat_u64(-1, ffi.CSTAT_HARD_FAILURES),
                "final_phys_used": tier.stat_u64(ffi.CTIER_STAT_PHYS_USED),
                "pages_spilled": tier.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED),
            })
            return rec
        finally:
            if contract is not None:
                contract.close()
            tier.close()


def main() -> int:
    ap = common.make_argparser("E2 utilization vs violation frontier")
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    seeds = [common.seed_for(i) for i in range(n_seeds)]
    out_path = Path(args.out) / "e2_frontier.json"
    config = {
        "pool_bytes": POOL, "max_pages": MAX_PAGES, "zlib_level": 1,
        "n_ops": N_OPS, "warm_pages": WARM, "admit_batch": BATCH,
        "n_seeds": n_seeds, "configs": CONFIGS,
        "content": "drift_content r0=0.1 r1=0.3 over 30k ops "
                   "(steady-mixed base, RANDOM share ramps)",
        "U_def": "peak (WRITTEN_GUAR+WRITTEN_OPP)*4096/pool_bytes "
                 "(counters monotone => peak==final)",
        "U_guar_def": "WRITTEN_GUAR*4096/pool_bytes (guaranteed utilization)",
        "V_def": "R3_FORCED_SPILLS * 10000 / WRITTEN_GUAR",
        "driver": "one-shot advertisement-honoring GUAR provisioning after "
                  "warm, then OPP fill (see module docstring for why a "
                  "continuously re-querying client erases differentiation)",
        "fast": bool(args.fast),
    }
    failures: list[str] = []
    runs: list[dict] = []
    t0 = time.perf_counter_ns()
    for cfg in CONFIGS:
        for si in range(n_seeds):
            runs.append(run_one(cfg, common.seed_for(si), failures))
            print(f"[e2] {cfg['label']}/seed{si} U={runs[-1].get('U')} "
                  f"V={runs[-1].get('V_r3_per_10k_guar')}", flush=True)

    frontier = []
    for cfg in CONFIGS:
        rs = [r for r in runs if r["label"] == cfg["label"] and "U" in r]
        entry = dict(cfg)
        entry.update({
            "n_runs": len(rs),
            "U_mean": float(np.mean([r["U"] for r in rs])) if rs else None,
            "U_per_seed": [r["U"] for r in rs],
            "U_guar_mean": float(np.mean([r["U_guar"] for r in rs])) if rs else None,
            "U_guar_per_seed": [r["U_guar"] for r in rs],
            "V_mean": (float(np.mean([r["V_r3_per_10k_guar"] for r in rs]))
                       if rs and all(r["V_r3_per_10k_guar"] is not None for r in rs)
                       else None),
            "V_per_seed": [r["V_r3_per_10k_guar"] for r in rs],
            "opp_drops_mean": float(np.mean([r["opp_drops"] for r in rs])) if rs else None,
            "violations_mean": float(np.mean([r["violations"] for r in rs])) if rs else None,
        })
        frontier.append(entry)

    wall = (time.perf_counter_ns() - t0) / 1e9
    common.write_result(out_path, "E2", "utilization vs safety frontier",
                        config, seeds, {"frontier": frontier, "runs": runs},
                        modeled_fields=[], failures=failures, wall_seconds=wall)
    print(f"wrote {out_path} ({len(runs)} runs, {wall:.1f}s, "
          f"{len(failures)} failure(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
