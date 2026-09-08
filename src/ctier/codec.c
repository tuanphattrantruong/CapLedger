/* codec.c — pluggable page compressor for the emulated tier.
 *
 * The tier's design is codec-agnostic: it stores whatever byte string the
 * compressor returns, rounded up to a 128 B size class. Which compressor is
 * used changes the compressed-size *distribution* the contract sees, which is
 * exactly the external-validity question E0/E6 ask (a real CXL controller runs
 * an LZ4/Snappy-class engine, not DEFLATE). Level semantics are per codec:
 *   ZLIB  1..9   (deflate)
 *   LZ4   1      (fast)          / >=2 -> LZ4-HC at that level
 *   ZSTD  1..19  (>=1 only; the tier never uses negative levels)
 */
#include <string.h>
#include <zlib.h>
#include <lz4.h>
#include <lz4hc.h>
#include <zstd.h>
#include "internal.h"

int ct_codec_compress(int codec, int level, const uint8_t *src, uint32_t srclen,
                      uint8_t *dst, uint32_t dstcap, uint32_t *dlen) {
  switch (codec) {
  case CTIER_CODEC_LZ4: {
    int n = (level <= 1)
        ? LZ4_compress_default((const char *)src, (char *)dst, (int)srclen,
                               (int)dstcap)
        : LZ4_compress_HC((const char *)src, (char *)dst, (int)srclen,
                          (int)dstcap, level);
    if (n <= 0) return -1;
    *dlen = (uint32_t)n;
    return 0;
  }
  case CTIER_CODEC_ZSTD: {
    size_t n = ZSTD_compress(dst, dstcap, src, srclen, level < 1 ? 1 : level);
    if (ZSTD_isError(n)) return -1;
    *dlen = (uint32_t)n;
    return 0;
  }
  case CTIER_CODEC_ZLIB:
  default: {
    uLongf n = dstcap;
    if (compress2(dst, &n, src, srclen, level) != Z_OK) return -1;
    *dlen = (uint32_t)n;
    return 0;
  }
  }
}

/* Decompress exactly srclen bytes into a dstcap-sized buffer; returns 0 and
 * sets *outlen on success. LZ4's block format needs the original size, which
 * for this tier is always PAGE_SIZE. */
int ct_codec_decompress(int codec, const uint8_t *src, uint32_t srclen,
                        uint8_t *dst, uint32_t dstcap, uint32_t *outlen) {
  switch (codec) {
  case CTIER_CODEC_LZ4: {
    int n = LZ4_decompress_safe((const char *)src, (char *)dst, (int)srclen,
                                (int)dstcap);
    if (n < 0) return -1;
    *outlen = (uint32_t)n;
    return 0;
  }
  case CTIER_CODEC_ZSTD: {
    size_t n = ZSTD_decompress(dst, dstcap, src, srclen);
    if (ZSTD_isError(n)) return -1;
    *outlen = (uint32_t)n;
    return 0;
  }
  case CTIER_CODEC_ZLIB:
  default: {
    uLongf n = dstcap;
    if (uncompress(dst, &n, src, srclen) != Z_OK) return -1;
    *outlen = (uint32_t)n;
    return 0;
  }
  }
}

/* Worst-case compressed size for a PAGE_SIZE input (used to size the scratch
 * buffer; the tier stores raw whenever the result is not smaller than a page,
 * so the bound only has to be safe, not tight). */
uint32_t ct_codec_bound(int codec, uint32_t srclen) {
  switch (codec) {
  case CTIER_CODEC_LZ4:  return (uint32_t)LZ4_compressBound((int)srclen);
  case CTIER_CODEC_ZSTD: return (uint32_t)ZSTD_compressBound(srclen);
  case CTIER_CODEC_ZLIB:
  default:               return (uint32_t)compressBound(srclen);
  }
}

const char *ct_codec_name(int codec) {
  switch (codec) {
  case CTIER_CODEC_LZ4:  return "lz4";
  case CTIER_CODEC_ZSTD: return "zstd";
  case CTIER_CODEC_ZLIB:
  default:               return "zlib";
  }
}
