// srfd client of the cockpit. The webview never opens a socket: it calls the Go bridge (cockpit/app.go), which
// speaks the daemon's UDS (src/surfacebook/manager/uds.go). This module turns the bridge's answer into typed
// values or into a SrfdError; an answer is never guessed, defaulted or downgraded to "ok".

import type { LeaseCredential } from './aiops/CapsuleBar.svelte';

import type {
  AccionPbx,
  CandidatoGrabacion,
  ClaseSrfd,
  ComandoSrfd,
  EstadoSrfd,
  ExtensionPbx,
  GateOverride,
  HostTelemetry,
  InfoSuperficieSrfd,
  InformeMediacion,
  RecordingSession,
  RespuestaSrfd,
} from './types/hypervisor';

/** Exit codes srfd assigns (uds.go: RCFallo, RCUso, RCPeerRechazado, RCSlotOcupado; parada.go: RCParada). */
export const RC_FALLO = 1;
export const RC_USO = 2;
export const RC_PARADA = 3;
export const RC_PEER_RECHAZADO = 9;
export const RC_SLOT_OCUPADO = 69;

const CAUSA_RC: Readonly<Record<number, string>> = {
  [RC_FALLO]: 'the operation failed',
  [RC_USO]: 'malformed request, unknown class or unknown slot',
  [RC_PARADA]: 'the stop signal (PARADA) is effective',
  [RC_PEER_RECHAZADO]: 'the caller uid is not admitted (SO_PEERCRED)',
  [RC_SLOT_OCUPADO]: 'the slot is already active or transitioning',
};

/** Human cause of an srfd exit code; an unknown code says so instead of inventing a cause. */
export function causaDeRC(rc: number): string {
  return CAUSA_RC[rc] ?? `unknown srfd exit code ${rc}`;
}

export type CodigoSrfdError =
  | 'sin_puente' // no Wails bridge in this webview (a plain browser)
  | 'transporte' // the bridge call itself failed: socket missing, daemon down, timeout
  | 'respuesta_invalida' // the bridge answered with something that is not the srfd envelope
  | 'rechazado'; // srfd answered ok=false

export class SrfdError extends Error {
  readonly codigo: CodigoSrfdError;
  /** srfd exit code for `rechazado`; RC_FALLO for local failures. Never 0. */
  readonly rc: number;

  constructor(codigo: CodigoSrfdError, rc: number, mensaje: string, causa?: unknown) {
    super(mensaje, causa === undefined ? undefined : { cause: causa });
    this.name = 'SrfdError';
    this.codigo = codigo;
    this.rc = rc === 0 ? RC_FALLO : rc;
  }
}

/** One request/response round trip with srfd; the production one is the Wails bridge. */
export interface TransporteSrfd {
  enviar(comando: ComandoSrfd, clase: string, slot: number, holder?: string): Promise<unknown>;
}

interface PuenteWails {
  Enviar(comando: string, clase: string, request: number | {slot: number; holder: string}): Promise<unknown>;
}

/** The bridge Wails injects (`window.go.main.App`), or null in a plain browser. */
export function transporteWails(global: unknown = globalThis): TransporteSrfd | null {
  const app = (global as { go?: { main?: { App?: Partial<PuenteWails> } } }).go?.main?.App;
  if (app === undefined || typeof app.Enviar !== 'function') return null;
  const enviar = app.Enviar.bind(app) as PuenteWails['Enviar'];
  return { enviar: (comando, clase, slot, holder) => enviar(comando, clase, holder === undefined ? slot : {slot,holder}) };
}

function esEnvoltorio(v: unknown): v is RespuestaSrfd {
  if (typeof v !== 'object' || v === null) return false;
  const o = v as Record<string, unknown>;
  return typeof o.ok === 'boolean' && typeof o.rc === 'number' && (o.error === undefined || typeof o.error === 'string');
}

export interface ClienteSrfd {
  estado(): Promise<EstadoSrfd>;
  listar(): Promise<InfoSuperficieSrfd[]>;
  iniciar(clase: ClaseSrfd, slot: number, holder?: string): Promise<InfoSuperficieSrfd>;
  detener(slot: number, clase?: ClaseSrfd): Promise<void>;
}

/** Client over any transport. `null` (no bridge) is refused with sin_puente on the first call, not earlier. */
export function crearClienteSrfd(transporte: TransporteSrfd | null, onLease?: (slot: number, credential: LeaseCredential) => void): ClienteSrfd {
  async function pedir<T>(comando: ComandoSrfd, clase = '', slot = 0, holder?: string): Promise<T | undefined> {
    if (transporte === null) {
      throw new SrfdError('sin_puente', RC_FALLO, 'no Wails bridge in this webview: srfd is unreachable from here');
    }
    let bruto: unknown;
    try {
      bruto = await transporte.enviar(comando, clase, slot, holder);
    } catch (e) {
      const texto = e instanceof Error ? e.message : String(e);
      throw new SrfdError('transporte', RC_FALLO, `srfd ${comando}: ${texto}`, e);
    }
    if (!esEnvoltorio(bruto)) {
      throw new SrfdError('respuesta_invalida', RC_FALLO, `srfd ${comando}: the answer is not an srfd envelope`);
    }
    if (!bruto.ok) {
      const rc = bruto.rc === 0 ? RC_FALLO : bruto.rc; // a refusal never carries rc 0
      throw new SrfdError('rechazado', rc, `srfd ${comando} refused (rc ${rc}): ${bruto.error ?? causaDeRC(rc)}`);
    }
    if (comando === 'iniciar' && holder !== undefined) {
      const lease = (bruto as RespuestaSrfd & {lease?: unknown}).lease;
      const c = lease as Partial<LeaseCredential> | null;
      if (!c || c.holder !== holder || !Number.isSafeInteger(c.epoch) || Number(c.epoch) < 1 ||
        typeof c.token !== 'string' || !/^[0-9a-f]{32}$/.test(c.token)) {
        throw new SrfdError('respuesta_invalida', RC_USO, 'launch lease missing or mismatched');
      }
      onLease?.(slot, c as LeaseCredential);
    }
    return bruto.datos as T | undefined;
  }

  async function exigirDatos<T>(comando: ComandoSrfd, clase = '', slot = 0, holder?: string): Promise<T> {
    const datos = await pedir<T>(comando, clase, slot, holder);
    if (datos === undefined || datos === null) {
      throw new SrfdError('respuesta_invalida', RC_FALLO, `srfd ${comando}: ok answer without datos`);
    }
    return datos;
  }

  return {
    estado: () => exigirDatos<EstadoSrfd>('estado'),
    // srfd always answers `listar` with an array (`[]` for an empty table); anything else is a protocol violation.
    listar: async () => {
      const datos = await exigirDatos<unknown>('listar');
      if (!Array.isArray(datos)) {
        throw new SrfdError('respuesta_invalida', RC_FALLO, 'srfd listar: datos is not an array');
      }
      return datos as InfoSuperficieSrfd[];
    },
    iniciar: (clase, slot, holder) => exigirDatos<InfoSuperficieSrfd>('iniciar', clase, slot, holder),
    detener: async (slot, clase) => {
      await pedir<undefined>('detener', clase ?? '', slot);
    },
  };
}

// ---- the rest of the bridge (cockpit/app.go beyond Enviar): recording, PTY, gates, telemetry, PBX -------------------
// Each wrapper calls the bound Go method by name and fails with sin_puente when the webview has no bridge or the
// bridge lacks the method (an older cockpit binary): a missing method is reported, never emulated.

export interface PuenteCockpit {
  /** The SRF2 stream URL of one agent's shell (cockpit/ptyserver.go). */
  ptyUrl(agentId: string): Promise<string>;
  estadoGrabacion(): Promise<RecordingSession>;
  candidatosGrabacion(): Promise<CandidatoGrabacion[]>;
  censoMediacion(): Promise<InformeMediacion>;
  tripGate(gateId: string, reason: string): Promise<void>;
  resetGate(gateId: string): Promise<void>;
  estadoPuertas(): Promise<GateOverride[]>;
  hostTelemetria(): Promise<HostTelemetry>;
  pbxLeases(): Promise<ExtensionPbx[]>;
  pbxAccion(accion: AccionPbx, slug: string): Promise<ExtensionPbx>;
  /** Which of the methods above the bridge actually binds (for the views' FIXTURE / BRIDGE labels). */
  disponible(metodo: keyof PuenteCockpit): boolean;
}

type MetodoPuente = (...args: unknown[]) => Promise<unknown>;

/** Go method names as Wails binds them (window.go.main.App.<Name>). */
export const METODOS_PUENTE: Readonly<Record<Exclude<keyof PuenteCockpit, 'disponible'>, string>> = {
  ptyUrl: 'PtyUrl',
  estadoGrabacion: 'EstadoGrabacion',
  candidatosGrabacion: 'CandidatosGrabacion',
  censoMediacion: 'CensoMediacion',
  tripGate: 'TripGate',
  resetGate: 'ResetGate',
  estadoPuertas: 'EstadoPuertas',
  hostTelemetria: 'GetHostTelemetry',
  pbxLeases: 'PbxLeases',
  pbxAccion: 'PbxAccion',
};

/** The bridge wrappers over `window.go.main.App` (or any object shaped like it); null bridge → every call fails sin_puente. */
export function crearPuenteCockpit(global: unknown = globalThis): PuenteCockpit {
  const app = (global as { go?: { main?: { App?: Record<string, unknown> } } }).go?.main?.App ?? null;
  const metodo = (clave: Exclude<keyof PuenteCockpit, 'disponible'>): MetodoPuente | null => {
    const nombre = METODOS_PUENTE[clave];
    const fn = app === null ? undefined : app[nombre];
    return typeof fn === 'function' ? (fn as MetodoPuente).bind(app) : null;
  };
  async function llamar<T>(clave: Exclude<keyof PuenteCockpit, 'disponible'>, ...args: unknown[]): Promise<T> {
    const fn = metodo(clave);
    if (fn === null) {
      throw new SrfdError('sin_puente', RC_FALLO, app === null ? `no Wails bridge in this webview: ${METODOS_PUENTE[clave]} is unreachable` : `${METODOS_PUENTE[clave]} is not bound by this cockpit (AUSENTE)`);
    }
    try {
      return (await fn(...args)) as T;
    } catch (e) {
      const texto = e instanceof Error ? e.message : String(e);
      throw new SrfdError('transporte', RC_FALLO, `${METODOS_PUENTE[clave]}: ${texto}`, e);
    }
  }
  return {
    ptyUrl: (agentId) => llamar<string>('ptyUrl', agentId),
    estadoGrabacion: () => llamar<RecordingSession>('estadoGrabacion'),
    candidatosGrabacion: async () => (await llamar<CandidatoGrabacion[] | null>('candidatosGrabacion')) ?? [],
    censoMediacion: () => llamar<InformeMediacion>('censoMediacion'),
    tripGate: async (gateId, reason) => {
      await llamar<unknown>('tripGate', gateId, reason);
    },
    resetGate: async (gateId) => {
      await llamar<unknown>('resetGate', gateId);
    },
    estadoPuertas: async () => (await llamar<GateOverride[] | null>('estadoPuertas')) ?? [],
    hostTelemetria: () => llamar<HostTelemetry>('hostTelemetria'),
    pbxLeases: async () => (await llamar<ExtensionPbx[] | null>('pbxLeases')) ?? [],
    pbxAccion: (accion, slug) => llamar<ExtensionPbx>('pbxAccion', accion, slug),
    disponible: (clave) => clave !== 'disponible' && metodo(clave) !== null,
  };
}
