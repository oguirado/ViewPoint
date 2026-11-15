#!/bin/sh
# Runs only in the offline builder. /input is disposable scratch, /recipe is read-only.
set -eu
trap 'rc=$?; if [ "$rc" -ne 0 ]; then exit 3; fi' 0
mkdir -p /stage
tar -xf /input/rootfs.tar -C /stage
mkdir -p /stage/sbin /stage/usr/local/bin /stage/run/srfview
cp /input/srf-guest-init /stage/sbin/srf-guest-init
cp /input/srf-guest-init /stage/init
cp /input/srf-guest-agent /stage/usr/local/bin/srf-guest-agent
chmod 0755 /stage/init /stage/sbin/srf-guest-init /stage/usr/local/bin/srf-guest-agent
# These copies were adapted and checked on the host before export.
cp /input/identity-entrypoint.sh /stage/usr/local/bin/identity-entrypoint.sh
cp /input/identity-xpra-layer.sh /stage/usr/local/bin/identity-xpra-layer.sh
chmod 0555 /stage/usr/local/bin/identity-entrypoint.sh /stage/usr/local/bin/identity-xpra-layer.sh
# The program image need not contain e2fsprogs. Install the offline builder's
# e2fsck with its own loader and libraries, without replacing guest system libs.
fsck_runtime=/stage/usr/local/lib/srf-fsck
mkdir -p "$fsck_runtime"
cp /sbin/e2fsck "$fsck_runtime/e2fsck"
loader=$(readelf -l /sbin/e2fsck | sed -n 's/.*interpreter: \(.*\)]/\1/p')
[ -n "$loader" ] && [ -f "$loader" ]
cp "$loader" "$fsck_runtime/ld.so"
ldd /sbin/e2fsck > /input/e2fsck.ldd
awk '/=> \// {print $3}' /input/e2fsck.ldd > /input/e2fsck.libs
while IFS= read -r library; do
    [ -f "$library" ]
    cp "$library" "$fsck_runtime/"
done < /input/e2fsck.libs
rm -f /stage/sbin/fsck
cat > /stage/sbin/fsck <<'FSCK'
#!/bin/sh
exec /usr/local/lib/srf-fsck/ld.so --library-path /usr/local/lib/srf-fsck /usr/local/lib/srf-fsck/e2fsck "$@"
FSCK
chmod 0555 /stage/sbin/fsck "$fsck_runtime/ld.so" "$fsck_runtime/e2fsck"
truncate -s 3G /input/root.ext4
mke2fs -q -F -t ext4 -b 4096 -L srf-vm-root \
    -E lazy_itable_init=0,lazy_journal_init=0 -d /stage /input/root.ext4

# The GPT wrapper is written on the host (util-linux sfdisk is absent from this builder).
