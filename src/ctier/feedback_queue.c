/* feedback_queue.c -- completion queue, loss tracker, and placeability gate. */
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include "feedback.h"

_Static_assert(sizeof(ct_feedback_record_t) == 16,
               "completion ABI must remain 16 bytes");

struct ct_feedback_queue {
  pthread_mutex_t lock;
  ct_feedback_record_t *ring;
  uint32_t capacity, head, tail, count, next_sequence;
  uint64_t dropped;
};

ct_feedback_queue_t *ct_feedback_create(uint32_t capacity) {
  if (capacity == 0) return NULL;
  ct_feedback_queue_t *q = calloc(1, sizeof(*q));
  if (!q) return NULL;
  q->ring = calloc(capacity, sizeof(*q->ring));
  if (!q->ring || pthread_mutex_init(&q->lock, NULL) != 0) {
    free(q->ring); free(q); return NULL;
  }
  q->capacity = capacity;
  q->next_sequence = 1;
  return q;
}

void ct_feedback_destroy(ct_feedback_queue_t *q) {
  if (!q) return;
  pthread_mutex_destroy(&q->lock);
  free(q->ring);
  free(q);
}

int ct_feedback_emit(ct_feedback_queue_t *q, uint64_t token,
                     uint16_t generation, uint8_t size_class,
                     uint8_t status) {
  if (!q || size_class >= 32) return -1;
  pthread_mutex_lock(&q->lock);
  uint32_t sequence = q->next_sequence++;
  if (q->count == q->capacity) {
    q->dropped++;
    pthread_mutex_unlock(&q->lock);
    return -1;
  }
  q->ring[q->tail] = (ct_feedback_record_t){
      .token = token, .sequence = sequence, .size_class = size_class,
      .status = status, .generation = generation};
  q->tail = (q->tail + 1) % q->capacity;
  q->count++;
  pthread_mutex_unlock(&q->lock);
  return 0;
}

int ct_feedback_poll(ct_feedback_queue_t *q, ct_feedback_record_t *out) {
  if (!q || !out) return -1;
  pthread_mutex_lock(&q->lock);
  if (q->count == 0) { pthread_mutex_unlock(&q->lock); return 0; }
  *out = q->ring[q->head];
  q->head = (q->head + 1) % q->capacity;
  q->count--;
  pthread_mutex_unlock(&q->lock);
  return 1;
}

uint64_t ct_feedback_dropped(const ct_feedback_queue_t *cq) {
  if (!cq) return 0;
  ct_feedback_queue_t *q = (ct_feedback_queue_t *)cq;
  pthread_mutex_lock(&q->lock);
  uint64_t out = q->dropped;
  pthread_mutex_unlock(&q->lock);
  return out;
}

uint32_t ct_feedback_depth(const ct_feedback_queue_t *cq) {
  if (!cq) return 0;
  ct_feedback_queue_t *q = (ct_feedback_queue_t *)cq;
  pthread_mutex_lock(&q->lock);
  uint32_t out = q->count;
  pthread_mutex_unlock(&q->lock);
  return out;
}

void ct_feedback_tracker_init(ct_feedback_tracker_t *t) {
  if (!t) return;
  memset(t, 0, sizeof(*t));
  t->expected_sequence = 1;
}

int ct_feedback_tracker_apply(ct_feedback_tracker_t *t,
                              const ct_feedback_record_t *r,
                              uint64_t bytes_per_missing) {
  if (!t || !r) return -1;
  int32_t delta = (int32_t)(r->sequence - t->expected_sequence);
  if (delta < 0) { t->stale_records++; return 1; }
  if (delta > 0) {
    t->gap_records += (uint32_t)delta;
    t->telemetry_debt_bytes += (uint64_t)(uint32_t)delta * bytes_per_missing;
  }
  t->expected_sequence = r->sequence + 1;
  return delta > 0 ? 2 : 0;
}

void ct_feedback_tracker_resync(ct_feedback_tracker_t *t,
                                uint32_t next_sequence) {
  if (!t) return;
  t->expected_sequence = next_sequence;
  t->telemetry_debt_bytes = 0;
}

struct ct_place_gate {
  pthread_mutex_t lock;
  uint64_t extent_bytes;
  uint32_t free_extents;
  uint64_t free_slots[32];
};

static uint64_t class_bytes(uint16_t k) { return 128ull * (uint64_t)(k + 1); }

ct_place_gate_t *ct_place_gate_create(uint64_t extent_bytes,
                                      uint32_t free_extents,
                                      const uint64_t initial[32]) {
  if (extent_bytes < 4096 || extent_bytes % 4096 != 0) return NULL;
  ct_place_gate_t *g = calloc(1, sizeof(*g));
  if (!g || pthread_mutex_init(&g->lock, NULL) != 0) { free(g); return NULL; }
  g->extent_bytes = extent_bytes;
  g->free_extents = free_extents;
  if (initial) memcpy(g->free_slots, initial, sizeof(g->free_slots));
  return g;
}

void ct_place_gate_destroy(ct_place_gate_t *g) {
  if (!g) return;
  pthread_mutex_destroy(&g->lock);
  free(g);
}

int ct_place_gate_reserve(ct_place_gate_t *g, uint16_t k, uint64_t pages) {
  if (!g || k >= 32) return -1;
  pthread_mutex_lock(&g->lock);
  uint64_t have = g->free_slots[k];
  if (have < pages) {
    uint64_t slots_per_extent = g->extent_bytes / class_bytes(k);
    uint64_t need = pages - have;
    uint64_t extents = (need + slots_per_extent - 1) / slots_per_extent;
    if (slots_per_extent == 0 || extents > g->free_extents) {
      pthread_mutex_unlock(&g->lock);
      return -1;
    }
    g->free_extents -= (uint32_t)extents;
    g->free_slots[k] += extents * slots_per_extent;
  }
  g->free_slots[k] -= pages;
  pthread_mutex_unlock(&g->lock);
  return 0;
}

void ct_place_gate_release(ct_place_gate_t *g, uint16_t k, uint64_t pages) {
  if (!g || k >= 32) return;
  pthread_mutex_lock(&g->lock);
  g->free_slots[k] += pages;
  pthread_mutex_unlock(&g->lock);
}

uint64_t ct_place_gate_aggregate_free_bytes(const ct_place_gate_t *cg) {
  if (!cg) return 0;
  ct_place_gate_t *g = (ct_place_gate_t *)cg;
  pthread_mutex_lock(&g->lock);
  uint64_t total = (uint64_t)g->free_extents * g->extent_bytes;
  for (uint16_t k = 0; k < 32; k++) total += g->free_slots[k] * class_bytes(k);
  pthread_mutex_unlock(&g->lock);
  return total;
}

uint32_t ct_place_gate_free_extents(const ct_place_gate_t *cg) {
  if (!cg) return 0;
  ct_place_gate_t *g = (ct_place_gate_t *)cg;
  pthread_mutex_lock(&g->lock);
  uint32_t out = g->free_extents;
  pthread_mutex_unlock(&g->lock);
  return out;
}
