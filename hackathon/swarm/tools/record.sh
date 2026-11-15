#!/usr/bin/env bash
# ViewPoint Swarm X11 Capsule Recorder (H3)
# Usage: swarm/tools/record.sh [--allow-display0] <display> <name>
# Stops cleanly on SIGINT: kill -INT <PID>

set -euo pipefail
export LC_ALL=C

ALLOW_DISPLAY0=0
POSITIONAL=()

for arg in "$@"; do
  case "$arg" in
    --allow-display0)
      ALLOW_DISPLAY0=1
      ;;
    -h|--help)
      printf 'Usage: %s [--allow-display0] <display> <name>\n' "$0" >&2
      exit 0
      ;;
    -*)
      printf 'ERROR: unknown flag: %s\n' "$arg" >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$arg")
      ;;
  esac
done

if [[ "${#POSITIONAL[@]}" -ne 2 ]]; then
  printf 'Usage: %s [--allow-display0] <display> <name>\n' "$0" >&2
  exit 2
fi

DISPLAY_ARG="${POSITIONAL[0]}"
NAME_ARG="${POSITIONAL[1]}"

# Validate strict X11 display syntax ([host]:display[.screen][+x,y]) under LC_ALL=C:
#   - optional ASCII host ([A-Za-z0-9._-]+ or unix)
#   - exactly one colon ':'
#   - one or more ASCII digits ([0-9]+)
#   - optional dot + ASCII screen digits (\.[0-9]+)
#   - optional trailing offset (\+[0-9]+,[0-9]+)
# Explicitly refuses leading +x,y before ':', whitespace, non-ASCII digits, or trailing chars.
DISPLAY_REGEX='^([A-Za-z0-9._-]+|unix)?:([0-9]+)(\.[0-9]+)?(\+[0-9]+,[0-9]+)?$'
if [[ "$DISPLAY_ARG" == *$'\n'* || "$DISPLAY_ARG" == *$'\r'* || ! "$DISPLAY_ARG" =~ $DISPLAY_REGEX ]]; then
  printf 'REFUSED: invalid X11 display specification "%s" (expected [host]:<N>[.<S>][+<x>,<y>])\n' "$DISPLAY_ARG" >&2
  exit 2
fi

DISP_NUM="${BASH_REMATCH[2]}"

# Normalize display number in base-10 after stripping leading zeros (:01, :001, :00, :000)
DISP_STRIPPED="${DISP_NUM#"${DISP_NUM%%[!0]*}"}"
DISP_STRIPPED="${DISP_STRIPPED:-0}"
if (( ${#DISP_STRIPPED} > 9 )); then
  printf 'REFUSED: invalid X11 display number in "%s"\n' "$DISPLAY_ARG" >&2
  exit 2
fi
DISP_NUM_DEC=$((10#${DISP_STRIPPED}))

# Hard safety gate: :1 / :01 / :1.0 MUST ALWAYS be refused unconditionally
if (( DISP_NUM_DEC == 1 )); then
  printf 'REFUSED: display "%s" (:1) is strictly forbidden (reserved by active recording session).\n' "$DISPLAY_ARG" >&2
  exit 2
fi

# Safety gate: :0 / :00 / :0.0 refused unless --allow-display0 explicitly supplied
if (( DISP_NUM_DEC == 0 && ALLOW_DISPLAY0 != 1 )); then
  printf 'REFUSED: display "%s" (:0) requires explicit --allow-display0.\n' "$DISPLAY_ARG" >&2
  exit 2
fi

if [[ -z "$NAME_ARG" || "$NAME_ARG" == *$'\n'* || "$NAME_ARG" == *$'\r'* || ! "$NAME_ARG" =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf 'REFUSED: invalid recording name "%s" (use [A-Za-z0-9._-]+)\n' "$NAME_ARG" >&2
  exit 2
fi

VIDEO_DIR="$HOME/Vídeos/ViewPoint_hackathon"
mkdir -p "$VIDEO_DIR"

STAMP="$(date +%H%M%S)"
OUT="${VIDEO_DIR}/swarm_${NAME_ARG}_${STAMP}.mp4"

STATE_DIR="/tmp"
if [[ -n "${SWARM_RUN_DIR:-}" ]]; then
  mkdir -p "$SWARM_RUN_DIR"
  LOG="${SWARM_RUN_DIR}/swarm_record_${NAME_ARG}.ffmpeg.log"
else
  LOG="${STATE_DIR}/swarm_record_${NAME_ARG}.ffmpeg.log"
fi

# Sample wall-clock start timestamp immediately before spawning ffmpeg.
# Launch ffmpeg with DISPLAY and WAYLAND_DISPLAY scrubbed so no parser fallback can hit ambient DISPLAY.
START_TS_MS="$(date +%s%3N)"
setsid nohup env -u DISPLAY -u WAYLAND_DISPLAY ffmpeg -y \
  -video_size 1920x1080 \
  -framerate 15 \
  -f x11grab \
  -draw_mouse 1 \
  -i "${DISPLAY_ARG}" \
  -c:v libx264 \
  -preset ultrafast \
  -pix_fmt yuv420p \
  -movflags +faststart \
  "$OUT" > "${LOG}" 2>&1 < /dev/null &
PID=$!

sleep 0.15
if ! kill -0 "$PID" 2>/dev/null; then
  wait "$PID" 2>/dev/null || true
  rm -f \
    "/tmp/swarm_record_${NAME_ARG}.outpath" \
    "/tmp/swarm_record_${NAME_ARG}.pid" \
    "/tmp/swarm_record_${PID}.mp4.path"
  if [[ -n "${SWARM_RUN_DIR:-}" && "${SWARM_RUN_DIR}" != "/tmp" ]]; then
    rm -f \
      "${SWARM_RUN_DIR}/swarm_record_${NAME_ARG}.outpath" \
      "${SWARM_RUN_DIR}/swarm_record_${NAME_ARG}.pid" \
      "${SWARM_RUN_DIR}/swarm_record_${PID}.mp4.path"
  fi
  printf 'ERROR: ffmpeg exited immediately for display "%s". Log (%s):\n' "$DISPLAY_ARG" "$LOG" >&2
  tail -n 20 "$LOG" >&2 || true
  exit 1
fi

# Persist PID and output paths in /tmp and optional $SWARM_RUN_DIR after liveness succeeds (K4)
printf '%s\n' "$OUT" > "/tmp/swarm_record_${NAME_ARG}.outpath"
printf '%s\n' "$PID" > "/tmp/swarm_record_${NAME_ARG}.pid"
printf '%s\n' "$OUT" > "/tmp/swarm_record_${PID}.mp4.path"

if [[ -n "${SWARM_RUN_DIR:-}" && "${SWARM_RUN_DIR}" != "/tmp" ]]; then
  printf '%s\n' "$OUT" > "${SWARM_RUN_DIR}/swarm_record_${NAME_ARG}.outpath"
  printf '%s\n' "$PID" > "${SWARM_RUN_DIR}/swarm_record_${NAME_ARG}.pid"
  printf '%s\n' "$OUT" > "${SWARM_RUN_DIR}/swarm_record_${PID}.mp4.path"
fi

# Write sidecar wall-clock start metadata safely escaped via python3 json.dumps
env -u DISPLAY -u WAYLAND_DISPLAY python3 -c 'import json, sys; print(json.dumps({"v": 1, "name": sys.argv[1], "display": sys.argv[2], "output": sys.argv[3], "start_ts_ms": int(sys.argv[4]), "fps": 15, "width": 1920, "height": 1080}, separators=(",", ":")))' \
  "$NAME_ARG" "$DISPLAY_ARG" "$OUT" "$START_TS_MS" > "${OUT}.meta.json"

printf 'OUT=%s\n' "$OUT" >&2
printf '%s\n' "$PID"
