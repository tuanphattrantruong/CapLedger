#!/usr/bin/env python3
"""E15 - detector stress beyond a monotone entropy increase.

The experiment replays the shipped one-sided CUSUM and fast/slow ratio over
abrupt, gradual, oscillating, mean-reverting, variance-only, entropy-decreasing,
and adaptive-below-threshold streams.  Results are detector-model evidence,
not application latency measurements.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

import common

PRE = 10000
POST = 50000
SEEDS = 20
MU0 = 2200.0
SIGMA0 = 320.0
K = 0.5
H = 5.0
# Match src/ctier/internal.h and contract.c exactly.  Earlier versions of E15
# accidentally characterized a sensitivity variant (.08/.002, ratio 1.10).
ALPHA_FAST = 0.05
ALPHA_SLOW = 0.005
RATIO_THRESHOLD = 1.40


def ewma_detector(x: np.ndarray) -> dict:
    slow = fast = var = 0.0
    nsamples = 0
    cusum = 0.0
    c_alarm = r_alarm = None
    false_c = false_r = 0
    for i, value in enumerate(x):
        if nsamples == 0:
            slow = fast = float(value)
            var = 0.0
        else:
            old_slow = slow
            delta = value - slow
            slow += ALPHA_SLOW * delta
            var = (1.0 - ALPHA_SLOW) * (var + ALPHA_SLOW * delta * delta)
            fast += ALPHA_FAST * (value - fast)
        sigma = max(32.0, var ** 0.5)
        if nsamples >= 64:
            # hist.c uses the just-updated slow mean as its reference.
            cusum = max(0.0, cusum + (value - slow - K * sigma))
        c = nsamples >= 64 and cusum > H * sigma
        r = nsamples >= 64 and fast / max(1.0, slow) > RATIO_THRESHOLD
        if i < PRE:
            false_c += int(c)
            false_r += int(r)
            if c:
                cusum = 0.0
        else:
            if c_alarm is None and c:
                c_alarm = i - PRE
            if r_alarm is None and r:
                r_alarm = i - PRE
        nsamples += 1
    return {"cusum_lag": c_alarm, "ratio_lag": r_alarm,
            "cusum_false_ops": false_c, "ratio_false_ops": false_r}


def stream(shape: str, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.clip(rng.normal(MU0, SIGMA0, PRE + POST), 0, 4096)
    t = np.arange(POST)
    if shape == "stationary":
        pass
    elif shape == "abrupt":
        base[PRE:] += 1200
    elif shape == "gradual":
        base[PRE:] += np.minimum(1200, 1200 * t / 20000)
    elif shape == "oscillating":
        base[PRE:] += 900 * ((t // 256) % 2)
    elif shape == "mean_reverting":
        base[PRE:] += 1000 * ((t % 2048) < 128)
    elif shape == "variance_only":
        base[PRE:] = MU0 + rng.normal(0, 900, POST)
    elif shape == "entropy_decrease":
        base[:PRE] += 1200
        base[PRE:] -= 700
    elif shape == "adaptive_below_k":
        # A strategic stream can stay below the per-sample allowance and still
        # accumulate physical exposure until a periodic mark-to-market epoch.
        base[PRE:] = MU0 + (K - 0.05) * SIGMA0
    else:
        raise ValueError(shape)
    return np.clip(base, 0, 4096)


def main() -> int:
    ap = common.make_argparser("E15 drift and adaptive-shaping stress")
    args = ap.parse_args()
    nseed = common.fast_trials(SEEDS, args.fast)
    shapes = ["stationary", "abrupt", "gradual", "oscillating",
              "mean_reverting", "variance_only", "entropy_decrease",
              "adaptive_below_k"]
    t0 = time.perf_counter_ns()
    runs = []
    for shape in shapes:
        for i in range(nseed):
            x = stream(shape, common.seed_for(i))
            r = ewma_detector(x)
            r.update({"shape": shape, "seed": common.seed_for(i),
                      "post_mean": float(x[PRE:].mean()),
                      "post_std": float(x[PRE:].std())})
            runs.append(r)
    summary = []
    for shape in shapes:
        rs = [r for r in runs if r["shape"] == shape]
        for detector, key in (("cusum", "cusum_lag"), ("ratio", "ratio_lag")):
            lags = [r[key] for r in rs if r[key] is not None]
            summary.append({
                "shape": shape, "detector": detector, "runs": len(rs),
                "alarm_rate": len(lags) / max(1, len(rs)),
                # Stationary and entropy-decreasing streams have no harmful
                # upper shift; an alarm there is not a true detection.
                "detection_rate": (len(lags) / max(1, len(rs))
                                   if shape not in ("stationary", "entropy_decrease")
                                   else None),
                "lag_p50": float(np.median(lags)) if lags else None,
                "lag_p99": float(np.percentile(lags, 99)) if lags else None,
                "false_alarm_ops": int(sum(r[f"{detector}_false_ops"] for r in rs)),
                "false_alarm_opportunities": PRE * len(rs),
            })
    # Operational exposure bound for the measured evasive stream. A deployment
    # that reprices at least every T writes bounds unpriced excess by
    # T*(k-epsilon)*sigma, plus one already-admitted batch capped separately.
    eps = 0.05
    bounds = [{"repricing_interval_writes": t,
               "modeled_max_unpriced_excess_bytes": t * (K - eps) * SIGMA0,
               "assumptions": "per-write excess <= (k-epsilon)*sigma; batch cap enforced"}
              for t in (256, 1024, 4096, 16384)]
    config = {"pre_ops": PRE, "post_ops": POST, "seeds": nseed,
              "cusum_k": K, "cusum_h": H, "ratio_threshold": RATIO_THRESHOLD,
              "alpha_fast": ALPHA_FAST, "alpha_slow": ALPHA_SLOW,
              "shapes": shapes, "fast": args.fast}
    data = {"summary": summary, "runs": runs,
            "adaptive_exposure_bounds": bounds,
            "interpretation": ("CUSUM is advisory even for the tested upward-change "
                "model. Alarms on stationary or entropy-decreasing streams are "
                "false positives, not detections. Periodic repricing and a batch "
                "cap are required for a stream below detector sensitivity.")}
    common.write_result(Path(args.out) / "e15_drift_adversary.json", "E15",
                        "Oscillating, adaptive, and false-positive detector stress",
                        config, [common.seed_for(i) for i in range(nseed)], data,
                        modeled_fields=["data.adaptive_exposure_bounds"],
                        failures=[],
                        wall_seconds=(time.perf_counter_ns() - t0) / 1e9)
    print("[e15] " + ", ".join(
        f"{r['shape']}:{r['alarm_rate']:.2f}"
        for r in summary if r["detector"] == "cusum"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
