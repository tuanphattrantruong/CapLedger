/* hist.c — online compressibility telemetry: decayed class histogram,
 * slow/fast EWMAs, ring buffer of recent class-rounded sizes.
 * All state guarded by the hist leaf mutex.
 *
 * Every write records one sample x = class-rounded size (0 for zero pages).
 * hist bins count only nonzero samples (bin = x/128 - 1); EWMAs and the ring
 * include zeros. Every HIST_DECAY_INTERVAL records, all bins *= 0.9.
 * EWMA: on the first sample mean := x, var := 0; afterwards
 *   d = x - mean; mean += a*d; var = (1-a)*(var + a*d*d).
 *
 * Two additions beyond the EWMAs, both O(1) per record and both expressible as
 * device accumulators (the point of R4's hardware-minimalism requirement):
 *  - exact integer running sums (win_sum, win_sumsq) over the ring window, so
 *    windowed sample moments cost O(1) rather than an O(ring_cap) scan;
 *  - an upper-sided CUSUM on the size stream, an alternative change detector
 *    to the fast/slow EWMA ratio (evaluated against it in E8).
 */
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "internal.h"

int ct_hist_init(hist_t *h, uint32_t ring_cap) {
  memset(h, 0, sizeof(*h));
  if (pthread_mutex_init(&h->mx, NULL) != 0) return -1;
  h->ring = malloc((size_t)ring_cap * sizeof(uint16_t));
  if (!h->ring) { pthread_mutex_destroy(&h->mx); return -1; }
  h->ring_cap = ring_cap;
  return 0;
}

void ct_hist_destroy(hist_t *h) {
  free(h->ring);
  h->ring = NULL;
  pthread_mutex_destroy(&h->mx);
}

void ct_hist_record(hist_t *h, uint16_t class_rounded) {
  double x = (double)class_rounded;
  pthread_mutex_lock(&h->mx);
  h->nrec++;
  if (class_rounded > 0) {
    int bin = (int)(class_rounded / 128u) - 1;
    if (bin >= 0 && bin < N_CLASSES) h->bins[bin] += 1.0;
  }
  if (h->nrec % HIST_DECAY_INTERVAL == 0)
    for (int i = 0; i < N_CLASSES; i++) h->bins[i] *= HIST_DECAY_LAMBDA;
  if (h->nsamples == 0) {
    h->slow_mean = x;
    h->slow_var = 0.0;
    h->fast_mean = x;
  } else {
    double d = x - h->slow_mean;
    h->slow_mean += ALPHA_SLOW * d;
    h->slow_var = (1.0 - ALPHA_SLOW) * (h->slow_var + ALPHA_SLOW * d * d);
    h->fast_mean += ALPHA_FAST * (x - h->fast_mean);
  }
  /* CUSUM on the just-updated slow mean (matching the executable E15 model).
   * Uses a sigma floor because a
   * near-constant stream has sigma -> 0 and would alarm on any change, and
   * does not accumulate at all until the EWMAs it references have converged --
   * during the first writes slow_var is still ~0 and every sample looks like a
   * change, which latched the alarm before any real drift existed. */
  if (h->nsamples >= CUSUM_MIN_SAMPLES) {
    double sg = h->slow_var > 0 ? sqrt(h->slow_var) : 0.0;
    if (sg < CUSUM_SIGMA_FLOOR) sg = CUSUM_SIGMA_FLOOR;
    double inc = x - h->slow_mean - CUSUM_K * sg;
    h->cusum_pos += inc;
    if (h->cusum_pos < 0) h->cusum_pos = 0;
    if (h->cusum_pos > CUSUM_H * sg) h->cusum_alarm = 1;
  }
  h->nsamples++;
  /* O(1) window moments: subtract the evicted sample, add the new one. */
  if (h->ring_count >= h->ring_cap) {
    uint64_t old = h->ring[h->ring_pos];
    h->win_sum   -= old;
    h->win_sumsq -= old * old;
  }
  h->win_sum   += (uint64_t)class_rounded;
  h->win_sumsq += (uint64_t)class_rounded * (uint64_t)class_rounded;
  h->ring[h->ring_pos] = class_rounded;
  h->ring_pos = (h->ring_pos + 1u) % h->ring_cap;
  h->ring_count++;
  pthread_mutex_unlock(&h->mx);
}

void ct_hist_read_ewma(hist_t *h, double *slow_mean, double *slow_var,
                       double *fast_mean) {
  pthread_mutex_lock(&h->mx);
  if (slow_mean) *slow_mean = h->slow_mean;
  if (slow_var)  *slow_var  = h->slow_var;
  if (fast_mean) *fast_mean = h->fast_mean;
  pthread_mutex_unlock(&h->mx);
}

int ct_hist_snapshot(hist_t *h, double out[N_CLASSES]) {
  pthread_mutex_lock(&h->mx);
  memcpy(out, h->bins, sizeof(h->bins));
  pthread_mutex_unlock(&h->mx);
  return 0;
}

/* most-recent-first: out[0] is the newest sample */
int ct_ring_snapshot(hist_t *h, uint16_t *out, int cap) {
  if (cap < 0) return CTIER_EINVAL;
  pthread_mutex_lock(&h->mx);
  uint64_t have = h->ring_count < h->ring_cap ? h->ring_count : h->ring_cap;
  int n = (uint64_t)cap < have ? cap : (int)have;
  for (int i = 0; i < n; i++) {
    uint32_t idx = (h->ring_pos + h->ring_cap - 1u - (uint32_t)i) % h->ring_cap;
    out[i] = h->ring[idx];
  }
  pthread_mutex_unlock(&h->mx);
  return n;
}

/* Windowed sample moments over the most recent samples.
 *   w == 0 or w >= ring_cap : the whole ring window, O(1) from the running
 *                             integer sums (the hot path).
 *   0 < w < ring_cap        : an O(w) scan of the newest w samples.
 * Returns the sample count actually used (0 if the ring is empty), and writes
 * the sample mean and the population standard deviation. */
int ct_ring_moments(hist_t *h, uint32_t w, double *mu, double *sigma) {
  pthread_mutex_lock(&h->mx);
  uint64_t have = h->ring_count < h->ring_cap ? h->ring_count : h->ring_cap;
  if (have == 0) {
    pthread_mutex_unlock(&h->mx);
    if (mu) *mu = 0.0;
    if (sigma) *sigma = 0.0;
    return 0;
  }
  uint64_t n, s1, s2;
  if (w == 0 || (uint64_t)w >= have) {
    n = have;
    s1 = h->win_sum;
    s2 = h->win_sumsq;
  } else {
    n = w;
    s1 = s2 = 0;
    for (uint64_t i = 0; i < n; i++) {
      uint32_t idx = (h->ring_pos + h->ring_cap - 1u - (uint32_t)i) % h->ring_cap;
      uint64_t v = h->ring[idx];
      s1 += v;
      s2 += v * v;
    }
  }
  pthread_mutex_unlock(&h->mx);
  double m = (double)s1 / (double)n;
  double var = (double)s2 / (double)n - m * m;
  if (var < 0) var = 0;              /* rounding only; sums are exact */
  if (mu) *mu = m;
  if (sigma) *sigma = sqrt(var);
  return (int)n;
}

/* Re-arm the detector. Policy calls this after acting on an alarm; without it
 * a latching detector reports one event per device lifetime. */
void ct_hist_reset_cusum(hist_t *h) {
  pthread_mutex_lock(&h->mx);
  h->cusum_pos = 0.0;
  h->cusum_alarm = 0;
  pthread_mutex_unlock(&h->mx);
}

void ct_hist_read_cusum(hist_t *h, double *pos, uint64_t *alarm) {
  pthread_mutex_lock(&h->mx);
  if (pos) *pos = h->cusum_pos;
  if (alarm) *alarm = h->cusum_alarm;
  pthread_mutex_unlock(&h->mx);
}
