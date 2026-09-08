/* calib.c — E0 zlib calibration binary. Self-contained (does not use ctier):
 * measures compress2/uncompress cost and ratio per content class x zlib level
 * on this machine. Emits a single JSON object to stdout; writes no files.
 *
 * Content classes (deterministic, xoshiro256** seeded 0xC0FFEE):
 *   TEXT    slices of /usr/share/dict/words tiled to 16 MiB in memory
 *   BINARY  slices of /usr/lib/dyld tiled to 16 MiB in memory
 *   FLOAT   512 Box-Muller float64 gaussians per page
 *   FLOATQ  4096 int8-quantized gaussians (x32, clipped to [-127,127])
 *   JSON    templated log records repeated to 4096 bytes
 *   ZERO    all zeros
 *   RANDOM  raw xoshiro output (incompressible)
 *
 * Per cell: N_PAGES distinct pages, each compressed then decompressed, per-page
 * ns via mach_absolute_time. mean_ratio = mean(zlib bytes)/4096 (may exceed 1
 * for incompressible data); mean_class_rounded_ratio applies the tier's
 * accounting (all-zero page -> 0; >= 4096 -> raw 4096; else rounded up to the
 * 128-byte class). MB/s uses decimal MB (1e6 bytes).
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>
#include <mach/mach_time.h>

#define PAGE 4096
#define N_PAGES 2000
#define CORPUS_BYTES (16u << 20)
#define SEED 0xC0FFEEull

static const char *TEXT_SRC = "/usr/share/dict/words";
static const char *BIN_SRC = "/usr/lib/dyld";

/* ---------- timer ---------- */
static mach_timebase_info_data_t tb;
static uint64_t now_ns(void) { return mach_absolute_time() * tb.numer / tb.denom; }

/* ---------- xoshiro256** ---------- */
static uint64_t xs[4];
static uint64_t splitmix64(uint64_t *s) {
  uint64_t z = (*s += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}
static void xs_seed(uint64_t seed) {
  for (int i = 0; i < 4; i++) xs[i] = splitmix64(&seed);
}
static inline uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
static uint64_t xs_next(void) {
  uint64_t r = rotl(xs[1] * 5, 7) * 9;
  uint64_t t = xs[1] << 17;
  xs[2] ^= xs[0]; xs[3] ^= xs[1]; xs[1] ^= xs[2]; xs[0] ^= xs[3];
  xs[2] ^= t; xs[3] = rotl(xs[3], 45);
  return r;
}
static double xs_unit(void) { return (double)(xs_next() >> 11) / 9007199254740992.0; }

/* ---------- corpora ---------- */
static uint8_t *load_tiled(const char *path, size_t *got_bytes) {
  uint8_t *buf = malloc(CORPUS_BYTES);
  if (!buf) { fprintf(stderr, "oom corpus\n"); exit(1); }
  FILE *f = fopen(path, "rb");
  if (!f) { fprintf(stderr, "cannot open %s\n", path); exit(1); }
  size_t n = fread(buf, 1, CORPUS_BYTES, f);
  fclose(f);
  if (n == 0) { fprintf(stderr, "empty %s\n", path); exit(1); }
  *got_bytes = n;
  size_t fill = n;
  while (fill < CORPUS_BYTES) {              /* tile to 16 MiB */
    size_t chunk = n < CORPUS_BYTES - fill ? n : CORPUS_BYTES - fill;
    memcpy(buf + fill, buf, chunk);
    fill += chunk;
  }
  return buf;
}

/* ---------- page generators ---------- */
enum { C_TEXT, C_BINARY, C_FLOAT, C_FLOATQ, C_JSON, C_ZERO, C_RANDOM, N_CC };
static const char *cc_name[N_CC] = {"TEXT", "BINARY", "FLOAT", "FLOATQ",
                                    "JSON", "ZERO", "RANDOM"};
static uint8_t *corpus_text, *corpus_bin;

static void gen_page(int cc, uint8_t *b) {
  switch (cc) {
  case C_TEXT: {
    size_t off = (size_t)(xs_next() % (CORPUS_BYTES - PAGE));
    memcpy(b, corpus_text + off, PAGE);
    break;
  }
  case C_BINARY: {
    size_t off = (size_t)(xs_next() % (CORPUS_BYTES - PAGE));
    memcpy(b, corpus_bin + off, PAGE);
    break;
  }
  case C_FLOAT: {
    double *d = (double *)(void *)b;
    for (int i = 0; i < 512; i += 2) {
      double u1 = xs_unit(), u2 = xs_unit();
      if (u1 <= 0) u1 = 1e-12;
      double r = sqrt(-2.0 * log(u1));
      d[i] = r * cos(2.0 * M_PI * u2);
      d[i + 1] = r * sin(2.0 * M_PI * u2);
    }
    break;
  }
  case C_FLOATQ: {
    for (int i = 0; i < PAGE; i += 2) {
      double u1 = xs_unit(), u2 = xs_unit();
      if (u1 <= 0) u1 = 1e-12;
      double r = sqrt(-2.0 * log(u1));
      double g1 = r * cos(2.0 * M_PI * u2) * 32.0;
      double g2 = r * sin(2.0 * M_PI * u2) * 32.0;
      if (g1 > 127) g1 = 127; if (g1 < -127) g1 = -127;
      if (g2 > 127) g2 = 127; if (g2 < -127) g2 = -127;
      b[i] = (uint8_t)(int8_t)g1;
      b[i + 1] = (uint8_t)(int8_t)g2;
    }
    break;
  }
  case C_JSON: {
    size_t off = 0;
    while (off < PAGE) {
      char rec[160];
      int n = snprintf(rec, sizeof(rec),
          "{\"ts\":%llu,\"lvl\":%d,\"id\":\"%016llx\",\"msg\":\"request %llu "
          "completed in %llu us\"}\n",
          (unsigned long long)(1700000000ull + (xs_next() % 604800)),
          (int)(xs_next() % 6),
          (unsigned long long)xs_next(),
          (unsigned long long)(xs_next() % 1000000),
          (unsigned long long)(xs_next() % 50000));
      size_t l = (size_t)n;
      if (off + l > PAGE) l = PAGE - off;
      memcpy(b + off, rec, l);
      off += l;
    }
    break;
  }
  case C_ZERO:
    memset(b, 0, PAGE);
    break;
  case C_RANDOM:
  default:
    for (int i = 0; i < PAGE; i += 8) {
      uint64_t v = xs_next();
      memcpy(b + i, &v, 8);
    }
    break;
  }
}

static int is_zero_page(const uint8_t *b) {
  for (int i = 0; i < PAGE; i++)
    if (b[i]) return 0;
  return 1;
}

static int cmp_dbl(const void *a, const void *b) {
  double x = *(const double *)a, y = *(const double *)b;
  return x < y ? -1 : x > y ? 1 : 0;
}
static double pctile(double *sorted, int n, double p) {
  int idx = (int)ceil(p * n) - 1;
  if (idx < 0) idx = 0;
  if (idx >= n) idx = n - 1;
  return sorted[idx];
}

int main(void) {
  mach_timebase_info(&tb);
  size_t text_bytes = 0, bin_bytes = 0;
  corpus_text = load_tiled(TEXT_SRC, &text_bytes);
  corpus_bin = load_tiled(BIN_SRC, &bin_bytes);

  static const int levels[4] = {1, 3, 6, 9};
  static uint8_t page[PAGE], comp[16384], back[PAGE];
  static double comp_us[N_PAGES], decomp_us[N_PAGES];

  printf("{\n");
  printf("  \"bench\": \"e0-zlib-calibration\",\n");
  printf("  \"seed\": %llu,\n", (unsigned long long)SEED);
  printf("  \"prng\": \"xoshiro256**\",\n");
  printf("  \"n_per_cell\": %d,\n", N_PAGES);
  printf("  \"page_bytes\": %d,\n", PAGE);
  printf("  \"zlib_version\": \"%s\",\n", zlibVersion());
  printf("  \"levels\": [1, 3, 6, 9],\n");
  printf("  \"sources\": {\"text\": \"%s\", \"text_bytes_read\": %zu, "
         "\"binary\": \"%s\", \"binary_bytes_read\": %zu, "
         "\"tiled_to_bytes\": %u},\n",
         TEXT_SRC, text_bytes, BIN_SRC, bin_bytes, CORPUS_BYTES);
  printf("  \"mbps_definition\": \"1e6 bytes per second, over 4096-byte "
         "logical pages\",\n");
  printf("  \"table\": {\n");

  for (int cc = 0; cc < N_CC; cc++) {
    printf("    \"%s\": {\n", cc_name[cc]);
    for (int li = 0; li < 4; li++) {
      int level = levels[li];
      /* fresh deterministic stream per cell */
      xs_seed(SEED + (uint64_t)cc * 1000 + (uint64_t)level);
      double sum_ratio = 0, sum_cr_ratio = 0;
      uint64_t comp_ns_total = 0, decomp_ns_total = 0;
      int fails = 0;
      for (int i = 0; i < N_PAGES; i++) {
        gen_page(cc, page);
        uLongf clen = sizeof(comp);
        uint64_t t0 = now_ns();
        int rc = compress2(comp, &clen, page, PAGE, level);
        uint64_t t1 = now_ns();
        uLongf dlen = PAGE;
        int rd = Z_ERRNO;
        uint64_t t2 = t1, t3 = t1;
        if (rc == Z_OK) {
          t2 = now_ns();
          rd = uncompress(back, &dlen, comp, clen);
          t3 = now_ns();
        }
        if (rc != Z_OK || rd != Z_OK || dlen != PAGE ||
            memcmp(back, page, PAGE) != 0) {
          fails++;
          continue;
        }
        comp_us[i - fails] = (double)(t1 - t0) / 1000.0;
        decomp_us[i - fails] = (double)(t3 - t2) / 1000.0;
        comp_ns_total += (t1 - t0);
        decomp_ns_total += (t3 - t2);
        sum_ratio += (double)clen / PAGE;
        uint32_t cr;
        if (is_zero_page(page)) cr = 0;
        else if (clen >= PAGE) cr = PAGE;
        else cr = 128u * (uint32_t)(((uint32_t)clen - 1) / 128 + 1);
        sum_cr_ratio += (double)cr / PAGE;
      }
      int n_ok = N_PAGES - fails;
      qsort(comp_us, (size_t)n_ok, sizeof(double), cmp_dbl);
      qsort(decomp_us, (size_t)n_ok, sizeof(double), cmp_dbl);
      double bytes = (double)n_ok * PAGE;
      printf("      \"%d\": {\"mean_ratio\": %.6f, "
             "\"mean_class_rounded_ratio\": %.6f, "
             "\"comp_MBps\": %.2f, \"decomp_MBps\": %.2f, "
             "\"comp_p50_us\": %.3f, \"comp_p99_us\": %.3f, "
             "\"decomp_p50_us\": %.3f, \"decomp_p99_us\": %.3f, "
             "\"n\": %d, \"zlib_failures\": %d}%s\n",
             level,
             sum_ratio / n_ok, sum_cr_ratio / n_ok,
             bytes / 1e6 / ((double)comp_ns_total / 1e9),
             bytes / 1e6 / ((double)decomp_ns_total / 1e9),
             pctile(comp_us, n_ok, 0.50), pctile(comp_us, n_ok, 0.99),
             pctile(decomp_us, n_ok, 0.50), pctile(decomp_us, n_ok, 0.99),
             n_ok, fails,
             li < 3 ? "," : "");
    }
    printf("    }%s\n", cc < N_CC - 1 ? "," : "");
  }
  printf("  }\n}\n");
  free(corpus_text);
  free(corpus_bin);
  return 0;
}
