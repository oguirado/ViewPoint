#ifndef LANGX_X1_H
#define LANGX_X1_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * C ABI for LANG-X X1 repack and damage functions.
 * Returns 0 on success, or a negative error code on failure.
 *
 * Error codes:
 *  -1: E_DIM   (Dimensions outside valid range)
 *  -2: E_STRIDE(Stride below minimum required)
 *  -3: E_SHORT (Buffer length too short)
 *  -4: E_RECT  (Invalid rectangle with negative width or height)
 *  -5: E_GRID  (Tile size s is not a power of two in [1, 256])
 */

int32_t x1_repack(
    const uint8_t *src,
    size_t src_len,
    uint32_t w,
    uint32_t h,
    size_t src_stride,
    uint8_t *dst,
    size_t dst_len,
    size_t dst_stride
);

int32_t x1_damage(
    uint32_t w,
    uint32_t h,
    uint32_t s,
    const int32_t *rects,
    size_t rect_count,
    uint64_t *out_words,
    size_t out_words_len,
    uint64_t *out_popcount
);

#ifdef __cplusplus
}
#endif

#endif /* LANGX_X1_H */
