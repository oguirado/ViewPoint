// Logic of the capsule cards (ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md sections 3 and 4). Nothing in this module
// renders: the Svelte components read what it decides. Every geometry figure comes from ../geometria (the single
// source); the colours it names are the tokens of section 3.4 and never a literal.

import { LIENZO_H, LIENZO_H_ALTO, LIENZO_W } from '../geometria';
import type {
  Archetype,
  GateStatus,
  Geometry,
  HealthStatus,
  IsolationTier,
  OperationMode,
  SurfaceCapsule,
  WindowMode,
} from '../types/hypervisor';

/** Optional producer framebuffer context; legacy standalone cards keep their original geometry. */
export const FRAMEBUFFER_CONTEXT = Symbol('cockpit-framebuffer');
export type FramebufferContext = () => TamanoLienzo | null;

// ---- viewer adapter (spec 3.2) --------------------------------------------------------------------

export interface ViewerAdapter {
  mount(host: HTMLElement): Promise<void>;
  setViewOnly(value: boolean): void;
  fitLocal(): void; // CSS aspect-fit only when the DOM is narrower than the canvas (ANCHO_EXACTO_MIN in geometria.ts)
  focus(): void;
  pause(paused: boolean): void;
  disconnect(): void;
}

export interface TamanoLienzo {
  w: number;
  h: number;
}

/** The canvas of ONE card in a window mode: the 1/6 canvas, or the tall canvas in the 1/3 tall column (spec 1.2; figures live in geometria.ts only). */
export function tamanoLienzo(mode: WindowMode): TamanoLienzo {
  return { w: LIENZO_W, h: mode === 'column_1_3_tall' ? LIENZO_H_ALTO : LIENZO_H };
}

// ---- the Xpra frame ---------------------------------------------------------------------------------

/** The sandbox of the Xpra HTML5 frame, exactly as spec 3.2 spells it. */
export const SANDBOX_XPRA = 'allow-scripts allow-same-origin allow-pointer-lock';

/**
 * Client-URL layer of "the clipboard is disabled at server, client URL, display and prompt layers" (spec 4), plus
 * the Xpra client features a card has no use for: printing, file transfer, sound and the floating menu.
 */
const PARAMETROS_XPRA: ReadonlyArray<readonly [string, string]> = [
  ['clipboard', 'false'],
  ['printing', 'false'],
  ['file_transfer', 'false'],
  ['sound', 'false'],
  ['floating_menu', 'false'],
];

/** URL of the Xpra HTML5 client of a capsule: loopback only, the port is the capsule's own. */
export function urlXpra(puerto: number): string {
  if (!Number.isInteger(puerto) || puerto < 1 || puerto > 65535) {
    throw new RangeError(`urlXpra: ${String(puerto)} is not a TCP port`);
  }
  const consulta = new URLSearchParams(PARAMETROS_XPRA as Array<[string, string]>);
  return `http://127.0.0.1:${puerto}/?${consulta.toString()}`;
}

export interface OpcionesAdaptadorXpra {
  iframe: () => HTMLIFrameElement | null;
  envoltura: () => HTMLElement | null;
  geometria: () => Geometry;
}

/**
 * ViewerAdapter of the Xpra frame. It never replaces or re-navigates the frame except in disconnect(): a
 * navigation destroys the Xpra WebSocket and the client keyboard focus (spec 2.1). Read-only itself is enforced by
 * the Xpra layer of the capsule; setViewOnly only records what the card believes, for the chrome to show.
 */
export function crearAdaptadorXpra(o: OpcionesAdaptadorXpra): ViewerAdapter {
  const marco = (): HTMLIFrameElement => {
    const f = o.iframe();
    if (f === null) throw new Error('xpra adapter: the frame is not mounted');
    return f;
  };
  return {
    async mount(host) {
      if (!host.contains(marco())) throw new Error('xpra adapter: the frame is not attached to the given host');
    },
    setViewOnly(value) {
      const env = o.envoltura();
      if (env !== null) env.dataset.soloLectura = String(value);
    },
    fitLocal() {
      const env = o.envoltura();
      const g = o.geometria();
      if (env !== null) env.style.setProperty('--escala-local', String(g.scaled ? g.scale : 1));
    },
    focus() {
      const f = marco();
      f.focus();
      f.contentWindow?.focus();
    },
    pause(paused) {
      const env = o.envoltura();
      if (env !== null) env.dataset.pausado = String(paused);
    },
    disconnect() {
      const f = marco();
      f.src = 'about:blank';
      const env = o.envoltura();
      if (env !== null) env.dataset.desconectado = 'true';
    },
  };
}

// ---- zero-click-loss (spec 4) -----------------------------------------------------------------------

export interface MensajeRecibido {
  origin: string;
  source: unknown;
  data: unknown;
}

/**
 * Whether a message from the embedded viewer must activate the card: it has to come from THIS frame (source and
 * origin), carry the pointerdown notice, and the card must not be held by an agent (takeover is an explicit verb).
 */
export function debeActivar(msg: MensajeRecibido, esperado: { origin: string; source: unknown }, modo: OperationMode): boolean {
  if (msg.source !== esperado.source || msg.origin !== esperado.origin) return false;
  if (typeof msg.data !== 'object' || msg.data === null) return false;
  if ((msg.data as { tipo?: unknown }).tipo !== 'superficie:pointerdown') return false;
  return modo !== 'agent_locked';
}

// ---- the four archetypes (spec 3, table) ------------------------------------------------------------

export type RutaRender = 'xpra_iframe' | 'xterm_dom';

export interface DescriptorArquetipo {
  arquetipo: Archetype;
  etiqueta: string;
  glifo: string;
  tier: IsolationTier;
  ruta: RutaRender;
  /** The archetype has an authenticated CDP port next to the Xpra one. */
  cdp: boolean;
  telemetriaPrimaria: string;
  /** Control latency target in ms [TARGET]. */
  latenciaObjetivoMs: number;
}

export const ARQUETIPOS: Readonly<Record<Archetype, DescriptorArquetipo>> = {
  browser: { arquetipo: 'browser', etiqueta: 'Browser', glifo: '◎', tier: 'docker_l2', ruta: 'xpra_iframe', cdp: true, telemetriaPrimaria: 'CDP gates · DOM nodes · JS heap', latenciaObjetivoMs: 25 },
  desktop: { arquetipo: 'desktop', etiqueta: 'Desktop', glifo: '▣', tier: 'docker_l2', ruta: 'xpra_iframe', cdp: false, telemetriaPrimaria: 'X11 clients · window geometry', latenciaObjetivoMs: 30 },
  microvm: { arquetipo: 'microvm', etiqueta: 'MicroVM', glifo: '⬡', tier: 'kvm_hardware', ruta: 'xpra_iframe', cdp: false, telemetriaPrimaria: 'vCPU · guest RAM · V-gates', latenciaObjetivoMs: 25 },
  terminal: { arquetipo: 'terminal', etiqueta: 'Terminal', glifo: '❯', tier: 'pty_jaula', ruta: 'xterm_dom', cdp: false, telemetriaPrimaria: 'PIDs · cgroup memory · exit code', latenciaObjetivoMs: 5 },
};

// ---- badges (spec 3.3) and the AAA policy of their colours (spec 3.4) -------------------------------

export type TokenColor =
  | '--text-polar'
  | '--text-silver'
  | '--text-muted'
  | '--accent-cyan'
  | '--accent-amber'
  | '--accent-crimson'
  | '--accent-emerald'
  | '--accent-violet';

/**
 * Tokens a badge may use for its TEXT. They reach 7:1 on the opaque chip background (--canvas-void), which
 * scripts/ng/medir-contraste.mjs audits on the token file. crimson, violet and muted do not reach 7:1 as text:
 * they only ever paint the chip's border and dot, which need 3:1 (WCAG 1.4.11), and the label stays polar/silver.
 */
export const TOKENS_DE_TEXTO: ReadonlySet<TokenColor> = new Set<TokenColor>(['--text-polar', '--text-silver', '--accent-cyan', '--accent-amber', '--accent-emerald']);

export type ClaseInsignia = 'aislamiento' | 'modo' | 'salud' | 'gate' | 'escala';
export type AnimacionInsignia = 'pulso' | 'parpadeo' | null;

export interface Insignia {
  clase: ClaseInsignia;
  /** Stable machine value (the union member, the gate name). */
  valor: string;
  /** The text the badge shows, exactly as the specification spells it. */
  texto: string;
  /** Token of the border and the dot. */
  indicador: TokenColor;
  /** Token of the label. Always a member of TOKENS_DE_TEXTO. */
  textoToken: TokenColor;
  animacion: AnimacionInsignia;
}

const insignia = (clase: ClaseInsignia, valor: string, texto: string, indicador: TokenColor, textoToken: TokenColor, animacion: AnimacionInsignia = null): Insignia => ({
  clase,
  valor,
  texto,
  indicador,
  textoToken,
  animacion,
});

export function insigniaAislamiento(tier: IsolationTier): Insignia {
  switch (tier) {
    case 'docker_l2':
      return insignia('aislamiento', tier, 'DOCKER L2', '--accent-cyan', '--accent-cyan');
    case 'kvm_hardware':
      return insignia('aislamiento', tier, 'KVM HARDWARE', '--accent-violet', '--text-polar');
    case 'pty_jaula':
      return insignia('aislamiento', tier, 'PTY JAULA', '--text-silver', '--text-silver');
  }
}

export function insigniaModo(mode: OperationMode): Insignia {
  switch (mode) {
    case 'read_only':
      return insignia('modo', mode, 'READ-ONLY', '--accent-amber', '--accent-amber');
    case 'operator_controlled':
      return insignia('modo', mode, 'OPERATOR-CONTROL', '--accent-emerald', '--accent-emerald');
    case 'agent_locked':
      return insignia('modo', mode, 'AGENT-HELD', '--accent-crimson', '--text-polar');
    case 'switching':
      return insignia('modo', mode, 'SWITCHING…', '--accent-amber', '--accent-amber', 'pulso');
  }
}

export function insigniaSalud(health: HealthStatus): Insignia {
  switch (health) {
    case 'healthy':
      return insignia('salud', health, 'HEALTHY', '--accent-emerald', '--accent-emerald');
    case 'attaching':
      return insignia('salud', health, 'ATTACHING…', '--accent-amber', '--accent-amber', 'pulso');
    case 'degraded':
      return insignia('salud', health, 'DEGRADED', '--accent-amber', '--accent-amber');
    case 'breach':
      return insignia('salud', health, 'BREACH', '--accent-crimson', '--text-polar', 'parpadeo');
    case 'zombie':
      return insignia('salud', health, 'ZOMBIE', '--text-muted', '--text-silver');
  }
}

/** `SCALED ×0.nn` while the canvas is not pixel-exact; null otherwise. */
export function insigniaEscala(geo: Pick<Geometry, 'scaled' | 'scale'>): Insignia | null {
  if (!geo.scaled) return null;
  return insignia('escala', 'scaled', `SCALED ×${geo.scale.toFixed(2)}`, '--accent-amber', '--accent-amber');
}

const GATES_CDP: ReadonlyArray<readonly [keyof NonNullable<GateStatus['cdp']>, string]> = [
  ['auth', 'G-AUTH'],
  ['human', 'G-HUMAN'],
  ['attach', 'G-ATTACH'],
  ['submit', 'G-SUBMIT'],
  ['harvest', 'G-COSECHA'],
];

const GATES_MICROVM = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I'] as const;

export interface PildoraGate extends Insignia {
  ok: boolean;
}

/** Gate pills of a capsule: the CDP ladder or the MicroVM V-gates, emerald when they hold and grey when they do not. */
export function pildorasGates(gates: GateStatus): PildoraGate[] {
  const pildora = (valor: string, texto: string, ok: boolean): PildoraGate => ({
    ...insignia('gate', valor, texto, ok ? '--accent-emerald' : '--text-muted', ok ? '--accent-emerald' : '--text-silver'),
    ok,
  });
  const salida: PildoraGate[] = [];
  if (gates.cdp !== undefined) for (const [clave, texto] of GATES_CDP) salida.push(pildora(texto, texto, gates.cdp[clave]));
  if (gates.microvm !== undefined) for (const l of GATES_MICROVM) salida.push(pildora(`V-${l}`, `V-${l}`, gates.microvm[l]));
  return salida;
}

// ---- telemetry text ---------------------------------------------------------------------------------

const SIN_DATO = '—';

export function formatoCpu(porcentaje: number): string {
  return Number.isFinite(porcentaje) && porcentaje >= 0 ? `CPU ${porcentaje.toFixed(1)}%` : `CPU ${SIN_DATO}`;
}

export function formatoMemoria(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return `RAM ${SIN_DATO}`;
  const mib = bytes / 2 ** 20;
  return mib >= 2 ** 10 ? `RAM ${(mib / 2 ** 10).toFixed(1)}G` : `RAM ${Math.round(mib)}M`;
}

/** Port pair as the bar shows it: `cdp/xpra` for a browser, the Xpra port alone for the others. */
export function formatoPuertos(c: Pick<SurfaceCapsule, 'xpraPort' | 'cdpPort'>): string {
  return c.cdpPort === null ? String(c.xpraPort) : `${c.cdpPort}/${c.xpraPort}`;
}

// ---- overlay visibility (spec 3.1) ------------------------------------------------------------------

/** Overlays auto-hide after this long without pointer or keyboard activity. */
export const AUTO_OCULTAR_MS = 2_000;
/** Height of the pointer zone at the top (header) and bottom (strip) of the canvas that reveals an overlay. */
export const ZONA_HOVER_PX = 64;

export interface EntradaOverlay {
  fijado: boolean;
  /** The pointer is over the overlay itself, or in the hot zone at its edge. */
  sobreZona: boolean;
  foco: boolean;
  /** An activity (pointer entry, focus) happened less than AUTO_OCULTAR_MS ago. */
  actividadReciente: boolean;
  salud: HealthStatus;
  modo: OperationMode;
}

/** The 28 px header: pinned, hovered, focused or recently used, and always while a switch is under way. */
export function cabeceraVisible(e: EntradaOverlay): boolean {
  return e.fijado || e.sobreZona || e.foco || e.actividadReciente || e.modo === 'switching' || e.salud !== 'healthy';
}

/** The 28 px telemetry strip: pinned automatically whenever health is not `healthy`. */
export function franjaVisible(e: EntradaOverlay): boolean {
  return e.fijado || e.sobreZona || e.foco || e.actividadReciente || e.salud !== 'healthy';
}

// ---- the optimistic Read-Only <-> Operator switch (spec 4) ------------------------------------------

export type DestinoConmutacion = 'operador' | 'solo_lectura';

/** What the backend's switch answers (srf-xpra `intervenir` / `blindar`, ConmutacionResult). */
export interface ResultadoConmutacion {
  readonly: boolean;
  ms: number;
  estado: string;
}

/**
 * The seam between a card and the daemon. srfd's UDS has no switch verb yet (the cockpit bridge exposes estado,
 * iniciar, detener, mediacion, listar), so the production binding is not part of this unit: the card takes this
 * interface, and the measurement harness implements it over `bin/srf-xpra` and an HTTP probe of the Xpra port.
 */
export interface ConmutadorCapsula {
  conmutar(slot: number, destino: DestinoConmutacion): Promise<ResultadoConmutacion>;
  /** HTTP status of the capsule's Xpra port. A refused connection rejects. */
  sondear(puertoXpra: number): Promise<number>;
}

export interface RelojConmutacion {
  ahora(): number;
  esperar(ms: number): Promise<void>;
}

export const SONDEO_MS = 100;
export const LIMITE_CONMUTACION_MS = 3000;
/** Three-pass focus transfer after a successful switch, in ms from the settle (spec 4, item 3). */
export const PASOS_FOCO_MS: readonly number[] = [0, 120, 400];

export type FaseConmutacion = 'reposo' | 'conmutando' | 'asentado' | 'degradado';

export interface EstadoConmutacion {
  fase: FaseConmutacion;
  modo: OperationMode;
  /** Health forced by the switch (`degraded` after a failure); null leaves the capsule's own health. */
  salud: HealthStatus | null;
  destino: DestinoConmutacion | null;
  /** Wall time of the switch by the card's own clock; null unless `asentado`. */
  ms: number | null;
  /** The literal backend or probe error of a `degradado` switch; empty otherwise. */
  error: string;
}

export interface OpcionesConmutacion {
  slot: number;
  puertoXpra: number;
  modoInicial: OperationMode;
  conmutador: ConmutadorCapsula;
  reloj: RelojConmutacion;
  alCambiar: (e: EstadoConmutacion) => void;
  /** Runs once when the switch settles: the card does fitLocal() and the focus transfer here. */
  alAsentar?: (destino: DestinoConmutacion) => void;
}

export interface ControlConmutacion {
  conmutar(destino: DestinoConmutacion): Promise<EstadoConmutacion>;
  estado(): EstadoConmutacion;
}

const mensajeDe = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/**
 * The optimistic switch. The mode flips to `switching` synchronously, before anything is awaited, and the frame is
 * never touched: this controller has no reference to it. The switch settles only when the backend has answered with a
 * read-back that agrees with the destination AND the Xpra port answers HTTP 200 afterwards (polled every SONDEO_MS,
 * the first probe immediately). A failure or LIMITE_CONMUTACION_MS without success becomes `degradado` carrying the
 * literal error; it never silently reverts to the previous mode.
 */
export function crearConmutacion(o: OpcionesConmutacion): ControlConmutacion {
  let actual: EstadoConmutacion = { fase: 'reposo', modo: o.modoInicial, salud: null, destino: null, ms: null, error: '' };
  let enCurso = false;
  const poner = (e: EstadoConmutacion): void => {
    actual = e;
    o.alCambiar(e);
  };

  async function conmutar(destino: DestinoConmutacion): Promise<EstadoConmutacion> {
    if (enCurso) return actual; // one switch at a time per capsule: the backend holds a per-slot exclusion too
    enCurso = true;
    const t0 = o.reloj.ahora();
    poner({ fase: 'conmutando', modo: 'switching', salud: null, destino, ms: null, error: '' });

    let cerrado = false;
    const cerrar = (e: EstadoConmutacion): void => {
      if (cerrado) return;
      cerrado = true;
      enCurso = false;
      poner(e);
      if (e.fase !== 'asentado') return;
      try {
        o.alAsentar?.(destino);
      } catch (err) {
        // the switch did settle: the failure of the step after it is shown next to the settled mode, never swallowed
        poner({ ...e, error: `settled, but the step after the switch failed: ${mensajeDe(err)}` });
      }
    };
    const degradar = (error: string): void => cerrar({ fase: 'degradado', modo: 'switching', salud: 'degraded', destino, ms: null, error });

    let ultimoError = '';
    const trabajo = (async (): Promise<void> => {
      let res: ResultadoConmutacion;
      try {
        res = await o.conmutador.conmutar(o.slot, destino);
      } catch (e) {
        degradar(`switch refused: ${mensajeDe(e)}`);
        return;
      }
      if (res.readonly !== (destino === 'solo_lectura')) {
        degradar(`read-back disagrees: the backend reports readonly=${String(res.readonly)} after a switch to ${destino}`);
        return;
      }
      for (;;) {
        if (cerrado) return;
        try {
          const estado = await o.conmutador.sondear(o.puertoXpra);
          if (estado === 200) {
            cerrar({ fase: 'asentado', modo: destino === 'operador' ? 'operator_controlled' : 'read_only', salud: null, destino, ms: o.reloj.ahora() - t0, error: '' });
            return;
          }
          ultimoError = `Xpra port ${o.puertoXpra} answered HTTP ${estado}`;
        } catch (e) {
          ultimoError = `Xpra port ${o.puertoXpra} probe failed: ${mensajeDe(e)}`;
        }
        if (o.reloj.ahora() - t0 >= LIMITE_CONMUTACION_MS) {
          degradar(ultimoError);
          return;
        }
        await o.reloj.esperar(SONDEO_MS);
      }
    })();
    // At the deadline the last probe error is the literal one to show; with none (the backend never answered) the deadline itself is.
    const plazo = o.reloj.esperar(LIMITE_CONMUTACION_MS).then(() => degradar(ultimoError !== '' ? ultimoError : `no confirmation within ${LIMITE_CONMUTACION_MS} ms`));
    await Promise.race([trabajo, plazo]);
    return actual;
  }

  return { conmutar, estado: () => actual };
}

/** Three-pass focus transfer: `enfocar` runs at PASOS_FOCO_MS after the call, counted from the first pass. */
export async function transferirFoco(reloj: RelojConmutacion, enfocar: () => void): Promise<void> {
  let previo = 0;
  for (const t of PASOS_FOCO_MS) {
    if (t > previo) await reloj.esperar(t - previo);
    previo = t;
    enfocar();
  }
}
