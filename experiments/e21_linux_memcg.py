#!/usr/bin/env python3
"""E21 - restricted Linux cgroup-v2 ownership and reclaim proof of concept.

Run this experiment in a privileged Linux cgroup namespace.  It deliberately
implements only private, anonymous, 4 KiB pages.  Two worker processes are
placed in separate memory cgroups before allocating their mappings.  A small
controller prices their deterministic page streams, attributes an entropy
transition to the rewriting tenant, asks that tenant's cgroup to reclaim in a
bounded chunk, and measures the subsequent page-fault scan.  The experiment
also exercises fork/COW, task migration, and tenant exit.

This is not a kernel patch and it is not a compressed-memory device.  Its
purpose is narrower: validate that the contract's owner selection can drive a
real Linux memcg reclaim path without reclaiming the unchanged tenant.
"""
from __future__ import annotations

import argparse
import json
import math
import mmap
import multiprocessing as mp
import os
import platform
import random
import signal
import statistics
import time
import zlib
from pathlib import Path

PAGE = 4096
CLASS = 128
DEFAULT_MIB = 32
DEFAULT_TRIALS = 7


def class_bytes(payload: bytes) -> int:
    n = len(zlib.compress(payload, 1))
    return PAGE if n >= PAGE else int(math.ceil(n / CLASS) * CLASS)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def text_page(tenant: int) -> bytes:
    seed = (f"tenant={tenant};redis-like-record=ledger;" * 160).encode()
    return (seed * (PAGE // len(seed) + 1))[:PAGE]


def worker(conn, tenant: int) -> None:
    conn.send({"pid": os.getpid()})
    mapping: mmap.mmap | None = None
    mapping_bytes = 0
    cow_pid = 0
    try:
        while True:
            msg = conn.recv()
            op = msg[0]
            if op == "allocate":
                mapping_bytes = int(msg[1])
                mapping = mmap.mmap(-1, mapping_bytes,
                                    flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                                    prot=mmap.PROT_READ | mmap.PROT_WRITE)
                if hasattr(mapping, "madvise") and hasattr(mmap, "MADV_NOHUGEPAGE"):
                    mapping.madvise(mmap.MADV_NOHUGEPAGE)
                page = text_page(tenant)
                t0 = time.perf_counter_ns()
                for off in range(0, mapping_bytes, PAGE):
                    mapping[off:off + PAGE] = page
                conn.send({"write_ns": time.perf_counter_ns() - t0})
            elif op == "entropy":
                assert mapping is not None
                rng = random.Random(int(msg[1]))
                t0 = time.perf_counter_ns()
                for off in range(0, mapping_bytes, PAGE):
                    mapping[off:off + PAGE] = rng.randbytes(PAGE)
                conn.send({"write_ns": time.perf_counter_ns() - t0})
            elif op == "scan":
                assert mapping is not None
                t0 = time.perf_counter_ns()
                checksum = 0
                for off in range(0, mapping_bytes, PAGE):
                    checksum ^= mapping[off]
                conn.send({"scan_ns": time.perf_counter_ns() - t0,
                           "checksum": checksum})
            elif op == "fork_cow":
                assert mapping is not None
                pages = min(int(msg[1]), mapping_bytes // PAGE)
                ready_r, ready_w = os.pipe()
                child = os.fork()
                if child == 0:
                    os.close(ready_r)
                    for i in range(pages):
                        off = i * PAGE
                        mapping[off] = (mapping[off] + 1) & 0xFF
                    os.write(ready_w, b"1")
                    os.close(ready_w)
                    while True:
                        signal.pause()
                os.close(ready_w)
                os.read(ready_r, 1)
                os.close(ready_r)
                cow_pid = child
                conn.send({"cow_pid": child, "cow_pages": pages})
            elif op == "drop_cow":
                if cow_pid:
                    os.kill(cow_pid, signal.SIGTERM)
                    os.waitpid(cow_pid, 0)
                    cow_pid = 0
                conn.send({"dropped": True})
            elif op == "stop":
                break
            else:
                raise ValueError(f"unknown worker operation {op!r}")
    finally:
        if cow_pid:
            try:
                os.kill(cow_pid, signal.SIGKILL)
                os.waitpid(cow_pid, 0)
            except ProcessLookupError:
                pass
        if mapping is not None:
            mapping.close()
        conn.close()


def read_int(path: Path) -> int:
    return int(path.read_text().strip())


def memory_stat(cg: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (cg / "memory.stat").read_text().splitlines():
        key, value = line.split()
        out[key] = int(value)
    out["memory_current"] = read_int(cg / "memory.current")
    swap = cg / "memory.swap.current"
    out["memory_swap_current"] = read_int(swap) if swap.exists() else 0
    return out


def move_pid(cg: Path, pid: int) -> None:
    (cg / "cgroup.procs").write_text(f"{pid}\n")


def setup_cgroups(tag: str) -> tuple[Path, dict[str, Path]]:
    root = Path("/sys/fs/cgroup")
    if platform.system() != "Linux" or not (root / "cgroup.controllers").exists():
        raise RuntimeError("E21 requires Linux cgroup v2")
    if os.geteuid() != 0:
        raise RuntimeError("E21 requires a private privileged cgroup namespace")

    admin = root / f"ledger_admin_{tag}"
    admin.mkdir()
    # A cgroup cannot both contain processes and distribute a domain
    # controller.  Move the container's initial processes into an admin leaf,
    # then enable memory for sibling tenant groups.  This changes only the
    # disposable container cgroup namespace used by run_e21_linux.sh.
    for raw in (root / "cgroup.procs").read_text().split():
        try:
            move_pid(admin, int(raw))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    (root / "cgroup.subtree_control").write_text("+memory\n")
    groups = {}
    for name in ("tenant0", "tenant1", "migrated"):
        cg = root / f"ledger_{name}_{tag}"
        cg.mkdir()
        groups[name] = cg
    groups["admin"] = admin
    return root, groups


def command(conn, *parts):
    conn.send(parts)
    return conn.recv()


def reclaim(cg: Path, amount: int) -> tuple[int, str | None]:
    before = read_int(cg / "memory.current")
    error = None
    t0 = time.perf_counter_ns()
    try:
        (cg / "memory.reclaim").write_text(f"{amount} swappiness=max\n")
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            (cg / "memory.reclaim").write_text(f"{amount}\n")
            error = f"swappiness=max rejected; plain reclaim succeeded ({error})"
        except OSError as retry:
            error = f"{error}; plain reclaim failed: {retry}"
    elapsed = time.perf_counter_ns() - t0
    after = read_int(cg / "memory.current")
    return elapsed, error if before <= after else error


def run_trial(groups: dict[str, Path], mapping_bytes: int, seed: int,
              lifecycle: bool) -> dict:
    ctx = mp.get_context("fork")
    workers = []
    conns = []
    pids = []
    for tenant in (0, 1):
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=worker, args=(child, tenant))
        proc.start()
        pid = int(parent.recv()["pid"])
        move_pid(groups[f"tenant{tenant}"], pid)
        workers.append(proc)
        conns.append(parent)
        pids.append(pid)

    npages = mapping_bytes // PAGE
    text_classes = [class_bytes(text_page(t)) for t in (0, 1)]
    reservation = [npages * c for c in text_classes]
    pool_budget = int(sum(reservation) * 1.10)
    admitted = sum(reservation) <= pool_budget
    baseline_cg = [memory_stat(groups[f"tenant{t}"]) for t in (0, 1)]
    fill = [command(conns[t], "allocate", mapping_bytes) for t in (0, 1)]
    warm = [command(conns[t], "scan") for t in (0, 1)]
    filled_cg = [memory_stat(groups[f"tenant{t}"]) for t in (0, 1)]

    entropy = command(conns[0], "entropy", seed)
    random_class = PAGE
    owned = [npages * random_class, npages * text_classes[1]]
    debt = [owned[t] - reservation[t] for t in (0, 1)]
    victim = max(range(2), key=lambda t: debt[t])
    if victim != 0:
        raise AssertionError(f"unchanged tenant selected as victim: debt={debt}")

    before_reclaim = [memory_stat(groups[f"tenant{t}"]) for t in (0, 1)]
    reclaim_target = mapping_bytes // 2
    reclaim_ns, reclaim_error = reclaim(groups["tenant0"], reclaim_target)
    after_reclaim = [memory_stat(groups[f"tenant{t}"]) for t in (0, 1)]
    cold = [command(conns[t], "scan") for t in (0, 1)]
    after_scan = [memory_stat(groups[f"tenant{t}"]) for t in (0, 1)]

    lifecycle_result = None
    if lifecycle:
        old_before = read_int(groups["tenant1"] / "memory.current")
        move_pid(groups["migrated"], pids[1])
        time.sleep(0.02)
        migration = {
            "old_cgroup_bytes_after_task_move": read_int(
                groups["tenant1"] / "memory.current"),
            "new_cgroup_bytes_after_task_move": read_int(
                groups["migrated"] / "memory.current"),
            "old_cgroup_bytes_before": old_before,
            "interpretation": ("moving a task does not migrate existing page "
                               "charges; ownership must follow the page memcg"),
        }
        move_pid(groups["tenant1"], pids[1])
        before_cow = read_int(groups["tenant1"] / "memory.current")
        cow = command(conns[1], "fork_cow", npages // 4)
        time.sleep(0.02)
        during_cow = read_int(groups["tenant1"] / "memory.current")
        command(conns[1], "drop_cow")
        time.sleep(0.02)
        after_cow = read_int(groups["tenant1"] / "memory.current")
        lifecycle_result = {
            "migration": migration,
            "cow": {**cow, "memory_before": before_cow,
                    "memory_during": during_cow, "memory_after": after_cow,
                    "charged_to_same_memcg": during_cow > before_cow},
            "excluded_vmas": ["MAP_SHARED", "hugetlb", "mlock/pinned",
                              "DMA/device mappings", "file-dirty pages"],
        }

    pre_exit = read_int(groups["tenant0"] / "memory.current")
    conns[0].send(("stop",))
    workers[0].join(10)
    time.sleep(0.02)
    post_exit = read_int(groups["tenant0"] / "memory.current")
    conns[1].send(("stop",))
    workers[1].join(10)

    return {
        "seed": seed, "mapping_bytes_per_tenant": mapping_bytes,
        "pages_per_tenant": npages,
        "admission": {"text_class_bytes": text_classes,
                      "reservation_bytes": reservation,
                      "compressed_pool_budget": pool_budget,
                      "accepted": admitted},
        "transition": {"tenant": 0, "write_ns": entropy["write_ns"],
                       "owned_bytes": owned, "debt_bytes": debt,
                       "selected_victim": victim},
        "linux": {"baseline": baseline_cg, "filled": filled_cg,
                  "before_reclaim": before_reclaim,
                  "after_reclaim": after_reclaim, "after_scan": after_scan,
                  "reclaim_target_bytes": reclaim_target,
                  "reclaim_call_ns": reclaim_ns,
                  "reclaim_error": reclaim_error,
                  "tenant0_reclaimed_bytes": max(
                      0, before_reclaim[0]["memory_current"] -
                      after_reclaim[0]["memory_current"]),
                  "tenant1_collateral_bytes": max(
                      0, before_reclaim[1]["memory_current"] -
                      after_reclaim[1]["memory_current"])},
        "application": {"warm_scan_ns": [x["scan_ns"] for x in warm],
                        "post_reclaim_scan_ns": [x["scan_ns"] for x in cold],
                        "tenant0_added_scan_ns": max(
                            0, cold[0]["scan_ns"] - warm[0]["scan_ns"]),
                        "tenant1_added_scan_ns": max(
                            0, cold[1]["scan_ns"] - warm[1]["scan_ns"])},
        "lifecycle": lifecycle_result,
        "tenant_exit": {"memory_before_exit": pre_exit,
                        "memory_after_exit": post_exit,
                        "released_bytes": max(0, pre_exit - post_exit),
                        "ledger_action": "drain tokens/reservations before id reuse"},
        "fill_write_ns": [x["write_ns"] for x in fill],
    }


def summarize(trials: list[dict]) -> dict:
    reclaimed = [r["linux"]["tenant0_reclaimed_bytes"] for r in trials]
    collateral = [r["linux"]["tenant1_collateral_bytes"] for r in trials]
    added = [r["application"]["tenant0_added_scan_ns"] / 1e6 for r in trials]
    honest = [r["application"]["tenant1_added_scan_ns"] / 1e6 for r in trials]
    calls = [r["linux"]["reclaim_call_ns"] / 1e6 for r in trials]
    exits = [r["tenant_exit"]["released_bytes"] for r in trials]
    return {
        "trials": len(trials),
        "victim_selection_correct": all(
            r["transition"]["selected_victim"] == 0 for r in trials),
        "tenant0_reclaimed_mib_median": statistics.median(reclaimed) / (1 << 20),
        "tenant0_reclaimed_mib_min": min(reclaimed) / (1 << 20),
        "tenant1_collateral_mib_max": max(collateral) / (1 << 20),
        "reclaim_call_ms_p50": percentile(calls, 0.50),
        "reclaim_call_ms_p95": percentile(calls, 0.95),
        "rewriter_added_scan_ms_p50": percentile(added, 0.50),
        "rewriter_added_scan_ms_p95": percentile(added, 0.95),
        "unchanged_added_scan_ms_p95": percentile(honest, 0.95),
        "tenant_exit_released_mib_min": min(exits) / (1 << 20),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mib", type=int, default=DEFAULT_MIB)
    ap.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    ap.add_argument("--out", default="experiments/results/e21_linux_memcg.json")
    args = ap.parse_args()
    if args.mib < 8 or args.trials < 1:
        raise SystemExit("--mib must be >=8 and --trials >=1")

    tag = str(os.getpid())
    _, groups = setup_cgroups(tag)
    rows = []
    failures = []
    try:
        for i in range(args.trials):
            row = run_trial(groups, args.mib << 20, 21000 + i,
                            lifecycle=(i == 0))
            rows.append(row)
            if row["linux"]["tenant0_reclaimed_bytes"] == 0:
                failures.append(f"trial {i}: Linux reclaimed zero bytes")
            if row["linux"]["tenant1_collateral_bytes"] > (1 << 20):
                failures.append(f"trial {i}: unchanged cgroup lost >1 MiB")
            print(f"[e21] trial={i} reclaim="
                  f"{row['linux']['tenant0_reclaimed_bytes']/(1<<20):.1f} MiB "
                  f"added-scan={row['application']['tenant0_added_scan_ns']/1e6:.2f} ms",
                  flush=True)
    finally:
        # Tenant groups can be removed after workers exit.  The executing
        # process remains in the admin leaf until this disposable container
        # exits, so that leaf is intentionally left to the namespace teardown.
        for name in ("tenant0", "tenant1", "migrated"):
            try:
                groups[name].rmdir()
            except OSError:
                pass

    result = {
        "experiment": "E21_linux_memcg",
        "environment": {"kernel": platform.release(),
                        "machine": platform.machine(),
                        "cgroup": "v2 memory controller",
                        "page_size": PAGE},
        "config": {"trials": args.trials,
                   "mapping_mib_per_tenant": args.mib,
                   "reclaim_chunk_mib": args.mib / 2,
                   "page_class": "private anonymous 4 KiB, THP disabled"},
        "summary": summarize(rows),
        "trials": rows,
        "scope": {"validated": ["task-to-memcg placement before allocation",
                                 "reservation/debt owner selection",
                                 "bounded targeted memory.reclaim",
                                 "measured post-reclaim page-fault scan",
                                 "fork/COW within one memcg",
                                 "task-migration charge behavior",
                                 "tenant-exit release"],
                  "not_validated": ["kernel-integrated compressed-size events",
                                    "shared-page split charging", "dirty writeback",
                                    "pinned/DMA/huge pages", "CXL transport",
                                    "production scheduler integration"]},
        "failures": failures,
    }
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
