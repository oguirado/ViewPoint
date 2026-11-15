#!/bin/sh
# OPERATOR-ONLY download (needs network): resolves gcc, libc6-dev, make, xauth and tmux against the
# identity browser image's installed packages and saves the .deb closure into the host deb dir.
# The AI never runs this; the operator does, then os-cu-0 pins every .deb sha256 in the recipe.
set -eu
OUT=${1:-$HOME/.local/share/opensuperficies/toolchain/debs}
install -d -m 0755 "$OUT"
exec docker run --rm --user 0 --network bridge -v "$OUT":/out --entrypoint sh \
  opensuperficies-identity-browser-arm64:0.1.0 -c \
  'apt-get update -qq && apt-get install -y -qq --no-install-recommends --download-only gcc libc6-dev make xauth tmux && cp /var/cache/apt/archives/*.deb /out/ && chown -R '"$(id -u):$(id -g)"' /out && ls /out | wc -l'
