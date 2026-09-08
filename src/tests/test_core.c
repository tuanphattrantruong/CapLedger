/* test_core.c — assert-based unit tests for the ctier device and the capacity
 * contract. Exit 0 on pass; one line per test. Run from the repo root
 * (spill files go under build/tmp/). */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ctier.h"
#include "contract.h"

/* ---------- deterministic content generators ---------- */

static uint64_t splitmix64(uint64_t *s) {
  uint64_t z = (*s += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}
static inline uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
static void gen_text(uint8_t *b, uint64_t key) {
  static const char *words[] = {"capacity", "contract", "memory", "tier",
      "compressed", "kernel", "page", "probabilistic", "quantile", "advertise",
      "reservation", "tenant", "overdraft", "violation", "recovery"};
  size_t off = 0;
  uint64_t s = key * 2654435761u + 1;
  while (off < PAGE_SIZE) {
    const char *w = words[splitmix64(&s) % 15];
    size_t l = strlen(w);
    if (off + l + 1 > PAGE_SIZE) { b[off++] = '\n'; continue; }
    memcpy(b + off, w, l);
    off += l;
    b[off++] = ' ';
  }
}
static void gen_binaryish(uint8_t *b, uint64_t key) {
  uint64_t s = key + 77;
  for (int i = 0; i < PAGE_SIZE; i += 16) {
    uint64_t v = splitmix64(&s);
    memcpy(b + i, &v, 8);
    memset(b + i + 8, (int)(v & 0x3F), 8);   /* half structured, half runs */
  }
}
static void gen_float(uint8_t *b, uint64_t key) {
  uint64_t s = key ^ 0xF10A7ull;
  double *d = (double *)(void *)b;
  for (int i = 0; i < 512; i += 2) {
    double u1 = ((double)(splitmix64(&s) >> 11) + 1.0) / 9007199254740993.0;
    double u2 = (double)(splitmix64(&s) >> 11) / 9007199254740992.0;
    double r = sqrt(-2.0 * log(u1));
    d[i] = r * cos(2.0 * M_PI * u2);
    d[i + 1] = r * sin(2.0 * M_PI * u2);
  }
}
static void gen_floatq(uint8_t *b, uint64_t key) {
  uint64_t s = key ^ 0x0F10ull;
  for (int i = 0; i < PAGE_SIZE; i += 2) {
    double u1 = ((double)(splitmix64(&s) >> 11) + 1.0) / 9007199254740993.0;
    double u2 = (double)(splitmix64(&s) >> 11) / 9007199254740992.0;
    double r = sqrt(-2.0 * log(u1));
    double g1 = r * cos(2.0 * M_PI * u2) * 32.0;
    double g2 = r * sin(2.0 * M_PI * u2) * 32.0;
    if (g1 > 127) g1 = 127; if (g1 < -127) g1 = -127;
    if (g2 > 127) g2 = 127; if (g2 < -127) g2 = -127;
    b[i] = (uint8_t)(int8_t)g1;
    b[i + 1] = (uint8_t)(int8_t)g2;
  }
}
static void gen_json(uint8_t *b, uint64_t key) {
  uint64_t s = key + 3;
  size_t off = 0;
  while (off < PAGE_SIZE) {
    char rec[128];
    int n = snprintf(rec, sizeof(rec),
                     "{\"ts\":%llu,\"lvl\":%d,\"id\":\"%08llx\",\"msg\":\"op %llu ok\"}\n",
                     (unsigned long long)(1700000000 + (splitmix64(&s) % 100000)),
                     (int)(splitmix64(&s) % 5),
                     (unsigned long long)(splitmix64(&s) & 0xFFFFFFFFull),
                     (unsigned long long)(splitmix64(&s) % 4096));
    size_t l = (size_t)n;
    if (off + l > PAGE_SIZE) l = PAGE_SIZE - off;
    memcpy(b + off, rec, l);
    off += l;
  }
}
static void gen_random(uint8_t *b, uint64_t key) {
  uint64_t s = key ^ 0xDEADBEEFull;
  uint64_t st[4];
  for (int i = 0; i < 4; i++) st[i] = splitmix64(&s);
  for (int i = 0; i < PAGE_SIZE; i += 8) {
    uint64_t r = rotl(st[1] * 5, 7) * 9;
    uint64_t t = st[1] << 17;
    st[2] ^= st[0]; st[3] ^= st[1]; st[1] ^= st[2]; st[0] ^= st[3];
    st[2] ^= t; st[3] = rotl(st[3], 45);
    memcpy(b + i, &r, 8);
  }
}

typedef void (*genfn)(uint8_t *, uint64_t);

static int g_test_no = 0;
#define OK(name) printf("ok %2d - %s\n", ++g_test_no, name)

static ctier_t *mk_tier_ext(uint64_t pool, uint64_t max_pages, uint64_t extent,
                            const char *tag) {
  char path[256];
  snprintf(path, sizeof(path), "build/tmp/test_spill_%s.dat", tag);
  ctier_t *t = ctier_create(pool, max_pages, 1, extent, 0, 0, path);
  assert(t && "ctier_create failed");
  return t;
}
static ctier_t *mk_tier(uint64_t pool, uint64_t max_pages, const char *tag) {
  return mk_tier_ext(pool, max_pages, 0, tag);
}
/* small pools get 128 KiB extents so a handful of size classes does not
 * strand the whole pool */
#define SMALL_EXTENT (128ull << 10)

/* ---------- ctier tests ---------- */

static void test_roundtrip_all_styles(void) {
  ctier_t *t = mk_tier(16ull << 20, 4096, "rt");
  genfn fns[7] = {gen_text, gen_binaryish, gen_float, gen_floatq, gen_json,
                  NULL /*zero*/, gen_random};
  uint8_t src[PAGE_SIZE], dst[PAGE_SIZE];
  for (int i = 0; i < 7; i++) {
    if (fns[i]) fns[i](src, (uint64_t)i * 101 + 7);
    else memset(src, 0, PAGE_SIZE);
    int64_t r = ctier_write(t, (uint64_t)i, src);
    assert(r >= 0 && r <= PAGE_SIZE);
    memset(dst, 0xAA, PAGE_SIZE);
    assert(ctier_read(t, (uint64_t)i, dst) == 0);
    assert(memcmp(src, dst, PAGE_SIZE) == 0);
  }
  assert(ctier_stat_u64(t, CTIER_STAT_PAGES_PRESENT) == 7);
  ctier_destroy(t);
  OK("write/read roundtrip, all 7 content styles");
}

static void test_zero_and_raw(void) {
  ctier_t *t = mk_tier(8ull << 20, 4096, "zr");
  uint8_t src[PAGE_SIZE], dst[PAGE_SIZE];
  memset(src, 0, PAGE_SIZE);
  assert(ctier_write(t, 1, src) == 0);
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == 0);
  assert(ctier_stat_u64(t, CTIER_STAT_PAGES_ZERO) == 1);
  gen_random(src, 42);
  int64_t r = ctier_write(t, 2, src);
  assert(r == PAGE_SIZE);
  assert(ctier_stat_u64(t, CTIER_STAT_PAGES_RAW) == 1);
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == PAGE_SIZE);
  assert(ctier_read(t, 2, dst) == 0 && memcmp(src, dst, PAGE_SIZE) == 0);
  ctier_destroy(t);
  OK("zero page costs 0 phys; raw fallback for random");
}

static void test_overwrite_accounting(void) {
  ctier_t *t = mk_tier(8ull << 20, 4096, "ow");
  uint8_t src[PAGE_SIZE];
  uint64_t expect = 0;
  gen_random(src, 1);
  int64_t r = ctier_write(t, 7, src);
  assert(r == PAGE_SIZE);
  expect += (uint64_t)r;
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == expect);
  gen_text(src, 2);                       /* shrink */
  int64_t r2 = ctier_write(t, 7, src);
  assert(r2 > 0 && r2 < PAGE_SIZE);
  expect = expect - PAGE_SIZE + (uint64_t)r2;
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == expect);
  memset(src, 0, PAGE_SIZE);              /* shrink to zero */
  assert(ctier_write(t, 7, src) == 0);
  expect -= (uint64_t)r2;
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == expect);
  gen_random(src, 3);                     /* grow */
  assert(ctier_write(t, 7, src) == PAGE_SIZE);
  expect += PAGE_SIZE;
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == expect);
  assert(expect == PAGE_SIZE);
  ctier_destroy(t);
  OK("overwrite shrink/grow accounting exact");
}

static void test_free_returns_bytes(void) {
  ctier_t *t = mk_tier(8ull << 20, 4096, "fr");
  uint8_t src[PAGE_SIZE];
  gen_text(src, 5);
  int64_t r = ctier_write(t, 3, src);
  assert(r > 0);
  uint64_t before = ctier_stat_u64(t, CTIER_STAT_PHYS_USED);
  int f = ctier_free_page(t, 3);
  assert(f == (int)r);
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == before - (uint64_t)r);
  assert(ctier_free_page(t, 3) == CTIER_EFAULT);
  ctier_destroy(t);
  OK("free returns class-rounded bytes released");
}

static void test_spill_promote(void) {
  ctier_t *t = mk_tier(8ull << 20, 4096, "sp");
  uint8_t a[PAGE_SIZE], b[PAGE_SIZE], dst[PAGE_SIZE], z[PAGE_SIZE];
  gen_text(a, 11);
  gen_random(b, 12);
  memset(z, 0, PAGE_SIZE);
  int64_t ra = ctier_write(t, 10, a);
  int64_t rb = ctier_write(t, 11, b);
  assert(ctier_write(t, 12, z) == 0);
  uint64_t pu = ctier_stat_u64(t, CTIER_STAT_PHYS_USED);
  assert(ctier_spill(t, 10) == (int)ra);
  assert(ctier_spill(t, 11) == (int)rb);
  assert(ctier_spill(t, 12) == 0);        /* zero page: no-op success */
  assert(ctier_stat_u64(t, CTIER_STAT_PAGES_SPILLED) == 2);
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == pu - (uint64_t)ra - (uint64_t)rb);
  assert(ctier_read(t, 10, dst) == 0 && memcmp(a, dst, PAGE_SIZE) == 0);
  assert(ctier_read(t, 11, dst) == 0 && memcmp(b, dst, PAGE_SIZE) == 0);
  assert(ctier_read(t, 12, dst) == 0 && memcmp(z, dst, PAGE_SIZE) == 0);
  assert(ctier_stat_u64(t, CTIER_STAT_PROMOTES) == 2);
  assert(ctier_stat_u64(t, CTIER_STAT_PAGES_SPILLED) == 0);
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_USED) == pu);
  assert(ctier_stat_u64(t, CTIER_STAT_SPILL_BYTES_W) > 0);
  assert(ctier_stat_u64(t, CTIER_STAT_SPILL_BYTES_R) ==
         ctier_stat_u64(t, CTIER_STAT_SPILL_BYTES_W));
  ctier_destroy(t);
  OK("spill + promote roundtrip byte-exact");
}

static void test_compaction(void) {
  ctier_t *t = mk_tier(8ull << 20, 8192, "cp");
  uint8_t src[PAGE_SIZE], dst[PAGE_SIZE];
  int n = 0;
  for (; n < 3000; n++) {
    gen_text(src, (uint64_t)n);
    int64_t r = ctier_write(t, (uint64_t)n, src);
    if (r == CTIER_ENOSPACE) break;
    assert(r > 0);
  }
  assert(n > 500);
  for (int i = 0; i < n; i++)
    if (i % 4 != 0) assert(ctier_free_page(t, (uint64_t)i) > 0);
  uint64_t resv_before = ctier_stat_u64(t, CTIER_STAT_PHYS_RESERVED);
  int64_t rel = ctier_compact(t);
  assert(rel > 0);
  assert(ctier_stat_u64(t, CTIER_STAT_PHYS_RESERVED) < resv_before);
  for (int i = 0; i < n; i += 4) {
    gen_text(src, (uint64_t)i);
    assert(ctier_read(t, (uint64_t)i, dst) == 0);
    assert(memcmp(src, dst, PAGE_SIZE) == 0);
  }
  ctier_destroy(t);
  OK("compaction releases extents and preserves data");
}

static void test_enospace_old_intact(void) {
  ctier_t *t = mk_tier(2ull << 20, 4096, "en");
  uint8_t src[PAGE_SIZE], dst[PAGE_SIZE];
  int n = 0;
  for (; n < 4096; n++) {
    gen_random(src, (uint64_t)n);
    int64_t r = ctier_write(t, (uint64_t)n, src);
    if (r == CTIER_ENOSPACE) break;
    assert(r == PAGE_SIZE);
  }
  assert(n == 512);                       /* 2 extents x 256 raw chunks */
  gen_random(src, 999999);                /* rewrite lpn 0 must fail... */
  assert(ctier_write(t, 0, src) == CTIER_ENOSPACE);
  gen_random(src, 0);                     /* ...and leave the old data intact */
  assert(ctier_read(t, 0, dst) == 0);
  assert(memcmp(src, dst, PAGE_SIZE) == 0);
  ctier_destroy(t);
  OK("ENOSPACE on tiny pool; old page intact after failed rewrite");
}

static void test_telemetry(void) {
  ctier_t *t = mk_tier(16ull << 20, 4096, "tl");
  uint8_t src[PAGE_SIZE];
  uint16_t mn = 0xFFFF, mx = 0;
  int n = 200;
  for (int i = 0; i < n; i++) {
    if (i % 3 == 0) gen_text(src, (uint64_t)i);
    else if (i % 3 == 1) gen_floatq(src, (uint64_t)i);
    else gen_json(src, (uint64_t)i);
    int64_t r = ctier_write(t, (uint64_t)i, src);
    assert(r > 0);
    if ((uint16_t)r < mn) mn = (uint16_t)r;
    if ((uint16_t)r > mx) mx = (uint16_t)r;
  }
  uint16_t ring[4096];
  int cnt = ctier_ring_snapshot(t, ring, 4096);
  assert(cnt == n);
  for (int i = 0; i < cnt; i++)
    assert(ring[i] <= PAGE_SIZE && ring[i] % 128 == 0 && ring[i] >= mn && ring[i] <= mx);
  double sm = ctier_stat_f64(t, CTIER_STAT_EWMA_MEAN_SLOW);
  double fm = ctier_stat_f64(t, CTIER_STAT_EWMA_MEAN_FAST);
  double sv = ctier_stat_f64(t, CTIER_STAT_EWMA_VAR_SLOW);
  assert(sm >= mn && sm <= mx);
  assert(fm >= mn && fm <= mx);
  assert(sv >= 0);
  double hist[32];
  assert(ctier_hist_snapshot(t, hist) == 0);
  double sum = 0;
  for (int i = 0; i < 32; i++) { assert(hist[i] >= 0); sum += hist[i]; }
  assert(sum > 0);
  ctier_destroy(t);
  OK("ring/hist/EWMA sanity (mean within [min,max])");
}

/* ---------- contract tests ---------- */

static void test_norminv(void) {
  assert(fabs(norminv(0.5)) < 1e-9);
  assert(fabs(norminv(0.975) - 1.959963984540054) < 1.2e-9);
  assert(fabs(norminv(0.99) - 2.326347874040841) < 1.2e-9);
  assert(fabs(norminv(0.2) + norminv(0.8)) < 1.2e-9);
  OK("norminv matches reference values");
}

static void warm_opp(contract_t *c, ctier_t *t, int nwarm, uint64_t base_lpn) {
  (void)t;
  uint8_t src[PAGE_SIZE];
  for (int i = 0; i < nwarm; i++) {
    if (i % 2 == 0) gen_text(src, (uint64_t)i);
    else gen_random(src, (uint64_t)i);
    assert(contract_write(c, 0, CONTRACT_OPP, base_lpn + (uint64_t)i, src) == 0);
  }
}

static void test_admit_monotonic_q(void) {
  ctier_t *t = mk_tier(16ull << 20, 65536, "mq");
  uint8_t src[PAGE_SIZE];
  for (int i = 0; i < 128; i++) {         /* warm global slow EWMA */
    if (i % 2 == 0) gen_text(src, (uint64_t)i);
    else gen_floatq(src, (uint64_t)i);
    assert(ctier_write(t, (uint64_t)i, src) > 0);
  }
  contract_t *c50 = contract_create(t, CONTRACT_EST_ANALYTIC, 0.5, 0.05, 2.0, 1024, 1, 1);
  contract_t *c99 = contract_create(t, CONTRACT_EST_ANALYTIC, 0.99, 0.05, 2.0, 1024, 1, 1);
  assert(c50 && c99);
  int64_t cap50 = contract_capacity_pages(c50, 0, CONTRACT_GUAR);
  int64_t cap99 = contract_capacity_pages(c99, 0, CONTRACT_GUAR);
  assert(cap50 > 0 && cap99 > 0);
  assert(cap50 >= cap99);
  int64_t opp = contract_capacity_pages(c99, 0, CONTRACT_OPP);
  assert(opp >= cap99);                   /* C_0.5 informational >= C_0.99 */
  contract_destroy(c50);
  contract_destroy(c99);
  ctier_destroy(t);
  OK("advertised capacity monotonic in q (C_0.5 >= C_0.99)");
}

static void test_reject_over_budget(void) {
  ctier_t *t = mk_tier_ext(4ull << 20, 65536, SMALL_EXTENT, "rj");
  contract_t *c = contract_create(t, CONTRACT_EST_PESSIMISTIC, 0.99, 0.05, 2.0, 1024, 1, 1);
  assert(c);
  /* B_total = 0.95*4MiB = 3984588; pessimistic reserve = n*4096 */
  assert(contract_admit(c, 0, CONTRACT_GUAR, 2000) == CONTRACT_EREJECT);
  assert(contract_admit(c, 0, CONTRACT_GUAR, 500) == 0);
  assert(contract_check_invariant(c) == 0);
  contract_destroy(c);
  ctier_destroy(t);
  OK("admission rejected when r(n) > B");
}

static void test_reservation_drain(void) {
  ctier_t *t = mk_tier(16ull << 20, 65536, "rd");
  contract_t *c = contract_create(t, CONTRACT_EST_ANALYTIC, 0.99, 0.05, 2.0, 1024, 1, 1);
  assert(c);
  warm_opp(c, t, 128, 0);
  assert(contract_admit(c, 0, CONTRACT_GUAR, 50) == 0);
  assert(contract_stat_u64(c, -1, CONTRACT_STAT_RESV_BYTES) > 0);
  assert(contract_check_invariant(c) == 0);
  uint8_t src[PAGE_SIZE];
  for (int i = 0; i < 50; i++) {
    gen_text(src, (uint64_t)(1000 + i));
    assert(contract_write(c, 0, CONTRACT_GUAR, (uint64_t)(10000 + i), src) == 0);
  }
  assert(contract_stat_u64(c, -1, CONTRACT_STAT_RESV_BYTES) <= 1);
  assert(contract_check_invariant(c) == 0);
  /* unadmitted guaranteed write on a fresh lpn must be rejected */
  gen_text(src, 7777);
  assert(contract_write(c, 0, CONTRACT_GUAR, 20000, src) == CONTRACT_EINVAL);
  contract_destroy(c);
  ctier_destroy(t);
  OK("reservation drains to 0 after writing all admitted pages");
}

/* E1-style fill: warm a high-variance zero/random 50:50 workload until the
 * tenant EWMA is unbiased (400 samples at alpha=0.02), advertise C_q once,
 * admit it as ONE reservation group, then write C_q pages of a mildly
 * random-heavier mix (62.5% random -> per-page mean 1.25x the advertised mu).
 * The analytic variance margin (~4.5% of the pool at q=0.99) plus R1's
 * opportunistic relief absorbs the drift; a zero-margin (optimistic)
 * advertisement is pushed past the L3 watermark into forced spills. */
static uint64_t run_estimator_fill(int estimator, uint64_t *r1, uint64_t *r3,
                                   uint64_t *hard, const char *tag) {
  ctier_t *t = mk_tier_ext(4ull << 20, 65536, SMALL_EXTENT, tag);
  contract_t *c = contract_create(t, estimator, 0.99, 0.05, 2.0, 1024, 1,
                                  CONTRACT_VICTIM_DENSITY_AGE);
  assert(c);
  uint8_t src[PAGE_SIZE];
  for (int i = 0; i < 400; i++) {         /* warm: OPP, zero/random alternate */
    if (i % 2 == 0) memset(src, 0, PAGE_SIZE);
    else gen_random(src, (uint64_t)i);
    assert(contract_write(c, 0, CONTRACT_OPP, (uint64_t)i, src) == 0);
  }
  int64_t cq = contract_capacity_pages(c, 0, CONTRACT_GUAR);
  assert(cq > 100);
  assert(contract_admit(c, 0, CONTRACT_GUAR, (uint64_t)cq) == 0);
  for (int64_t i = 0; i < cq; i++) {
    uint64_t lpn = 1000 + (uint64_t)i;
    if (i % 8 < 5) gen_random(src, lpn);  /* 62.5% random: the mild lie */
    else memset(src, 0, PAGE_SIZE);
    int w = contract_write(c, 0, CONTRACT_GUAR, lpn, src);
    assert(w == 0 || w == CONTRACT_ENOSPACE);
    if (i % 256 == 0) assert(contract_check_invariant(c) == 0);
  }
  assert(contract_check_invariant(c) == 0);
  uint64_t v = contract_stat_u64(c, -1, CONTRACT_STAT_VIOLATIONS);
  *r1 = contract_stat_u64(c, -1, CONTRACT_STAT_R1_SPILLS);
  *r3 = contract_stat_u64(c, -1, CONTRACT_STAT_R3_FORCED_SPILLS);
  *hard = contract_stat_u64(c, -1, CONTRACT_STAT_HARD_FAILURES);
  contract_destroy(c);
  ctier_destroy(t);
  return v;
}

static void test_ladder_estimators(void) {
  uint64_t r1o, r3o, ho, r1a, r3a, ha;
  uint64_t v_opt = run_estimator_fill(CONTRACT_EST_OPTIMISTIC, &r1o, &r3o, &ho, "lo");
  uint64_t v_ana = run_estimator_fill(CONTRACT_EST_ANALYTIC, &r1a, &r3a, &ha, "la");
  assert(v_opt > 0);                      /* optimistic lies -> forced spills */
  assert(r3o > 0);
  assert(r1o > 0);                        /* ladder engaged from rung 1 */
  assert(v_ana == 0);                     /* analytic q=0.99 honest, same load */
  assert(ha == 0);
  (void)r1a; (void)r3a;
  OK("ladder engages; VIOLATIONS>0 optimistic, ==0 analytic q=0.99");
}

/* shed callback: reentrantly frees up to 64 of the recorded GUAR pages */
typedef struct {
  contract_t *c;
  uint64_t lpns[512];
  int count;
  int invoked;
} shed_ctx_t;

static void shed_cb(void *arg, uint64_t bytes_needed) {
  shed_ctx_t *s = arg;
  (void)bytes_needed;
  s->invoked++;
  int freed = 0;
  while (s->count > 0 && freed < 64) {
    uint64_t lpn = s->lpns[--s->count];
    if (contract_free(s->c, 0, lpn) == 0) freed++;
  }
}

static void test_shed_reentrant(void) {
  ctier_t *t = mk_tier_ext(4ull << 20, 65536, SMALL_EXTENT, "sh");
  contract_t *c = contract_create(t, CONTRACT_EST_EXTERNAL, 0.99, 0.05, 2.0, 1024, 1, 1);
  assert(c);
  shed_ctx_t ctx = {.c = c, .count = 0, .invoked = 0};
  assert(contract_register_shed(c, 0, shed_cb, &ctx) == 0);
  warm_opp(c, t, 100, 0);
  assert(contract_admit(c, 0, CONTRACT_GUAR, 4000) == 0);  /* gate open */
  uint8_t src[PAGE_SIZE];
  uint64_t lpn = 2000;
  for (int i = 0; i < 1200; i++, lpn++) {
    gen_random(src, lpn);
    int w = contract_write(c, 0, CONTRACT_GUAR, lpn, src);
    if (w != 0) break;
    if (ctx.count < 512) ctx.lpns[ctx.count++] = lpn;
    if (ctx.invoked > 0 && i > 900) break;
  }
  assert(ctx.invoked > 0);
  assert(contract_stat_u64(c, -1, CONTRACT_STAT_R2_SHED_CALLS) > 0);
  assert(contract_stat_u64(c, -1, CONTRACT_STAT_R2_SHED_BYTES) > 0);
  assert(contract_check_invariant(c) == 0);
  contract_destroy(c);
  ctier_destroy(t);
  OK("shed callback invoked; reentrant contract_free works");
}

static void test_external_admits_anything(void) {
  ctier_t *t = mk_tier_ext(4ull << 20, 65536, SMALL_EXTENT, "ex");
  contract_t *c = contract_create(t, CONTRACT_EST_EXTERNAL, 0.99, 0.05, 2.0, 1024, 1, 1);
  assert(c);
  assert(contract_admit(c, 0, CONTRACT_GUAR, 1000000) == 0);
  assert(contract_admit(c, 0, CONTRACT_GUAR, 123456789) == 0);
  assert(contract_check_invariant(c) == 0);
  contract_destroy(c);
  ctier_destroy(t);
  OK("EXTERNAL estimator admits anything");
}

static void test_feedback_integration(void) {
  ctier_t *t = mk_tier_ext(4ull << 20, 1024, SMALL_EXTENT, "fb");
  assert(ctier_feedback_enable(t, 2) == 0);
  uint8_t src[PAGE_SIZE];
  gen_text(src, 7);
  assert(ctier_write(t, 7, src) >= 0);
  gen_random(src, 7);
  assert(ctier_write(t, 7, src) >= 0);
  gen_text(src, 8);
  assert(ctier_write(t, 8, src) >= 0);   /* completion overflows, write stays */
  assert(ctier_feedback_dropped(t) == 1);
  ct_feedback_record_t a, b, d, f, r;
  assert(ctier_feedback_poll(t, &a) == 1 && a.sequence == 1 &&
         a.token == 7 && a.generation == 1);
  assert(ctier_feedback_poll(t, &b) == 1 && b.sequence == 2 &&
         b.token == 7 && b.generation == 1);
  gen_text(src, 9);
  assert(ctier_write(t, 9, src) >= 0);
  assert(ctier_feedback_poll(t, &d) == 1 && d.sequence == 4);
  ct_feedback_tracker_t tr;
  ct_feedback_tracker_init(&tr);
  assert(ct_feedback_tracker_apply(&tr, &a, 4096) == 0);
  assert(ct_feedback_tracker_apply(&tr, &b, 4096) == 0);
  assert(ct_feedback_tracker_apply(&tr, &d, 4096) == 2);
  assert(tr.gap_records == 1 && tr.telemetry_debt_bytes == 4096);
  assert(ctier_free_page(t, 7) >= 0);
  assert(ctier_feedback_poll(t, &f) == 1 && f.sequence == 5 &&
         f.token == 7 && f.generation == 1 && f.status == CT_FEEDBACK_FREE);
  gen_text(src, 7);
  assert(ctier_write(t, 7, src) >= 0);
  assert(ctier_feedback_poll(t, &r) == 1 && r.sequence == 6 &&
         r.token == 7 && r.generation == 2);
  ctier_destroy(t);
  OK("completions preserve allocation epochs, report free, and expose loss debt");
}

int main(void) {
  test_roundtrip_all_styles();
  test_zero_and_raw();
  test_overwrite_accounting();
  test_free_returns_bytes();
  test_spill_promote();
  test_compaction();
  test_enospace_old_intact();
  test_telemetry();
  test_norminv();
  test_admit_monotonic_q();
  test_reject_over_budget();
  test_reservation_drain();
  test_ladder_estimators();
  test_shed_reentrant();
  test_external_admits_anything();
  test_feedback_integration();
  printf("all %d tests passed\n", g_test_no);
  return 0;
}
