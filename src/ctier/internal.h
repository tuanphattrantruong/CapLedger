/* internal.h — private structures of the ctier device (Package 1 only).
 *
 * LOCKING PLAN (deadlock-free by global acquisition order):
 *   bucket mutex (lpn & 255)  ->  class mutex(es, ascending index)  ->
 *   structural rwlock (rd for normal ops / wr for compaction+extent assignment)
 *   ->  leaf mutexes (free-extent stack, spill, hist — never nested together).
 *
 * Invariants:
 *  - PTE state/class_idx/comp_size fields are modified only by bucket holders;
 *    extent_idx/chunk_idx may additionally be modified by compaction (wr held).
 *    Hence reading placement fields requires bucket + structural(rd).
 *  - Class state (partial lists, extent freelists, chunk alloc/free) is touched
 *    under (class mutex + structural rd) or under structural wr alone.
 *  - The free-extent stack is guarded by its own leaf mutex (pushes can happen
 *    from rd holders when a chunk free empties an extent; pops happen under wr).
 *  - Stats are C11 _Atomic with relaxed ordering.
 */
#ifndef CTIER_INTERNAL_H
#define CTIER_INTERNAL_H

#include <stdint.h>
#include <stdatomic.h>
#include <pthread.h>
#include "ctier.h"

#define CT_FREE         0
#define CT_PRESENT_COMP 1
#define CT_PRESENT_RAW  2
#define CT_SPILLED      3
#define CT_ZERO         4

#define CHUNK_NIL   0xFFFFu
#define OWNER_NIL   0xFFFFFFFFu
#define CLASS_NIL   0xFFFFu
#define EXT_NIL     (-1)

#define N_BUCKETS   256
#define SPILL_SLOT_BYTES 4608u
#define SPILL_GROW_SLOTS 256u
#define HIST_DECAY_INTERVAL 4096u
#define HIST_DECAY_LAMBDA 0.9
#define ALPHA_SLOW 0.005
#define ALPHA_FAST 0.05
#define CUSUM_K 0.5             /* slack in sigma_slow units */
#define CUSUM_H 5.0             /* alarm threshold in sigma_slow units */
#define CUSUM_SIGMA_FLOOR 32.0  /* sigma floor (bytes) for near-constant streams */
#define CUSUM_MIN_SAMPLES 64u   /* do not accumulate until the EWMAs converge */

/* 16-byte PTE */
typedef struct {
  uint32_t extent_idx;   /* extent (PRESENT_*) or spill slot (SPILLED) */
  uint16_t chunk_idx;    /* chunk within extent */
  uint8_t  state;        /* CT_* */
  uint8_t  class_idx;    /* size class (31 for raw) */
  uint16_t comp_size;    /* raw zlib bytes; 4096 raw; 0 zero page */
  uint16_t generation;   /* allocation/reuse epoch; preserved across rewrites */
  uint32_t _pad2;
} pte_t;

/* extent descriptor (malloc'd array, one per pool extent) */
typedef struct {
  uint8_t  *base;
  uint16_t  class_idx;   /* CLASS_NIL when unassigned */
  uint16_t  nchunks;
  uint16_t  live;
  uint16_t  free_head;   /* CHUNK_NIL = none; list threaded through chunk[0..1] */
  uint32_t *owner_lpn;   /* reverse map, OWNER_NIL for free chunks */
  int32_t   next_p, prev_p;  /* class partial-list links (EXT_NIL = none) */
  uint8_t   in_partial;
} extent_t;

/* spill file state (leaf mutex) */
typedef struct {
  pthread_mutex_t mx;
  int       fd;
  char     *path;
  uint32_t  nslots;      /* slots the file has been grown to */
  uint32_t *free_stack;
  uint32_t  free_top;    /* number of entries in free_stack */
  uint32_t  free_cap;
} spill_t;

/* telemetry (leaf mutex) */
typedef struct {
  pthread_mutex_t mx;
  double    bins[N_CLASSES];
  uint64_t  nrec;                 /* records since creation (drives decay) */
  uint64_t  nsamples;
  double    slow_mean, slow_var, fast_mean;
  uint16_t *ring;
  uint32_t  ring_cap;
  uint32_t  ring_pos;             /* next write position */
  uint64_t  ring_count;           /* total pushed */
  /* Exact integer running sums over the ring window, maintained in O(1) per
   * record (add the new sample, subtract the one it evicts). Values are <=
   * 4096 and the window is <= ring_cap, so sumsq <= 4096^2 * ring_cap stays
   * well inside uint64 and is exactly representable in double. Two
   * accumulators is the whole hardware ask for windowed moments. */
  uint64_t  win_sum, win_sumsq;
  /* Page-Hinkley / CUSUM upper-sided change detector on the size stream:
   *   S+ = max(0, S+ + (x - mu0 - k)),  alarm when S+ > h,
   * with mu0 the slow EWMA mean, k = CUSUM_K * sigma_slow (slack) and
   * h = CUSUM_H * sigma_slow (threshold). One accumulator. */
  double    cusum_pos;
  uint64_t  cusum_alarm;          /* latched 0/1 */
} hist_t;

struct ctier {
  /* immutable config */
  uint8_t  *pool;
  uint64_t  pool_bytes;
  uint64_t  extent_bytes;
  uint32_t  n_extents;
  uint64_t  max_pages;
  int       zlib_level;           /* compression level, codec-specific range */
  int       codec;                /* CTIER_CODEC_* */
  uint32_t  flags;

  pte_t    *pte;
  extent_t *ext;

  /* free-extent stack (leaf mutex) */
  pthread_mutex_t stack_mx;
  uint32_t *free_stack;
  uint32_t  free_top;

  /* per-class partial-extent list heads (touched under class+rd or wr) */
  int32_t   partial_head[N_CLASSES];

  /* locks */
  pthread_rwlock_t structural;
  pthread_mutex_t  class_mx[N_CLASSES];
  pthread_mutex_t  bucket_mx[N_BUCKETS];

  spill_t   spill;
  hist_t    hist;
  ct_feedback_queue_t *feedback;

  _Atomic uint64_t st[17];
};

/* pool.c (all require structural held as documented per function) */
int  ct_pool_init(struct ctier *t);
void ct_pool_destroy(struct ctier *t);
/* fast-path alloc from an assigned partial extent of class k.
 * Caller holds class mutex k + structural rd (or wr). Returns 0 and fills
 * (*e_idx,*chunk) or -1 if no free chunk exists in class k. */
int  ct_chunk_alloc_fast(struct ctier *t, int k, uint64_t lpn,
                         uint32_t *e_idx, uint16_t *chunk);
/* slow-path alloc: caller holds structural WR (no class mutex needed).
 * Tries partial -> assign new extent -> targeted compaction of k ->
 * full compaction -> assign. Returns 0 or CTIER_ENOSPACE. */
int  ct_chunk_alloc_slow(struct ctier *t, int k, uint64_t lpn,
                         uint32_t *e_idx, uint16_t *chunk);
/* free a chunk. Caller holds class mutex of its class + structural rd,
 * or structural wr. Releases the extent to the free stack if it empties. */
void ct_chunk_free(struct ctier *t, uint32_t e_idx, uint16_t chunk);
/* compaction of one class; caller holds structural WR. Returns bytes of
 * extents released. */
int64_t ct_compact_class(struct ctier *t, int k);

/* spill.c */
int  ct_spill_init(spill_t *s, const char *path);
void ct_spill_destroy(spill_t *s);           /* close + unlink + free */
/* write one slot; returns slot idx >= 0 or CTIER_EIO/ENOMEM(-EIO).
 * bytes_io out-param = bytes written (header+payload). */
int64_t ct_spill_put(spill_t *s, uint32_t lpn, uint16_t comp_size,
                     const uint8_t *payload, uint32_t payload_len,
                     uint64_t *bytes_io, int64_t *meta_delta);
/* read one slot into payload (cap 4096); returns payload_len or CTIER_EIO/
 * CTIER_ECORRUPT. comp_size_out: header field (0 => raw 4096). */
int64_t ct_spill_get(spill_t *s, uint32_t slot, uint32_t lpn_expect,
                     uint16_t *comp_size_out, uint8_t *payload,
                     uint64_t *bytes_io);
void ct_spill_release_slot(spill_t *s, uint32_t slot);

/* hist.c */
int  ct_hist_init(hist_t *h, uint32_t ring_cap);
void ct_hist_destroy(hist_t *h);
void ct_hist_record(hist_t *h, uint16_t class_rounded);
void ct_hist_read_ewma(hist_t *h, double *slow_mean, double *slow_var,
                       double *fast_mean);
int  ct_hist_snapshot(hist_t *h, double out[N_CLASSES]);
int  ct_ring_snapshot(hist_t *h, uint16_t *out, int cap);
int  ct_ring_moments(hist_t *h, uint32_t w, double *mu, double *sigma);
void ct_hist_read_cusum(hist_t *h, double *pos, uint64_t *alarm);
void ct_hist_reset_cusum(hist_t *h);

/* codec.c */
int  ct_codec_compress(int codec, int level, const uint8_t *src, uint32_t srclen,
                       uint8_t *dst, uint32_t dstcap, uint32_t *dlen);
int  ct_codec_decompress(int codec, const uint8_t *src, uint32_t srclen,
                         uint8_t *dst, uint32_t dstcap, uint32_t *outlen);
uint32_t ct_codec_bound(int codec, uint32_t srclen);
const char *ct_codec_name(int codec);

/* ctier.c helpers shared with pool.c */
static inline uint32_t ct_class_size(int k) { return 128u * (uint32_t)(k + 1); }
static inline void ct_stat_add(struct ctier *t, int id, uint64_t v) {
  atomic_fetch_add_explicit(&t->st[id], v, memory_order_relaxed);
}
static inline void ct_stat_sub(struct ctier *t, int id, uint64_t v) {
  atomic_fetch_sub_explicit(&t->st[id], v, memory_order_relaxed);
}
static inline uint64_t ct_stat_get(struct ctier *t, int id) {
  return atomic_load_explicit(&t->st[id], memory_order_relaxed);
}
static inline void ct_stat_set(struct ctier *t, int id, uint64_t v) {
  atomic_store_explicit(&t->st[id], v, memory_order_relaxed);
}

#endif /* CTIER_INTERNAL_H */
