#!/bin/sh
set -eu
VISION_INSTALL_MODE=${1:---dry-run}
case "$VISION_INSTALL_MODE" in --dry-run|--apply) ;; *) echo 'usage: install.sh [--dry-run|--apply]' >&2; exit 2;; esac
if [ "$#" -gt 1 ]; then echo 'usage: install.sh [--dry-run|--apply]' >&2; exit 2; fi
if [ "$VISION_INSTALL_MODE" = --apply ] && [ "${VISION_INSTALL_OK:-}" != 1 ] && [ "${VIS_INSTALL_OK:-}" != 1 ]; then
  echo 'E_RULE: --apply requires VISION_INSTALL_OK=1 (VIS_INSTALL_OK=1 also accepted)' >&2; exit 5
fi
VISION_INSTALL_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 -I "$VISION_INSTALL_ROOT/skill_copies.py" "$VISION_INSTALL_MODE"
