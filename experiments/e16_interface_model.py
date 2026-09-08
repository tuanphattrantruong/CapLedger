#!/usr/bin/env python3
"""E16 - completion-path bandwidth, queue, metadata, and ordering model.

This is an architectural co-simulation around the measured 16-byte software
record path. It does not claim CXL transport or controller timing. It makes the
missing engineering budget explicit and compares lower-bandwidth alternatives.
"""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import common

RECORD_BYTES = 16
PAGE = 4096
MEASURED_HOST_RECORDS_S = 54.24e6


def bandwidth_table() -> list[dict]:
    rows = []
    for gb_s in (64, 128, 256, 512):
        writes = gb_s * 1e9 / PAGE
        for change in (1.0, 0.25, 0.05):
            events = writes * change
            stream = events * RECORD_BYTES
            rows.append({
                "device_write_GB_s": gb_s, "size_change_fraction": change,
                "page_writes_s": writes, "completion_records_s": events,
                "completion_GB_s": stream / 1e9,
                "completion_fraction_of_write_bandwidth": stream / (gb_s * 1e9),
                "measured_host_core_equivalents": events / MEASURED_HOST_RECORDS_S,
            })
    return rows


def queue_table() -> list[dict]:
    rows = []
    for rate in (0.2e6, 5e6, 25e6, 125e6):
        for poll_us in (10, 100, 1000):
            nominal = rate * poll_us / 1e6
            rows.append({
                "records_s": rate, "poll_us": poll_us,
                "nominal_depth": int(nominal + 0.999),
                "two_x_burst_depth": int(2 * nominal + 0.999),
                "two_x_burst_queue_MiB": 2 * nominal * RECORD_BYTES / (1 << 20),
                "pessimistic_headroom_MiB": 2 * nominal * PAGE / (1 << 20),
            })
    return rows


def alternatives() -> list[dict]:
    return [
        {"scheme": "per-page completion", "relative_records": 1.0,
         "keeps_exact_owner": True, "keeps_generation_order": True,
         "loss": "none; highest traffic"},
        {"scheme": "exception-only class change", "relative_records": "p(class changes)",
         "keeps_exact_owner": True, "keeps_generation_order": True,
         "loss": "host must initialize and rescan unchanged pages"},
        {"scheme": "64-page region aggregate", "relative_records": 1 / 64,
         "keeps_exact_owner": False, "keeps_generation_order": False,
         "loss": "cannot charge a page owner or reject stale page generations"},
        {"scheme": "1/64 sampling", "relative_records": 1 / 64,
         "keeps_exact_owner": False, "keeps_generation_order": False,
         "loss": "estimation only; cannot maintain exact owned bytes"},
    ]


def ordering_litmus() -> dict:
    """Enumerate delivery orders around free/reuse.

    A completion belongs to generation 1; the token is then freed and reused as
    generation 2. The safe consumer rejects generation-1 records after reuse.
    The broken consumer uses token alone and can charge the new owner.
    """
    events = ("completion_g1", "free_g1", "reuse_g2")
    safe_bad = broken_bad = valid_orders = 0
    examples = []
    for order in itertools.permutations(events):
        if order.index("free_g1") > order.index("reuse_g2"):
            continue
        valid_orders += 1
        safe_owner = broken_owner = "g1"
        safe_gen = broken_gen = 1
        safe_charge = {"g1": 0, "g2": 0}
        broken_charge = {"g1": 0, "g2": 0}
        for ev in order:
            if ev == "reuse_g2":
                safe_owner = broken_owner = "g2"
                safe_gen = broken_gen = 2
            elif ev == "completion_g1":
                if safe_gen == 1:
                    safe_charge[safe_owner] += 4096
                # stale generation is rejected and becomes gap debt otherwise
                broken_charge[broken_owner] += 4096
        if safe_charge["g2"]:
            safe_bad += 1
        if broken_charge["g2"]:
            broken_bad += 1
            examples.append({"order": order, "wrong_charge": broken_charge["g2"]})
    return {"valid_orders": valid_orders,
            "safe_generation_protocol_wrong_owner": safe_bad,
            "broken_token_only_wrong_owner": broken_bad,
            "broken_examples": examples,
            "required_relation": ("free/migration waits through the last issued "
                "sequence; reuse increments generation; stale records create "
                "pessimistic debt until snapshot resynchronization")}


def metadata_table() -> list[dict]:
    logical_pages_tib = (1 << 40) // PAGE
    rows = []
    for active in (1.0, 0.25, 0.10, 0.01):
        flat = logical_pages_tib * 16 * active
        rows.append({"active_fraction": active,
                     "pte_metadata_GiB_per_TiB_logical": flat / (1 << 30),
                     "note": "16-byte host/device page record; excludes extents, queues, windows"})
    return rows


def main() -> int:
    ap = common.make_argparser("E16 completion interface model")
    args = ap.parse_args()
    t0 = time.perf_counter_ns()
    data = {"bandwidth": bandwidth_table(), "queues": queue_table(),
            "alternatives": alternatives(), "ordering": ordering_litmus(),
            "metadata": metadata_table(),
            "measured_anchor": {"software_records_s": MEASURED_HOST_RECORDS_S,
                                 "record_bytes": RECORD_BYTES},
            "scope": ("analytical queue/link accounting plus ordering litmus; "
                      "not an FPGA, CXL transport, interrupt, DMA, coherence, "
                      "or controller-SRAM measurement")}
    failures = []
    if data["ordering"]["safe_generation_protocol_wrong_owner"] != 0:
        failures.append("generation-safe ordering litmus misattributed a record")
    common.write_result(Path(args.out) / "e16_interface_model.json", "E16",
                        "Completion bandwidth, queue, metadata, and ordering model",
                        {"record_bytes": RECORD_BYTES,
                         "measured_host_records_s": MEASURED_HOST_RECORDS_S,
                         "write_bandwidths_GB_s": [64, 128, 256, 512],
                         "poll_intervals_us": [10, 100, 1000]},
                        [], data,
                        modeled_fields=["data.bandwidth", "data.queues",
                                        "data.metadata"],
                        failures=failures,
                        wall_seconds=(time.perf_counter_ns() - t0) / 1e9)
    print(json.dumps({"ordering": data["ordering"],
                      "max_completion_GB_s": max(
                          r["completion_GB_s"] for r in data["bandwidth"])},
                     indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
