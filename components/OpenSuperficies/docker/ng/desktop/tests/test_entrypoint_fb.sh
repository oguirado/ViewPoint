#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd "$(dirname "$0")/../../../.." && pwd)"
scratch="$root/build/room_RB2/shell"
mkdir -p "$scratch/bin"
# Only execute the bounded preflight prefix: no daemon, X, browser or docker.
python3 - "$root/docker/ng/desktop/entrypoint.sh" "$scratch" <<'PY'
import pathlib,sys
src=pathlib.Path(sys.argv[1]).read_text()
out=pathlib.Path(sys.argv[2])
out.joinpath('preflight.sh').write_text(src.split('export DBUS_SESSION_BUS_ADDRESS=')[0])
PY
cat > "$scratch/bin/stat" <<'SH'
#!/bin/sh
printf '%s\n' "${RB2_STAT_FS:-tmpfs}"
SH
cat > "$scratch/bin/fbread" <<'SH'
#!/bin/sh
exit 0
SH
chmod 0755 "$scratch/bin/stat" "$scratch/bin/fbread"
probe() {
 local cursor="$1" fs="$2" bin="$3" want_rc="$4" token="$5" rc=0
 PATH="$scratch/bin:$PATH" XVFB_CURSOR="$cursor" RB2_STAT_FS="$fs" SRF_FBREAD_BIN="$bin" \
   timeout 3 bash "$scratch/preflight.sh" >"$scratch/out" 2>"$scratch/err" || rc=$?
 [[ "$rc" == "$want_rc" ]] || { echo "preflight rc=$rc want=$want_rc" >&2; exit 5; }
 [[ -z "$token" ]] || { grep -Fxq "$token" "$scratch/err" || { cat "$scratch/err" >&2; exit 5; }; }
}
probe on tmpfs "$scratch/bin/fbread" 4 cursor_forbidden_ui
probe off ext4 "$scratch/bin/fbread" 4 fb_not_tmpfs
probe off tmpfs "$scratch/absent" 4 fbread_missing
probe off tmpfs "$scratch/bin/fbread" 0 ''
python3 - "$root/docker/ng/desktop/entrypoint.sh" <<'PY'
import pathlib,re,sys
s=pathlib.Path(sys.argv[1]).read_text()
def valid(t):
 m=re.search(r'^Xvfb .*?(?=\nXVFB_PID=)',t,re.M|re.S)
 return bool(m and '-fbdir /run/vqa/fb' in m[0] and '-nocursor' in m[0])
if not valid(s) or valid(s.replace('-nocursor','')) or valid(s.replace('-fbdir /run/vqa/fb','')):
 sys.exit(5)
if 'exit 1' in s: sys.exit(5)
PY
printf 'RB2 desktop preflight and cursor negative controls PASS\n'
