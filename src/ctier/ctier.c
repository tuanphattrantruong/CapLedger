/* ctier.c — emulated compressed memory tier: create/destroy, read/write/free,
 * spill/promote, compaction entry point, timers, stats. See internal.h for the
 * locking plan. */
#include <stdlib.h>
#include <string.h>
#include <zlib.h>
#include <sys/mman.h>
#include <mach/mach_time.h>
#include "internal.h"

/* ---------- timer ---------- */

static mach_timebase_info_data_t g_tb;
static pthread_once_t g_tb_once = PTHREAD_ONCE_INIT;
static void tb_init(void) { mach_timebase_info(&g_tb); }

uint64_t ctier_now_ns(void) {
  pthread_once(&g_tb_once, tb_init);
  return mach_absolute_time() * g_tb.numer / g_tb.denom;
}

/* ---------- helpers ---------- */

static int is_all_zero(const uint8_t *src) {
  uint64_t v, acc = 0;
  for (int i = 0; i < PAGE_SIZE; i += 8) {
    memcpy(&v, src + i, 8);
    acc |= v;
    if (acc) return 0;
  }
  return 1;
}

static void lock_classes(struct ctier *t, int a, int b) { /* -1 = none */
  if (a > b) { int tmp = a; a = b; b = tmp; }
  if (a >= 0) pthread_mutex_lock(&t->class_mx[a]);
  if (b >= 0 && b != a) pthread_mutex_lock(&t->class_mx[b]);
}
static void unlock_classes(struct ctier *t, int a, int b) {
  if (a > b) { int tmp = a; a = b; b = tmp; }
  if (b >= 0 && b != a) pthread_mutex_unlock(&t->class_mx[b]);
  if (a >= 0) pthread_mutex_unlock(&t->class_mx[a]);
}

/* ---------- create / destroy ---------- */

ctier_t *ctier_create(uint64_t pool_bytes, uint64_t max_pages, int zlib_level,
                      uint64_t extent_bytes, uint32_t ring_capacity,
                      uint32_t flags, const char *spill_path) {
  return ctier_create2(pool_bytes, max_pages, CTIER_CODEC_ZLIB, zlib_level,
                       extent_bytes, ring_capacity, flags, spill_path);
}

ctier_t *ctier_create2(uint64_t pool_bytes, uint64_t max_pages, int codec,
                       int level, uint64_t extent_bytes, uint32_t ring_capacity,
                       uint32_t flags, const char *spill_path) {
  if (codec != CTIER_CODEC_ZLIB && codec != CTIER_CODEC_LZ4 &&
      codec != CTIER_CODEC_ZSTD)
    return NULL;
  int zlib_level = level;
  if (pool_bytes == 0) pool_bytes = 256ull << 20;
  if (max_pages == 0) max_pages = 1ull << 20;
  if (zlib_level <= 0) zlib_level = 1;
  if (codec == CTIER_CODEC_ZLIB && zlib_level > 9) zlib_level = 9;
  if (codec == CTIER_CODEC_ZSTD && zlib_level > 19) zlib_level = 19;
  if (codec == CTIER_CODEC_LZ4 && zlib_level > 12) zlib_level = 12;
  if (extent_bytes == 0) extent_bytes = 1ull << 20;
  if (ring_capacity == 0) ring_capacity = 4096;
  if (!spill_path) spill_path = "build/tmp/spill_default.dat";
  if (extent_bytes < (64ull << 10) || extent_bytes > (4ull << 20) ||
      extent_bytes % PAGE_SIZE != 0)
    return NULL;
  pool_bytes -= pool_bytes % extent_bytes;
  if (pool_bytes == 0 || max_pages == 0 || max_pages > 0xFFFFFFF0ull)
    return NULL;

  struct ctier *t = calloc(1, sizeof(*t));
  if (!t) return NULL;
  t->pool_bytes = pool_bytes;
  t->extent_bytes = extent_bytes;
  t->n_extents = (uint32_t)(pool_bytes / extent_bytes);
  t->max_pages = max_pages;
  t->zlib_level = zlib_level;
  t->codec = codec;
  t->flags = flags;
  t->spill.fd = -1;

  int locks_ok = 0, pool_ok = 0, spill_ok = 0, hist_ok = 0;
  t->pool = mmap(NULL, pool_bytes, PROT_READ | PROT_WRITE,
                 MAP_ANON | MAP_PRIVATE, -1, 0);
  if (t->pool == MAP_FAILED) { t->pool = NULL; goto fail; }
  t->pte = calloc(max_pages, sizeof(pte_t));
  t->ext = calloc(t->n_extents, sizeof(extent_t));
  if (!t->pte || !t->ext) goto fail;

  if (pthread_rwlock_init(&t->structural, NULL) != 0) goto fail;
  if (pthread_mutex_init(&t->stack_mx, NULL) != 0) {
    pthread_rwlock_destroy(&t->structural);
    goto fail;
  }
  for (int i = 0; i < N_CLASSES; i++) pthread_mutex_init(&t->class_mx[i], NULL);
  for (int i = 0; i < N_BUCKETS; i++) pthread_mutex_init(&t->bucket_mx[i], NULL);
  locks_ok = 1;

  if (ct_pool_init(t) != 0) goto fail;
  pool_ok = 1;
  if (ct_spill_init(&t->spill, spill_path) != 0) goto fail;
  spill_ok = 1;
  if (!(flags & CTIER_F_NOHIST)) {
    if (ct_hist_init(&t->hist, ring_capacity) != 0) goto fail;
    hist_ok = 1;
  }

  ct_stat_set(t, CTIER_STAT_POOL_BYTES, pool_bytes);
  uint64_t meta = sizeof(*t) + max_pages * sizeof(pte_t) +
                  (uint64_t)t->n_extents * sizeof(extent_t) +
                  (uint64_t)t->n_extents * sizeof(uint32_t) +
                  (hist_ok ? (uint64_t)ring_capacity * sizeof(uint16_t) : 0);
  ct_stat_set(t, CTIER_STAT_META_BYTES, meta);
  return t;

fail:
  if (hist_ok) ct_hist_destroy(&t->hist);
  if (spill_ok) ct_spill_destroy(&t->spill);
  if (pool_ok) ct_pool_destroy(t);
  if (locks_ok) {
    pthread_rwlock_destroy(&t->structural);
    pthread_mutex_destroy(&t->stack_mx);
    for (int i = 0; i < N_CLASSES; i++) pthread_mutex_destroy(&t->class_mx[i]);
    for (int i = 0; i < N_BUCKETS; i++) pthread_mutex_destroy(&t->bucket_mx[i]);
  }
  free(t->ext);
  free(t->pte);
  if (t->pool) munmap(t->pool, pool_bytes);
  free(t);
  return NULL;
}

void ctier_destroy(ctier_t *t) {
  if (!t) return;
  ct_feedback_destroy(t->feedback);
  if (!(t->flags & CTIER_F_NOHIST)) ct_hist_destroy(&t->hist);
  ct_spill_destroy(&t->spill);
  ct_pool_destroy(t);
  pthread_rwlock_destroy(&t->structural);
  pthread_mutex_destroy(&t->stack_mx);
  for (int i = 0; i < N_CLASSES; i++) pthread_mutex_destroy(&t->class_mx[i]);
  for (int i = 0; i < N_BUCKETS; i++) pthread_mutex_destroy(&t->bucket_mx[i]);
  free(t->ext);
  free(t->pte);
  munmap(t->pool, t->pool_bytes);
  free(t);
}

/* ---------- write ---------- */

int64_t ctier_write(ctier_t *t, uint64_t lpn, const uint8_t *src) {
  static _Thread_local uint8_t tls_comp[8192];
  if (!t || !src || lpn >= t->max_pages) return CTIER_EINVAL;

  uint8_t new_state;
  int k_new = -1;
  uint16_t comp_size = 0;
  const uint8_t *payload = NULL;
  uint32_t payload_len = 0;

  if (is_all_zero(src)) {
    new_state = CT_ZERO;
  } else {
    uint32_t dlen = sizeof(tls_comp);
    uint64_t t0 = ctier_now_ns();
    int zrc = ct_codec_compress(t->codec, t->zlib_level, src, PAGE_SIZE,
                                tls_comp, sizeof(tls_comp), &dlen);
    ct_stat_add(t, CTIER_STAT_COMP_NS_TOTAL, ctier_now_ns() - t0);
    if (zrc != 0 || dlen >= PAGE_SIZE) {
      new_state = CT_PRESENT_RAW;
      k_new = N_CLASSES - 1;
      comp_size = PAGE_SIZE;
      payload = src;
      payload_len = PAGE_SIZE;
    } else {
      new_state = CT_PRESENT_COMP;
      comp_size = (uint16_t)dlen;
      k_new = ((int)comp_size - 1) / 128;
      payload = tls_comp;
      payload_len = comp_size;
    }
  }
  uint16_t recorded =
      (new_state == CT_ZERO) ? 0 : (uint16_t)ct_class_size(k_new);

  pthread_mutex_lock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
  pte_t *p = &t->pte[lpn];
  uint8_t old_state = p->state;
  int k_old = (old_state == CT_PRESENT_COMP || old_state == CT_PRESENT_RAW)
                  ? (int)p->class_idx : -1;
  int need_chunk = (new_state != CT_ZERO);

  lock_classes(t, need_chunk ? k_new : -1, k_old);
  pthread_rwlock_rdlock(&t->structural);
  int have_wr = 0;
  uint32_t ne = 0;
  uint16_t nc = 0;
  if (need_chunk) {
    if (ct_chunk_alloc_fast(t, k_new, lpn, &ne, &nc) != 0) {
      pthread_rwlock_unlock(&t->structural);
      pthread_rwlock_wrlock(&t->structural);
      have_wr = 1;
      if (ct_chunk_alloc_slow(t, k_new, lpn, &ne, &nc) != 0) {
        uint16_t failed_generation = (uint16_t)(
            p->generation + (old_state == CT_FREE ? 1u : 0u));
        pthread_rwlock_unlock(&t->structural);
        unlock_classes(t, k_new, k_old);
        if (t->feedback)
          (void)ct_feedback_emit(t->feedback, lpn, failed_generation,
                                 (uint8_t)(k_new >= 0 ? k_new : 0),
                                 CT_FEEDBACK_ENOSPACE);
        pthread_mutex_unlock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
        return CTIER_ENOSPACE;
      }
    }
    memcpy(t->ext[ne].base + (uint64_t)nc * ct_class_size(k_new),
           payload, payload_len);
  }
  /* release old resources only after the new placement is durable */
  uint32_t old_slot = p->extent_idx;
  if (old_state == CT_PRESENT_COMP || old_state == CT_PRESENT_RAW)
    ct_chunk_free(t, p->extent_idx, p->chunk_idx);
  /* PTE update while structural is still held */
  p->state = new_state;
  p->class_idx = (uint8_t)(k_new >= 0 ? k_new : 0);
  p->comp_size = comp_size;
  /* A generation identifies an allocation lifetime. Rewrites retain it;
   * allocation after FREE advances it, preventing hot-page write aliasing. */
  if (old_state == CT_FREE) p->generation++;
  uint16_t completion_generation = p->generation;
  if (need_chunk) { p->extent_idx = ne; p->chunk_idx = nc; }
  else { p->extent_idx = 0; p->chunk_idx = 0; }
  pthread_rwlock_unlock(&t->structural);
  (void)have_wr;
  unlock_classes(t, need_chunk ? k_new : -1, k_old);

  /* gauges (still under the bucket mutex for this lpn) */
  if (old_state == CT_SPILLED) {
    ct_spill_release_slot(&t->spill, old_slot);
    ct_stat_sub(t, CTIER_STAT_PAGES_SPILLED, 1);
    ct_stat_add(t, CTIER_STAT_PAGES_PRESENT, 1);
  } else if (old_state == CT_FREE) {
    ct_stat_add(t, CTIER_STAT_PAGES_PRESENT, 1);
  }
  if (old_state == CT_ZERO) ct_stat_sub(t, CTIER_STAT_PAGES_ZERO, 1);
  if (old_state == CT_PRESENT_RAW) ct_stat_sub(t, CTIER_STAT_PAGES_RAW, 1);
  if (new_state == CT_ZERO) ct_stat_add(t, CTIER_STAT_PAGES_ZERO, 1);
  if (new_state == CT_PRESENT_RAW) ct_stat_add(t, CTIER_STAT_PAGES_RAW, 1);
  if (t->feedback) {
    uint8_t status = new_state == CT_ZERO ? CT_FEEDBACK_ZERO :
                     new_state == CT_PRESENT_RAW ? CT_FEEDBACK_RAW :
                     CT_FEEDBACK_OK;
    (void)ct_feedback_emit(t->feedback, lpn, completion_generation,
                           (uint8_t)(k_new >= 0 ? k_new : 0), status);
  }
  pthread_mutex_unlock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);

  ct_stat_add(t, CTIER_STAT_WRITES, 1);
  if (!(t->flags & CTIER_F_NOHIST)) ct_hist_record(&t->hist, recorded);
  return (int64_t)recorded;
}

int ctier_feedback_enable(ctier_t *t, uint32_t capacity) {
  if (!t || capacity == 0 || t->feedback) return CTIER_EINVAL;
  t->feedback = ct_feedback_create(capacity);
  return t->feedback ? 0 : CTIER_EIO;
}

int ctier_feedback_poll(ctier_t *t, ct_feedback_record_t *out) {
  if (!t || !t->feedback) return CTIER_EINVAL;
  return ct_feedback_poll(t->feedback, out);
}

uint64_t ctier_feedback_dropped(ctier_t *t) {
  return (t && t->feedback) ? ct_feedback_dropped(t->feedback) : 0;
}

/* ---------- read (incl. promote) ---------- */

static int promote_locked(ctier_t *t, uint64_t lpn, pte_t *p, uint8_t *dst) {
  /* caller holds the bucket mutex; p->state == CT_SPILLED */
  uint8_t payload[PAGE_SIZE];
  uint16_t slot_comp = 0;
  uint64_t io = 0;
  uint32_t slot = p->extent_idx;
  int64_t plen = ct_spill_get(&t->spill, slot, (uint32_t)lpn, &slot_comp,
                              payload, &io);
  if (plen < 0) return (int)plen;
  uint8_t new_state = (slot_comp == 0) ? CT_PRESENT_RAW : CT_PRESENT_COMP;
  int k = (slot_comp == 0) ? N_CLASSES - 1 : ((int)slot_comp - 1) / 128;

  pthread_mutex_lock(&t->class_mx[k]);
  pthread_rwlock_rdlock(&t->structural);
  uint32_t ne = 0;
  uint16_t nc = 0;
  if (ct_chunk_alloc_fast(t, k, lpn, &ne, &nc) != 0) {
    pthread_rwlock_unlock(&t->structural);
    pthread_rwlock_wrlock(&t->structural);
    if (ct_chunk_alloc_slow(t, k, lpn, &ne, &nc) != 0) {
      pthread_rwlock_unlock(&t->structural);
      pthread_mutex_unlock(&t->class_mx[k]);
      return CTIER_ENOSPACE;
    }
  }
  memcpy(t->ext[ne].base + (uint64_t)nc * ct_class_size(k), payload,
         (uint32_t)plen);
  p->state = new_state;
  p->class_idx = (uint8_t)k;
  p->comp_size = (slot_comp == 0) ? PAGE_SIZE : slot_comp;
  p->extent_idx = ne;
  p->chunk_idx = nc;
  pthread_rwlock_unlock(&t->structural);
  pthread_mutex_unlock(&t->class_mx[k]);

  ct_spill_release_slot(&t->spill, slot);
  ct_stat_add(t, CTIER_STAT_SPILL_BYTES_R, io);
  ct_stat_sub(t, CTIER_STAT_PAGES_SPILLED, 1);
  ct_stat_add(t, CTIER_STAT_PAGES_PRESENT, 1);
  if (new_state == CT_PRESENT_RAW) ct_stat_add(t, CTIER_STAT_PAGES_RAW, 1);
  ct_stat_add(t, CTIER_STAT_PROMOTES, 1);

  /* deliver data from the local copy */
  if (new_state == CT_PRESENT_RAW) {
    memcpy(dst, payload, PAGE_SIZE);
  } else {
    uint32_t dl = PAGE_SIZE;
    uint64_t t0 = ctier_now_ns();
    int zrc = ct_codec_decompress(t->codec, payload, plen, dst, PAGE_SIZE, &dl);
    ct_stat_add(t, CTIER_STAT_DECOMP_NS_TOTAL, ctier_now_ns() - t0);
    if (zrc != 0 || dl != PAGE_SIZE) return CTIER_ECORRUPT;
  }
  return 0;
}

int ctier_read(ctier_t *t, uint64_t lpn, uint8_t *dst) {
  if (!t || !dst || lpn >= t->max_pages) return CTIER_EINVAL;
  int rc = 0;
  pthread_mutex_lock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
  pte_t *p = &t->pte[lpn];
  switch (p->state) {
  case CT_FREE:
    rc = CTIER_EFAULT;
    break;
  case CT_ZERO:
    memset(dst, 0, PAGE_SIZE);
    break;
  case CT_PRESENT_RAW: {
    pthread_rwlock_rdlock(&t->structural);
    extent_t *e = &t->ext[p->extent_idx];
    memcpy(dst, e->base + (uint64_t)p->chunk_idx * ct_class_size(e->class_idx),
           PAGE_SIZE);
    pthread_rwlock_unlock(&t->structural);
    break;
  }
  case CT_PRESENT_COMP: {
    pthread_rwlock_rdlock(&t->structural);
    extent_t *e = &t->ext[p->extent_idx];
    const uint8_t *chunk =
        e->base + (uint64_t)p->chunk_idx * ct_class_size(e->class_idx);
    uint32_t dl = PAGE_SIZE;
    uint64_t t0 = ctier_now_ns();
    int zrc = ct_codec_decompress(t->codec, chunk, p->comp_size, dst, PAGE_SIZE,
                                  &dl);
    uint64_t dt = ctier_now_ns() - t0;
    pthread_rwlock_unlock(&t->structural);
    ct_stat_add(t, CTIER_STAT_DECOMP_NS_TOTAL, dt);
    if (zrc != 0 || dl != PAGE_SIZE) rc = CTIER_ECORRUPT;
    break;
  }
  case CT_SPILLED:
    rc = promote_locked(t, lpn, p, dst);
    break;
  default:
    rc = CTIER_EINVAL;
  }
  pthread_mutex_unlock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
  if (rc == 0) ct_stat_add(t, CTIER_STAT_READS, 1);
  return rc;
}

/* ---------- free ---------- */

int ctier_free_page(ctier_t *t, uint64_t lpn) {
  if (!t || lpn >= t->max_pages) return CTIER_EINVAL;
  int ret = 0;
  pthread_mutex_lock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
  pte_t *p = &t->pte[lpn];
  uint16_t completion_generation = p->generation;
  uint8_t completion_class = p->class_idx;
  switch (p->state) {
  case CT_FREE:
    ret = CTIER_EFAULT;
    break;
  case CT_ZERO:
    ct_stat_sub(t, CTIER_STAT_PAGES_ZERO, 1);
    ct_stat_sub(t, CTIER_STAT_PAGES_PRESENT, 1);
    p->state = CT_FREE;
    ret = 0;
    break;
  case CT_SPILLED:
    ct_spill_release_slot(&t->spill, p->extent_idx);
    ct_stat_sub(t, CTIER_STAT_PAGES_SPILLED, 1);
    p->state = CT_FREE;
    ret = 0;
    break;
  case CT_PRESENT_COMP:
  case CT_PRESENT_RAW: {
    int k = p->class_idx;
    pthread_mutex_lock(&t->class_mx[k]);
    pthread_rwlock_rdlock(&t->structural);
    ct_chunk_free(t, p->extent_idx, p->chunk_idx);
    pthread_rwlock_unlock(&t->structural);
    pthread_mutex_unlock(&t->class_mx[k]);
    if (p->state == CT_PRESENT_RAW) ct_stat_sub(t, CTIER_STAT_PAGES_RAW, 1);
    ct_stat_sub(t, CTIER_STAT_PAGES_PRESENT, 1);
    ret = (int)ct_class_size(k);
    p->state = CT_FREE;
    break;
  }
  default:
    ret = CTIER_EINVAL;
  }
  if (ret >= 0) {
    p->comp_size = 0;
    p->extent_idx = 0;
    p->chunk_idx = 0;
    p->class_idx = 0;
  }
  if (ret >= 0 && t->feedback)
    (void)ct_feedback_emit(t->feedback, lpn, completion_generation,
                           completion_class, CT_FEEDBACK_FREE);
  pthread_mutex_unlock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
  return ret;
}

/* ---------- spill ---------- */

int ctier_spill(ctier_t *t, uint64_t lpn) {
  if (!t || lpn >= t->max_pages) return CTIER_EINVAL;
  uint8_t payload[PAGE_SIZE];
  int ret = 0;
  pthread_mutex_lock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
  pte_t *p = &t->pte[lpn];
  switch (p->state) {
  case CT_FREE:
    ret = CTIER_EFAULT;
    break;
  case CT_ZERO:
  case CT_SPILLED:
    ret = 0;                      /* successful no-op: nothing in the pool */
    break;
  case CT_PRESENT_COMP:
  case CT_PRESENT_RAW: {
    int k = p->class_idx;
    int raw = (p->state == CT_PRESENT_RAW);
    uint32_t plen = raw ? PAGE_SIZE : p->comp_size;
    uint16_t hdr_comp = raw ? 0 : p->comp_size;
    pthread_mutex_lock(&t->class_mx[k]);
    pthread_rwlock_rdlock(&t->structural);
    extent_t *e = &t->ext[p->extent_idx];
    memcpy(payload,
           e->base + (uint64_t)p->chunk_idx * ct_class_size(e->class_idx),
           plen);
    uint64_t io = 0;
    int64_t meta_delta = 0;
    int64_t slot = ct_spill_put(&t->spill, (uint32_t)lpn, hdr_comp, payload,
                                plen, &io, &meta_delta);
    if (slot < 0) {
      pthread_rwlock_unlock(&t->structural);
      pthread_mutex_unlock(&t->class_mx[k]);
      ret = (int)slot;
      break;
    }
    ct_chunk_free(t, p->extent_idx, p->chunk_idx);
    p->state = CT_SPILLED;
    p->extent_idx = (uint32_t)slot;
    p->chunk_idx = 0;
    pthread_rwlock_unlock(&t->structural);
    pthread_mutex_unlock(&t->class_mx[k]);
    if (meta_delta > 0) ct_stat_add(t, CTIER_STAT_META_BYTES, (uint64_t)meta_delta);
    else if (meta_delta < 0) ct_stat_sub(t, CTIER_STAT_META_BYTES, (uint64_t)(-meta_delta));
    ct_stat_add(t, CTIER_STAT_SPILL_BYTES_W, io);
    ct_stat_add(t, CTIER_STAT_PAGES_SPILLED, 1);
    ct_stat_sub(t, CTIER_STAT_PAGES_PRESENT, 1);
    if (raw) ct_stat_sub(t, CTIER_STAT_PAGES_RAW, 1);
    ret = (int)ct_class_size(k);
    break;
  }
  default:
    ret = CTIER_EINVAL;
  }
  pthread_mutex_unlock(&t->bucket_mx[lpn & (N_BUCKETS - 1)]);
  return ret;
}

/* ---------- compaction ---------- */

int64_t ctier_compact(ctier_t *t) {
  if (!t) return CTIER_EINVAL;
  int64_t released = 0;
  pthread_rwlock_wrlock(&t->structural);
  for (int k = 0; k < N_CLASSES; k++) released += ct_compact_class(t, k);
  pthread_rwlock_unlock(&t->structural);
  ct_stat_add(t, CTIER_STAT_COMPACTIONS, 1);
  return released;
}

/* ---------- stats / telemetry ---------- */

uint64_t ctier_stat_u64(ctier_t *t, int stat_id) {
  if (!t || stat_id < 0 || stat_id > CTIER_STAT_META_BYTES) return 0;
  return ct_stat_get(t, stat_id);
}

double ctier_stat_f64(ctier_t *t, int stat_id) {
  if (!t || (t->flags & CTIER_F_NOHIST)) return 0.0;
  double sm, sv, fm;
  ct_hist_read_ewma(&t->hist, &sm, &sv, &fm);
  switch (stat_id) {
  case CTIER_STAT_EWMA_MEAN_SLOW: return sm;
  case CTIER_STAT_EWMA_VAR_SLOW:  return sv;
  case CTIER_STAT_EWMA_MEAN_FAST: return fm;
  default: return 0.0;
  }
}

int ctier_hist_snapshot(ctier_t *t, double out[N_CLASSES]) {
  if (!t || !out) return CTIER_EINVAL;
  if (t->flags & CTIER_F_NOHIST) return CTIER_EINVAL;
  return ct_hist_snapshot(&t->hist, out);
}

int ctier_ring_snapshot(ctier_t *t, uint16_t *out, int cap) {
  if (!t || !out || cap < 0) return CTIER_EINVAL;
  if (t->flags & CTIER_F_NOHIST) return 0;
  return ct_ring_snapshot(&t->hist, out, cap);
}

int ctier_ring_moments(ctier_t *t, uint32_t w, double *mu, double *sigma) {
  if (!t) return CTIER_EINVAL;
  if (t->flags & CTIER_F_NOHIST) {
    if (mu) *mu = 0.0;
    if (sigma) *sigma = 0.0;
    return 0;
  }
  return ct_ring_moments(&t->hist, w, mu, sigma);
}

int ctier_cusum(ctier_t *t, double *stat, uint64_t *alarm) {
  if (!t) return CTIER_EINVAL;
  if (t->flags & CTIER_F_NOHIST) {
    if (stat) *stat = 0.0;
    if (alarm) *alarm = 0;
    return 0;
  }
  ct_hist_read_cusum(&t->hist, stat, alarm);
  return 0;
}

int ctier_cusum_reset(ctier_t *t) {
  if (!t) return CTIER_EINVAL;
  if (!(t->flags & CTIER_F_NOHIST)) ct_hist_reset_cusum(&t->hist);
  return 0;
}

const char *ctier_codec_name(int codec) { return ct_codec_name(codec); }
