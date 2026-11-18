/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include "../../../contracts/vp-toro-1/v1/bulk_v1.h"

#include <assert.h>
#include <fcntl.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int32_t bootstrap(vp_bulk_api_v1 *api)
{
    return vp_bulk_get_api_v1(VP_BULK_ABI_V1, sizeof(*api),
                              sizeof(vp_bulk_intent_v1), sizeof(vp_bulk_lease_v1),
                              sizeof(vp_bulk_block_v1), sizeof(vp_bulk_view_v1), api);
}

static uint64_t deadline(void)
{
    struct timespec now;
    assert(clock_gettime(CLOCK_MONOTONIC, &now) == 0);
    return (uint64_t)(now.tv_sec + 10) * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}

int main(void)
{
    const uint32_t pinned[] = {
        VP_BULK_ABI_V1, sizeof(vp_bulk_api_v1), sizeof(vp_bulk_intent_v1),
        sizeof(vp_bulk_lease_v1), sizeof(vp_bulk_block_v1), sizeof(vp_bulk_view_v1),
    };
    vp_bulk_api_v1 api = {0};
    for (size_t field = 0; field < sizeof(pinned) / sizeof(*pinned); ++field) {
        uint32_t altered[sizeof(pinned) / sizeof(*pinned)];
        memcpy(altered, pinned, sizeof(altered));
        ++altered[field];
        unsigned char marker[sizeof(api)];
        memset(marker, 0x79, sizeof(marker));
        memcpy(&api, marker, sizeof(api));
        assert(vp_bulk_get_api_v1(altered[0], altered[1], altered[2], altered[3], altered[4],
                                  altered[5], &api) == VP_BULK_ABI_MISMATCH);
        assert(memcmp(&api, marker, sizeof(api)) == 0);
    }
    api = (vp_bulk_api_v1){0};
    assert(bootstrap(NULL) == VP_BULK_INVALID);
    assert(bootstrap(&api) == VP_BULK_OK);
    assert(vp_bulk_validate_api_v1(&api) == VP_BULK_OK);
    vp_bulk_api_v1 altered = api;
    altered.struct_size++;
    assert(vp_bulk_validate_api_v1(&altered) == VP_BULK_ABI_MISMATCH);
    altered = api;
    altered.cpu_view = NULL;
    assert(vp_bulk_validate_api_v1(&altered) == VP_BULK_ABI_MISMATCH);

    uint32_t count = 15;
    vp_bulk_intent_v1 intent = { 71, deadline(), 12, 0, 0, VP_BULK_HAS_PRIORITY };
    vp_bulk_block_v1 block = { .lease = {1, 2, 3, 4, 5}, .length = 3, .kind = VP_BULK_BYTES };
    vp_bulk_block_v1 out = {.fd = -42};
    vp_bulk_view_v1 view = {(const uint8_t *)"canary", 6};
    pid_t child = fork();
    assert(child >= 0);
    if (child == 0) {
        if (api.borrow_batch(api.context, 2, &intent, VP_BULK_ORDERED, &out, 1, &count) != VP_BULK_BROKEN ||
            count != 0 || out.fd != -42 ||
            api.cpu_view(api.context, &block.lease, &view) != VP_BULK_BROKEN ||
            view.data != NULL || view.length != 0)
            _exit(1);
        _exit(0);
    }
    int disposition;
    assert(waitpid(child, &disposition, 0) == child && WIFEXITED(disposition) && WEXITSTATUS(disposition) == 0);

    assert(api.publish_batch(NULL, 2, &intent, &block, 1, &count) == VP_BULK_INVALID && count == 0);
    count = 4;
    assert(api.publish_batch(api.context, 2, &intent, NULL, 1, &count) == VP_BULK_INVALID && count == 0);
    count = 4;
    assert(api.borrow_batch(api.context, 2, &intent, VP_BULK_ORDERED, NULL, 1, &count) ==
           VP_BULK_INVALID && count == 0);
    assert(api.cpu_view(api.context, NULL, &view) == VP_BULK_INVALID);
    assert(view.data == NULL && view.length == 0);
    assert(api.release(api.context, NULL) == VP_BULK_INVALID);

    block.fd = memfd_create("vp-abi-probe", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    assert(block.fd >= 0);
    assert(write(block.fd, "xyz", 3) == 3);
    assert(fcntl(block.fd, F_ADD_SEALS, F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW) == 0);
    assert(api.publish_batch(api.context, 2, &intent, &block, 1, &count) == VP_BULK_OK && count == 1);
    assert(close(block.fd) == 0);
    assert(api.borrow_batch(api.context, 2, &intent, VP_BULK_ORDERED, &out, 1, &count) ==
           VP_BULK_OK && count == 1);
    assert(fcntl(out.fd, F_GETFD) == FD_CLOEXEC);
    assert(api.cpu_view(api.context, &out.lease, &view) == VP_BULK_OK);
    assert(api.break_and_drain(api.context, 1) == VP_BULK_AGAIN);
    assert(view.length == 3 && memcmp(view.data, "xyz", 3) == 0);
    assert(api.release(api.context, &out.lease) == VP_BULK_OK);
    assert(api.release(api.context, &out.lease) == VP_BULK_ALREADY_RELEASED);
    assert(fcntl(out.fd, F_GETFD) == -1);
    assert(api.break_and_drain(api.context, 1) == VP_BULK_OK);
    return 0;
}
