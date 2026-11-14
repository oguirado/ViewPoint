# OPERATOR-RUN vendor_fetch.sh replaces OPERATOR_DIGEST with the pulled arm64 digest.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
COPY docker/vm/vendor/ /tmp/srf-vendor/
RUN cd /tmp/srf-vendor && sha256sum -c SHA256SUMS && \
    dpkg -i ./*.deb && \
    rm -rf /tmp/srf-vendor && \
    mkdir -p /usr/local/libexec /usr/local/share/srf-vm /home/worker && \
    ln -sf /usr/local/bin/identity-entrypoint.sh /usr/bin/xdotool && \
    ln -sf /usr/local/bin/identity-entrypoint.sh /usr/bin/python3 && \
    ln -sf /usr/local/bin/identity-entrypoint.sh /usr/local/libexec/srf-fbread
COPY build/room_CAPSULE/srf-vm-entry /usr/local/bin/identity-entrypoint.sh
COPY build/vm_guest/Image build/vm_guest/root.raw build/vm_guest/MANIFEST.sha256 /usr/local/share/srf-vm/
RUN cd /usr/local/share/srf-vm && sha256sum -c MANIFEST.sha256
USER 10001:10001
ENV HOME=/home/worker PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENTRYPOINT ["/usr/local/bin/identity-entrypoint.sh"]
