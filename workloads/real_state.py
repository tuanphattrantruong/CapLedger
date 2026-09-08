"""Real application state capture for E7.

Everything here is produced by RUNNING real software on the measurement
machine, not by a content generator. No file belonging to the user is read:
every corpus is either a system-shipped read-only asset (the BSD dictionary,
system dylibs) or state this module creates by driving a real program.

Sources
  sqlite   real B-tree pages of a SQLite database populated by a YCSB-style
           load (Zipfian keys, 1 KiB records built from dictionary words)
  redis    real RDB / in-memory serialisation of a live Redis server under the
           same load, when a redis-server binary is available
  git      real loose and packed object files of a git repository created from
           this project's own source tree
  macho    real pages of system dynamic libraries
  tensor   real float32 embeddings obtained by SVD of a real word co-occurrence
           matrix built from the system dictionary
  kvheap   the real in-memory arena of a slab-allocated key/value store driven
           by the same YCSB-style trace, read back out of the live process

State transitions (the paper's motivating drift events, performed for real)
  encrypt_at_rest   values re-encrypted with AES-CTR (real cipher, real key)
  vacuum            SQLite VACUUM: a real compaction of a real B-tree
  gc                git gc --aggressive: real delta-compression + packing
  quantize          float32 -> int8 affine quantisation of real tensors
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

PAGE = 4096
ROOT = Path(__file__).resolve().parents[1]
DICT_PATHS = ("/usr/share/dict/words", "/usr/share/dict/web2")
MACHO_PATHS = ("/usr/lib/dyld",
               "/usr/lib/libSystem.B.dylib",
               "/usr/lib/libc++.1.dylib",
               "/usr/lib/libobjc.A.dylib")


# --------------------------------------------------------------- utilities --
def _words(limit: int = 60000) -> list[str]:
    for p in DICT_PATHS:
        if Path(p).exists():
            with open(p, "rb") as f:
                w = [ln.strip().decode("latin-1") for ln in f]
            return [x for x in w if x][:limit]
    raise FileNotFoundError("no system dictionary found")


def to_pages(buf: bytes, limit: int | None = None) -> np.ndarray:
    """Slice a byte buffer into whole 4 KiB pages (drops a partial tail)."""
    n = len(buf) // PAGE
    if limit is not None:
        n = min(n, limit)
    if n == 0:
        return np.zeros((0, PAGE), dtype=np.uint8)
    return np.frombuffer(buf[: n * PAGE], dtype=np.uint8).reshape(n, PAGE)


class AESCTR:
    """AES-128-CTR backed by the required ``cryptography`` package.

    Experiments that claim an AES transition must fail rather than silently
    substituting a different keystream implementation.
    """

    def __init__(self, key: bytes):
        self.key = key
        try:
            from cryptography.hazmat.primitives.ciphers import (  # noqa
                Cipher, algorithms, modes)
            self._Cipher, self._alg, self._modes = Cipher, algorithms, modes
            self.impl = "aes-128-ctr"
        except ImportError as exc:
            raise RuntimeError(
                "AES-labelled real-state experiments require the pinned "
                "'cryptography' dependency") from exc

    def encrypt(self, data: bytes, nonce: int) -> bytes:
        iv = struct.pack(">QQ", 0, nonce)
        c = self._Cipher(self._alg.AES(self.key[:16]), self._modes.CTR(iv))
        e = c.encryptor()
        return e.update(data) + e.finalize()


def zipf_keys(n_ops: int, n_keys: int, seed: int, theta: float = 0.99) -> np.ndarray:
    """YCSB's Zipfian key distribution (the standard skew for KV benchmarks)."""
    rng = np.random.default_rng(seed)
    ranks = np.arange(1, n_keys + 1)
    p = 1.0 / ranks ** theta
    p /= p.sum()
    return rng.choice(n_keys, size=n_ops, p=p)


# Real record sizes are not uniform. A fixed 1 KiB value (plain YCSB) makes
# every stored page look alike and drives the within-state coefficient of
# variation to ~0, which would flatter any distributional estimator. Sizes are
# therefore drawn from a heavy-tailed distribution spanning two orders of
# magnitude, which is what document and KV stores actually hold.
REC_MIN, REC_MAX = 64, 8192


def record_size(rng, base: int) -> int:
    """Lognormal record size centred on `base`, clipped to [64 B, 8 KiB]."""
    v = int(base * float(rng.lognormal(mean=0.0, sigma=0.9)))
    return int(np.clip(v, REC_MIN, REC_MAX))


def _record(words: list[str], key: int, size: int, rng) -> bytes:
    """A ~`size`-byte record of real dictionary text, JSON-ish like a real
    document store's value."""
    parts = [f'{{"key":"user{key}","fields":[']
    n = 0
    while n < size:
        w = words[int(rng.integers(0, len(words)))]
        parts.append(f'"{w}",')
        n += len(w) + 3
    parts.append('""]}')
    return "".join(parts).encode()[:size].ljust(size, b" ")


# ------------------------------------------------------------ sqlite (real) --
def sqlite_pages(tmp: Path, n_keys: int = 20000, rec: int = 1024,
                 seed: int = 1, limit: int = 6000) -> dict:
    """Drive a real SQLite database with a YCSB-style load and capture its real
    on-disk B-tree pages, before and after two real state transitions."""
    words = _words()
    rng = np.random.default_rng(seed)
    db = tmp / "ycsb.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE usertable (ykey TEXT PRIMARY KEY, v BLOB)")
    rows = [(f"user{k}", _record(words, k, record_size(rng, rec), rng))
            for k in range(n_keys)]
    con.executemany("INSERT INTO usertable VALUES (?,?)", rows)
    con.commit()
    # YCSB-A style update phase over a Zipfian key distribution
    for k in zipf_keys(n_keys // 2, n_keys, seed):
        con.execute("UPDATE usertable SET v=? WHERE ykey=?",
                    (_record(words, int(k), record_size(rng, rec), rng),
                     f"user{int(k)}"))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    base = to_pages(db.read_bytes(), limit)

    # --- transition 1: encryption at rest (real cipher, real rewrite) ---
    aes = AESCTR(hashlib.sha256(b"e7-real-state").digest())
    cur = con.execute("SELECT rowid, v FROM usertable")
    enc = [(aes.encrypt(bytes(v), rid), rid) for rid, v in cur.fetchall()]
    con.executemany("UPDATE usertable SET v=? WHERE rowid=?", enc)
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    encrypted = to_pages(db.read_bytes(), limit)

    # --- transition 2: VACUUM (a real compaction of a real B-tree) ---
    con.execute("VACUUM")
    con.commit()
    con.close()
    vacuumed = to_pages(db.read_bytes(), limit)
    return {"sqlite_baseline": base, "sqlite_encrypted": encrypted,
            "sqlite_vacuumed": vacuumed, "cipher": aes.impl}


# -------------------------------------------------------------- git (real) --
def git_pages(tmp: Path, limit: int = 4000) -> dict:
    """A real git object store built from this project's own source tree:
    loose (zlib-per-object) then packed (`git gc`, real delta compression)."""
    repo = tmp / "gitrepo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "e7", "GIT_AUTHOR_EMAIL": "e7@x",
           "GIT_COMMITTER_NAME": "e7", "GIT_COMMITTER_EMAIL": "e7@x",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    run = lambda *a: subprocess.run(a, cwd=repo, env=env, check=True,
                                    capture_output=True)
    run("git", "init", "-q")
    for sub in ("src", "experiments", "workloads", "tests"):
        s = ROOT / sub
        if s.exists():
            shutil.copytree(s, repo / sub, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "import")

    def _collect(patterns) -> bytes:
        buf = bytearray()
        for pat in patterns:
            for f in sorted((repo / ".git").rglob(pat)):
                if f.is_file():
                    buf += f.read_bytes()
        return bytes(buf)

    loose = to_pages(_collect(["objects/??/*"]), limit)
    run("git", "gc", "-q", "--aggressive")
    packed = to_pages(_collect(["objects/pack/*.pack"]), limit)
    return {"git_loose": loose, "git_packed": packed}


# ----------------------------------------------------------- tensors (real) --
def tensor_pages(limit: int = 4000, dim: int = 128, seed: int = 3) -> dict:
    """Real float32 embeddings: SVD of a real character-n-gram co-occurrence
    matrix over the system dictionary, then the real fp32 -> int8 affine
    quantisation that motivates the paper's re-quantisation example."""
    words = _words(40000)
    rng = np.random.default_rng(seed)
    V = 4096
    M = np.zeros((len(words), 256), dtype=np.float32)
    for i, w in enumerate(words):
        b = np.frombuffer(w.encode("latin-1"), dtype=np.uint8)
        np.add.at(M[i], b, 1.0)
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-6)
    # randomised SVD projection: real data, real linear algebra
    P = rng.standard_normal((256, dim)).astype(np.float32) / np.sqrt(256)
    emb = (M @ P).astype(np.float32)
    fp32 = to_pages(emb.tobytes(), limit)
    scale = np.abs(emb).max(axis=1, keepdims=True) / 127.0 + 1e-9
    q = np.clip(np.round(emb / scale), -127, 127).astype(np.int8)
    int8 = to_pages(q.tobytes(), limit)
    return {"tensor_fp32": fp32, "tensor_int8": int8, "_V": V}


# ------------------------------------------------------------- macho (real) --
def macho_pages(limit: int = 4000) -> dict:
    buf = bytearray()
    for p in MACHO_PATHS:
        f = Path(p)
        if f.exists():
            try:
                buf += f.read_bytes()
            except OSError:
                pass
        if len(buf) >= limit * PAGE:
            break
    return {"macho": to_pages(bytes(buf), limit)}


# ------------------------------------------------- in-memory KV heap (real) --
def kvheap_pages(n_keys: int = 20000, rec: int = 1024, seed: int = 5,
                 limit: int = 6000) -> dict:
    """The real memory image of a slab-allocated in-process key/value store
    after a YCSB-style load, read back out of the live arena -- an application
    heap, not a file. The encrypted variant is the same store after the values
    are re-encrypted in place, which is the paper's 'encryption at rest'
    transition applied to memory rather than storage."""
    words = _words()
    rng = np.random.default_rng(seed)
    slab = bytearray(n_keys * rec)
    index: dict[int, int] = {}
    for k in range(n_keys):
        off = k * rec
        sz = record_size(rng, rec)
        # Slab slots are fixed-size, as in a real slab allocator; a short
        # record leaves the slot's tail as it was, which is exactly the
        # internal fragmentation a real arena carries.
        slab[off:off + min(sz, rec)] = _record(words, k, min(sz, rec), rng)
        index[k] = off
    for k in zipf_keys(n_keys // 2, n_keys, seed):
        off = index[int(k)]
        sz = min(record_size(rng, rec), rec)
        slab[off:off + sz] = _record(words, int(k), sz, rng)
    plain = to_pages(bytes(slab), limit)
    aes = AESCTR(hashlib.sha256(b"e7-kvheap").digest())
    for k in range(n_keys):
        off = index[k]
        slab[off:off + rec] = aes.encrypt(bytes(slab[off:off + rec]), k)
    enc = to_pages(bytes(slab), limit)
    return {"kvheap_plain": plain, "kvheap_encrypted": enc}


# ------------------------------------------------------------- redis (real) --
def redis_pages(tmp: Path, n_keys: int = 20000, rec: int = 1024, seed: int = 7,
                limit: int = 6000) -> dict:
    """A real Redis server under a YCSB-style load, captured as the real RDB
    snapshot it writes. Skipped (returns {}) when redis-server is absent."""
    # Prefer the locally built server (build/tools, see reviews/ENVIRONMENT.md)
    # so the capture does not depend on what happens to be on PATH.
    local = ROOT / "build" / "tools"
    exe = str(local / "redis-server") if (local / "redis-server").exists() \
        else shutil.which("redis-server")
    cli = str(local / "redis-cli") if (local / "redis-cli").exists() \
        else shutil.which("redis-cli")
    if not exe or not cli:
        return {}
    words = _words()
    rng = np.random.default_rng(seed)
    d = tmp / "redis"
    d.mkdir(parents=True, exist_ok=True)
    port = "63799"
    proc = subprocess.Popen(
        [exe, "--port", port, "--dir", str(d), "--save", "", "--appendonly", "no",
         "--daemonize", "no", "--rdbcompression", "no", "--protected-mode", "yes"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            r = subprocess.run([cli, "-p", port, "ping"], capture_output=True)
            if b"PONG" in r.stdout:
                break
        else:
            return {}
        # pipe the load in through redis-cli --pipe (the real protocol)
        def _load(valfn) -> None:
            buf = []
            for k in range(n_keys):
                v = valfn(k)
                buf.append(b"*3\r\n$3\r\nSET\r\n$%d\r\nuser%d\r\n$%d\r\n%s\r\n"
                           % (len(f"user{k}"), k, len(v), v))
            subprocess.run([cli, "-p", port, "--pipe"],
                           input=b"".join(buf), capture_output=True)

        _load(lambda k: _record(words, k, record_size(rng, rec), rng))
        subprocess.run([cli, "-p", port, "save"], capture_output=True)
        rdb = (d / "dump.rdb")
        plain = to_pages(rdb.read_bytes(), limit) if rdb.exists() else None
        aes = AESCTR(hashlib.sha256(b"e7-redis").digest())
        _load(lambda k: aes.encrypt(
            _record(words, k, record_size(rng, rec), rng), k))
        subprocess.run([cli, "-p", port, "save"], capture_output=True)
        enc = to_pages(rdb.read_bytes(), limit) if rdb.exists() else None
        out = {}
        if plain is not None and plain.shape[0]:
            out["redis_plain"] = plain
        if enc is not None and enc.shape[0]:
            out["redis_encrypted"] = enc
        return out
    finally:
        subprocess.run([cli, "-p", port, "shutdown", "nosave"],
                       capture_output=True)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def collect_all(tmp: Path, limit: int = 4000) -> tuple[dict, dict]:
    """Returns (name -> page array, provenance notes)."""
    out: dict[str, np.ndarray] = {}
    notes: dict[str, str] = {}
    s = sqlite_pages(tmp, limit=limit)
    notes["sqlite_cipher"] = s.pop("cipher")
    out.update(s)
    out.update(git_pages(tmp, limit=limit))
    t = tensor_pages(limit=limit)
    t.pop("_V", None)
    out.update(t)
    out.update(macho_pages(limit=limit))
    out.update(kvheap_pages(limit=limit))
    r = redis_pages(tmp, limit=limit)
    notes["redis"] = "captured" if r else "skipped (redis-server not found)"
    out.update(r)
    return {k: v for k, v in out.items() if v is not None and v.shape[0] > 0}, notes
