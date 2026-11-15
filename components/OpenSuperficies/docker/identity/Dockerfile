FROM opensuperficies-desktop-arm64:1.1.0
USER root
# Exact installed package check; no package downloads or upgrades.
RUN test "$(dpkg --print-architecture)" = arm64 \
 && test "$(dpkg-query -W -f='${Version}' chromium)" = '152.0.7977.82-1~deb12u1' \
 && test -u /usr/lib/chromium/chrome-sandbox
COPY --chown=0:0 --chmod=0555 docker/identity/xpra-layer.sh /usr/local/bin/identity-xpra-layer.sh
COPY --chown=0:0 --chmod=0555 docker/identity/entrypoint.sh /usr/local/bin/identity-entrypoint.sh
COPY --chown=0:0 --chmod=0555 docker/identity/ax-shim.py /usr/local/bin/ax-shim.py
COPY --chown=0:0 --chmod=0444 docker/identity/ax-allowlist.json /usr/local/bin/ax-allowlist.json
ENV DISPLAY=:100 HOME=/profile XDG_CONFIG_HOME=/profile/config \
    XDG_CACHE_HOME=/profile/cache XDG_RUNTIME_DIR=/run/newton-tenant \
    DBUS_SESSION_BUS_ADDRESS=disabled: DBUS_SYSTEM_BUS_ADDRESS=disabled: \
    GTK_USE_PORTAL=0 NO_AT_BRIDGE=1 GIO_USE_VFS=local
USER 10001:0
WORKDIR /profile
ENTRYPOINT ["/usr/local/bin/identity-entrypoint.sh"]
