/* pool.c — size classes, extents, chunk allocation, compaction. */
#include <stdlib.h>
#include <string.h>
#include "internal.h"

/* ---- partial-list helpers (caller holds class mutex + rd, or wr) ---- */

static void partial_push(struct ctier *t, int k, uint32_t e_idx) {
  extent_t *e = &t->ext[e_idx];
  if (e->in_partial) return;
  e->prev_p = EXT_NIL;
  e->next_p = t->partial_head[k];
  if (t->partial_head[k] != EXT_NIL)
    t->ext[t->partial_head[k]].prev_p = (int32_t)e_idx;
  t->partial_head[k] = (int32_t)e_idx;
  e->in_partial = 1;
}

static void partial_remove(struct ctier *t, int k, uint32_t e_idx) {
  extent_t *e = &t->ext[e_idx];
  if (!e->in_partial) return;
  if (e->prev_p != EXT_NIL) t->ext[e->prev_p].next_p = e->next_p;
  else t->partial_head[k] = e->next_p;
  if (e->next_p != EXT_NIL) t->ext[e->next_p].prev_p = e->prev_p;
  e->next_p = e->prev_p = EXT_NIL;
  e->in_partial = 0;
}

/* ---- free-extent stack (leaf mutex) ---- */

static int stack_pop(struct ctier *t, uint32_t *e_idx) {
  pthread_mutex_lock(&t->stack_mx);
  if (t->free_top == 0) { pthread_mutex_unlock(&t->stack_mx); return -1; }
  *e_idx = t->free_stack[--t->free_top];
  pthread_mutex_unlock(&t->stack_mx);
  return 0;
}

static void stack_push(struct ctier *t, uint32_t e_idx) {
  pthread_mutex_lock(&t->stack_mx);
  t->free_stack[t->free_top++] = e_idx;
  pthread_mutex_unlock(&t->stack_mx);
}

/* release an empty, not-in-partial extent back to the free stack.
 * Caller holds (class mutex + rd) or wr; live must be 0. */
static void release_extent(struct ctier *t, uint32_t e_idx) {
  extent_t *e = &t->ext[e_idx];
  if (e->owner_lpn) {
    ct_stat_sub(t, CTIER_STAT_META_BYTES, (uint64_t)e->nchunks * sizeof(uint32_t));
    free(e->owner_lpn);
    e->owner_lpn = NULL;
  }
  e->class_idx = CLASS_NIL;
  e->nchunks = 0;
  e->free_head = CHUNK_NIL;
  ct_stat_sub(t, CTIER_STAT_EXTENTS_ACTIVE, 1);
  ct_stat_sub(t, CTIER_STAT_PHYS_RESERVED, t->extent_bytes);
  stack_push(t, e_idx);
}

/* assign a free extent to class k. Caller holds structural WR.
 * Returns 0 or -1 (stack empty / malloc failure). */
static int assign_extent(struct ctier *t, int k) {
  uint32_t e_idx;
  if (stack_pop(t, &e_idx) != 0) return -1;
  extent_t *e = &t->ext[e_idx];
  uint32_t csz = ct_class_size(k);
  uint32_t n = (uint32_t)(t->extent_bytes / csz);
  if (n > 0xFFFEu) n = 0xFFFEu;              /* keep CHUNK_NIL sentinel valid */
  e->owner_lpn = malloc((size_t)n * sizeof(uint32_t));
  if (!e->owner_lpn) { stack_push(t, e_idx); return -1; }
  for (uint32_t i = 0; i < n; i++) e->owner_lpn[i] = OWNER_NIL;
  ct_stat_add(t, CTIER_STAT_META_BYTES, (uint64_t)n * sizeof(uint32_t));
  e->class_idx = (uint16_t)k;
  e->nchunks = (uint16_t)n;
  e->live = 0;
  /* thread the free list through the first 2 bytes of each chunk */
  for (uint32_t i = 0; i < n; i++) {
    uint16_t nxt = (i + 1 < n) ? (uint16_t)(i + 1) : CHUNK_NIL;
    memcpy(e->base + (uint64_t)i * csz, &nxt, sizeof(uint16_t));
  }
  e->free_head = 0;
  ct_stat_add(t, CTIER_STAT_EXTENTS_ACTIVE, 1);
  ct_stat_add(t, CTIER_STAT_PHYS_RESERVED, t->extent_bytes);
  partial_push(t, k, e_idx);
  return 0;
}

/* pop one chunk from extent e (has a free chunk). */
static uint16_t take_chunk(struct ctier *t, int k, uint32_t e_idx, uint64_t lpn) {
  extent_t *e = &t->ext[e_idx];
  uint32_t csz = ct_class_size(k);
  uint16_t c = e->free_head, nxt;
  memcpy(&nxt, e->base + (uint64_t)c * csz, sizeof(uint16_t));
  e->free_head = nxt;
  e->owner_lpn[c] = (uint32_t)lpn;
  e->live++;
  if (e->free_head == CHUNK_NIL) partial_remove(t, k, e_idx);
  ct_stat_add(t, CTIER_STAT_PHYS_USED, csz);
  return c;
}

int ct_chunk_alloc_fast(struct ctier *t, int k, uint64_t lpn,
                        uint32_t *e_idx, uint16_t *chunk) {
  int32_t h = t->partial_head[k];
  if (h == EXT_NIL) return -1;
  *e_idx = (uint32_t)h;
  *chunk = take_chunk(t, k, (uint32_t)h, lpn);
  return 0;
}

int ct_chunk_alloc_slow(struct ctier *t, int k, uint64_t lpn,
                        uint32_t *e_idx, uint16_t *chunk) {
  if (ct_chunk_alloc_fast(t, k, lpn, e_idx, chunk) == 0) return 0;
  if (assign_extent(t, k) == 0)
    return ct_chunk_alloc_fast(t, k, lpn, e_idx, chunk);
  /* targeted compaction of this class */
  if (ct_compact_class(t, k) > 0) {
    ct_stat_add(t, CTIER_STAT_COMPACTIONS, 1);
    if (ct_chunk_alloc_fast(t, k, lpn, e_idx, chunk) == 0) return 0;
    if (assign_extent(t, k) == 0)
      return ct_chunk_alloc_fast(t, k, lpn, e_idx, chunk);
  }
  /* last resort: full compaction may release extents stranded in other
   * classes (documented deviation: strictly widens the "compaction reclaims
   * nothing" condition before declaring ENOSPACE). */
  int64_t rel = 0;
  for (int j = 0; j < N_CLASSES; j++) rel += ct_compact_class(t, j);
  if (rel > 0) {
    ct_stat_add(t, CTIER_STAT_COMPACTIONS, 1);
    if (ct_chunk_alloc_fast(t, k, lpn, e_idx, chunk) == 0) return 0;
    if (assign_extent(t, k) == 0)
      return ct_chunk_alloc_fast(t, k, lpn, e_idx, chunk);
  }
  return CTIER_ENOSPACE;
}

void ct_chunk_free(struct ctier *t, uint32_t e_idx, uint16_t chunk) {
  extent_t *e = &t->ext[e_idx];
  int k = e->class_idx;
  uint32_t csz = ct_class_size(k);
  int was_full = (e->free_head == CHUNK_NIL);
  memcpy(e->base + (uint64_t)chunk * csz, &e->free_head, sizeof(uint16_t));
  e->free_head = chunk;
  e->owner_lpn[chunk] = OWNER_NIL;
  e->live--;
  ct_stat_sub(t, CTIER_STAT_PHYS_USED, csz);
  if (e->live == 0) {
    partial_remove(t, k, e_idx);
    release_extent(t, e_idx);
  } else if (was_full) {
    partial_push(t, k, e_idx);
  }
}

/* Compaction of one class. Caller holds structural WR (exclusive: no class or
 * bucket mutexes needed; only extent_idx/chunk_idx PTE fields are updated). */
int64_t ct_compact_class(struct ctier *t, int k) {
  int64_t released = 0;
  uint32_t csz = ct_class_size(k);
  for (;;) {
    /* scan class-k extents: release empties, find min-live, count free
     * capacity elsewhere */
    int32_t emin = EXT_NIL;
    uint64_t free_elsewhere = 0;
    uint32_t min_live = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < t->n_extents; i++) {
      extent_t *e = &t->ext[i];
      if (e->class_idx != (uint16_t)k) continue;
      if (e->live == 0) {
        partial_remove(t, k, i);
        release_extent(t, i);
        released += (int64_t)t->extent_bytes;
        continue;
      }
      if (e->live < min_live) {
        if (emin != EXT_NIL)
          free_elsewhere += (uint64_t)(t->ext[emin].nchunks - t->ext[emin].live);
        min_live = e->live;
        emin = (int32_t)i;
      } else {
        free_elsewhere += (uint64_t)(e->nchunks - e->live);
      }
    }
    if (emin == EXT_NIL) break;
    extent_t *src = &t->ext[emin];
    if (free_elsewhere < src->live) break;   /* not consolidatable further */
    /* move every live chunk of src into other partial extents of class k */
    partial_remove(t, k, (uint32_t)emin);
    for (uint32_t c = 0; c < src->nchunks && src->live > 0; c++) {
      if (src->owner_lpn[c] == OWNER_NIL) continue;
      int32_t d = t->partial_head[k];
      if (d == EXT_NIL) break;               /* defensive; capacity was checked */
      uint32_t lpn = src->owner_lpn[c];
      uint16_t dc = take_chunk(t, k, (uint32_t)d, lpn);
      /* take_chunk added csz to PHYS_USED; the move is neutral */
      ct_stat_sub(t, CTIER_STAT_PHYS_USED, csz);
      memcpy(t->ext[d].base + (uint64_t)dc * csz,
             src->base + (uint64_t)c * csz, csz);
      t->pte[lpn].extent_idx = (uint32_t)d;
      t->pte[lpn].chunk_idx = dc;
      src->owner_lpn[c] = OWNER_NIL;
      src->live--;
    }
    if (src->live == 0) {
      release_extent(t, (uint32_t)emin);
      released += (int64_t)t->extent_bytes;
    } else {
      /* could not fully drain (defensive path): put it back and stop */
      partial_push(t, k, (uint32_t)emin);
      break;
    }
  }
  return released;
}

int ct_pool_init(struct ctier *t) {
  t->free_stack = malloc((size_t)t->n_extents * sizeof(uint32_t));
  if (!t->free_stack) return -1;
  t->free_top = 0;
  /* push in reverse so extent 0 pops first (determinism) */
  for (uint32_t i = t->n_extents; i > 0; i--)
    t->free_stack[t->free_top++] = i - 1;
  for (int k = 0; k < N_CLASSES; k++) t->partial_head[k] = EXT_NIL;
  for (uint32_t i = 0; i < t->n_extents; i++) {
    extent_t *e = &t->ext[i];
    e->base = t->pool + (uint64_t)i * t->extent_bytes;
    e->class_idx = CLASS_NIL;
    e->nchunks = 0;
    e->live = 0;
    e->free_head = CHUNK_NIL;
    e->owner_lpn = NULL;
    e->next_p = e->prev_p = EXT_NIL;
    e->in_partial = 0;
  }
  return 0;
}

void ct_pool_destroy(struct ctier *t) {
  if (t->ext)
    for (uint32_t i = 0; i < t->n_extents; i++)
      free(t->ext[i].owner_lpn);
  free(t->free_stack);
  t->free_stack = NULL;
}
