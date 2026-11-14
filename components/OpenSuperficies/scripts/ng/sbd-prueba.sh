#!/usr/bin/env bash
# scripts/ng/sbd-prueba.sh · Hermetic sbd daemon launcher for U-17 oracle and tests.
# Manages ephemeral OpenSwitchboard daemon with short UDS path and pinned HTTP loopback port.
set -euo pipefail
export LC_ALL=C

SBD_BIN="/home/user/Documentos/OpenSwitchboard/bin/sbd"
ACTIVE_FILE="/tmp/srf-sbd-u17.active"
HTTP_PORT="10025"

cmd_arrancar() {
  local root="/tmp/srf-sbd-$$"
  local base_dir="$root/openswitchboard"
  mkdir -p "$base_dir/rooms" "$base_dir/wal"
  mkdir -p evidencia/v2/u17

  local uds="$root/sbd.sock"
  local wal_path="$base_dir/wal/aries.wal"
  local rooms_dir="$base_dir/rooms"

  local dpid=0
  # Release on every error path during arrancar
  trap 'local err_rc=$?; if [[ $dpid -gt 0 ]]; then kill -9 "$dpid" 2>/dev/null || true; fi; rm -rf "$root" "$ACTIVE_FILE" 2>/dev/null || true; exit $err_rc' ERR

  export XDG_RUNTIME_DIR="$root"
  "$SBD_BIN" daemon \
    --salas "$rooms_dir" \
    --wal "$wal_path" \
    --uds "$uds" \
    --http "127.0.0.1:$HTTP_PORT" > "$root/sbd.log" 2>&1 &
  dpid=$!

  echo "$dpid" > "$root/daemon.pid"

  python3 -c "
import json
data = {
    'root': '$root',
    'uds': '$uds',
    'http': '127.0.0.1:$HTTP_PORT',
    'pid': $dpid,
    'vivo': True
}
with open('$ACTIVE_FILE', 'w') as f:
    json.dump(data, f)
"

  # Wait for UDS socket to become ready
  local ok=false
  for _ in $(seq 1 40); do
    if [[ -S "$uds" ]] && kill -0 "$dpid" 2>/dev/null; then
      ok=true
      break
    fi
    sleep 0.05
  done

  if [[ "$ok" != "true" ]]; then
    kill -9 "$dpid" 2>/dev/null || true
    rm -rf "$root" "$ACTIVE_FILE"
    printf '{"vivo":false,"error":"timeout_waiting_for_uds_socket"}\n'
    exit 1
  fi

  local uds_len=${#uds}
  python3 -c "
import json
out = {
    'vivo': True,
    'pid': $dpid,
    'raiz_temporal': '$root',
    'longitud_ruta_uds': $uds_len,
    'http_fijado_a': '127.0.0.1:$HTTP_PORT',
    'uds': '$uds'
}
print(json.dumps(out))
"
}

cmd_parar() {
  local exit_rc=0
  local root=""
  local dpid=0

  if [[ -f "$ACTIVE_FILE" ]]; then
    root=$(python3 -c "import json; d=json.load(open('$ACTIVE_FILE')); print(d.get('root',''))" 2>/dev/null || true)
    dpid=$(python3 -c "import json; d=json.load(open('$ACTIVE_FILE')); print(d.get('pid',0))" 2>/dev/null || true)
  fi

  # Fallback: discover root from active /tmp directory if active file was absent
  if [[ -z "$root" || ! -d "$root" ]]; then
    for candidate in /tmp/srf-sbd-*; do
      if [[ -d "$candidate" ]]; then
        root="$candidate"
        break
      fi
    done
  fi

  # parar must read $root/daemon.pid
  if [[ -n "$root" && -f "$root/daemon.pid" ]]; then
    local pid_from_file
    pid_from_file=$(cat "$root/daemon.pid" 2>/dev/null || echo 0)
    if [[ -n "$pid_from_file" && "$pid_from_file" =~ ^[0-9]+$ && "$pid_from_file" -gt 0 ]]; then
      dpid="$pid_from_file"
    fi
  fi

  # Hunt orphan daemon if arrancar failed between spawning daemon and recording pid
  if [[ -z "$dpid" || "$dpid" -le 0 ]] || ! kill -0 "$dpid" 2>/dev/null; then
    local orphan_pid
    orphan_pid=$(pgrep -f "$SBD_BIN daemon.*127.0.0.1:$HTTP_PORT" 2>/dev/null | head -n 1 || true)
    if [[ -n "$orphan_pid" && "$orphan_pid" =~ ^[0-9]+$ ]]; then
      dpid="$orphan_pid"
    fi
  fi

  # Kill daemon process if running
  if [[ -n "$dpid" && "$dpid" -gt 0 ]]; then
    if kill -0 "$dpid" 2>/dev/null; then
      kill -TERM "$dpid" 2>/dev/null || true
      for _ in $(seq 1 30); do
        if ! kill -0 "$dpid" 2>/dev/null; then
          break
        fi
        sleep 0.05
      done
      if kill -0 "$dpid" 2>/dev/null; then
        kill -KILL "$dpid" 2>/dev/null || true
        sleep 0.05
      fi
      if kill -0 "$dpid" 2>/dev/null; then
        echo "error: failed to kill sbd daemon pid $dpid" >&2
        exit_rc=1
      fi
    fi
  fi

  # Release on every path: purge root, active marker, and temp files
  if [[ -n "$root" && -d "$root" && "$root" == /tmp/srf-sbd-* ]]; then
    rm -rf "$root" || exit_rc=1
  fi
  rm -f "$ACTIVE_FILE"
  rm -f /tmp/srf-sbd-u17.active
  rm -rf /tmp/srf-sbd-nonexistent

  exit $exit_rc
}

case "${1:-}" in
  arrancar)
    cmd_arrancar
    ;;
  parar)
    cmd_parar
    ;;
  *)
    echo "Uso: $0 {arrancar|parar}" >&2
    exit 2
    ;;
esac
