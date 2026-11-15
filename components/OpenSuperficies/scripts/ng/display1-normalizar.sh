#!/usr/bin/env bash
# display1-normalizar.sh - Normalizador del arbol X11 y verificador de foco para :1
set -euo pipefail
export LC_ALL=C

ACCION="${1:-ayuda}"
NORM_BASE="/tmp/display1-norm-base.txt"
ACTIVE_BASE="/tmp/display1-activewindow.txt"

case "$ACCION" in
  base)
    xwininfo -display :1 -root -tree | sed -E 's/0x[0-9a-fA-F]+/0x.../g' > "$NORM_BASE"
    DISPLAY=:1 xdotool getactivewindow > "$ACTIVE_BASE" 2>/dev/null || echo "none" > "$ACTIVE_BASE"
    ;;
  diff)
    DIFF_OUT=$(diff -u "$NORM_BASE" <(xwininfo -display :1 -root -tree | sed -E 's/0x[0-9a-fA-F]+/0x.../g') || true)
    if [ -n "$DIFF_OUT" ]; then
      printf '%s\n' "$DIFF_OUT"
    fi
    ;;
  foco)
    if [ ! -f "$ACTIVE_BASE" ]; then
      exit 1
    fi
    CURR=$(DISPLAY=:1 xdotool getactivewindow 2>/dev/null || echo "none")
    BASE=$(cat "$ACTIVE_BASE")
    if [ "$CURR" = "$BASE" ]; then
      exit 0
    else
      exit 1
    fi
    ;;
  *)
    echo "uso: $0 {base|diff|foco}" >&2
    exit 2
    ;;
esac
