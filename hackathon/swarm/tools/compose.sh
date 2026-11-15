#!/usr/bin/env bash
# ViewPoint Swarm Off-Display Overlay Compositor Wrapper (H3)
# Usage: swarm/tools/compose.sh <input.mp4> <events.jsonl> <output.mp4> [extra compositor flags...]

set -euo pipefail

if [[ "$#" -lt 3 ]]; then
  printf 'Usage: %s <input.mp4> <events.jsonl> <output.mp4> [extra compositor flags...]\n' "$0" >&2
  exit 2
fi

INPUT_MP4="$1"
EVENTS_JSONL="$2"
OUTPUT_MP4="$3"

if [[ ! -f "$INPUT_MP4" ]]; then
  printf 'ERROR: input video not found: %s\n' "$INPUT_MP4" >&2
  exit 2
fi

if [[ ! -f "$EVENTS_JSONL" ]]; then
  printf 'ERROR: events log not found: %s\n' "$EVENTS_JSONL" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# Strictly off-display: unset DISPLAY and WAYLAND_DISPLAY so zero display connections can ever occur
env -u DISPLAY -u WAYLAND_DISPLAY python3 -m swarm.overlay.compositor \
  --input "$INPUT_MP4" \
  --events "$EVENTS_JSONL" \
  --output "$OUTPUT_MP4" \
  "${@:4}"

if [[ ! -s "$OUTPUT_MP4" ]]; then
  printf 'ERROR: composed output is missing or empty: %s\n' "$OUTPUT_MP4" >&2
  exit 1
fi

PROBE_OUT="$(ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,width,height,pix_fmt \
  -of default=noprint_wrappers=1:nokey=0 "$OUTPUT_MP4")"

if ! grep -q '^codec_name=h264$' <<<"$PROBE_OUT"; then
  printf 'ERROR: ffprobe verification failed (expected h264 video stream):\n%s\n' "$PROBE_OUT" >&2
  exit 1
fi

if ! grep -q '^pix_fmt=yuv420p$' <<<"$PROBE_OUT"; then
  printf 'ERROR: ffprobe verification failed (expected pix_fmt=yuv420p):\n%s\n' "$PROBE_OUT" >&2
  exit 1
fi

if ! grep -qE '^width=[1-9][0-9]*$' <<<"$PROBE_OUT" || ! grep -qE '^height=[1-9][0-9]*$' <<<"$PROBE_OUT"; then
  printf 'ERROR: ffprobe verification failed (invalid dimensions):\n%s\n' "$PROBE_OUT" >&2
  exit 1
fi

# Verify +faststart: top-level moov atom must precede mdat atom
env -u DISPLAY -u WAYLAND_DISPLAY python3 -c '
import struct, sys
moov_off = None
mdat_off = None
with open(sys.argv[1], "rb") as f:
    f.seek(0, 2)
    end = f.tell()
    pos = 0
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        size, atom = struct.unpack(">I4s", hdr)
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                break
            size = struct.unpack(">Q", ext)[0]
        elif size == 0:
            size = end - pos
        if size < 8:
            break
        if atom == b"moov" and moov_off is None:
            moov_off = pos
        elif atom == b"mdat" and mdat_off is None:
            mdat_off = pos
        if moov_off is not None and mdat_off is not None:
            break
        pos += size
if moov_off is None or mdat_off is None or moov_off >= mdat_off:
    sys.stderr.write(f"ERROR: faststart verification failed (moov={moov_off}, mdat={mdat_off})\n")
    sys.exit(1)
' "$OUTPUT_MP4"
