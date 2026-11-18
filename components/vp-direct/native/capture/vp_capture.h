#ifndef VP_CAPTURE_H
#define VP_CAPTURE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct vp_capture vp_capture;

enum vp_capture_status {
    VP_CAPTURE_OK = 0,
    VP_CAPTURE_INVALID_ARGUMENT = 1,
    VP_CAPTURE_BAD_DISPLAY = 2,
    VP_CAPTURE_UNSUPPORTED_FORMAT = 3,
    VP_CAPTURE_BUFFER_TOO_SMALL = 4,
    VP_CAPTURE_X11_ERROR = 5,
    VP_CAPTURE_RESOURCE_ERROR = 6,
    VP_CAPTURE_TIMED_OUT = 7,
    VP_CAPTURE_CLOSED = 8
};

enum { VP_CAPTURE_RGB24 = 1, VP_CAPTURE_SHA256_BYTES = 32 };

typedef struct {
    uint32_t width;
    uint32_t height;
    uint32_t row_stride;
    uint32_t format_id;
    uint64_t byte_len;
} vp_capture_frame_info;

/*
 * Candidate ABI. The caller serializes calls on each handle and supplies an
 * owned Xvfb PID and its local display number (90 through 65535, screen 0).
 * Both output objects must be writable and non-overlapping. *out must be NULL.
 * On success, stop owns the obligation to dispose of the returned handle.
 */
int32_t vp_capture_start(uint32_t display, int32_t xvfb_pid,
                         vp_capture **out, vp_capture_frame_info *info);

/*
 * Writes one dense RGB24 frame and its binary SHA-256. The writable buffers
 * must be live, exclusive, and non-overlapping until return. Only byte_len
 * bytes in dst are written. Neither output is valid after a failed capture.
 * X11, resource, and timeout errors close the worker. Call stop to free its handle.
 */
int32_t vp_capture_next_frame(vp_capture *capture, uint8_t *dst, size_t dst_len,
                              uint8_t sha256_out[VP_CAPTURE_SHA256_BYTES]);

/* Joins the worker and nulls *capture. A valid slot containing NULL is OK. */
int32_t vp_capture_stop(vp_capture **capture);

#ifdef __cplusplus
}
#endif
#endif
