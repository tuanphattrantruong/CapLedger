/* contract.c — capacity contract: probabilistic advertisement, admission
 * against reservation groups, per-tenant accounting, lie detection, and the
 * graduated recovery ladder. One recursive mutex guards all state; lock order
 * is always contract -> ctier. See contract.h for caller-visible semantics.
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>
#include "contract.h"
#include "internal.h"   /* private ctier view (same package): max_pages */

#define CMAX_TENANTS 8
#define NIL32 0xFFFFFFFFu

#define WM_L1 0.85
#define WM_L2 0.92
#define WM_L3 0.97
#define WM_LOW 0.80
#define WM_L3_TARGET 0.92
#define LIE_RATIO 1.4
#define ALPHA_TENANT 0.02
#define COLD_MIN 64u
#define LADDER_RETRIES 8
#define SCAN_K 64
#define PRIOR_MU 4096.0
#define PRIOR_SIGMA 0.0

typedef struct { uint64_t n_remaining; double per_page; } group_t;

typedef struct {          /* FIFO ring of reservation groups */
  group_t *g;
  uint32_t cap, head, count;
} gq_t;

typedef struct {
  double mu, sigma2;
  uint64_t nsamples;
  /* Per-tenant window of recent class-rounded write sizes, with the same
   * exact-integer running sums the device keeps globally. The device's window
   * is device-GLOBAL, which is the right thing for a controller to export but
   * the wrong thing to price a tenant with: one tenant's inflation would raise
   * every tenant's price and couple them together (measured in E9). The
   * contract already sees the size of every write it forwards, so it can keep
   * a private window per tenant for a few KiB and price each on its own data. */
  uint16_t *win;
  uint32_t win_cap, win_pos;
  uint64_t win_count, win_sum, win_sumsq;
  int64_t realized;       /* bytes currently IN POOL attributable to tenant;
                           * reconciles against the device PHYS_USED gauge */
  int64_t owned;          /* bytes of all the tenant's live pages, resident OR
                           * spilled. Attribution must not depend on where a
                           * page currently sits: `realized` drops when the
                           * ladder spills a page, so an overdraft built on it
                           * falls every time the tenant is evicted -- the liar
                           * stops looking like the liar after the first spill,
                           * and the next victim is someone innocent. A tenant
                           * still owns a page it was forced to spill (a read
                           * promotes it back), so `owned` is the honest basis
                           * for "what you hold vs what you bought". */
  double consumed;        /* reservation bytes consumed by writes */
  /* Bytes of reservation revoked from this tenant by mark-to-market, i.e. the
   * discount it obtained by buying at a price its data no longer justifies.
   * Repricing makes a shaper's booking honest, which by construction erases
   * the positive overdraft the ladder uses to blame it -- so the debt is
   * remembered separately and kept in the victim ordering. Without it,
   * repricing protects the shaper and the eviction lands on honest tenants
   * (measured in E9 before this was added). */
  double reprice_debt;
  contract_shed_cb cb;
  void *cb_arg;
} tenant_t;

struct contract {
  pthread_mutex_t mx;     /* PTHREAD_MUTEX_RECURSIVE */
  ctier_t *tier;
  int estimator, ntenants, victim_policy;
  uint32_t ring_window;     /* ANALYTIC_RING moment window (0 = whole ring) */
  int reprice;              /* mark-to-market open groups on a raised lie flag */
  double reserve_frac;      /* TRANSPARENT device-held reserve */
  int gate_open;            /* 1: admission never refuses (EXTERNAL/ZSWAP/TRANSP) */
  int no_reserve;           /* 1: no per-tenant reservation accounting (ZSWAP)   */
  int no_ladder;            /* 1: no spill path at all; ENOSPACE is fatal (TRANSP)*/
  int last_lie;             /* edge detector for reprice */
  double q, z, headroom, static_ratio;
  uint32_t epoch_ops;
  uint64_t max_pages;
  uint64_t B_total;

  /* per-page arrays (max_pages each) */
  uint8_t  *pg_tenant, *pg_cls, *pg_ref, *pg_state; /* 0 none,1 resident,2 spilled */
  uint32_t *pg_epoch, *pg_prev, *pg_next;
  uint16_t *pg_cost;      /* class-rounded bytes of the last placement */
  double   *pg_resv;      /* exact reservation bytes charged to this GUAR page */

  uint32_t hand[CMAX_TENANTS][2];      /* clock hands, NIL32 = empty list */
  uint64_t list_len[CMAX_TENANTS][2];

  uint64_t epoch_now, op_counter, pages_tracked;
  double resv_bytes;
  gq_t groups[CMAX_TENANTS];
  tenant_t ten[CMAX_TENANTS];

  uint64_t st_g[CONTRACT_N_U64_STATS];
  uint64_t st_t[CMAX_TENANTS][CONTRACT_N_U64_STATS];
  int in_ladder;
};

/* ---------- small helpers ---------- */

static uint64_t phys_used(contract_t *c) {
  return ctier_stat_u64(c->tier, CTIER_STAT_PHYS_USED);
}

/* Bytes the contract has PROMISED to back: every live page of every tenant,
 * resident or spilled. The admission gate must charge against this rather than
 * against the device's resident-bytes gauge. Charging against residency lets
 * the gate admit into room the ladder just made by force-spilling guaranteed
 * pages -- admit, spill, admit again -- so "guaranteed" degenerates into "we
 * will find you somewhere to be", and the spill counter grows without bound
 * while the gate reports success (the admit-and-spill treadmill measured in
 * E9). The watermarks stay on phys_used: those are about the physical pool. */
static uint64_t committed(contract_t *c) {
  int64_t s = 0;
  for (int t = 0; t < c->ntenants; t++) s += c->ten[t].owned;
  return s > 0 ? (uint64_t)s : 0;
}

static void bump(contract_t *c, int tenant, int id, uint64_t v) {
  if (id < 0 || id >= CONTRACT_N_U64_STATS) return;
  c->st_g[id] += v;
  if (tenant >= 0 && tenant < c->ntenants) c->st_t[tenant][id] += v;
}

static void op_tick(contract_t *c) {
  c->op_counter++;
  if (c->op_counter % c->epoch_ops == 0) {
    c->epoch_now++;
    c->st_g[CONTRACT_STAT_EPOCH] = c->epoch_now;
  }
}

/* O(1): add the new sample, subtract the one it evicts. */
static void tenant_window_push(tenant_t *T, uint16_t x) {
  if (!T->win || T->win_cap == 0) return;
  if (T->win_count >= T->win_cap) {
    uint64_t old = T->win[T->win_pos];
    T->win_sum -= old;
    T->win_sumsq -= old * old;
  }
  T->win_sum += x;
  T->win_sumsq += (uint64_t)x * (uint64_t)x;
  T->win[T->win_pos] = x;
  T->win_pos = (T->win_pos + 1u) % T->win_cap;
  T->win_count++;
}

/* Sample mean and population stddev of the tenant's own window. Returns the
 * sample count, 0 when the window is empty or absent. */
static uint64_t tenant_window_moments(tenant_t *T, double *mu, double *sg) {
  if (!T->win || T->win_count == 0) return 0;
  uint64_t n = T->win_count < T->win_cap ? T->win_count : T->win_cap;
  double m = (double)T->win_sum / (double)n;
  double var = (double)T->win_sumsq / (double)n - m * m;
  if (var < 0) var = 0;
  *mu = m;
  *sg = sqrt(var);
  return n;
}

/* The accounting quantity: bytes this tenant's live pages occupy, minus the
 * reservation backing them. Reported as-is via CONTRACT_STAT_OVERDRAFT_BYTES. */
static double overdraft(contract_t *c, int t) {
  return (double)c->ten[t].owned - c->ten[t].consumed;
}

/* The victim-ordering key: overdraft plus any remembered repricing debt. */
static double victim_debt(contract_t *c, int t) {
  return overdraft(c, t) + c->ten[t].reprice_debt;
}

/* Repricing debt is evidence attached to live holdings, not a permanent
 * punishment.  It is globally capped by one pool, paid down as pages are
 * force-spilled or freed, and erased when the tenant owns no pages.  We do not
 * cap it to the footprint at the instant the first detector edge fires: early
 * detection can precede most of the shaped reservation's writes, and doing so
 * would discard precisely the exposure the debt is meant to remember. */
static void bound_reprice_debt(contract_t *c, int t) {
  double cap = c->ten[t].owned > 0 ? (double)c->B_total : 0.0;
  if (c->ten[t].reprice_debt < 0) c->ten[t].reprice_debt = 0;
  if (c->ten[t].reprice_debt > cap) c->ten[t].reprice_debt = cap;
}

static void pay_reprice_debt(contract_t *c, int t, uint64_t bytes) {
  c->ten[t].reprice_debt -= (double)bytes;
  bound_reprice_debt(c, t);
}

/* effective per-page (mu, sigma).
 *
 * ANALYTIC_RING takes its moments from the device's ring window instead of the
 * tenant EWMA. E1 diagnosed the original arm's miscalibration as estimator-INPUT
 * noise (an alpha=0.02 EWMA has an effective sample size of ~2/alpha = 100, so
 * mu-hat wanders far more than the CLT margin it is being fed into); a 4096-
 * sample window is ~40x more data for the same O(1) query cost. Named tenants
 * use their own window; only an explicitly device-global query (tenant < 0)
 * may use the controller-wide ring.
 *
 * A named tenant never inherits another tenant's history: until its own
 * window/EWMA has 64 samples it receives the pessimistic raw-page prior.
 * Tenant < 0 is an explicitly device-global query and may use global state. */
static void eff_musigma(contract_t *c, int tenant, double *mu, double *sg) {
  int named = tenant >= 0 && tenant < c->ntenants;
  if (c->estimator == CONTRACT_EST_ANALYTIC_RING) {
    double m = 0, v = 0;
    /* the tenant's own window first, so tenants are priced independently */
    if (named &&
        tenant_window_moments(&c->ten[tenant], &m, &v) >= COLD_MIN && m > 0) {
      *mu = m;
      *sg = v;
      return;
    }
    if (named) {
      *mu = PRIOR_MU;
      *sg = PRIOR_SIGMA;
      return;
    }
    int n = ctier_ring_moments(c->tier, c->ring_window, &m, &v);
    if (n >= (int)COLD_MIN && m > 0) { *mu = m; *sg = v; return; }
    /* fall through to the EWMA chain while both windows are still filling */
  }
  if (named &&
      c->ten[tenant].nsamples >= COLD_MIN) {
    *mu = c->ten[tenant].mu;
    double v = c->ten[tenant].sigma2;
    *sg = v > 0 ? sqrt(v) : 0.0;
    if (*mu > 0) return;
  }
  if (named) {
    *mu = PRIOR_MU;
    *sg = PRIOR_SIGMA;
    return;
  }
  if (ctier_stat_u64(c->tier, CTIER_STAT_WRITES) >= COLD_MIN) {
    double m = ctier_stat_f64(c->tier, CTIER_STAT_EWMA_MEAN_SLOW);
    double v = ctier_stat_f64(c->tier, CTIER_STAT_EWMA_VAR_SLOW);
    if (m > 0 || v > 0) {
      *mu = m;
      *sg = v > 0 ? sqrt(v) : 0.0;
      return;
    }
  }
  *mu = PRIOR_MU;
  *sg = PRIOR_SIGMA;
}

static double reserve_for(contract_t *c, int tenant, uint64_t n) {
  double mu, sg, r;
  eff_musigma(c, tenant, &mu, &sg);
  switch (c->estimator) {
  case CONTRACT_EST_STATIC:      r = (double)n * (PAGE_SIZE / c->static_ratio); break;
  case CONTRACT_EST_OPTIMISTIC:
  case CONTRACT_EST_EXTERNAL:
  case CONTRACT_EST_ZSWAP:
  case CONTRACT_EST_TRANSPARENT: r = (double)n * mu; break;
  case CONTRACT_EST_PESSIMISTIC: r = (double)n * PAGE_SIZE; break;
  case CONTRACT_EST_ANALYTIC:
  case CONTRACT_EST_ANALYTIC_RING:
  default:
    r = (double)n * mu + c->z * sg * sqrt((double)n);
    break;
  }
  /* A page can always be charged at its uncompressed 4 KiB size.  No
   * statistical estimate may reserve more than that deterministic cap. */
  double raw = (double)n * PAGE_SIZE;
  if (r > raw) r = raw;
  return r > 0 ? r : 0.0;
}

/* advertised capacity in pages for capacity class cls; z_eff = z for GUAR
 * analytic, 0 for the OPP informational C_0.5 */
static int64_t capacity_pages_locked(contract_t *c, int tenant, int cls) {
  double mu, sg;
  eff_musigma(c, tenant, &mu, &sg);
  double pu = (double)committed(c);
  double B = (double)c->B_total - pu - c->resv_bytes;
  uint64_t free_slots = c->max_pages - c->pages_tracked;
  if (B <= 0) return 0;
  double C;
  int est = (cls == CONTRACT_OPP) ? CONTRACT_EST_ANALYTIC : c->estimator;
  double z = (cls == CONTRACT_OPP) ? 0.0 : c->z;
  switch (est) {
  case CONTRACT_EST_STATIC:
    C = floor(B / (PAGE_SIZE / c->static_ratio));
    break;
  case CONTRACT_EST_PESSIMISTIC:
    C = floor(B / PAGE_SIZE);
    break;
  case CONTRACT_EST_OPTIMISTIC:
  case CONTRACT_EST_EXTERNAL:
  case CONTRACT_EST_ZSWAP:
  case CONTRACT_EST_TRANSPARENT:
    C = (mu < 1e-9) ? (double)free_slots : floor(B / mu);
    break;
  case CONTRACT_EST_ANALYTIC:
  case CONTRACT_EST_ANALYTIC_RING:
  default:
    if (mu < 1e-9) {
      C = (double)free_slots;
    } else {
      double zz = z * sg;
      double x = (-zz + sqrt(zz * zz + 4.0 * mu * B)) / (2.0 * mu);
      if (x < 0) x = 0;
      C = floor(x * x);
    }
    break;
  }
  /* Inverting the capped reservation requires capacity to be at least the
   * deterministic raw-page capacity, even when the Gaussian expression is
   * inflated by variance or estimation noise. */
  double raw_C = floor(B / PAGE_SIZE);
  if (C < raw_C) C = raw_C;
  if (C < 0) C = 0;
  if (C > (double)free_slots) C = (double)free_slots;
  return (int64_t)C;
}

/* ---------- group FIFO ---------- */

static int gq_push(gq_t *q, uint64_t n, double per_page) {
  if (q->count == q->cap) {
    uint32_t nc = q->cap ? q->cap * 2 : 16;
    group_t *ng = malloc((size_t)nc * sizeof(group_t));
    if (!ng) return -1;
    for (uint32_t i = 0; i < q->count; i++)
      ng[i] = q->g[(q->head + i) % (q->cap ? q->cap : 1)];
    free(q->g);
    q->g = ng;
    q->cap = nc;
    q->head = 0;
  }
  q->g[(q->head + q->count) % q->cap] = (group_t){n, per_page};
  q->count++;
  return 0;
}

static group_t *gq_head(gq_t *q) {
  while (q->count && q->g[q->head].n_remaining == 0) {
    q->head = (q->head + 1) % q->cap;
    q->count--;
  }
  return q->count ? &q->g[q->head] : NULL;
}

/* Snap resv_bytes to the exact sum over open groups. Incremental -=
 * subtractions accumulate one ulp of float error per consumed unit; over tens
 * of thousands of writes that drift can exceed the invariant tolerance.
 * Called whenever a group drains, bounding drift by the open-group count. */
static void recompute_resv(contract_t *c) {
  double s = 0;
  for (int t = 0; t < c->ntenants; t++) {
    gq_t *q = &c->groups[t];
    for (uint32_t i = 0; i < q->count; i++) {
      group_t *g = &q->g[(q->head + i) % q->cap];
      s += (double)g->n_remaining * g->per_page;
    }
  }
  c->resv_bytes = s;
}

/* ---------- clock lists ---------- */

static void clk_insert(contract_t *c, int t, int cl, uint32_t lpn) {
  uint32_t h = c->hand[t][cl];
  if (h == NIL32) {
    c->pg_next[lpn] = c->pg_prev[lpn] = lpn;
    c->hand[t][cl] = lpn;
  } else {
    uint32_t p = c->pg_prev[h];
    c->pg_next[p] = lpn;
    c->pg_prev[lpn] = p;
    c->pg_next[lpn] = h;
    c->pg_prev[h] = lpn;
  }
  c->list_len[t][cl]++;
}

static void clk_remove(contract_t *c, int t, int cl, uint32_t lpn) {
  if (c->pg_next[lpn] == lpn) {
    c->hand[t][cl] = NIL32;
  } else {
    uint32_t p = c->pg_prev[lpn], n = c->pg_next[lpn];
    c->pg_next[p] = n;
    c->pg_prev[n] = p;
    if (c->hand[t][cl] == lpn) c->hand[t][cl] = n;
  }
  c->list_len[t][cl]--;
}

/* victim selection on one tenant x class list; NIL32 if none usable.
 * CLOCK_ONLY: classic second chance. DENSITY_AGE: scan up to SCAN_K clock
 * candidates (clearing refbits as passed), evict max cost*(1+age_epochs).
 * Zero-cost pages (zero pages / already accounted) are never selected. */
static uint32_t pick_victim(contract_t *c, int t, int cl) {
  uint64_t len = c->list_len[t][cl];
  if (len == 0) return NIL32;
  if (c->victim_policy == CONTRACT_VICTIM_CLOCK_ONLY) {
    uint64_t lim = 2 * len + 2;
    while (lim--) {
      uint32_t v = c->hand[t][cl];
      c->hand[t][cl] = c->pg_next[v];
      if (c->pg_cost[v] == 0) continue;
      if (c->pg_ref[v]) { c->pg_ref[v] = 0; continue; }
      return v;
    }
    return NIL32;
  }
  if (c->victim_policy == CONTRACT_VICTIM_GLOBAL_LRU) {
    /* Oldest-epoch-first within this list, tenant-blind by construction: the
     * ZSWAP baseline calls this once per tenant list and takes the globally
     * oldest candidate (see spill_one_guar). Ties break toward the clock hand,
     * matching an LRU list walked from the tail. */
    uint64_t m = len < SCAN_K ? len : SCAN_K;
    uint32_t best = NIL32;
    int64_t best_age = -1;
    for (uint64_t i = 0; i < m; i++) {
      uint32_t v = c->hand[t][cl];
      c->hand[t][cl] = c->pg_next[v];
      if (c->pg_cost[v] == 0) continue;
      int64_t age = (int64_t)(uint32_t)(c->epoch_now - c->pg_epoch[v]);
      if (age > best_age) { best_age = age; best = v; }
    }
    return best;
  }
  uint64_t m = len < SCAN_K ? len : SCAN_K;
  uint32_t best = NIL32;
  double best_score = -1.0;
  for (uint64_t i = 0; i < m; i++) {
    uint32_t v = c->hand[t][cl];
    c->hand[t][cl] = c->pg_next[v];
    if (c->pg_ref[v]) c->pg_ref[v] = 0;
    if (c->pg_cost[v] == 0) continue;
    uint32_t age = (uint32_t)c->epoch_now - c->pg_epoch[v];
    double score = (double)c->pg_cost[v] * (1.0 + (double)age);
    if (score > best_score) { best_score = score; best = v; }
  }
  return best;
}

/* spill one tracked resident page; returns bytes released or <0 */
static int spill_page(contract_t *c, uint32_t lpn) {
  int t = c->pg_tenant[lpn], cl = c->pg_cls[lpn];
  int b = ctier_spill(c->tier, lpn);
  if (b < 0) return b;
  clk_remove(c, t, cl, lpn);
  c->pg_state[lpn] = 2;
  c->ten[t].realized -= (int64_t)c->pg_cost[lpn];   /* owned is unchanged */
  return b;
}

/* Mark open reservation groups to market (the history-shaping countermeasure,
 * E9). A tenant that wrote compressible pages while its telemetry window filled
 * buys its reservation at a stale-cheap price and then stores incompressible
 * data in those pages: it holds budget at a discount to what it will consume.
 *
 * The repair is to hold the tenant to the BYTES it purchased rather than the
 * PAGE COUNT. Re-pricing a group at the current unit price and re-booking the
 * same page count is the wrong move -- it converts a hidden overcommit into a
 * visible one that crowds honest tenants out of the gate (measured: it made
 * their admission rate worse). Instead the group's booked bytes are held fixed
 * and its page count is scaled down by the price increase:
 *
 *     keep = floor(n_remaining * old_price / new_price)
 *
 * so a tenant whose data got 2.3x denser keeps 43% of its unwritten pages and
 * must re-admit for the rest, at today's price, competing on equal terms.
 * A tenant whose price did not move keeps everything. Already-consumed
 * reservation is untouched; the overdraft accounting already covers it.
 * Called on the rising edge of LIE_FLAG. */
static void reprice_groups(contract_t *c) {
  uint64_t revoked = 0;
  for (int t = 0; t < c->ntenants; t++) {
    gq_t *q = &c->groups[t];
    if (q->count == 0) continue;
    double unit = reserve_for(c, t, 1);          /* this tenant's price now */
    if (unit <= 0) continue;
    for (uint32_t i = 0; i < q->count; i++) {
      group_t *g = &q->g[(q->head + i) % q->cap];
      if (g->n_remaining == 0 || unit <= g->per_page) continue;
      uint64_t keep = (uint64_t)floor((double)g->n_remaining * g->per_page / unit);
      if (keep > g->n_remaining) keep = g->n_remaining;
      uint64_t lost = g->n_remaining - keep;
      revoked += lost;
      /* remember the discount, in bytes, so attribution survives repricing */
      c->ten[t].reprice_debt += (double)lost * (unit - g->per_page);
      g->n_remaining = keep;
      g->per_page = unit;
    }
    bound_reprice_debt(c, t);
  }
  recompute_resv(c);
  c->st_g[CONTRACT_STAT_RESV_BYTES] =
      (uint64_t)(c->resv_bytes > 0 ? c->resv_bytes : 0);
  c->st_g[CONTRACT_STAT_REPRICE_EVENTS] += 1;
  c->st_g[CONTRACT_STAT_REPRICE_REVOKED] += revoked;
}

/* ---------- pressure / lie / ladder ---------- */

static void refresh_pressure(contract_t *c) {
  double u = (double)phys_used(c) / (double)c->B_total;
  uint64_t lvl = u > WM_L3 ? 3 : u > WM_L2 ? 2 : u > WM_L1 ? 1 : 0;
  c->st_g[CONTRACT_STAT_PRESSURE_LEVEL] = lvl;
}

static void update_lie(contract_t *c) {
  uint64_t n = ctier_stat_u64(c->tier, CTIER_STAT_WRITES);
  double fast = ctier_stat_f64(c->tier, CTIER_STAT_EWMA_MEAN_FAST);
  double slow = ctier_stat_f64(c->tier, CTIER_STAT_EWMA_MEAN_SLOW);
  int flag = (n >= COLD_MIN && fast > 0 && fast > LIE_RATIO * slow) ? 1 : 0;
  c->st_g[CONTRACT_STAT_LIE_FLAG] = (uint64_t)flag;
  uint64_t alarm = 0;
  ctier_cusum(c->tier, NULL, &alarm);
  c->st_g[CONTRACT_STAT_CUSUM_FLAG] = alarm;
  if (c->reprice && flag && !c->last_lie) reprice_groups(c);
  c->last_lie = flag;
}

/* spill one opportunistic page (largest-OPP-population tenant first);
 * returns bytes freed or -1 when no victim exists */
static int spill_one_opp(contract_t *c) {
  int tv = -1;
  uint64_t most = 0;
  for (int t = 0; t < c->ntenants; t++)
    if (c->list_len[t][CONTRACT_OPP] > most) {
      most = c->list_len[t][CONTRACT_OPP];
      tv = t;
    }
  if (tv < 0) return -1;
  uint32_t v = pick_victim(c, tv, CONTRACT_OPP);
  if (v == NIL32) {
    int found = 0;
    for (int t = 0; t < c->ntenants && !found; t++) {
      if (t == tv || c->list_len[t][CONTRACT_OPP] == 0) continue;
      v = pick_victim(c, t, CONTRACT_OPP);
      if (v != NIL32) { tv = t; found = 1; }
    }
    if (!found) return -1;
  }
  int b = spill_page(c, v);
  if (b <= 0) return -1;
  bump(c, tv, CONTRACT_STAT_R1_SPILLS, 1);
  return b;
}

/* forced-spill one guaranteed page (a counted contract violation).
 * Pass 1: positive-overdraft tenants, worst first; pass 2: any tenant
 * (touching a non-overdrafted tenant additionally counts a breach).
 * Returns bytes freed or -1 when no victim exists. */
static int spill_one_guar(contract_t *c) {
  uint32_t v = NIL32;
  int tv = -1, breach = 0;
  if (c->victim_policy == CONTRACT_VICTIM_GLOBAL_LRU) {
    /* No attribution: pick the globally oldest guaranteed page across every
     * tenant. This is the software-tier behaviour the contract is compared
     * against -- the cost lands on whoever is coldest, not on whoever lied. */
    int64_t best_age = -1;
    for (int i = 0; i < c->ntenants; i++) {
      if (c->list_len[i][CONTRACT_GUAR] == 0) continue;
      uint32_t cand = pick_victim(c, i, CONTRACT_GUAR);
      if (cand == NIL32) continue;
      int64_t age = (int64_t)(uint32_t)(c->epoch_now - c->pg_epoch[cand]);
      if (age > best_age) { best_age = age; v = cand; tv = i; }
    }
    if (v == NIL32 || tv < 0) return -1;
    int b = spill_page(c, v);
    if (b <= 0) return -1;
    pay_reprice_debt(c, tv, c->pg_cost[v]);
    bump(c, tv, CONTRACT_STAT_VIOLATIONS, 1);
    bump(c, tv, CONTRACT_STAT_R3_FORCED_SPILLS, 1);
    return b;
  }
  {
    int t = -1;
    double best = 1e-6;
    for (int i = 0; i < c->ntenants; i++)
      if (c->list_len[i][CONTRACT_GUAR] > 0) {
        double od = victim_debt(c, i);
        if (od > best) { best = od; t = i; }
      }
    if (t >= 0) {
      v = pick_victim(c, t, CONTRACT_GUAR);
      if (v != NIL32) tv = t;
    }
  }
  if (v == NIL32) {
    for (int i = 0; i < c->ntenants; i++) {
      if (c->list_len[i][CONTRACT_GUAR] == 0) continue;
      v = pick_victim(c, i, CONTRACT_GUAR);
      if (v != NIL32) {
        tv = i;
        breach = (victim_debt(c, i) <= 1e-6);
        break;
      }
    }
  }
  if (v == NIL32 || tv < 0) return -1;
  int b = spill_page(c, v);
  if (b <= 0) return -1;
  pay_reprice_debt(c, tv, c->pg_cost[v]);
  bump(c, tv, CONTRACT_STAT_VIOLATIONS, 1);
  bump(c, tv, CONTRACT_STAT_R3_FORCED_SPILLS, 1);
  if (breach) bump(c, tv, CONTRACT_STAT_INVARIANT_BREACHES, 1);
  return b;
}

static uint64_t ext_active(contract_t *c) {
  return ctier_stat_u64(c->tier, CTIER_STAT_EXTENTS_ACTIVE);
}

/* Cap on fragmentation-relief spills per rung per ladder pass: enough to
 * drain one worst-case extent of raw pages with headroom. */
#define FRAG_BATCH 512

/* force levels: 0 = post-write check (thresholds only); 1 = after a first
 * ENOSPACE (R1 engages down to the low watermark even below L1, and, if the
 * device is fragmented — ENOSPACE below the watermarks — R1 spills
 * opportunistic pages until a whole extent is released); 2 = persistent
 * ENOSPACE (R2 considered at L1+, R3 engages unconditionally, with the same
 * extent-release fragmentation relief using guaranteed pages, each counted as
 * a violation). */
static void run_ladder(contract_t *c, int force) {
  if (c->in_ladder) return;   /* shed-callback reentrancy guard */
  if (c->no_ladder) {         /* TRANSPARENT: the OS has no spill path at all */
    update_lie(c);
    refresh_pressure(c);
    return;
  }
  c->in_ladder = 1;
  uint64_t t0 = ctier_now_ns();
  update_lie(c);
  double bt = (double)c->B_total;
  double u = (double)phys_used(c) / bt;
  uint64_t ext0 = ext_active(c);

  /* R1: spill opportunistic pages */
  if (u > WM_L1 || (force >= 1 && u > WM_LOW)) {
    uint64_t guard = 2 * c->pages_tracked + 64;
    while (u > WM_LOW && guard--) {
      if (spill_one_opp(c) < 0) break;
      u = (double)phys_used(c) / bt;
    }
  }
  /* R1 fragmentation relief: ENOSPACE with no extent freed yet */
  if (force >= 1 && ext_active(c) >= ext0) {
    int cap = FRAG_BATCH;
    while (cap-- && ext_active(c) >= ext0)
      if (spill_one_opp(c) < 0) break;
  }

  /* R2: shed callbacks, worst-overdraft tenant first */
  u = (double)phys_used(c) / bt;
  if (u > WM_L2 || (force >= 2 && u > WM_L1)) {
    int order[CMAX_TENANTS], nord = 0;
    for (int t = 0; t < c->ntenants; t++)
      if (c->ten[t].cb) order[nord++] = t;
    for (int i = 1; i < nord; i++)          /* insertion sort, overdraft desc */
      for (int j = i; j > 0 &&
           victim_debt(c, order[j]) > victim_debt(c, order[j - 1]); j--) {
        int tmp = order[j]; order[j] = order[j - 1]; order[j - 1] = tmp;
      }
    for (int i = 0; i < nord; i++) {
      uint64_t pu = phys_used(c);
      if ((double)pu <= WM_L1 * bt) break;
      uint64_t need = pu - (uint64_t)(WM_L1 * bt);
      int t = order[i];
      bump(c, t, CONTRACT_STAT_R2_SHED_CALLS, 1);
      c->ten[t].cb(c->ten[t].cb_arg, need);
      uint64_t pu2 = phys_used(c);
      if (pu2 < pu) bump(c, t, CONTRACT_STAT_R2_SHED_BYTES, pu - pu2);
    }
  }

  /* R3: forced spill of guaranteed pages (contract violations) */
  u = (double)phys_used(c) / bt;
  if (u > WM_L3 || force >= 2) {
    uint64_t guard = 2 * c->pages_tracked + 64;
    while (u > WM_L3_TARGET && guard--) {
      if (spill_one_guar(c) < 0) break;
      u = (double)phys_used(c) / bt;
    }
  }
  /* R3 fragmentation relief: persistent ENOSPACE, still no extent freed */
  if (force >= 2 && ext_active(c) >= ext0) {
    int cap = FRAG_BATCH;
    while (cap-- && ext_active(c) >= ext0)
      if (spill_one_guar(c) < 0) break;
  }

  refresh_pressure(c);
  c->st_g[CONTRACT_STAT_RECOVERY_NS] += ctier_now_ns() - t0;
  c->in_ladder = 0;
}

/* ---------- public API ---------- */

contract_t *contract_create(ctier_t *tier, int estimator, double q_guaranteed,
                            double headroom_frac, double static_ratio,
                            uint32_t epoch_ops, int ntenants, int victim_policy) {
  contract_cfg_t cfg = {0};
  cfg.estimator = estimator;
  cfg.q_guaranteed = q_guaranteed;
  cfg.headroom_frac = headroom_frac;
  cfg.static_ratio = static_ratio;
  cfg.epoch_ops = epoch_ops;
  cfg.ntenants = ntenants;
  cfg.victim_policy = victim_policy;
  return contract_create2(tier, &cfg);
}

uint64_t contract_cfg_size(void) { return (uint64_t)sizeof(contract_cfg_t); }

contract_t *contract_create2(ctier_t *tier, const contract_cfg_t *cfg) {
  if (!tier || !cfg) return NULL;
  int estimator = cfg->estimator;
  double q_guaranteed = cfg->q_guaranteed, headroom_frac = cfg->headroom_frac;
  double static_ratio = cfg->static_ratio;
  uint32_t epoch_ops = cfg->epoch_ops;
  int ntenants = cfg->ntenants, victim_policy = cfg->victim_policy;
  if (estimator < CONTRACT_EST_ANALYTIC || estimator > CONTRACT_EST_REACTIVE_TENANT)
    return NULL;
  contract_t *c = calloc(1, sizeof(*c));
  if (!c) return NULL;
  c->tier = tier;
  c->estimator = estimator;
  c->q = (q_guaranteed <= 0.0 || q_guaranteed >= 1.0) ? 0.99 : q_guaranteed;
  c->z = norminv(c->q);
  c->headroom = headroom_frac < 0.0 ? 0.05 : headroom_frac;
  c->static_ratio = static_ratio <= 0.0 ? 2.0 : static_ratio;
  c->epoch_ops = epoch_ops == 0 ? 1024 : epoch_ops;
  c->ntenants = ntenants <= 0 ? 1 : (ntenants > CMAX_TENANTS ? CMAX_TENANTS : ntenants);
  c->ring_window = cfg->ring_window;
  c->reprice = cfg->reprice ? 1 : 0;
  c->reserve_frac = cfg->reserve_frac > 0 ? cfg->reserve_frac : 0.10;
  /* Baseline personalities. EXTERNAL and ZSWAP never refuse an admission;
   * ZSWAP additionally drops per-tenant reservation accounting and reclaims
   * over a global LRU; TRANSPARENT has no OS spill path, so exhausting the
   * device-held reserve is fatal rather than recoverable. */
  c->gate_open = (estimator == CONTRACT_EST_EXTERNAL ||
                  estimator == CONTRACT_EST_ZSWAP ||
                  estimator == CONTRACT_EST_TRANSPARENT ||
                  estimator == CONTRACT_EST_REACTIVE_TENANT);
  c->no_reserve = (estimator == CONTRACT_EST_ZSWAP ||
                   estimator == CONTRACT_EST_REACTIVE_TENANT);
  c->no_ladder  = (estimator == CONTRACT_EST_TRANSPARENT);
  if (estimator == CONTRACT_EST_ZSWAP)
    victim_policy = CONTRACT_VICTIM_GLOBAL_LRU;
  c->victim_policy = (victim_policy == CONTRACT_VICTIM_CLOCK_ONLY ||
                      victim_policy == CONTRACT_VICTIM_GLOBAL_LRU)
                         ? victim_policy
                         : CONTRACT_VICTIM_DENSITY_AGE;
  c->max_pages = tier->max_pages;
  uint64_t pool = ctier_stat_u64(tier, CTIER_STAT_POOL_BYTES);
  double frac = c->no_ladder ? c->reserve_frac : c->headroom;
  c->B_total = (uint64_t)floor((double)pool * (1.0 - frac));

  pthread_mutexattr_t at;
  pthread_mutexattr_init(&at);
  pthread_mutexattr_settype(&at, PTHREAD_MUTEX_RECURSIVE);
  if (pthread_mutex_init(&c->mx, &at) != 0) {
    pthread_mutexattr_destroy(&at);
    free(c);
    return NULL;
  }
  pthread_mutexattr_destroy(&at);

  c->pg_tenant = calloc(c->max_pages, 1);
  c->pg_cls    = calloc(c->max_pages, 1);
  c->pg_ref    = calloc(c->max_pages, 1);
  c->pg_state  = calloc(c->max_pages, 1);
  c->pg_epoch  = calloc(c->max_pages, 4);
  c->pg_prev   = calloc(c->max_pages, 4);
  c->pg_next   = calloc(c->max_pages, 4);
  c->pg_cost   = calloc(c->max_pages, 2);
  c->pg_resv   = calloc(c->max_pages, sizeof(*c->pg_resv));
  if (!c->pg_tenant || !c->pg_cls || !c->pg_ref || !c->pg_state ||
      !c->pg_epoch || !c->pg_prev || !c->pg_next || !c->pg_cost ||
      !c->pg_resv) {
    contract_destroy(c);
    return NULL;
  }
  uint32_t wcap = cfg->ring_window ? cfg->ring_window : 4096u;
  for (int t = 0; t < CMAX_TENANTS; t++) {
    c->hand[t][0] = c->hand[t][1] = NIL32;
    c->ten[t].mu = 0;
    c->ten[t].sigma2 = 0;
    if (t < c->ntenants) {
      c->ten[t].win = calloc(wcap, sizeof(uint16_t));
      if (!c->ten[t].win) { contract_destroy(c); return NULL; }
      c->ten[t].win_cap = wcap;
    }
  }
  return c;
}

void contract_destroy(contract_t *c) {
  if (!c) return;
  free(c->pg_tenant); free(c->pg_cls); free(c->pg_ref); free(c->pg_state);
  free(c->pg_epoch); free(c->pg_prev); free(c->pg_next); free(c->pg_cost);
  free(c->pg_resv);
  for (int t = 0; t < CMAX_TENANTS; t++) { free(c->groups[t].g); free(c->ten[t].win); }
  pthread_mutex_destroy(&c->mx);
  free(c);
}

int64_t contract_capacity_pages(contract_t *c, int tenant, int cls) {
  if (!c || tenant >= c->ntenants || (cls != CONTRACT_GUAR && cls != CONTRACT_OPP))
    return CONTRACT_EINVAL;
  pthread_mutex_lock(&c->mx);
  int64_t r = capacity_pages_locked(c, tenant, cls);
  pthread_mutex_unlock(&c->mx);
  return r;
}

int contract_admit(contract_t *c, int tenant, int cls, uint64_t npages) {
  if (!c || tenant < 0 || tenant >= c->ntenants ||
      (cls != CONTRACT_GUAR && cls != CONTRACT_OPP))
    return CONTRACT_EINVAL;
  if (npages == 0) return 0;
  pthread_mutex_lock(&c->mx);
  if (cls == CONTRACT_OPP) {
    bump(c, tenant, CONTRACT_STAT_ADMITTED_OPP, npages);
    pthread_mutex_unlock(&c->mx);
    return 0;
  }
  bump(c, tenant, CONTRACT_STAT_ADMIT_CALLS, 1);
  double r = reserve_for(c, tenant, npages);
  if (c->no_reserve) r = 0.0;      /* ZSWAP: no reservation is ever purchased */
  if (!c->gate_open) {
    double B = (double)c->B_total - (double)committed(c) - c->resv_bytes;
    uint64_t free_slots = c->max_pages - c->pages_tracked;
    if (r > B || npages > free_slots) {
      bump(c, tenant, CONTRACT_STAT_ADMIT_REJECTS, 1);
      pthread_mutex_unlock(&c->mx);
      return CONTRACT_EREJECT;
    }
  }
  if (gq_push(&c->groups[tenant], npages, r / (double)npages) != 0) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;
  }
  c->resv_bytes += r;
  c->st_g[CONTRACT_STAT_RESV_BYTES] = (uint64_t)(c->resv_bytes > 0 ? c->resv_bytes : 0);
  bump(c, tenant, CONTRACT_STAT_ADMITTED_GUAR, npages);
  pthread_mutex_unlock(&c->mx);
  return 0;
}

int contract_write(contract_t *c, int tenant, int cls, uint64_t lpn,
                   const uint8_t *src) {
  if (!c || !src || tenant < 0 || tenant >= c->ntenants ||
      (cls != CONTRACT_GUAR && cls != CONTRACT_OPP) || lpn >= c->max_pages)
    return CONTRACT_EINVAL;
  pthread_mutex_lock(&c->mx);
  op_tick(c);
  uint8_t ps = c->pg_state[lpn];
  if (ps != 0 &&
      (c->pg_tenant[lpn] != (uint8_t)tenant || c->pg_cls[lpn] != (uint8_t)cls)) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;
  }
  int is_new = (ps == 0);
  if (is_new && cls == CONTRACT_GUAR && gq_head(&c->groups[tenant]) == NULL) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;   /* unadmitted guaranteed write */
  }

  int64_t wr = ctier_write(c->tier, lpn, src);
  if (wr == CTIER_ENOSPACE) {
    if (cls == CONTRACT_OPP) {
      run_ladder(c, 1);
      wr = ctier_write(c->tier, lpn, src);
      if (wr == CTIER_ENOSPACE) {
        bump(c, tenant, CONTRACT_STAT_OPP_DROPS, 1);
        refresh_pressure(c);
        pthread_mutex_unlock(&c->mx);
        return CONTRACT_EAGAIN;
      }
    } else if (c->no_ladder) {
      /* TRANSPARENT: no spill path exists, so device exhaustion is terminal. */
      bump(c, tenant, CONTRACT_STAT_HARD_FAILURES, 1);
      refresh_pressure(c);
      pthread_mutex_unlock(&c->mx);
      return CONTRACT_ENOSPACE;
    } else {
      int attempts = 0;
      while (wr == CTIER_ENOSPACE) {
        attempts++;
        if (attempts > LADDER_RETRIES) {
          bump(c, tenant, CONTRACT_STAT_HARD_FAILURES, 1);
          refresh_pressure(c);
          pthread_mutex_unlock(&c->mx);
          return CONTRACT_ENOSPACE;
        }
        run_ladder(c, attempts >= 2 ? 2 : 1);
        wr = ctier_write(c->tier, lpn, src);
      }
    }
  }
  if (wr < 0) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;
  }

  /* The ENOSPACE ladder above (or a reentrant shed callback inside it) may
   * have spilled — or even freed — THIS lpn while we were retrying: the
   * pre-write snapshot of ps is then stale. Re-read the state for the
   * accounting below; spill_page/contract_free already adjusted realized for
   * the transition they performed, so accounting from the CURRENT state is
   * the only consistent choice (a stale ps==1 would double-subtract the old
   * cost and leave a resident page off the clock lists). */
  ps = c->pg_state[lpn];
  is_new = (ps == 0);

  tenant_t *T = &c->ten[tenant];
  uint16_t old_cost = (ps == 1) ? c->pg_cost[lpn] : 0;
  if (ps == 1) {
    T->realized += wr - (int64_t)old_cost;
    T->owned    += wr - (int64_t)old_cost;
  } else {
    T->realized += wr;                  /* new page, or spilled being rewritten */
    /* a spilled page being rewritten was already counted in `owned` */
    T->owned    += (ps == 2) ? (wr - (int64_t)c->pg_cost[lpn]) : wr;
  }
  c->pg_cost[lpn] = (uint16_t)wr;
  bound_reprice_debt(c, tenant);

  if (is_new) {
    c->pg_tenant[lpn] = (uint8_t)tenant;
    c->pg_cls[lpn] = (uint8_t)cls;
    c->pages_tracked++;
    if (cls == CONTRACT_GUAR) {
      /* Normally non-NULL (checked before the write). It can be NULL only in
       * the exotic case where a shed callback freed this very lpn mid-retry
       * (making the write "new" again) with no group left open; the page is
       * still tracked so invariant (ii) holds, we just cannot attribute a
       * reservation unit to it. */
      group_t *g = gq_head(&c->groups[tenant]);
      if (g) {
        c->resv_bytes -= g->per_page;
        T->consumed += g->per_page;
        c->pg_resv[lpn] = g->per_page;
        g->n_remaining--;
        if (g->n_remaining == 0) recompute_resv(c);
        c->st_g[CONTRACT_STAT_RESV_BYTES] =
            (uint64_t)(c->resv_bytes > 0 ? c->resv_bytes : 0);
      }
    }
  }
  if (ps != 1) {                        /* new or was spilled */
    clk_insert(c, tenant, cls, (uint32_t)lpn);
    c->pg_state[lpn] = 1;
  }
  c->pg_ref[lpn] = 1;
  c->pg_epoch[lpn] = (uint32_t)c->epoch_now;

  /* tenant EWMA and per-tenant window of class-rounded write sizes */
  tenant_window_push(T, (uint16_t)(wr > 65535 ? 65535 : wr));
  double x = (double)wr;
  if (T->nsamples == 0) { T->mu = x; T->sigma2 = 0.0; }
  else {
    double d = x - T->mu;
    T->mu += ALPHA_TENANT * d;
    T->sigma2 = (1.0 - ALPHA_TENANT) * (T->sigma2 + ALPHA_TENANT * d * d);
  }
  T->nsamples++;

  bump(c, tenant,
       cls == CONTRACT_GUAR ? CONTRACT_STAT_WRITTEN_GUAR
                            : CONTRACT_STAT_WRITTEN_OPP, 1);
  /* TRANSPARENT (Compresso-style): capacity is hidden behind a device-held
   * reserve and software has no say, so the first write that pushes physical
   * use past the reserve boundary is the machine check -- charged to whoever
   * happens to be writing, which is the failure mode the contract exists to
   * replace. The model COUNTS the event and keeps running so the experiment can
   * measure the whole distribution of who would have been killed and when; real
   * hardware stops at the first one. */
  if (c->no_ladder && phys_used(c) > c->B_total)
    bump(c, tenant, CONTRACT_STAT_HARD_FAILURES, 1);
  run_ladder(c, 0);
  pthread_mutex_unlock(&c->mx);
  return 0;
}

int contract_read(contract_t *c, uint64_t lpn, uint8_t *dst) {
  if (!c || !dst || lpn >= c->max_pages) return CONTRACT_EINVAL;
  pthread_mutex_lock(&c->mx);
  if (c->pg_state[lpn] == 0) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;
  }
  op_tick(c);
  uint8_t was_spilled = (c->pg_state[lpn] == 2);
  int tenant = c->pg_tenant[lpn], cls = c->pg_cls[lpn];
  int r = ctier_read(c->tier, lpn, dst);
  int attempts = 0;
  while (r == CTIER_ENOSPACE && attempts < LADDER_RETRIES) {
    attempts++;
    run_ladder(c, attempts >= 2 ? 2 : 1);
    r = ctier_read(c->tier, lpn, dst);
  }
  if (r == CTIER_ENOSPACE) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_ENOSPACE;
  }
  if (r < 0) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;
  }
  if (was_spilled) {                    /* promoted back into the pool */
    c->ten[tenant].realized += (int64_t)c->pg_cost[lpn];
    clk_insert(c, tenant, cls, (uint32_t)lpn);
    c->pg_state[lpn] = 1;
    run_ladder(c, 0);                   /* promote grew phys_used */
  }
  c->pg_ref[lpn] = 1;
  c->pg_epoch[lpn] = (uint32_t)c->epoch_now;
  refresh_pressure(c);
  pthread_mutex_unlock(&c->mx);
  return 0;
}

int contract_free(contract_t *c, int tenant, uint64_t lpn) {
  if (!c || tenant < 0 || tenant >= c->ntenants || lpn >= c->max_pages)
    return CONTRACT_EINVAL;
  pthread_mutex_lock(&c->mx);
  uint8_t ps = c->pg_state[lpn];
  if (ps == 0 || c->pg_tenant[lpn] != (uint8_t)tenant) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;
  }
  op_tick(c);
  int b = ctier_free_page(c->tier, lpn);
  if (b < 0) {
    pthread_mutex_unlock(&c->mx);
    return CONTRACT_EINVAL;
  }
  if (ps == 1) {
    c->ten[tenant].realized -= (int64_t)c->pg_cost[lpn];
    clk_remove(c, tenant, c->pg_cls[lpn], (uint32_t)lpn);
  }
  c->ten[tenant].owned -= (int64_t)c->pg_cost[lpn];
  pay_reprice_debt(c, tenant, c->pg_cost[lpn]);
  /* Return the reservation this page consumed. Without this, `consumed` only
   * ever grows while `realized` tracks live pages, so overdraft drifts
   * unboundedly negative for any tenant that churns pages -- and a tenant can
   * BANK that negative overdraft on purpose by writing and freeing cheap
   * pages, buying itself immunity from a victim selector that prefers positive
   * overdraft. (Found by E9; E5 never freed a page, so it never showed.)
   * Overdraft is meant to be a statement about the pages a tenant holds NOW:
   * realized bytes of its live pages minus the reservation backing them. */
  if (c->pg_cls[lpn] == CONTRACT_GUAR && c->pg_resv[lpn]) {
    c->ten[tenant].consumed -= (double)c->pg_resv[lpn];
    if (c->ten[tenant].consumed < 0) c->ten[tenant].consumed = 0;
  }
  c->pg_resv[lpn] = 0;
  c->pg_state[lpn] = 0;
  c->pg_cost[lpn] = 0;
  c->pg_ref[lpn] = 0;
  c->pages_tracked--;
  refresh_pressure(c);
  pthread_mutex_unlock(&c->mx);
  return 0;
}

int contract_register_shed(contract_t *c, int tenant, contract_shed_cb cb,
                           void *arg) {
  if (!c || tenant < 0 || tenant >= c->ntenants) return CONTRACT_EINVAL;
  pthread_mutex_lock(&c->mx);
  c->ten[tenant].cb = cb;
  c->ten[tenant].cb_arg = arg;
  pthread_mutex_unlock(&c->mx);
  return 0;
}

int contract_check_invariant(contract_t *c) {
  if (!c) return CONTRACT_EINVAL;
  pthread_mutex_lock(&c->mx);
  int bad = 0;
  /* (i) resv_bytes == sum over open groups */
  double s = 0;
  for (int t = 0; t < c->ntenants; t++) {
    gq_t *q = &c->groups[t];
    for (uint32_t i = 0; i < q->count; i++) {
      group_t *g = &q->g[(q->head + i) % q->cap];
      s += (double)g->n_remaining * g->per_page;
    }
  }
  if (fabs(s - c->resv_bytes) > 1e-6 + 1e-9 * fabs(s)) bad |= 1;
  /* (ii) sum(realized) == tier PHYS_USED (assumes all tier writes flow
   * through this contract instance) */
  int64_t sr = 0;
  for (int t = 0; t < c->ntenants; t++) sr += c->ten[t].realized;
  if (sr < 0 || (uint64_t)sr != phys_used(c)) bad |= 2;
  /* (iii) pressure level consistent with current utilization */
  double u = (double)phys_used(c) / (double)c->B_total;
  uint64_t lvl = u > WM_L3 ? 3 : u > WM_L2 ? 2 : u > WM_L1 ? 1 : 0;
  if (c->st_g[CONTRACT_STAT_PRESSURE_LEVEL] != lvl) bad |= 4;
  pthread_mutex_unlock(&c->mx);
  return bad;
}

uint64_t contract_stat_u64(contract_t *c, int tenant, int stat_id) {
  if (!c || stat_id < 0 || stat_id >= CONTRACT_N_U64_STATS) return 0;
  pthread_mutex_lock(&c->mx);
  uint64_t v;
  if (stat_id == CONTRACT_STAT_RESV_BYTES)
    v = (uint64_t)(c->resv_bytes > 0 ? c->resv_bytes : 0);
  else if (stat_id == CONTRACT_STAT_EPOCH)
    v = c->epoch_now;
  else if ((stat_id >= CONTRACT_STAT_RECOVERY_NS &&
            stat_id <= CONTRACT_STAT_RESV_BYTES) ||
           stat_id >= CONTRACT_STAT_REPRICE_EVENTS)
    v = c->st_g[stat_id];               /* global-only ids 10..15 and 19..21 */
  else if (tenant >= 0 && tenant < c->ntenants)
    v = c->st_t[tenant][stat_id];
  else
    v = c->st_g[stat_id];
  pthread_mutex_unlock(&c->mx);
  return v;
}

double contract_stat_f64(contract_t *c, int tenant, int stat_id) {
  if (!c) return 0.0;
  pthread_mutex_lock(&c->mx);
  double v = 0.0;
  double mu, sg;
  switch (stat_id) {
  case CONTRACT_STAT_CQ_GUAR_PAGES:
    v = (double)capacity_pages_locked(c, tenant, CONTRACT_GUAR);
    break;
  case CONTRACT_STAT_MU_TENANT:
    eff_musigma(c, tenant, &mu, &sg);
    v = mu;
    break;
  case CONTRACT_STAT_SIGMA_TENANT:
    eff_musigma(c, tenant, &mu, &sg);
    v = sg;
    break;
  case CONTRACT_STAT_OVERDRAFT_BYTES:
    if (tenant >= 0 && tenant < c->ntenants) v = overdraft(c, tenant);
    else
      for (int t = 0; t < c->ntenants; t++) v += overdraft(c, t);
    break;
  case CONTRACT_STAT_REPRICE_DEBT_BYTES:
    if (tenant >= 0 && tenant < c->ntenants)
      v = c->ten[tenant].reprice_debt;
    else
      for (int t = 0; t < c->ntenants; t++) v += c->ten[t].reprice_debt;
    break;
  default:
    v = 0.0;
  }
  pthread_mutex_unlock(&c->mx);
  return v;
}
