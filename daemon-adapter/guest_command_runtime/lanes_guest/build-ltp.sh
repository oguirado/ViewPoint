#!/bin/sh
# Run offline with a local upstream LTP 20260930 source archive.
# https://codeload.github.com/linux-test-project/ltp/tar.gz/2279d708c817db657611a30c427c7d06c4360049
set -efu
export LC_ALL=C SOURCE_DATE_EPOCH=0
[ "$#" = 2 ] || exit 2
printf '%s  %s\n' 35146cb1d0e82517aac8dbefbd88bd96300ce383736aae069e435fd991ea50bb "$1" |
    sha256sum -c -
mkdir "$2"
tar -xzf "$1" -C "$2" --strip-components=1
cd "$2"
make autotools
./configure --disable-metadata CFLAGS="-O2 -g0" LDFLAGS="-static -no-pie -s -Wl,--build-id=none"
make -C lib -j4
for target in openat2/openat201 openat2/openat203 ptrace/ptrace01 unshare/unshare02 \
    mount/mount03 mount/mount03_suid_child; do
    make -C "testcases/kernel/syscalls/${target%/*}" "${target#*/}"
done
