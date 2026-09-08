#!/usr/bin/env python3
"""Check the paper's load-bearing claims against experiments/results/*.json.

`paper/data/tables.py` guarantees that every number PRINTED in the paper comes
from a results file. This script checks the complementary thing: that the
paper's *claims* -- the qualitative statements the argument rests on -- are
actually true of those files. Each check names the claim it defends, so a
failure says which sentence in the paper is now wrong rather than which
assertion tripped.

Run:  python3 experiments/verify_claims.py
      python3 experiments/verify_claims.py --strict
Exit: 0 if every checked claim holds. By default a missing result is reported
      as SKIP so an isolated subset is checkable. ``--strict`` additionally
      validates artifact/manifest.json and turns every skip into a failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "experiments" / "results"
DEFAULT_MANIFEST = ROOT / "artifact" / "manifest.json"
RES = DEFAULT_RESULTS
checks: list[tuple[str, str, object]] = []


def load(name):
    with open(RES / name) as f:
        return json.load(f)["data"]


def load_document(name):
    with open(RES / name) as f:
        return json.load(f)


def claim(section: str, text: str):
    def deco(fn):
        checks.append((section, text, fn))
        return fn
    return deco


def _lookup(document: dict, dotted: str):
    value = document
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


def validate_manifest(path: Path) -> list[str]:
    """Validate the complete retained-evidence set declared by *path*.

    The manifest deliberately describes legacy result formats as well as the
    current ccap-results-v1 format. Presence, JSON parseability, explicitly
    declared schemas/keys, and recorded failures are strict; converting old
    result files to a new schema is a separate provenance-preserving task.
    """
    errors: list[str] = []
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return [f"manifest is missing: {path}"]
    except json.JSONDecodeError as exc:
        return [f"manifest is not valid JSON: {path}: {exc}"]
    if not isinstance(document, dict):
        return ["manifest root must be a JSON object"]
    if document.get("schema") != "ledger-artifact-manifest-v1":
        errors.append("manifest schema is not ledger-artifact-manifest-v1")
    entries = document.get("required_results")
    if not isinstance(entries, list) or not entries:
        return errors + ["manifest required_results must be a non-empty list"]

    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            errors.append(f"manifest entry {index} has no string file")
            continue
        name = entry["file"]
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            errors.append(f"manifest entry is not a safe relative path: {name}")
            continue
        if name in seen:
            errors.append(f"manifest lists {name} more than once")
            continue
        seen.add(name)
        result_path = RES / rel
        label = entry.get("id", name)
        try:
            payload = json.loads(result_path.read_text())
        except FileNotFoundError:
            errors.append(f"{label}: missing {name}")
            continue
        except json.JSONDecodeError as exc:
            errors.append(f"{label}: malformed JSON in {name}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{label}: {name} is not a JSON object")
            continue
        expected_schema = entry.get("result_schema")
        if expected_schema is not None and payload.get("schema") != expected_schema:
            errors.append(
                f"{label}: {name} schema {payload.get('schema')!r}, "
                f"expected {expected_schema!r}")
        required_keys = entry.get("required_keys", [])
        if not isinstance(required_keys, list) or not all(
                isinstance(key, str) for key in required_keys):
            errors.append(f"{label}: required_keys must be a string list")
            continue
        for key in required_keys:
            try:
                _lookup(payload, key)
            except KeyError:
                errors.append(f"{label}: {name} is missing key {key}")
        if payload.get("failures"):
            errors.append(f"{label}: {name} records failures: {payload['failures']}")
    return errors


# ----------------------------------------------------------------- claims --
@claim("2.2", "encryption at rest destroys a large share of real tenants' "
              "effective capacity without added application records")
def c_real_drift():
    cen = load("e7_realstate.json")["census"]
    out = []
    for plain, enc in (("redis_plain", "redis_encrypted"),
                       ("kvheap_plain", "kvheap_encrypted"),
                       ("sqlite_baseline", "sqlite_encrypted")):
        a = cen[plain]["zlib"]["capacity_multiplier"]
        b = cen[enc]["zlib"]["capacity_multiplier"]
        assert a > 1.5, f"{plain} multiplier only {a:.2f}: no capacity to lose"
        # The claim is that a large share of capacity is destroyed, not that
        # the encrypted state lands at exactly 1.0x. It does not always:
        # DEFLATE occasionally shaves a few percent off ciphertext, and the
        # tier's size classes keep that. Test the claim, not the incidental.
        assert a / b > 1.8, f"{plain}->{enc}: only {a:.2f}x -> {b:.2f}x"
        out.append(f"{plain.split('_')[0]} {a:.2f}->{b:.2f}x "
                   f"(-{100*(1-b/a):.0f}%)")
    return "; ".join(out)


@claim("2.2", "the advertised multiplier depends on the compressor")
def c_codec():
    c = load("e7_realstate.json")["census"]["redis_plain"]
    lo = min(c[k]["capacity_multiplier"] for k in ("zlib", "lz4", "zstd"))
    hi = max(c[k]["capacity_multiplier"] for k in ("zlib", "lz4", "zstd"))
    assert hi / lo > 1.3, f"codec spread only {hi/lo:.2f}x"
    return f"{lo:.2f}x..{hi:.2f}x on identical bytes"


@claim("5.1", "a mean-based reserve is exceeded on ~half of admissions in "
              "every tested cell")
def c_mean_is_coinflip():
    cells = [c for c in load("e6_regime.json")["cells"] if c["q"] == 0.5]
    assert cells, "no q=0.5 cells"
    lo, hi = min(c["empirical"] for c in cells), max(c["empirical"] for c in cells)
    assert 0.35 < lo and hi < 0.70, f"q=0.5 rates span {lo:.2f}..{hi:.2f}"
    return f"{len(cells)} cells, {lo:.2f}..{hi:.2f}"


@claim("5.1", "window moments improve the large-batch E6 estimate over EWMA")
def c_ring_beats_ewma():
    cells = load("e6_regime.json")["cells"]
    n = max(c["n"] for c in cells)
    ew = [c["empirical"] for c in cells
          if c["q"] == 0.99 and c["arm"] == "analytic" and c["n"] == n]
    rg = [c["empirical"] for c in cells
          if c["q"] == 0.99 and c["arm"] == "analytic_ring" and c["n"] == n]
    assert min(ew) > 4 * max(rg), \
        f"at n={n}: EWMA {min(ew):.3f} vs ring {max(rg):.3f}"
    return f"at n={n}: EWMA {min(ew):.2f}-{max(ew):.2f}, ring {min(rg):.3f}-{max(rg):.3f}"


@claim("5.1", "the i.i.d. matched-ratio mean is near target through n/W=1/8 "
              "and degrades far beyond the window")
def c_window_rule():
    wc = load("e6_regime.json").get("window_cells")
    if not wc:
        # A measurement that has not been taken is not a refuted claim.
        raise FileNotFoundError(2, "not run", "e6_regime.json:window_cells")
    grouped = {}
    for cell in wc:
        grouped.setdefault(cell["n_over_window"], []).append(cell["empirical"])
    curve = {ratio: sum(values) / len(values)
             for ratio, values in grouped.items()}
    inside = [rate for ratio, rate in curve.items() if ratio <= 0.125]
    outside = [c["empirical"] for c in wc if c["n_over_window"] >= 4.0]
    assert inside and outside, "sweep does not cover n/W <= 1/8 and >= 4"
    assert max(inside) <= 0.015, \
        f"mean rate through n/W=1/8 is {max(inside):.3f}"
    assert max(inside) < min(outside), \
        f"inside n<=W/8: {max(inside):.3f}, outside n>=4W: {min(outside):.3f}"
    return f"group mean n<=W/8: <={max(inside):.3f}; n>=4W: >={min(outside):.3f}"


@claim("5.2", "the contract charges the liar and the software-tier baseline "
              "does not")
def c_attribution():
    iso = {r["label"]: r for r in load("e5_multitenant.json")["isolation_summary"]}
    for k in ("analytic_ring_q0.99", "analytic_q0.99", "optimistic"):
        if k in iso:
            assert iso[k]["total_victim_violations_mean"] == 0, \
                f"{k} charged victims"
            assert iso[k]["bomber_violations_mean"] > 0, f"{k} charged nobody"
    z = iso.get("zswap_reactive")
    assert z and z["total_victim_violations_mean"] > 0, \
        "zswap baseline did not misattribute; the comparison is vacuous"
    share = z["total_victim_violations_mean"] / (
        z["total_victim_violations_mean"] + z["bomber_violations_mean"])
    rt = load("e17_reactive_baseline.json")["summary"]
    assert rt["bomber_violations_mean"] > 0, \
        "tenant-aware reactive control generated no recovery"
    return (f"contract victims 0; zswap victims {100*share:.0f}% of recovery; "
            f"tenant-reactive victims {rt['victim_violations_mean']:.0f}")


@claim("5.1", "dependence-aware prices are evaluated on ordered real and "
              "object-grouped traces")
def c_dependence():
    d = load("e14_dependence.json")
    rows = d["rows"]
    assert any(r["kind"] == "stationary_ordered" and
               r["source"].startswith("sqlite_") for r in rows), \
        "no ordered SQLite batches"
    assert any("object_block_64" in r["source"] for r in rows), \
        "no long object-grouped trace"
    iid = d["overall"]["iid_normal"]["rate"]
    ess = d["overall"]["ess_normal"]["rate"]
    assert ess <= iid, f"ESS adjustment worsened aggregate calibration: {ess}>{iid}"
    return f"{len(rows)} ordered batches; iid {iid:.3f}, ESS {ess:.3f}"


@claim("5.3", "detector stress includes oscillation, mean reversion, variance "
              "change, entropy decrease, and below-threshold shaping")
def c_drift_shapes():
    s = load("e15_drift_adversary.json")["summary"]
    shapes = {r["shape"] for r in s}
    required = {"oscillating", "mean_reverting", "variance_only",
                "entropy_decrease", "adaptive_below_k"}
    assert required <= shapes, f"missing shapes: {sorted(required-shapes)}"
    return f"{len(shapes)} shapes, two detectors, periodic exposure bound"


@claim("3", "generation ordering prevents stale completions from charging a "
            "reused page token")
def c_feedback_ordering():
    o = load("e16_interface_model.json")["ordering"]
    assert o["safe_generation_protocol_wrong_owner"] == 0
    assert o["broken_token_only_wrong_owner"] > 0
    return (f"{o['valid_orders']} valid orders; safe 0 wrong-owner, "
            f"token-only {o['broken_token_only_wrong_owner']}")


@claim("5", "the restricted Linux path selects the rewriting memcg, reclaims "
            "real anonymous pages, and leaves the unchanged memcg resident")
def c_linux_memcg():
    with open(RES / "e21_linux_memcg.json") as f:
        d = json.load(f)
    s = d["summary"]
    assert not d["failures"], f"E21 failures: {d['failures']}"
    assert s["victim_selection_correct"], "wrong memcg selected"
    assert s["tenant0_reclaimed_mib_min"] >= 15.0, \
        f"only {s['tenant0_reclaimed_mib_min']:.1f} MiB reclaimed"
    assert s["tenant1_collateral_mib_max"] <= 1.0, \
        f"unchanged memcg lost {s['tenant1_collateral_mib_max']:.1f} MiB"
    life = next(r["lifecycle"] for r in d["trials"] if r["lifecycle"])
    assert life["cow"]["charged_to_same_memcg"], "COW escaped the owner memcg"
    assert life["migration"]["new_cgroup_bytes_after_task_move"] < 1 << 20, \
        "task migration unexpectedly migrated existing page charges"
    return (f"{s['trials']} Linux trials; reclaim median "
            f"{s['tenant0_reclaimed_mib_median']:.1f} MiB; unchanged max "
            f"{s['tenant1_collateral_mib_max']:.1f} MiB")


@claim("5.2", "the transparent-reserve baseline hard-fails where the contract "
              "does not")
def c_transparent():
    iso = {r["label"]: r for r in load("e5_multitenant.json")["isolation_summary"]}
    t = iso.get("transparent_reserve")
    assert t and t["hard_failures_total"] > 0, "transparent baseline never failed"
    for k, r in iso.items():
        if k != "transparent_reserve":
            assert r["hard_failures_total"] == 0, f"{k} hard-failed"
    bt = t.get("hard_failures_by_tenant_total") or []
    innocent = sum(bt[:3]) / sum(bt) if sum(bt) else 0
    return (f"{t['hard_failures_total']} hard failures, "
            f"{100*innocent:.0f}% on innocent tenants; contract 0")


@claim("5.3", "on E8's monotone controls, the shipped ratio threshold detects "
              "only the instantaneous flip and a lower one has no observed alarms")
def c_detector():
    S = load("e8_detector.json")["summary"]
    drifting = [r for r in S if r["drift_rate"] > 0]
    det14 = [r["drift_rate"] for r in drifting
             if r["ratio"]["1.4"]["detected"] == r["ratio"]["1.4"]["of"]]
    assert det14 == [1.0], f"ratio>1.4 fully detected at {det14}"
    det11 = [r["drift_rate"] for r in drifting
             if r["ratio"]["1.1"]["detected"] == r["ratio"]["1.1"]["of"]]
    assert min(det11) <= 0.05, f"ratio>1.1 floor only {min(det11)}"
    fa = max(r["ratio"]["1.1"]["false_positive_rate"] for r in S)
    assert fa == 0.0, f"ratio>1.1 false-alarm rate {fa}"
    return f"1.4 detects only at {det14}; 1.1 down to {min(det11)}, FA={fa}"


@claim("5.5", "three measured emulator properties remain stable and "
              "attribution holds across the tested pool-size sweep")
def c_scale():
    S = load("e10_scale.json")["summary"]
    assert len(S) >= 3, "fewer than three scales measured"
    for r in S:
        assert r["violations_victims_total"] == 0, f"{r['label']} charged victims"
        assert r["hard_failures_total"] == 0, f"{r['label']} hard-failed"
        assert r["invariant_failures_total"] == 0, f"{r['label']} broke the invariant"
    lags = [r["cusum_op_median"] for r in S if r["cusum_op_median"] is not None]
    span = max(r["pool_bytes"] for r in S) / min(r["pool_bytes"] for r in S)
    assert max(lags) <= 3 * min(lags), f"detection-lag span is too large: {lags}"
    return (f"{span:.0f}x pool span, detection lag {min(lags):.0f}-{max(lags):.0f} ops, "
            f"victims 0 everywhere")


@claim("5.4", "history shaping works and is invisible to the violation counter")
def c_shaping():
    S = {r["arm"]: r for r in load("e9_strategic.json")["summary"]}
    h, a = S["honest"], S["shaper"]
    assert a["shaper_pages_resident_mean"] > 1.4 * h["shaper_pages_resident_mean"], \
        "the shaper gained nothing"
    assert a["honest_pages_resident_mean"] < h["honest_pages_resident_mean"], \
        "honest tenants lost nothing"
    assert a["violations_shaper_mean"] == 0, "the shaper was charged after all"
    return (f"shaper residency x{a['shaper_pages_resident_mean']/h['shaper_pages_resident_mean']:.1f}, "
            f"honest -{100*(1-a['honest_pages_resident_mean']/h['honest_pages_resident_mean']):.0f}%, "
            f"violations 0")


@claim("5.1", "the safety boundary held in every E1 fill even where the "
              "advertisement did not")
def c_safety():
    cur = load("e1_accuracy.json")["calibration_curve"]
    worst = max(r["overflow_bytes_max"] for r in cur)
    assert worst == 0, f"peak physical use exceeded the budget by {worst} B"
    return f"overflow_bytes_max = 0 across {sum(r['n_trials'] for r in cur)} fills"


@claim("A", "attribution holds while recovery is watermark-driven, and the "
            "sweep locates where it stops")
def c_extent_boundary():
    d = load("e11_sensitivity.json")["summary"]
    hs = [r for r in d if r["param"] == "headroom"]
    assert hs and all(r["violations_victims"] == 0 for r in hs), \
        "headroom sweep charged victims"
    es = sorted((r for r in d if r["param"] == "extent"),
                key=lambda r: r["value"])
    assert es, "no extent sweep"
    clean = [r for r in es if r["violations_victims"] == 0]
    dirty = [r for r in es if r["violations_victims"] > 0]
    assert clean, "attribution never clean at any extent size"
    # If the sweep no longer straddles the boundary, the paper's claim that it
    # LOCATES the boundary is no longer supported by this file.
    assert dirty, ("extent sweep no longer reaches device starvation; the "
                   "paper claims it locates that boundary")
    assert max(r["value"] for r in clean) < min(r["value"] for r in dirty), \
        "clean and starved extent sizes interleave"
    return (f"clean <= {max(r['value'] for r in clean)>>10} KiB extents, "
            f"starved >= {min(r['value'] for r in dirty)>>10} KiB "
            f"(victims {max(r['violations_victims'] for r in dirty):.0f})")


@claim("5.2", "E23 connects loss-visible software completions to modeled "
              "victim selection, repeated memcg requests, and value-checked Redis")
def c_e23_connected_path():
    document = load_document("e23_end_to_end_redis.json")
    summary = document["summary"]
    trials = document["trials"]
    assert set(summary) == {"age", "exact", "ledger"}, "missing E23 policy arm"
    assert all(summary[name]["trials"] == 3 for name in summary), \
        "E23 does not have three trials per arm"
    assert summary["age"]["selected_changed_tenant"] == 0, \
        "controlled age arm unexpectedly selects the changed tenant"
    assert summary["exact"]["selected_changed_tenant"] == 3, \
        "exact controller model does not select the changed tenant"
    assert summary["ledger"]["selected_changed_tenant"] == 3, \
        "CapLedger victim debt does not select the changed tenant"
    assert sum(arm["reclaim_cycles"] for arm in summary.values()) == 27, \
        "E23 does not contain 27 reclaim requests"
    assert sum(arm["get_errors"] for arm in summary.values()) == 0, \
        "E23 observed a Redis value-integrity error"
    for trial in trials:
        completion = trial["completion_path"]
        assert completion["gap_records"] == 8 and completion["gap_events"] == 1
        assert completion["telemetry_debt_before_resync"] == 8 * 4096
        assert completion["telemetry_debt_after_resync"] == 0
        assert completion["empirical_admission_blocked_after_gap"] is True
        assert completion["host_owned_after_resync"] == completion["actual_owned_after"]
        assert all(cycle["compressed_debt_discharged_bytes"] == 0
                   for cycle in trial["reclaim_cycles"]), \
            "generic memcg deltas incorrectly discharged compressed debt"
    gets = sum(item["operations"]
               for trial in trials
               for phase in trial["application"].values()
               if isinstance(phase, list)
               for item in phase)
    return f"9 trials, 27 requests, 8-record gaps, {gets} value-checked GETs"


@claim("5.3", "E24 fails every tested stratum closed to the raw-page cap "
              "without claiming a coverage certificate")
def c_e24_raw_fallback():
    document = load_document("e24_calibration_guard.json")
    summary = document["summary"]
    assert summary["empirical_batches"] == 0, "an E24 batch retained overcommit"
    assert summary["raw_cap_batches"] == summary["evaluated_batches"] == 2378
    assert summary["violations"] == summary["raw_cap_violations"] == 0
    assert all(not stratum["screen_passed"] for stratum in document["strata"])
    assert "not a coverage certificate" in document["guarantee"]
    return "27/27 strata rejected; 2378/2378 batches at 4096 B/page"


@claim("5.7", "the E25 tracker reaches 4096 tenants without gaps while "
              "multi-queue throughput fails to scale")
def c_e25_tracker_scale():
    rows = load("e25_tracker_scale.json")["summary"]
    assert {row["tenants"] for row in rows} == {8, 256, 4096}
    assert {row["queues"] for row in rows} == {1, 4, 8}
    assert all(row["dropped"] == 0 and row["gaps"] == 0 for row in rows)
    large = {row["queues"]: row for row in rows if row["tenants"] == 4096}
    assert large[8]["median_million_records_per_s"] < \
        large[1]["median_million_records_per_s"], \
        "eight queues unexpectedly scale above one queue; update the paper"
    return (f"4096 tenants: 1q {large[1]['median_million_records_per_s']:.2f} "
            f"vs 8q {large[8]['median_million_records_per_s']:.2f} Mrec/s; 0 gaps")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir", type=Path, default=DEFAULT_RESULTS,
        help="result directory to check (default: experiments/results)")
    parser.add_argument(
        "--strict", action="store_true",
        help="require every manifest result and fail rather than skip")
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST,
        help="complete-evidence manifest used by --strict")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global RES
    args = parse_args(argv)
    RES = args.results_dir.resolve()
    width = max(len(c[0]) for c in checks) + 2
    bad = skipped = held = 0
    if args.strict:
        manifest_errors = validate_manifest(args.manifest.resolve())
        if manifest_errors:
            print("FAIL  artifact evidence manifest")
            for error in manifest_errors:
                print(f"        {error}")
            bad += len(manifest_errors)
        else:
            print(f"ok    artifact evidence manifest\n"
                  f"        all required results present in {RES}")
    for section, text, fn in checks:
        try:
            note = fn()
        except FileNotFoundError as e:
            filename = Path(e.filename).name if e.filename else "required evidence"
            if args.strict:
                print(f"FAIL  §{section:<{width}} {text}\n        (no {filename})")
                bad += 1
            else:
                print(f"SKIP  §{section:<{width}} {text}\n        (no {filename})")
                skipped += 1
            continue
        except AssertionError as e:
            print(f"FAIL  §{section:<{width}} {text}\n        {e}")
            bad += 1
            continue
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            print(f"FAIL  §{section:<{width}} {text}\n"
                  f"        malformed or incomplete evidence: {e}")
            bad += 1
            continue
        print(f"ok    §{section:<{width}} {text}\n        {note}")
        held += 1
    print(f"\n{held} held, {bad} failed, {skipped} skipped")
    return 1 if bad or (args.strict and skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
