#!/bin/sh
# apply.sh [remove]: install (default) or remove the open-mode capsule egress firewall, then
# write (or delete) the witness srf-identity-surface checks before an open start.
# Idempotent: removes its own jumps and flushes only its own chains; never flushes DOCKER-USER,
# INPUT, FORWARD or any other chain. Root only. rc 0 ok, 2 usage, 3 not root or tool missing, 5 failure.
set -eu
die() { echo "apply.sh: $2" >&2; exit "$1"; }
[ $# -le 1 ] || die 2 "usage: apply.sh [remove]"
MODE=${1:-apply}
case "$MODE" in apply|remove) ;; *) die 2 "usage: apply.sh [remove]" ;; esac
[ "$(id -u)" = 0 ] || die 3 "root required"
for tool in iptables ip6tables iptables-restore ip6tables-restore sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die 3 "missing $tool"
done
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || die 5 "script directory unavailable"
V4="$HERE/srf-open-egress.rules"
V6="$HERE/srf-open-egress6.rules"
WDIR=/run/srf-open-egress
WITNESS="$WDIR/rules.sha256"

unhook() {
  # Witness first: a half-removed firewall must never look installed.
  rm -f "$WITNESS" || die 5 "witness removal failed"
  while iptables -w -D DOCKER-USER -s 172.29.0.0/16 -j SRF-OPEN-EGRESS 2>/dev/null; do :; done
  while iptables -w -D INPUT -i srfo+ -j SRF-OPEN-INPUT 2>/dev/null; do :; done
  while ip6tables -w -D FORWARD -i srfo+ -j DROP 2>/dev/null; do :; done
  while ip6tables -w -D INPUT -i srfo+ -j DROP 2>/dev/null; do :; done
}

if [ "$MODE" = remove ]; then
  unhook
  for chain in SRF-OPEN-EGRESS SRF-OPEN-INPUT; do
    if iptables -w -n -L "$chain" >/dev/null 2>&1; then
      iptables -w -F "$chain" || die 5 "flush $chain failed"
      iptables -w -X "$chain" || die 5 "delete $chain failed"
    fi
  done
  if [ -d "$WDIR" ] && [ ! -L "$WDIR" ]; then rmdir "$WDIR" 2>/dev/null || :; fi
  exit 0
fi

[ -f "$V4" ] && [ ! -L "$V4" ] && [ -f "$V6" ] && [ ! -L "$V6" ] || die 3 "rules files missing"
unhook
# --noflush: the chain declarations flush only SRF-OPEN-EGRESS and SRF-OPEN-INPUT.
iptables-restore -w --noflush < "$V4" || die 5 "IPv4 rules refused"
ip6tables-restore -w --noflush < "$V6" || die 5 "IPv6 rules refused"
SUM=$(sha256sum "$V4" | cut -d ' ' -f 1) || die 5 "rules hash failed"
[ ! -L "$WDIR" ] || die 5 "witness directory is a symlink"
install -d -m 0755 -o root -g root "$WDIR" || die 5 "witness directory failed"
chmod 0755 "$WDIR" || die 5 "witness directory mode failed"
TMP="$WDIR/.rules.sha256.$$"
printf '%s\n' "$SUM" > "$TMP" || die 5 "witness write failed"
chown root:root "$TMP" && chmod 0644 "$TMP" && mv -f "$TMP" "$WITNESS" || { rm -f "$TMP"; die 5 "witness install failed"; }
exit 0
