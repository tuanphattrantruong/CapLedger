"""Shared experiment infrastructure (DESIGN.md validation/provenance section).

- run_metadata(): verbatim `date -u`, uname -a, sysctl, clang/python/numpy/zlib
- write_result(): enforces the ccap-results-v1 schema, refuses NaN/inf in
  `data` unless the corresponding failure is recorded
- spill_path()/temp_spill(): unique per-run spill files under experiments/tmp/
- base seed and --fast/--out argparse plumbing

Importing this module puts src/pybind and workloads on sys.path.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import subprocess
import sys
import zlib
import re
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src" / "pybind", ROOT / "workloads", ROOT / "experiments"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

RESULTS_DIR = ROOT / "experiments" / "results"
TMP_DIR = ROOT / "experiments" / "tmp"

BASE_SEED = 0xA5F00D1E
SCHEMA = "ccap-results-v1"


def seed_for(i: int) -> int:
    return BASE_SEED + i


def fast_trials(n: int, fast: bool) -> int:
    """--fast runs ~quarter of the trials (never 0)."""
    return max(1, n // 4) if fast else n


# ------------------------------------------------------------- CLI plumbing --
def make_argparser(description: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--fast", action="store_true",
                    help="~quarter trials; smoke-test only, never paper numbers")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR,
                    help="output directory for the results JSON")
    return ap


# ----------------------------------------------------------------- metadata --
def _cmd(args: list[str]) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"<failed: {e}>"


def run_metadata() -> dict:
    # Never persist hostnames or user paths in an anonymous-review artifact.
    # `uname -srm` retains the kernel/architecture facts needed for provenance
    # while omitting the node name included by `uname -a`.
    uname = _cmd(["uname", "-srm"])
    ncpu = _cmd(["sysctl", "-n", "hw.ncpu"])
    memsize = _cmd(["sysctl", "-n", "hw.memsize"])
    clang = _cmd(["clang", "--version"]).splitlines()[0] if _cmd(["clang", "--version"]) else "<none>"
    osver = _cmd(["sw_vers", "-productVersion"])
    return {
        "date_utc": _cmd(["date", "-u"]),
        "host": {
            "uname": uname,
            "ncpu": int(ncpu) if ncpu.isdigit() else ncpu,
            "memsize": int(memsize) if memsize.isdigit() else memsize,
            "os": f"macOS {osver}",
        },
        "toolchain": {
            "clang": clang,
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "zlib": zlib.ZLIB_VERSION,
        },
    }


# -------------------------------------------------------------- JSON writer --
def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, Path):
        obj = str(obj)
    if isinstance(obj, str):
        root = str(ROOT)
        if obj.startswith(root):
            obj = "<artifact-root>" + obj[len(root):]
        # Inputs should normally already be relative. This is a final guard for
        # absolute paths contributed by subprocesses or capture libraries.
        obj = re.sub(r"/(?:Users|home)/[^/\s]+", "<user-home>", obj)
        return obj
    return obj


def _find_nonfinite(obj, path: str, out: list[str]) -> None:
    if isinstance(obj, float) and not math.isfinite(obj):
        out.append(path)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _find_nonfinite(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _find_nonfinite(v, f"{path}[{i}]", out)


def _replace_nonfinite(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return "NaN" if math.isnan(obj) else ("+Inf" if obj > 0 else "-Inf")
    if isinstance(obj, dict):
        return {k: _replace_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_nonfinite(v) for v in obj]
    return obj


def write_result(path: Path, experiment: str, title: str, config: dict,
                 seeds: list[int], data: dict, modeled_fields: list[str] | None = None,
                 failures: list[str] | None = None, wall_seconds: float = 0.0) -> Path:
    """Write one ccap-results-v1 file. Refuses to write NaN/inf inside `data`
    unless failures are recorded (then the values are stringified so the JSON
    stays parseable and the failure text explains them)."""
    modeled_fields = list(modeled_fields or [])
    failures = list(failures or [])
    data = _jsonable(data)
    bad: list[str] = []
    _find_nonfinite(data, "data", bad)
    if bad:
        if not failures:
            raise RuntimeError(
                f"refusing to write {path}: non-finite values at {bad[:8]}"
                f"{'...' if len(bad) > 8 else ''} and no failure recorded")
        failures.append(f"non-finite values stringified at: {', '.join(bad[:32])}")
        data = _replace_nonfinite(data)
    meta = run_metadata()
    doc = {
        "schema": SCHEMA,
        "experiment": experiment,
        "title": title,
        "date_utc": meta["date_utc"],
        "wall_seconds": round(float(wall_seconds), 3),
        "host": meta["host"],
        "toolchain": meta["toolchain"],
        "config": _jsonable(config),
        "seeds": _jsonable(list(seeds)),
        "data": data,
        "modeled_fields": modeled_fields,
        "failures": failures,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, allow_nan=False)
        f.write("\n")
    return path


# -------------------------------------------------------------- spill files --
_spill_counter = 0


def spill_path() -> Path:
    """Unique spill file path under experiments/tmp/ (not created here)."""
    global _spill_counter
    _spill_counter += 1
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR / f"spill_{os.getpid()}_{_spill_counter}.dat"


def cleanup_spill(p: Path) -> None:
    with contextlib.suppress(OSError):
        Path(p).unlink(missing_ok=True)


@contextlib.contextmanager
def temp_spill():
    """Context manager yielding a unique spill path, unlinked on exit
    (ctier_destroy also unlinks; both are tolerant of a missing file)."""
    p = spill_path()
    try:
        yield p
    finally:
        cleanup_spill(p)


# ---------------------------------------------------------------- statistics --
def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson 95% score interval for k successes out of n."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ------------------------------------------------------- parallel cell runs --
# Every experiment cell builds its own tier + contract and is deterministic in
# its seed, so cells are independent and safe to run in separate processes.
# This is what makes the seed counts the reviews asked for affordable: the
# measurement machine has 24 performance cores, and a cell is pure CPU.
#
# Timing-sensitive experiments (E4 overheads, anything reporting latency) must
# NOT use this -- concurrent cells contend for cores and inflate every
# percentile. Use it only for counter-valued experiments.

def default_jobs() -> int:
    """Performance-core count, leaving two cores for the OS and the driver."""
    try:
        p = _cmd(["sysctl", "-n", "hw.perflevel0.logicalcpu"])
        n = int(p) if p.isdigit() else os.cpu_count() or 4
    except Exception:
        n = os.cpu_count() or 4
    return max(1, n - 2)


def run_cells(fn, cellargs: list[tuple], jobs: int | None = None,
              label: str = "cell", progress=None) -> list:
    """Map `fn` over `cellargs` in a process pool, preserving input order.

    `fn` must be a module-level function (picklable) returning a dict, and must
    not rely on process-global state carried over from the parent. jobs=1 runs
    in-process, which is what --fast and timing experiments should use.
    """
    jobs = default_jobs() if jobs is None else max(1, int(jobs))
    total = len(cellargs)
    out: list = []
    if jobs == 1:
        for i, a in enumerate(cellargs):
            out.append(fn(*a))
            if progress:
                progress(i + 1, total, a, out[-1])
        return out
    import multiprocessing as mp
    ctx = mp.get_context("spawn")   # fork + a loaded dylib is not safe on macOS
    # imap (not starmap): starmap blocks until every cell is done, which on a
    # multi-minute sweep means no progress output at all.
    with ctx.Pool(processes=jobs, maxtasksperchild=32) as pool:
        for i, r in enumerate(pool.imap(_apply, [(fn, a) for a in cellargs],
                                        chunksize=1)):
            out.append(r)
            if progress:
                progress(i + 1, total, cellargs[i], r)
    return out


def _apply(job):
    """Module-level trampoline so imap can carry (fn, args) pairs."""
    fn, args = job
    return fn(*args)
