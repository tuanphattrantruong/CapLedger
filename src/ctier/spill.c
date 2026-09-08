/* spill.c — far-tier stand-in: a plain file of 4608-byte slots.
 * Slot layout: {u32 lpn; u16 comp_size (0 => payload is raw 4096); u16 flags}
 * followed by the payload. Free slots are kept on an in-memory stack; the file
 * grows via ftruncate in 256-slot steps. All calls are serialized by the
 * spill leaf mutex.
 */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/stat.h>
#include "internal.h"

typedef struct {
  uint32_t lpn;
  uint16_t comp_size;
  uint16_t flags;
} slot_hdr_t;

#define SLOT_HDR_BYTES 8u

/* best-effort mkdir -p for the parent directories of path */
static void mkdirs_for(const char *path) {
  char buf[1024];
  size_t n = strlen(path);
  if (n >= sizeof(buf)) return;
  memcpy(buf, path, n + 1);
  for (size_t i = 1; i < n; i++) {
    if (buf[i] == '/') {
      buf[i] = '\0';
      mkdir(buf, 0755);            /* EEXIST is fine */
      buf[i] = '/';
    }
  }
}

int ct_spill_init(spill_t *s, const char *path) {
  memset(s, 0, sizeof(*s));
  if (pthread_mutex_init(&s->mx, NULL) != 0) return -1;
  s->path = strdup(path);
  if (!s->path) { pthread_mutex_destroy(&s->mx); return -1; }
  mkdirs_for(path);
  s->fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (s->fd < 0) {
    free(s->path); s->path = NULL;
    pthread_mutex_destroy(&s->mx);
    return -1;
  }
  s->nslots = 0;
  s->free_stack = NULL;
  s->free_top = 0;
  s->free_cap = 0;
  return 0;
}

void ct_spill_destroy(spill_t *s) {
  if (s->fd >= 0) close(s->fd);
  if (s->path) { unlink(s->path); free(s->path); }
  free(s->free_stack);
  s->free_stack = NULL;
  s->path = NULL;
  s->fd = -1;
  pthread_mutex_destroy(&s->mx);
}

/* grow by SPILL_GROW_SLOTS; caller holds s->mx. meta_delta accumulates the
 * change in bookkeeping bytes. */
static int spill_grow(spill_t *s, int64_t *meta_delta) {
  uint32_t new_n = s->nslots + SPILL_GROW_SLOTS;
  if (ftruncate(s->fd, (off_t)new_n * SPILL_SLOT_BYTES) != 0) return -1;
  /* the stack must be able to hold EVERY slot simultaneously (all released) */
  if (new_n > s->free_cap) {
    uint32_t nc = s->free_cap ? s->free_cap * 2 : SPILL_GROW_SLOTS;
    while (nc < new_n) nc *= 2;
    uint32_t *ns = realloc(s->free_stack, (size_t)nc * sizeof(uint32_t));
    if (!ns) return -1;
    *meta_delta += ((int64_t)nc - (int64_t)s->free_cap) * (int64_t)sizeof(uint32_t);
    s->free_stack = ns;
    s->free_cap = nc;
  }
  /* push new slots in reverse so the lowest index pops first */
  for (uint32_t i = new_n; i > s->nslots; i--)
    s->free_stack[s->free_top++] = i - 1;
  s->nslots = new_n;
  return 0;
}

int64_t ct_spill_put(spill_t *s, uint32_t lpn, uint16_t comp_size,
                     const uint8_t *payload, uint32_t payload_len,
                     uint64_t *bytes_io, int64_t *meta_delta) {
  uint8_t buf[SPILL_SLOT_BYTES];
  if (payload_len > PAGE_SIZE) return CTIER_EINVAL;
  pthread_mutex_lock(&s->mx);
  if (s->free_top == 0) {
    if (spill_grow(s, meta_delta) != 0) {
      pthread_mutex_unlock(&s->mx);
      return CTIER_EIO;
    }
  }
  uint32_t slot = s->free_stack[--s->free_top];
  slot_hdr_t h = { lpn, comp_size, 0 };
  memcpy(buf, &h, SLOT_HDR_BYTES);
  memcpy(buf + SLOT_HDR_BYTES, payload, payload_len);
  size_t towrite = SLOT_HDR_BYTES + payload_len;
  ssize_t w = pwrite(s->fd, buf, towrite, (off_t)slot * SPILL_SLOT_BYTES);
  if (w != (ssize_t)towrite) {
    s->free_stack[s->free_top++] = slot;   /* roll back */
    pthread_mutex_unlock(&s->mx);
    return CTIER_EIO;
  }
  *bytes_io = towrite;
  pthread_mutex_unlock(&s->mx);
  return (int64_t)slot;
}

int64_t ct_spill_get(spill_t *s, uint32_t slot, uint32_t lpn_expect,
                     uint16_t *comp_size_out, uint8_t *payload,
                     uint64_t *bytes_io) {
  uint8_t buf[SPILL_SLOT_BYTES];
  pthread_mutex_lock(&s->mx);
  if (slot >= s->nslots) { pthread_mutex_unlock(&s->mx); return CTIER_EINVAL; }
  ssize_t r = pread(s->fd, buf, SPILL_SLOT_BYTES, (off_t)slot * SPILL_SLOT_BYTES);
  pthread_mutex_unlock(&s->mx);
  if (r < (ssize_t)SLOT_HDR_BYTES) return CTIER_EIO;
  slot_hdr_t h;
  memcpy(&h, buf, SLOT_HDR_BYTES);
  if (h.lpn != lpn_expect) return CTIER_ECORRUPT;
  uint32_t plen = (h.comp_size == 0) ? PAGE_SIZE : h.comp_size;
  if (plen > PAGE_SIZE || (ssize_t)(SLOT_HDR_BYTES + plen) > r) return CTIER_ECORRUPT;
  memcpy(payload, buf + SLOT_HDR_BYTES, plen);
  *comp_size_out = h.comp_size;
  *bytes_io = SLOT_HDR_BYTES + plen;
  return (int64_t)plen;
}

void ct_spill_release_slot(spill_t *s, uint32_t slot) {
  pthread_mutex_lock(&s->mx);
  if (slot < s->nslots && s->free_top < s->free_cap)
    s->free_stack[s->free_top++] = slot;
  pthread_mutex_unlock(&s->mx);
}
