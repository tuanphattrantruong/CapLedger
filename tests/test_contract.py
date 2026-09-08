"""Contract-layer invariant tests, driven through the real dylib via the P2
ctypes bindings. These are deliberately adversarial: the crash-rewrite storm,
the reentrant Python shed callback, the entropy-bomb tenant, and the
device-full failure paths are the mechanisms the paper's claims rest on.

Covers (DESIGN.md contract/accounting sections and src/include/contract.h):
  - admission gate: r(n) <= B enforced exactly (STATIC estimator, closed form)
  - contract_check_invariant() == 0 through every adversarial sequence
  - pessimistic cold start (prior mu=4096 before 64 samples) and the
    post-warm-up capacity gain; C_q monotone in q
  - reservation groups drain exactly; overdraft = owned - consumed
  - capability enforcement: unadmitted GUAR writes, tenant/cls pinning on
    tracked pages, cross-tenant frees
  - EXTERNAL estimator always admits (E1 bootstrap-arm hook, coupling pt e)
  - REGRESSION: compressibility-crash rewrite storm keeps the accounting
    invariant intact while the ladder spills the very pages being rewritten
    (guards the stale-page-state bug fixed at integration)
  - reentrant shed callback from Python (R2), with CFUNCTYPE kept alive
  - multi-tenant isolation: violations confined to the entropy-bomb tenant
  - CONTRACT_EAGAIN / CONTRACT_ENOSPACE + HARD_FAILURES on a full device
  - norminv reference values; bootstrap vs analytic C_q cross-check

Run:  python3 -m unittest discover -s tests -v      (from the project root)
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("src/pybind", "workloads", "experiments"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402

import bootstrap  # noqa: E402
import common  # noqa: E402
import ctier_ffi as ffi  # noqa: E402
import traces as T  # noqa: E402

PAGE = 4096


def b_total(pool: int, headroom: float = 0.05) -> int:
    return int(math.floor(pool * (1.0 - headroom)))


def staged_fill(contract, tenant: int, n: int, content: int, seed: int,
                lpn0: int, batch: int = 256, version: int = 0) -> int:
    """Admit+write n GUAR pages in batches (cold-start pitfall: upfront
    admission of a large working set is rejected under the pessimistic prior;
    real clients admit as the estimator warms). Returns pages written."""
    done = 0
    while done < n:
        want = min(batch, n - done)
        while want > 0 and contract.admit(tenant, ffi.CONTRACT_GUAR, want) != 0:
            want //= 2
        if want == 0:
            break
        for i in range(want):
            lpn = lpn0 + done + i
            rc = contract.write(tenant, ffi.CONTRACT_GUAR, lpn,
                                T.page_bytes(content, lpn, seed, version))
            if rc != 0:
                return done + i
        done += want
    return done


class TestAdmissionGate(unittest.TestCase):
    def test_static_gate_exact_boundary(self):
        """STATIC ratio 2.0 reserves exactly 2048 B/page, so the gate boundary
        is computable in closed form and must be exact."""
        pool = 8 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 16384, str(sp))
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_STATIC, 0.99, static_ratio=2.0)
            B = b_total(pool)
            n_max = B // 2048
            self.assertEqual(c.capacity_pages(0, ffi.CONTRACT_GUAR), n_max)
            self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, n_max), 0)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_RESV_BYTES),
                             n_max * 2048)
            # one more full batch must be rejected (remaining B < 64*2048)
            self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 64),
                             ffi.CONTRACT_EREJECT)
            # opportunistic admission never reserves and never rejects
            self.assertEqual(c.admit(0, ffi.CONTRACT_OPP, 10 ** 9), 0)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_RESV_BYTES),
                             n_max * 2048)
            self.assertEqual(c.check_invariant(), 0)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)

    def test_cold_start_prior_then_warmup_gain(self):
        """Before 64 samples the ANALYTIC advertisement must use the
        pessimistic prior (mu=4096, sigma=0): C ~= B_total/4096. After warming
        with compressible TEXT the advertisement must exceed that."""
        pool = 8 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 16384, str(sp))
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_ANALYTIC, 0.99)
            B = b_total(pool)
            cold = c.capacity_pages(0, ffi.CONTRACT_GUAR)
            self.assertLessEqual(abs(cold - B // PAGE), 1,
                                 "cold start must advertise the raw prior")
            n = staged_fill(c, 0, 512, T.TEXT, seed=3, lpn0=0)
            self.assertEqual(n, 512)
            warm = c.capacity_pages(0, ffi.CONTRACT_GUAR)
            self.assertGreater(warm, cold,
                               "TEXT warm-up must raise advertised capacity")
            mu = c.stat_f64(0, ffi.CSTAT_MU_TENANT)
            self.assertTrue(128 <= mu < 4096, f"warmed mu implausible: {mu}")
            self.assertEqual(c.check_invariant(), 0)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)

    def test_capacity_monotone_in_q(self):
        """C_0.5 >= C_0.9 >= C_0.99 > 0 on the same warmed telemetry."""
        pool = 8 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 16384, str(sp))
        try:
            for i in range(300):  # warm the tier's global EWMAs directly
                tier.write(i, T.page_bytes(T.TEXT, i, seed=5, version=0))
            caps = []
            for q in (0.5, 0.9, 0.99):
                c = ffi.Contract(tier, ffi.EST_ANALYTIC, q)
                try:
                    caps.append(c.capacity_pages(0, ffi.CONTRACT_GUAR))
                finally:
                    c.close()
            self.assertGreater(caps[-1], 0)
            self.assertGreaterEqual(caps[0], caps[1])
            self.assertGreaterEqual(caps[1], caps[2])
        finally:
            tier.close()
            common.cleanup_spill(sp)

    def test_external_always_admits(self):
        """Coupling point (e): the EXTERNAL estimator's gate must accept ANY
        guaranteed admission — E1's Python bootstrap arm depends on it."""
        pool = 4 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 4096, str(sp))
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_EXTERNAL, 0.99)
            self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 1 << 30), 0)
            self.assertEqual(c.stat_u64(0, ffi.CSTAT_ADMITTED_GUAR), 1 << 30)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)


class TestReservationAccounting(unittest.TestCase):
    def test_reservation_drains_exactly_and_overdraft(self):
        # 256 KiB extents on a 16 MiB pool: with 1 MiB extents this pool has
        # only 8 of them, TEXT pages land in a dozen different size classes,
        # and the device runs out of EXTENTS while 99% of its bytes are still
        # free -- so the ladder spills most of the fill and this test silently
        # stops measuring what it means to measure.
        pool = 16 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 16384, str(sp), extent_bytes=(256 << 10))
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_STATIC, 0.99, static_ratio=2.0)
            self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 256), 0)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_RESV_BYTES), 256 * 2048)
            for i in range(256):
                self.assertEqual(
                    c.write(0, ffi.CONTRACT_GUAR, i,
                            T.page_bytes(T.TEXT, i, seed=11, version=0)), 0)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_RESV_BYTES), 0,
                             "reservation must drain to 0 after the fill")
            self.assertEqual(tier.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED), 0,
                             "the fill spilled; overdraft is then owned-based "
                             "rather than resident-based and this assertion "
                             "no longer means what it says")
            phys = tier.stat_u64(ffi.CTIER_STAT_PHYS_USED)
            od = c.stat_f64(0, ffi.CSTAT_OVERDRAFT_BYTES)
            self.assertAlmostEqual(od, phys - 256 * 2048, delta=1.0,
                                   msg="overdraft != owned - consumed")
            # rewrites of tracked pages must NOT consume new reservations
            self.assertEqual(
                c.write(0, ffi.CONTRACT_GUAR, 0,
                        T.page_bytes(T.TEXT, 0, seed=11, version=1)), 0)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_RESV_BYTES), 0)
            self.assertEqual(c.check_invariant(), 0)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)


class TestCapabilityEnforcement(unittest.TestCase):
    def test_unadmitted_guar_and_tenant_cls_pinning(self):
        pool = 4 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 4096, str(sp))
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_ANALYTIC, 0.99, ntenants=2)
            pg = T.page_bytes(T.TEXT, 0, seed=13, version=0)
            # GUAR write with no open reservation group: rejected, nothing
            # reaches the tier
            self.assertEqual(c.write(0, ffi.CONTRACT_GUAR, 0, pg),
                             ffi.CONTRACT_EINVAL)
            self.assertEqual(tier.stat_u64(ffi.CTIER_STAT_WRITES), 0)
            # OPP writes are implicitly admitted
            self.assertEqual(c.write(0, ffi.CONTRACT_OPP, 0, pg), 0)
            # tenant/cls are pinned at first write
            self.assertEqual(c.write(0, ffi.CONTRACT_GUAR, 0, pg),
                             ffi.CONTRACT_EINVAL)
            self.assertEqual(c.write(1, ffi.CONTRACT_OPP, 0, pg),
                             ffi.CONTRACT_EINVAL)
            self.assertEqual(c.free(1, 0), ffi.CONTRACT_EINVAL)
            self.assertEqual(c.free(0, 0), 0)
            # read of an untracked lpn
            self.assertEqual(c.read(7)[0], ffi.CONTRACT_EINVAL)
            self.assertEqual(c.check_invariant(), 0)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)


class TestCrashRegression(unittest.TestCase):
    def test_crash_rewrite_storm_invariant_holds(self):
        """REGRESSION (integration bug): during ENOSPACE retries the recovery
        ladder may spill THE page being rewritten; accounting from the stale
        pre-ladder page state then double-subtracts its cost and the
        sum(realized)==PHYS_USED invariant (bit 2) breaks. Reproduces E3's
        crash pattern at small scale: TEXT fill, then a RANDOM rewrite storm
        under DENSITY_AGE, checking the invariant throughout. 256 KiB
        extents: extent-per-class stranding then produces real device
        ENOSPACE during the storm (verified at integration: this exact
        configuration fails with invariant bit 2 when the stale-state fix is
        reverted), which is the only path exercising ENOSPACE-retry
        accounting."""
        pool = 8 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 8192, str(sp), extent_bytes=262144)
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_ANALYTIC, 0.99,
                             victim_policy=ffi.VICTIM_DENSITY_AGE)
            n_guar, n_opp = 2048, 512
            self.assertEqual(
                staged_fill(c, 0, n_guar, T.TEXT, seed=17, lpn0=0), n_guar)
            c.admit(0, ffi.CONTRACT_OPP, n_opp)
            for i in range(n_opp):
                self.assertEqual(
                    c.write(0, ffi.CONTRACT_OPP, n_guar + i,
                            T.page_bytes(T.TEXT, n_guar + i, seed=17, version=0)),
                    0)
            self.assertEqual(c.check_invariant(), 0)

            # phase 1: entropy crash — rewrite every GUAR page with RANDOM
            for i in range(n_guar):
                rc = c.write(0, ffi.CONTRACT_GUAR, i,
                             T.page_bytes(T.RANDOM, i, seed=17, version=1))
                self.assertEqual(rc, 0, f"hard failure at rewrite {i}")
                if i % 64 == 0:
                    self.assertEqual(c.check_invariant(), 0,
                                     f"invariant broke at rewrite {i}")
            self.assertEqual(c.check_invariant(), 0)
            self.assertGreater(c.stat_u64(-1, ffi.CSTAT_R1_SPILLS), 0,
                               "R1 never engaged (opp pages not spilled)")
            self.assertGreater(c.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS), 0,
                               "R3 never engaged")
            self.assertGreater(c.stat_u64(-1, ffi.CSTAT_VIOLATIONS), 0)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES), 0,
                             "spill capacity is unbounded; no hard failures")
            self.assertGreater(c.stat_u64(-1, ffi.CSTAT_RECOVERY_NS), 0)

            # every page must still be readable and byte-exact (promotes)
            buf = np.empty(PAGE, dtype=np.uint8)
            for i in range(0, n_guar, 37):
                rc, _ = c.read(i, buf)
                self.assertEqual(rc, 0, f"read of rewritten page {i} -> {rc}")
                self.assertTrue(
                    (buf == T.page_bytes(T.RANDOM, i, seed=17, version=1)).all(),
                    f"page {i} corrupted by the recovery ladder")
            for i in range(n_guar, n_guar + n_opp, 41):
                rc, _ = c.read(i, buf)
                self.assertEqual(rc, 0)
                self.assertTrue(
                    (buf == T.page_bytes(T.TEXT, i, seed=17, version=0)).all(),
                    f"opp page {i} corrupted by spill/promote")
            self.assertEqual(c.check_invariant(), 0)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)


class TestShedCallback(unittest.TestCase):
    def test_reentrant_python_shed_callback(self):
        """R2 must invoke the Python shed callback (CFUNCTYPE kept alive by
        the wrapper) with the contract mutex held; the callback reenters
        contract_free from the same thread. Accounting must survive."""
        pool = 8 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 8192, str(sp), extent_bytes=65536)
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_EXTERNAL, 0.99)
            n_text = 1700
            self.assertEqual(
                staged_fill(c, 0, n_text, T.TEXT, seed=19, lpn0=0, batch=512),
                n_text)
            sheddable = list(range(600))     # coldest TEXT pages
            calls = {"n": 0, "max_need": 0}

            def shed(bytes_needed: int) -> None:
                calls["n"] += 1
                calls["max_need"] = max(calls["max_need"], bytes_needed)
                for _ in range(64):
                    if not sheddable:
                        return
                    c.free(0, sheddable.pop())   # reentrant contract_free

            self.assertEqual(c.register_shed(0, shed), 0)
            # push utilization over L2 with incompressible pages; no OPP pages
            # exist, so R1 cannot relieve pressure and R2 must fire
            self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 1200), 0)
            for i in range(1200):
                rc = c.write(0, ffi.CONTRACT_GUAR, n_text + i,
                             T.page_bytes(T.RANDOM, n_text + i, seed=19,
                                          version=0))
                self.assertEqual(rc, 0)
            self.assertGreater(calls["n"], 0, "shed callback never invoked")
            self.assertGreater(calls["max_need"], 0)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_R2_SHED_CALLS),
                             calls["n"])
            self.assertGreater(c.stat_u64(-1, ffi.CSTAT_R2_SHED_BYTES), 0,
                               "shed bytes not measured via phys delta")
            self.assertEqual(c.check_invariant(), 0)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)


class TestMultiTenantIsolation(unittest.TestCase):
    def test_entropy_bomb_confined_to_lying_tenant(self):
        """t0 honest TEXT, t1 flips its whole working set to RANDOM. All
        forced-spill violations must land on t1 (the positive-overdraft
        liar); t0 must see zero violations and keep its data intact, and
        INVARIANT_BREACHES must stay 0."""
        pool = 12 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 8192, str(sp), extent_bytes=65536)
        c = None
        try:
            c = ffi.Contract(tier, ffi.EST_ANALYTIC, 0.99, ntenants=2,
                             victim_policy=ffi.VICTIM_DENSITY_AGE)
            n0, n1 = 1200, 2600
            self.assertEqual(staged_fill(c, 0, n0, T.TEXT, seed=23, lpn0=0),
                             n0)
            self.assertEqual(staged_fill(c, 1, n1, T.TEXT, seed=23, lpn0=n0),
                             n1)
            self.assertEqual(c.check_invariant(), 0)
            for i in range(n1):                      # the bomb
                rc = c.write(1, ffi.CONTRACT_GUAR, n0 + i,
                             T.page_bytes(T.RANDOM, n0 + i, seed=23, version=1))
                self.assertEqual(rc, 0)
                if i % 128 == 0:
                    self.assertEqual(c.check_invariant(), 0,
                                     f"invariant broke at bomb write {i}")
            self.assertEqual(c.check_invariant(), 0)
            self.assertGreater(c.stat_u64(1, ffi.CSTAT_VIOLATIONS), 0,
                               "bomber was never forced to spill")
            self.assertEqual(c.stat_u64(0, ffi.CSTAT_VIOLATIONS), 0,
                             "honest tenant took violations: isolation broken")
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_INVARIANT_BREACHES), 0)
            buf = np.empty(PAGE, dtype=np.uint8)
            for i in range(0, n0, 61):               # honest data intact
                rc, _ = c.read(i, buf)
                self.assertEqual(rc, 0)
                self.assertTrue(
                    (buf == T.page_bytes(T.TEXT, i, seed=23, version=0)).all(),
                    f"honest tenant page {i} corrupted")
            self.assertEqual(c.check_invariant(), 0)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)


class TestFullDeviceFailurePaths(unittest.TestCase):
    def test_eagain_and_hard_failure_when_nothing_reclaimable(self):
        """Device completely full of pages the contract does not track (raw
        prefill), so the ladder has NO victims: an OPP write must come back
        EAGAIN (counted as a drop, not a violation) and a GUAR write must
        exhaust its ladder retries into CONTRACT_ENOSPACE + HARD_FAILURES.
        (check_invariant is inapplicable here by construction: the raw
        prefill bypassed the contract.)"""
        pool = 2 * 1024 * 1024
        sp = common.spill_path()
        tier = ffi.Tier(pool, 2048, str(sp), extent_bytes=65536)
        c = None
        try:
            n = 0
            while True:
                r = tier.write(n, T.page_bytes(T.RANDOM, n, seed=29, version=0))
                if r == ffi.CTIER_ENOSPACE:
                    break
                n += 1
            c = ffi.Contract(tier, ffi.EST_EXTERNAL, 0.99)
            pg = T.page_bytes(T.RANDOM, 2000, seed=29, version=0)
            self.assertEqual(c.write(0, ffi.CONTRACT_OPP, 2000, pg),
                             ffi.CONTRACT_EAGAIN)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_OPP_DROPS), 1)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_VIOLATIONS), 0)
            self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 1), 0)
            self.assertEqual(c.write(0, ffi.CONTRACT_GUAR, 2001, pg),
                             ffi.CONTRACT_ENOSPACE)
            self.assertEqual(c.stat_u64(-1, ffi.CSTAT_HARD_FAILURES), 1)
        finally:
            if c:
                c.close()
            tier.close()
            common.cleanup_spill(sp)


class TestEstimators(unittest.TestCase):
    def test_norminv_reference_values(self):
        lib = ffi._lib
        import ctypes
        lib.norminv.restype = ctypes.c_double
        lib.norminv.argtypes = [ctypes.c_double]
        self.assertAlmostEqual(lib.norminv(0.5), 0.0, delta=1e-12)
        self.assertAlmostEqual(lib.norminv(0.99), 2.3263478740408408,
                               delta=1e-8)
        self.assertAlmostEqual(lib.norminv(0.9), 1.2815515655446004,
                               delta=1e-8)
        self.assertAlmostEqual(lib.norminv(0.01), -lib.norminv(0.99),
                               delta=1e-8)

    def test_bootstrap_vs_analytic_crosscheck(self):
        """DESIGN 3.6: on a well-behaved size distribution the bootstrap C_q
        must agree with the analytic CLT capacity within a few percent."""
        rng = np.random.default_rng(31)
        sizes = np.clip(rng.normal(2000, 400, 4096), 128, 4096)
        sizes = (np.ceil(sizes / 128) * 128).astype(np.uint16)
        budget = 4 * 1024 * 1024
        mu = float(sizes.mean())
        sigma = float(sizes.std())
        for q in (0.8, 0.95):
            ana = bootstrap.cq_normal(mu, sigma, budget, q)
            boo = bootstrap.cq_bootstrap(sizes, budget, q, K=400,
                                         rng=np.random.default_rng(7))
            self.assertGreater(ana, 0)
            self.assertLess(abs(ana - boo) / ana, 0.05,
                            f"q={q}: analytic {ana} vs bootstrap {boo}")
        # monotone in q
        b50 = bootstrap.cq_bootstrap(sizes, budget, 0.5, K=400,
                                     rng=np.random.default_rng(7))
        b99 = bootstrap.cq_bootstrap(sizes, budget, 0.99, K=400,
                                     rng=np.random.default_rng(7))
        self.assertGreaterEqual(b50, b99)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# Round-2 additions: the estimator repair, the two external baselines, the
# device-side window moments / CUSUM, and reservation mark-to-market.
# ---------------------------------------------------------------------------
CONTENT_IDX = {n: i for i, n in enumerate(T.CONTENT_NAMES)}


def _content(name: str, seed: int, n: int, version: int = 0) -> list[np.ndarray]:
    """n distinct pages of one content class, deterministic in (seed, key)."""
    cid = CONTENT_IDX[name]
    return [T.page_bytes(cid, k, seed, version) for k in range(n)]


def _spill(tag: str):
    return common.spill_path()


class TestNewArms(unittest.TestCase):
    """Behavioural contracts of the arms added for E6-E9."""

    # Pools here are deliberately generous: the device dedicates whole 1 MiB
    # extents to size classes, so a small pool runs out of EXTENTS long before
    # it runs out of bytes and writes fail for a reason unrelated to what these
    # tests are checking.
    def _tier(self, pool=(64 << 20), pages=16384, **kw):
        t = ffi.Tier(pool, pages, str(_spill("newarms")), **kw)
        self.addCleanup(t.close)
        return t

    def test_ffi_cfg_struct_matches_c(self):
        """The ctypes mirror of contract_cfg_t must match the C layout, or
        every create2 call silently misreads its arguments."""
        import ctypes
        self.assertEqual(ffi.cfg_size_c(), ctypes.sizeof(ffi.ContractCfg))

    def test_ring_moments_match_a_numpy_reduction(self):
        """O(1) running sums must agree exactly with a scan of the ring."""
        t = self._tier()
        rng = np.random.default_rng(7)
        for lpn in range(600):
            pg = ffi.new_page()
            n = int(rng.integers(0, 4096))
            pg[:n] = rng.integers(0, 256, size=n, dtype=np.uint8)
            t.write(lpn, pg)
        n, mu, sg = t.ring_moments(0)
        ring = t.ring_snapshot(4096)[:n].astype(np.float64)
        self.assertEqual(n, ring.size)
        self.assertAlmostEqual(mu, float(ring.mean()), places=6)
        self.assertAlmostEqual(sg, float(ring.std()), places=6)

    def test_ring_moments_windowed_matches_scan(self):
        t = self._tier()
        rng = np.random.default_rng(11)
        for lpn in range(500):
            pg = ffi.new_page()
            n = int(rng.integers(0, 4096))
            pg[:n] = rng.integers(0, 256, size=n, dtype=np.uint8)
            t.write(lpn, pg)
        n, mu, sg = t.ring_moments(128)
        ring = t.ring_snapshot(128).astype(np.float64)
        self.assertEqual(n, 128)
        self.assertAlmostEqual(mu, float(ring.mean()), places=6)
        self.assertAlmostEqual(sg, float(ring.std()), places=6)

    def test_cusum_fires_on_a_shift_and_not_on_a_steady_stream(self):
        t = self._tier(pool=(64 << 20), pages=16384)
        text = _content("TEXT", 0, 4000)
        for lpn, pg in enumerate(text):
            t.write(lpn, pg)
        _, alarm_steady = t.cusum()
        self.assertEqual(alarm_steady, 0, "CUSUM alarmed on a steady stream")
        for lpn, pg in enumerate(_content("RANDOM", 1, 200)):
            t.write(4000 + lpn, pg)
        _, alarm_shift = t.cusum()
        self.assertEqual(alarm_shift, 1, "CUSUM missed a text->random shift")

    def test_analytic_ring_advertises_from_the_window_not_the_ewma(self):
        """The repaired arm must track the ring window; with a stationary
        stream the two arms should agree closely, which is the sanity check
        that the ring path is wired to the same formula."""
        t = self._tier()
        c = ffi.Contract(t, ffi.EST_ANALYTIC_RING, 0.99, ntenants=1)
        self.addCleanup(c.close)
        for lpn, pg in enumerate(_content("TEXT", 3, 3000)):
            while c.admit(0, ffi.CONTRACT_GUAR, 1) != 0:
                pass
            self.assertEqual(c.write(0, ffi.CONTRACT_GUAR, lpn, pg), 0)
        n, mu_ring, sg_ring = t.ring_moments(0)
        self.assertGreater(n, 64)
        self.assertAlmostEqual(c.stat_f64(0, ffi.CSTAT_MU_TENANT), mu_ring, places=6)
        self.assertAlmostEqual(c.stat_f64(0, ffi.CSTAT_SIGMA_TENANT), sg_ring, places=6)
        self.assertGreater(c.capacity_pages(0, ffi.CONTRACT_GUAR), 0)

    def test_zswap_baseline_never_refuses_and_never_reserves(self):
        t = self._tier()
        c = ffi.Contract(t, ffi.EST_ZSWAP, 0.99, ntenants=2)
        self.addCleanup(c.close)
        self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 10 ** 6), 0,
                         "zswap baseline refused an admission")
        self.assertEqual(c.stat_u64(0, ffi.CSTAT_ADMIT_REJECTS), 0)
        self.assertEqual(c.stat_u64(-1, ffi.CSTAT_RESV_BYTES), 0,
                         "zswap baseline purchased a reservation")

    def test_tenant_reactive_baseline_is_ungated_unreserved_and_attributed(self):
        """The stronger control keeps exact reactive accounting and the
        overdraft victim order, but removes risk pricing and reservations."""
        t = self._tier()
        c = ffi.Contract(t, ffi.EST_REACTIVE_TENANT, 0.99, ntenants=2,
                         victim_policy=ffi.VICTIM_CLOCK_ONLY)
        self.addCleanup(c.close)
        self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 10 ** 6), 0)
        self.assertEqual(c.stat_u64(-1, ffi.CSTAT_RESV_BYTES), 0)
        self.assertEqual(c.stat_u64(0, ffi.CSTAT_ADMIT_REJECTS), 0)

    def test_zswap_inverts_attribution_where_the_contract_confines_it(self):
        """The comparison the paper rests on. Same trace, same device, same
        victim policy within a list: overdraft-first reclaim charges the liar,
        global-LRU reclaim charges the honest tenant -- and does so almost
        exclusively, because the liar's just-rewritten pages are the YOUNGEST
        in LRU order, so an age-only policy protects exactly the pages that
        caused the pressure."""
        N = 3000

        def run(est):
            # 256 KiB extents: the device dedicates whole extents to size
            # classes, so a pool this small with 1 MiB extents starves on
            # extents rather than bytes.
            t = ffi.Tier(20 << 20, 16384, str(common.spill_path()),
                         extent_bytes=(256 << 10))
            try:
                c = ffi.Contract(t, est, 0.99, ntenants=2,
                                 victim_policy=ffi.VICTIM_CLOCK_ONLY)
                try:
                    honest = _content("TEXT", 5, N)
                    liar_t = _content("TEXT", 6, N)
                    liar_r = _content("RANDOM", 7, N)
                    for i in range(N):
                        for tid, pg, base in ((0, honest[i], 0),
                                              (1, liar_t[i], 5000)):
                            for _ in range(4):   # cold-start prior: retry a few
                                if c.admit(tid, ffi.CONTRACT_GUAR, 1) == 0:
                                    break
                            c.write(tid, ffi.CONTRACT_GUAR, base + i, pg)
                    for i in range(N):           # tenant 1 inflates in place
                        c.write(1, ffi.CONTRACT_GUAR, 5000 + i, liar_r[i])
                    return (c.stat_u64(0, ffi.CSTAT_VIOLATIONS),
                            c.stat_u64(1, ffi.CSTAT_VIOLATIONS))
                finally:
                    c.close()
            finally:
                t.close()

        v0_contract, v1_contract = run(ffi.EST_ANALYTIC)
        v0_zswap, v1_zswap = run(ffi.EST_ZSWAP)
        self.assertGreater(v1_contract, 0, "no pressure generated; test is vacuous")
        self.assertEqual(v0_contract, 0, "contract charged the honest tenant")
        self.assertGreater(v0_zswap, v1_zswap,
                           "global-LRU baseline did not misattribute; the E5 "
                           "comparison would be vacuous")

    def test_transparent_baseline_hard_fails_instead_of_spilling(self):
        t = self._tier(pool=(24 << 20), pages=8192)
        c = ffi.Contract(t, ffi.EST_TRANSPARENT, 0.99, ntenants=1,
                         reserve_frac=0.10)
        self.addCleanup(c.close)
        pages = _content("RANDOM", 9, 8000)
        for i in range(8000):
            c.admit(0, ffi.CONTRACT_GUAR, 1)   # the open gate never refuses
            c.write(0, ffi.CONTRACT_GUAR, i, pages[i])
        self.assertGreater(c.stat_u64(0, ffi.CSTAT_HARD_FAILURES), 0,
                           "transparent baseline never tripped its reserve")
        self.assertEqual(c.stat_u64(-1, ffi.CSTAT_R3_FORCED_SPILLS), 0,
                         "transparent baseline spilled; it has no spill path")

    def test_repricing_revokes_stale_cheap_reservations(self):
        """A tenant that buys reservation while mu-hat is low and then inflates
        should have its unwritten remainder repriced once the lie is flagged."""
        t = self._tier(pool=(96 << 20), pages=24576)
        c = ffi.Contract(t, ffi.EST_ANALYTIC, 0.99, ntenants=2, reprice=1)
        self.addCleanup(c.close)
        # Fill the telemetry window with cheap pages so mu-hat is low.
        for i, pg in enumerate(_content("TEXT", 12, 5000)):
            self.assertEqual(c.write(0, ffi.CONTRACT_OPP, i, pg), 0)
        # Tenant 1 needs its own 64 samples before it is priced off the global
        # mean rather than the pessimistic prior.
        for i, pg in enumerate(_content("TEXT", 14, 200)):
            while c.admit(1, ffi.CONTRACT_GUAR, 1) != 0:
                pass
            self.assertEqual(c.write(1, ffi.CONTRACT_GUAR, 10000 + i, pg), 0)
        granted = 0
        for _ in range(40):                      # buy a large block cheaply
            if c.admit(1, ffi.CONTRACT_GUAR, 200) == 0:
                granted += 200
        self.assertGreater(granted, 0, "no reservation was granted; test vacuous")
        resv_before = c.stat_u64(-1, ffi.CSTAT_RESV_BYTES)
        self.assertGreater(resv_before, 0)
        # The EWMA-ratio flag is a transient: it rises when fast outruns slow
        # and clears again once slow absorbs the shift, so it has to be
        # observed as it happens, not read at the end of the run.
        saw_flag = False
        for i, pg in enumerate(_content("RANDOM", 13, 600)):   # inflate
            c.write(1, ffi.CONTRACT_GUAR, 12000 + i, pg)
            if c.stat_u64(-1, ffi.CSTAT_LIE_FLAG):
                saw_flag = True
        self.assertTrue(saw_flag, "lie flag never rose; test is vacuous")
        self.assertGreater(c.stat_u64(-1, ffi.CSTAT_REPRICE_EVENTS), 0,
                           "repricing never ran on the lie-flag edge")
        self.assertEqual(c.check_invariant(), 0,
                         "repricing broke the accounting invariant")

    def test_codecs_roundtrip_and_shift_the_size_distribution(self):
        """Every codec must round-trip exactly; lz4 should be measurably
        worse than zlib on text, which is why codec choice is an external-
        validity variable rather than a detail."""
        means = {}
        pages = _content("TEXT", 21, 400)
        for codec in (ffi.CODEC_ZLIB, ffi.CODEC_LZ4, ffi.CODEC_ZSTD):
            t = ffi.Tier(16 << 20, 2048, str(_spill(f"codec{codec}")),
                         codec=codec)
            try:
                for i, pg in enumerate(pages):
                    self.assertGreater(t.write(i, pg), 0)
                for i, pg in enumerate(pages):
                    rc, got = t.read(i)
                    self.assertEqual(rc, 0)
                    np.testing.assert_array_equal(got, pg)
                means[codec] = t.ring_moments(0)[1]
            finally:
                t.close()
        self.assertGreater(means[ffi.CODEC_LZ4], means[ffi.CODEC_ZLIB],
                           "lz4 did not compress text worse than zlib")

    def test_overdraft_is_returned_on_free(self):
        """Overdraft must describe the pages a tenant holds NOW. If freeing a
        page does not return the reservation it consumed, `consumed` only ever
        grows, overdraft drifts negative without bound, and a tenant can bank
        that credit by churning cheap pages to make itself an unattractive
        eviction victim. (Regression for the bug E9 exposed.)"""
        t = self._tier(pool=(64 << 20), pages=16384)
        c = ffi.Contract(t, ffi.EST_ANALYTIC_RING, 0.99, ntenants=1)
        self.addCleanup(c.close)
        pages = _content("TEXT", 31, 4000)
        for i, pg in enumerate(pages):
            while c.admit(0, ffi.CONTRACT_GUAR, 1) != 0:
                pass
            self.assertEqual(c.write(0, ffi.CONTRACT_GUAR, i, pg), 0)
        od_loaded = c.stat_f64(0, ffi.CSTAT_OVERDRAFT_BYTES)
        for i in range(4000):
            self.assertEqual(c.free(0, i), 0)
        od_empty = c.stat_f64(0, ffi.CSTAT_OVERDRAFT_BYTES)
        self.assertAlmostEqual(
            od_empty, 0.0, delta=4096.0,
            msg=f"overdraft did not return to ~0 after freeing every page "
                f"(loaded={od_loaded:.0f}, empty={od_empty:.0f})")
        self.assertEqual(c.check_invariant(), 0)

    def test_churn_cannot_bank_negative_overdraft(self):
        """The attack the previous test guards against, end to end: a tenant
        that writes and frees compressible pages must not end up looking
        better-behaved than one that never touched the tier."""
        t = self._tier(pool=(64 << 20), pages=16384)
        c = ffi.Contract(t, ffi.EST_ANALYTIC_RING, 0.99, ntenants=2)
        self.addCleanup(c.close)
        for rounds in range(3):                     # tenant 0 churns
            for i, pg in enumerate(_content("TEXT", 40 + rounds, 1500)):
                while c.admit(0, ffi.CONTRACT_GUAR, 1) != 0:
                    pass
                c.write(0, ffi.CONTRACT_GUAR, i, pg)
            for i in range(1500):
                c.free(0, i)
        self.assertGreater(c.stat_f64(0, ffi.CSTAT_OVERDRAFT_BYTES), -4096.0,
                           "churning banked negative overdraft (eviction "
                           "immunity)")

    def test_fractional_reservation_is_returned_exactly(self):
        """A non-integral per-page quote must not leave sub-byte credit after
        repeated write/free cycles.  pg_resv used to truncate the double quote
        to uint16 while consumed retained the fraction."""
        t = self._tier(pool=(32 << 20), pages=8192)
        c = ffi.Contract(t, ffi.EST_ANALYTIC_RING, 0.99, ntenants=1)
        self.addCleanup(c.close)
        text = _content("TEXT", 44, 128)
        random = _content("RANDOM", 45, 128)
        warm = [page for pair in zip(text, random) for page in pair]
        for i, pg in enumerate(warm):
            while c.admit(0, ffi.CONTRACT_GUAR, 1) != 0:
                pass
            self.assertEqual(c.write(0, ffi.CONTRACT_GUAR, i, pg), 0)
        for i in range(len(warm)):
            self.assertEqual(c.free(0, i), 0)
        for round_no in range(200):
            self.assertEqual(c.admit(0, ffi.CONTRACT_GUAR, 3), 0)
            for j in range(3):
                lpn = 1000 + j
                self.assertEqual(c.write(0, ffi.CONTRACT_GUAR, lpn,
                                         warm[(round_no + j) % len(warm)]), 0)
            for j in range(3):
                self.assertEqual(c.free(0, 1000 + j), 0)
        self.assertAlmostEqual(c.stat_f64(0, ffi.CSTAT_OVERDRAFT_BYTES),
                               0.0, delta=1e-6)
        self.assertEqual(c.check_invariant(), 0)

    def test_overdraft_survives_a_forced_spill(self):
        """Attribution must not depend on where a page currently sits. If
        overdraft is computed from pool-resident bytes, the ladder's first
        eviction of the lying tenant lowers its overdraft, it stops being the
        preferred victim, and the next page taken belongs to someone innocent.
        (Regression for the bug E9 exposed.)"""
        t = self._tier(pool=(20 << 20), pages=16384, extent_bytes=(256 << 10))
        c = ffi.Contract(t, ffi.EST_ANALYTIC_RING, 0.99, ntenants=2,
                         victim_policy=ffi.VICTIM_CLOCK_ONLY)
        self.addCleanup(c.close)
        N = 3000
        honest = _content("TEXT", 51, N)
        liar_t = _content("TEXT", 52, N)
        liar_r = _content("RANDOM", 53, N)
        for i in range(N):
            for tid, pg, base in ((0, honest[i], 0), (1, liar_t[i], 5000)):
                for _ in range(4):
                    if c.admit(tid, ffi.CONTRACT_GUAR, 1) == 0:
                        break
                c.write(tid, ffi.CONTRACT_GUAR, base + i, pg)
        for i in range(N):
            c.write(1, ffi.CONTRACT_GUAR, 5000 + i, liar_r[i])
        spilled = t.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED)
        self.assertGreater(spilled, 0, "no spilling happened; test is vacuous")
        self.assertGreater(c.stat_f64(1, ffi.CSTAT_OVERDRAFT_BYTES), 0.0,
                           "the lying tenant's overdraft went non-positive "
                           "once its pages were spilled")
        self.assertEqual(c.stat_u64(0, ffi.CSTAT_VIOLATIONS), 0,
                         "honest tenant charged after the liar was spilled")
