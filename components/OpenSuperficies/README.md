# OpenSuperficies

Sustrato autónomo Docker + Xpra + CDP: cada web es DATO y el motor es INVARIANTE.
Topología B standalone (DEC-1): la cápsula contiene su Xvfb, su gestor de ventanas, su capa de streaming y su navegador; la frontera es un par de puertos de loopback.

| Propiedad | Valor Canónico |
|---|---|
| `plan_id` | `opensuperficies-v1` |
| `sustrato` | Linux aarch64 6.17.0-1018-nvidia, NVIDIA GB10, 20 núcleos, Docker 29.2.1 [MEASURED] |
| `diario` | `LOG-CAMPANA.md` (un registro JSON por turno) |
| `evidencia` | `evidencia/` (salidas crudas con firma SHA256 relativa; NO versionado, ignorado por `.gitignore`) |
| `estado` | Ola 5 en curso (Tramo U-08, U-09, U-10 completado; U-05 en cola) |

---

## Inicio Rápido en Cinco Líneas

```bash
scripts/docker-xpra-surface.sh iniciar srf-prod
scripts/docker-xpra-surface.sh verificar srf-prod
python3 motor/cdp_runner.py --fixture limpio --instancia srf-prod --informe /tmp/resultado.json
scripts/docker-xpra-surface.sh detener srf-prod
cat /tmp/resultado.json
```

---

## Estructura del Repositorio

```
OpenSuperficies/
├── adaptador/   # Adaptador de entorno de campaña sellado por sha256
├── contexto/    # Paquetes de contexto destilados de FASE 0
├── contrato/    # Esquema formal y adaptadores de sitios (U-05 pendiente)
├── docker/      # Dockerfile y scripts de soporte de la cápsula (congelado)
├── evidencia/   # Evidencias por unidad (solo local, no versionado)
├── fixture/     # Dobles de prueba locales deterministas (congelado)
├── guias/       # Documentación operativa canónica (U-08, U-09, U-10)
├── motor/       # Motor CDP crudo invariante (cdp_runner.py) (congelado)
└── scripts/     # Orquestador de superficie (docker-xpra-surface.sh) (congelado); scripts/cu/ = gates de Computer Use
```

También versionados: `src/`, `cmd/`, `cockpit/`, `vision/`, `capsule/`, `bench/`, `eval/`, `hyperbooks/`, `contracts/`. Computer Use (cubook): `docs/CU_W4.md`, `docs/CU_KNOWN_LIMITATIONS.md`, `docs/HB_OS.md`; pruebas: `QUICKSTART.md`. Licencia: AGPL-3.0-only (`LICENSE`); avisos de terceros en `THIRD_PARTY_NOTICES.md`. CI en `.github/workflows/`.

---

## Mapa de Gates de Certificación

| Gate | Descripción | Estado de Validación |
|---|---|---|
| `B-01` .. `B-02` | Construcción y firma de imagen base browser-arm64 | CONFORME (U-00) |
| `B-03` .. `B-06` | Aislamiento display `:1`, red interna, Xpra y loopback | CONFORME (U-02) |
| `B-07` .. `B-08` | Barrera de cuarentena estricta 14 puertas (A..N) | CONFORME (U-03) |
| `B-09` .. `B-11` | Bloqueo concurrente por flock y exclusión mutua | CONFORME (U-01) |
| `B-12` .. `B-14` | Orquestador de ciclo de vida y puente CDP D-U04-04 | CONFORME (U-04) |
| `B-16` .. `B-19` | Oráculo motor CDP: G-ATTACH, G-STREAM, G-COSECHA, G-HUMAN | CONFORME (U-06) |
| `B-20` | Censo estricto de cero literales prohibidos en código | CONFORME (U-06) |
| `B-21` | Invariante diff cero en motor ante onboarding de web | RESERVADO (U-05/U-07) |
| `B-23` | Guía de operación y tabla canónica de 10 códigos de salida | CONFORME (U-08) |
| `B-24` | Runbook de triaje, inventario de artefactos y árbol verificado | CONFORME (U-10) |
