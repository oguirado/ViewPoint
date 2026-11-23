/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef VP_TORO_BULK_V1_H
#define VP_TORO_BULK_V1_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define VP_BULK_ABI_V1 UINT32_C(1)

enum vp_bulk_kind_v1 {
    VP_BULK_BYTES = 1,
    VP_BULK_FRAME = 2,
    VP_BULK_END = 3
};

enum vp_bulk_delivery_v1 {
    VP_BULK_ORDERED = 1,
    VP_BULK_LATEST = 2
};

enum vp_bulk_intent_flags_v1 {
    VP_BULK_HAS_PRIORITY = 1u << 0,
    VP_BULK_HAS_EXPECTED_ORDER = 1u << 1
};

enum vp_bulk_status_v1 {
    VP_BULK_OK = 0,
    VP_BULK_AGAIN = 1,
    VP_BULK_INVALID = -1,
    VP_BULK_DEADLINE = -2,
    VP_BULK_BROKEN = -3,
    VP_BULK_STALE = -4,
    VP_BULK_ALREADY_RELEASED = -5,
    VP_BULK_ABI_MISMATCH = -6
};

typedef struct vp_bulk_intent_v1 {
    uint64_t consumer_id;
    uint64_t deadline_monotonic_ns;
    uint64_t affinity_group;
    uint64_t expected_order;
    int32_t priority;
    uint32_t flags;
} vp_bulk_intent_v1;

typedef struct vp_bulk_lease_v1 {
    uint64_t session_id;
    uint64_t lane_id;
    uint64_t generation;
    uint64_t reference;
    uint64_t epoch;
} vp_bulk_lease_v1;

typedef struct vp_bulk_block_v1 {
    vp_bulk_lease_v1 lease;
    uint64_t offset;
    uint64_t length;
    uint64_t skipped;
    uint32_t kind;
    uint32_t width;
    uint32_t height;
    uint32_t row_stride;
    uint32_t format_id;
    int32_t fd;
} vp_bulk_block_v1;

typedef struct vp_bulk_view_v1 {
    const uint8_t *data;
    uint64_t length;
} vp_bulk_view_v1;

/* Calling and threading contract:
 * The provider keeps context live and supports concurrent calls (Rust: Sync),
 * including break_and_drain racing with dispatch or a live view. The caller
 * serializes release of a lease against its cpu_view calls and joins/drops all
 * local users of its pointers and borrowed fds before release. Outputs are
 * exclusively writable, properly aligned and disjoint from inputs, each
 * other, and provider storage. Input structs/arrays are readable, aligned and
 * stable for the call. Null required arguments are INVALID; a block array may
 * be NULL only when its count/capacity is zero. An accessible count output is
 * zeroed on error; an accessible cpu_view output is {NULL, 0} on error.
 * Non-null pointer validity and non-aliasing are caller preconditions.
 * No unwinding across any C entry point, including bootstrap. Rust catches
 * unwinding panics and returns BROKEN, fences the affected session (all
 * sessions of the context if unattributable), rolls back uncommitted custody,
 * and preserves outstanding views and cleanup. Commit and output publication
 * must be non-panicking. A committed release returns OK and is never repeated.
 * A bootstrap panic leaves the output table zeroed and is a hard failure.
 */
typedef struct vp_bulk_api_v1 {
    uint32_t abi_version;
    uint32_t struct_size;
    void *context;
    int32_t (*publish_batch)(void *context, uint64_t lane_id,
                             const vp_bulk_intent_v1 *intent,
                             const vp_bulk_block_v1 *blocks, uint32_t count,
                             uint32_t *consumed);
    int32_t (*borrow_batch)(void *context, uint64_t lane_id,
                            const vp_bulk_intent_v1 *intent,
                            uint32_t delivery, vp_bulk_block_v1 *blocks,
                            uint32_t capacity, uint32_t *produced);
    int32_t (*cpu_view)(void *context, const vp_bulk_lease_v1 *lease,
                        vp_bulk_view_v1 *view);
    int32_t (*release)(void *context, const vp_bulk_lease_v1 *lease);
    int32_t (*break_and_drain)(void *context, uint64_t session_id);
} vp_bulk_api_v1;

/* Publication and borrowing:
 * publish_batch accepts BYTES/FRAME only with a nonnegative fd for a sealed
 * memfd, even for an empty BYTES block. FRAME must be nonempty.
 * END is the only fd == -1 block:
 * no payload, zero offset and length, no fd to close. The input array and
 * original fds are borrowed for the call. The publisher owns and closes each
 * original fd on every status. Before committing a consumed block, the
 * provider takes its own close-on-exec duplicate, validating that duplicate.
 * No implicit copy, raw-pointer publication, or reserve entry point in v1.
 * Callers keep their originals open and unchanged through the call.
 * OK consumes the entire offered batch (including empty); AGAIN consumes
 * exactly the reported proper prefix, possibly empty. A suffix remains retryable
 * and the provider keeps no duplicates for it. On INVALID, DEADLINE, BROKEN
 * or STALE, no publication is committed and the provider closes all provisional
 * duplicates. ALREADY_RELEASED is release-only; ABI_MISMATCH is bootstrap-only.
 * INVALID means malformed input, DEADLINE means expired before commit, STALE
 * means the requested epoch is no longer current, and BROKEN means the session
 * is fenced or a provider failure has fenced it.
 * Any negative batch status commits no blocks or leases and reports zero.
 * borrow_batch returns OK with its exact nonzero produced count (or zero when
 * capacity is zero), or AGAIN with zero when no block is available. On a
 * negative status, no descriptors/leases are delivered and the provider reclaims
 * provisional borrow resources. A returned block fd is borrowed from the
 * provider through explicit release. Borrowers never close the borrowed fd.
 * The provider closes its owned fds when their last queue/lease owner settles.
 * Negative errors must be detected before committing any part of a batch;
 * ordinary capacity backpressure uses AGAIN for publish and never a negative.
 */

/* View and drain ordering:
 * Before any memfd mapping, verify F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW,
 * checked offset plus length <= INTPTR_MAX, fd extent equal to that sum and
 * valid checked geometry. The backing extent is the whole readable object.
 * Map read-only. A nonempty cpu_view refers to initialized, immutable bytes,
 * within one allocation, with length <= INTPTR_MAX and no address wrap. Empty
 * BYTES views may have data == NULL. Rust must return an empty slice for zero
 * length without calling from_raw_parts on NULL. END has no CPU view (INVALID).
 * In break_and_drain the break fence linearizes against publish_batch,
 * borrow_batch and cpu_view. New commits/views after it return BROKEN.
 * Views already issued remain readable until their exact lease is released.
 * break_and_drain is nonblocking and idempotent: AGAIN while live leases or
 * attributable work/custody remain, OK only when settlement is complete.
 * It never invalidates or reclaims a live lease. release stays callable after
 * break/deadline for the exact outstanding lease and reclaims its borrow once.
 * Release of that lease again is ALREADY_RELEASED while its session is tracked;
 * unknown or retired identities are STALE and cannot affect current leases.
 * Context/table code must outlive all calls, releases and drain settlement.
 */

/* The sole v1 bootstrap symbol. The loader verifies the pinned provider before
 * loading and resolves this exact symbol. Pass all sizes in bytes and a writable,
 * aligned, zero-initialized caller-owned table. The provider checks the ABI
 * version and all sizes before writing anything to the table. Bootstrap neither
 * opens an underlying session nor takes payload custody. A mismatch returns
 * ABI_MISMATCH and leaves the output untouched. With matching sizes, NULL out is
 * INVALID. On other failure the table stays zeroed. On OK it contains the
 * provider's version, size, non-null stable context and all five callbacks.
 * Check the status and validate the resulting table before any callback or open.
 * Missing symbol or failed validation is an ABI mismatch, with no fallback.
 */
int32_t vp_bulk_get_api_v1(uint32_t abi_version, uint32_t api_struct_size,
                           uint32_t intent_struct_size, uint32_t lease_struct_size,
                           uint32_t block_struct_size, uint32_t view_struct_size,
                           vp_bulk_api_v1 *out);

/* api points to the full caller-owned output table of the bootstrap. */
static inline int32_t vp_bulk_validate_api_v1(const vp_bulk_api_v1 *api)
{
    if (api == NULL || api->abi_version != VP_BULK_ABI_V1 ||
        api->struct_size != sizeof(*api) || api->context == NULL ||
        api->publish_batch == NULL || api->borrow_batch == NULL ||
        api->cpu_view == NULL || api->release == NULL || api->break_and_drain == NULL)
        return VP_BULK_ABI_MISMATCH;
    return VP_BULK_OK;
}

#ifdef __cplusplus
}
#endif
#endif
