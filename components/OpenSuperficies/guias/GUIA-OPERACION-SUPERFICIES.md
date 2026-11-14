# Guía Canónica de Operación de Superficies Aisladas

## 1. Ciclo Diario de Operación

La operación de una cápsula de superficie aislada sigue una secuencia determinista para garantizar que ninguna actividad contamine la sesión del operador ni fugue datos fuera de la frontera de aislamiento:

1. **Captura de Línea Base**: Antes de levantar cualquier cápsula, se sondea el display `:1` del operador para registrar su estado libre de contaminación:
   ```bash
   /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh estado srf-operacion
   ```
2. **Inicio de Cápsula (`iniciar`)**: Crea el contenedor Docker `newton-srf-<instancia>`, su red bridge privada `newton-srf-<instancia>`, su display Xvfb interno y el servidor Xpra:
   ```bash
   /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh iniciar srf-operacion
   ```
3. **Verificación de Cuarentena (`verificar`)**: Ejecuta las 14 puertas de cuarentena (A..N) del estándar G-QUARANTINE-14, re-ancladas por R-24: **11 con veredicto** (A, E..N) y **3 informativas** (B, C, D). La puerta A prueba el mecanismo desde dentro de la cápsula —`DISPLAY=:1 xdpyinfo` debe fallar, `/tmp/.X11-unix` ausente o vacío y cero montajes `.X11-unix`— y cae en `[FAIL]` si la cápsula no está viva o la sonda no puede correr. B, C y D publican con prefijo `[INFO]` su cifra (clientes nuevos, cambio de foco, portales nuevos) pero no emiten veredicto: observan el display `:1` que el operador usa en vivo y se pondrían rojas por su trabajo normal. El `VEREDICTO` y el código de salida se calculan solo con las 11 de veredicto:
   ```bash
   /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh verificar srf-operacion
   ```
4. **Intervención del Operador (`intervenir`)**: Expone el visor HTML5 en el puerto asignado para resolver desafíos interactivos (Cloudflare/Turnstile) o autenticación de sesión:
   ```bash
   /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh intervenir srf-operacion
   ```
5. **Blindaje Post-Intervención (`blindar`)**: Cierra la exposición del visor tras la resolución humana, restituyendo el aislamiento estricto:
   ```bash
   /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh blindar srf-operacion
   ```
6. **Consulta Automatizada (`consultar`)**: El motor CDP interactúa con el navegador dentro del contenedor enviando el prompt y cosechando los resultados:
   ```bash
   NEWTON_SELECTORS=contrato/adaptador.<sitio>.json python3 /home/user/OpenSuperficies/motor/cdp_runner.py consultar --puerto 9222 --url "https://<sitio>" --prompt "Consulta de produccion" --destino /tmp/cosecha-operacion.md
   ```
   La variable de entorno `NEWTON_SELECTORS` especifica la ruta al archivo del adaptador de selectores de la superficie objetivo, y la bandera `--url` define la dirección web de destino a consultar.
7. **Detención Limpia (`detener`)**: Desmonta los procesos, elimina el contenedor y destruye la red privada:
   ```bash
   /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh detener srf-operacion
   ```

Los siete verbos canónicos implementados en `scripts/docker-xpra-surface.sh` son: `iniciar`, `intervenir`, `blindar`, `estado`, `verificar`, `purgar`, `detener` [MEASURED].

---

## 2. Taxonomía Canónica de Códigos de Salida (§5)

El motor CDP (`motor/cdp_runner.py`) y el orquestador de sustrato aplican estrictamente la siguiente taxonomía de diez códigos de salida. Cada código define inequívocamente si admite o prohíbe el reintento automático:

| Código | Identificador | Significado Operativo | Reintento Automático | Acción Operativa Recomendada |
|---|---|---|---|---|
| 0 | `RC_OK` | Ejecución completada satisfactoriamente con cosecha íntegra y validada. | No | Consumir el archivo de cosecha generado en el destino especificado. |
| 1 | `RC_LIMPIO` | Fallo limpio en etapa previa; NADA se envió a la plataforma remota. | Sí | Corregir parámetros de entrada, nombre de archivo o reintentar. |
| 2 | `RC_USO` | Error de sintaxis o uso en los argumentos de la línea de órdenes. | No | Corregir la invocación CLI de acuerdo con el subcomando ejecutado. |
| 3 | `RC_SUSTRATO` | Fallo en sustrato local (CDP inaccesible, contenedor caído o adaptador ausente). | Sí | Verificar Docker, comprobar existencia de adaptador o usar `--fixture`. |
| 4 | `RC_SIN_FIN` | Prompt enviado; el gate de fin no vio el cierre formal (cosecha parcial). | No | Triaje humano: inspeccionar el archivo parcial antes de cualquier acción. |
| 5 | `RC_OCUPADA` | Instancia en uso o retenida por otro proceso consumidor. | Sí | Esperar liberación del token bucket o seleccionar otra instancia. |
| 6 | `RC_AMBIGUO` | Envío ambiguo (G-SUBMIT timeout): pudo enviarse y cobrarse cuota remota. | JAMÁS | Sellar `needs_human`. Prohibido reintentar para evitar doble cargo. |
| 7 | `RC_HUMANO` | Detección de desafío interactivo (Cloudflare, CAPTCHA, Turnstile) o login. | JAMÁS | Invocar `intervenir`, resolver manualmente en el visor, y `blindar`. |
| 8 | `RC_ABORTADA` | Operación cancelada por timeout de inactividad o señal de terminación externa. | No | Evaluar causa del timeout o límite de presupuesto temporal. |
| 9 | `RC_NOAUTH` | Perfil virgen sin sesión activa en servicio que exige autenticación. | No | Intervenir para iniciar sesión en la plataforma y persistir perfil. |

---

## 3. Protocolo de Sellado `needs_human` y Traspaso de Control

Cuando el motor emite `rc 6` (envío ambiguo) o `rc 7` (desafío interactivo), el proceso automatizado se detiene de forma inmediata:
- **Invariante de Sesión**: La sesión no se destruye. La cápsula se mantiene en ejecución en su estado exacto para permitir la inspección forense o la resolución interactiva.
- **Procedimiento de Traspaso**:
  1. El orquestador registra el incidente en el diario operativo y emite una notificación de advertencia.
  2. El operador ejecuta `scripts/docker-xpra-surface.sh intervenir <instancia>`, lo que abre temporalmente el puerto 10000 en loopback.
  3. El operador se conecta mediante su navegador local a `http://127.0.0.1:10000/` y resuelve manualmente el desafío visual.
  4. Una vez resuelto, el operador ejecuta `scripts/docker-xpra-surface.sh blindar <instancia>` para restaurar la cuarentena.

---

## 4. Destino de la Cosecha y Separación de Flujos

- La cosecha generada por el motor se deposita por defecto en rutas explícitas temporales (e.g., `/tmp/cosecha-<instancia>.md`).
- **Aislamiento de Campaña**: La cosecha jamás se escribe directamente dentro de los árboles de salida o repositorio de la campaña sin validación previa. Esto previene la contaminación de artefactos versionados con payloads no auditados.

---

## 5. Procedimiento de Purga tras Cierre Sucio

Si una sesión termina de forma anómala (e.g., interrupción abrupta de energía o muerte de proceso sin teardown), los recursos huérfanos pueden bloquear ejecuciones posteriores (e.g., colisión de lock o error `rc 69`):
1. **Ejecutar Purga de Instancia**:
   ```bash
   /home/user/OpenSuperficies/scripts/docker-xpra-surface.sh purgar srf-operacion
   ```
2. **Verificar Ausencia de Residuos**:
   ```bash
   docker ps -a --filter "name=newton-srf"
   ```

---

## 6. Adjuntos: el nombre del fichero debe ser corto (HAL-U13-37)

**Síntoma**: el motor devuelve `rc 1` con `G-ATTACH rojo, NADA se envio: ... el chip '<nombre>' nunca se asento`, aunque el
fichero sí se subió.

**Causa medida** (Perplexity, 2026-09-13, cápsula de 1280x900 con `innerWidth` 1271):
- La plataforma recorta el nombre del chip en el texto visible. `especificacion_aislamiento_sustrato.txt` (39 caracteres)
  aparece en `document.body.innerText` como `especificacion_aislamie...txt`.
- El nombre completo solo está en los atributos `title` y `aria-label`.
- G-ATTACH exige el nombre completo dentro de `innerText`, así que con ese nombre nunca se pone verde, aunque el chip esté
  asentado (7.9 KB, sin progreso pendiente, sin errores).

**Norma operativa**:
- **Nombre del adjunto de 15 caracteres o menos, extensión incluida**. Medido: `aislamiento.txt` (15) da G-ATTACH verde
  en menos de 1 s y cosecha completa.
- El umbral exacto está entre 16 y 38 caracteres y **no está medido**: un nombre más largo se prueba antes a cuota cero.
- No hace falta cambiar el contenido: copiar el fichero con otro nombre conserva su `sha256`, y la autorización del
  operador liga el sha del contenido, no el nombre.

**Prueba a cuota cero** (sube el fichero y comprueba G-ATTACH sin enviar ningún prompt), y limpieza posterior:
```bash
NEWTON_SELECTORS=contrato/adaptador.perplexity.json python3 -B motor/cdp_runner.py adjuntar --puerto <CDP> --fichero /uploads/<nombre> --nombre <nombre>
```
- `rc 0` con `G-ATTACH verde` significa que el nombre es apto.
- Antes de consultar hay que recargar la portada de la cápsula para retirar el chip de prueba; si no, la consulta llevaría
  dos adjuntos.
