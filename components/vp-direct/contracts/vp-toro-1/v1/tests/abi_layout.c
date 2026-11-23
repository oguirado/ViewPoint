/* SPDX-License-Identifier: AGPL-3.0-only */
#include <stddef.h>
#include <assert.h>
#include "../bulk_v1.h"

#define CHECK_SIZE(type, bytes, alignment) \
    _Static_assert(sizeof(type) == (bytes) && _Alignof(type) == (alignment), #type " layout")
#define CHECK_OFFSET(type, field, offset) \
    _Static_assert(offsetof(type, field) == (offset), #type "." #field " offset")
#define CHECK_TYPE(expression, type) \
    _Static_assert(_Generic((expression), type: 1, default: 0), #expression " signature")

_Static_assert(sizeof(void *) == 8, "v1 pointer width");
_Static_assert(VP_BULK_ABI_V1 == 1, "v1 ABI version");
_Static_assert(VP_BULK_BYTES == 1 && VP_BULK_FRAME == 2 && VP_BULK_END == 3, "block tags");
_Static_assert(VP_BULK_ORDERED == 1 && VP_BULK_LATEST == 2, "delivery tags");
_Static_assert(VP_BULK_HAS_PRIORITY == 1 && VP_BULK_HAS_EXPECTED_ORDER == 2, "intent flags");
_Static_assert(VP_BULK_OK == 0 && VP_BULK_AGAIN == 1 && VP_BULK_INVALID == -1 &&
               VP_BULK_DEADLINE == -2 && VP_BULK_BROKEN == -3 &&
               VP_BULK_STALE == -4 && VP_BULK_ALREADY_RELEASED == -5 &&
               VP_BULK_ABI_MISMATCH == -6, "status values");

CHECK_SIZE(vp_bulk_intent_v1, 40, 8);
CHECK_OFFSET(vp_bulk_intent_v1, consumer_id, 0);
CHECK_OFFSET(vp_bulk_intent_v1, deadline_monotonic_ns, 8);
CHECK_OFFSET(vp_bulk_intent_v1, affinity_group, 16);
CHECK_OFFSET(vp_bulk_intent_v1, expected_order, 24);
CHECK_OFFSET(vp_bulk_intent_v1, priority, 32);
CHECK_OFFSET(vp_bulk_intent_v1, flags, 36);

CHECK_SIZE(vp_bulk_lease_v1, 40, 8);
CHECK_OFFSET(vp_bulk_lease_v1, session_id, 0);
CHECK_OFFSET(vp_bulk_lease_v1, lane_id, 8);
CHECK_OFFSET(vp_bulk_lease_v1, generation, 16);
CHECK_OFFSET(vp_bulk_lease_v1, reference, 24);
CHECK_OFFSET(vp_bulk_lease_v1, epoch, 32);

CHECK_SIZE(vp_bulk_block_v1, 88, 8);
CHECK_OFFSET(vp_bulk_block_v1, lease, 0);
CHECK_OFFSET(vp_bulk_block_v1, offset, 40);
CHECK_OFFSET(vp_bulk_block_v1, length, 48);
CHECK_OFFSET(vp_bulk_block_v1, skipped, 56);
CHECK_OFFSET(vp_bulk_block_v1, kind, 64);
CHECK_OFFSET(vp_bulk_block_v1, width, 68);
CHECK_OFFSET(vp_bulk_block_v1, height, 72);
CHECK_OFFSET(vp_bulk_block_v1, row_stride, 76);
CHECK_OFFSET(vp_bulk_block_v1, format_id, 80);
CHECK_OFFSET(vp_bulk_block_v1, fd, 84);
_Static_assert(_Generic(((vp_bulk_block_v1 *)0)->fd, int32_t: 1, default: 0), "fd type");

CHECK_SIZE(vp_bulk_view_v1, 16, 8);
CHECK_OFFSET(vp_bulk_view_v1, data, 0);
CHECK_OFFSET(vp_bulk_view_v1, length, 8);
CHECK_TYPE(((vp_bulk_view_v1 *)0)->data, const uint8_t *);
CHECK_TYPE(((vp_bulk_view_v1 *)0)->length, uint64_t);

CHECK_SIZE(vp_bulk_api_v1, 56, 8);
CHECK_OFFSET(vp_bulk_api_v1, abi_version, 0);
CHECK_OFFSET(vp_bulk_api_v1, struct_size, 4);
CHECK_OFFSET(vp_bulk_api_v1, context, 8);
CHECK_OFFSET(vp_bulk_api_v1, publish_batch, 16);
CHECK_OFFSET(vp_bulk_api_v1, borrow_batch, 24);
CHECK_OFFSET(vp_bulk_api_v1, cpu_view, 32);
CHECK_OFFSET(vp_bulk_api_v1, release, 40);
CHECK_OFFSET(vp_bulk_api_v1, break_and_drain, 48);

CHECK_TYPE(&vp_bulk_get_api_v1, int32_t (*)(uint32_t, uint32_t, uint32_t, uint32_t,
                                          uint32_t, uint32_t, vp_bulk_api_v1 *));
CHECK_TYPE(&vp_bulk_validate_api_v1, int32_t (*)(const vp_bulk_api_v1 *));
CHECK_TYPE(((vp_bulk_api_v1 *)0)->publish_batch,
           int32_t (*)(void *, uint64_t, const vp_bulk_intent_v1 *, const vp_bulk_block_v1 *,
                       uint32_t, uint32_t *));
CHECK_TYPE(((vp_bulk_api_v1 *)0)->borrow_batch,
           int32_t (*)(void *, uint64_t, const vp_bulk_intent_v1 *, uint32_t,
                       vp_bulk_block_v1 *, uint32_t, uint32_t *));
CHECK_TYPE(((vp_bulk_api_v1 *)0)->cpu_view,
           int32_t (*)(void *, const vp_bulk_lease_v1 *, vp_bulk_view_v1 *));
CHECK_TYPE(((vp_bulk_api_v1 *)0)->release, int32_t (*)(void *, const vp_bulk_lease_v1 *));
CHECK_TYPE(((vp_bulk_api_v1 *)0)->break_and_drain, int32_t (*)(void *, uint64_t));

/* Typed fixture callbacks trap any dispatch during bootstrap or table validation. */
static int32_t publish(void *context, uint64_t lane, const vp_bulk_intent_v1 *intent,
                       const vp_bulk_block_v1 *blocks, uint32_t count, uint32_t *consumed)
{
    (void)context; (void)lane; (void)intent; (void)blocks; (void)count; (void)consumed;
    assert(0);
    return VP_BULK_BROKEN;
}

static int32_t borrow(void *context, uint64_t lane, const vp_bulk_intent_v1 *intent,
                      uint32_t delivery, vp_bulk_block_v1 *blocks, uint32_t capacity,
                      uint32_t *produced)
{
    (void)context; (void)lane; (void)intent; (void)delivery;
    (void)blocks; (void)capacity; (void)produced;
    assert(0);
    return VP_BULK_BROKEN;
}

static int32_t view(void *context, const vp_bulk_lease_v1 *lease, vp_bulk_view_v1 *out)
{
    (void)context; (void)lease; (void)out;
    assert(0);
    return VP_BULK_BROKEN;
}

static int32_t release(void *context, const vp_bulk_lease_v1 *lease)
{
    (void)context; (void)lease;
    assert(0);
    return VP_BULK_BROKEN;
}

static int32_t drain(void *context, uint64_t session)
{
    (void)context; (void)session;
    assert(0);
    return VP_BULK_BROKEN;
}

int32_t vp_bulk_get_api_v1(uint32_t version, uint32_t api_size, uint32_t intent_size,
                           uint32_t lease_size, uint32_t block_size, uint32_t view_size,
                           vp_bulk_api_v1 *out)
{
    static int fixture_context;

    if (version != VP_BULK_ABI_V1 || api_size != sizeof(*out) ||
        intent_size != sizeof(vp_bulk_intent_v1) || lease_size != sizeof(vp_bulk_lease_v1) ||
        block_size != sizeof(vp_bulk_block_v1) || view_size != sizeof(vp_bulk_view_v1))
        return VP_BULK_ABI_MISMATCH;
    if (out == NULL)
        return VP_BULK_INVALID;
    *out = (vp_bulk_api_v1){VP_BULK_ABI_V1, sizeof(*out), &fixture_context,
                           publish, borrow, view, release, drain};
    return VP_BULK_OK;
}

int main(void)
{
    const uint32_t expected[] = {VP_BULK_ABI_V1, sizeof(vp_bulk_api_v1),
                                 sizeof(vp_bulk_intent_v1), sizeof(vp_bulk_lease_v1),
                                 sizeof(vp_bulk_block_v1), sizeof(vp_bulk_view_v1)};
    const size_t fields = sizeof(expected) / sizeof(expected[0]);
    vp_bulk_api_v1 api = {0};

    for (size_t field = 0; field < fields; ++field) {
        uint32_t request[sizeof(expected) / sizeof(expected[0])];
        for (size_t i = 0; i < fields; ++i)
            request[i] = expected[i];
        ++request[field];
        assert(vp_bulk_get_api_v1(request[0], request[1], request[2], request[3],
                                  request[4], request[5], &api) == VP_BULK_ABI_MISMATCH);
        assert(api.abi_version == 0 && api.struct_size == 0 && api.context == NULL &&
               api.publish_batch == NULL && api.borrow_batch == NULL &&
               api.cpu_view == NULL && api.release == NULL && api.break_and_drain == NULL);
    }
    assert(vp_bulk_get_api_v1(expected[0], expected[1], expected[2], expected[3], expected[4],
                              expected[5], NULL) == VP_BULK_INVALID);
    assert(vp_bulk_get_api_v1(expected[0], expected[1], expected[2], expected[3], expected[4],
                              expected[5], &api) == VP_BULK_OK);
    assert(vp_bulk_validate_api_v1(&api) == VP_BULK_OK);
    assert(vp_bulk_validate_api_v1(NULL) == VP_BULK_ABI_MISMATCH);

#define REJECT_TABLE(field, value) do { \
        vp_bulk_api_v1 altered = api; \
        altered.field = (value); \
        assert(vp_bulk_validate_api_v1(&altered) == VP_BULK_ABI_MISMATCH); \
    } while (0)
    REJECT_TABLE(abi_version, VP_BULK_ABI_V1 + 1);
    REJECT_TABLE(struct_size, sizeof(api) - 1);
    REJECT_TABLE(struct_size, sizeof(api) + 1);
    REJECT_TABLE(context, NULL);
    REJECT_TABLE(publish_batch, NULL);
    REJECT_TABLE(borrow_batch, NULL);
    REJECT_TABLE(cpu_view, NULL);
    REJECT_TABLE(release, NULL);
    REJECT_TABLE(break_and_drain, NULL);
#undef REJECT_TABLE
    return 0;
}
