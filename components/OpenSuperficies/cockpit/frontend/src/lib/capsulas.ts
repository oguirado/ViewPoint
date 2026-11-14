// The srfd table (`listar`) as the views read it. One pure mapping per consumer, so App.svelte carries no
// projection logic and tests/shell.test.ts can pin every rule without a DOM. Nothing here guesses: a field srfd
// does not send stays 0 (load, queue depth, latency), a state it does not name is OFFLINE / CRITICAL, never healthy.

import type { InfoSuperficieSrfd, RecordingSession, RecordingState } from './types/hypervisor';
import type { CapsuleSlot } from './components/CockpitMasterView.svelte';
import type { CapsulaWorkspace } from './workspace/WorkspaceMatrix.svelte';

/** Port contract of hypervisor.ts (SurfaceCapsule.xpraPort, SurfaceCapsule.cdpPort), the fallback when srfd omits a port. */
export const PUERTO_XPRA_BASE = 10020;
export const PUERTO_CDP_BASE = 9220;
/** manager.go: the lifecycle states of a mediated slot. */
export const ESTADO_INICIANDO = 'iniciando';
export const ESTADO_LISTO = 'listo';
export const ESTADO_DETENIENDO = 'deteniendo';
export const ESTADO_HUERFANA = 'huerfana';
/** srfd slots are uint16; the shell provisions in this window (spec 7.1: 16 slots per column). */
export const SLOTS_MAX = 16;

const MIB = 1 << 20;

/** Stable id of a surface: srfd's own when it sends one, else the port-contract name `srf-ng-<slot>`. */
export function idDeSuperficie(info: Pick<InfoSuperficieSrfd, 'id' | 'slot'>): string {
  return info.id !== undefined && info.id !== '' ? info.id : `srf-ng-${info.slot}`;
}

/** Isolation tier of a class (manager.go clasesValidas: browser and desktop are containers, microvm is KVM). */
export function nivelDeClase(clase: string): CapsuleSlot['tier'] {
  if (clase === 'microvm') return 'KVM HARDWARE';
  if (clase === 'browser' || clase === 'desktop') return 'DOCKER L2';
  return 'PTY JAULA';
}

/** Health of a lifecycle state; a state this module does not know is OFFLINE, not healthy. */
export function saludDeEstado(estado: string): CapsuleSlot['health'] {
  if (estado === ESTADO_LISTO) return 'HEALTHY';
  if (estado === ESTADO_INICIANDO || estado === ESTADO_DETENIENDO) return 'WARN';
  if (estado === ESTADO_HUERFANA) return 'CRIT';
  return 'OFFLINE';
}

/** manager.SurfaceInfo.Reserva is bytes (gobernador.EstimarReservaBytes); the views show MiB. */
export function reservaMib(info: Pick<InfoSuperficieSrfd, 'reserva'>): number {
  return Math.round((info.reserva ?? 0) / MIB);
}

/** A cockpit slot (VIEW-01 CapsuleSlot) of one srfd surface. Load, queue depth and latency are not in `listar`: 0. */
export function ranuraDeSuperficie(info: InfoSuperficieSrfd): CapsuleSlot {
  const salud = saludDeEstado(info.estado);
  return {
    id: idDeSuperficie(info),
    name: `${info.clase} · slot ${info.slot}${info.contenedor !== undefined && info.contenedor !== '' ? ` · ${info.contenedor}` : ''}`,
    slot: info.slot,
    tier: nivelDeClase(info.clase),
    mode: salud === 'HEALTHY' ? 'OPERATOR-CONTROL' : 'READ-ONLY',
    health: salud,
    queueDepth: 0,
    latencyMs: 0,
    cpuPercent: 0,
    memoryMb: reservaMib(info),
    cdpPort: info.puerto_cdp ?? PUERTO_CDP_BASE + info.slot,
    xpraPort: info.puerto_xpra ?? PUERTO_XPRA_BASE + info.slot,
  };
}

const TIER_MATRIZ: Readonly<Record<CapsuleSlot['tier'], CapsulaWorkspace['isolation_tier']>> = {
  'DOCKER L2': 'DOCKER_L2',
  'KVM HARDWARE': 'KVM_HARDWARE',
  'PTY JAULA': 'PTY_JAULA',
};
const SALUD_MATRIZ: Readonly<Record<CapsuleSlot['health'], CapsulaWorkspace['health_status']>> = {
  HEALTHY: 'HEALTHY',
  WARN: 'WARNING',
  CRIT: 'CRITICAL',
  OFFLINE: 'CRITICAL',
};

/** A workspace card (VIEW-04 CapsulaWorkspace) of one srfd surface; the screen is the matrix's to assign (INV-PANTALLA-02). */
export function capsulaWorkspaceDeSuperficie(info: InfoSuperficieSrfd, pantalla = ''): CapsulaWorkspace {
  const salud = saludDeEstado(info.estado);
  return {
    id: idDeSuperficie(info),
    title: `${info.clase} · slot ${info.slot}`,
    pantalla,
    isolation_tier: TIER_MATRIZ[nivelDeClase(info.clase)],
    health_status: SALUD_MATRIZ[salud],
    telemetry: { cpu_pct: 0, memory_mb: reservaMib(info) },
    ports: { cdp: info.puerto_cdp ?? null, xpra: info.puerto_xpra ?? PUERTO_XPRA_BASE + info.slot },
  };
}

/** The table in slot order (srfd hands a map's iteration order, which is not stable across answers). */
export function ordenarPorSlot(tabla: readonly InfoSuperficieSrfd[]): InfoSuperficieSrfd[] {
  return [...tabla].sort((a, b) => a.slot - b.slot);
}

/** The lowest slot in [0, max) the table does not hold, or -1 when every one is taken. */
export function primerSlotLibre(tabla: readonly InfoSuperficieSrfd[], max = SLOTS_MAX): number {
  const ocupados = new Set(tabla.map((s) => s.slot));
  for (let i = 0; i < max; i++) if (!ocupados.has(i)) return i;
  return -1;
}

/** Whether two answers of `listar` describe the same table (same slots, same fields), so a poll that changed nothing re-renders nothing. */
export function mismaTabla(a: readonly InfoSuperficieSrfd[], b: readonly InfoSuperficieSrfd[]): boolean {
  if (a.length !== b.length) return false;
  const sa = ordenarPorSlot(a);
  const sb = ordenarPorSlot(b);
  for (let i = 0; i < sa.length; i++) {
    if (JSON.stringify(sa[i]) !== JSON.stringify(sb[i])) return false;
  }
  return true;
}

// ---- recording (cockpit/grabacion.go) as the header, the cards and the slots show it ---------------------------------

/** The six affordance states of the REC control (package section 6.6). */
export type EstadoRec = 'idle' | 'no target' | 'starting' | 'recording' | 'stopping' | 'sealed';

/** What the REC control shows for a session state; `error` from the bridge rests as idle with the error in the title. */
export function estadoRec(sesion: RecordingSession | null, hayObjetivo = true): EstadoRec {
  if (sesion === null) return hayObjetivo ? 'idle' : 'no target';
  switch (sesion.fase) {
    case 'starting':
      return 'starting';
    case 'recording':
      return 'recording';
    case 'stopping':
      return 'stopping';
    case 'sealed':
      return 'sealed';
    default:
      return hayObjetivo ? 'idle' : 'no target';
  }
}

/** mm:ss of an elapsed span, the timer next to the crimson dot. */
export function mmss(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

/** RecordingState (hypervisor.ts) of a slot: the live session when it is this slot's, otherwise not recording. */
export function grabacionDeSlot(sesion: RecordingSession | null, slot: number): RecordingState {
  if (sesion === null || sesion.slot !== slot || !sesion.recording) {
    return { recording: false, sessionId: '', targetXid: '', sinceMonoMs: 0, offsetMs: null, verdict: sesion === null ? '' : sesion.verdict };
  }
  return { recording: true, sessionId: sesion.sessionId, targetXid: sesion.targetXid, sinceMonoMs: sesion.sinceMonoMs, offsetMs: sesion.offsetMs, verdict: sesion.verdict };
}

// Compatibility exports for existing consumers; canonical cells have no view dependencies.
export { muestra, celdaCpu, celdaRam, slotIdCanonico } from './telemetry/celdas';
export type { CeldaSensor } from './telemetry/celdas';
