# Runbook de Respuesta a Incidentes de Superficies Aisladas

## 1. Matriz de Triaje Operativo de Incidentes

| N° | Síntoma Observado | Causa Raíz Probable | Acción Correctiva Inmediata | Reintento Automático |
|---|---|---|---|---|
| 1 | Salida `rc 69` (`flock` fallido al iniciar cápsula). | Perfil bloqueado por otro contenedor activo o terminación sucia. | Ejecutar `scripts/docker-xpra-surface.sh purgar <instancia>`. | Sí |
| 2 | Salida `rc 6` (`RC_AMBIGUO` en `enviar`). | Timeout en gate de submit; el prompt pudo enviarse y consumir cuota. | Sellar `needs_human`; notificar al operador; no destruir la cápsula. | JAMÁS |
| 3 | Salida `rc 7` (`RC_HUMANO` en `gate_desafio`). | Desafío interactivo en pantalla (Cloudflare Turnstile, CAPTCHA). | Ejecutar `intervenir <instancia>`, resolver visualmente y `blindar`. | JAMÁS |
| 4 | Salida `rc 3` (`RC_SUSTRATO` en motor). | Endpoint CDP inaccesible o archivo de adaptador no encontrado. | Comprobar contenedor Docker y verificar `contrato/adaptador.*.json`. | Sí |
| 5 | Salida `rc 1` (`RC_LIMPIO` en subida de archivo). | Degradación de adjunto a texto plano (e.g. `paste.txt`) o input ausente. | Comprobar integridad del payload en uploads y formato del fichero. | Sí |
| 6 | Salida `rc 4` (`RC_SIN_FIN` en vigilancia de stream). | Interrupción del stream o agotamiento del budget sin acciones de fin. | Triage manual: inspeccionar `/tmp/cosecha-<instancia>.md` escrito. | No |
| 7 | Contenedor en estado `Created` (oculto a `docker ps`). | Falla en comando de arranque o colisión de red (hallazgo HAL-U04-3). | Ejecutar `docker ps -a --filter name=newton-srf` y `docker rm -f`. | Sí |
| 8 | Colisión de display X11 (`:1` ocupado por operador). | Contaminación del display base del host o script invocando display 1. | Verificar display libre mediante `xdpyinfo -display :N` antes de bind. | Sí |
| 9 | Fuga de Cuarentena (Puertas A, B, C o D rojas). | Filtración de ventanas o focos hacia la sesión activa del operador. | Detención forzosa de la cápsula; auditar Xpra y reglas de aislamiento. | No |
| 10 | Procesos huérfanos de `cdp_runner.py` en host. | Terminación anormal del orquestador padre sin señal de interrupción. | Localizar con `pgrep -f cdp_runner.py` y finalizar limpiamente. | Sí |
| 11 | Literal caduco (selectores DOM modificados remotamente). | Actualización de interfaz en el sitio web de terceros. | Re-calibrar selectores y actualizar el ADAPTADOR, JAMÁS el motor. | No |
| 12 | Agotamiento de memoria o CPU excesivo en host. | Pérdida de control en renderizado de Chromium o bucle JS infinito. | Limitar pids.max y memory.max en cgroup de la cápsula y detener. | No |
| 13 | Fallo de resolución de nombres en curl desde bridge. | Red bridge por defecto no resuelve hostnames de redes privadas. | Comportamiento esperado: usar IP directa de la cápsula para sondeos. | No |
| 14 | Error de montura de volumen en directorio uploads. | Permisos de lectura/escritura denegados en `staging/superficies/`. | Restablecer permisos de usuario `admin:admin` en directorio uploads. | Sí |

---

## 2. Límites de Cobertura y Fuera de Alcance

Este runbook NO cubre:
- Recuperación de credenciales corporativas o restablecimiento de contraseñas en sitios de terceros.
- Modificación del motor CDP invariante para eludir contramedidas anti-bot comerciales.
- Operaciones en entornos fuera de la arquitectura Topología B de OpenSuperficies.
