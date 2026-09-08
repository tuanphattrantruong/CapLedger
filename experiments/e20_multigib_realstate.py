#!/usr/bin/env python3
"""E20: multi-GiB replay of a real SQLite encryption-at-rest transition.

The captured SQLite B-tree page sequence is tiled, preserving every captured
byte and its page ordering but not claiming that replicas are independent. The
purpose is to exercise the implementation and its operation/byte accounting at
a multi-GiB physical-pool scale; E7 remains the distributional study.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

import common
import ctier_ffi as ffi
import real_state as R

POOL = 4 << 30
LOGICAL_PAGES = 1_200_000
EXTENT = 4 << 20


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "experiments/results/e20_multigib_realstate.json")
    spill = common.spill_path()
    with tempfile.TemporaryDirectory(prefix="e20-sqlite-") as td:
        cap = R.sqlite_pages(Path(td), limit=6000)
        if cap["cipher"] != "aes-128-ctr":
            raise RuntimeError(f"E20 requires AES-128-CTR, got {cap['cipher']}")
        before, after = cap["sqlite_baseline"], cap["sqlite_encrypted"]
        nsrc = min(len(before), len(after))
        digest_before = hashlib.sha256(before[:nsrc].tobytes()).hexdigest()
        digest_after = hashlib.sha256(after[:nsrc].tobytes()).hexdigest()
        tier = ffi.Tier(POOL, LOGICAL_PAGES + 1024, str(spill),
                        codec=ffi.CODEC_LZ4, zlib_level=1,
                        extent_bytes=EXTENT, ring_capacity=4096)
        try:
            c = ffi.Contract(tier, ffi.EST_ANALYTIC_RING, .99, ntenants=1,
                             victim_policy=ffi.VICTIM_CLOCK_ONLY)
            try:
                t0 = time.perf_counter()
                wrote = 0
                for base in range(0, LOGICAL_PAGES, 4096):
                    n = min(4096, LOGICAL_PAGES - base)
                    if c.admit(0, ffi.CONTRACT_GUAR, n) != 0:
                        # Keep the admission gate binding rather than bypass it.
                        for j in range(n):
                            if c.admit(0, ffi.CONTRACT_GUAR, 1) != 0:
                                break
                            if c.write(0, ffi.CONTRACT_GUAR, base + j,
                                       before[(base + j) % nsrc]) != 0:
                                break
                            wrote += 1
                    else:
                        for j in range(n):
                            if c.write(0, ffi.CONTRACT_GUAR, base + j,
                                       before[(base + j) % nsrc]) != 0:
                                break
                            wrote += 1
                load_s = time.perf_counter() - t0
                phys_before = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
                v0 = c.stat_u64(-1, ffi.CSTAT_VIOLATIONS)
                tier.cusum_reset()
                lie_op = cusum_op = None
                t0 = time.perf_counter()
                rewritten = 0
                for i in range(wrote):
                    if c.write(0, ffi.CONTRACT_GUAR, i, after[i % nsrc]) != 0:
                        break
                    rewritten += 1
                    if lie_op is None and c.stat_u64(-1, ffi.CSTAT_LIE_FLAG):
                        lie_op = i
                    if cusum_op is None and tier.cusum()[1]:
                        cusum_op = i
                rewrite_s = time.perf_counter() - t0
                phys_after = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
                result = {
                    "experiment": "E20_multigib_captured_real_state",
                    "transition": "real SQLite B-tree values rewritten with AES-CTR",
                    "capture_cipher": cap["cipher"],
                    "pool_bytes": POOL, "pool_gib": POOL / (1 << 30),
                    "logical_pages_requested": LOGICAL_PAGES,
                    "logical_state_gib": LOGICAL_PAGES * 4096 / (1 << 30),
                    "captured_source_pages": nsrc,
                    "captured_plain_sha256": digest_before,
                    "captured_encrypted_sha256": digest_after,
                    "scaling_method": "deterministic tiling of the captured page sequence; replicas are not independent",
                    "codec": "lz4", "q": .99,
                    "pages_loaded": wrote, "pages_rewritten": rewritten,
                    "load_seconds": load_s, "rewrite_seconds": rewrite_s,
                    "load_us_per_page": load_s / max(1, wrote) * 1e6,
                    "rewrite_us_per_page": rewrite_s / max(1, rewritten) * 1e6,
                    "phys_before": phys_before, "phys_after": phys_after,
                    "utilization_before": phys_before / POOL,
                    "utilization_after": phys_after / POOL,
                    "cusum_op": cusum_op, "ratio_lie_op": lie_op,
                    "violations": c.stat_u64(-1, ffi.CSTAT_VIOLATIONS) - v0,
                    "hard_failures": c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES),
                    "attribution_breaches": c.stat_u64(-1, ffi.CSTAT_INVARIANT_BREACHES),
                    "invariant": c.check_invariant(),
                    "scope": "single-host scale validation of implementation and accounting, not additional independent workload diversity",
                }
            finally:
                c.close()
        finally:
            tier.close()
            Path(spill).unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["invariant"] == 0 and result["hard_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
