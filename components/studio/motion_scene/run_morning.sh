#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export LC_ALL=C.UTF-8 LANG=C.UTF-8 TZ=UTC PYTHONDONTWRITEBYTECODE=1
exec python3 -B "$here/motion_scene.py" run-morning "$@"
