FROM opensuperficies-desktop-arm64:1.1.0
USER root
# Installed package assertions only; no downloads.
RUN test "$(dpkg --print-architecture)" = arm64 \
 && test "$(dpkg-query -W -f='${Version}' python3-gi)" = '3.42.2-3+b1' \
 && test "$(dpkg-query -W -f='${Version}' gir1.2-gtk-3.0)" = '3.24.38-2~deb12u3' \
 && test "$(dpkg-query -W -f='${Version}' libatk-bridge2.0-0)" = '2.46.0-5' \
 && test "$(dpkg-query -W -f='${Version}' at-spi2-core)" = '2.46.0-5' \
 && test "$(dpkg-query -W -f='${Version}' xdotool)" = '1:3.20160805.1-5' \
 && test "$(dpkg-query -W -f='${Version}' xpra)" = '3.1.3-0.1'
COPY --chown=0:0 --chmod=0555 docker/identity/xpra-layer.sh /usr/local/bin/identity-xpra-layer.sh
COPY --chown=0:0 --chmod=0555 docker/program/entrypoint.sh /usr/local/bin/identity-entrypoint.sh
COPY --chown=0:0 --chmod=0555 docker/program/apps/srf-notepad.py /usr/local/libexec/srf-notepad.py
ENV DISPLAY=:100 HOME=/profile XDG_CONFIG_HOME=/profile/config \
    XDG_CACHE_HOME=/profile/cache XDG_RUNTIME_DIR=/run/newton-tenant \
    DBUS_SESSION_BUS_ADDRESS=disabled: DBUS_SYSTEM_BUS_ADDRESS=disabled: \
    GTK_USE_PORTAL=0 NO_AT_BRIDGE=1 GIO_USE_VFS=local
USER 10001:0
WORKDIR /profile
ENTRYPOINT ["/usr/local/bin/identity-entrypoint.sh"]
