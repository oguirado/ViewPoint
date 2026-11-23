#define _GNU_SOURCE
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <xcb/shm.h>
#include <xcb/xcb.h>

#include "../vp_capture.h"

struct vp_x11 {
    xcb_connection_t *connection;
    xcb_window_t root;
    xcb_visualid_t visual;
    xcb_shm_seg_t segment;
    uint16_t width;
    uint16_t height;
    uint32_t size;
    const uint8_t *pixels;
};

int vp_x11_connect(uint32_t display, int32_t pid) {
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    int len = snprintf(address.sun_path, sizeof address.sun_path,
                       "/tmp/.X11-unix/X%u", display);
    if (len < 0 || (size_t)len >= sizeof address.sun_path)
        return -1;

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0)
        return -1;

    struct ucred peer;
    socklen_t peer_len = sizeof peer;
    if (connect(fd, (struct sockaddr *)&address, sizeof address) != 0 ||
        getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_len) != 0 ||
        peer_len != sizeof peer || peer.pid != pid || peer.uid != geteuid() ||
        fcntl(fd, F_SETFL, 0) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

void vp_x11_close(struct vp_x11 *source) {
    if (source->pixels)
        munmap((void *)source->pixels, source->size);
    /* Disconnect also releases the X server's segment registration. */
    xcb_disconnect(source->connection);
    free(source);
}

static bool expected_visual(const xcb_setup_t *setup, const xcb_screen_t *screen) {
    if (screen->root_depth != 24 || setup->image_byte_order != XCB_IMAGE_ORDER_LSB_FIRST)
        return false;
    for (xcb_format_iterator_t fmt = xcb_setup_pixmap_formats_iterator(setup);
         fmt.rem; xcb_format_next(&fmt)) {
        if (fmt.data->depth != 24)
            continue;
        if (fmt.data->bits_per_pixel != 32 || fmt.data->scanline_pad != 32)
            return false;

        for (xcb_depth_iterator_t depth = xcb_screen_allowed_depths_iterator(screen);
             depth.rem; xcb_depth_next(&depth)) {
            if (depth.data->depth != 24)
                continue;
            for (xcb_visualtype_iterator_t v = xcb_depth_visuals_iterator(depth.data);
                 v.rem; xcb_visualtype_next(&v)) {
                if (v.data->visual_id == screen->root_visual)
                    return v.data->_class == XCB_VISUAL_CLASS_TRUE_COLOR &&
                           v.data->red_mask == 0xff0000 &&
                           v.data->green_mask == 0x00ff00 &&
                           v.data->blue_mask == 0x0000ff;
            }
        }
        return false;
    }
    return false;
}

static int32_t map_segment(struct vp_x11 *source) {
    xcb_connection_t *connection = source->connection;
    const xcb_query_extension_reply_t *extension =
        xcb_get_extension_data(connection, &xcb_shm_id);
    if (!extension || !extension->present)
        return VP_CAPTURE_UNSUPPORTED_FORMAT;
    xcb_shm_query_version_reply_t *version =
        xcb_shm_query_version_reply(connection, xcb_shm_query_version(connection), NULL);
    if (!version)
        return VP_CAPTURE_X11_ERROR;
    bool supported = version->major_version == 1 && version->minor_version >= 2;
    free(version);
    if (!supported)
        return VP_CAPTURE_UNSUPPORTED_FORMAT;

    source->segment = xcb_generate_id(connection);
    xcb_shm_create_segment_cookie_t request =
        xcb_shm_create_segment(connection, source->segment, source->size, 0);
    xcb_shm_create_segment_reply_t *reply =
        xcb_shm_create_segment_reply(connection, request, NULL);
    if (!reply)
        return VP_CAPTURE_X11_ERROR;
    int *fds = xcb_shm_create_segment_reply_fds(connection, reply);
    int nfd = reply->nfd;
    int32_t status = VP_CAPTURE_X11_ERROR;
    if (nfd == 1 && fds) {
        struct stat info;
        if (fstat(fds[0], &info) == 0 && S_ISREG(info.st_mode) &&
            info.st_size >= (off_t)source->size) {
            void *pixels = mmap(NULL, source->size, PROT_READ, MAP_SHARED, fds[0], 0);
            if (pixels != MAP_FAILED) {
                source->pixels = pixels;
                status = VP_CAPTURE_OK;
            } else {
                status = VP_CAPTURE_RESOURCE_ERROR;
            }
        }
    }
    if (fds)
        for (int i = 0; i < nfd; i++)
            close(fds[i]);
    free(reply);
    return status;
}

/* Always consumes fd. The caller can interrupt XCB by shutting down an alias. */
int32_t vp_x11_open(int fd, struct vp_x11 **out, uint32_t *width, uint32_t *height) {
    struct vp_x11 *source = calloc(1, sizeof *source);
    if (!source) {
        close(fd);
        return VP_CAPTURE_RESOURCE_ERROR;
    }
    /* Pass no Xauthority data. In tests the server is isolated by namespaces. */
    source->connection = xcb_connect_to_fd(fd, NULL);
    int32_t status = VP_CAPTURE_BAD_DISPLAY;
    if (xcb_connection_has_error(source->connection))
        goto fail;

    const xcb_setup_t *setup = xcb_get_setup(source->connection);
    xcb_screen_iterator_t screens = xcb_setup_roots_iterator(setup);
    status = VP_CAPTURE_UNSUPPORTED_FORMAT;
    if (screens.rem != 1 || !expected_visual(setup, screens.data))
        goto fail;
    const xcb_screen_t *screen = screens.data;
    source->width = screen->width_in_pixels;
    source->height = screen->height_in_pixels;
    uint64_t size = (uint64_t)source->width * source->height * 4;
    if (!size || size > 64u * 1024 * 1024)
        goto fail;
    source->size = (uint32_t)size;
    source->root = screen->root;
    source->visual = screen->root_visual;

    status = map_segment(source);
    if (status != VP_CAPTURE_OK)
        goto fail;

    *out = source;
    *width = source->width;
    *height = source->height;
    return VP_CAPTURE_OK;

fail:
    vp_x11_close(source);
    return status;
}

const uint8_t *vp_x11_pixels(struct vp_x11 *source) {
    xcb_shm_get_image_cookie_t request =
        xcb_shm_get_image(source->connection, source->root, 0, 0, source->width,
                          source->height, 0x00ffffff, XCB_IMAGE_FORMAT_Z_PIXMAP,
                          source->segment, 0);
    xcb_shm_get_image_reply_t *reply =
        xcb_shm_get_image_reply(source->connection, request, NULL);
    bool valid = reply && reply->size == source->size &&
                 reply->depth == 24 && reply->visual == source->visual;
    free(reply);
    return valid ? source->pixels : NULL;
}
