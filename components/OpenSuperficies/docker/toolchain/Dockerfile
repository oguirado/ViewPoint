# Holdings toolchain capsule image (operator approval 2026-10-25, relayed by the Holdings conductor):
# node 24.14.0, rust 1.97.0, go 1.26.10 (0.2.0, for metaviewer-0), a C toolchain, make, xauth and
# tmux on the identity browser base, which
# already carries Xvfb, xvfb-run, python3 and the Chromium/Electron runtime libraries.
# Built only by scripts/toolchain/print_build.py output, with --network=none: every input is in the
# context and pinned in docker/toolchain/recipe.json. No credentials, no network at run time.
FROM opensuperficies-identity-browser-arm64:0.1.0
USER root
COPY debs/ /tmp/debs/
RUN dpkg -i /tmp/debs/*.deb && rm -rf /tmp/debs \
    && command -v gcc cc ld make xauth tmux
ADD node.tar /opt/node/
ADD rust.tar /opt/rust/
# Official go.dev archive, pinned by its published sha256; ADD unpacks it to /opt/go.
ADD go1.26.10.linux-arm64.tar.gz /opt/
# Login shells reset PATH (/etc/profile): node, npm, npx and go stay reachable from /usr/local/bin.
RUN ln -s /opt/node/bin/node /opt/node/bin/npm /opt/node/bin/npx /opt/node/bin/corepack /opt/go/bin/go /opt/go/bin/gofmt /usr/local/bin/
ENV PATH=/opt/node/bin:/opt/rust/bin:/opt/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CARGO_NET_OFFLINE=true \
    GOTOOLCHAIN=local \
    GOPROXY=off \
    GOTELEMETRY=off \
    ELECTRON_SKIP_BINARY_DOWNLOAD=1 \
    npm_config_offline=true
RUN node --version && npx --version && rustc --version && cargo --version && go version \
    && bash -lc 'command -v node npm go'
ENTRYPOINT []
CMD ["bash"]
