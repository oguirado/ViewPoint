#ifndef X1_H
#define X1_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Error codes returned by C ABI functions */
#define X1_SUCCESS     0
#define X1_ERR_DIM    -1
#define X1_ERR_STRIDE -2
#define X1_ERR_SHORT  -3
#define X1_ERR_RECT   -4
#define X1_ERR_GRID   -5

/**
 * Kernel K1: Repack BGRX32 to RGB24 with stride.
 *
 * @param src Pointer to source BGRX buffer.
 * @param src_len Length of src buffer in bytes.
 * @param w Image width in pixels (0 to 16384).
 * @param h Image height in pixels (0 to 16384).
 * @param src_stride Bytes per row in src (>= 4 * w).
 * @param dst Pointer to destination RGB buffer.
 * @param dst_len Length of dst buffer in bytes.
 * @param dst_stride Bytes per row in dst (>= 3 * w).
 * @return 0 on success, or a negative error code.
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

/**
 * Kernel K2: Damage rectangles to tile bitset.
 *
 * @param w Screen width in pixels (1 to 16384).
 * @param h Screen height in pixels (1 to 16384).
 * @param s Tile size in pixels (power of two from 1 to 256).
 * @param rects Pointer to array of int32_t[4] representing (x, y, w, h).
 * @param num_rects Number of rectangles in rects.
 * @param out_words Pointer to destination buffer for uint64_t tile words.
 * @param max_words Capacity of out_words in uint64_t elements.
 * @param out_num_words Pointer to store total number of tile words generated.
 * @param out_popcount Pointer to store total popcount (number of set bits).
 * @return 0 on success, or a negative error code.
 */
int32_t x1_damage(
    uint32_t w,
    uint32_t h,
    uint32_t s,
    const int32_t *rects,
    size_t num_rects,
    uint64_t *out_words,
    size_t max_words,
    size_t *out_num_words,
    uint64_t *out_popcount
);

#ifdef __cplusplus
}
#endif

#endif /* X1_H */
