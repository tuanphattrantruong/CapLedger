/* feedback.h -- protocol-faithful host/device completion and placeability API.
 *
 * A completion is exactly 16 bytes: an opaque host token, a monotonically
 * increasing sequence, an allocation generation, a compressed-size class,
 * and a status.
 * The controller never sees tenant identity.  The host maps token+generation
 * back to its page/tenant table and treats sequence gaps as pessimistic
 * telemetry debt until a resynchronization snapshot completes.
 */
#ifndef CTIER_FEEDBACK_H
#define CTIER_FEEDBACK_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  uint64_t token;
  uint32_t sequence;
  uint16_t generation;  /* host allocation/reuse epoch, not a write counter */
  uint8_t  size_class;  /* 0..31 */
  uint8_t  status;
} ct_feedback_record_t;

#define CT_FEEDBACK_OK       0
#define CT_FEEDBACK_ZERO     1
#define CT_FEEDBACK_RAW      2
#define CT_FEEDBACK_ENOSPACE 3
#define CT_FEEDBACK_FREE     4

typedef struct ct_feedback_queue ct_feedback_queue_t;

ct_feedback_queue_t *ct_feedback_create(uint32_t capacity);
void ct_feedback_destroy(ct_feedback_queue_t *q);
/* Returns 0 when queued, -1 on overflow. Sequence advances even on overflow,
 * making loss visible in the next delivered record. */
int ct_feedback_emit(ct_feedback_queue_t *q, uint64_t token, uint16_t generation,
                     uint8_t size_class, uint8_t status);
/* Non-blocking: 1 record, 0 empty, -1 invalid. */
int ct_feedback_poll(ct_feedback_queue_t *q, ct_feedback_record_t *out);
uint64_t ct_feedback_dropped(const ct_feedback_queue_t *q);
uint32_t ct_feedback_depth(const ct_feedback_queue_t *q);

typedef struct {
  uint32_t expected_sequence;
  uint64_t telemetry_debt_bytes;
  uint64_t gap_records;
  uint64_t stale_records;
} ct_feedback_tracker_t;

void ct_feedback_tracker_init(ct_feedback_tracker_t *t);
/* Apply ordering only. Missing completions are charged pessimistically at
 * `bytes_per_missing`; stale/duplicate records are counted and ignored. */
int ct_feedback_tracker_apply(ct_feedback_tracker_t *t,
                              const ct_feedback_record_t *record,
                              uint64_t bytes_per_missing);
void ct_feedback_tracker_resync(ct_feedback_tracker_t *t,
                                uint32_t next_sequence);

/* Class-aware admission gate. `initial_free_slots[k]` describes unused slots
 * in already assigned class-k extents; `free_extents` are still unassigned.
 * A reservation fails if the target class cannot be placed even when the
 * aggregate byte count is large. */
typedef struct ct_place_gate ct_place_gate_t;
ct_place_gate_t *ct_place_gate_create(uint64_t extent_bytes,
                                      uint32_t free_extents,
                                      const uint64_t initial_free_slots[32]);
void ct_place_gate_destroy(ct_place_gate_t *g);
int ct_place_gate_reserve(ct_place_gate_t *g, uint16_t size_class,
                          uint64_t pages);
void ct_place_gate_release(ct_place_gate_t *g, uint16_t size_class,
                           uint64_t pages);
uint64_t ct_place_gate_aggregate_free_bytes(const ct_place_gate_t *g);
uint32_t ct_place_gate_free_extents(const ct_place_gate_t *g);

#ifdef __cplusplus
}
#endif
#endif
