# CapLedger — reproduction package

CapLedger is an operating-system capacity contract for hardware-compressed
memory tiers, where a device's usable capacity is a function of what tenants
write. The prototype attributes compressed bytes and debt to the tenant whose
data inflated, then selects that debtor before an existing hotness policy
selects pages.

This package is self-contained: C11 sources, frozen workload inputs, experiment
drivers, unit tests, and the retained result JSON that the claim checker
validates. No CXL or compressed-memory hardware is required. E0–E20 and E24–E25
run entirely against the software emulator; E21–E23 exercise Linux cgroup v2
inside a disposable privileged container.

## Quick check

After the setup below, one command validates the stored evidence:

```bash
./verify.sh
```

It builds the native library, runs 16 C tests and 37 Python tests, confirms that
all 36 result documents declared by `manifest.json` are present and parseable,
and evaluates 19 qualitative claim checks with zero skips. It takes seconds to a
few minutes and reruns no costly measurement. A zero exit status means every
check passed.

For a platform-independent evidence check using only the Python standard
library:

```bash
python3 experiments/verify_claims.py --strict --manifest manifest.json
```

The non-strict mode is only for partial or staged suites — it prints `SKIP` for
absent evidence. Validation must use `--strict`.

## Layout

| path | what |
|---|---|
| `src/ctier/` | emulated compressed tier: pluggable codec (zlib/LZ4/zstd), size-class pool, spill/compaction, telemetry, and a 16-byte `{token64, sequence32, generation16, size_class8, status8}` completion queue |
| `src/contract/` | quantile advertisement, admission classes, per-tenant windows, recovery ladder, overdraft attribution, reservation repricing |
| `src/include/` | frozen public C ABI (`ctier.h`, `contract.h`, `feedback.h`) |
| `src/pybind/` | ctypes bindings for the tier/contract path and the bootstrap estimator |
| `src/bench/` | native calibration, overhead, feedback, and tracker-scale drivers |
| `workloads/` | deterministic content generators, phase-change traces, frozen corpora, and `real_state.py`, which captures real application state by *running* Redis/SQLite/Git/NumPy |
| `experiments/` | E0–E25 drivers, suite runners, and `verify_claims.py` |
| `experiments/results/` | retained result JSON — the evidence `verify.sh` checks |
| `tests/` | 37 Python unit tests (27 contract, 10 tier) |
| `manifest.json` | the complete list of result documents required for strict validation |

## Requirements

The Makefile produces a macOS `.dylib` with Apple clang and Homebrew libraries.
It is a macOS build, not a portable Linux build; E23 separately compiles only
the portable C11 feedback queue inside Linux.

The reference host was an arm64 Mac Studio with 512 GiB RAM. E10 really touches
a 64 GiB anonymous pool, so a full E0–E11/E14–E17/E24–E25 run needs at least
64 GiB of usable memory. On smaller machines use smoke mode (below) as the
functional check.

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
brew install lz4 zstd
```

The pinned Python set is NumPy 2.5.2, cryptography 50.0.1, cffi 2.1.1, and
pycparser 3.0. `cryptography` is required: AES-labelled E7/E20 captures fail
rather than silently substituting another transform. The reference native
toolchain was Apple clang 21.0.0, zlib 1.2.12, LZ4 1.10.0, and zstd 1.5.7. Each
result's own `host` and `toolchain` metadata — not this section — is
authoritative for the run that produced it.

E7's Redis rows need Redis 7.4.2 built into `build/tools/`:

```bash
git clone --depth 1 --branch 7.4.2 https://github.com/redis/redis.git /tmp/redis-src
make -C /tmp/redis-src -j MALLOC=libc
mkdir -p build/tools
cp /tmp/redis-src/src/redis-server /tmp/redis-src/src/redis-cli build/tools/
```

Without those binaries E7 is a partial developer run and does not reproduce the
live-Redis rows.

E21–E23 require Docker, Linux cgroup v2, swap/reclaim support, and privileged
containers. Their wrappers pin base-image digests, request a private cgroup
namespace, mount the source read-only, and mount only the chosen result
directory read-write. E22/E23 install Debian Python and compiler packages at run
time and therefore also need network access; those Debian snapshots are not
vendored.

## Build and test

```bash
make all
./build/test_core
.venv/bin/python -m unittest discover -s tests
```

`make all` builds the library, the native benchmark drivers, and the C test
binary. Expect exactly 16 C assertions and 37 Python tests.

## Running the experiments

| Path | Platform and privilege | Command | Reference cost | Successful outcome |
|---|---|---|---|---|
| Emulator smoke | macOS, unprivileged | `FAST=1 PY=.venv/bin/python ./experiments/run_all.sh` | Machine-dependent; not retained evidence | Exit 0; JSON only under `build/results-smoke/` |
| Emulator full | macOS, unprivileged | `PY=.venv/bin/python ./experiments/run_all.sh` | About 81 minutes from retained `wall_seconds`; E10 alone about 35 minutes and 64 GiB | Every stage exits 0; canonical E0–E11/E14–E17/E24–E25 files updated |
| E12 feedback | macOS, unprivileged | `PY=.venv/bin/python ./experiments/run_e12_feedback.sh` | Timing-sensitive; three sequential million-record runs | Three raw repeats and a regenerated summary; no throughput drops |
| E13 async queue | macOS, unprivileged | `PY=.venv/bin/python ./experiments/run_e13_async.sh` | Timing-sensitive; three repeats | Four queue cells per repeat; summary `pass: true` |
| E18 callback | macOS, unprivileged | `.venv/bin/python experiments/e18_r2_callback.py experiments/results/e18_r2_callback.json` | About 67 seconds | Exit 0 and no recorded failures |
| E19 derived analysis | Python, unprivileged | `.venv/bin/python experiments/e19_review_analysis.py` | Sub-second | Cost/interval JSON regenerated from E4/E7/E8 |
| E20 multi-GiB replay | macOS, unprivileged, >4 GiB RAM | `.venv/bin/python experiments/e20_multigib_realstate.py experiments/results/e20_multigib_realstate.json` | About 20 seconds | 1.2 M pages loaded and rewritten; 59,020 reservation violations; zero attribution breaches or hard failures |
| E21 Linux ownership | Docker/cgroup v2, privileged | `./experiments/run_e21_linux.sh` | Not recorded by the retained run | Correct victim; ≥15 MiB target reclaim; ≤1 MiB collateral |
| E22 live Redis | Docker/cgroup v2, privileged, network | `./experiments/run_e22_redis.sh` | Not recorded by the retained run | Every trial reclaims; zero GET errors; ≤1 MiB control delta |
| E23 integrated Redis | Docker/cgroup v2, privileged, network | `./experiments/run_e23_linux.sh` | About 23 seconds | An eight-record gap records a would-block state until controller-oracle resync; CapLedger selects the changed tenant; zero GET errors |
| E24 offline eligibility screen | Python, unprivileged | `.venv/bin/python experiments/e24_calibration_guard.py` | About 0.01 seconds | All 2,378 tested batches take the 4096 B/page fallback; zero exceedances |
| E25 tracker scale | macOS native build, unprivileged | `.venv/bin/python experiments/e25_tracker_scale.py` | About 1.3 seconds | 8/256/4096 tenants × 1/4/8 queues; zero drops or gaps |

`run_all.sh` deliberately excludes E12/E13, the focused E18–E20 runs, and the
privileged E21–E23 wrappers: it is the macOS emulator suite, not a one-command
E0–E25 reproduction. Every runner stops at the first failed stage.

To avoid overwriting retained evidence during a trial run, redirect the output:

```bash
FAST=1 SMOKE_OUT=/tmp/capledger-smoke ./experiments/run_all.sh
RESULTS_OUT=/tmp/capledger-full ./experiments/run_all.sh
E12_RESULTS_OUT=/tmp/capledger-e12 ./experiments/run_e12_feedback.sh
E13_RESULTS_OUT=/tmp/capledger-e13 ./experiments/run_e13_async.sh
E21_RESULTS_OUT=/tmp/capledger-e21 ./experiments/run_e21_linux.sh
E22_RESULTS_OUT=/tmp/capledger-e22 ./experiments/run_e22_redis.sh
E23_RESULTS_OUT=/tmp/capledger-e23 ./experiments/run_e23_linux.sh
```

Smoke mode refuses to write into `experiments/results/`. E19 is the one legacy
driver that still writes directly to its canonical path, so run it only when you
intend to regenerate that evidence.

## Interpreting the numbers

| Class | Interpretation | Representative evidence |
|---|---|---|
| Counted | Deterministic event and accounting counters for fixed code, seeds, and frozen inputs | admission/violation/invariant counts; E12 loss and debt; E23 selection and resync; E25 drops and gaps |
| Measured | Host-, runtime-, and load-dependent elapsed time, throughput, latency, memory delta, or compressor output | E0/E4/E7/E12/E13/E20–E23/E25 timings and rates |
| Derived | Deterministic transformation of retained measurements | E14 dependence summaries; E19 intervals and cost mapping; E24 screen decisions |
| Modeled | Formula or counterfactual, not a direct system observation | E3 OCP timing; E15 exposure bounds; E16 bandwidth and queue-pipeline estimates; E19 counterfactual refault cost |

Counted invariants should reproduce exactly under the same inputs. Measured
timing and throughput should not be compared for bitwise equality across
machines. Where a result declares `modeled_fields` or `metric_classes`, that
declaration is the final authority.

## Scope and limits

The emulator is a userspace design study, not a CXL implementation: there is no
physical transport, interrupt path, device snapshot, or kernel page-token hook.
Wall-clock time is dominated by software compression (about 91% of a
representative run), so operations and bytes transfer across hosts while stall
percentiles do not. E21–E23 validate cgroup-v2 ownership, reclaim, and
actuation for private anonymous base pages only; shared, file-backed, dirty,
pinned, huge, DMA, and device-mapped pages are out of scope and are rejected or
charged pessimistically.
