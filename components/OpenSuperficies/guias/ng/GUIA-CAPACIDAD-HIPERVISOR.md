# Capacity Guide: Sovereign Surface Hypervisor (srfd daemon and cockpit)

Scope: how many capsules the shipped daemon will admit and why, using only what is on disk on 2026-09-20. Companion guides: `guias/ng/GUIA-OPERACION-HIPERVISOR.md` (operation, exit-code table) and `guias/ng/RUNBOOK-INCIDENTES-HIPERVISOR.md` (incidents, known limits). The frozen v1 capacity guide `guias/GUIA-CONCURRENCIA-Y-CAPACIDAD.md` is read-only input and describes the v1 Docker CLI capsule.

Rules of this guide. Every sentence and every table row that states a capacity, memory or slot figure carries a tag. `[MEASURED <date>]` is a figure read from a captured run of this session (raw output in `evidencia/v2/u22/comandos-runbook.txt`, the log line is cited) or from a dated measurement artifact that is cited. `[CITED]` is a figure quoted from a document or a source comment and not measured here. `[TARGET]` is a constant coded in source, a design value and not a measurement. `AUSENTE` marks a figure the tree does not contain. A simulation is arithmetic on the inputs it is given; it is never presented as a load test. Every `file:line` citation is re-read by `evidencia/v2/u22/verificar-guias.sh`, which also recomputes the arithmetic of this guide and checks that every figure sentence or row carries a tag (operation guide, section 10).

## 1. Slots and ports

The port plan gives every slot a pair: Xpra `10020 + slot`, CDP `9220 + slot`, for 80 slots [TARGET] (`src/displaybook/puertos/puertos.go:13`, `src/displaybook/puertos/puertos.go:14`, `src/displaybook/puertos/puertos.go:15`). Slot 0 gave 10020/9220 (`evidencia/v2/u22/comandos-runbook.txt:75`), slot 79 gave 10099/9299 (`evidencia/v2/u22/comandos-runbook.txt:91`), and slot 80 was refused with `limite n=80` (`evidencia/v2/u22/comandos-runbook.txt:95`) [MEASURED 2026-09-20]. The census counts listeners inside the windows 10020 to 10099 and 9220 to 9299 [TARGET] (`src/surfacebook/manager/mediacion.go:38`, `src/surfacebook/manager/mediacion.go:40`). A capsule is named `srf-ng-<slot>` (`src/surfacebook/manager/manager.go:139`).

## 2. The admission governor (what really limits the count)

Every start passes the governor before anything is launched (`src/surfacebook/manager/manager.go:242`). It is a memory-first predicate: admitted only when free memory minus the growth of reservations minus the new reservation still leaves at least the sovereign reserve (`src/orchestratorbook/gobernador/gobernador.go:231`, `src/orchestratorbook/gobernador/gobernador.go:232`). Its constants, all design values in source and not measurements:

| Constant | Value | Tag | Source |
|---|---|---|---|
| sovereign memory reserve | 24 GiB | [TARGET] | `src/orchestratorbook/gobernador/gobernador.go:174` |
| CPU pressure threshold (`some avg10`) | 20 % | [TARGET] | `src/orchestratorbook/gobernador/gobernador.go:175` |
| CDP latency SLA | 250 ms | [TARGET] | `src/orchestratorbook/gobernador/gobernador.go:176` |
| hard ceiling of live capsules | 24 | [TARGET] | `src/orchestratorbook/gobernador/gobernador.go:177` |
| active MicroVMs at most | 2 | [TARGET] | `src/orchestratorbook/gobernador/gobernador.go:178` |

Refusal reasons, in the order the predicate meets them: `candado` (`src/orchestratorbook/gobernador/gobernador.go:211`), `techo` (`src/orchestratorbook/gobernador/gobernador.go:214`), `techo_microvm` (`src/orchestratorbook/gobernador/gobernador.go:217`), `telemetria` (fail-closed when any reading fails, `src/orchestratorbook/gobernador/gobernador.go:224`), `ram` (`src/orchestratorbook/gobernador/gobernador.go:233`), `cpu` (`src/orchestratorbook/gobernador/gobernador.go:237`) and `latencia` (`src/orchestratorbook/gobernador/gobernador.go:242`). Until 32 latency samples exist the latency rule refuses only when 3 or more capsules are already live [TARGET] (`src/orchestratorbook/gobernador/gobernador.go:240`). The daemon reports a refusal as rc 1 with the text `gobernador: admission denied: <reason>` (`src/surfacebook/manager/manager.go:247`), so the reason is in the `error` field, not in the rc.

Live inputs. Free memory is `MemAvailable` from `/proc/meminfo` (`src/orchestratorbook/gobernador/gobernador.go:54`) and CPU pressure comes from `/proc/pressure/cpu` (`src/orchestratorbook/gobernador/gobernador.go:79`); nothing is cached. The governor adds up the CPUs of admitted capsules (`src/orchestratorbook/gobernador/gobernador.go:249`) but no admission rule compares that sum with a ceiling: CPU is gated only by the pressure threshold. The daemon charges 1.5 CPUs per start [TARGET] (`src/surfacebook/manager/manager.go:243`).

## 3. Reservation per class

The daemon reserves memory for 6 pages of a browser [TARGET] (`src/surfacebook/manager/manager.go:242`).

| Class | Reservation | Rule | Tag | Source |
|---|---|---|---|---|
| browser (6 pages) | 2,112 MiB | `64 * ceil((733 + 183*P + 256) / 64)` | [TARGET] | `src/orchestratorbook/gobernador/estimador.go:14` |
| desktop | 2,048 MiB | constant | [TARGET] | `src/orchestratorbook/gobernador/estimador.go:21` |
| microvm | 4,352 MiB | guest RAM 4,096 MiB default plus 256 MiB VMM overhead | [TARGET] | `src/orchestratorbook/gobernador/estimador.go:28`, `src/orchestratorbook/gobernador/estimador.go:30` |

The source comment states the browser figure for 6 pages: 2,112 MiB, against a 1,646 MiB measured footprint of the earlier capsule (`src/orchestratorbook/gobernador/estimador.go:8`) [CITED; no re-measurement in this session]. The arithmetic is recomputed by the checker: with P = 6 the rule gives 64 * ceil(2,087 / 64) = 2,112 MiB and 4,096 + 256 = 4,352 MiB [TARGET]. The estimator also has a terminal rule, but `terminal` is not a class the daemon accepts (`src/surfacebook/manager/manager.go:38`), so it never reaches admission.

## 4. Admission arithmetic, simulated

`bin/srf-gobernador simular` runs the same predicate with the memory you give it (`src/orchestratorbook/gobernador/gobernador.go:380`). These simulations, raw output in `evidencia/v2/u22/comandos-runbook.txt`, prove the arithmetic and not throughput [MEASURED 2026-09-20]. Committed memory is admitted times the unit reservation, and the final slack is available memory minus committed memory; both are recomputed by the checker from each JSON line.

| Request | Available MiB | Admitted | Denied | Committed MiB | Final slack MiB | Tag | Source line |
|---|---|---|---|---|---|---|---|
| 24 browsers | 80,400 | 24 | 0 | 50,688 | 29,712 | [MEASURED 2026-09-20] | `evidencia/v2/u22/comandos-runbook.txt:79` |
| 25 browsers | 80,400 | 24 | 1 | 50,688 | 29,712 | [MEASURED 2026-09-20] | `evidencia/v2/u22/comandos-runbook.txt:99` |
| 24 browsers | 60,000 | 16 | 8 | 33,792 | 26,208 | [MEASURED 2026-09-20] | `evidencia/v2/u22/comandos-runbook.txt:103` |
| 24 desktops | 80,400 | 24 | 0 | 49,152 | 31,248 | [MEASURED 2026-09-20] | `evidencia/v2/u22/comandos-runbook.txt:107` |
| 24 microvms | 80,400 | 2 | 22 | 8,704 | 71,696 | [MEASURED 2026-09-20] | `evidencia/v2/u22/comandos-runbook.txt:111` |
| 24 browsers | 49,228 | 11 | 13 | 23,232 | 25,996 | [MEASURED 2026-09-20] | `evidencia/v2/u22/comandos-runbook.txt:128` |

Reading the rows. The 25th browser is refused by the ceiling of 24, not by memory: 25 reservations of 2,112 MiB would still leave 27,600 MiB of the 80,400 MiB, above the 24,576 MiB reserve (recomputed by the checker) [MEASURED 2026-09-20]. The figure 80,400 MiB is the top of the range the pack recorded on 2026-09-16 (`contexto/PAQUETE-ENRIQUECIMIENTO-CATALOGOS-OPENSUPERFICIES-V2.md:122`) [CITED]. The last row uses the host's live `MemAvailable` of 50,409,380 kB read at 2026-09-20T04:47:35Z (`evidencia/v2/u22/comandos-runbook.txt:116`) [MEASURED 2026-09-20], which is 49,227.9 MiB and was rounded to 49,228 MiB for the simulation. On that reading the predicate admits 11 browsers, not 24 [MEASURED 2026-09-20]: available memory was 80,400 MiB at the top of the pack's 2026-09-16 range and 49,228 MiB on 2026-09-20, which is why the daemon reads it on every start.

## 5. Host facts the plan is built on

The host has 124,609 MiB of RAM, with 76,484 to 80,400 MiB available within one day (`contexto/PAQUETE-ENRIQUECIMIENTO-CATALOGOS-OPENSUPERFICIES-V2.md:122`) [CITED, measured 2026-09-16 by the pack]. CPUs: the ten X925 cores are 5 to 9 and 15 to 19, the ten A725 cores are 0 to 4 and 10 to 14 (`contexto/PAQUETE-ENRIQUECIMIENTO-CATALOGOS-OPENSUPERFICIES-V2.md:118`) [CITED, measured 2026-09-16 by the pack]. MicroVM vCPUs are pinned to X925 (`PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md:354`); the MicroVM guest boot is pending U-24 in the plan, so no MicroVM runtime figure exists here (`AUSENTE`).

## 6. v1 capsule figures, quoted with their dates

These were measured on the v1 Docker CLI capsule on 2026-09-11 [CITED], not on the v2 capsule. They are quoted, not re-measured, and their own 90-day validity ends on 2026-12-10, which is 2026-09-11 plus 90 days and is recomputed by the checker (`guias/GUIA-CONCURRENCIA-Y-CAPACIDAD.md:59`).

| Quantity | v1 figure | Tag in v1 | Source |
|---|---|---|---|
| idle capsule CPU after settling | below 2.5 % of one core | `[MEASURED 2026-09-11]` | `guias/GUIA-CONCURRENCIA-Y-CAPACIDAD.md:44` |
| idle capsule RAM | 540 to 550 MiB | `[MEASURED 2026-09-11]` | `guias/GUIA-CONCURRENCIA-Y-CAPACIDAD.md:47` |
| configured memory limit | 4.0 GiB per capsule | `[MEASURED 2026-09-11]` | `guias/GUIA-CONCURRENCIA-Y-CAPACIDAD.md:49` |
| idle capsules in RAM | 16 | `[TARGET]` | `guias/GUIA-CONCURRENCIA-Y-CAPACIDAD.md:50` |
| simultaneous painting slots | 4 | `[TARGET]` | `guias/GUIA-CONCURRENCIA-Y-CAPACIDAD.md:51` |

The v1 "4 painting slots" ceiling was a `flock` token bucket of the v1 script [CITED]. The v2 daemon has no such bucket: its concurrency ceiling is the governor's 24 live capsules [TARGET] (`src/orchestratorbook/gobernador/gobernador.go:177`), and no v2 measurement shows that 24 capsules can paint at once. That is `AUSENTE`.

## 7. Cockpit-side capacity

The cockpit refreshes status every 5000 ms [TARGET] (`cockpit/frontend/src/App.svelte:14`). The stack keeps every card mounted (`cockpit/frontend/src/lib/stack/PilaMultislot.svelte:5`) and the stack test mounts 24 cards [TARGET] (`cockpit/frontend/tests/stack.test.ts:525`); the drawer allows 56 interactive terminals and 2,000 lines of scrollback each [TARGET] (`cockpit/frontend/src/lib/terminal/termguard.ts:23`, `cockpit/frontend/src/lib/terminal/termguard.ts:21`). The drawer's 2 MiB high-water mark per terminal is a `[TARGET]` of a client with no server behind it (`cockpit/frontend/src/lib/terminal/wsstream.ts:40`). These are limits of components; the shipped shell composes none of them (operation guide, section 9).

## 8. What is not measured (AUSENTE)

- No daemon-level load figure (`AUSENTE`): start latency with 24 live capsules, or `srf` call latency under load. No daemon was started for this guide (no background processes were allowed).
- No v2 CPU or RAM per capsule (`AUSENTE`); only the v1 dates above and the governor's design reservation.
- No MicroVM runtime figure (`AUSENTE`; guest boot pending U-24, `PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md:354`).
- No measured cost of the cockpit itself (`AUSENTE`): memory or frame time.

## 9. How to re-measure

1. `grep -E '^(MemTotal|MemAvailable):' /proc/meminfo` and note the time.
2. `bin/srf-gobernador simular --n 24 --clase browser --paginas 6 --mem-disponible-mib <MemAvailable in MiB> --json` (the form of invariant INV-10, `PLAN-CAMPANA-OPENSUPERFICIES-V2-NEXTGEN.md:379`).
3. `bin/srf-puertos par --slot <n> --json` for the ports of a slot.
4. `scripts/ng/campana.sh residuos` after any load test; it must exit 0 (`scripts/ng/campana.sh:13`).
