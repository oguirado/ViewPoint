# syntax=docker/dockerfile:1
FROM opensuperficies-browser-arm64:1.0.0 AS base

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    at-spi2-core \
    gsettings-desktop-schemas \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/newton-tenant && chown -R 10001:0 /run/newton-tenant && chmod 0700 /run/newton-tenant

# Reproduce this pinned static artifact from the repository root (no image build):
# CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -buildvcs=false -trimpath -ldflags='-s -w -buildid=' -o build/ramcap/srf-fbread ./cmd/srf-fbread
COPY --chown=0:0 --chmod=0555 build/ramcap/srf-fbread /usr/local/libexec/srf-fbread
RUN printf '%s\n' '5b63ae8fc34ff822dc727144f9b3be516456a084ce71d42e857a215a5e9042ba  /usr/local/libexec/srf-fbread' | sha256sum -c -

COPY docker/ng/desktop/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

FROM scratch
COPY --from=base / /

ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/newton-tenant/bus" \
    DBUS_SYSTEM_BUS_ADDRESS="disabled:" \
    XDG_RUNTIME_DIR="/run/newton-tenant" \
    XDG_CURRENT_DESKTOP="" \
    XDG_SESSION_TYPE="x11" \
    GTK_USE_PORTAL=0 \
    GDK_BACKEND=x11 \
    QT_QPA_PLATFORM=xcb \
    NO_AT_BRIDGE=0 \
    GIO_USE_VFS=local \
    DISPLAY=":100" \
    LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe \
    LP_NUM_THREADS=4 \
    HOME=/home/tenant

USER 10001:0
WORKDIR /home/tenant
EXPOSE 10000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
