#!/usr/bin/env python3
"""E14 - dependence-aware calibration on ordered real and grouped traces.

This experiment deliberately does not free and reshuffle pages into iid
batches.  It retains application page order, estimates effective sample size,
and compares the paper's iid normal reserve with two dependence-aware prices:
an autocorrelation-inflated normal reserve and a moving-block bootstrap.
"""
from __future__ import annotations

import math
import tempfile
import time
import zlib
from pathlib import Path
from statistics import NormalDist

import numpy as np

import common
import real_state as R

PAGE = 4096
CLASS = 128
Q = 0.99
Z = NormalDist().inv_cdf(Q)
WINDOW = 1024
BATCHES = (64, 256)
BOOT_K = 400


def compressed_sizes(pages: np.ndarray) -> np.ndarray:
    out = np.empty(len(pages), dtype=np.int64)
    for i, page in enumerate(pages):
        raw = memoryview(page).tobytes()
        if not any(raw):
            out[i] = 0
            continue
        n = len(zlib.compress(raw, 1))
        out[i] = PAGE if n >= PAGE else ((n + CLASS - 1) // CLASS) * CLASS
    return out


def dependence(window: np.ndarray, max_lag: int = 64) -> tuple[float, float, int]:
    """Return (integrated autocorrelation time, effective N, block length).

    The positive-sequence truncation is conservative for the bursty streams
    evaluated here and avoids cancelling positive short-range dependence with
    noisy negative long-range estimates.
    """
    x = np.asarray(window, dtype=np.float64)
    x = x - x.mean()
    den = float(np.dot(x, x))
    if den <= 0:
        return 1.0, float(len(x)), 1
    tau = 1.0
    for lag in range(1, min(max_lag, len(x) // 4) + 1):
        rho = float(np.dot(x[:-lag], x[lag:]) / den)
        if rho <= 0:
            break
        tau += 2.0 * rho
    tau = min(float(len(x)), max(1.0, tau))
    return tau, len(x) / tau, max(1, int(math.ceil(tau)))


def block_price(window: np.ndarray, n: int, block: int,
                rng: np.random.Generator) -> float:
    """Moving-block bootstrap q-quantile of an ordered n-page sum."""
    w = len(window)
    if block == 1:
        idx = rng.integers(0, w, size=(BOOT_K, n))
    else:
        idx = np.empty((BOOT_K, n), dtype=np.int32)
        pos = 0
        while pos < n:
            take = min(block, n - pos)
            starts = rng.integers(0, w, size=(BOOT_K, 1))
            idx[:, pos:pos + take] = (starts + np.arange(take)) % w
            pos += take
    sums = window[idx].sum(axis=1, dtype=np.int64)
    return float(np.quantile(sums, Q, method="higher"))


def prices(window: np.ndarray, n: int, rng: np.random.Generator) -> tuple[dict, dict]:
    mu = float(window.mean())
    sigma = float(window.std(ddof=1))
    tau, neff, block = dependence(window)
    return {
        "iid_normal": n * mu + Z * sigma * math.sqrt(n),
        "ess_normal": n * mu + Z * sigma * math.sqrt(n * tau),
        "moving_block": block_price(window, n, block, rng),
        "raw_page_cap": n * PAGE,
    }, {"tau": tau, "effective_window": neff, "block": block,
        "mu": mu, "sigma": sigma}


def evaluate_ordered(name: str, sizes: np.ndarray, n: int, seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    rows = []
    # Consecutive non-overlapping test batches preserve order without making
    # the reported trial count look larger through overlapping outcomes.
    stride = n
    for end in range(WINDOW, len(sizes) - n + 1, stride):
        window = sizes[end - WINDOW:end]
        demand = int(sizes[end:end + n].sum())
        ps, dep = prices(window, n, rng)
        rows.append({"source": name, "kind": "stationary_ordered", "n": n,
                     "window_end": end, "demand": demand, **dep,
                     "violations": {k: demand > v for k, v in ps.items()},
                     "reserve": ps})
    return rows


def evaluate_transition(name: str, before: np.ndarray, after: np.ndarray,
                        n: int, seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    history = list(before[-WINDOW:])
    rows = []
    for off in range(0, min(len(after), n * 12), n):
        batch = after[off:off + n]
        if len(batch) != n:
            break
        window = np.asarray(history[-WINDOW:], dtype=np.int64)
        demand = int(batch.sum())
        ps, dep = prices(window, n, rng)
        rows.append({"source": name, "kind": "ordered_transition", "n": n,
                     "batch_index": off // n, "demand": demand, **dep,
                     "violations": {k: demand > v for k, v in ps.items()},
                     "reserve": ps})
        history.extend(int(x) for x in batch)
    return rows


def grouped_trace(block: int, seed: int, length: int = 40000) -> np.ndarray:
    """Object-grouped sizes: one content class persists for `block` pages."""
    rng = np.random.default_rng(seed)
    vals = []
    centers = np.asarray([384, 2048, 4096], dtype=np.int64)
    probs = np.asarray([0.20, 0.55, 0.25])
    while len(vals) < length:
        center = int(rng.choice(centers, p=probs))
        jitter = rng.normal(0, 96, size=block)
        run = np.clip(np.rint((center + jitter) / CLASS) * CLASS, 0, PAGE)
        vals.extend(int(x) for x in run)
    return np.asarray(vals[:length], dtype=np.int64)


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["kind"], r["source"], r["n"]), []).append(r)
    out = []
    for (kind, source, n), rs in sorted(groups.items()):
        row = {"kind": kind, "source": source, "n": n, "batches": len(rs),
               "tau_mean": float(np.mean([r["tau"] for r in rs])),
               "effective_window_mean": float(np.mean(
                   [r["effective_window"] for r in rs]))}
        for method in ("iid_normal", "ess_normal", "moving_block", "raw_page_cap"):
            row[f"{method}_violations"] = sum(r["violations"][method] for r in rs)
            row[f"{method}_rate"] = row[f"{method}_violations"] / max(1, len(rs))
            row[f"{method}_bytes_per_page"] = float(np.mean(
                [r["reserve"][method] / n for r in rs]))
        out.append(row)
    return out


def main() -> int:
    ap = common.make_argparser("E14 ordered and dependence-aware calibration")
    args = ap.parse_args()
    limit = 1800 if args.fast else 6000
    t0 = time.perf_counter_ns()
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="e14-real-") as td:
        pages, notes = R.collect_all(Path(td), limit=limit)
        keep = {k: compressed_sizes(v) for k, v in pages.items()
                if k.startswith(("sqlite_", "kvheap_", "tensor_"))}
        for i, (name, sizes) in enumerate(sorted(keep.items())):
            for n in BATCHES:
                rows += evaluate_ordered(name, sizes, n, common.seed_for(i) + n)
        for i, (label, a, b) in enumerate((
            ("sqlite_encrypt", "sqlite_baseline", "sqlite_encrypted"),
            ("kvheap_encrypt", "kvheap_plain", "kvheap_encrypted"),
            ("tensor_quantize", "tensor_fp32", "tensor_int8"),
        )):
            if a in keep and b in keep:
                for n in BATCHES:
                    rows += evaluate_transition(label, keep[a], keep[b], n,
                                                common.seed_for(100 + i) + n)
    for block in (1, 4, 16, 64):
        sizes = grouped_trace(block, common.seed_for(200 + block),
                              12000 if args.fast else 24000)
        for n in BATCHES:
            rows += evaluate_ordered(f"synthetic_object_block_{block}", sizes, n,
                                     common.seed_for(300 + block) + n)

    summary = summarize(rows)
    failures = []
    if not rows:
        failures.append("no ordered cells produced")
    data = {"summary": summary, "rows": rows, "real_state_notes": notes,
            "overall": {m: {
                "violations": sum(r["violations"][m] for r in rows),
                "batches": len(rows),
                "rate": sum(r["violations"][m] for r in rows) / max(1, len(rows)),
            } for m in ("iid_normal", "ess_normal", "moving_block", "raw_page_cap")}}
    config = {"q": Q, "window": WINDOW, "batch_sizes": list(BATCHES),
              "bootstrap_replicates": BOOT_K, "page_order": "preserved",
              "real_sources": "SQLite B-tree, live KV heap, tensor state",
              "synthetic_object_blocks": [1, 4, 16, 64], "fast": args.fast}
    common.write_result(Path(args.out) / "e14_dependence.json", "E14",
                        "Ordered real-state and dependence-aware calibration",
                        config, [common.seed_for(0)], data,
                        modeled_fields=[], failures=failures,
                        wall_seconds=(time.perf_counter_ns() - t0) / 1e9)
    print(f"[e14] {len(rows)} ordered batches; "
          f"iid={data['overall']['iid_normal']['rate']:.4f} "
          f"ess={data['overall']['ess_normal']['rate']:.4f} "
          f"block={data['overall']['moving_block']['rate']:.4f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
