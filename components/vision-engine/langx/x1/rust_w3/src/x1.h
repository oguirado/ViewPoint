#ifndef X1_H
#define X1_H

#include <stddef.h>
#include <stdint.h>

#define X1_SUCCESS 0
#define X1_E_DIM -1
#define X1_E_STRIDE -2
#define X1_E_SHORT -3
#define X1_E_GRID -4
#define X1_E_RECT -5

#ifdef __cplusplus
extern "C" {
#endif

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
    size_t num_rects,
    uint64_t *dst_words,
    size_t dst_words_len,
    uint32_t *out_popcount
);

#ifdef __cplusplus
}
#endif

#endif /* X1_H */
