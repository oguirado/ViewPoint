#ifndef LANGX_X1_H
#define LANGX_X1_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define X1_SUCCESS 0
#define X1_E_DIM -1
#define X1_E_STRIDE -2
#define X1_E_SHORT -3
#define X1_E_GRID -4
#define X1_E_RECT -5

int32_t x1_repack(
    const uint8_t *src,
    size_t src_len,
    size_t w,
    size_t h,
    size_t src_stride,
    uint8_t *dst,
    size_t dst_len,
    size_t dst_stride
);

int32_t x1_damage(
    size_t w,
    size_t h,
    size_t s,
    const int32_t *rects,
    size_t num_rects,
    uint64_t *out_words,
    size_t out_words_cap,
    size_t *out_words_len,
    uint64_t *out_popcount
);

#ifdef __cplusplus
}
#endif

#endif /* LANGX_X1_H */
