# RESULTS_SUMMARY — verified evidence base for "CapLedger"

**This file is the ground truth the paper cites from.** Where a stored number
is a noise artifact, or a claim is not supported by the data, that is stated
here and the paper follows this file.

**Two mechanical guarantees back that claim.** Every number in the paper's
prose and every hand-built table body is emitted by `paper/data/tables.py` as a
`\newcommand` read directly from `experiments/results/*.json`; every plot reads
a CSV emitted by `paper/data/extract.py` from the same files. Neither can drift
from the results, and a macro the paper references but the generator cannot
produce becomes a visible `[?Name]` placeholder rather than silently vanishing.

## Environment

Native results were produced on an Apple M3 Ultra (32 cores: 24P + 8E), 512 GiB,
macOS 26.5.2 and Apple clang 21; Redis 7.4.2 was built from source for E7.
Individual JSON `host`/`toolchain` fields are authoritative because targeted
reruns span interpreter patch versions. See `reviews/ENVIRONMENT.md`. E21--E23
ran separately in disposable privileged LinuxKit 6.12.72 aarch64 cgroup-v2
containers. An earlier version of this work was measured on an
8-core / 16 GiB laptop; **no result from that machine survives** — the whole
suite was re-run here. The one laptop artifact kept for provenance is
`results/superseded/e4_variance_laptop.json`, which nothing cites.

Given fixed seeds, code, and frozen input bytes, emulator counters are
deterministic. Timing, throughput, live Redis state, compression-library
output, cgroup memory deltas, and scheduler-sensitive queue depth may vary.

## Claims-to-evidence

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| C1 | Compressibility spans a wide range across content, and the emulation cost is quantified | **SUPPORTED** | `e0_calibration.json`: zlib level sweep plus a codec × content-class matrix (3 codecs × 7 classes). Level 1 is the right default: level 9 buys TEXT ~0.05 ratio at a third of the throughput. |
| C2 | **The capacity lie is real, measured on real application state** | **SUPPORTED** | `e7_realstate.json data.census`. Redis 7.4 under a YCSB-style load and fixed record count: 1.89× effective capacity, 1.00× after encryption at rest—47% gone, with a 0.31% RDB page-count increase. The in-memory KV arena changes from 2.34× to 1.06× and SQLite from 2.41× to 1.27×. Also reported: `git gc` changes page count but barely changes effective capacity because the objects are already compressed, while int8 quantization is a capacity win. |
| C3 | The advertised multiplier depends on the compressor, not only the data | **SUPPORTED** | Same file: the identical Redis image is 1.89× (zlib) / 2.00× (zstd) / **1.28× (LZ4)**. Every distributional result in the paper is reported under all three. |
| C4 | **A mean quote is exceeded on about half of tested admissions** | **SUPPORTED as the expected median diagnostic** | `e6_regime.json data.cells`: at `q=0.5`, the measured exceedance rate is 0.45–0.56 across all 36 cells: every batch size, content variance, and moment source (17,280 admissions). This expected median behavior explains why a fleet-average multiplier is not a tail-risk reserve; it is not evidence that the normal estimator is generally robust, and zswap itself performs no admission control. |
| C5 | **Telemetry-window moments improve the large-batch estimate over the short-memory EWMA** | **SUPPORTED, without a general calibration claim** | Same file: at `q=0.99` and the largest batch, the tenant EWMA reaches tens-of-percent violation rates, whereas the 4096-entry window-moment arm stays at 0–7.7% across the full sweep. The latter still exceeds a nominal 1% in some cells. |
| C6 | **The tested i.i.d. mean-rate envelope depends strongly on admission/window ratio** | **SUPPORTED as a grouped descriptive result** | `e6_regime.json data.window_cells` (4 windows × 5 batch sizes × 8 seed clusters). Ratio-group mean violation is at most 1% through `n/W ≤ 1/8` and 2.5% at `n/W = 1/2`, then rises to 8.6%, 22%, 35%, and 40% at ratios 2, 8, 32, and 128. Matched-ratio cells are not identical: their overall spread reaches 0.027, and one individual cell below `n/W ≤ 1/8` reaches 1.67%. Thus this is not a per-cell guarantee or a universal `n/W` law; E14/E24 reject transfer to ordered traces. |
| C7 | A single whole-pool advertisement is calibrated | **NOT SUPPORTED — and this retires an earlier claim** | `e1_accuracy.json`: at 60 seeds/cell no arm tracks its target at `q ≥ 0.9`, bootstrap included. At the 12 seeds this experiment first ran with, every cell was consistent with its target (Wilson upper bound 0.24 at 0/12) — the earlier "bootstrap is calibrated" claim was a resolution artifact. E1 operates at `n/W ≈ 5`, outside C6's envelope. **Bonus, unambiguous:** `overflow_bytes_max = 0` in every cell — the advertisement was violated, the *safety* boundary never was. |
| C8 | Nonstationary content breaks any stationary advertisement | **SUPPORTED (by design)** | `e1_accuracy.json`, drift workload: 100% violation at every `q` for every arm. Motivates detection + ladder, and is framed that way. |
| C9 | **Exact sizes plus tenant-oblivious global-age reclaim do not buy attribution** | **SUPPORTED** | `e5_multitenant.json data.isolation_summary` (20 seeds/config): contract configurations charge the bomber 3,328 violations and victims **0**; the zswap-style global-LRU policy model charges the bomber 3,022 and victims **4,717** — 61% of recovery lands on tenants whose data never changed because the liar's rewritten pages are the *youngest*. E17 separately shows that exact *tenant-aware* accounting can attribute correctly without a quantile. |
| C10 | **Transparency converts the lie into unattributable hard failures** | **SUPPORTED** | Same file: the transparent-reserve baseline takes 214,822 hard failures across 20 runs, distributed [344, 0, 23,595, 190,883] across tenants — 11% land on tenants that did nothing. `e3_phase_stress.json`: 10,195 hard failures per run against 0 for every contract configuration. |
| C11 | Misattribution also costs more work | **SUPPORTED** | `e2_frontier.json` (20 seeds): the zswap-style policy reaches the same guaranteed utilization (1.570× pool vs 1.571×) at 3,750 forced spills per 10 K guaranteed writes vs 2,462 — 52% more. `e3`: 21% more on the entropy-flip trace. |
| C12 | **The shipped ratio detector covers only the instantaneous flip** | **SUPPORTED (a negative result about our own mechanism)** | `e8_detector.json data.summary`: at a drift share of 1.0 the fast/slow ratio fires in ~17 ops; at 0.5 and below it never fires, because its peak ratio never reaches the 1.4 threshold. Threshold 1.1 recovers coverage to a drift share of 0.01. The CUSUM at h=5 detects **every** drift share down to 0.002, with lags 1–15,738 ops (an order of magnitude faster than the ratio test wherever both fire). **Zero false alarms** for either detector at any of the 11 thresholds swept, measured over every run's stationary phase and a no-drift control. **Instrumentation note:** an earlier pass armed the CUSUM before the working set was laid down, counted the resulting warm-up transients as stationary false alarms, and reported a ~33% false-alarm rate that the corrected instrument does not show; the device's latch and an offline replay of the same stream now agree exactly (0 disagreements over 84 runs, identical lag in 72/72). |
| C13 | Detection leads pool pressure | **SUPPORTED** | `e3_phase_stress.json`: the flag rises 15–19 ops after the flip in 84/84 runs, ~2,688 ops before the first ladder engagement. |
| C14 | Contract machinery is cheap | **SUPPORTED with mandatory caveats** | `e4_overheads.json` + `e4_variance.json` (4-run study): admission 26.5–28.6 ns mean, p50=p90=p99=42 ns (**one timer tick — resolution-limited upper bounds, not measurements**), max 8.4–11.5 µs. Telemetry cost is **below the noise floor** of the A/B methodology (delta spans −0.7 to +0.3 µs and flips sign, while the write path itself moves by more) and **must not be quoted as a measured number**. Device throughput is monotone in threads but **saturating**: 6.6× a single thread by 8 threads, only 6.7× at 16. The sweep stops at 16 threads because the benchmark harness became unreliable beyond it (one run reported zero for the 24-thread cell, a rerun did not terminate); a separate 24-thread driver showed **no stuck threads**, so the fault is in the harness and is not a device deadlock — we report the sweep as far as it is trustworthy rather than publishing a device limit we have not shown. |
| C15 | **A strategic tenant can shape its history, and repricing partially answers it** | **SUPPORTED, with a bounded transient** | `e9_strategic.json`: the shaper roughly doubles its residency at honest tenants' expense while taking **zero** violations — a contract that counted only violations would report a clean run. Repricing improves but does not restore honest residency, modestly reduces the shaper, and revokes ~4.5 K pages of stale-priced reservation at the cost of an eviction transient. The retained result is the repaired endpoint, not a no-debt ablation. Also: shaping with all-zero pages does **not** work (a free reservation is an empty one). |
| C16 | Three measured properties survive the tested scale sweep | **SUPPORTED in the emulator** | `e10_scale.json`: 256 MiB → 64 GiB pools. Detection lag stays at 12–13 *operations*, the advertisement-call cost does not grow detectably with the pool, and device-PTE metadata stays a fixed fraction; host token/window/queue memory is excluded. Attribution holds at every tested scale and no run breaks the invariant or hard-fails. This is not a general scalability claim. |
| C17 | The design's constants are not load-bearing surprises | **PARTIALLY SUPPORTED — one is** | `e11_sensitivity.json` (10 seeds/cell). Headroom is a clean utilization/recovery knob (0→20% moves utilization 0.92→0.73 and forced spills 17,764→13,747) with attribution intact throughout, and the codec changes how much recovery work a trace costs without changing who is charged. **But extent granularity is load-bearing:** at 4 MiB extents on a 96 MiB pool the device exhausts its extent supply while 53% of its bytes are free, recovery fires under device `ENOSPACE` rather than under a watermark, and that pressure has no lying tenant to charge — victims take 2,918 violations and one run hard-fails. Attribution is clean at 128–256 KiB extents. **The attribution guarantee is conditional on a device that is not extent-starved**; the paper states this. |
| C18 | Density-aware victim selection reclaims more per eviction | **REFUTED** | `e3_phase_stress.json`: plain CLOCK takes 5,356 forced spills against density-age's 6,868 (28% fewer) at equal bytes per spill. After a whole-working-set flip, `bytes × age` scoring prefers the old *compressible* pages, freeing the fewest pool bytes. No workload in the suite favours density-age. |
| C19 | Wall numbers are emulation-bound; op counts transfer | **SUPPORTED (framing discipline held)** | zlib time is measured separately (~91% of a representative run) and OCP-scale series are op-counts × spec latency, listed in `modeled_fields`. No modeled number is presented as measured. |
| C20 | The i.i.d. `n/W` envelope transfers to ordered traces | **REFUTED** | `e14_dependence.json`: 2,378 non-overlapping ordered batches from captured SQLite, KV-heap, tensor, encrypt/quantize transitions, and grouped-object streams. IID pricing violates 16.4%; ESS inflation reduces this to 1.93% and moving-block bootstrap to 4.79%. A 4 KiB/page cap is the only evaluated assumption-free bound and forfeits compression. |
| C21 | Detector-only protection covers realistic drift shapes | **REFUTED** | `e15_drift_adversary.json`: eight shapes over 20 seeds expose stationary alarms and 20/20 misses on adaptive below-reference shaping. Periodic repricing plus a per-write excess cap bounds accounting exposure; it is not an application SLO. |
| C22 | The completion ABI's scale and ordering costs can be left implicit | **REFUTED / QUANTIFIED** | `e13_async_summary.json` measures batching/visibility/headroom; `e16_interface_model.json` maps 64–512 GB/s writes to record traffic and software queue-pipeline equivalents (not CPU cores), enumerates free/reuse/completion orders, compares aggregation/sampling, and sizes page metadata. These are software and analytical results, not physical CXL validation. |
| C23 | Attribution gains are an artifact of global LRU | **REFUTED** | `e17_reactive_baseline.json`: an ungated, unreserved exact-size baseline with tenant debt and CLOCK assigns a mean 3,276.8 spills to the rewriter, zero to unchanged tenants, and has no hard failure. This isolates attribution from probabilistic admission. |
| C24 | The cooperative callback is a validated recovery rung | **REFUTED** | `e18_r2_callback.json`: the callback releases application state but produces zero extra compressed-pool bytes after R1. The paper treats it as notification only and validates a two-rung reclaim mechanism. |
| C25 | Recovery has measured application-visible cost | **PARTIALLY SUPPORTED** | `e19_review_analysis.json` maps sampled reuse through measured read/promotion rates but remains counterfactual. E21 adds a Linux post-reclaim scan; E23 adds three short trials per policy of Redis throughput/p99 ratios and exact-value checks. None establishes a production SLO. |
| C26 | A real-derived transition scales beyond the small emulated pools | **SUPPORTED, scoped** | `e20_multigib_realstate.json`: captured SQLite/AES pages tiled to 1.2 million pages (4.58 GiB logical) in a 4 GiB pool complete with 59,020 reservation violations but zero hard failures or attribution breaches. Tiling tests scale, not independent workload diversity. |
| C27 | The owner decision can drive a real Linux reclaim path | **SUPPORTED for a restricted page class** | `e21_linux_memcg.json`: seven LinuxKit cgroup-v2 trials with two 32 MiB private-anonymous tenants. CapLedger selects the rewriting memcg in every trial; a bounded `memory.reclaim` request is followed by at least a 16.08 MiB target `memory.current` drop and 0 MiB maximum unchanged-memcg drop. P95 reclaim-call time is 7.88 ms and target p95 added scan time is 46.66 ms. Fork/COW stays in the allocation memcg; task migration does not migrate existing page charges; tenant exit releases at least 32.13 MiB. |
| C28 | Targeted cgroup reclaim has an observable live-Redis outcome | **SUPPORTED, descriptive** | `e22_redis_memcg.json`: five fresh-process trials reclaim from the preselected target Redis cgroup, produce zero GET errors, and change the control cgroup by at most 0.004 MiB. This validates the actuator, not CapLedger's victim selection; short runs and five trials are not a tail-SLO certification. |
| C29 | The software control path can be connected from completion loss to a live process | **SUPPORTED only as a sidecar integration** | `e23_end_to_end_redis.json`: logical-value classes traverse the C queue; an injected eight-record gap records a would-block state; a controller oracle reconciles the snapshot; CapLedger/exact select the rewritten tenant while the controlled age model selects the unchanged tenant; fixed-size reclaim requests run beside Redis probes with zero GET errors. Sizes are not Redis VM-page completions, no contract admission function is invoked for the recorded block, and three shared-host trials do not establish policy superiority or a tail SLO. |
| C30 | Uncertified dependent strata can conservatively lose overcommit | **SUPPORTED as a negative result** | `e24_calibration_guard.json`: all 27 strata and 2,378 evaluated batches use the deterministic 4 KiB/page cap, with zero exceedances and zero empirical batches. The offline screen trusts E14 labels and page-size autocorrelation; it validates fallback bookkeeping, not independence, stationarity, or future coverage. |
| C31 | The host tracker data plane runs with thousands of tenants and multiple queues | **SUPPORTED for the C11 prototype** | `e25_tracker_scale.json`: 8, 256, and 4,096 tenants crossed with 1, 4, and 8 queues, three million-record repeats per cell, with zero drops and gaps. Eight queues do not improve throughput at 4,096 tenants; the result excludes policy, reclaim, kernel integration, and CXL transport. |

## Defects this evaluation exposed in our own mechanism

Each is now a regression test in `tests/test_contract.py::TestNewArms`.

1. **Freeing a page did not return its reservation.** `consumed` grew
   monotonically, so overdraft drifted negative without bound; a tenant could
   bank that credit by churning cheap pages and buy eviction immunity.
   Invisible until E9, because no earlier experiment freed a page.
2. **Overdraft counted resident bytes.** Spilling the lying tenant's page
   lowered its overdraft, so it stopped being the preferred victim and the next
   page taken belonged to someone innocent.
3. **The admission gate charged against resident bytes.** The ladder frees pool
   bytes by spilling, so the gate could admit into room it had just made by
   evicting guaranteed pages — an admit-and-spill treadmill reporting success.
   Fixing it cut honest tenants' collateral evictions ~10× in E9.
4. **Per-tenant pricing used a device-global window.** One tenant's inflation
   raised every tenant's price. Windows are now per tenant.

## Honest limitations (the paper carries all of these)

1. **Emulation-bound timing.** Software compressors stand in for a hardware
   engine. No stall or latency number transfers; op counts, byte accounting,
   violation counts, and detection lags *in operations* do.
2. **A whole-pool advertisement is not calibratable** from a bounded window
   (C7). The contract should quote capacity in units bounded by `W`.
3. **Nonstationary content breaks any stationary advertisement** (C8).
4. **R2 (shed callbacks) reclaims no pool bytes in any experiment.** By the
   time the middle rung engages, R1 has already spilled what the callbacks
   would free. It survives as a pressure-notification hook, not a validated
   reclaim stage.
5. **Density-age victim selection is refuted** (C18).
6. **Isolation is not specific to the probabilistic advertisement** — the
   mean-reserve configuration isolates identically. Credit the reservation and
   overdraft accounting plus overdraft-first victim selection.
7. **Repricing works through an eviction transient** that is bounded but not
   eliminated. Pricing on realized rather than reserved bytes is unexplored.
8. **Timer resolution.** Admission percentiles are quantized at one 42 ns tick
   and are upper bounds. Telemetry overhead is below this methodology's noise
   floor and is not quoted as a number.
9. **Single machine, three software compressors.** Transferability rests on the
   E0/E7 factoring argument, not on measurement.
10. **Scale reaches 64 GiB**, two orders of magnitude above the rest of the
    evaluation and still an order of magnitude below a TB-scale tier.
11. **Real-state breadth.** E7 captures a live Redis server, a real B-tree, a
    real object store, a live in-process arena, real tensors, and system
    binaries — but each is one application under one load generator.
12. **Detector limits remain.** E15 covers eight shapes, but its adaptive
    generator is not a proof over all tenant strategies, and false-positive
    application cost remains unmeasured.
13. **Attribution assumes the device is not extent-starved** (C17). When the
    pool runs out of extents rather than bytes, recovery is triggered by
    device exhaustion, which no tenant's overdraft explains, and the ladder
    falls back to an arbitrary victim. This is a device-configuration
    constraint, found by the sensitivity sweep and not anticipated.
14. **Zero-page accounting.** Zero pages cost nothing and record size 0; mixes
    dominated by them would drag μ̂ toward 0 and are unstressed beyond E0's
    ZERO class.
15. **Restricted Linux path.** E21 exercises cgroup ownership, bounded
    `memory.reclaim` requests, refault scans, fork/COW, task migration, and exit only for
    private anonymous 4 KiB pages. It is a userspace controller, not a kernel
    patch; shared, dirty, pinned, DMA, huge-page, and real compressed-device
    paths remain outside the validation.
16. **E22/E23 are short sidecar tests.** Redis is live, but the supplied size
    classes are not VM-page or hardware completions. E23 records a scripted
    would-block decision and controller-oracle resynchronization; neither run
    establishes a production tail-SLO guarantee.
17. **`memory.reclaim` bounds the request, not the observed delta.** Kernel
    reclaim and concurrent process activity can move `memory.current` by more
    than the requested byte count.
18. **E24 preserves no overcommit.** Every retained batch takes the raw-page
    cap. Its zero violations are therefore a conservative negative result, not
    evidence that its empirical screening procedure certifies a useful quote.
19. **E25 is data-plane only.** Its scale result excludes admission, policy,
    reclaim, kernel hooks, and physical transport.
