"""Bootstrap C_q estimator plus the analytic cross-check documented in DESIGN.md.

cq_bootstrap: binary-search the largest page count N such that the fraction of
K bootstrap sums (each the sum of N draws with replacement from the ring of
recent class-rounded compressed sizes) exceeding `budget_bytes` is <= 1-q.

Pure numpy + stdlib; no dylib dependency (this is the periodic, amortized
Python-side estimator — the C analytic estimator is the hot path).
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

MAX_DRAWS_PER_CHUNK = 1 << 24  # cap on rng draws materialized at once
N_HI_CAP = 1 << 22             # hard cap on the search upper bound
MU_ZERO_CAP = 1 << 40          # sentinel cap when mu <= 0 in cq_normal


def cq_bootstrap(ring_sizes: np.ndarray, budget_bytes: int, q: float,
                 K: int = 200, rng: np.random.Generator | None = None) -> int:
    """Largest N in [0, N_hi] with P_boot(sum of N page sizes > budget) <= 1-q.

    ring_sizes: uint16 array of recent class-rounded compressed sizes
                (0 = zero page, 4096 = raw/incompressible).
    Edge cases: empty ring -> 0; all-zero sizes -> N_hi cap; budget <= 0 -> 0.
    """
    if rng is None:
        rng = np.random.default_rng(0)
    sizes = np.ascontiguousarray(ring_sizes, dtype=np.int64)
    n = int(sizes.size)
    if n == 0:
        return 0
    budget = int(budget_bytes)
    if budget <= 0:
        return 0
    if not (0.0 < q < 1.0):
        raise ValueError(f"q must be in (0,1), got {q}")
    mean = float(sizes.mean())
    n_hi = int(min(N_HI_CAP, (budget / max(mean, 1.0)) * 4.0))
    if n_hi <= 0:
        return 0
    # count <= allowed  <=>  count/K <= 1-q (counts are integers)
    allowed = int(math.floor((1.0 - q) * K + 1e-12))

    def acceptable(N: int) -> bool:
        if N <= 0:
            return True
        exceed = 0
        k_block = max(1, MAX_DRAWS_PER_CHUNK // N)
        done = 0
        while done < K:
            kb = min(k_block, K - done)
            idx = rng.integers(0, n, size=(kb, N))
            sums = sizes[idx].sum(axis=1)
            exceed += int((sums > budget).sum())
            if exceed > allowed:
                return False  # early out: already too many exceedances
            done += kb
        return exceed <= allowed

    lo, hi = 0, n_hi
    if acceptable(n_hi):
        return n_hi
    # invariant: acceptable(lo) is True, acceptable(hi) is False
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if acceptable(mid):
            lo = mid
        else:
            hi = mid
    return int(lo)


def cq_normal(mu: float, sigma: float, budget_bytes: float, q: float) -> int:
    """Analytic CLT capacity: largest n with n*mu + z_q*sigma*sqrt(n) <= B.

    Positive root of mu*x^2 + z*sigma*x = B with x = sqrt(n); C_q = floor(x^2).
    Mirrors the C analytic estimator (Acklam inverse-normal there,
    statistics.NormalDist().inv_cdf here) for cross-checking.
    mu <= 0 is capped at MU_ZERO_CAP (the C side caps at free PTE slots).
    """
    B = float(budget_bytes)
    if B <= 0:
        return 0
    if not (0.0 < q < 1.0):
        raise ValueError(f"q must be in (0,1), got {q}")
    if mu <= 0.0:
        return MU_ZERO_CAP
    z = NormalDist().inv_cdf(q)
    if sigma <= 0.0:
        return int(B // mu)
    disc = z * z * sigma * sigma + 4.0 * mu * B
    x = (-z * sigma + math.sqrt(disc)) / (2.0 * mu)
    if x <= 0.0:
        return 0
    return int(math.floor(x * x))
