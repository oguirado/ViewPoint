#!/usr/bin/env bash
# int-demo: ONE command for the integrated Track P demo (claude-fable-5-2).
# Starts a private Blender (vision job vpintdemo, 1920x1080) with the F1 bridge, the rewind broker with the
# auction scheduler and the R3 judge, records the display, runs the scenario with one deliberate wrong edit
# (judge rejects, capsule rewinds), composes cursors + queue strip, projects to OpenSwitchboard room
# viewpoint-capsule-test and TaskLight room "demo", runs 4 stub agents, saves evidence, stops the job.
# usage: run_demo.sh [tag] [--no-auction]     evidence -> product/ref_demo/evidence/<tag>/
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
tag="${1:-$(date +%H%M%S)}"; shift || true
case "$tag" in *[!A-Za-z0-9_-]*|"") echo "tag must be [A-Za-z0-9_-]+" >&2; exit 2;; esac
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
exec systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 "$here/demo_live.py" --job vpintdemo --rt "/dev/shm/vqa-cu/ref-int-demo-$tag" \
  --evidence "$here/evidence/$tag" "$@"
