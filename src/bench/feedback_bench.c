/* feedback_bench.c -- E12 protocol, loss/recovery, attribution, throughput. */
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ctier.h"
#include "feedback.h"

typedef struct {
  ct_feedback_queue_t *q;
  uint64_t n;
  uint64_t consumed;
  uint64_t token_xor;
} run_t;

static void *producer(void *arg) {
  run_t *r = arg;
  for (uint64_t i = 0; i < r->n; i++) {
    if (ct_feedback_emit(r->q, i + 1, (uint16_t)(i & 0xffff),
                         (uint8_t)(i & 31), 0) != 0) {
      fprintf(stderr, "unexpected overflow at %llu\n", (unsigned long long)i);
      return (void *)1;
    }
  }
  return NULL;
}

static void *consumer(void *arg) {
  run_t *r = arg;
  ct_feedback_record_t rec;
  while (r->consumed < r->n) {
    int got = ct_feedback_poll(r->q, &rec);
    if (got == 1) {
      r->token_xor ^= rec.token;
      r->consumed++;
    } else {
      sched_yield();
    }
  }
  return NULL;
}

int main(int argc, char **argv) {
  uint64_t n = 1000000;
  const char *out = NULL;
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "--records") == 0 && i + 1 < argc)
      n = strtoull(argv[++i], NULL, 0);
    else if (strcmp(argv[i], "--out") == 0 && i + 1 < argc)
      out = argv[++i];
    else { fprintf(stderr, "usage: feedback --records N --out file.json\n"); return 2; }
  }
  if (!out || n == 0 || n > 4000000) return 2;

  /* Loss is explicit and becomes pessimistic debt at the next sequence. */
  ct_feedback_queue_t *small = ct_feedback_create(8);
  for (uint64_t i = 0; i < 16; i++)
    (void)ct_feedback_emit(small, 1000 + i, (uint16_t)i, (uint8_t)(i & 31), 0);
  ct_feedback_tracker_t tracker;
  ct_feedback_tracker_init(&tracker);
  ct_feedback_record_t rec;
  while (ct_feedback_poll(small, &rec) == 1)
    (void)ct_feedback_tracker_apply(&tracker, &rec, 4096);
  /* Sequence 17 reveals the eight completions lost at overflow. */
  (void)ct_feedback_emit(small, 1016, 16, 16, 0);
  (void)ct_feedback_poll(small, &rec);
  int gap_rc = ct_feedback_tracker_apply(&tracker, &rec, 4096);
  uint64_t debt_before_resync = tracker.telemetry_debt_bytes;
  uint64_t gap_records = tracker.gap_records;
  uint64_t dropped = ct_feedback_dropped(small);
  ct_feedback_tracker_resync(&tracker, 18);
  uint64_t debt_after_resync = tracker.telemetry_debt_bytes;
  ct_feedback_destroy(small);

  /* Opaque tokens are attributed by the host, never by the controller. */
  uint64_t tenant_bytes[4] = {0, 0, 0, 0};
  ct_feedback_queue_t *attrib = ct_feedback_create(64);
  for (uint64_t token = 0; token < 64; token++)
    (void)ct_feedback_emit(attrib, token, 1, (uint8_t)(token & 31), 0);
  while (ct_feedback_poll(attrib, &rec) == 1)
    tenant_bytes[rec.token % 4] += 128ull * (rec.size_class + 1);
  ct_feedback_destroy(attrib);

  /* 53% aggregate bytes free, but all in 128-byte slots and no unassigned
   * extent: a raw-page reservation must be rejected by the class gate. */
  uint64_t slots[32] = {0};
  const uint64_t extent = 4ull << 20;
  const uint64_t pool = 16ull * extent;
  slots[0] = (pool * 53 / 100) / 128;
  ct_place_gate_t *gate = ct_place_gate_create(extent, 0, slots);
  uint64_t agg_free = ct_place_gate_aggregate_free_bytes(gate);
  int raw_reject = ct_place_gate_reserve(gate, 31, 1);
  int small_accept = ct_place_gate_reserve(gate, 0, 1);
  ct_place_gate_destroy(gate);

  /* Concurrent producer/consumer throughput. Capacity is deliberately n so
   * this cell measures transport rather than overflow handling. */
  ct_feedback_queue_t *q = ct_feedback_create((uint32_t)n);
  if (!q) return 2;
  run_t run = {.q = q, .n = n};
  pthread_t pt, ct;
  uint64_t t0 = ctier_now_ns();
  pthread_create(&ct, NULL, consumer, &run);
  pthread_create(&pt, NULL, producer, &run);
  void *prc = NULL;
  pthread_join(pt, &prc);
  pthread_join(ct, NULL);
  uint64_t t1 = ctier_now_ns();
  uint64_t throughput_dropped = ct_feedback_dropped(q);
  ct_feedback_destroy(q);
  if (prc || run.consumed != n || throughput_dropped != 0) return 1;
  double seconds = (double)(t1 - t0) / 1e9;
  double mrps = (double)n / seconds / 1e6;
  double mibps = (double)n * sizeof(ct_feedback_record_t) / seconds / (1024.0 * 1024.0);

  FILE *f = fopen(out, "w");
  if (!f) return 2;
  fprintf(f, "{\n");
  fprintf(f, "  \"meta\": {\"experiment\": \"E12_feedback_placeability\", \"record_bytes\": %zu},\n",
          sizeof(ct_feedback_record_t));
  fprintf(f, "  \"loss\": {\"dropped\": %llu, \"gap_apply_code\": %d, \"gap_records\": %llu, \"debt_before_resync\": %llu, \"debt_after_resync\": %llu},\n",
          (unsigned long long)dropped, gap_rc,
          (unsigned long long)gap_records,
          (unsigned long long)debt_before_resync,
          (unsigned long long)debt_after_resync);
  fprintf(f, "  \"attribution\": {\"tenant_bytes\": [%llu, %llu, %llu, %llu]},\n",
          (unsigned long long)tenant_bytes[0], (unsigned long long)tenant_bytes[1],
          (unsigned long long)tenant_bytes[2], (unsigned long long)tenant_bytes[3]);
  fprintf(f, "  \"placeability\": {\"pool_bytes\": %llu, \"aggregate_free_bytes\": %llu, \"aggregate_free_fraction\": %.6f, \"raw_page_reservation_code\": %d, \"matching_class_reservation_code\": %d},\n",
          (unsigned long long)pool, (unsigned long long)agg_free,
          (double)agg_free / (double)pool, raw_reject, small_accept);
  fprintf(f, "  \"throughput\": {\"records\": %llu, \"seconds\": %.9f, \"million_records_per_s\": %.3f, \"mib_per_s\": %.3f, \"dropped\": %llu, \"token_xor\": %llu}\n",
          (unsigned long long)n, seconds, mrps, mibps,
          (unsigned long long)throughput_dropped,
          (unsigned long long)run.token_xor);
  fprintf(f, "}\n");
  fclose(f);

  int ok = sizeof(ct_feedback_record_t) == 16 && dropped == 8 &&
           gap_records == 8 && debt_before_resync == 8 * 4096 &&
           debt_after_resync == 0 && raw_reject == -1 && small_accept == 0;
  fprintf(stderr, "E12: record=16B loss/debt=%s placeability=%s throughput=%.3f Mrps\n",
          (dropped == 8 && gap_records == 8) ? "PASS" : "FAIL",
          (raw_reject == -1 && small_accept == 0) ? "PASS" : "FAIL", mrps);
  return ok ? 0 : 1;
}
