# Guía Canónica de Concurrencia y Capacidad de Superficies

## 1. Mapa de Puertos y Fronteras de Red

Cada cápsula de superficie aislada se ejecuta vinculada a su propio par de puertos loopback en el host:
- **Rango de Visor Xpra (HTML5)**: `10020` a `10099` (máximo 80 instancias numeradas).
- **Rango de Depuración CDP (Chromium)**: `9220` a `9299` (mapeado 1:1 con el offset de instancia).

Cada instancia se ejecuta sobre su propia red bridge privada de Docker (`newton-srf-<instancia>`), desacoplada completamente de la red bridge por defecto.

---

## 2. Diagnóstico de Exposición y Modelo de Amenaza de Red

De acuerdo con la medición empírica realizada en REP-16 y ratificada por la auditoría de Turno 9:
- **Comportamiento Interno**: Desde un contenedor vecino conectado a la red privada de la instancia (`newton-srf-<instancia>`), el puerto `9222` de Chromium es alcanzable por TCP. Su única protección es la comprobación de cabecera `Host` de Chromium, la cual se sortea enviando `Host: localhost`. El visor HTML5 en el puerto `10000` responde con HTTP `200` sin ninguna guarda de autenticación [MEASURED 2026-09-10T23:44, REP-16].
- **Aislamiento Externo**: Desde el bridge por defecto de Docker o desde la red pública del host, ambos puertos resultan inalcanzables.
- **Gobernanza**: Esta brecha residual interna se encuentra formalmente documentada bajo el requerimiento `R-21` (incorporación de la futura Puerta O / G-QUARANTINE-15 previa al sellado definitivo). No se suaviza ni se omite.

---

## 3. Modelo de Concurrencia: Token Bucket por `flock`

La concurrencia de procesamiento está desacoplada del número de contenedores en reposo:
- **Instancias Residentes Ociosas**: Pueden existir múltiples instancias en memoria listas para recibir comandos sin consumir tiempo de CPU activo una vez asentadas.
- **Token Bucket de Pintado Activo**: El renderizado dinámico de Chromium impone una carga computacional intensiva. Se limita estrictamente a un máximo de **CUATRO (4)** slots de pintado simultáneo mediante un mecanismo de exclusión mutua basado en `flock`:
  - En un host de 20 núcleos físicos ARMv9 (NVIDIA GB10) con `--cpus=5` por cápsula (`HostConfig.NanoCpus = 5000000000` [MEASURED]), 4 slots de pintado simultáneo alcanzan 20 de los 20 núcleos físicos en saturación completa, consumiendo hasta el 100% de la capacidad pico del host sin margen excedente si los 4 saturan a la vez [MEASURED 2026-09-11].
  - Se prohíbe terminantemente autorizar o documentar más de 4 slots de pintado simultáneo en esta topología.

---

## 4. Techos de Capacidad y Recursos Medidos

### Unidades de Medida y Parámetros de Sustrato
- `docker stats` expresa el porcentaje de CPU como `100.0%` por cada núcleo físico completo utilizado.
- Con la configuración `--cpus=5` asignada a cada contenedor (`HostConfig.NanoCpus = 5000000000` [MEASURED]), una cápsula saturando sus 5 núcleos asignados alcanza un máximo teórico de `500.0%`.
- El límite configurado de memoria por contenedor es de `4.0 GiB` (`HostConfig.Memory = 4294967296` bytes [MEASURED 2026-09-11]).

### Tabla de Techos y Métricas Empíricas
Las siguientes métricas reflejan las tomas directas registradas en `evidencia/u09/stats-ociosa.txt` y `evidencia/u09/stats-pintando.txt`:

| Métrica de Capacidad | Valor Empírico o Techo | Estado de Calibración | Justificación y Fuente de la Cifra |
|---|---|---|---|
| CPU Cápsula Ociosa Asentada (60s reposo) | < 2.5% de un núcleo | [MEASURED 2026-09-11] | Media de 3 muestras en reposo tras asentamiento en `stats-ociosa.txt` (0.32%, 0.32%, 2.40%). |
| CPU Cápsula Pintando en Stream Activo | 1.0% - 73.0% de un núcleo (fixture) | [MEASURED 2026-09-11] | 5 muestras capturadas durante el streaming en `stats-pintando.txt` (pico de 72.83%). |
| Carga de Renderizado frente a Cita de 513% | No comparables directamente: suelo 72.83% frente a cita 513% | [MEASURED 2026-09-11] / [CITED] | El pico de 72.83% sobre fixture local estático representa un suelo de ejecución [MEASURED 2026-09-11]; la cita de 513% corresponde a renderizado multi-hilo en sitio real y permanece [CITED] pendiente de medición en U-05. |
| RAM Consumida por Cápsula Ociosa | 540 MiB - 550 MiB | [MEASURED 2026-09-11] | Uso real de memoria reportado por `docker stats` en `stats-ociosa.txt` (542 MiB). |
| RAM Consumida por Cápsula en Cosecha | 500 MiB - 540 MiB | [MEASURED 2026-09-11] | Uso real de memoria reportado por `docker stats` en `stats-pintando.txt` (507-538 MiB). |
| Límite Superior de Memoria Configurada | 4.0 GiB por cápsula | [MEASURED 2026-09-11] | `HostConfig.Memory` verificado en `tabla.txt`. |
| Techo Máximo de Cápsulas Ociosas en RAM | 16 cápsulas simultáneas | [TARGET] | Estimación conservadora basada en 78 GiB libres y ~550 MiB por cápsula. |
| Techo Máximo de Slots de Pintado Activo | CUATRO (4) slots concurrentes | [TARGET] | Límite estricto de exclusión mutua para no sobrepasar los 20 núcleos del host. |

> **Advertencia de Saturación**: Con `--cpus=5` por cápsula, CUATRO slots de pintado equivalen a 20 de los 20 núcleos del host en saturación real. El límite de cuatro no deja margen si las cuatro saturan a la vez; deja margen solo mientras la carga real se parezca a la medida.

---

## 5. Procedimiento de Re-medición y Caducidad

Las cifras de capacidad publicadas en este documento poseen una vigencia máxima de noventa (90) días. Cualquier actualización mayor en el kernel del host (`6.17.0`), la versión de Docker (`29.2.1`) o la versión base del motor Chromium (`152`) invalida las garantías y exige ejecutar el oráculo de re-calibración `O-U09`.
