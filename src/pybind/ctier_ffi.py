"""ctypes bindings for build/libctier.dylib.

Mirrors the frozen compressed-tier and contract APIs documented in DESIGN.md.
Every exported function gets explicit argtypes/restype; never rely on default
int conversion for u64 lpns or pointers (the DESIGN.md FFI-width pitfall).

The dylib is resolved relative to the project root:
    <root>/build/libctier.dylib
An override for harness self-testing is available via the CTIER_LIB environment
variable (absolute path to an ABI-compatible dylib); production runs never set it.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import (
    CFUNCTYPE,
    POINTER,
    byref,
    c_char_p,
    c_double,
    c_int,
    c_int64,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LIB_PATH = Path(os.environ.get("CTIER_LIB", str(ROOT / "build" / "libctier.dylib")))

# ---------------------------------------------------------------- constants --
PAGE = 4096

CTIER_F_NOHIST = 1

# ctier errors
CTIER_EINVAL = -1
CTIER_EFAULT = -2
CTIER_ENOSPACE = -3
CTIER_EIO = -4
CTIER_ECORRUPT = -5

# ctier u64 stats
CTIER_STAT_PHYS_USED = 0
CTIER_STAT_PHYS_RESERVED = 1
CTIER_STAT_POOL_BYTES = 2
CTIER_STAT_PAGES_PRESENT = 3
CTIER_STAT_PAGES_RAW = 4
CTIER_STAT_PAGES_ZERO = 5
CTIER_STAT_PAGES_SPILLED = 6
CTIER_STAT_SPILL_BYTES_W = 7
CTIER_STAT_SPILL_BYTES_R = 8
CTIER_STAT_COMPACTIONS = 9
CTIER_STAT_EXTENTS_ACTIVE = 10
CTIER_STAT_WRITES = 11
CTIER_STAT_READS = 12
CTIER_STAT_PROMOTES = 13
CTIER_STAT_COMP_NS_TOTAL = 14
CTIER_STAT_DECOMP_NS_TOTAL = 15
CTIER_STAT_META_BYTES = 16

# ctier f64 stats
CTIER_STAT_EWMA_MEAN_SLOW = 100
CTIER_STAT_EWMA_VAR_SLOW = 101
CTIER_STAT_EWMA_MEAN_FAST = 102

# contract classes
CONTRACT_GUAR = 0
CONTRACT_OPP = 1

# contract estimators
EST_ANALYTIC = 0
EST_STATIC = 1
EST_OPTIMISTIC = 2
EST_PESSIMISTIC = 3
EST_EXTERNAL = 4
EST_ANALYTIC_RING = 5   # closed form fed by the device ring window (E1 repair)
EST_ZSWAP = 6           # software-tier baseline: exact, ungated, global-LRU
EST_TRANSPARENT = 7     # hardware/Compresso baseline: device reserve, fatal OOM
EST_REACTIVE_TENANT = 8 # exact, ungated, tenant-aware reactive reclaim

CODEC_ZLIB = 0
CODEC_LZ4 = 1
CODEC_ZSTD = 2
CODEC_NAMES = {CODEC_ZLIB: "zlib", CODEC_LZ4: "lz4", CODEC_ZSTD: "zstd"}
EST_NAMES = {
    EST_ANALYTIC: "analytic", EST_STATIC: "static", EST_OPTIMISTIC: "optimistic",
    EST_PESSIMISTIC: "pessimistic", EST_EXTERNAL: "external",
    EST_ANALYTIC_RING: "analytic_ring", EST_ZSWAP: "zswap",
    EST_TRANSPARENT: "transparent", EST_REACTIVE_TENANT: "reactive_tenant",
}

# victim policies
VICTIM_CLOCK_ONLY = 0
VICTIM_DENSITY_AGE = 1
VICTIM_GLOBAL_LRU = 2

# contract errors
CONTRACT_EREJECT = -10
CONTRACT_EAGAIN = -11
CONTRACT_EINVAL = -12
CONTRACT_ENOSPACE = -13

# contract u64 stats
CSTAT_ADMITTED_GUAR = 0
CSTAT_ADMITTED_OPP = 1
CSTAT_WRITTEN_GUAR = 2
CSTAT_WRITTEN_OPP = 3
CSTAT_VIOLATIONS = 4
CSTAT_R1_SPILLS = 5
CSTAT_R2_SHED_CALLS = 6
CSTAT_R2_SHED_BYTES = 7
CSTAT_R3_FORCED_SPILLS = 8
CSTAT_OPP_DROPS = 9
CSTAT_RECOVERY_NS = 10
CSTAT_PRESSURE_LEVEL = 11
CSTAT_LIE_FLAG = 12
CSTAT_INVARIANT_BREACHES = 13
CSTAT_EPOCH = 14
CSTAT_RESV_BYTES = 15
CSTAT_HARD_FAILURES = 16
CSTAT_ADMIT_REJECTS = 17
CSTAT_ADMIT_CALLS = 18
CSTAT_REPRICE_EVENTS = 19
CSTAT_REPRICE_REVOKED = 20
CSTAT_CUSUM_FLAG = 21

# contract f64 stats
CSTAT_CQ_GUAR_PAGES = 100
CSTAT_MU_TENANT = 101
CSTAT_SIGMA_TENANT = 102
CSTAT_OVERDRAFT_BYTES = 103
CSTAT_REPRICE_DEBT_BYTES = 104

SHED_CB = CFUNCTYPE(None, c_void_p, c_uint64)

_U8P = POINTER(c_uint8)


# ------------------------------------------------------------------ loading --
def _load() -> ctypes.CDLL:
    if not LIB_PATH.exists():
        raise RuntimeError(
            f"ctier dylib not found: {LIB_PATH}\n"
            f"Build it first with `make lib` in {ROOT} (Makefile owned by the C package)."
        )
    try:
        return ctypes.CDLL(str(LIB_PATH))
    except OSError as e:  # pragma: no cover
        raise RuntimeError(f"failed to load {LIB_PATH}: {e}") from e


_lib = _load()


class ContractCfg(ctypes.Structure):
    """Mirror of contract_cfg_t. Field order and types are load-bearing:
    ctypes lays this out with the platform's natural alignment, which matches
    the C struct because every member is 4- or 8-byte aligned in the same
    order. test_ffi_cfg_struct_matches_c pins the size against the C side."""
    _fields_ = [
        ("estimator", c_int),
        ("q_guaranteed", c_double),
        ("headroom_frac", c_double),
        ("static_ratio", c_double),
        ("epoch_ops", c_uint32),
        ("ntenants", c_int),
        ("victim_policy", c_int),
        ("ring_window", c_uint32),
        ("reprice", c_int),
        ("reserve_frac", c_double),
    ]


class FeedbackRecord(ctypes.Structure):
    """Mirror of the fixed 16-byte controller completion record."""
    _fields_ = [
        ("token", c_uint64),
        ("sequence", c_uint32),
        ("generation", c_uint16),
        ("size_class", c_uint8),
        ("status", c_uint8),
    ]


def _sig(name: str, restype, argtypes) -> None:
    fn = getattr(_lib, name)
    fn.restype = restype
    fn.argtypes = argtypes


# ctier
_sig("ctier_create", c_void_p,
     [c_uint64, c_uint64, c_int, c_uint64, c_uint32, c_uint32, c_char_p])
_sig("ctier_destroy", None, [c_void_p])
_sig("ctier_write", c_int64, [c_void_p, c_uint64, _U8P])
_sig("ctier_read", c_int, [c_void_p, c_uint64, _U8P])
_sig("ctier_free_page", c_int, [c_void_p, c_uint64])
_sig("ctier_spill", c_int, [c_void_p, c_uint64])
_sig("ctier_compact", c_int64, [c_void_p])
_sig("ctier_stat_u64", c_uint64, [c_void_p, c_int])
_sig("ctier_stat_f64", c_double, [c_void_p, c_int])
_sig("ctier_hist_snapshot", c_int, [c_void_p, POINTER(c_double)])
_sig("ctier_ring_snapshot", c_int, [c_void_p, POINTER(c_uint16), c_int])
_sig("ctier_create2", c_void_p,
     [c_uint64, c_uint64, c_int, c_int, c_uint64, c_uint32, c_uint32, c_char_p])
_sig("ctier_ring_moments", c_int,
     [c_void_p, c_uint32, POINTER(c_double), POINTER(c_double)])
_sig("ctier_cusum", c_int, [c_void_p, POINTER(c_double), POINTER(c_uint64)])
_sig("ctier_cusum_reset", c_int, [c_void_p])
_sig("ctier_feedback_enable", c_int, [c_void_p, c_uint32])
_sig("ctier_feedback_poll", c_int, [c_void_p, POINTER(FeedbackRecord)])
_sig("ctier_feedback_dropped", c_uint64, [c_void_p])
_sig("ctier_now_ns", c_uint64, [])

# contract
_sig("contract_create", c_void_p,
     [c_void_p, c_int, c_double, c_double, c_double, c_uint32, c_int, c_int])
_sig("contract_destroy", None, [c_void_p])
_sig("contract_capacity_pages", c_int64, [c_void_p, c_int, c_int])
_sig("contract_admit", c_int, [c_void_p, c_int, c_int, c_uint64])
_sig("contract_write", c_int, [c_void_p, c_int, c_int, c_uint64, _U8P])
_sig("contract_read", c_int, [c_void_p, c_uint64, _U8P])
_sig("contract_free", c_int, [c_void_p, c_int, c_uint64])
_sig("contract_register_shed", c_int, [c_void_p, c_int, SHED_CB, c_void_p])
_sig("contract_check_invariant", c_int, [c_void_p])
_sig("contract_stat_u64", c_uint64, [c_void_p, c_int, c_int])
_sig("contract_stat_f64", c_double, [c_void_p, c_int, c_int])
_sig("contract_create2", c_void_p, [c_void_p, POINTER(ContractCfg)])
_sig("contract_cfg_size", c_uint64, [])
_sig("norminv", c_double, [c_double])


# ------------------------------------------------------------------ helpers --
def cfg_size_c() -> int:
    """sizeof(contract_cfg_t) as the C library sees it (layout cross-check)."""
    return int(_lib.contract_cfg_size())


def as_u8p(arr: np.ndarray):
    """Pointer to a contiguous uint8 numpy buffer (caller keeps arr alive)."""
    if arr.dtype != np.uint8 or not arr.flags["C_CONTIGUOUS"]:
        raise ValueError("buffer must be a C-contiguous uint8 ndarray")
    return arr.ctypes.data_as(_U8P)


def new_page() -> np.ndarray:
    return np.empty(PAGE, dtype=np.uint8)


def now_ns() -> int:
    return int(_lib.ctier_now_ns())


# ----------------------------------------------------------------- wrappers --
class Tier:
    """Thin RAII wrapper over ctier_t*. Context-manager cleanup; unlinks its
    spill file on destroy (ctier_destroy performs the documented unlink)."""

    def __init__(self, pool_bytes: int, max_pages: int, spill_path: str,
                 zlib_level: int = 1, extent_bytes: int = 1 << 20,
                 ring_capacity: int = 4096, flags: int = 0,
                 codec: int = CODEC_ZLIB):
        self._h = None
        h = _lib.ctier_create2(
            c_uint64(pool_bytes), c_uint64(max_pages), c_int(codec),
            c_int(zlib_level), c_uint64(extent_bytes), c_uint32(ring_capacity),
            c_uint32(flags), str(spill_path).encode())
        if not h:
            raise RuntimeError(
                f"ctier_create2 failed (pool={pool_bytes}, max_pages={max_pages}, "
                f"codec={codec}, spill={spill_path})")
        self._h = h
        self.pool_bytes = int(pool_bytes)
        self.max_pages = int(max_pages)
        self.codec = int(codec)
        self.level = int(zlib_level)
        self._rbuf = np.empty(PAGE, dtype=np.uint8)

    # -- lifecycle
    def close(self) -> None:
        if self._h:
            _lib.ctier_destroy(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):  # best-effort; scripts must close() explicitly
        try:
            self.close()
        except Exception:
            pass

    @property
    def handle(self):
        if not self._h:
            raise RuntimeError("tier already destroyed")
        return self._h

    # -- ops
    def write(self, lpn: int, page: np.ndarray) -> int:
        """Returns class-rounded phys bytes (>=0) or negative CTIER_E*."""
        return int(_lib.ctier_write(self._h, c_uint64(lpn), as_u8p(page)))

    def read(self, lpn: int, out: np.ndarray | None = None) -> tuple[int, np.ndarray]:
        buf = out if out is not None else self._rbuf
        rc = int(_lib.ctier_read(self._h, c_uint64(lpn), as_u8p(buf)))
        return rc, buf

    def free_page(self, lpn: int) -> int:
        """>= 0 (bytes freed) on success, negative CTIER_E* on error."""
        return int(_lib.ctier_free_page(self._h, c_uint64(lpn)))

    def spill(self, lpn: int) -> int:
        """>= 0 (bytes moved to the far tier) on success, negative on error."""
        return int(_lib.ctier_spill(self._h, c_uint64(lpn)))

    def compact(self) -> int:
        return int(_lib.ctier_compact(self._h))

    def feedback_enable(self, capacity: int) -> int:
        return int(_lib.ctier_feedback_enable(self._h, c_uint32(capacity)))

    def feedback_poll_all(self) -> list[FeedbackRecord]:
        records: list[FeedbackRecord] = []
        while True:
            record = FeedbackRecord()
            rc = int(_lib.ctier_feedback_poll(self._h, byref(record)))
            if rc <= 0:
                if rc < 0:
                    raise RuntimeError(f"ctier_feedback_poll -> {rc}")
                return records
            records.append(record)

    def feedback_dropped(self) -> int:
        return int(_lib.ctier_feedback_dropped(self._h))

    # -- telemetry
    def stat_u64(self, stat_id: int) -> int:
        return int(_lib.ctier_stat_u64(self._h, c_int(stat_id)))

    def stat_f64(self, stat_id: int) -> float:
        return float(_lib.ctier_stat_f64(self._h, c_int(stat_id)))

    def hist_snapshot(self) -> np.ndarray:
        out = (c_double * 32)()
        rc = int(_lib.ctier_hist_snapshot(self._h, out))
        if rc < 0:
            raise RuntimeError(f"ctier_hist_snapshot -> {rc}")
        return np.array(out, dtype=np.float64)

    def ring_snapshot(self, cap: int = 4096) -> np.ndarray:
        buf = np.zeros(cap, dtype=np.uint16)
        n = int(_lib.ctier_ring_snapshot(
            self._h, buf.ctypes.data_as(POINTER(c_uint16)), c_int(cap)))
        if n < 0:
            raise RuntimeError(f"ctier_ring_snapshot -> {n}")
        return buf[:n].copy()

    def ring_moments(self, window: int = 0) -> tuple[int, float, float]:
        """(n, mu, sigma) over the most recent `window` class-rounded sizes;
        window=0 means the whole ring and is O(1) on the C side."""
        mu, sg = c_double(0.0), c_double(0.0)
        n = _lib.ctier_ring_moments(self._h, c_uint32(window), byref(mu), byref(sg))
        return int(n), float(mu.value), float(sg.value)

    def cusum(self) -> tuple[float, int]:
        """(statistic, latched alarm) of the device-side CUSUM detector."""
        st, al = c_double(0.0), c_uint64(0)
        _lib.ctier_cusum(self._h, byref(st), byref(al))
        return float(st.value), int(al.value)

    def cusum_reset(self) -> None:
        """Re-arm the latching CUSUM after policy has acted on an alarm."""
        _lib.ctier_cusum_reset(self._h)


class Contract:
    """Thin RAII wrapper over contract_t*. Does NOT own the tier.
    Keeps shed-callback CFUNCTYPE objects referenced (GC pitfall)."""

    def __init__(self, tier: Tier, estimator: int, q: float,
                 headroom_frac: float = 0.05, static_ratio: float = 2.0,
                 epoch_ops: int = 1024, ntenants: int = 1,
                 victim_policy: int = VICTIM_DENSITY_AGE,
                 ring_window: int = 0, reprice: int = 0,
                 reserve_frac: float = 0.0):
        self._h = None
        cfg = ContractCfg(
            estimator=c_int(estimator), q_guaranteed=c_double(q),
            headroom_frac=c_double(headroom_frac),
            static_ratio=c_double(static_ratio), epoch_ops=c_uint32(epoch_ops),
            ntenants=c_int(ntenants), victim_policy=c_int(victim_policy),
            ring_window=c_uint32(ring_window), reprice=c_int(reprice),
            reserve_frac=c_double(reserve_frac))
        self.cfg = cfg
        h = _lib.contract_create2(tier.handle, byref(cfg))
        if not h:
            raise RuntimeError(
                f"contract_create2 failed (estimator={estimator}, q={q}, "
                f"ntenants={ntenants})")
        self._h = h
        self._shed_refs: list = []  # keep CFUNCTYPE objects alive
        self._rbuf = np.empty(PAGE, dtype=np.uint8)

    def close(self) -> None:
        if self._h:
            _lib.contract_destroy(self._h)
            self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # -- ops
    def capacity_pages(self, tenant: int, cls: int) -> int:
        return int(_lib.contract_capacity_pages(self._h, c_int(tenant), c_int(cls)))

    def admit(self, tenant: int, cls: int, npages: int) -> int:
        return int(_lib.contract_admit(self._h, c_int(tenant), c_int(cls),
                                       c_uint64(npages)))

    def write(self, tenant: int, cls: int, lpn: int, page: np.ndarray) -> int:
        return int(_lib.contract_write(self._h, c_int(tenant), c_int(cls),
                                       c_uint64(lpn), as_u8p(page)))

    def read(self, lpn: int, out: np.ndarray | None = None) -> tuple[int, np.ndarray]:
        buf = out if out is not None else self._rbuf
        rc = int(_lib.contract_read(self._h, c_uint64(lpn), as_u8p(buf)))
        return rc, buf

    def free(self, tenant: int, lpn: int) -> int:
        return int(_lib.contract_free(self._h, c_int(tenant), c_uint64(lpn)))

    def register_shed(self, tenant: int, fn) -> int:
        """fn(bytes_needed:int) -> None. Called synchronously from the writing
        thread; may reenter contract_free/contract_write (same thread only)."""
        cfn = SHED_CB(lambda _arg, bytes_needed, _f=fn: _f(int(bytes_needed)))
        self._shed_refs.append(cfn)
        return int(_lib.contract_register_shed(self._h, c_int(tenant), cfn, None))

    def check_invariant(self) -> int:
        return int(_lib.contract_check_invariant(self._h))

    def stat_u64(self, tenant: int, stat_id: int) -> int:
        return int(_lib.contract_stat_u64(self._h, c_int(tenant), c_int(stat_id)))

    def stat_f64(self, tenant: int, stat_id: int) -> float:
        return float(_lib.contract_stat_f64(self._h, c_int(tenant), c_int(stat_id)))
