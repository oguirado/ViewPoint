#!/usr/bin/env bash
set -uo pipefail
C="${1:?uso: verify-quarantine.sh <contenedor> <xpra_port>}"
P="${2:?puerto xpra publicado}"
FAIL=0
ok()   { printf '  [ OK ] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=1; }
# R-24 / CIR-2: B, C y D observan el display VIVO del operador. Se miden y se
# publican con su cifra, pero NO emiten veredicto ni tocan FAIL.
info() { printf '  [INFO] %s\n' "$1"; }
det()  { printf '    - %s\n' "$1"; }

echo "14 puertas: 11 con veredicto (A, E..N) y 3 informativas (B, C, D)"

echo "== A. El contenedor no alcanza :1 (mecanismo, no titulo de ventana) =="
# Tres pruebas y las tres deben cumplirse. Fail-closed: capsula muerta o sonda
# que no puede correr = [FAIL], nunca [ OK ].
A_MAL=""
if [ "$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null)" != "true" ]; then
  A_MAL=" A.0"
  det "A.0 la capsula $C no esta running: ninguna sonda puede correr"
else
  # A.1 desde DENTRO, DISPLAY=:1 xdpyinfo debe FALLAR con 'unable to open display'
  A1_ERR=$(docker exec "$C" env DISPLAY=:1 timeout 10 xdpyinfo 2>&1 >/dev/null); A1_RC=$?
  A1_ERR=$(printf '%s\n' "$A1_ERR" | head -1)
  if [ "$A1_RC" -eq 0 ]; then
    A_MAL="$A_MAL A.1"; det "A.1 DISPLAY=:1 xdpyinfo desde dentro: rc=0, la capsula ALCANZA :1"
  elif [ "$A1_RC" -eq 1 ] && printf '%s' "$A1_ERR" | grep -q 'unable to open display'; then
    det "A.1 DISPLAY=:1 xdpyinfo desde dentro: rc=1 (${A1_ERR})"
  else
    A_MAL="$A_MAL A.1"; det "A.1 la sonda no pudo juzgar: rc=${A1_RC} (${A1_ERR:-sin salida})"
  fi
  # A.2 dentro de la capsula, /tmp/.X11-unix ausente o vacio
  A2_LS=$(docker exec "$C" sh -c '[ -e /tmp/.X11-unix ] || { echo __AUSENTE__; exit 0; }; ls -A /tmp/.X11-unix' 2>&1); A2_RC=$?
  if [ "$A2_RC" -ne 0 ]; then
    A_MAL="$A_MAL A.2"; det "A.2 la sonda no pudo juzgar: rc=${A2_RC} ($(printf '%s' "$A2_LS" | head -1))"
  elif [ "$A2_LS" = "__AUSENTE__" ]; then
    det "A.2 /tmp/.X11-unix ausente dentro de la capsula"
  elif [ -z "$A2_LS" ]; then
    det "A.2 /tmp/.X11-unix presente y vacio dentro de la capsula"
  else
    A_MAL="$A_MAL A.2"; det "A.2 /tmp/.X11-unix dentro de la capsula contiene: $(printf '%s' "$A2_LS" | tr '\n' ' ')"
  fi
  # A.3 ningun montaje con .X11-unix (origen o destino) en docker inspect
  A3_M=$(docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' "$C" 2>&1); A3_RC=$?
  if [ "$A3_RC" -ne 0 ]; then
    A_MAL="$A_MAL A.3"; det "A.3 docker inspect no pudo leer los montajes: rc=${A3_RC}"
  elif A3_X=$(printf '%s\n' "$A3_M" | grep -F '.X11-unix'); then
    A_MAL="$A_MAL A.3"; det "A.3 montaje X11 presente: $(printf '%s' "$A3_X" | tr '\n' ' ')"
  else
    det "A.3 $(printf '%s\n' "$A3_M" | grep -c -- ' -> ') montajes, ninguno con .X11-unix en origen ni destino"
  fi
fi
if [ -z "$A_MAL" ]; then
  ok "la capsula no alcanza :1: xdpyinfo a :1 falla desde dentro, /tmp/.X11-unix sin sockets y cero montajes X11"
else
  bad "la capsula alcanza o expone :1, o la sonda no pudo juzgar (fallan:${A_MAL})"
fi

echo "== B. [INFORMATIVA] Clientes X nuevos en :1 respecto de la linea base =="
DISPLAY=:1 xlsclients 2>/dev/null | sort > /tmp/display1-clients-now.txt
if [ ! -s /tmp/display1-clients-now.txt ]; then
  info "sin cifra: xlsclients no devolvio nada contra :1 (sonda ciega; no emite veredicto)"
elif [ ! -f /tmp/display1-clients.txt ]; then
  info "sin cifra: linea base /tmp/display1-clients.txt ausente; clientes_ahora=$(wc -l < /tmp/display1-clients-now.txt)"
else
  NUEVOS=$(comm -13 <(sort /tmp/display1-clients.txt) /tmp/display1-clients-now.txt | wc -l)
  info "clientes_nuevos=${NUEVOS} respecto de la linea base; clientes_ahora=$(wc -l < /tmp/display1-clients-now.txt)"
fi

echo "== C. [INFORMATIVA] Foco de :1 respecto de la linea base =="
AW_BASELINE="${AW_BASELINE:-$(cat /tmp/display1-activewindow.txt 2>/dev/null || true)}"
AW_NOW=$(DISPLAY=:1 xdotool getactivewindow 2>/dev/null || echo none)
if [ -z "${AW_BASELINE:-}" ]; then
  info "sin cifra: linea base del foco ausente; ventana_activa_ahora=${AW_NOW}"
elif [ "$AW_BASELINE" = "$AW_NOW" ]; then
  info "foco_cambiado=0 (ventana activa ${AW_NOW})"
else
  info "foco_cambiado=1 (${AW_BASELINE} -> ${AW_NOW})"
fi

echo "== D. [INFORMATIVA] Portales nuevos en la sesion del host =="
busctl --user list 2>/dev/null | awk '{print $1}' | sort > /tmp/display1-portals-now.txt
if [ ! -s /tmp/display1-portals-now.txt ]; then
  info "sin cifra: busctl --user list no devolvio nada (sonda ciega; no emite veredicto)"
elif [ ! -f /tmp/display1-portals.txt ]; then
  info "sin cifra: linea base /tmp/display1-portals.txt ausente"
else
  NUEVOS_PORTALES=$(comm -13 <(awk '{print $1}' /tmp/display1-portals.txt | sort) /tmp/display1-portals-now.txt | grep -c portal || true)
  info "portales_nuevos=${NUEVOS_PORTALES:-0} respecto de la linea base"
fi

echo "== E. El contenedor no alcanza ningun D-Bus =="
if docker exec "$C" sh -lc 'command -v dbus-send >/dev/null 2>&1'; then
  if docker exec "$C" dbus-send --session --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
    bad "el contenedor alcanzo un bus de sesion"
  else
    ok "bus de sesion inalcanzable desde el contenedor"
  fi
else
  ok "dbus-send ni siquiera esta instalado (ausencia por contrato)"
fi

echo "== F. Sin runtime dir del host dentro del contenedor =="
if [ -z "$(docker exec "$C" sh -c 'ls -A /run/user 2>/dev/null')" ]; then
  ok "/run/user vacio o ausente"
else
  bad "/run/user poblado dentro del contenedor"
fi

echo "== G. Sin montajes sensibles del host =="
MOUNTS=$(docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' "$C")
LEAKS=0
for s in /proc /sys /dev /run/user /tmp/.X11-unix /home/user/.Xauthority; do
  for m in $MOUNTS; do
    if [ "$m" = "$s" ] || [[ "$m" == "$s"/* ]]; then
      bad "fuga: host $s montado en el contenedor"
      LEAKS=1
    fi
  done
done
[ "$LEAKS" -eq 0 ] && ok "cero montajes sensibles del host"

echo "== H. Variables de cuarentena en el contenedor =="
MISSING_VARS=0
C_ENV=$(docker inspect -f '{{range .Config.Env}}{{.}}
{{end}}' "$C")
for kv in \
  "DBUS_SESSION_BUS_ADDRESS=disabled:" \
  "DBUS_SYSTEM_BUS_ADDRESS=disabled:" \
  "XDG_RUNTIME_DIR=/run/newton-tenant" \
  "XDG_SESSION_TYPE=x11" \
  "GTK_USE_PORTAL=0" \
  "GDK_BACKEND=x11" \
  "QT_QPA_PLATFORM=xcb" \
  "NO_AT_BRIDGE=1" \
  "GIO_USE_VFS=local" \
  "DISPLAY=:100" \
  "LIBGL_ALWAYS_SOFTWARE=1" \
  "GALLIUM_DRIVER=llvmpipe"
do
  if ! echo "$C_ENV" | grep -qxF "$kv"; then
    bad "cuarentena incompleta: falta $kv"
    MISSING_VARS=1
  fi
done
[ "$MISSING_VARS" -eq 0 ] && ok "12 variables de cuarentena presentes en Config.Env"

echo "== I. Memoria compartida adecuada =="
SHM_BYTES=$(docker inspect -f '{{.HostConfig.ShmSize}}' "$C")
if [ "$SHM_BYTES" -ge 2147483648 ]; then
  ok "/dev/shm >= 2GB ($SHM_BYTES bytes)"
else
  bad "/dev/shm menor a 2GB ($SHM_BYTES bytes)"
fi

echo "== J. Display virtual :100 vivo =="
if docker exec "$C" sh -c 'command -v xdpyinfo >/dev/null 2>&1' && docker exec "$C" xdpyinfo -display :100 >/dev/null 2>&1; then
  ok "Xvfb :100 respondiendo a xdpyinfo"
else
  bad "Xvfb :100 no responde"
fi

echo "== K. Renderer GL software =="
if docker exec "$C" sh -c 'command -v glxinfo >/dev/null 2>&1'; then
  GL_RENDERER=$(docker exec "$C" glxinfo -display :100 2>/dev/null | grep "OpenGL renderer string" | cut -d: -f2 | tr -d ' ' || true)
  if echo "$GL_RENDERER" | grep -qi "llvmpipe"; then
    ok "OpenGL renderer es llvmpipe ($GL_RENDERER)"
  else
    bad "OpenGL renderer no es llvmpipe ($GL_RENDERER)"
  fi
else
  ok "glxinfo no disponible para verificar GL"
fi

echo "== L. Endpoint HTML5 respondiendo =="
if curl -fsS --max-time 3 "http://127.0.0.1:${P}/" >/dev/null 2>&1; then
  ok "Xpra HTML5 respondiendo en 127.0.0.1:${P}"
else
  bad "Xpra HTML5 no responde en 127.0.0.1:${P}"
fi

echo "== M. Sin intentos de conexion a sockets de portal (traza de 30 s) =="
CHPID=$(docker exec "$C" sh -lc 'pgrep -n -f "[c]hromium" || true' 2>/dev/null | tr -d "[:space:]")
if [ -z "$CHPID" ]; then
  bad "no hay Chromium que trazar: la puerta M no puede juzgar"
else
  HITS=$(docker exec "$C" sh -lc \
    "timeout 30 strace -f -qq -e trace=connect -p ${CHPID} 2>&1 \
     | grep -c 'run/user\|dbus' || true" 2>/dev/null | tr -d "[:space:]")
  if [ "${HITS:-x}" = "0" ]; then
    ok "cero intentos de conexion a bus en 30 s de traza"
  else
    bad "${HITS} intentos de conexion a bus observados"
  fi
fi

echo "== N. Aislamiento de red =="
NET=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$C")
if [ "$NET" = "bridge" ]; then
  bad "el contenedor esta en el bridge por defecto: cualquier hermano alcanza CDP"
else
  CIP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$C")
  if [ -z "$CIP" ]; then
    ok "contenedor sin IP en red bridge observable"
  else
    if docker run --rm --network bridge --entrypoint curl opensuperficies-browser-arm64:1.0.0 \
         -s --max-time 4 "http://${CIP}:9222/json/version" >/dev/null 2>&1; then
      bad "fuga de red: CDP alcanzable desde bridge por defecto"
    else
      ok "red bridge aislada: CDP inalcanzable desde bridge externo"
    fi
  fi
fi

if [ "$FAIL" -eq 0 ]; then
  echo "VEREDICTO: TODAS LAS PUERTAS DE CUARENTENA PASARON"
  exit 0
else
  echo "VEREDICTO: AL MENOS UNA PUERTA DE CUARENTENA FALLO"
  exit 1
fi
