#!/usr/bin/env python3
"""Build the workload corpora described in DESIGN.md.

  workloads/corpus_text.bin   16 MiB  /usr/share/dict/words tiled, per-tile
                                       seeded line shuffle (+ optional extra
                                       system text if trivially readable)
  workloads/corpus_binary.bin 16 MiB  /usr/lib/dyld first, then /bin/* and
                                       readable /usr/lib/*.dylib until full
  workloads/corpus_meta.json          sources, byte counts, sha256, seed,
                                       verbatim `date -u`

Idempotent: skipped when both corpora exist and their sha256 match the meta.
Use --force to rebuild.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WL = ROOT / "workloads"
TEXT_BIN = WL / "corpus_text.bin"
BINARY_BIN = WL / "corpus_binary.bin"
META = WL / "corpus_meta.json"

CORPUS_BYTES = 16 * 1024 * 1024
DEFAULT_SEED = 0xA5F00D1E  # project base seed

WORDS = Path("/usr/share/dict/words")
# optional extra text sources, used only if trivially readable
EXTRA_TEXT_CANDIDATES = [
    Path("/usr/share/calendar"),
    Path("/usr/share/misc"),
]
EXTRA_TEXT_CAP = 2 * 1024 * 1024

DYLD = Path("/usr/lib/dyld")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gather_extra_text() -> tuple[bytes, list[str]]:
    """Best-effort extra text; empty result is fine (words alone acceptable)."""
    chunks: list[bytes] = []
    used: list[str] = []
    total = 0
    for cand in EXTRA_TEXT_CANDIDATES:
        try:
            if not cand.exists():
                continue
            files = sorted(x for x in cand.rglob("*") if x.is_file()) if cand.is_dir() else [cand]
        except OSError:
            continue
        for f in files:
            if total >= EXTRA_TEXT_CAP:
                break
            try:
                data = f.read_bytes()
            except OSError:
                continue
            # keep only mostly-printable content
            if not data:
                continue
            sample = data[:4096]
            printable = sum(1 for b in sample if 9 <= b <= 126)
            if printable / max(1, len(sample)) < 0.9:
                continue
            take = data[: EXTRA_TEXT_CAP - total]
            chunks.append(take)
            used.append(str(f))
            total += len(take)
    return b"".join(chunks), used


def build_text(seed: int) -> dict:
    if not WORDS.exists():
        raise RuntimeError(f"{WORDS} missing; cannot build text corpus")
    raw = WORDS.read_bytes()
    lines = raw.split(b"\n")
    lines = [ln for ln in lines if ln]
    extra, extra_used = _gather_extra_text()
    rng = np.random.default_rng(seed)
    out = bytearray()
    out += extra
    n = len(lines)
    tiles = 0
    while len(out) < CORPUS_BYTES:
        perm = rng.permutation(n)
        tile = b"\n".join(lines[i] for i in perm) + b"\n"
        out += tile
        tiles += 1
    data = bytes(out[:CORPUS_BYTES])
    TEXT_BIN.write_bytes(data)
    return {
        "path": str(TEXT_BIN),
        "bytes": len(data),
        "sha256": sha256_file(TEXT_BIN),
        "sources": [str(WORDS)] + extra_used,
        "tiles": tiles,
        "extra_text_bytes": len(extra),
        "word_lines": n,
    }


def build_binary() -> dict:
    sources: list[Path] = []
    if DYLD.exists():
        sources.append(DYLD)
    try:
        sources += sorted(p for p in Path("/bin").iterdir() if p.is_file())
    except OSError:
        pass
    try:
        sources += sorted(Path("/usr/lib").glob("*.dylib"))
    except OSError:
        pass
    out = bytearray()
    used: list[dict] = []
    for src in sources:
        if len(out) >= CORPUS_BYTES:
            break
        try:
            data = src.read_bytes()
        except OSError:
            continue
        if not data:
            continue
        take = data[: CORPUS_BYTES - len(out)]
        out += take
        used.append({"path": str(src), "bytes": len(take)})
    if not out:
        raise RuntimeError("no readable binary sources found (/usr/lib/dyld, /bin/*, /usr/lib/*.dylib)")
    # if everything readable was still short of 16 MiB, tile what we have
    tiled = False
    while len(out) < CORPUS_BYTES:
        tiled = True
        out += out[: CORPUS_BYTES - len(out)]
    data = bytes(out[:CORPUS_BYTES])
    BINARY_BIN.write_bytes(data)
    return {
        "path": str(BINARY_BIN),
        "bytes": len(data),
        "sha256": sha256_file(BINARY_BIN),
        "sources": used,
        "tiled_to_fill": tiled,
    }


def up_to_date() -> bool:
    if not (TEXT_BIN.exists() and BINARY_BIN.exists() and META.exists()):
        return False
    try:
        meta = json.loads(META.read_text())
        return (
            meta.get("text", {}).get("sha256") == sha256_file(TEXT_BIN)
            and meta.get("binary", {}).get("sha256") == sha256_file(BINARY_BIN)
            and meta.get("text", {}).get("bytes") == TEXT_BIN.stat().st_size
            and meta.get("binary", {}).get("bytes") == BINARY_BIN.stat().st_size
        )
    except (json.JSONDecodeError, OSError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="rebuild even if up to date")
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=DEFAULT_SEED)
    args = ap.parse_args()

    if not args.force and up_to_date():
        print(f"corpora up to date ({TEXT_BIN.name}, {BINARY_BIN.name}); use --force to rebuild")
        return 0

    WL.mkdir(parents=True, exist_ok=True)
    date_utc = subprocess.run(["date", "-u"], capture_output=True, text=True).stdout.strip()
    text_meta = build_text(args.seed)
    binary_meta = build_binary()
    meta = {
        "schema": "ccap-corpus-v1",
        "seed": args.seed,
        "date_utc": date_utc,
        "corpus_bytes": CORPUS_BYTES,
        "text": text_meta,
        "binary": binary_meta,
    }
    META.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {TEXT_BIN} ({text_meta['bytes']} B, sha256 {text_meta['sha256'][:16]}...)")
    print(f"wrote {BINARY_BIN} ({binary_meta['bytes']} B, sha256 {binary_meta['sha256'][:16]}...)")
    print(f"wrote {META}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
