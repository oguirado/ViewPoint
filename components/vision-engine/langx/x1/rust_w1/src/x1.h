#ifndef LANGX_X1_RUST_W1_H
#define LANGX_X1_RUST_W1_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define X1_SUCCESS 0
#define X1_ERR_DIM -1
#define X1_ERR_STRIDE -2
#define X1_ERR_SHORT -3
#define X1_ERR_RECT -4
#define X1_ERR_GRID -5

typedef struct {
    int32_t x;
    int32_t y;
    int32_t w;
    int32_t h;
} x1_rect;

int32_t x1_repack(
    const uint8_t *src_ptr,
    size_t src_len,
    int32_t w,
    int32_t h,
    size_t src_stride,
    uint8_t *dst_ptr,
    size_t dst_len,
    size_t dst_stride
);

int32_t x1_damage(
    int32_t w,
    int32_t h,
    int32_t s,
    const x1_rect *rects_ptr,
    size_t num_rects,
    uint64_t *words_out,
    size_t words_cap,
    size_t *words_written_out,
    size_t *popcount_out
);

#ifdef __cplusplus
}
#endif

#endif /* LANGX_X1_RUST_W1_H */
