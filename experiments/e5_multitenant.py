#!/usr/bin/env python3
"""E5 — multi-tenant isolation under an entropy bomb.

Multitenant trace (4 tenants, 80k ops; t3 flips TEXT -> RANDOM rewrites of its
whole working set at op 30000), 96 MiB pool, configs {analytic q=0.99,
analytic-ring q=0.99, optimistic, zswap-reactive, reactive-tenant,
transparent-reserve} x 20 seeds, ntenants=4. Contract arms request
DENSITY_AGE victims; the zswap personality overrides this with GLOBAL_LRU.

Each tenant admits its guaranteed working set upfront (retry smaller on
reject; granted is recorded — under the pessimistic cold-start prior the
gate is deliberately conservative and grants only part of the demand), then
tops up its reservation every 512 ops during replay as the estimator warms
and consumed reservations free budget (guaranteed writes of NEW lpns require
an open reservation unit — the real gate returns EINVAL otherwise — while
rewrites of owned pages do not; fill writes that race ahead of their top-up
fail and are recorded). Every tenant registers a shed callback
that frees its 512 newest OPP pages (recency tracked in Python via replay's
live map; the callback calls contract_free — same-thread reentrancy is
supported by the recursive contract mutex). The admission invariant is
sampled every 1000 ops.

Isolation claim: violations concentrate on the bomber (t3) under the analytic
contract; INVARIANT_BREACHES == 0 and all invariant samples pass.
"""
from __future__ import annotations

import sys
import time
from itertools import islice
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import traces as T

POOL = 96 * 1024 * 1024
# Extent size sets how many extents the pool has, and the device dedicates
# whole extents to size classes. Too few and it starves on EXTENTS while
# its bytes are free, which moves recovery from a watermark the contract
# controls onto device exhaustion (E11 measures that boundary).
EXTENT = 1 << 20
MAX_PAGES = 131072
N_SEEDS = 20
NTENANTS = 4
SHED_PAGES = 512
INVARIANT_EVERY = 1000
TOPUP_EVERY = 512

CONFIGS = [
    {"label": "analytic_q0.99", "estimator": ffi.EST_ANALYTIC},
    {"label": "analytic_ring_q0.99", "estimator": ffi.EST_ANALYTIC_RING},
    {"label": "optimistic", "estimator": ffi.EST_OPTIMISTIC},
    # External baselines, not knobs of our own gate. ZSWAP is the software-tier
    # lineage (exact accounting, no admission gate, reactive global-LRU
    # reclaim); TRANSPARENT is the hardware lineage (device-held reserve, no OS
    # spill path, exhaustion is a machine check charged to whoever writes next).
    {"label": "zswap_reactive", "estimator": ffi.EST_ZSWAP},
    # Stronger control: exact size accounting plus tenant-aware reactive
    # reclaim, but no probabilistic admission or purchased reservation.
    {"label": "reactive_tenant", "estimator": ffi.EST_REACTIVE_TENANT},
    {"label": "transparent_reserve", "estimator": ffi.EST_TRANSPARENT},
]

PER_TENANT_U64 = {
    "violations": ffi.CSTAT_VIOLATIONS,
    "r3_forced_spills": ffi.CSTAT_R3_FORCED_SPILLS,
    "opp_drops": ffi.CSTAT_OPP_DROPS,
    "written_guar": ffi.CSTAT_WRITTEN_GUAR,
    "written_opp": ffi.CSTAT_WRITTEN_OPP,
    # per-tenant hard failures: the transparent-reserve baseline's whole result
    # is WHICH tenant the machine check lands on
    "hard_failures": ffi.CSTAT_HARD_FAILURES,
}
GLOBAL_U64 = {
    "violations": ffi.CSTAT_VIOLATIONS,
    "r1_spills": ffi.CSTAT_R1_SPILLS,
    "r2_shed_calls": ffi.CSTAT_R2_SHED_CALLS,
    "r2_shed_bytes": ffi.CSTAT_R2_SHED_BYTES,
    "r3_forced_spills": ffi.CSTAT_R3_FORCED_SPILLS,
    "opp_drops": ffi.CSTAT_OPP_DROPS,
    "hard_failures": ffi.CSTAT_HARD_FAILURES,
    "invariant_breaches": ffi.CSTAT_INVARIANT_BREACHES,
    "resv_bytes": ffi.CSTAT_RESV_BYTES,
}


def run_one(cfg: dict, seed: int, failures: list[str]) -> dict:
    trace = T.multitenant(seed=seed)
    tinfo = trace["attrs"]["tenants"]
    rec: dict = {"label": cfg["label"], "seed": seed}
    with common.temp_spill() as sp:
        tier = ffi.Tier(POOL, MAX_PAGES, str(sp), zlib_level=1,
                        extent_bytes=EXTENT)
        contract = None
        try:
            contract = ffi.Contract(tier, cfg["estimator"], 0.99,
                                    headroom_frac=0.05, static_ratio=2.0,
                                    epoch_ops=1024, ntenants=NTENANTS,
                                    victim_policy=ffi.VICTIM_DENSITY_AGE)
            # ---- upfront GUAR admission per tenant (retry smaller on reject)
            admissions = []
            for t in range(NTENANTS):
                requested = tinfo[t]["guar_pages"]
                n = requested
                granted = 0
                attempts = 0
                while n > 0 and attempts < 40:
                    attempts += 1
                    if contract.admit(t, ffi.CONTRACT_GUAR, n) == 0:
                        granted = n
                        break
                    cap = contract.capacity_pages(t, ffi.CONTRACT_GUAR)
                    n = min(n - 1, cap) if cap > 0 else n // 2
                contract.admit(t, ffi.CONTRACT_OPP, tinfo[t]["opp_pages"])
                admissions.append({"tenant": t, "requested_guar": requested,
                                   "granted_guar_upfront": granted,
                                   "granted_guar": granted,
                                   "topups": 0,
                                   "attempts": attempts})
            rec["admissions"] = admissions

            # ---- shed callbacks: free 512 recently resident OPP pages.
            # R1 already spills the coldest pages, so asking the application to
            # delete those same pages can free logical objects yet release zero
            # additional pool bytes.  Choosing the newest entries makes R2 a
            # real byte-reclamation action rather than a callback-count proxy.
            live: dict = {}
            shed_log = {t: {"calls": 0, "freed": 0} for t in range(NTENANTS)}

            def make_shed(t: int):
                def shed(bytes_needed: int) -> None:
                    shed_log[t]["calls"] += 1
                    d = live.get((t, ffi.CONTRACT_OPP))
                    if not d:
                        return
                    victims = list(islice(reversed(d), SHED_PAGES))
                    for lpn in victims:
                        if contract.free(t, lpn) == 0:
                            shed_log[t]["freed"] += 1
                        d.pop(lpn, None)
                return shed

            for t in range(NTENANTS):
                rc = contract.register_shed(t, make_shed(t))
                if rc != 0:
                    failures.append(f"{cfg['label']}/seed{seed}: "
                                    f"register_shed(t{t}) rc={rc}")

            # ---- replay with invariant sampling and reservation top-ups
            inv = {"samples": 0, "failed": 0, "first_failed_op": None}

            def snapshot(i: int) -> None:
                if (i + 1) % TOPUP_EVERY == 0:
                    for t in range(NTENANTS):
                        a = admissions[t]
                        deficit = a["requested_guar"] - a["granted_guar"]
                        if deficit <= 0:
                            continue
                        cap = contract.capacity_pages(t, ffi.CONTRACT_GUAR)
                        n = min(deficit, cap)
                        if n > 0 and contract.admit(t, ffi.CONTRACT_GUAR, n) == 0:
                            a["granted_guar"] += n
                            a["topups"] += 1
                if (i + 1) % INVARIANT_EVERY == 0:
                    inv["samples"] += 1
                    if contract.check_invariant() != 0:
                        inv["failed"] += 1
                        if inv["first_failed_op"] is None:
                            inv["first_failed_op"] = i

            res = T.replay(trace, contract, timing=False, snapshot_every=1,
                           snapshot_fn=snapshot, live=live)
            rec["replay_counts"] = {
                k: ({str(kk): vv for kk, vv in v.items()} if isinstance(v, dict) else v)
                for k, v in res.items()}
            rec["shed"] = {str(t): shed_log[t] for t in range(NTENANTS)}

            per_tenant = []
            for t in range(NTENANTS):
                row = {"tenant": t, "content": tinfo[t]["content"],
                       "requested_guar": admissions[t]["requested_guar"],
                       "granted_guar": admissions[t]["granted_guar"]}
                for name, sid in PER_TENANT_U64.items():
                    row[name] = contract.stat_u64(t, sid)
                row["overdraft_bytes"] = contract.stat_f64(t, ffi.CSTAT_OVERDRAFT_BYTES)
                per_tenant.append(row)
            rec["per_tenant"] = per_tenant
            rec["global"] = {name: contract.stat_u64(-1, sid)
                             for name, sid in GLOBAL_U64.items()}
            rec["invariant_samples"] = inv
            rec["final_phys_used"] = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
            rec["pages_spilled"] = tier.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED)

            bomber_v = per_tenant[3]["violations"]
            victim_v = max(per_tenant[t]["violations"] for t in range(3))
            rec["isolation"] = {
                "bomber_violations": bomber_v,
                "max_victim_violations": victim_v,
                "total_victim_violations": sum(
                    per_tenant[t]["violations"] for t in range(3)),
                "hard_failures": rec["global"]["hard_failures"],
                "invariant_breaches": rec["global"]["invariant_breaches"],
            }
            # The transparent-reserve baseline has no spill path, so its
            # failure mode is hard failures rather than violations; record who
            # took them, since "whoever writes next" is exactly the point.
            rec["isolation"]["hard_failures_by_tenant"] = [
                per_tenant[t].get("hard_failures", 0) for t in range(4)]
            if rec["global"]["invariant_breaches"] != 0 or inv["failed"] != 0:
                # The transparent-reserve baseline is EXPECTED to breach: it has
                # no attribution mechanism at all. Recording that as a suite
                # failure would make the comparison unrunnable.
                if cfg["label"] != "transparent_reserve":
                    failures.append(
                        f"{cfg['label']}/seed{seed}: invariant breach "
                        f"(counter={rec['global']['invariant_breaches']}, "
                        f"samples_failed={inv['failed']})")
            return rec
        finally:
            if contract is not None:
                contract.close()
            tier.close()


def main() -> int:
    ap = common.make_argparser("E5 multi-tenant entropy-bomb isolation")
    args = ap.parse_args()
    n_seeds = common.fast_trials(N_SEEDS, args.fast)
    seeds = [common.seed_for(i) for i in range(n_seeds)]
    out_path = Path(args.out) / "e5_multitenant.json"
    trace0 = T.multitenant(seed=seeds[0])
    config = {
        "pool_bytes": POOL, "extent_bytes": EXTENT, "max_pages": MAX_PAGES, "zlib_level": 1,
        "n_seeds": n_seeds, "ntenants": NTENANTS, "q": 0.99,
        "victim_policy": "DENSITY_AGE", "shed_pages_per_call": SHED_PAGES,
        "invariant_sample_every_ops": INVARIANT_EVERY,
        "reservation_topup_every_ops": TOPUP_EVERY,
        "configs": [c["label"] for c in CONFIGS],
        "trace_params": trace0["attrs"]["params"],
        "sizing_note": "working-set sizes calibrated to measured zlib-1 "
                       "class-rounded sizes so that guaranteed bytes alone "
                       "exceed the 0.97*B_total L3 watermark after the bomb "
                       "(see workloads/traces.py multitenant comment)",
        "fast": bool(args.fast),
    }
    failures: list[str] = []
    runs = []
    t0 = time.perf_counter_ns()
    for cfg in CONFIGS:
        for si in range(n_seeds):
            runs.append(run_one(cfg, common.seed_for(si), failures))
            iso = runs[-1].get("isolation", {})
            print(f"[e5] {cfg['label']}/seed{si} bomber_v="
                  f"{iso.get('bomber_violations')} victim_v="
                  f"{iso.get('max_victim_violations')}", flush=True)

    summary = []
    for cfg in CONFIGS:
        rs = [r for r in runs if r["label"] == cfg["label"] and "isolation" in r]
        summary.append({
            "label": cfg["label"], "n_runs": len(rs),
            "bomber_violations_mean": float(np.mean(
                [r["isolation"]["bomber_violations"] for r in rs])) if rs else None,
            "max_victim_violations_mean": float(np.mean(
                [r["isolation"]["max_victim_violations"] for r in rs])) if rs else None,
            "bomber_violations_per_seed": [r["isolation"]["bomber_violations"] for r in rs],
            "max_victim_violations_per_seed": [r["isolation"]["max_victim_violations"] for r in rs],
            "total_victim_violations_mean": float(np.mean(
                [r["isolation"]["total_victim_violations"] for r in rs])) if rs else None,
            "hard_failures_total": int(sum(
                r["isolation"]["hard_failures"] for r in rs)),
            "hard_failures_by_tenant_total": [
                int(sum(r["isolation"]["hard_failures_by_tenant"][t] for r in rs))
                for t in range(4)] if rs else [],
            "invariant_breaches_total": int(sum(
                r["isolation"]["invariant_breaches"] for r in rs)),
        })
    breaches = sum(r.get("global", {}).get("invariant_breaches", 0) for r in runs)
    samples_failed = sum(r.get("invariant_samples", {}).get("failed", 0) for r in runs)
    data = {
        "runs": runs,
        "per_tenant": [r["per_tenant"] for r in runs if "per_tenant" in r],
        "isolation_summary": summary,
        "invariant": {"breaches": breaches, "samples_failed": samples_failed},
    }
    wall = (time.perf_counter_ns() - t0) / 1e9
    common.write_result(out_path, "E5", "multi-tenant entropy-bomb isolation",
                        config, seeds, data, modeled_fields=[],
                        failures=failures, wall_seconds=wall)
    print(f"wrote {out_path} ({len(runs)} runs, {wall:.1f}s, "
          f"{len(failures)} failure(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
