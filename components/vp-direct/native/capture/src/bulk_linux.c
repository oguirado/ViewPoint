/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include "../../../contracts/vp-toro-1/v1/bulk_v1.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

int vp_bulk_dup(int original)
{
    int fd;
    do {
        fd = fcntl(original, F_DUPFD_CLOEXEC, 0);
    } while (fd < 0 && errno == EINTR);
    if (fd >= 0)
        return fd;
    return errno == EBADF ? VP_BULK_INVALID : VP_BULK_BROKEN;
}

int vp_bulk_map(int fd, size_t extent, int nonempty, void **out)
{
    static const int required = F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW;
    char path[64], name[8];
    struct stat info;
    int seals = fcntl(fd, F_GET_SEALS);
    if (seals < 0 || (seals & required) != required)
        return VP_BULK_INVALID;
    snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
    if (readlink(path, name, sizeof(name)) < 7 || memcmp(name, "/memfd:", 7))
        return VP_BULK_INVALID;
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) ||
        info.st_size < 0 || (uint64_t)info.st_size != extent)
        return VP_BULK_INVALID;
    if (!nonempty)
        return VP_BULK_OK;
    void *mapped = mmap(NULL, extent, PROT_READ, MAP_SHARED, fd, 0);
    if (mapped == MAP_FAILED)
        return VP_BULK_BROKEN;
    if (!mapped || (uintptr_t)mapped > UINTPTR_MAX - extent) {
        munmap(mapped, extent);
        return VP_BULK_BROKEN;
    }
    *out = mapped;
    return VP_BULK_OK;
}

void vp_bulk_unmap(void *address, size_t length)
{
    munmap(address, length);
}

uint64_t vp_bulk_monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now))
        return UINT64_MAX;
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}
