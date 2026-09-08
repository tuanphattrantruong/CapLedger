"""Device-level invariant tests for the emulated compressed tier (ctier),
driven through the real dylib via the P2 ctypes bindings — i.e. these tests
exercise the exact integration surface the experiments use.

Covers (DESIGN.md compressed-tier sections and src/include/ctier.h):
  - write returns the CLASS-ROUNDED physical cost, cross-checked against an
    independent Python zlib level-1 compression of the same bytes
  - exact physical accounting (PHYS_USED == running sum of write returns,
    through interleaved overwrites and frees)
  - zero-page semantics (0 cost, PAGES_ZERO gauge, spill is a 0-byte no-op)
  - overwrite-allocates-before-free: a rewrite that hits ENOSPACE leaves the
    OLD page byte-intact (adversarial: pool completely full of raw pages)
  - spill + promote byte-exactness, double-spill no-op, spill-file unlink
  - compaction preserves data and releases extents
  - ring snapshot most-recent-first; decayed histogram counts nonzero only
  - CTIER_F_NOHIST disables telemetry (hist/ring -> EINVAL) but not I/O
  - error codes: EFAULT for untracked lpns, EINVAL for out-of-range lpns

Run:  python3 -m unittest discover -s tests -v      (from the project root)
"""
from __future__ import annotations

import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("src/pybind", "workloads", "experiments"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402

import common  # noqa: E402
import ctier_ffi as ffi  # noqa: E402
import traces as T  # noqa: E402

PAGE = 4096


def class_rounded(page: np.ndarray) -> int:
    """Independent prediction of ctier_write's return: zlib level 1, class
    step 128, raw fallback at >= 4096, zero pages cost 0."""
    b = page.tobytes()
    if b == b"\x00" * PAGE:
        return 0
    z = len(zlib.compress(b, 1))
    return PAGE if z >= PAGE else 128 * ((z - 1) // 128 + 1)


def make_tier(pool=8 * 1024 * 1024, max_pages=8192, extent=1 << 20, flags=0):
    sp = common.spill_path()
    t = ffi.Tier(pool, max_pages, str(sp), zlib_level=1, extent_bytes=extent,
                 flags=flags)
    return t, sp


class TestWriteAccounting(unittest.TestCase):
    def test_write_returns_class_rounded_cost(self):
        """Coupling point (a): the write return IS the class-rounded physical
        cost; verified independently with Python zlib for every content class."""
        t, sp = make_tier()
        try:
            for c in (T.TEXT, T.BINARY, T.FLOAT, T.FLOATQ, T.JSON, T.ZERO,
                      T.RANDOM):
                for key in range(4):
                    pg = T.page_bytes(c, key, seed=42, version=0)
                    self.assertEqual(t.write(c * 8 + key, pg),
                                     class_rounded(pg),
                                     f"content {T.CONTENT_NAMES[c]} key {key}")
        finally:
            t.close()
            common.cleanup_spill(sp)

    def test_phys_used_exact_through_overwrites_and_frees(self):
        """PHYS_USED must equal the running sum of write returns, through an
        adversarial interleaving of writes, shrinking/growing overwrites, and
        frees. free_page must return exactly the page's last recorded cost.
        64 KiB extents: with 1 MiB extents an 8 MiB pool has only 8 extents,
        and extent-per-class stranding (each extent is dedicated to one size
        class) yields legitimate ENOSPACE long before the pool is full."""
        t, sp = make_tier(extent=65536)
        try:
            rng = np.random.default_rng(7)
            cost: dict[int, int] = {}
            contents = [T.TEXT, T.BINARY, T.JSON, T.RANDOM, T.ZERO, T.FLOATQ]
            version = {}
            for step in range(600):
                lpn = int(rng.integers(0, 200))
                action = rng.random()
                if action < 0.7 or lpn not in cost:
                    c = contents[int(rng.integers(0, len(contents)))]
                    v = version.get(lpn, 0)
                    pg = T.page_bytes(c, lpn, seed=9, version=v)
                    version[lpn] = v + 1
                    r = t.write(lpn, pg)
                    self.assertGreaterEqual(r, 0)
                    cost[lpn] = r
                else:
                    freed = t.free_page(lpn)
                    self.assertEqual(freed, cost.pop(lpn),
                                     f"free return != last write cost, lpn {lpn}")
                self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PHYS_USED),
                                 sum(cost.values()),
                                 f"phys accounting drifted at step {step}")
        finally:
            t.close()
            common.cleanup_spill(sp)

    def test_zero_page_semantics(self):
        t, sp = make_tier()
        try:
            z = np.zeros(PAGE, dtype=np.uint8)
            self.assertEqual(t.write(1, z), 0)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PAGES_ZERO), 1)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PHYS_USED), 0)
            # spilling a zero page is a 0-byte success no-op
            self.assertEqual(t.spill(1), 0)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED), 0)
            rc, buf = t.read(1)
            self.assertEqual(rc, 0)
            self.assertTrue((buf == 0).all())
            # overwrite zero -> nonzero and back: gauge and accounting follow
            pg = T.page_bytes(T.TEXT, 1, seed=3, version=0)
            r = t.write(1, pg)
            self.assertEqual(r, class_rounded(pg))
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PAGES_ZERO), 0)
            self.assertEqual(t.write(1, z), 0)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PHYS_USED), 0)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PAGES_ZERO), 1)
        finally:
            t.close()
            common.cleanup_spill(sp)


class TestEnospaceAtomicity(unittest.TestCase):
    def test_failed_rewrite_leaves_old_page_intact(self):
        """DESIGN pitfall 1 (transactional overwrite): fill the pool with raw
        pages, then rewrite one with different raw content. The allocation must
        fail (ENOSPACE) BEFORE the old chunk is freed, so the old bytes must
        still read back exactly. After freeing another page, the same rewrite
        must succeed and the new bytes must read back."""
        t, sp = make_tier(pool=2 * 1024 * 1024, max_pages=2048, extent=65536)
        try:
            lpn = 0
            n = 0
            while True:
                r = t.write(n, T.page_bytes(T.RANDOM, n, seed=1, version=0))
                if r == ffi.CTIER_ENOSPACE:
                    break
                self.assertEqual(r, PAGE)
                n += 1
                self.assertLess(n, 2048, "pool never filled")
            self.assertGreater(n, 0)
            old = T.page_bytes(T.RANDOM, lpn, seed=1, version=0)
            new = T.page_bytes(T.RANDOM, lpn, seed=1, version=1)
            self.assertEqual(t.write(lpn, new), ffi.CTIER_ENOSPACE)
            rc, buf = t.read(lpn)
            self.assertEqual(rc, 0)
            self.assertTrue((buf == old).all(),
                            "old page corrupted by failed rewrite")
            # free one other raw page -> the rewrite must now succeed
            self.assertEqual(t.free_page(n - 1), PAGE)
            self.assertEqual(t.write(lpn, new), PAGE)
            rc, buf = t.read(lpn)
            self.assertEqual(rc, 0)
            self.assertTrue((buf == new).all())
        finally:
            t.close()
            common.cleanup_spill(sp)


class TestSpillPromote(unittest.TestCase):
    def test_spill_promote_roundtrip_and_double_spill(self):
        t, sp = make_tier()
        try:
            pg = T.page_bytes(T.TEXT, 11, seed=5, version=0)
            cost = t.write(11, pg)
            self.assertGreater(cost, 0)
            phys0 = t.stat_u64(ffi.CTIER_STAT_PHYS_USED)
            self.assertEqual(t.spill(11), cost)     # releases exactly the cost
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PHYS_USED), phys0 - cost)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED), 1)
            self.assertEqual(t.spill(11), 0)        # already spilled: 0 no-op
            rc, buf = t.read(11)                    # promote
            self.assertEqual(rc, 0)
            self.assertTrue((buf == pg).all(), "promote not byte-exact")
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PHYS_USED), phys0)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PAGES_SPILLED), 0)
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PROMOTES), 1)
            self.assertGreater(t.stat_u64(ffi.CTIER_STAT_SPILL_BYTES_W), 0)
            self.assertGreater(t.stat_u64(ffi.CTIER_STAT_SPILL_BYTES_R), 0)
        finally:
            t.close()
            common.cleanup_spill(sp)

    def test_spill_file_created_and_unlinked_on_destroy(self):
        sp = common.spill_path()
        t = ffi.Tier(8 * 1024 * 1024, 8192, str(sp))
        try:
            t.write(0, T.page_bytes(T.TEXT, 0, seed=2, version=0))
            self.assertEqual(t.spill(0) > 0, True)
            self.assertTrue(Path(sp).exists(), "spill file not created")
        finally:
            t.close()
        self.assertFalse(Path(sp).exists(),
                         "ctier_destroy must unlink its spill file")


class TestCompaction(unittest.TestCase):
    def test_compaction_releases_extents_and_preserves_data(self):
        t, sp = make_tier(pool=4 * 1024 * 1024, max_pages=4096, extent=65536)
        try:
            n = 768  # 3 MiB of raw pages over 48 extents of 16 chunks each
            for i in range(n):
                self.assertEqual(
                    t.write(i, T.page_bytes(T.RANDOM, i, seed=4, version=0)),
                    PAGE)
            ext0 = t.stat_u64(ffi.CTIER_STAT_EXTENTS_ACTIVE)
            survivors = [i for i in range(n) if i % 4 == 0]
            for i in range(n):
                if i % 4 != 0:
                    self.assertEqual(t.free_page(i), PAGE)
            reclaimed = t.compact()
            self.assertGreater(reclaimed, 0, "compaction reclaimed nothing")
            self.assertLess(t.stat_u64(ffi.CTIER_STAT_EXTENTS_ACTIVE), ext0)
            for i in survivors:
                rc, buf = t.read(i)
                self.assertEqual(rc, 0)
                self.assertTrue(
                    (buf == T.page_bytes(T.RANDOM, i, seed=4, version=0)).all(),
                    f"page {i} corrupted by compaction")
        finally:
            t.close()
            common.cleanup_spill(sp)


class TestTelemetry(unittest.TestCase):
    def test_ring_most_recent_first_and_hist_nonzero_only(self):
        t, sp = make_tier()
        try:
            text = T.page_bytes(T.TEXT, 0, seed=6, version=0)
            t.write(0, text)                          # oldest
            t.write(1, np.zeros(PAGE, dtype=np.uint8))
            t.write(2, T.page_bytes(T.RANDOM, 2, seed=6, version=0))  # newest
            ring = t.ring_snapshot()
            self.assertEqual(len(ring), 3)
            self.assertEqual(int(ring[0]), PAGE, "ring[0] must be the newest")
            self.assertEqual(int(ring[1]), 0)
            self.assertEqual(int(ring[2]), class_rounded(text))
            hist = t.hist_snapshot()
            # 2 nonzero samples total; the zero page appears in no bin
            self.assertAlmostEqual(float(hist.sum()), 2.0, places=9)
            self.assertAlmostEqual(float(hist[31]), 1.0, places=9)  # raw
            self.assertEqual(t.stat_u64(ffi.CTIER_STAT_PAGES_ZERO), 1)
            # EWMA sanity: mean within [0, 4096], fast/slow both populated
            for sid in (ffi.CTIER_STAT_EWMA_MEAN_SLOW,
                        ffi.CTIER_STAT_EWMA_MEAN_FAST):
                m = t.stat_f64(sid)
                self.assertGreaterEqual(m, 0.0)
                self.assertLessEqual(m, 4096.0)
        finally:
            t.close()
            common.cleanup_spill(sp)

    def test_nohist_flag_disables_telemetry_not_io(self):
        t, sp = make_tier(flags=ffi.CTIER_F_NOHIST)
        try:
            pg = T.page_bytes(T.TEXT, 0, seed=8, version=0)
            self.assertEqual(t.write(0, pg), class_rounded(pg))
            rc, buf = t.read(0)
            self.assertEqual(rc, 0)
            self.assertTrue((buf == pg).all())
            with self.assertRaises(RuntimeError):
                t.hist_snapshot()           # EINVAL under NOHIST
            self.assertEqual(len(t.ring_snapshot()), 0,
                             "NOHIST ring snapshot must be empty (returns 0)")
            self.assertEqual(t.stat_f64(ffi.CTIER_STAT_EWMA_MEAN_SLOW), 0.0)
        finally:
            t.close()
            common.cleanup_spill(sp)


class TestErrorCodes(unittest.TestCase):
    def test_untracked_and_out_of_range_lpns(self):
        t, sp = make_tier(pool=2 * 1024 * 1024, max_pages=1024, extent=65536)
        try:
            # untracked (state FREE): EFAULT
            self.assertEqual(t.read(5)[0], ffi.CTIER_EFAULT)
            self.assertEqual(t.free_page(5), ffi.CTIER_EFAULT)
            self.assertEqual(t.spill(5), ffi.CTIER_EFAULT)
            # out of range (lpn >= max_pages): EINVAL
            pg = T.page_bytes(T.TEXT, 0, seed=1, version=0)
            self.assertEqual(t.write(1024, pg), ffi.CTIER_EINVAL)
            self.assertEqual(t.read(1024)[0], ffi.CTIER_EINVAL)
            self.assertEqual(t.free_page(1024), ffi.CTIER_EINVAL)
            self.assertEqual(t.spill(1024), ffi.CTIER_EINVAL)
        finally:
            t.close()
            common.cleanup_spill(sp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
