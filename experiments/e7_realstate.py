#!/usr/bin/env python3
"""E7 — real application state: is the capacity lie a real phenomenon?

Everything measured so far used content generators. The paper's premise -- that
a tenant's compressibility collapses when real software does ordinary things --
has been argued from first principles and never measured. E7 measures it, on
state produced by running real programs on the measurement machine:

  redis    a real Redis 7.4 server under a YCSB-style Zipfian load, captured
           as the RDB image it writes
  sqlite   real B-tree pages of a SQLite database under the same load
  kvheap   the live in-process arena of a slab-allocated KV store
  git      a real git object store built from this project's own source
  tensor   real float32 embeddings from an SVD of dictionary co-occurrence
  macho    real system dynamic-library pages

and four real state transitions, each performed by the software itself rather
than simulated:

  encryption at rest   values re-encrypted (AES-CTR) in Redis, SQLite, and the
                       KV arena
  compaction           SQLite VACUUM
  object packing       git gc --aggressive (delta compression)
  re-quantisation      float32 -> int8 affine quantisation

Part 1 reports the compressibility distribution of every source under three
codecs (zlib, lz4, zstd) -- so no distributional claim in the paper rests on a
single compressor -- and the effective-capacity multiplier each implies.

Part 2 replays each transition THROUGH THE CONTRACT on a real tier: the
before-state is admitted and written, the after-state is written in place with
no logical growth, and we record whether the pool overruns, whether the
detector fires, and what the ladder has to do. This is the capacity lie,
end to end, with real bytes.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import common
import ctier_ffi as ffi
import real_state as R

PAGE = 4096
LIMIT = 12000                  # pages captured per source
CODECS = [("zlib", ffi.CODEC_ZLIB, 1),
          ("lz4", ffi.CODEC_LZ4, 1),
          ("zstd", ffi.CODEC_ZSTD, 1)]

# (label, before-state, after-state) — the real transitions.
TRANSITIONS = [
    ("redis_encrypt_at_rest", "redis_plain", "redis_encrypted"),
    ("sqlite_encrypt_at_rest", "sqlite_baseline", "sqlite_encrypted"),
    ("sqlite_vacuum", "sqlite_baseline", "sqlite_vacuumed"),
    ("kvheap_encrypt_at_rest", "kvheap_plain", "kvheap_encrypted"),
    ("git_gc_pack", "git_loose", "git_packed"),
    ("tensor_quantize_int8", "tensor_fp32", "tensor_int8"),
]

CLASS_BYTES = 128              # the tier's size-class granularity


def class_round(n: int) -> int:
    if n == 0:
        return 0
    if n >= PAGE:
        return PAGE
    return min(PAGE, ((n - 1) // CLASS_BYTES + 1) * CLASS_BYTES)


def census(pages: np.ndarray, codec_id: int, level: int) -> dict:
    """Compressed-size distribution of real pages under one codec, measured by
    the tier itself so the numbers are exactly what the contract would see
    (class rounding, raw-page fallback, zero-page handling included)."""
    n = int(pages.shape[0])
    spill = common.spill_path()
    tier = ffi.Tier(max(64 << 20, n * PAGE * 2), n + 64, str(spill),
                    codec=codec_id, zlib_level=level, extent_bytes=(1 << 20),
                    ring_capacity=max(4096, n))
    try:
        sizes = np.empty(n, dtype=np.int64)
        t0 = time.perf_counter_ns()
        for i in range(n):
            w = tier.write(i, pages[i])
            if w < 0:
                sizes = sizes[:i]
                break
            sizes[i] = w
        dt = time.perf_counter_ns() - t0
        comp_ns = tier.stat_u64(ffi.CTIER_STAT_COMP_NS_TOTAL)
        s = sizes.astype(np.float64)
        return {
            "pages": int(s.size),
            "mean_bytes": float(s.mean()),
            "std_bytes": float(s.std()),
            "cv": float(s.std() / s.mean()) if s.mean() > 0 else None,
            "p50": float(np.percentile(s, 50)), "p90": float(np.percentile(s, 90)),
            "p99": float(np.percentile(s, 99)), "max": float(s.max()),
            "ratio_mean": float(s.mean() / PAGE),
            "capacity_multiplier": float(PAGE / s.mean()) if s.mean() > 0 else None,
            "frac_incompressible": float((s >= PAGE).mean()),
            "comp_ns_per_page": comp_ns / max(1, int(s.size)),
            "wall_ns_per_page": dt / max(1, int(s.size)),
        }
    finally:
        tier.close()
        Path(spill).unlink(missing_ok=True)


def replay_transition(before: np.ndarray, after: np.ndarray, codec_id: int,
                      level: int, fill: float = 0.72) -> dict:
    """Admit and write the before-state through the contract, then rewrite
    every page in place with the after-state -- zero logical growth, which is
    exactly the capacity lie. The pool is sized to `fill` of the before-state's
    footprint so the run starts comfortably and the transition is what applies
    the pressure."""
    n = int(min(before.shape[0], after.shape[0]))
    before, after = before[:n], after[:n]
    # Size the pool from a measurement of the before-state footprint so the run
    # starts at `fill` utilisation and the TRANSITION is what applies pressure.
    # 256 KiB extents: the device dedicates whole extents to size classes, so a
    # pool this small with 1 MiB extents starves on extents, not on bytes.
    probe = census(before[: min(n, 2000)], codec_id, level)
    ext = 256 << 10
    pool = int(probe["mean_bytes"] * n / fill)
    pool = max(pool - pool % ext, 8 << 20)
    spill = common.spill_path()
    tier = ffi.Tier(pool, n + 1024, str(spill), codec=codec_id, zlib_level=level,
                    extent_bytes=ext, ring_capacity=4096)
    try:
        if tier.feedback_enable(8 * n + 4096) != 0:
            raise RuntimeError("failed to enable completion feedback")
        c = ffi.Contract(tier, ffi.EST_ANALYTIC_RING, 0.99, ntenants=1,
                         victim_policy=ffi.VICTIM_CLOCK_ONLY)
        try:
            for i in range(n):
                for _ in range(4):
                    if c.admit(0, ffi.CONTRACT_GUAR, 1) == 0:
                        break
                c.write(0, ffi.CONTRACT_GUAR, i, before[i])
            phys_before = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
            cq_before = c.capacity_pages(0, ffi.CONTRACT_GUAR)
            v0 = c.stat_u64(-1, ffi.CSTAT_VIOLATIONS)
            r1_0 = c.stat_u64(-1, ffi.CSTAT_R1_SPILLS)
            tier.cusum_reset()

            lie_op = cusum_op = None
            for i in range(n):
                c.write(0, ffi.CONTRACT_GUAR, i, after[i])
                if lie_op is None and c.stat_u64(-1, ffi.CSTAT_LIE_FLAG):
                    lie_op = i
                if cusum_op is None and tier.cusum()[1]:
                    cusum_op = i
            phys_after = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)

            # E12's protocol path is optional in older result sets. Drain it
            # here so every real-byte transition also checks that integrated
            # device writes produce an ordered, loss-free completion stream.
            feedback = tier.feedback_poll_all()
            feedback_gaps = sum(
                max(0, int(b.sequence) - int(a.sequence) - 1)
                for a, b in zip(feedback, feedback[1:])
            )

            # What the transition costs the TENANT, not the pool. After the
            # ladder has absorbed the inflation, some of this tenant's working
            # set lives in the far tier; touching it faults it back. Sample the
            # working set uniformly and measure how much of it moved and what
            # re-reading costs, which is the application-visible consequence of
            # everything above.
            spilled_now = int(tier.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED))
            rng = np.random.default_rng(0xC0FFEE)
            sample = rng.choice(n, size=min(n, 2000), replace=False)
            p0 = tier.stat_u64(ffi.CTIER_STAT_PROMOTES)
            b0 = tier.stat_u64(ffi.CTIER_STAT_SPILL_BYTES_R)
            t_read0 = time.perf_counter_ns()
            for lpn in sample:
                c.read(int(lpn))
            read_ns = time.perf_counter_ns() - t_read0
            promotes = int(tier.stat_u64(ffi.CTIER_STAT_PROMOTES) - p0)
            read_bytes = int(tier.stat_u64(ffi.CTIER_STAT_SPILL_BYTES_R) - b0)
            return {
                "resident_frac_after": 1.0 - spilled_now / n,
                "sample_pages": int(sample.size),
                "sample_promote_frac": promotes / max(1, int(sample.size)),
                "sample_promote_bytes": read_bytes,
                "sample_read_ns_per_page": read_ns / max(1, int(sample.size)),
                "pages": n, "pool_bytes": pool,
                "phys_before": int(phys_before), "phys_after": int(phys_after),
                "inflation": (phys_after / phys_before) if phys_before else None,
                "u_before": phys_before / pool, "u_after": phys_after / pool,
                "cq_before": int(cq_before),
                "cq_after": int(c.capacity_pages(0, ffi.CONTRACT_GUAR)),
                "lie_flag_op": lie_op, "cusum_op": cusum_op,
                "violations": int(c.stat_u64(-1, ffi.CSTAT_VIOLATIONS) - v0),
                "r1_spills": int(c.stat_u64(-1, ffi.CSTAT_R1_SPILLS) - r1_0),
                "hard_failures": int(c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES)),
                "invariant": int(c.check_invariant()),
                "overdraft_bytes": float(c.stat_f64(0, ffi.CSTAT_OVERDRAFT_BYTES)),
                "feedback_records": len(feedback),
                "feedback_dropped": tier.feedback_dropped(),
                "feedback_sequence_gaps": feedback_gaps,
                "feedback_enospc": sum(int(r.status) == 3 for r in feedback),
            }
        finally:
            c.close()
    finally:
        tier.close()
        Path(spill).unlink(missing_ok=True)


def main() -> int:
    ap = common.make_argparser("E7 real application state and real drift")
    args = ap.parse_args()
    limit = 2000 if args.fast else LIMIT
    out_path = Path(args.out) / "e7_realstate.json"
    t0 = time.perf_counter_ns()
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="e7-") as td:
        tmp = Path(td)
        print("[e7] capturing real application state ...", flush=True)
        pages, notes = R.collect_all(tmp, limit=limit)
        for k, v in pages.items():
            print(f"[e7]   {k:24s} {v.shape[0]} pages", flush=True)

        # ---- part 1: compressibility census, three codecs ----
        cen: dict[str, dict] = {}
        for name, arr in pages.items():
            cen[name] = {}
            for cname, cid, lvl in CODECS:
                cen[name][cname] = census(arr, cid, lvl)
                r = cen[name][cname]
                print(f"[e7] census {name:24s} {cname:5s} "
                      f"ratio={r['ratio_mean']:.3f} cv={r['cv']:.2f} "
                      f"x{r['capacity_multiplier']:.2f}", flush=True)

        # ---- part 2: replay each real transition through the contract ----
        trans = []
        for label, a, b in TRANSITIONS:
            if a not in pages or b not in pages:
                notes[label] = "skipped (source unavailable)"
                continue
            for cname, cid, lvl in CODECS:
                r = replay_transition(pages[a], pages[b], cid, lvl)
                r.update({"transition": label, "codec": cname,
                          "before": a, "after": b,
                          "ratio_before": cen[a][cname]["ratio_mean"],
                          "ratio_after": cen[b][cname]["ratio_mean"]})
                trans.append(r)
                print(f"[e7] replay {label:24s} {cname:5s} "
                      f"inflation={r['inflation']:.2f}x u:{r['u_before']:.2f}->"
                      f"{r['u_after']:.2f} lie@{r['lie_flag_op']} "
                      f"cusum@{r['cusum_op']} viol={r['violations']} "
                      f"hard={r['hard_failures']}", flush=True)
                if r["invariant"] != 0:
                    failures.append(f"E7 invariant broke: {label}/{cname}")

    config = {
        "pages_per_source": limit, "codecs": [c[0] for c in CODECS],
        "codec_levels": {c[0]: c[2] for c in CODECS},
        "sources": list(pages.keys()),
        "transitions": [t[0] for t in TRANSITIONS],
        "provenance": notes,
        "capture_policy": "state is produced by running real software on the "
                          "measurement machine (Redis 7.4.2 built from source, "
                          "SQLite via the Python stdlib, git, numpy) or read "
                          "from system-shipped read-only assets; no file "
                          "belonging to the user is read",
        "workload": "YCSB-style: 20 K keys, 1 KiB records built from the BSD "
                    "dictionary, Zipfian (theta=0.99) update phase over half "
                    "the key space",
        "replay_fill": 0.72,
        "replay_def": "admit+write the before-state, then rewrite every page in "
                      "place with the after-state (zero logical growth) — the "
                      "capacity lie, with real bytes",
        "fast": bool(args.fast),
    }
    data = {"census": cen, "transitions": trans,
            "wall_s": (time.perf_counter_ns() - t0) / 1e9}
    common.write_result(
        out_path, "e7_realstate",
        "Real application state: measured compressibility and measured drift",
        config, [1, 3, 5, 7], data, modeled_fields=[], failures=failures,
        wall_seconds=data["wall_s"])
    print(f"[e7] wrote {out_path} ({len(failures)} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
