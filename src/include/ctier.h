/* ctier.h — public ABI of the emulated compressed memory tier.
 *
 * FROZEN ABI (DESIGN.md, compressed-tier API): consumed via ctypes from Python. Do not rename
 * functions, reorder/renumber enum values, or change signatures.
 *
 * Semantics notes (binding for callers):
 *  - ctier_write returns the CLASS-ROUNDED physical bytes now used by the page
 *    (0 for an all-zero page, 4096 for raw/incompressible), or a negative error.
 *  - ctier_free_page and ctier_spill return the physical bytes RELEASED from the
 *    pool (>= 0; 0 for zero pages / already-spilled pages), or a negative error.
 *    ctier_spill on a ZERO page is a successful no-op (page stays resident ZERO,
 *    it occupies no pool bytes so there is nothing to spill).
 *  - ctier_read on a SPILLED page promotes it back into the pool (may propagate
 *    CTIER_ENOSPACE). The tier NEVER spills on its own; spilling happens only
 *    through explicit ctier_spill calls (policy lives above the device).
 *  - ctier_ring_snapshot copies the most recent class-rounded sizes,
 *    MOST-RECENT-FIRST (out[0] is the newest sample), and returns the count.
 *  - Telemetry: every write records one sample = class-rounded size (0 for zero
 *    pages) into the EWMAs and the ring. The decayed histogram hist[32] counts
 *    only NONZERO samples (bin i <=> class i); zero pages are visible via the
 *    PAGES_ZERO gauge and as 0-valued ring/EWMA samples.
 *  - Stat gauges vs counters: PAGES_PRESENT/PAGES_RAW/PAGES_ZERO/PAGES_SPILLED/
 *    EXTENTS_ACTIVE/PHYS_USED/PHYS_RESERVED/META_BYTES are current-value gauges;
 *    the rest are cumulative counters. SPILL_BYTES_W/R count bytes actually
 *    pwritten/pread to the spill file (8-byte slot header + payload).
 */
#ifndef CTIER_H
#define CTIER_H

#include <stdint.h>
#include "feedback.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifdef PAGE_SIZE              /* the macOS mach headers define PAGE_SIZE as   */
#undef PAGE_SIZE              /* vm_page_size; the tier's logical page is a   */
#endif                        /* fixed 4096 regardless of the host page size  */
#define PAGE_SIZE 4096
#define N_CLASSES 32          /* class i holds compressed payloads <= 128*(i+1) */

#define CTIER_F_NOHIST 1u     /* disable telemetry (hist/EWMA/ring) maintenance */

/* Page compressors. The tier is codec-agnostic (it stores whatever byte string
 * the codec returns, class-rounded); which codec runs changes the compressed-
 * size DISTRIBUTION the contract observes, which is the external-validity
 * question E0/E6 ask. A real CXL controller runs an LZ4/Snappy-class engine.
 * Level ranges: ZLIB 1-9; LZ4 1 = fast, >=2 = LZ4-HC at that level; ZSTD 1-19. */
#define CTIER_CODEC_ZLIB 0
#define CTIER_CODEC_LZ4  1
#define CTIER_CODEC_ZSTD 2

/* errors */
#define CTIER_EINVAL   (-1)
#define CTIER_EFAULT   (-2)
#define CTIER_ENOSPACE (-3)
#define CTIER_EIO      (-4)
#define CTIER_ECORRUPT (-5)

/* u64 stat ids */
enum {
  CTIER_STAT_PHYS_USED       = 0,
  CTIER_STAT_PHYS_RESERVED   = 1,
  CTIER_STAT_POOL_BYTES      = 2,
  CTIER_STAT_PAGES_PRESENT   = 3,
  CTIER_STAT_PAGES_RAW       = 4,
  CTIER_STAT_PAGES_ZERO      = 5,
  CTIER_STAT_PAGES_SPILLED   = 6,
  CTIER_STAT_SPILL_BYTES_W   = 7,
  CTIER_STAT_SPILL_BYTES_R   = 8,
  CTIER_STAT_COMPACTIONS     = 9,
  CTIER_STAT_EXTENTS_ACTIVE  = 10,
  CTIER_STAT_WRITES          = 11,
  CTIER_STAT_READS           = 12,
  CTIER_STAT_PROMOTES        = 13,
  CTIER_STAT_COMP_NS_TOTAL   = 14,
  CTIER_STAT_DECOMP_NS_TOTAL = 15,
  CTIER_STAT_META_BYTES      = 16
};

/* f64 stat ids */
enum {
  CTIER_STAT_EWMA_MEAN_SLOW = 100,
  CTIER_STAT_EWMA_VAR_SLOW  = 101,
  CTIER_STAT_EWMA_MEAN_FAST = 102
};

typedef struct ctier ctier_t;

/* Defaults when 0/NULL is passed: pool_bytes 256 MiB, max_pages 1<<20,
 * zlib_level 1, extent_bytes 1 MiB, ring_capacity 4096,
 * spill_path "build/tmp/spill_default.dat".
 * pool_bytes is rounded down to a multiple of extent_bytes (query the actual
 * value via CTIER_STAT_POOL_BYTES). extent_bytes must be a multiple of 4096 in
 * [64 KiB, 4 MiB]. Returns NULL on invalid config or allocation failure. */
ctier_t *ctier_create(uint64_t pool_bytes, uint64_t max_pages, int zlib_level,
                      uint64_t extent_bytes, uint32_t ring_capacity,
                      uint32_t flags, const char *spill_path);
/* As ctier_create, plus an explicit codec. ctier_create(...) is exactly
 * ctier_create2(..., CTIER_CODEC_ZLIB, level, ...). */
ctier_t *ctier_create2(uint64_t pool_bytes, uint64_t max_pages, int codec,
                       int level, uint64_t extent_bytes, uint32_t ring_capacity,
                       uint32_t flags, const char *spill_path);
void     ctier_destroy(ctier_t *t);      /* frees all, closes AND unlinks spill */
int64_t  ctier_write(ctier_t *t, uint64_t lpn, const uint8_t *src4k);
int      ctier_read (ctier_t *t, uint64_t lpn, uint8_t *dst4k);
int      ctier_free_page(ctier_t *t, uint64_t lpn);
int      ctier_spill(ctier_t *t, uint64_t lpn);
int64_t  ctier_compact(ctier_t *t);      /* bytes of extents released */
uint64_t ctier_stat_u64(ctier_t *t, int stat_id);
double   ctier_stat_f64(ctier_t *t, int stat_id);
int      ctier_hist_snapshot(ctier_t *t, double out[32]);
int      ctier_ring_snapshot(ctier_t *t, uint16_t *out, int cap);
/* Sample mean and population stddev of the most recent class-rounded sizes.
 * w == 0 (or w >= ring capacity) uses the whole ring window and is O(1): the
 * device keeps exact integer running sums, so this is two accumulators, not a
 * scan -- the form a controller could plausibly export. 0 < w < capacity costs
 * an O(w) scan. Returns the sample count used (0 if no samples yet). */
int      ctier_ring_moments(ctier_t *t, uint32_t w, double *mu, double *sigma);
/* Upper-sided CUSUM change detector on the size stream (alternative to the
 * fast/slow EWMA ratio; compared against it in E8). *alarm latches at 1. */
int      ctier_cusum(ctier_t *t, double *stat, uint64_t *alarm);
/* Re-arm the CUSUM after policy has acted on an alarm. Without this a latching
 * detector fires once per device lifetime. */
int      ctier_cusum_reset(ctier_t *t);
/* Optional protocol-faithful completion path. Enabling it makes every
 * successful write (and every ENOSPACE completion) enqueue a 16-byte record
 * whose opaque token is the LPN in this emulator. Queue loss never rolls back
 * a device write; it is detected by sequence gaps and treated as host debt. */
int      ctier_feedback_enable(ctier_t *t, uint32_t capacity);
int      ctier_feedback_poll(ctier_t *t, ct_feedback_record_t *out);
uint64_t ctier_feedback_dropped(ctier_t *t);
const char *ctier_codec_name(int codec);
uint64_t ctier_now_ns(void);             /* mach_absolute_time scaled to ns */

#ifdef __cplusplus
}
#endif
#endif /* CTIER_H */
