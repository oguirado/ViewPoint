# syntax=docker/dockerfile:1
FROM opensuperficies-browser-arm64:1.0.0 AS base

USER root
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 && rm -rf /var/lib/apt/lists/*
COPY bin/srf-relay /usr/local/bin/srf-relay
COPY docker/ng/browser/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/srf-relay /usr/local/bin/entrypoint.sh

FROM scratch
COPY --from=base / /

ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DBUS_SESSION_BUS_ADDRESS="disabled:" \
    DBUS_SYSTEM_BUS_ADDRESS="disabled:" \
    XDG_RUNTIME_DIR="/run/newton-tenant" \
    XDG_CURRENT_DESKTOP="" \
    XDG_SESSION_TYPE="x11" \
    GTK_USE_PORTAL=0 \
    GDK_BACKEND=x11 \
    QT_QPA_PLATFORM=xcb \
    NO_AT_BRIDGE=1 \
    GIO_USE_VFS=local \
    DISPLAY=":100" \
    LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe \
    LP_NUM_THREADS=4 \
    HOME=/home/tenant

USER 10001:0
WORKDIR /home/tenant
EXPOSE 10000 9220

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
