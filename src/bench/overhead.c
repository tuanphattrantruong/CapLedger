/* overhead.c — E4 overhead/scalability binary. Emits one JSON object to
 * stdout. Sub-benchmarks:
 *   (a) admission: pure contract_admit(GUAR, n=16) call latency on a warmed
 *       analytic contract (fresh contracts every ADMIT_SEGMENT calls, outside
 *       the timed region, so reservations never saturate the gate);
 *   (b) writepath: ctier_write ns/op for TEXT and RANDOM, hist-on vs
 *       CTIER_F_NOHIST, fresh tier per cell (content pregenerated, so the
 *       timed loop is tier work only);
 *   (c) compaction: fill the pool with mixed content, free 75% (lpn%4 != 0),
 *       time one ctier_compact;
 *   (d) spill: spill 10000 TEXT pages then promote them back, MB/s + pages/s;
 *   (e) scaling: T threads x ops 70% read / 30% write over disjoint
 *       pre-populated lpn ranges on one shared tier, aggregate Mops/s.
 * All timings via ctier_now_ns (mach_absolute_time). Deterministic seeds.
 * CLI: --pool BYTES --threads-list 1,2,4,8,... --ops N --spill-path PATH
 *      (thread counts must be in 1..MAX_THREADS)
 */
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ctier.h"
#include "contract.h"

#define CORPUS_BYTES (16u << 20)
#define ADMIT_ITERS 100000
#define ADMIT_SEGMENT 3000
#define ADMIT_GROUP_N 16
#define WARM_PAGES 8192
#define PREGEN 1024
#define SPILL_PAGES 10000
#define SEED 0xC0FFEEull

static const char *TEXT_SRC = "/usr/share/dict/words";

/* ---------- PRNG (xoshiro256**, explicit state) ---------- */
typedef struct { uint64_t s[4]; } xsr_t;
static uint64_t splitmix64(uint64_t *s) {
  uint64_t z = (*s += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}
static void xsr_seed(xsr_t *x, uint64_t seed) {
  for (int i = 0; i < 4; i++) x->s[i] = splitmix64(&seed);
}
static inline uint64_t rotl(uint64_t v, int k) { return (v << k) | (v >> (64 - k)); }
static uint64_t xsr_next(xsr_t *x) {
  uint64_t r = rotl(x->s[1] * 5, 7) * 9;
  uint64_t t = x->s[1] << 17;
  x->s[2] ^= x->s[0]; x->s[3] ^= x->s[1]; x->s[1] ^= x->s[2]; x->s[0] ^= x->s[3];
  x->s[2] ^= t; x->s[3] = rotl(x->s[3], 45);
  return r;
}

/* ---------- content ---------- */
static uint8_t *corpus_text;

static void load_corpus(void) {
  corpus_text = malloc(CORPUS_BYTES);
  if (!corpus_text) { fprintf(stderr, "oom\n"); exit(1); }
  FILE *f = fopen(TEXT_SRC, "rb");
  if (!f) { fprintf(stderr, "cannot open %s\n", TEXT_SRC); exit(1); }
  size_t n = fread(corpus_text, 1, CORPUS_BYTES, f);
  fclose(f);
  if (n == 0) { fprintf(stderr, "empty corpus\n"); exit(1); }
  size_t fill = n;
  while (fill < CORPUS_BYTES) {
    size_t chunk = n < CORPUS_BYTES - fill ? n : CORPUS_BYTES - fill;
    memcpy(corpus_text + fill, corpus_text, chunk);
    fill += chunk;
  }
}

static void gen_text(xsr_t *x, uint8_t *b) {
  size_t off = (size_t)(xsr_next(x) % (CORPUS_BYTES - PAGE_SIZE));
  memcpy(b, corpus_text + off, PAGE_SIZE);
}
static void gen_random(xsr_t *x, uint8_t *b) {
  for (int i = 0; i < PAGE_SIZE; i += 8) {
    uint64_t v = xsr_next(x);
    memcpy(b + i, &v, 8);
  }
}
static void gen_floatq(xsr_t *x, uint8_t *b) {
  for (int i = 0; i < PAGE_SIZE; i++)
    b[i] = (uint8_t)(int8_t)((int64_t)(xsr_next(x) % 65) - 32); /* narrow ints */
}
static void gen_json(xsr_t *x, uint8_t *b) {
  size_t off = 0;
  while (off < PAGE_SIZE) {
    char rec[160];
    int n = snprintf(rec, sizeof(rec),
        "{\"ts\":%llu,\"lvl\":%d,\"id\":\"%016llx\",\"msg\":\"request %llu ok\"}\n",
        (unsigned long long)(1700000000ull + (xsr_next(x) % 604800)),
        (int)(xsr_next(x) % 6), (unsigned long long)xsr_next(x),
        (unsigned long long)(xsr_next(x) % 1000000));
    size_t l = (size_t)n;
    if (off + l > PAGE_SIZE) l = PAGE_SIZE - off;
    memcpy(b + off, rec, l);
    off += l;
  }
}

static int cmp_dbl(const void *a, const void *b) {
  double u = *(const double *)a, v = *(const double *)b;
  return u < v ? -1 : u > v ? 1 : 0;
}
static double pctile(double *sorted, int n, double p) {
  int idx = (int)ceil(p * n) - 1;
  if (idx < 0) idx = 0;
  if (idx >= n) idx = n - 1;
  return sorted[idx];
}

/* ---------- simple barrier (Darwin has no pthread_barrier_t) ---------- */
typedef struct {
  pthread_mutex_t mx;
  pthread_cond_t cv;
  int count, waiting, gen;
} barrier_t;
static void barrier_init(barrier_t *b, int count) {
  pthread_mutex_init(&b->mx, NULL);
  pthread_cond_init(&b->cv, NULL);
  b->count = count; b->waiting = 0; b->gen = 0;
}
static void barrier_wait(barrier_t *b) {
  pthread_mutex_lock(&b->mx);
  int g = b->gen;
  if (++b->waiting == b->count) {
    b->waiting = 0; b->gen++;
    pthread_cond_broadcast(&b->cv);
  } else {
    while (g == b->gen) pthread_cond_wait(&b->cv, &b->mx);
  }
  pthread_mutex_unlock(&b->mx);
}

/* ---------- config ---------- */
static uint64_t g_pool = 256ull << 20;
static uint64_t g_ops = 50000;
/* Thread-count configurations to sweep, and the largest sweep this benchmark
 * will run. Both were sized for an 8-core laptop; on a 24-performance-core
 * part a cap of 16 silently truncates the sweep and hides where the device
 * stops scaling. */
#define MAX_THREADS 64
static int g_threads[16], g_nthreads_cfgs = 4;
static char g_spill[512] = "build/tmp/overhead_spill.dat";

static ctier_t *mk_tier_sz(const char *suffix, uint32_t flags, uint64_t pool) {
  char path[600];
  snprintf(path, sizeof(path), "%s.%s", g_spill, suffix);
  ctier_t *t = ctier_create(pool, 1ull << 20, 1, 0, 0, flags, path);
  if (!t) { fprintf(stderr, "ctier_create failed (%s)\n", suffix); exit(1); }
  return t;
}

static ctier_t *mk_tier(const char *suffix, uint32_t flags) {
  return mk_tier_sz(suffix, flags, g_pool);
}

/* ---------- (a) admission ---------- */
static void bench_admission(void) {
  ctier_t *t = mk_tier("adm", 0);
  xsr_t x; xsr_seed(&x, SEED + 1);
  uint8_t page[PAGE_SIZE];
  contract_t *c = contract_create(t, CONTRACT_EST_ANALYTIC, 0.99, 0.05, 2.0,
                                  1024, 1, CONTRACT_VICTIM_DENSITY_AGE);
  for (int i = 0; i < WARM_PAGES; i++) {   /* warm: mixed TEXT/RANDOM */
    if (i % 2 == 0) gen_text(&x, page); else gen_random(&x, page);
    if (contract_write(c, 0, CONTRACT_OPP, (uint64_t)i, page) != 0) {
      fprintf(stderr, "admission warm write failed at %d\n", i);
      exit(1);
    }
  }
  static double ns[ADMIT_ITERS];
  uint64_t accepts = 0, rejects = 0;
  for (int i = 0; i < ADMIT_ITERS; i++) {
    if (i > 0 && i % ADMIT_SEGMENT == 0) {  /* fresh groups, untimed */
      contract_destroy(c);
      c = contract_create(t, CONTRACT_EST_ANALYTIC, 0.99, 0.05, 2.0, 1024, 1,
                          CONTRACT_VICTIM_DENSITY_AGE);
    }
    uint64_t t0 = ctier_now_ns();
    int rc = contract_admit(c, 0, CONTRACT_GUAR, ADMIT_GROUP_N);
    uint64_t t1 = ctier_now_ns();
    ns[i] = (double)(t1 - t0);
    if (rc == 0) accepts++; else rejects++;
  }
  contract_destroy(c);
  ctier_destroy(t);
  qsort(ns, ADMIT_ITERS, sizeof(double), cmp_dbl);
  double sum = 0;
  for (int i = 0; i < ADMIT_ITERS; i++) sum += ns[i];
  printf("  \"admission\": {\"iters\": %d, \"group_npages\": %d, "
         "\"warm_pages\": %d, \"segment\": %d, \"accepts\": %llu, "
         "\"rejects\": %llu, \"mean_ns\": %.1f, \"p50_ns\": %.0f, "
         "\"p90_ns\": %.0f, \"p99_ns\": %.0f, \"max_ns\": %.0f},\n",
         ADMIT_ITERS, ADMIT_GROUP_N, WARM_PAGES, ADMIT_SEGMENT,
         (unsigned long long)accepts, (unsigned long long)rejects,
         sum / ADMIT_ITERS, pctile(ns, ADMIT_ITERS, 0.50),
         pctile(ns, ADMIT_ITERS, 0.90), pctile(ns, ADMIT_ITERS, 0.99),
         ns[ADMIT_ITERS - 1]);
}

/* ---------- (b) writepath ---------- */
static double writepath_cell(int is_random, uint32_t flags, const char *tag) {
  ctier_t *t = mk_tier(tag, flags);
  xsr_t x; xsr_seed(&x, SEED + 2);
  static uint8_t pregen[PREGEN][PAGE_SIZE];
  for (int i = 0; i < PREGEN; i++) {
    if (is_random) gen_random(&x, pregen[i]); else gen_text(&x, pregen[i]);
  }
  uint64_t t0 = ctier_now_ns();
  for (uint64_t i = 0; i < g_ops; i++) {
    int64_t r = ctier_write(t, i, pregen[i % PREGEN]);
    if (r < 0) { fprintf(stderr, "writepath ENOSPACE/err %lld at %llu\n",
                         (long long)r, (unsigned long long)i); exit(1); }
  }
  uint64_t dt = ctier_now_ns() - t0;
  ctier_destroy(t);
  return (double)dt / (double)g_ops;
}

static void bench_writepath(void) {
  double th = writepath_cell(0, 0, "wp_th");
  double tn = writepath_cell(0, CTIER_F_NOHIST, "wp_tn");
  double rh = writepath_cell(1, 0, "wp_rh");
  double rn = writepath_cell(1, CTIER_F_NOHIST, "wp_rn");
  printf("  \"writepath\": {\"ops_per_cell\": %llu, \"pregen_pages\": %d, "
         "\"text_hist_ns_op\": %.1f, \"text_nohist_ns_op\": %.1f, "
         "\"random_hist_ns_op\": %.1f, \"random_nohist_ns_op\": %.1f, "
         "\"hist_overhead_text_ns\": %.1f, \"hist_overhead_random_ns\": %.1f},\n",
         (unsigned long long)g_ops, PREGEN, th, tn, rh, rn, th - tn, rh - rn);
}

/* ---------- (c) compaction ---------- */
static void bench_compaction(void) {
  ctier_t *t = mk_tier("cmp", 0);
  xsr_t x; xsr_seed(&x, SEED + 3);
  uint8_t page[PAGE_SIZE];
  uint64_t n = 0;
  for (;;) {
    switch (n % 5) {                       /* mixed fill */
    case 0: case 1: gen_text(&x, page); break;
    case 2: gen_floatq(&x, page); break;
    case 3: gen_json(&x, page); break;
    default: gen_random(&x, page); break;
    }
    int64_t r = ctier_write(t, n, page);
    if (r == CTIER_ENOSPACE) break;
    if (r < 0) { fprintf(stderr, "compaction fill err\n"); exit(1); }
    n++;
    if (n >= (1ull << 20)) break;
  }
  for (uint64_t i = 0; i < n; i++)
    if (i % 4 != 0) (void)ctier_free_page(t, i);
  uint64_t resv0 = ctier_stat_u64(t, CTIER_STAT_PHYS_RESERVED);
  uint64_t ext0 = ctier_stat_u64(t, CTIER_STAT_EXTENTS_ACTIVE);
  uint64_t t0 = ctier_now_ns();
  int64_t rel = ctier_compact(t);
  uint64_t dt = ctier_now_ns() - t0;
  uint64_t resv1 = ctier_stat_u64(t, CTIER_STAT_PHYS_RESERVED);
  uint64_t ext1 = ctier_stat_u64(t, CTIER_STAT_EXTENTS_ACTIVE);
  ctier_destroy(t);
  printf("  \"compaction\": {\"pages_filled\": %llu, \"freed_frac\": 0.75, "
         "\"bytes_released\": %lld, \"mb_released\": %.2f, "
         "\"compact_ms\": %.3f, \"mbps\": %.2f, "
         "\"extents_before\": %llu, \"extents_after\": %llu, "
         "\"phys_reserved_before\": %llu, \"phys_reserved_after\": %llu},\n",
         (unsigned long long)n, (long long)rel, (double)rel / 1e6,
         (double)dt / 1e6,
         dt ? (double)rel / 1e6 / ((double)dt / 1e9) : 0.0,
         (unsigned long long)ext0, (unsigned long long)ext1,
         (unsigned long long)resv0, (unsigned long long)resv1);
}

/* ---------- (d) spill / promote ---------- */
static void bench_spill(void) {
  ctier_t *t = mk_tier("spl", 0);
  xsr_t x; xsr_seed(&x, SEED + 4);
  uint8_t page[PAGE_SIZE];
  for (int i = 0; i < SPILL_PAGES; i++) {
    gen_text(&x, page);
    if (ctier_write(t, (uint64_t)i, page) < 0) { fprintf(stderr, "spill fill\n"); exit(1); }
  }
  uint64_t w0 = ctier_stat_u64(t, CTIER_STAT_SPILL_BYTES_W);
  uint64_t t0 = ctier_now_ns();
  for (int i = 0; i < SPILL_PAGES; i++)
    if (ctier_spill(t, (uint64_t)i) < 0) { fprintf(stderr, "spill err\n"); exit(1); }
  uint64_t dt_s = ctier_now_ns() - t0;
  uint64_t wbytes = ctier_stat_u64(t, CTIER_STAT_SPILL_BYTES_W) - w0;
  uint64_t r0 = ctier_stat_u64(t, CTIER_STAT_SPILL_BYTES_R);
  uint64_t t1 = ctier_now_ns();
  for (int i = 0; i < SPILL_PAGES; i++)
    if (ctier_read(t, (uint64_t)i, page) != 0) { fprintf(stderr, "promote err\n"); exit(1); }
  uint64_t dt_p = ctier_now_ns() - t1;
  uint64_t rbytes = ctier_stat_u64(t, CTIER_STAT_SPILL_BYTES_R) - r0;
  uint64_t promotes = ctier_stat_u64(t, CTIER_STAT_PROMOTES);
  ctier_destroy(t);
  printf("  \"spill\": {\"pages\": %d, \"content\": \"TEXT\", "
         "\"spill_ms\": %.3f, \"spill_file_bytes\": %llu, "
         "\"spill_mbps_logical\": %.2f, \"spill_pages_per_s\": %.0f, "
         "\"promote_ms\": %.3f, \"promote_file_bytes\": %llu, "
         "\"promote_mbps_logical\": %.2f, \"promote_pages_per_s\": %.0f, "
         "\"promotes_counted\": %llu},\n",
         SPILL_PAGES, (double)dt_s / 1e6, (unsigned long long)wbytes,
         (double)SPILL_PAGES * PAGE_SIZE / 1e6 / ((double)dt_s / 1e9),
         (double)SPILL_PAGES / ((double)dt_s / 1e9),
         (double)dt_p / 1e6, (unsigned long long)rbytes,
         (double)SPILL_PAGES * PAGE_SIZE / 1e6 / ((double)dt_p / 1e9),
         (double)SPILL_PAGES / ((double)dt_p / 1e9),
         (unsigned long long)promotes);
}

/* ---------- (e) scaling ---------- */
#define RANGE_PAGES 4096
#define THREAD_PREGEN 64

typedef struct {
  ctier_t *tier;
  barrier_t *bar;
  int tid;
  uint64_t ops;
  uint64_t t_start, t_end;
  uint64_t errs;
} scale_arg_t;

static void *scale_worker(void *vp) {
  scale_arg_t *a = vp;
  xsr_t x; xsr_seed(&x, SEED + 100 + (uint64_t)a->tid);
  static _Thread_local uint8_t buf[PAGE_SIZE];
  uint8_t (*pre)[PAGE_SIZE] = malloc(sizeof(uint8_t[THREAD_PREGEN][PAGE_SIZE]));
  if (!pre) { a->errs = 1; return NULL; }
  for (int i = 0; i < THREAD_PREGEN; i++) gen_text(&x, pre[i]);
  uint64_t base = (uint64_t)a->tid * RANGE_PAGES;
  barrier_wait(a->bar);
  a->t_start = ctier_now_ns();
  for (uint64_t i = 0; i < a->ops; i++) {
    uint64_t r = xsr_next(&x);
    uint64_t lpn = base + (r % RANGE_PAGES);
    if ((r >> 32) % 10 < 7) {
      if (ctier_read(a->tier, lpn, buf) != 0) a->errs++;
    } else {
      if (ctier_write(a->tier, lpn, pre[i % THREAD_PREGEN]) < 0) a->errs++;
    }
  }
  a->t_end = ctier_now_ns();
  free(pre);
  return NULL;
}

static void bench_scaling(void) {
  printf("  \"scaling\": {\"ops_per_thread\": %llu, \"range_pages\": %d, "
         "\"read_frac\": 0.7, \"content\": \"TEXT\", \"results\": [",
         (unsigned long long)g_ops, RANGE_PAGES);
  for (int ci = 0; ci < g_nthreads_cfgs; ci++) {
    int T = g_threads[ci];
    char tag[32];
    snprintf(tag, sizeof(tag), "scl%d", T);
    /* The scaling cell prepopulates T * RANGE_PAGES pages. With a fixed pool
     * that overflows past ~8 threads, and a nearly-full pool makes chunk
     * allocation crawl -- which would be measured as "the device stops
     * scaling" when it is really "the benchmark ran out of room". Size the
     * pool to the thread count, with headroom. */
    uint64_t pool = (uint64_t)T * RANGE_PAGES * PAGE_SIZE * 2;
    if (pool < g_pool) pool = g_pool;
    ctier_t *t = mk_tier_sz(tag, 0, pool);
    xsr_t x; xsr_seed(&x, SEED + 5);
    uint8_t page[PAGE_SIZE];
    for (uint64_t i = 0; i < (uint64_t)T * RANGE_PAGES; i++) {
      gen_text(&x, page);
      if (ctier_write(t, i, page) < 0) { fprintf(stderr, "scale prepop\n"); exit(1); }
    }
    barrier_t bar;
    barrier_init(&bar, T);
    pthread_t th[MAX_THREADS];
    scale_arg_t args[16];
    for (int i = 0; i < T; i++) {
      args[i] = (scale_arg_t){.tier = t, .bar = &bar, .tid = i,
                              .ops = g_ops, .errs = 0};
      pthread_create(&th[i], NULL, scale_worker, &args[i]);
    }
    uint64_t min_start = UINT64_MAX, max_end = 0, errs = 0;
    for (int i = 0; i < T; i++) {
      pthread_join(th[i], NULL);
      if (args[i].t_start < min_start) min_start = args[i].t_start;
      if (args[i].t_end > max_end) max_end = args[i].t_end;
      errs += args[i].errs;
    }
    ctier_destroy(t);
    double wall_s = (double)(max_end - min_start) / 1e9;
    printf("{\"threads\": %d, \"wall_s\": %.4f, \"mops_per_s\": %.4f, "
           "\"errors\": %llu}%s",
           T, wall_s, (double)T * (double)g_ops / wall_s / 1e6,
           (unsigned long long)errs, ci < g_nthreads_cfgs - 1 ? ", " : "");
  }
  printf("]}\n");
}

int main(int argc, char **argv) {
  g_threads[0] = 1; g_threads[1] = 2; g_threads[2] = 4; g_threads[3] = 8;
  for (int i = 1; i < argc - 1; i++) {
    if (strcmp(argv[i], "--pool") == 0) g_pool = strtoull(argv[++i], NULL, 10);
    else if (strcmp(argv[i], "--ops") == 0) g_ops = strtoull(argv[++i], NULL, 10);
    else if (strcmp(argv[i], "--spill-path") == 0)
      snprintf(g_spill, sizeof(g_spill), "%s", argv[++i]);
    else if (strcmp(argv[i], "--threads-list") == 0) {
      g_nthreads_cfgs = 0;
      char *tok = strtok(argv[++i], ",");
      while (tok && g_nthreads_cfgs < 16) {
        int v = atoi(tok);
        if (v >= 1 && v <= MAX_THREADS) g_threads[g_nthreads_cfgs++] = v;
        else fprintf(stderr, "overhead: ignoring thread count %d "
                             "(outside 1..%d)\n", v, MAX_THREADS);
        tok = strtok(NULL, ",");
      }
      if (g_nthreads_cfgs == 0) { fprintf(stderr, "bad threads-list\n"); return 1; }
    }
  }
  load_corpus();
  printf("{\n");
  printf("  \"bench\": \"e4-overheads\",\n");
  printf("  \"seed\": %llu,\n", (unsigned long long)SEED);
  printf("  \"pool_bytes\": %llu,\n", (unsigned long long)g_pool);
  printf("  \"ops\": %llu,\n", (unsigned long long)g_ops);
  printf("  \"zlib_level\": 1,\n");
  printf("  \"timer\": \"mach_absolute_time\",\n");
  printf("  \"threads_list\": [");
  for (int i = 0; i < g_nthreads_cfgs; i++)
    printf("%d%s", g_threads[i], i < g_nthreads_cfgs - 1 ? ", " : "");
  printf("],\n");
  bench_admission();
  bench_writepath();
  bench_compaction();
  bench_spill();
  bench_scaling();
  printf("}\n");
  free(corpus_text);
  return 0;
}
