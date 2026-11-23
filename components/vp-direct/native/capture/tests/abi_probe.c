#include "vp_capture.h"

#include <assert.h>
#include <stdlib.h>
#include <string.h>

_Static_assert(sizeof(vp_capture_frame_info) == 24, "unexpected frame info size");
_Static_assert(offsetof(vp_capture_frame_info, byte_len) == 16, "unexpected byte_len offset");

int main(int argc, char **argv) {
    vp_capture *capture = NULL;
    vp_capture_frame_info frame = {0};

    assert(vp_capture_start(0, 1, &capture, &frame) == VP_CAPTURE_BAD_DISPLAY);
    assert(capture == NULL);
    assert(vp_capture_stop(&capture) == VP_CAPTURE_OK);
    if (argc == 1) {
        return 0;
    }

    assert(argc == 3);
    uint32_t display = (uint32_t)strtoul(argv[1], NULL, 10);
    int32_t pid = (int32_t)strtol(argv[2], NULL, 10);
    assert(vp_capture_start(display, pid, &capture, &frame) == VP_CAPTURE_OK);
    assert(capture != NULL);
    assert(frame.width > 0 && frame.height > 0 && frame.row_stride == frame.width * 3);
    assert(frame.format_id == VP_CAPTURE_RGB24 && frame.byte_len == frame.row_stride * frame.height);
    assert(vp_capture_start(display, pid, &capture, &frame) == VP_CAPTURE_INVALID_ARGUMENT);

    uint8_t *pixels = malloc(frame.byte_len);
    uint8_t digest[VP_CAPTURE_SHA256_BYTES];
    uint8_t zeros[VP_CAPTURE_SHA256_BYTES] = {0};
    assert(pixels != NULL);
    assert(vp_capture_next_frame(capture, pixels, frame.byte_len - 1, digest)
           == VP_CAPTURE_BUFFER_TOO_SMALL);
    assert(vp_capture_next_frame(capture, pixels, frame.byte_len, digest) == VP_CAPTURE_OK);
    assert(memcmp(digest, zeros, sizeof zeros) != 0);
    assert(vp_capture_stop(&capture) == VP_CAPTURE_OK && capture == NULL);
    assert(vp_capture_stop(&capture) == VP_CAPTURE_OK);
    free(pixels);
}
