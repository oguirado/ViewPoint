#!/usr/bin/env bash
# record.sh <display> <name> <outdir>
# Records the given X display (1920x1080, 30 fps, x11grab) to
#   <outdir>/<name>_<HHMM>.mp4
# and prints the ffmpeg PID on stdout (one line, digits only).
# Sidecars: <same>.start_ms (epoch ms taken just before ffmpeg starts; pass it
# to compose.py --start-file) and <same>.log (ffmpeg stderr).
#
# STOP:  kill -INT <pid>     (ffmpeg then writes the moov atom and exits;
#                             never kill -9: the mp4 would be unplayable)
#
# Display :1 is the operator's desktop and is refused.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: record.sh <display> <name> <outdir>" >&2
  exit 2
fi
display="$1"; name="$2"; outdir="$3"

# Parse the display NUMBER: Xlib/xcb read ":01" and ":001.0" as display 1,
# so a string match on ":1" is not enough. Only :N or :N.S is accepted.
if [[ ! "$display" =~ ^:([0-9]{1,9})(\.[0-9]{1,9})?$ ]]; then
  echo "record.sh: display must look like :N or :N.S" >&2; exit 2
fi
if (( 10#${BASH_REMATCH[1]} == 1 )); then
  echo "record.sh: display :1 is off limits" >&2; exit 3
fi
case "$name" in
  *[!A-Za-z0-9_.-]*|"") echo "record.sh: name must be [A-Za-z0-9_.-]+" >&2; exit 2 ;;
esac

mkdir -p "$outdir"
base="$outdir/${name}_$(date +%H%M)"
out="$base.mp4"
if [ -e "$out" ]; then
  echo "record.sh: $out exists, not overwriting" >&2
  exit 4
fi

date +%s%3N > "$base.start_ms"
# setsid + nohup: the recorder survives the calling (non-interactive) shell.
# Scope with memory caps when systemd-run is available.
runner=()
if command -v systemd-run >/dev/null 2>&1 && [ "${VP_RECORD_NO_SCOPE:-0}" != 1 ]; then
  runner=(systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0)
fi
nohup setsid "${runner[@]}" ffmpeg -hide_banner -loglevel warning -nostdin -n \
  -f x11grab -draw_mouse 0 -video_size 1920x1080 -framerate 30 -i "$display" \
  -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p "$out" \
  > "$base.log" 2>&1 < /dev/null &
pid=$!

sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
  echo "record.sh: ffmpeg died at start, see $base.log" >&2
  exit 5
fi
if [ "$(cat "/proc/$pid/comm" 2>/dev/null)" != ffmpeg ]; then
  echo "record.sh: pid $pid is not ffmpeg (wrapper did not exec), see $base.log" >&2
  exit 6
fi
echo "$pid"
