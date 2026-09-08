/* E25 host-tracker stress: many tenants and concurrent completion queues.
 *
 * This is a sharded data-plane prototype, not the contract.c policy mutex.
 * Each queue has independent sequence/loss state; an array token table maps
 * opaque tokens to tenants; only updates to the same tenant serialize while a
 * 4096-entry window, sums, and debt are maintained. */
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "feedback.h"

#define MAX_QUEUES 16
#define WINDOW 4096

typedef struct {
  pthread_mutex_t lock;
  uint64_t sum, sumsq, count, debt;
  uint32_t pos;
} tenant_t;

typedef struct {
  ct_feedback_queue_t *queue;
  ct_feedback_tracker_t tracker;
  tenant_t *tenants;
  uint16_t *windows;
  uint32_t *owner;
  uint64_t base, n;
  uint32_t ntenants;
  _Atomic int *start;
  _Atomic int failed;
  uint64_t consumed;
} shard_t;

static uint64_t now_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void *producer(void *arg) {
  shard_t *s = arg;
  while (!atomic_load_explicit(s->start, memory_order_acquire)) { }
  for (uint64_t i = 0; i < s->n; i++) {
    uint64_t token = s->base + i;
    uint8_t k = (uint8_t)((token * 17u) & 31u);
    if (ct_feedback_emit(s->queue, token, 1, k, CT_FEEDBACK_OK) != 0) {
      atomic_store(&s->failed, 1);
      break;
    }
  }
  return NULL;
}

static void update_tenant(shard_t *s, const ct_feedback_record_t *r) {
  uint32_t id = s->owner[r->token];
  tenant_t *t = &s->tenants[id];
  uint16_t *ring = &s->windows[(uint64_t)id * WINDOW];
  uint16_t bytes = (uint16_t)(128u * ((uint16_t)r->size_class + 1u));
  pthread_mutex_lock(&t->lock);
  if (t->count >= WINDOW) {
    uint16_t old = ring[t->pos];
    t->sum -= old;
    t->sumsq -= (uint64_t)old * old;
  }
  ring[t->pos] = bytes;
  t->pos = (t->pos + 1u) % WINDOW;
  t->sum += bytes;
  t->sumsq += (uint64_t)bytes * bytes;
  t->count++;
  if (bytes > 2048) t->debt += (uint64_t)bytes - 2048;
  pthread_mutex_unlock(&t->lock);
}

static void *consumer(void *arg) {
  shard_t *s = arg;
  while (!atomic_load_explicit(s->start, memory_order_acquire)) { }
  while (s->consumed < s->n && !atomic_load(&s->failed)) {
    ct_feedback_record_t r;
    int got = ct_feedback_poll(s->queue, &r);
    if (got == 1) {
      if (ct_feedback_tracker_apply(&s->tracker, &r, 4096) != 0)
        atomic_store(&s->failed, 1);
      update_tenant(s, &r);
      s->consumed++;
    }
  }
  return NULL;
}

int main(int argc, char **argv) {
  uint32_t ntenants = 4096, nqueues = 8;
  uint64_t records = 1000000;
  for (int i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--tenants") && i + 1 < argc)
      ntenants = (uint32_t)strtoul(argv[++i], NULL, 0);
    else if (!strcmp(argv[i], "--queues") && i + 1 < argc)
      nqueues = (uint32_t)strtoul(argv[++i], NULL, 0);
    else if (!strcmp(argv[i], "--records") && i + 1 < argc)
      records = strtoull(argv[++i], NULL, 0);
    else {
      fprintf(stderr, "usage: tracker_scale --tenants N --queues N --records N\n");
      return 2;
    }
  }
  if (!ntenants || ntenants > 65536 || !nqueues || nqueues > MAX_QUEUES ||
      records < nqueues) return 2;

  tenant_t *tenants = calloc(ntenants, sizeof(*tenants));
  uint16_t *windows = calloc((uint64_t)ntenants * WINDOW, sizeof(*windows));
  uint32_t *owner = malloc(records * sizeof(*owner));
  shard_t *shards = calloc(nqueues, sizeof(*shards));
  pthread_t *prod = calloc(nqueues, sizeof(*prod));
  pthread_t *cons = calloc(nqueues, sizeof(*cons));
  if (!tenants || !windows || !owner || !shards || !prod || !cons) return 2;
  for (uint32_t t = 0; t < ntenants; t++) pthread_mutex_init(&tenants[t].lock, NULL);
  for (uint64_t i = 0; i < records; i++) owner[i] = (uint32_t)(i % ntenants);

  _Atomic int start = 0;
  uint64_t base = 0;
  for (uint32_t q = 0; q < nqueues; q++) {
    uint64_t n = records / nqueues + (q < records % nqueues);
    shards[q] = (shard_t){.tenants=tenants, .windows=windows, .owner=owner,
      .base=base, .n=n, .ntenants=ntenants, .start=&start};
    shards[q].queue = ct_feedback_create((uint32_t)n);
    ct_feedback_tracker_init(&shards[q].tracker);
    if (!shards[q].queue) return 2;
    base += n;
    pthread_create(&cons[q], NULL, consumer, &shards[q]);
    pthread_create(&prod[q], NULL, producer, &shards[q]);
  }

  clock_t cpu0 = clock();
  uint64_t wall0 = now_ns();
  atomic_store_explicit(&start, 1, memory_order_release);
  for (uint32_t q = 0; q < nqueues; q++) pthread_join(prod[q], NULL);
  for (uint32_t q = 0; q < nqueues; q++) pthread_join(cons[q], NULL);
  uint64_t wall1 = now_ns();
  clock_t cpu1 = clock();

  uint64_t consumed = 0, gaps = 0, dropped = 0, debt = 0;
  int failed = 0;
  for (uint32_t q = 0; q < nqueues; q++) {
    consumed += shards[q].consumed;
    gaps += shards[q].tracker.gap_records;
    dropped += ct_feedback_dropped(shards[q].queue);
    failed |= atomic_load(&shards[q].failed);
    ct_feedback_destroy(shards[q].queue);
  }
  for (uint32_t t = 0; t < ntenants; t++) debt += tenants[t].debt;
  double wall_s = (double)(wall1 - wall0) / 1e9;
  double cpu_s = (double)(cpu1 - cpu0) / CLOCKS_PER_SEC;
  printf("{\"tenants\":%u,\"queues\":%u,\"window\":%u,"
         "\"records\":%llu,\"consumed\":%llu,\"dropped\":%llu,"
         "\"gaps\":%llu,\"wall_seconds\":%.9f,\"cpu_seconds\":%.9f,"
         "\"million_records_per_s\":%.6f,\"cpu_ns_per_record\":%.3f,"
         "\"tenant_debt_checksum\":%llu}\n",
         ntenants, nqueues, WINDOW, (unsigned long long)records,
         (unsigned long long)consumed, (unsigned long long)dropped,
         (unsigned long long)gaps, wall_s, cpu_s, records / wall_s / 1e6,
         cpu_s * 1e9 / records, (unsigned long long)debt);

  for (uint32_t t = 0; t < ntenants; t++) pthread_mutex_destroy(&tenants[t].lock);
  free(cons); free(prod); free(shards); free(owner); free(windows); free(tenants);
  return failed || consumed != records || dropped || gaps;
}
