#!/usr/bin/env python3
"""E23: completion-to-reclaim integration with a live Redis workload.

This experiment runs in the disposable privileged Linux container created by
``run_e23_linux.sh``.  It connects the artifact's C11 completion queue and loss
tracker to a small host controller, a real cgroup-v2 reclaim actuator, and two
continuously queried Redis servers.  The size records are derived from the
exact values written to Redis in the same run; they are still controller-
supplied records, not zswap or hardware completions.

Three victim-selection policies see the same logical transition:

* ``age`` models a global age-first software tier.  Rewritten pages are young,
  so the unchanged tenant is selected.
* ``exact`` uses exact per-tenant sizes, without a statistical admission price.
* ``ledger`` (the stable machine-readable name for CapLedger) consumes
  token/generation/class/sequence completions, records a
  modeled empirical-admission would-block condition after an injected sequence
  gap, uses a controller-oracle snapshot to resync, and selects the
  positive-debt tenant. It does not call ``contract_admit``.

The experiment measures GET throughput and p99 latency over a short,
shared-host transition-and-reclaim interval while three bounded reclaim
requests run per trial. These repeated probes are descriptive, not a sustained
rate or application-SLO measurement. It is intentionally a software
integration test, not a kernel compressed-size hook or a port of zswap/TMO.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import threading
import time
import zlib
from pathlib import Path


PAGE = 4096
CLASS = 128
Q = 0.99
POLICIES = ("age", "exact", "ledger")


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] if lo == hi else xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def class_bytes(value: bytes) -> int:
    n = len(zlib.compress(value, 1))
    return PAGE if n >= PAGE else int(math.ceil(n / CLASS) * CLASS)


def empirical_reservation(sizes: list[int], q: float = Q) -> int:
    """The paper's normal i.i.d. price, explicitly an empirical estimate."""
    n = len(sizes)
    mu = statistics.fmean(sizes)
    sigma = statistics.stdev(sizes) if n > 1 else 0.0
    # z_0.99, written explicitly to avoid a scipy dependency in the image.
    return min(n * PAGE, math.ceil(n * mu + 2.326347874 * sigma * math.sqrt(n)))


class FeedbackRecord(ctypes.Structure):
    _fields_ = [("token", ctypes.c_uint64),
                ("sequence", ctypes.c_uint32),
                ("generation", ctypes.c_uint16),
                ("size_class", ctypes.c_uint8),
                ("status", ctypes.c_uint8)]


class FeedbackTracker(ctypes.Structure):
    _fields_ = [("expected_sequence", ctypes.c_uint32),
                ("telemetry_debt_bytes", ctypes.c_uint64),
                ("gap_records", ctypes.c_uint64),
                ("stale_records", ctypes.c_uint64)]


class CompletionPath:
    """ctypes wrapper around src/ctier/feedback_queue.c."""

    def __init__(self, library: Path, capacity: int):
        self.lib = ctypes.CDLL(str(library))
        L = self.lib
        L.ct_feedback_create.argtypes = [ctypes.c_uint32]
        L.ct_feedback_create.restype = ctypes.c_void_p
        L.ct_feedback_destroy.argtypes = [ctypes.c_void_p]
        L.ct_feedback_emit.argtypes = [ctypes.c_void_p, ctypes.c_uint64,
                                       ctypes.c_uint16, ctypes.c_uint8,
                                       ctypes.c_uint8]
        L.ct_feedback_emit.restype = ctypes.c_int
        L.ct_feedback_poll.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(FeedbackRecord)]
        L.ct_feedback_poll.restype = ctypes.c_int
        L.ct_feedback_dropped.argtypes = [ctypes.c_void_p]
        L.ct_feedback_dropped.restype = ctypes.c_uint64
        L.ct_feedback_tracker_init.argtypes = [ctypes.POINTER(FeedbackTracker)]
        L.ct_feedback_tracker_apply.argtypes = [ctypes.POINTER(FeedbackTracker),
                                                ctypes.POINTER(FeedbackRecord),
                                                ctypes.c_uint64]
        L.ct_feedback_tracker_apply.restype = ctypes.c_int
        L.ct_feedback_tracker_resync.argtypes = [ctypes.POINTER(FeedbackTracker),
                                                 ctypes.c_uint32]
        self.queue = L.ct_feedback_create(capacity)
        if not self.queue:
            raise RuntimeError("ct_feedback_create failed")
        self.tracker = FeedbackTracker()
        L.ct_feedback_tracker_init(ctypes.byref(self.tracker))

    def close(self) -> None:
        if self.queue:
            self.lib.ct_feedback_destroy(self.queue)
            self.queue = None

    def emit(self, token: int, generation: int, size: int) -> bool:
        k = max(0, min(31, size // CLASS - 1))
        status = 2 if size == PAGE else (1 if size == 0 else 0)
        return self.lib.ct_feedback_emit(self.queue, token, generation,
                                         k, status) == 0

    def poll_one(self) -> tuple[FeedbackRecord | None, int]:
        rec = FeedbackRecord()
        rc = self.lib.ct_feedback_poll(self.queue, ctypes.byref(rec))
        if rc < 0:
            raise RuntimeError(f"ct_feedback_poll returned {rc}")
        if rc == 0:
            return None, 0
        gap = self.lib.ct_feedback_tracker_apply(
            ctypes.byref(self.tracker), ctypes.byref(rec), PAGE)
        return rec, gap

    def resync(self, next_sequence: int) -> None:
        self.lib.ct_feedback_tracker_resync(ctypes.byref(self.tracker),
                                            next_sequence)

    @property
    def dropped(self) -> int:
        return int(self.lib.ct_feedback_dropped(self.queue))


def resp(*parts: bytes | str | int) -> bytes:
    encoded = [x if isinstance(x, bytes) else str(x).encode() for x in parts]
    return b"*" + str(len(encoded)).encode() + b"\r\n" + b"".join(
        b"$" + str(len(x)).encode() + b"\r\n" + x + b"\r\n"
        for x in encoded)


def read_reply(stream):
    lead = stream.read(1)
    if lead in (b"+", b":"):
        return stream.readline().rstrip(b"\r\n")
    if lead == b"$":
        n = int(stream.readline())
        if n < 0:
            return None
        value = stream.read(n)
        stream.read(2)
        return value
    if lead == b"-":
        raise RuntimeError(stream.readline().decode().strip())
    raise RuntimeError(f"bad Redis reply {lead!r}")


class RedisConn:
    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.stream = self.sock.makefile("rb")

    def command(self, *parts):
        self.sock.sendall(resp(*parts))
        return read_reply(self.stream)

    def close(self) -> None:
        self.stream.close()
        self.sock.close()


def move_pid(cgroup: Path, pid: int) -> None:
    (cgroup / "cgroup.procs").write_text(f"{pid}\n")


def setup_cgroups(tag: str) -> tuple[Path, Path]:
    root = Path("/sys/fs/cgroup")
    if platform.system() != "Linux" or not (root / "cgroup.controllers").exists():
        raise RuntimeError("E23 requires Linux cgroup v2")
    if os.geteuid() != 0:
        raise RuntimeError("E23 requires a disposable privileged cgroup namespace")
    admin = root / f"ledger_e23_admin_{tag}"
    admin.mkdir()
    for raw in (root / "cgroup.procs").read_text().split():
        try:
            move_pid(admin, int(raw))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    (root / "cgroup.subtree_control").write_text("+memory\n")
    groups = []
    for tenant in (0, 1):
        group = root / f"ledger_e23_tenant{tenant}_{tag}"
        group.mkdir()
        groups.append(group)
    return groups[0], groups[1]


def memory_stat(cgroup: Path) -> dict[str, int]:
    out = {k: int(v) for k, v in
           (line.split() for line in (cgroup / "memory.stat").read_text().splitlines())}
    out["current"] = int((cgroup / "memory.current").read_text())
    swap = cgroup / "memory.swap.current"
    out["swap"] = int(swap.read_text()) if swap.exists() else 0
    return out


def start_redis(port: int, cgroup: Path) -> subprocess.Popen:
    proc = subprocess.Popen([
        "redis-server", "--port", str(port), "--bind", "127.0.0.1",
        "--protected-mode", "no", "--save", "", "--appendonly", "no",
        "--daemonize", "no", "--loglevel", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    move_pid(cgroup, proc.pid)
    for _ in range(200):
        try:
            conn = RedisConn(port)
            conn.command("PING")
            conn.close()
            return proc
        except OSError:
            time.sleep(0.02)
    proc.terminate()
    raise RuntimeError("Redis did not start")


def compressible_value(index: int, size: int) -> bytes:
    word = (f"ledger:key={index % 97:02d};value=compressible;" * 8).encode()
    return (word * (size // len(word) + 1))[:size]


def entropy_value(trial: int, index: int, size: int) -> bytes:
    return hashlib.shake_256(f"ledger-e23:{trial}:{index}".encode()).digest(size)


def pipeline_sets(port: int, rows: list[tuple[int, bytes]]) -> None:
    conn = RedisConn(port)
    for start in range(0, len(rows), 128):
        block = rows[start:start + 128]
        conn.sock.sendall(b"".join(resp("SET", f"k:{i}", value)
                                    for i, value in block))
        for _ in block:
            if read_reply(conn.stream) != b"OK":
                raise RuntimeError("Redis SET failed")
    conn.close()


class LoadThread(threading.Thread):
    def __init__(self, port: int, keys: int, salt: int, tenant: int,
                 trial: int, value_bytes: int):
        super().__init__(daemon=True)
        self.port, self.keys, self.salt = port, keys, salt
        self.tenant = tenant
        # Redis SET is atomic: during the transition a GET from tenant 0 must
        # return either the complete old value or the complete new value.
        self.old_values = [compressible_value(i, value_bytes)
                           for i in range(keys)]
        self.new_values = ([entropy_value(trial, i, value_bytes)
                            for i in range(keys)] if tenant == 0 else None)
        self.stop_event = threading.Event()
        self.samples: list[tuple[int, int, bool]] = []

    def run(self) -> None:
        conn = RedisConn(self.port)
        i = 0
        try:
            while not self.stop_event.is_set():
                key = (i * 8191 + self.salt * 131) % self.keys
                begin = time.monotonic_ns()
                ok = True
                try:
                    value = conn.command("GET", f"k:{key}")
                    ok = value == self.old_values[key] or (
                        self.new_values is not None and
                        value == self.new_values[key])
                except Exception:
                    ok = False
                end = time.monotonic_ns()
                self.samples.append((end, end - begin, ok))
                i += 1
        finally:
            conn.close()

    def stop(self) -> None:
        self.stop_event.set()


def phase_metrics(samples: list[tuple[int, int, bool]], start: int,
                  end: int) -> dict:
    rows = [row for row in samples if start <= row[0] < end]
    latency = [row[1] / 1000 for row in rows]
    seconds = max(1e-9, (end - start) / 1e9)
    return {"operations": len(rows), "errors": sum(not row[2] for row in rows),
            "throughput_ops_s": len(rows) / seconds,
            "p50_us": quantile(latency, 0.50),
            "p95_us": quantile(latency, 0.95),
            "p99_us": quantile(latency, 0.99)}


def reclaim(cgroup: Path, amount: int) -> dict:
    before = memory_stat(cgroup)
    begin = time.perf_counter_ns()
    error = None
    try:
        (cgroup / "memory.reclaim").write_text(f"{amount} swappiness=max\n")
    except OSError as exc:
        error = f"swappiness=max rejected: {exc}"
        try:
            (cgroup / "memory.reclaim").write_text(f"{amount}\n")
        except OSError as retry:
            error += f"; plain reclaim failed: {retry}"
    elapsed = time.perf_counter_ns() - begin
    after = memory_stat(cgroup)
    return {"requested_bytes": amount,
            "reclaimed_bytes": max(0, before["current"] - after["current"]),
            "call_ms": elapsed / 1e6, "error": error,
            "before_current": before["current"],
            "after_current": after["current"]}


def run_completion_transition(path: CompletionPath, ports: tuple[int, int], trial: int,
                              keys: int, value_bytes: int,
                              initial_sizes: list[int], queue_capacity: int) -> dict:
    """Rewrite tenant 0 and consume the resulting protocol records."""
    def token(tenant: int, index: int) -> int:
        return (tenant << 32) | index

    owners = {token(t, i): t for t in (0, 1) for i in range(keys)}
    current = {token(t, i): initial_sizes[i] for t in (0, 1)
               for i in range(keys)}
    generations = {token(t, i): 1 for t in (0, 1) for i in range(keys)}
    host_sizes = current.copy()
    records_applied = 0
    gaps_seen = 0
    admission_blocked_after_gap = False
    rewritten: list[tuple[int, bytes]] = []

    def drain() -> None:
        nonlocal records_applied, gaps_seen, admission_blocked_after_gap
        while True:
            rec, gap = path.poll_one()
            if rec is None:
                return
            if gap == 2:
                gaps_seen += 1
                admission_blocked_after_gap = path.tracker.telemetry_debt_bytes > 0
            if rec.token not in generations or rec.generation != generations[rec.token]:
                continue
            host_sizes[int(rec.token)] = (int(rec.size_class) + 1) * CLASS
            records_applied += 1

    # The first capacity+8 records intentionally arrive without polling.  The
    # following delivered record makes exactly those eight losses observable.
    # Redis acknowledges each block before its device-style completions become
    # visible.  Polling is intentionally withheld across the first
    # queue_capacity+8 writes to create one observable gap.
    emitted = 0
    for start in range(0, keys, 128):
        rewritten = []
        for index in range(start, min(keys, start + 128)):
            value = entropy_value(trial, index, value_bytes)
            rewritten.append((index, value))
        pipeline_sets(ports[0], rewritten)
        for index, value in rewritten:
            page_token = token(0, index)
            # Rewrite within the same allocation lifetime: generation stays 1.
            size = class_bytes(value)
            current[page_token] = size
            path.emit(page_token, 1, size)
            emitted += 1
            if emitted == queue_capacity + 8:
                drain()
            elif emitted == queue_capacity + 9 or (
                    emitted > queue_capacity and emitted % 256 == 0):
                drain()

    # A small unchanged-tenant rewrite proves that the same opaque-token map
    # distinguishes both memcgs; its sizes stay at the original price.
    control_rows = [(i, compressible_value(i, value_bytes))
                    for i in range(min(16, keys))]
    pipeline_sets(ports[1], control_rows)
    for index, value in control_rows:
        page_token = token(1, index)
        size = class_bytes(value)
        current[page_token] = size
        path.emit(page_token, 1, size)
        emitted += 1
    drain()

    debt_before_resync = int(path.tracker.telemetry_debt_bytes)
    host_owned_before = [sum(size for page_token, size in host_sizes.items()
                             if owners[page_token] == tenant)
                         for tenant in (0, 1)]
    # Snapshot reconciliation is the conservative recovery from queue loss.
    # In E23 this snapshot is the controller oracle over sizes computed from
    # the issued Redis values; a device snapshot ABI remains future work.
    host_sizes = current.copy()
    path.resync(emitted + 1)
    debt_after_resync = int(path.tracker.telemetry_debt_bytes)
    host_owned_after = [sum(size for page_token, size in host_sizes.items()
                            if owners[page_token] == tenant)
                        for tenant in (0, 1)]
    actual_owned_after = [sum(size for page_token, size in current.items()
                              if owners[page_token] == tenant)
                          for tenant in (0, 1)]
    return {"records_issued": emitted, "records_applied": records_applied,
            "records_dropped": path.dropped, "gap_events": gaps_seen,
            "gap_records": int(path.tracker.gap_records),
            "telemetry_debt_before_resync": debt_before_resync,
            "telemetry_debt_after_resync": debt_after_resync,
            "empirical_admission_attempted_after_gap": True,
            "empirical_admission_blocked_after_gap": admission_blocked_after_gap,
            "admission_gate_result": "blocked_pending_snapshot" if admission_blocked_after_gap else "accepted_in_error",
            "snapshot_pages": len(host_sizes),
            "snapshot_source": "controller oracle over sizes of values issued to Redis",
            "host_owned_before_resync": host_owned_before,
            "host_owned_after_resync": host_owned_after,
            "actual_owned_after": actual_owned_after,
            "mean_size_after": statistics.fmean(
                size for page_token, size in current.items()
                if owners[page_token] == 0)}


def run_trial(policy: str, trial: int, groups: tuple[Path, Path],
              ports: tuple[int, int], args) -> dict:
    # Form the optional empirical quote before allocating the requested Redis
    # objects.  This is a deterministic controller-side model of admission,
    # not a call into contract.c and not a calibrated probability guarantee.
    rows = [(i, compressible_value(i, args.value_bytes))
            for i in range(args.keys)]
    initial_sizes = [class_bytes(value) for _, value in rows]
    purchased = empirical_reservation(initial_sizes)
    initial_owned = sum(initial_sizes)
    procs = [start_redis(ports[i], groups[i]) for i in (0, 1)]
    queue = CompletionPath(Path(args.feedback_lib), args.queue_capacity)
    try:
        for port in ports:
            pipeline_sets(port, rows)

        load = [LoadThread(ports[i], args.keys, trial * 7 + i, i, trial,
                           args.value_bytes) for i in (0, 1)]
        for thread in load:
            thread.start()
        load_start = time.monotonic_ns()
        time.sleep(args.baseline_seconds)
        transition_start = time.monotonic_ns()

        completion = run_completion_transition(
            queue, ports, trial, args.keys, args.value_bytes,
            initial_sizes, args.queue_capacity)
        transition_end = time.monotonic_ns()
        # The exact arm has no price: responsibility is footprint growth.  The
        # The CapLedger arm uses the bytes purchased at empirical admission.
        exact_debt = [max(0, completion["actual_owned_after"][0] - initial_owned),
                      max(0, completion["actual_owned_after"][1] - initial_owned)]
        ledger_debt = [max(0, completion["host_owned_after_resync"][t] - purchased)
                       for t in (0, 1)]
        if policy == "age":
            # Epoch 0 is the initial load.  Tenant 0 rewrites every object at
            # epoch 1; tenant 1 refreshes only 16 control objects.  Selecting
            # the lower median update epoch models an age-first global tier.
            age_epoch = [statistics.median([1] * args.keys),
                         statistics.median(
                             [1] * min(16, args.keys) +
                             [0] * max(0, args.keys - 16))]
            victim = min(range(2), key=lambda t: age_epoch[t])
            decision_basis = ("controlled age model: lowest median object-"
                              "update epoch")
        elif policy == "exact":
            age_epoch = None
            victim = max(range(2), key=lambda t: exact_debt[t])
            decision_basis = "exact per-tenant compressed bytes; no statistical gate"
        else:
            age_epoch = None
            victim = max(range(2), key=lambda t: ledger_debt[t])
            decision_basis = "completion-derived owned bytes minus purchased bytes"

        cycles = []
        remaining = ledger_debt[victim] if policy == "ledger" else exact_debt[victim]
        amount = args.reclaim_mib * (1 << 20)
        for cycle in range(args.cycles):
            before_all = [memory_stat(group)["current"] for group in groups]
            result = reclaim(groups[victim], amount)
            after_all = [memory_stat(group)["current"] for group in groups]
            # memory.current is an uncompressed logical-memory proxy.  It
            # cannot discharge compressed-byte debt one-for-one; only tier
            # release completions or a new snapshot could do that.  Preserve
            # the debt so this experiment validates selection/actuation, not
            # compressed-byte repayment.
            cycles.append({"cycle": cycle, "victim": victim, **result,
                           "changed_tenant_delta_bytes": after_all[0] - before_all[0],
                           "unchanged_tenant_delta_bytes": after_all[1] - before_all[1],
                           "unpaid_debt_bytes": remaining,
                           "compressed_debt_discharged_bytes": 0,
                           "debt_reconciliation":
                               "requires compressed-tier release feedback; not memory.current",
                           "shortfall_bytes": max(0, amount - result["reclaimed_bytes"]),
                           "shortfall_policy_executed": False,
                           "prescribed_shortfall_policy":
                               ("retain debt, block empirical admission, notify, "
                                "and retry the debtor; at the hard watermark "
                                "spill globally or reject and record an "
                                "attribution breach")})
            time.sleep(args.between_seconds)
        recovery_end = time.monotonic_ns()
        time.sleep(args.settle_seconds)
        load_end = time.monotonic_ns()
        for thread in load:
            thread.stop()
        for thread in load:
            thread.join(timeout=5)

        baseline = [phase_metrics(thread.samples, load_start, transition_start)
                    for thread in load]
        recovery = [phase_metrics(thread.samples, transition_start, recovery_end)
                    for thread in load]
        settle = [phase_metrics(thread.samples, recovery_end, load_end)
                  for thread in load]
        return {"policy": policy, "trial": trial,
                "configuration": {"keys": args.keys,
                                  "value_bytes": args.value_bytes,
                                  "logical_mib_per_tenant": args.keys * args.value_bytes / (1 << 20),
                                  "cycles": args.cycles,
                                  "reclaim_mib_per_cycle": args.reclaim_mib},
                "pricing": {"q_requested": Q,
                            "interpretation": "empirical target under representative-iid assumption",
                            "quote_stage": "before initial Redis object allocation",
                            "quote_implementation": "controller model, not contract_admit",
                            "initial_owned_bytes": initial_owned,
                            "purchased_bytes": purchased,
                            "exact_debt_bytes": exact_debt,
                            "ledger_debt_bytes": ledger_debt},
                "completion_path": completion,
                "detector": {"role": "advisory only",
                             "mean_ratio": completion["mean_size_after"] /
                                           statistics.fmean(initial_sizes),
                             "triggered": completion["mean_size_after"] >
                                          1.4 * statistics.fmean(initial_sizes)},
                "selection": {"victim": victim, "basis": decision_basis,
                              "median_update_epoch": age_epoch},
                "reclaim_cycles": cycles,
                "application": {"baseline": baseline, "transition_and_recovery": recovery,
                                "settle": settle,
                                "transition_seconds": (transition_end - transition_start) / 1e9}}
    finally:
        queue.close()
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        time.sleep(0.05)


def ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def summarize(trials: list[dict]) -> dict:
    out = {}
    for policy in POLICIES:
        rows = [row for row in trials if row["policy"] == policy]
        target_tp = [ratio(r["application"]["transition_and_recovery"][0]["throughput_ops_s"],
                           r["application"]["baseline"][0]["throughput_ops_s"])
                     for r in rows]
        control_tp = [ratio(r["application"]["transition_and_recovery"][1]["throughput_ops_s"],
                            r["application"]["baseline"][1]["throughput_ops_s"])
                      for r in rows]
        target_p99 = [ratio(r["application"]["transition_and_recovery"][0]["p99_us"],
                            r["application"]["baseline"][0]["p99_us"])
                      for r in rows]
        control_p99 = [ratio(r["application"]["transition_and_recovery"][1]["p99_us"],
                             r["application"]["baseline"][1]["p99_us"])
                       for r in rows]
        reclaim = [cycle["reclaimed_bytes"] / (1 << 20)
                   for row in rows for cycle in row["reclaim_cycles"]]
        out[policy] = {
            "trials": len(rows),
            "selected_changed_tenant": sum(r["selection"]["victim"] == 0 for r in rows),
            "selected_unchanged_tenant": sum(r["selection"]["victim"] == 1 for r in rows),
            "reclaim_cycles": len(reclaim),
            "reclaimed_mib_per_cycle_median": statistics.median(reclaim),
            "reclaim_shortfall_cycles": sum(c["shortfall_bytes"] > 0
                                             for r in rows for c in r["reclaim_cycles"]),
            "changed_throughput_ratio_median": statistics.median(target_tp),
            "unchanged_throughput_ratio_median": statistics.median(control_tp),
            "changed_p99_ratio_median": statistics.median(target_p99),
            "unchanged_p99_ratio_median": statistics.median(control_p99),
            "get_errors": sum(m["errors"] for r in rows
                              for phase in ("baseline", "transition_and_recovery", "settle")
                              for m in r["application"][phase]),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--keys", type=int, default=4096)
    parser.add_argument("--value-bytes", type=int, default=4096)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--reclaim-mib", type=int, default=2)
    parser.add_argument("--queue-capacity", type=int, default=1024)
    parser.add_argument("--baseline-seconds", type=float, default=0.5)
    parser.add_argument("--between-seconds", type=float, default=0.25)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--feedback-lib", default="build/linux/libfeedback.so")
    parser.add_argument("--out", default="experiments/results/e23_end_to_end_redis.json")
    args = parser.parse_args()
    if args.keys <= args.queue_capacity + 9:
        parser.error("keys must exceed queue-capacity+9 to expose the injected gap")

    groups = setup_cgroups(str(os.getpid()))
    ports = (6391, 6392)
    trials = []
    begin = time.time()
    execution_index = 0
    # A balanced rotation removes the fixed age->exact->CapLedger time ordering.
    for trial in range(args.trials):
        order = POLICIES[trial % len(POLICIES):] + POLICIES[:trial % len(POLICIES)]
        for policy in order:
            row = run_trial(policy, trial, groups, ports, args)
            row["execution_index"] = execution_index
            execution_index += 1
            trials.append(row)
            print(f"[e23] {policy} {trial + 1}/{args.trials}: "
                  f"victim={row['selection']['victim']} "
                  f"drops={row['completion_path']['records_dropped']}", flush=True)

    summary = summarize(trials)
    failures = []
    for row in trials:
        cp = row["completion_path"]
        if cp["records_dropped"] != 8 or cp["gap_records"] != 8:
            failures.append(f"{row['policy']}/{row['trial']}: loss injection mismatch")
        if not cp["empirical_admission_blocked_after_gap"]:
            failures.append(f"{row['policy']}/{row['trial']}: admission not blocked")
        if cp["telemetry_debt_after_resync"] != 0:
            failures.append(f"{row['policy']}/{row['trial']}: resync left telemetry debt")
    if any(summary[p]["get_errors"] for p in POLICIES):
        failures.append("Redis GET errors observed")
    if summary["age"]["selected_unchanged_tenant"] != args.trials:
        failures.append("age policy did not select the unchanged tenant")
    for policy in ("exact", "ledger"):
        if summary[policy]["selected_changed_tenant"] != args.trials:
            failures.append(f"{policy} did not select the changed tenant")

    result = {
        "schema": "ccap-results-v1", "experiment": "E23",
        "title": "Completion-to-cgroup-reclaim integration under live Redis load",
        "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_seconds": time.time() - begin,
        "host": {"uname": (f"{platform.system()} {platform.release()} "
                            f"{platform.machine()}"),
                 "python": platform.python_version(),
                 "redis": subprocess.check_output(["redis-server", "--version"],
                                                  text=True).strip()},
        "config": vars(args), "summary": summary, "trials": trials,
        "metric_classes": {
            "measured": ["Redis GET throughput/latency/value-integrity errors",
                         "memory.current deltas", "memory.reclaim call time"],
            "counted": ["completion records/drops/gaps", "selected tenant",
                        "reservation and debt bytes"],
            "modeled": ["empirical admission quote", "age-policy selection",
                        "controller-oracle resynchronization snapshot",
                        "prescribed shortfall response"],
        },
        "scope": ("software end-to-end path; size records are computed from the "
                  "values written to Redis and traverse the shipped C queue, but "
                  "are not emitted by zswap, a kernel page hook, or CXL hardware; "
                  "age is a controlled policy model, not a zswap port"),
        "failures": failures,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"summary": summary, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
