/* contract.h — public ABI of the capacity-contract layer.
 *
 * FROZEN ABI (DESIGN.md, contract API): consumed via ctypes from Python. Do not rename
 * functions, reorder/renumber enum values, or change signatures.
 *
 * Semantics notes (binding for callers):
 *  - One recursive mutex guards all contract state. Shed callbacks run WITH the
 *    mutex held and may reenter contract_free / contract_write from the SAME
 *    thread only. Lock order is always contract -> ctier, never the inverse.
 *  - A GUARANTEED write of a page not yet tracked requires an open reservation
 *    group for that tenant (from contract_admit); otherwise CONTRACT_EINVAL.
 *    Rewrites of an already-tracked page never consume a reservation unit.
 *    OPPORTUNISTIC writes are admitted implicitly (contract_admit(OPP, n) only
 *    bumps the ADMITTED_OPP counter; it never reserves and never rejects).
 *  - A tracked page's (tenant, cls) is fixed at first write; a write or free
 *    naming a different tenant/cls for that lpn returns CONTRACT_EINVAL.
 *  - contract_read on a spilled tracked page promotes it (tier side) and
 *    re-links it into the clock lists; promote ENOSPACE triggers the ladder
 *    and is retried, else CONTRACT_ENOSPACE.
 *  - Per-tenant stats: contract_stat_u64(c, tenant, id) with tenant in
 *    [0, ntenants) returns that tenant's row for ids 0..9, 16 and 17..18;
 *    ids 10..15 (RECOVERY_NS, PRESSURE_LEVEL, LIE_FLAG, INVARIANT_BREACHES,
 *    EPOCH, RESV_BYTES) and 19..21 (REPRICE_EVENTS, REPRICE_REVOKED,
 *    CUSUM_FLAG) are global-only and returned regardless of the tenant arg.
 *    tenant = -1 returns the global row for every id.
 *  - f64 stats: CQ_GUAR_PAGES = current advertised guaranteed capacity for the
 *    tenant (tenant -1: computed from the global/effective size distribution);
 *    MU_TENANT / SIGMA_TENANT = effective per-page mean / stddev (after the
 *    cold-start fallback chain); OVERDRAFT_BYTES = owned - consumed
 *    (tenant -1: sum over tenants).
 *  - contract_check_invariant returns 0 iff all three invariants hold, else a
 *    bitmask: 1 = reservation-sum mismatch, 2 = sum(realized) != tier
 *    PHYS_USED (assumes ALL tier writes flow through this contract),
 *    4 = pressure level inconsistent with current utilization.
 */
#ifndef CONTRACT_H
#define CONTRACT_H

#include <stdint.h>
#include "ctier.h"

#ifdef __cplusplus
extern "C" {
#endif

/* capacity classes */
#define CONTRACT_GUAR 0
#define CONTRACT_OPP  1

/* estimators. 0-4 are the original arms; 5-8 were added for the external
 * baselines and the E1 estimator repair (append-only: existing values keep
 * their meaning and their numbers). */
#define CONTRACT_EST_ANALYTIC    0  /* closed form, moments from tenant EWMA   */
#define CONTRACT_EST_STATIC      1
#define CONTRACT_EST_OPTIMISTIC  2
#define CONTRACT_EST_PESSIMISTIC 3
#define CONTRACT_EST_EXTERNAL    4  /* gate always accepts; reserve = mu */
#define CONTRACT_EST_ANALYTIC_RING 5 /* closed form, moments from the ring window */
/* ZSWAP models the software-compressed-tier lineage (Linux zswap / TMO /
 * Google far memory): the kernel sees every compressed size exactly, so there
 * is no advertisement problem -- and correspondingly no admission gate, no
 * per-tenant reservation, and reclaim is reactive over a GLOBAL LRU. It always
 * survives (it can always write back), but recovery cost lands on whoever is
 * at the LRU tail rather than on the tenant whose data inflated. */
#define CONTRACT_EST_ZSWAP       6
/* TRANSPARENT models the hardware lineage (MXT ... Compresso): capacity is
 * hidden from software behind a device-held reserve, there is no OS spill path,
 * and exhausting the reserve is a fatal event (machine check) charged to
 * whichever tenant happens to write next. */
#define CONTRACT_EST_TRANSPARENT 7
/* REACTIVE_TENANT is the stronger software-tier control requested by review:
 * exact compressed-size accounting, no probabilistic admission and no
 * reservation, but tenant-aware reactive reclaim.  With reservation zero,
 * overdraft is realized owned bytes, so the largest physical owner pays
 * first.  This isolates overdraft-aware attribution from risk pricing. */
#define CONTRACT_EST_REACTIVE_TENANT 8

/* victim policies */
#define CONTRACT_VICTIM_CLOCK_ONLY  0
#define CONTRACT_VICTIM_DENSITY_AGE 1
#define CONTRACT_VICTIM_GLOBAL_LRU  2 /* ignore tenants and overdraft (ZSWAP) */

/* errors */
#define CONTRACT_EREJECT  (-10)
#define CONTRACT_EAGAIN   (-11)
#define CONTRACT_EINVAL   (-12)
#define CONTRACT_ENOSPACE (-13)

/* u64 stat ids */
enum {
  CONTRACT_STAT_ADMITTED_GUAR     = 0,
  CONTRACT_STAT_ADMITTED_OPP      = 1,
  CONTRACT_STAT_WRITTEN_GUAR      = 2,
  CONTRACT_STAT_WRITTEN_OPP       = 3,
  CONTRACT_STAT_VIOLATIONS        = 4,
  CONTRACT_STAT_R1_SPILLS         = 5,
  CONTRACT_STAT_R2_SHED_CALLS     = 6,
  CONTRACT_STAT_R2_SHED_BYTES     = 7,
  CONTRACT_STAT_R3_FORCED_SPILLS  = 8,
  CONTRACT_STAT_OPP_DROPS         = 9,
  CONTRACT_STAT_RECOVERY_NS       = 10,
  CONTRACT_STAT_PRESSURE_LEVEL    = 11,
  CONTRACT_STAT_LIE_FLAG          = 12,
  CONTRACT_STAT_INVARIANT_BREACHES= 13,
  CONTRACT_STAT_EPOCH             = 14,
  CONTRACT_STAT_RESV_BYTES        = 15,
  CONTRACT_STAT_HARD_FAILURES     = 16,
  CONTRACT_STAT_ADMIT_REJECTS     = 17,  /* gate refusals (binding-gate proof) */
  CONTRACT_STAT_ADMIT_CALLS       = 18,
  CONTRACT_STAT_REPRICE_EVENTS    = 19,  /* mark-to-market passes over groups  */
  CONTRACT_STAT_REPRICE_REVOKED   = 20,  /* unconsumed reservation pages revoked*/
  CONTRACT_STAT_CUSUM_FLAG        = 21   /* device CUSUM alarm (latched)       */
};
/* Size of the u64 stat rows. Bump this whenever an id is appended above --
 * the per-tenant rows are a fixed-size array and ids index it directly. */
#define CONTRACT_N_U64_STATS 22

/* f64 stat ids */
enum {
  CONTRACT_STAT_CQ_GUAR_PAGES   = 100,
  CONTRACT_STAT_MU_TENANT       = 101,
  CONTRACT_STAT_SIGMA_TENANT    = 102,
  CONTRACT_STAT_OVERDRAFT_BYTES = 103,
  CONTRACT_STAT_REPRICE_DEBT_BYTES = 104
};

typedef struct contract contract_t;
typedef void (*contract_shed_cb)(void *arg, uint64_t bytes_needed);

/* Extended configuration (contract_create2). contract_create maps onto this
 * with the v1 defaults, so the original 8-argument entry point keeps working
 * unchanged. Zero-initialise and set what you need. */
typedef struct {
  int      estimator;
  double   q_guaranteed;      /* <=0 or >=1 -> 0.99 */
  double   headroom_frac;     /* <0 -> 0.05; 0 is honored */
  double   static_ratio;      /* <=0 -> 2.0 */
  uint32_t epoch_ops;         /* 0 -> 1024 */
  int      ntenants;          /* <=0 -> 1, capped at 8 */
  int      victim_policy;
  /* --- v2 --- */
  uint32_t ring_window;       /* ANALYTIC_RING moment window; 0 = whole ring  */
  int      reprice;           /* 1: mark open reservation groups to market on
                               * a raised lie flag, revoking the unconsumed
                               * remainder that current prices no longer back
                               * (the history-shaping countermeasure, E9)     */
  double   reserve_frac;      /* TRANSPARENT: device-held reserve; <=0 -> 0.10 */
} contract_cfg_t;

/* Defaults when <=0 is passed: q_guaranteed 0.99, static_ratio 2.0,
 * epoch_ops 1024, ntenants 1 (max 8), headroom_frac <0 -> 0.05 (0 is honored).
 * victim_policy other than 0/1 -> DENSITY_AGE. Returns NULL on failure. */
contract_t *contract_create(ctier_t *tier, int estimator, double q_guaranteed,
                            double headroom_frac, double static_ratio,
                            uint32_t epoch_ops, int ntenants, int victim_policy);
contract_t *contract_create2(ctier_t *tier, const contract_cfg_t *cfg);
/* sizeof(contract_cfg_t), so the ctypes mirror can assert layout agreement. */
uint64_t contract_cfg_size(void);
void    contract_destroy(contract_t *c);   /* does NOT destroy the tier */
int64_t contract_capacity_pages(contract_t *c, int tenant, int cls);
int     contract_admit(contract_t *c, int tenant, int cls, uint64_t npages);
int     contract_write(contract_t *c, int tenant, int cls, uint64_t lpn,
                       const uint8_t *src4k);
int     contract_read (contract_t *c, uint64_t lpn, uint8_t *dst4k);
int     contract_free (contract_t *c, int tenant, uint64_t lpn);
int     contract_register_shed(contract_t *c, int tenant,
                               contract_shed_cb cb, void *arg);
int     contract_check_invariant(contract_t *c);   /* 0 = holds */
uint64_t contract_stat_u64(contract_t *c, int tenant, int stat_id);
double   contract_stat_f64(contract_t *c, int tenant, int stat_id);

/* Acklam inverse normal CDF, |abs err| < 1.2e-9. p<=0 -> -HUGE_VAL,
 * p>=1 -> +HUGE_VAL. (norminv.c; exported for the Python side to cross-check.) */
double norminv(double p);

#ifdef __cplusplus
}
#endif
#endif /* CONTRACT_H */
