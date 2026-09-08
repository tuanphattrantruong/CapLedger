#!/usr/bin/env python3
"""E0 — zlib emulation calibration (wraps build/calib).

Runs the C calibration binary (single source of truth for the level -> ratio /
throughput table described in DESIGN.md), parses its stdout JSON, and wraps it with
run metadata into e0_calibration.json. The chosen default zlib level (1) is
recorded alongside the measured table.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import common
import ctier_ffi as ffi
import traces as T


CODEC_PAGES = 4000
CODECS = [("zlib", ffi.CODEC_ZLIB), ("lz4", ffi.CODEC_LZ4),
          ("zstd", ffi.CODEC_ZSTD)]


def codec_matrix(failures: list) -> dict:
    """Mean class-rounded compressed size per (content class, codec), measured
    through the tier so the numbers are exactly what the contract observes."""
    out: dict = {}
    for ci, cname in enumerate(T.CONTENT_NAMES):
        if cname == "MIXED":
            continue
        out[cname] = {}
        for kname, kid in CODECS:
            sp = common.spill_path()
            tier = ffi.Tier(256 << 20, CODEC_PAGES + 64, str(sp), codec=kid,
                            zlib_level=1, extent_bytes=(1 << 20),
                            ring_capacity=max(4096, CODEC_PAGES))
            try:
                t0 = time.perf_counter_ns()
                for i in range(CODEC_PAGES):
                    if tier.write(i, T.page_bytes(ci, i, seed=4242)) < 0:
                        failures.append(f"E0 codec matrix write failed "
                                        f"{cname}/{kname} at page {i}")
                        break
                dt = time.perf_counter_ns() - t0
                n, mu, sg = tier.ring_moments(0)
                comp_ns = tier.stat_u64(ffi.CTIER_STAT_COMP_NS_TOTAL)
                out[cname][kname] = {
                    "pages": n, "mean_bytes": mu, "std_bytes": sg,
                    "cv": (sg / mu) if mu > 0 else None,
                    "ratio_mean": mu / 4096.0,
                    "capacity_multiplier": (4096.0 / mu) if mu > 0 else None,
                    "comp_ns_per_page": comp_ns / max(1, n),
                    "wall_ns_per_page": dt / max(1, n),
                }
            finally:
                tier.close()
                Path(sp).unlink(missing_ok=True)
    return out


def main() -> int:
    ap = common.make_argparser("E0 zlib calibration (wraps build/calib)")
    args = ap.parse_args()
    out_path = Path(args.out) / "e0_calibration.json"
    calib = common.ROOT / "build" / "calib"
    config = {
        "binary": str(calib),
        "args": [],
        "chosen_default_level": 1,
        "codec_matrix_pages": CODEC_PAGES,
        "codecs": [c[0] for c in CODECS],
        "fast": bool(args.fast),
        "note": "build/calib is run with default arguments; its internal "
                "iteration counts are reported in its own JSON output",
    }
    t0 = time.perf_counter_ns()
    failures: list[str] = []
    data: dict = {}

    if not calib.exists():
        failures.append(f"calibration binary missing: {calib} (build with `make bench`)")
    else:
        try:
            r = subprocess.run([str(calib)], capture_output=True, text=True,
                               timeout=600)
            if r.returncode != 0:
                failures.append(
                    f"build/calib exited {r.returncode}; stderr: {r.stderr.strip()[:2000]}")
            else:
                try:
                    parsed = json.loads(r.stdout)
                    if isinstance(parsed, dict):
                        data.update(parsed)
                    else:
                        data["calib"] = parsed
                except json.JSONDecodeError as e:
                    failures.append(
                        f"build/calib stdout was not valid JSON ({e}); "
                        f"first 500 chars: {r.stdout[:500]!r}")
        except subprocess.TimeoutExpired:
            failures.append("build/calib timed out after 600 s")
        except OSError as e:
            failures.append(f"failed to run build/calib: {e}")

    # --- codec x content-class matrix -------------------------------------
    # The C calibration binary is zlib-only. Every distributional claim in the
    # paper is conditioned on the compressor, so the same seven content classes
    # are measured through the tier under all three codecs. (E7 does this on
    # real application state; this is the synthetic-class counterpart, and the
    # two should agree on the ordering.)
    data["codec_matrix"] = codec_matrix(failures)

    data["chosen_default_level"] = 1
    wall = (time.perf_counter_ns() - t0) / 1e9
    common.write_result(out_path, "E0",
                        "Emulation calibration: zlib level sweep and the "
                        "codec x content-class matrix",
                        config, seeds=[], data=data, modeled_fields=[],
                        failures=failures, wall_seconds=wall)
    print(f"wrote {out_path}" + (f" with {len(failures)} failure(s)" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
