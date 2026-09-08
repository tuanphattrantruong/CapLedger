#!/usr/bin/env python3
"""E22: live Redis outcome after bounded memcg reclaim of a preselected target.

Run inside the privileged disposable container created by run_e22_redis.sh.
Two independent Redis servers receive identical private working sets and are
placed in separate cgroup-v2 memory domains before loading.  The controller
reclaims only tenant 0, then concurrently probes both servers to distinguish
target recovery work from collateral application impact. This experiment does
not exercise or validate CapLedger victim selection.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import threading
import time
from pathlib import Path


def quantile(xs: list[float], q: float) -> float:
    ys = sorted(xs); pos = (len(ys) - 1) * q
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    return ys[lo] if lo == hi else ys[lo] * (hi - pos) + ys[hi] * (pos - lo)


def resp(*parts: bytes | str | int) -> bytes:
    encoded = [str(x).encode() if not isinstance(x, bytes) else x for x in parts]
    return b"*" + str(len(encoded)).encode() + b"\r\n" + b"".join(
        b"$" + str(len(x)).encode() + b"\r\n" + x + b"\r\n" for x in encoded)


def read_reply(stream):
    lead = stream.read(1)
    if lead in (b"+", b":"):
        return stream.readline().rstrip(b"\r\n")
    if lead == b"$":
        n = int(stream.readline()); data = stream.read(n); stream.read(2); return data
    if lead == b"-":
        raise RuntimeError(stream.readline().decode().strip())
    raise RuntimeError(f"bad Redis reply {lead!r}")


class RedisConn:
    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.stream = self.sock.makefile("rb")

    def command(self, *parts):
        self.sock.sendall(resp(*parts)); return read_reply(self.stream)

    def close(self):
        self.stream.close(); self.sock.close()


def move_pid(cg: Path, pid: int) -> None:
    (cg / "cgroup.procs").write_text(f"{pid}\n")


def setup_cgroups(tag: str) -> tuple[Path, Path]:
    root = Path("/sys/fs/cgroup")
    if platform.system() != "Linux" or not (root / "cgroup.controllers").exists():
        raise RuntimeError("E22 requires Linux cgroup v2")
    admin = root / f"ledger_redis_admin_{tag}"; admin.mkdir()
    for raw in (root / "cgroup.procs").read_text().split():
        try: move_pid(admin, int(raw))
        except (FileNotFoundError, ProcessLookupError, PermissionError): pass
    (root / "cgroup.subtree_control").write_text("+memory\n")
    groups = []
    for tenant in (0, 1):
        cg = root / f"ledger_redis_tenant{tenant}_{tag}"; cg.mkdir(); groups.append(cg)
    return groups[0], groups[1]


def stat(cg: Path) -> dict[str, int]:
    out = {k: int(v) for k, v in
           (line.split() for line in (cg / "memory.stat").read_text().splitlines())}
    out["current"] = int((cg / "memory.current").read_text())
    swap = cg / "memory.swap.current"
    out["swap"] = int(swap.read_text()) if swap.exists() else 0
    return out


def start_redis(port: int, cg: Path) -> subprocess.Popen:
    proc = subprocess.Popen([
        "redis-server", "--port", str(port), "--bind", "127.0.0.1",
        "--protected-mode", "no", "--save", "", "--appendonly", "no",
        "--daemonize", "no", "--loglevel", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    move_pid(cg, proc.pid)
    for _ in range(100):
        try:
            c = RedisConn(port); c.command("PING"); c.close(); return proc
        except OSError: time.sleep(0.02)
    proc.terminate(); raise RuntimeError("Redis did not start")


def deterministic_value(index: int, size: int) -> bytes:
    # Distinct but deterministic payloads avoid a single shared Python object
    # and give Redis a realistic private anonymous working set.
    seed = (index * 0x9E3779B1) & 0xffffffff
    word = seed.to_bytes(4, "little") + ((seed ^ 0xa5a5a5a5) & 0xffffffff).to_bytes(4, "little")
    return (word * (size // len(word) + 1))[:size]


def load_dataset(port: int, keys: int, value_bytes: int) -> None:
    c = RedisConn(port)
    batch = 128
    for start in range(0, keys, batch):
        stop = min(start + batch, keys)
        c.sock.sendall(b"".join(resp("SET", f"k:{i}",
                                      deterministic_value(i, value_bytes))
                                for i in range(start, stop)))
        for _ in range(start, stop):
            if read_reply(c.stream) != b"OK": raise RuntimeError("SET failed")
    c.close()


def benchmark(port: int, keys: int, operations: int, salt: int) -> dict:
    c = RedisConn(port); latency = []; errors = 0; checksum = 0
    t0 = time.perf_counter_ns()
    for i in range(operations):
        key = (i * 8191 + salt * 131) % keys
        q0 = time.perf_counter_ns()
        try:
            value = c.command("GET", f"k:{key}")
            if value is None: errors += 1
            else: checksum ^= value[0]
        except Exception: errors += 1
        latency.append(time.perf_counter_ns() - q0)
    elapsed = time.perf_counter_ns() - t0; c.close()
    return {"operations": operations, "errors": errors, "checksum": checksum,
            "throughput_ops_s": operations / (elapsed / 1e9),
            "p50_us": statistics.median(latency) / 1000,
            "p95_us": quantile(latency, .95) / 1000,
            "p99_us": quantile(latency, .99) / 1000}


def concurrent_bench(ports: tuple[int, int], keys: int, operations: int,
                     salt: int) -> list[dict]:
    barrier = threading.Barrier(2); out = [None, None]
    def work(i):
        barrier.wait(); out[i] = benchmark(ports[i], keys, operations, salt + i)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    return out


def reclaim(cg: Path, amount: int) -> dict:
    before = stat(cg); t0 = time.perf_counter_ns(); error = None
    try: (cg / "memory.reclaim").write_text(f"{amount} swappiness=max\n")
    except OSError as exc:
        error = str(exc)
        try: (cg / "memory.reclaim").write_text(f"{amount}\n")
        except OSError as retry: error += f"; retry: {retry}"
    elapsed = time.perf_counter_ns() - t0; after = stat(cg)
    return {"requested_bytes": amount, "reclaimed_bytes": max(0, before["current"] - after["current"]),
            "call_ms": elapsed / 1e6, "error": error, "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--keys", type=int, default=8192)
    ap.add_argument("--value-bytes", type=int, default=2048)
    ap.add_argument("--operations", type=int, default=512)
    args = ap.parse_args()
    groups = setup_cgroups(str(os.getpid()))
    ports = (6387, 6388); trials = []
    for trial in range(args.trials):
        procs = [start_redis(ports[i], groups[i]) for i in range(2)]
        try:
            for port in ports: load_dataset(port, args.keys, args.value_bytes)
            concurrent_bench(ports, args.keys, 256, trial)  # warm
            baseline = concurrent_bench(ports, args.keys, args.operations, trial + 10)
            before = [stat(g) for g in groups]
            rec = reclaim(groups[0], 8 * 2**20)
            after_reclaim = [stat(g) for g in groups]
            post = concurrent_bench(ports, args.keys, args.operations, trial + 20)
            after_probe = [stat(g) for g in groups]
            trials.append({"trial": trial, "baseline": baseline, "reclaim": rec,
                           "post_reclaim": post, "before": before,
                           "after_reclaim": after_reclaim, "after_probe": after_probe,
                           "unchanged_cgroup_delta_bytes": after_reclaim[1]["current"] - before[1]["current"]})
        finally:
            for proc in procs:
                proc.terminate()
            for proc in procs:
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            time.sleep(.05)

    target_p95_ratio = [t["post_reclaim"][0]["p95_us"] / t["baseline"][0]["p95_us"] for t in trials]
    control_p95_ratio = [t["post_reclaim"][1]["p95_us"] / t["baseline"][1]["p95_us"] for t in trials]
    reclaimed = [t["reclaim"]["reclaimed_bytes"] for t in trials]
    out = {"experiment": "E22_live_redis_memcg_reclaim",
           "redis_version": subprocess.check_output(["redis-server", "--version"], text=True).strip(),
           "configuration": vars(args) | {"dataset_mib_per_tenant": args.keys * args.value_bytes / 2**20,
                                           "reclaim_target_mib": 8},
           "summary": {"trials": len(trials),
                       "successful_reclaims": sum(x > 0 for x in reclaimed),
                       "median_reclaimed_mib": statistics.median(reclaimed) / 2**20,
                       "min_reclaimed_mib": min(reclaimed) / 2**20,
                       "target_post_over_baseline_p95_median": statistics.median(target_p95_ratio),
                       "target_post_over_baseline_p95_max": max(target_p95_ratio),
                       "control_post_over_baseline_p95_median": statistics.median(control_p95_ratio),
                       "control_post_over_baseline_p95_max": max(control_p95_ratio),
                       "total_get_errors": sum(b["errors"] for t in trials for b in t["baseline"] + t["post_reclaim"]),
                       "unchanged_cgroup_reclaim_max_mib": max(0, max(-t["unchanged_cgroup_delta_bytes"] for t in trials)) / 2**20},
           "trials": trials,
           "scope": ("live Redis GET outcome after bounded Linux cgroup-v2 "
                     "reclaim of preselected tenant 0; does not validate "
                     "CapLedger victim selection, compressed hardware, or a "
                     "production SLO certification")}
    path = Path("experiments/results/e22_redis_memcg.json")
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out["summary"], indent=2))
    return 0 if out["summary"]["successful_reclaims"] == args.trials and out["summary"]["total_get_errors"] == 0 else 1


if __name__ == "__main__": raise SystemExit(main())
