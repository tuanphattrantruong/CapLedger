"""Content classes, trace generators, and the replay driver (see DESIGN.md).

Content ids are frozen:
  0 TEXT, 1 BINARY, 2 FLOAT, 3 FLOATQ, 4 JSON, 5 ZERO, 6 RANDOM, 7 MIXED

Pages are synthesized deterministically from (content, key, seed, version) so
traces stay tiny; `version` distinguishes rewrites of the same lpn.

A Trace is a dict of parallel numpy arrays
  {op u8 (0 WRITE, 1 READ, 2 FREE), tenant u8, cls u8, content u8,
   lpn u32, phase u16}
plus trace['attrs'] = dict(seed=..., name=..., params=...).

This module deliberately does NOT import ctier_ffi: replay() drives any object
with the Contract wrapper API (write/read/free/stat_u64) and compares return
codes against the frozen error constants redeclared below.
"""
from __future__ import annotations

import mmap
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PAGE = 4096

# content ids (frozen)
TEXT, BINARY, FLOAT, FLOATQ, JSON, ZERO, RANDOM, MIXED = range(8)
CONTENT_NAMES = ["TEXT", "BINARY", "FLOAT", "FLOATQ", "JSON", "ZERO", "RANDOM", "MIXED"]

# trace ops
OP_WRITE, OP_READ, OP_FREE = 0, 1, 2

# capacity classes (frozen, mirrors contract ABI)
CLS_GUAR, CLS_OPP = 0, 1

# frozen contract error codes (redeclared to avoid an ffi import here)
CONTRACT_EAGAIN = -11
CONTRACT_ENOSPACE = -13

# the E1 steady-mixed content mix (see DESIGN.md experiment matrix)
E1_STEADY_MIX = {TEXT: 0.35, BINARY: 0.15, JSON: 0.20, FLOATQ: 0.20, RANDOM: 0.10}

_M64 = (1 << 64) - 1


# ---------------------------------------------------------------- hashing ---
def _splitmix(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & _M64
    z = x
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
    return z ^ (z >> 31)


def _hash3(seed: int, key: int, version: int) -> int:
    h = _splitmix((seed & _M64) ^ 0xD1B54A32D192ED03)
    h = _splitmix(h ^ (key & _M64))
    h = _splitmix(h ^ (version & _M64))
    return h


# ----------------------------------------------------------------- corpora --
_corpora: dict[str, tuple] = {}


def _corpus(kind: str) -> np.ndarray:
    if kind not in _corpora:
        p = ROOT / "workloads" / f"corpus_{kind}.bin"
        if not p.exists():
            raise RuntimeError(
                f"{p} missing — run `python3 workloads/gen_corpus.py` first")
        f = open(p, "rb")
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        arr = np.frombuffer(mm, dtype=np.uint8)
        _corpora[kind] = (f, mm, arr)  # keep file+mmap alive
    return _corpora[kind][2]


# ---------------------------------------------------------- page synthesis --
_JSON_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_JSON_OPS = ("get", "put", "scan", "delete", "batch", "sync")


def _json_page(h: int) -> np.ndarray:
    rng = np.random.default_rng(h)
    # draw all the randomness up front (cheap), then format
    n_est = 40
    ts = rng.integers(0, 1 << 27, size=n_est)
    ids = rng.integers(0, 1 << 62, size=n_est)
    users = rng.integers(0, 100000, size=n_est)
    lats = rng.integers(0, 5000, size=n_est)
    lvl = rng.integers(0, len(_JSON_LEVELS), size=n_est)
    op = rng.integers(0, len(_JSON_OPS), size=n_est)
    parts = []
    total = 0
    i = 0
    while total < PAGE:
        j = i % n_est
        rec = (
            '{"ts":%d,"lvl":"%s","id":"%016x","user":%d,"lat_ms":%d,'
            '"msg":"op %s completed","shard":%d}\n'
            % (1700000000 + int(ts[j]) + i, _JSON_LEVELS[lvl[j]], int(ids[j]),
               int(users[j]), int(lats[j]), _JSON_OPS[op[j]], i % 16)
        ).encode()
        parts.append(rec)
        total += len(rec)
        i += 1
    buf = b"".join(parts)[:PAGE]
    return np.frombuffer(buf, dtype=np.uint8).copy()


def page_bytes(content: int, key: int, seed: int, version: int = 0) -> np.ndarray:
    """Deterministic 4096-byte page for (content, key, seed, version)."""
    h = _hash3(seed, key, version)
    if content == TEXT or content == BINARY:
        arr = _corpus("text" if content == TEXT else "binary")
        off = h % (arr.size - PAGE)
        return arr[off:off + PAGE].copy()
    if content == FLOAT:
        rng = np.random.default_rng(h)
        return rng.standard_normal(PAGE // 8).view(np.uint8)
    if content == FLOATQ:
        rng = np.random.default_rng(h)
        return np.clip(rng.standard_normal(PAGE) * 32.0, -127, 127).astype(np.int8).view(np.uint8)
    if content == JSON:
        return _json_page(h)
    if content == ZERO:
        return np.zeros(PAGE, dtype=np.uint8)
    if content == RANDOM:
        rng = np.random.default_rng(h)
        return rng.integers(0, 256, size=PAGE, dtype=np.uint8)
    raise ValueError(f"page_bytes: content id {content} not synthesizable "
                     f"(MIXED must be resolved by the trace generator)")


# ------------------------------------------------------- content samplers ---
def _norm_mix(mix: dict[int, float]) -> tuple[np.ndarray, np.ndarray]:
    ids = np.array(sorted(mix), dtype=np.uint8)
    probs = np.array([mix[int(i)] for i in ids], dtype=np.float64)
    probs = probs / probs.sum()
    return ids, probs


def steady_mixed_content(n: int, seed: int, mix: dict[int, float] | None = None) -> np.ndarray:
    """n content ids drawn iid from `mix` (default: the E1 steady-mixed mix)."""
    ids, probs = _norm_mix(mix if mix is not None else E1_STEADY_MIX)
    rng = np.random.default_rng(_hash3(seed, 0x51EAD, 0))
    return ids[rng.choice(len(ids), size=n, p=probs)]


def drift_content(n: int, seed: int, base_mix: dict[int, float] | None = None,
                  r0: float = 0.0, r1: float = 0.8, ramp_ops: int = 20000) -> np.ndarray:
    """n content ids where the RANDOM share ramps r0 -> r1 linearly over
    ramp_ops draws and holds at r1 after; non-RANDOM draws come from base_mix
    (default: E1 steady-mixed with its RANDOM component removed, renormalized)."""
    if base_mix is None:
        base_mix = {k: v for k, v in E1_STEADY_MIX.items() if k != RANDOM}
    ids, probs = _norm_mix(base_mix)
    rng = np.random.default_rng(_hash3(seed, 0xD41F7, 0))
    p_rand = r0 + (r1 - r0) * np.minimum(1.0, np.arange(n) / max(1, ramp_ops))
    is_rand = rng.random(n) < p_rand
    out = ids[rng.choice(len(ids), size=n, p=probs)]
    out[is_rand] = RANDOM
    return out


# ------------------------------------------------------------------ traces --
def _empty_trace(n: int) -> dict:
    return {
        "op": np.zeros(n, dtype=np.uint8),
        "tenant": np.zeros(n, dtype=np.uint8),
        "cls": np.zeros(n, dtype=np.uint8),
        "content": np.zeros(n, dtype=np.uint8),
        "lpn": np.zeros(n, dtype=np.uint32),
        "phase": np.zeros(n, dtype=np.uint16),
    }


def steady(n_pages: int, n_ops: int, mix: dict[int, float] | None = None,
           zipf_s: float = 0.99, read_frac: float = 0.6, rewrite_frac: float = 0.3,
           free_frac: float = 0.1, seed: int = 0, tenant: int = 0,
           opp_frac: float = 0.0) -> dict:
    """Steady-state trace over lpns [0, n_pages): bounded-Zipf(s) page
    popularity; per-op type drawn (read, rewrite, free); a READ or FREE of a
    never-written/freed lpn becomes a WRITE (first touch)."""
    mix = mix if mix is not None else E1_STEADY_MIX
    rng = np.random.default_rng(_hash3(seed, 0x57EAD1, 0))
    # bounded zipf over ranks, ranks scattered over the lpn space
    ranks_p = 1.0 / np.arange(1, n_pages + 1, dtype=np.float64) ** zipf_s
    cdf = np.cumsum(ranks_p / ranks_p.sum())
    rank_of = rng.permutation(n_pages).astype(np.uint32)  # rank -> lpn
    lpn_draw = rank_of[np.searchsorted(cdf, rng.random(n_ops), side="left").clip(0, n_pages - 1)]
    type_draw = rng.choice(3, size=n_ops, p=[read_frac, rewrite_frac, free_frac])
    ids, probs = _norm_mix(mix)
    content_draw = ids[rng.choice(len(ids), size=n_ops, p=probs)]
    lpn_cls = (rng.random(n_pages) < opp_frac).astype(np.uint8)  # 1 = OPP

    tr = _empty_trace(n_ops)
    live = np.zeros(n_pages, dtype=bool)
    op_arr, lpn_arr, cnt_arr, cls_arr = tr["op"], tr["lpn"], tr["content"], tr["cls"]
    for i in range(n_ops):
        lpn = int(lpn_draw[i])
        t = type_draw[i]
        if t == 0 and live[lpn]:      # READ
            op_arr[i] = OP_READ
        elif t == 2 and live[lpn]:    # FREE
            op_arr[i] = OP_FREE
            live[lpn] = False
        else:                          # WRITE (fresh or rewrite)
            op_arr[i] = OP_WRITE
            live[lpn] = True
        lpn_arr[i] = lpn
        cnt_arr[i] = content_draw[i]
        cls_arr[i] = lpn_cls[lpn]
    tr["tenant"][:] = tenant
    tr["attrs"] = {
        "name": "steady", "seed": seed,
        "params": {"n_pages": n_pages, "n_ops": n_ops,
                   "mix": {CONTENT_NAMES[k]: v for k, v in mix.items()},
                   "zipf_s": zipf_s, "read_frac": read_frac,
                   "rewrite_frac": rewrite_frac, "free_frac": free_frac,
                   "opp_frac": opp_frac, "tenant": tenant},
    }
    return tr


def crash(n_pages: int = 14336, seed: int = 0, opp_frac: float = 0.125,
          tenant: int = 0) -> dict:
    """Compressibility crash: phase 0 writes lpns 0..n-1 as TEXT; phase 1
    rewrites the same lpns (seeded shuffled order) as RANDOM — 'encryption
    turned on'. opp_frac of the lpns are OPP so the R1 ladder rung has
    something to reclaim; the rest are GUAR.

    Default n_pages is 14336 rather than the earlier 20480 sketch: measured
    TEXT compressibility on this machine is ~2217 B class-rounded (shuffled
    dict-words corpus), so 20480 pages would already put the 48 MiB E3 pool at
    u ~ 0.95 in phase 0. 14336 pages give a healthy phase-0 u ~ 0.67 while
    phase 1 (RANDOM, 4096 B/page; guaranteed pages alone ~51 MB at
    opp_frac 0.125) still overwhelms the pool and forces the full ladder."""
    n_ops = 2 * n_pages
    rng = np.random.default_rng(_hash3(seed, 0xC4A54, 0))
    lpn_cls = (rng.random(n_pages) < opp_frac).astype(np.uint8)
    tr = _empty_trace(n_ops)
    tr["op"][:] = OP_WRITE
    tr["tenant"][:] = tenant
    tr["lpn"][:n_pages] = np.arange(n_pages, dtype=np.uint32)
    tr["content"][:n_pages] = TEXT
    tr["phase"][:n_pages] = 0
    order = rng.permutation(n_pages).astype(np.uint32)
    tr["lpn"][n_pages:] = order
    tr["content"][n_pages:] = RANDOM
    tr["phase"][n_pages:] = 1
    tr["cls"][:] = lpn_cls[tr["lpn"]]
    tr["attrs"] = {
        "name": "crash", "seed": seed, "phase_flip_op": n_pages,
        "params": {"n_pages": n_pages, "n_ops": n_ops, "opp_frac": opp_frac,
                   "tenant": tenant,
                   "guar_pages": int((lpn_cls == 0).sum()),
                   "opp_pages": int((lpn_cls == 1).sum())},
    }
    return tr


def drift(n_pages: int, n_ops: int, seed: int = 0, r0: float = 0.0,
          r1: float = 0.8, tenant: int = 0) -> dict:
    """Write-only drift trace: sequential lpns modulo n_pages; RANDOM write
    share ramps r0 -> r1 linearly over the whole trace."""
    tr = _empty_trace(n_ops)
    tr["op"][:] = OP_WRITE
    tr["tenant"][:] = tenant
    tr["lpn"][:] = (np.arange(n_ops) % n_pages).astype(np.uint32)
    tr["content"][:] = drift_content(n_ops, seed, r0=r0, r1=r1, ramp_ops=n_ops)
    tr["phase"][:] = (np.arange(n_ops) * 100 // max(1, n_ops)).astype(np.uint16)
    tr["attrs"] = {
        "name": "drift", "seed": seed,
        "params": {"n_pages": n_pages, "n_ops": n_ops, "r0": r0, "r1": r1,
                   "tenant": tenant},
    }
    return tr


# Multi-tenant sizing (see DESIGN.md experiment matrix, E5): pool is 96 MiB, so
# B_total = 0.95*pool ~ 95.6 MB. Sizes below were calibrated against class-
# rounded zlib-level-1 sizes measured on this machine (TEXT ~2217 B,
# JSON ~1403 B, FLOATQ ~3714 B, RANDOM = 4096 B): pre-bomb physical use is
# ~84.5 MB (u ~ 0.88), and after the bomb converts t3's 13000 TEXT pages to
# RANDOM, guaranteed-class bytes alone (~95 MB at opp_frac 0.125) exceed the
# 0.97*B_total L3 watermark, forcing R3 even once every OPP page is gone —
# without that margin the ladder stops at R1/R2 and the isolation question is
# vacuous. t3 owns 13000/30000 pre-bomb ops (fill) and 13000 post-bomb ops
# (one full RANDOM rewrite pass), placed by a deterministic slot schedule.
MT_N_PAGES = (7600, 7600, 7600, 13000)
MT_LPN_STRIDE = 32768


def multitenant(seed: int = 0, n_ops: int = 80000, bomb_op: int = 30000,
                n_pages: tuple[int, int, int, int] = MT_N_PAGES,
                opp_frac: float = 0.125, read_frac: float = 0.6) -> dict:
    """4 tenants, disjoint lpn ranges (tenant t: [t*32768, t*32768+n_t)).
    t0 TEXT, t1 JSON, t2 FLOATQ steady; t3 (the bomber) TEXT until global op
    `bomb_op`, then RANDOM rewrites of its whole working set (entropy bomb).

    t3's n3 fill writes are spread evenly over ops [0, bomb_op) and its n3
    RANDOM rewrites (seeded shuffled order) evenly over [bomb_op, n_ops);
    every other op belongs to the victims in round robin — fill first, then
    steady (read_frac READs, else same-content rewrites; no frees, because the
    working sets must persist to sustain pressure; recorded in params)."""
    n3 = n_pages[3]
    if n3 > bomb_op or n3 > n_ops - bomb_op:
        raise ValueError("t3 set must fit both its pre-bomb fill window and "
                         "its post-bomb rewrite window")
    tenant_content = (TEXT, JSON, FLOATQ, TEXT)
    rng = np.random.default_rng(_hash3(seed, 0x3417E4, 0))
    tr = _empty_trace(n_ops)
    # per-tenant per-lpn class assignment (1 = OPP)
    lpn_cls = [
        (np.random.default_rng(_hash3(seed, 0xC15 + t, 0)).random(n_pages[t])
         < opp_frac).astype(np.uint8)
        for t in range(4)
    ]
    # t3 slot schedule (deterministic, evenly spread)
    pre_slots = (np.arange(n3, dtype=np.int64) * bomb_op) // n3
    post_slots = bomb_op + (np.arange(n3, dtype=np.int64) * (n_ops - bomb_op)) // n3
    t3_slot = np.full(n_ops, -1, dtype=np.int64)  # -1 = victim op, else t3 op idx
    t3_slot[pre_slots] = np.arange(n3)
    t3_slot[post_slots] = np.arange(n3)
    bomb_order = np.random.default_rng(_hash3(seed, 0xB03B, 0)).permutation(
        n3).astype(np.uint32)
    fill_next = [0, 0, 0, 0]
    victim_rr = 0
    steady_read = rng.random(n_ops) < read_frac
    steady_pick = rng.random(n_ops)

    for i in range(n_ops):
        k = t3_slot[i]
        if k >= 0:
            t = 3
            j = int(k) if i < bomb_op else int(bomb_order[k])
            if i < bomb_op:
                fill_next[3] += 1
            tr["op"][i] = OP_WRITE
            tr["content"][i] = TEXT if i < bomb_op else RANDOM
        else:
            t = victim_rr
            victim_rr = (victim_rr + 1) % 3
            n_t = n_pages[t]
            if fill_next[t] < n_t:
                j = fill_next[t]
                fill_next[t] += 1
                tr["op"][i] = OP_WRITE
            else:
                j = int(steady_pick[i] * n_t)
                tr["op"][i] = OP_READ if steady_read[i] else OP_WRITE
            tr["content"][i] = tenant_content[t]
        tr["tenant"][i] = t
        tr["lpn"][i] = t * MT_LPN_STRIDE + j
        tr["cls"][i] = lpn_cls[t][j]
        tr["phase"][i] = 1 if i >= bomb_op else 0
    guar_counts = [int((lpn_cls[t][:n_pages[t]] == 0).sum()) for t in range(4)]
    opp_counts = [int((lpn_cls[t][:n_pages[t]] == 1).sum()) for t in range(4)]
    tr["attrs"] = {
        "name": "multitenant", "seed": seed, "bomb_op": bomb_op,
        "tenants": [
            {
                "tenant": t,
                "base": t * MT_LPN_STRIDE,
                "n_pages": int(n_pages[t]),
                "content": CONTENT_NAMES[tenant_content[t]],
                "guar_pages": guar_counts[t],
                "opp_pages": opp_counts[t],
                "opp_lpns": (t * MT_LPN_STRIDE
                             + np.nonzero(lpn_cls[t])[0].astype(np.uint32)),
            }
            for t in range(4)
        ],
        "params": {"n_ops": n_ops, "bomb_op": bomb_op,
                   "n_pages": list(n_pages), "opp_frac": opp_frac,
                   "read_frac": read_frac, "free_frac": 0.0,
                   "lpn_stride": MT_LPN_STRIDE},
    }
    return tr


# ------------------------------------------------------------------ replay --
def replay(trace: dict, contract, timing: bool = False,
           snapshot_every: int = 0, snapshot_fn=None, live: dict | None = None):
    """Replay a trace against a Contract wrapper.

    - synthesizes page contents at replay time; per-lpn version counters make
      every rewrite distinct content;
    - timing=True records per-contract-call wall ns (time.perf_counter_ns)
      into the returned dict under 'op_ns' (int64 array, one per trace op;
      the synthesis cost is excluded — only the contract call is timed);
    - snapshot_fn(i) is invoked after every snapshot_every-th op (1 = every op);
    - live (optional dict) is maintained as {(tenant, cls): {lpn: True}} in
      recency order (oldest first) — used by E5 shed callbacks; entries the
      callback frees must be removed by the callback itself.

    Returns outcome counts: write ok/eagain/enospace/other split by class,
    plus read/free counters.
    """
    import time as _time

    ops = trace["op"]; tenants = trace["tenant"]; clss = trace["cls"]
    contents = trace["content"]; lpns = trace["lpn"]
    seed = trace["attrs"]["seed"]
    n = len(ops)
    versions: dict[int, int] = {}
    op_ns = np.zeros(n, dtype=np.int64) if timing else None
    counts = {
        "write_ok": {CLS_GUAR: 0, CLS_OPP: 0},
        "eagain": {CLS_GUAR: 0, CLS_OPP: 0},
        "enospace": {CLS_GUAR: 0, CLS_OPP: 0},
        "write_other_err": {CLS_GUAR: 0, CLS_OPP: 0},
        "read_ok": 0, "read_err": 0, "free_ok": 0, "free_err": 0,
    }
    rbuf = np.empty(PAGE, dtype=np.uint8)
    pcns = _time.perf_counter_ns
    for i in range(n):
        o = ops[i]; lpn = int(lpns[i]); tnt = int(tenants[i]); cl = int(clss[i])
        if o == OP_WRITE:
            v = versions.get(lpn, 0)
            page = page_bytes(int(contents[i]), lpn, seed, v)
            t0 = pcns()
            rc = contract.write(tnt, cl, lpn, page)
            t1 = pcns()
            if rc == 0:
                versions[lpn] = v + 1
                counts["write_ok"][cl] += 1
                if live is not None:
                    d = live.setdefault((tnt, cl), {})
                    d.pop(lpn, None)
                    d[lpn] = True
            elif rc == CONTRACT_EAGAIN:
                counts["eagain"][cl] += 1
            elif rc == CONTRACT_ENOSPACE:
                counts["enospace"][cl] += 1
            else:
                counts["write_other_err"][cl] += 1
        elif o == OP_READ:
            t0 = pcns()
            rc, _ = contract.read(lpn, rbuf)
            t1 = pcns()
            if rc == 0:
                counts["read_ok"] += 1
                if live is not None:
                    d = live.get((tnt, cl))
                    if d is not None and lpn in d:
                        d.pop(lpn, None)
                        d[lpn] = True
            else:
                counts["read_err"] += 1
        else:  # OP_FREE
            t0 = pcns()
            rc = contract.free(tnt, lpn)
            t1 = pcns()
            if rc == 0:
                counts["free_ok"] += 1
                # version counter intentionally NOT reset: a later rewrite of
                # this lpn must still produce fresh content
                if live is not None:
                    d = live.get((tnt, cl))
                    if d is not None:
                        d.pop(lpn, None)
            else:
                counts["free_err"] += 1
        if timing:
            op_ns[i] = t1 - t0
        if snapshot_every and snapshot_fn is not None and (i + 1) % snapshot_every == 0:
            snapshot_fn(i)
    out = dict(counts)
    if timing:
        out["op_ns"] = op_ns
    return out
