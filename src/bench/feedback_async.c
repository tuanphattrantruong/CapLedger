/* feedback_async.c -- E13 asynchronous feedback batching and staleness.
 * The 16-byte ABI remains unchanged.  Tokens index harness-side emission
 * timestamps so this experiment can measure visibility latency without
 * smuggling a timestamp into the proposed device record. */
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "feedback.h"

typedef struct {
  ct_feedback_queue_t *q;
  uint64_t n, producer_gap_ns, poll_ns;
  uint32_t batch;
  uint64_t *emitted, *latency;
  _Atomic uint64_t consumed, max_depth;
  _Atomic int start, failed;
  ct_feedback_tracker_t tracker;
} run_t;

static uint64_t now_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void spin_until(uint64_t deadline) {
  while (now_ns() < deadline) sched_yield();
}

static void update_max(_Atomic uint64_t *dst, uint64_t value) {
  uint64_t old = atomic_load_explicit(dst, memory_order_relaxed);
  while (value > old && !atomic_compare_exchange_weak_explicit(
             dst, &old, value, memory_order_relaxed, memory_order_relaxed)) { }
}

static void *producer(void *arg) {
  run_t *r = arg;
  while (!atomic_load_explicit(&r->start, memory_order_acquire)) sched_yield();
  uint64_t next = now_ns();
  for (uint64_t i = 0; i < r->n; i++) {
    if (r->producer_gap_ns) spin_until(next);
    r->emitted[i] = now_ns();
    if (ct_feedback_emit(r->q, i + 1, (uint16_t)i,
                         (uint8_t)(i & 31), 0) != 0) {
      atomic_store(&r->failed, 1);
      break;
    }
    update_max(&r->max_depth, ct_feedback_depth(r->q));
    next += r->producer_gap_ns;
  }
  return NULL;
}

static void *consumer(void *arg) {
  run_t *r = arg;
  atomic_store_explicit(&r->start, 1, memory_order_release);
  uint64_t wake = now_ns();
  while (atomic_load_explicit(&r->consumed, memory_order_relaxed) < r->n &&
         !atomic_load_explicit(&r->failed, memory_order_relaxed)) {
    if (r->poll_ns) {
      wake += r->poll_ns;
      spin_until(wake);
    }
    uint32_t drained = 0;
    while (drained < r->batch) {
      ct_feedback_record_t rec;
      int got = ct_feedback_poll(r->q, &rec);
      if (got <= 0) break;
      if (rec.token == 0 || rec.token > r->n) {
        atomic_store(&r->failed, 1);
        break;
      }
      uint64_t idx = rec.token - 1;
      r->latency[idx] = now_ns() - r->emitted[idx];
      if (ct_feedback_tracker_apply(&r->tracker, &rec, 4096) == 1)
        atomic_store(&r->failed, 1);
      atomic_fetch_add_explicit(&r->consumed, 1, memory_order_relaxed);
      drained++;
    }
    if (!r->poll_ns && drained == 0) sched_yield();
  }
  return NULL;
}

static int cmp_u64(const void *a, const void *b) {
  uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
  return (x > y) - (x < y);
}

typedef struct {
  uint64_t poll_ns;
  uint32_t batch;
  const char *name;
} cell_t;

int main(int argc, char **argv) {
  uint64_t n = 100000, gap_ns = 5000;
  const char *out = NULL;
  for (int i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--records") && i + 1 < argc)
      n = strtoull(argv[++i], NULL, 0);
    else if (!strcmp(argv[i], "--producer-gap-ns") && i + 1 < argc)
      gap_ns = strtoull(argv[++i], NULL, 0);
    else if (!strcmp(argv[i], "--out") && i + 1 < argc)
      out = argv[++i];
    else return 2;
  }
  if (!out || n < 1000 || n > 2000000) return 2;
  const cell_t cells[] = {
    {0, 1, "busy_batch1"},
    {10000, 8, "poll10us_batch8"},
    {100000, 64, "poll100us_batch64"},
    {1000000, 256, "poll1ms_batch256"},
  };
  FILE *f = fopen(out, "w");
  if (!f) return 2;
  fprintf(f, "{\n  \"experiment\": \"E13_async_feedback\",\n");
  fprintf(f, "  \"record_bytes\": 16, \"records_per_cell\": %llu, "
             "\"producer_gap_ns\": %llu,\n  \"cells\": [\n",
          (unsigned long long)n, (unsigned long long)gap_ns);
  int all_ok = 1;
  for (size_t ci = 0; ci < sizeof(cells) / sizeof(cells[0]); ci++) {
    run_t r = {.n=n, .producer_gap_ns=gap_ns, .poll_ns=cells[ci].poll_ns,
               .batch=cells[ci].batch};
    r.q = ct_feedback_create((uint32_t)n);
    r.emitted = calloc((size_t)n, sizeof(uint64_t));
    r.latency = calloc((size_t)n, sizeof(uint64_t));
    ct_feedback_tracker_init(&r.tracker);
    if (!r.q || !r.emitted || !r.latency) return 2;
    pthread_t pt, ct;
    pthread_create(&pt, NULL, producer, &r);
    pthread_create(&ct, NULL, consumer, &r);
    pthread_join(pt, NULL);
    pthread_join(ct, NULL);
    uint64_t consumed = atomic_load(&r.consumed);
    uint64_t dropped = ct_feedback_dropped(r.q);
    qsort(r.latency, (size_t)consumed, sizeof(uint64_t), cmp_u64);
    uint64_t p50 = r.latency[consumed / 2];
    uint64_t p99 = r.latency[(consumed * 99) / 100];
    uint64_t max = r.latency[consumed - 1];
    uint64_t depth = atomic_load(&r.max_depth);
    int ok = !atomic_load(&r.failed) && consumed == n && dropped == 0 &&
             r.tracker.gap_records == 0 && r.tracker.stale_records == 0;
    all_ok &= ok;
    fprintf(f, "    %s{\"name\":\"%s\",\"poll_ns\":%llu,"
               "\"batch\":%u,\"consumed\":%llu,\"dropped\":%llu,"
               "\"gap_records\":%llu,\"stale_records\":%llu,"
               "\"latency_ns_p50\":%llu,\"latency_ns_p99\":%llu,"
               "\"latency_ns_max\":%llu,\"max_depth\":%llu,"
               "\"pessimistic_headroom_bytes\":%llu,\"pass\":%s}",
            ci ? ",\n" : "", cells[ci].name,
            (unsigned long long)cells[ci].poll_ns, cells[ci].batch,
            (unsigned long long)consumed, (unsigned long long)dropped,
            (unsigned long long)r.tracker.gap_records,
            (unsigned long long)r.tracker.stale_records,
            (unsigned long long)p50, (unsigned long long)p99,
            (unsigned long long)max, (unsigned long long)depth,
            (unsigned long long)(depth * 4096ull), ok ? "true" : "false");
    fprintf(stderr, "E13 %-20s p50=%8.1fus p99=%8.1fus depth=%llu %s\n",
            cells[ci].name, p50 / 1000.0, p99 / 1000.0,
            (unsigned long long)depth, ok ? "PASS" : "FAIL");
    ct_feedback_destroy(r.q);
    free(r.emitted); free(r.latency);
  }
  fprintf(f, "\n  ],\n  \"pass\": %s\n}\n", all_ok ? "true" : "false");
  fclose(f);
  return all_ok ? 0 : 1;
}
