// Guard and policy of the agents drawer's terminals (spec 6, DEC-5). This is the one place that says what a
// terminal of the cockpit may be: the xterm.js DOM renderer and nothing else, at most 2,000 lines of scrollback, at
// most 56 interactive instances, a drawer of fixed height and a three-pass focus order.
//
// WHY THE RENDERER IS GUARDED. A WebKitGTK compositor hang caused by a canvas or WebGL terminal layer throws no
// catchable error (spec 6): the only defence is never to create one. The guard is DENY BY DEFAULT and STRUCTURAL:
//   line 1  no addon reaches a terminal unless it is an INSTANCE that the drawer itself created through
//           crearAddonsPropios(), a closed list of factories that is EMPTY (the drawer needs no addon). term.loadAddon
//           of a guarded terminal (protegerTerminal) and cargarAddon() both decide by membership of that list, never
//           by a class name, a constructor chain or a package: the published renderer addons are minified (their
//           classes are called `a`, `l`, `p`) and a name is exactly what a bundler is free to change.
//   line 2  what the terminal produced is read back from its DOM: a canvas layer anywhere under the terminal
//           (leerRenderer, vigilarCapas) fails the panel, whichever way it got there.
// Nothing in this module or in the components ever imports a renderer addon.
//
// The figures below are the specification's; geometry proper lives in ../geometria.ts and is not repeated here.

import type { ITerminalAddon, ITerminalOptions } from '@xterm/xterm';

/** DEC-5: the scrollback limit of every terminal, in lines. */
export const SCROLLBACK_MAX = 2_000;
/** DEC-5: the red line. Beyond this many interactive instances a pane degrades to plain-text streaming (spec 6). */
export const PANELES_INTERACTIVOS_MAX = 56;
/** Spec 6 and the --drawer-h token of 1.5: the height of the open drawer, in CSS px. */
export const ALTURA_CAJON = 240;
/** Spec 6: the height of the grab handle the collapsed drawer shrinks to. */
export const ALTURA_ASA = 12;
/** The --overlay-head-h token of 1.5: the drawer's header row. */
export const ALTURA_CABECERA = 28;
/** Spec 4: the three-pass focus transfer, in ms after the panel becomes visible. */
export const PASOS_DE_FOCO_MS: readonly number[] = [0, 120, 400];

export const TAMANO_FUENTE = 12;
export const FUENTE_MONO = '"JetBrains Mono", "SF Mono", Menlo, Consolas, monospace';
/** Mirrors of the colour tokens of spec 3.4 (--surface-obsidian, --text-polar, --accent-cyan, --border-bold). */
export const TEMA_XTERM = {
  background: '#0e131f',
  foreground: '#f4f6fc',
  cursor: '#00e5ff',
  selectionBackground: '#3b4b6e',
} as const;

// ---- renderer policy -------------------------------------------------------------------------------------------
export interface DecisionDeRenderer {
  pedido: string;
  /** What the terminal will use whatever was asked: always the DOM renderer. */
  concedido: 'dom';
  rechazado: boolean;
  motivo: string;
}

/** The only renderer that exists here is the DOM one; every other request is refused, with the reason. */
export function decidirRenderer(pedido: string): DecisionDeRenderer {
  if (pedido === 'dom') return { pedido, concedido: 'dom', rechazado: false, motivo: '' };
  const conocido = pedido === 'canvas' || pedido === 'webgl';
  const motivo = conocido
    ? `the ${pedido} renderer is forbidden (spec 6: a WebKitGTK compositor hang throws no catchable error)`
    : `renderer "${pedido}" does not exist here: the DOM renderer is the only one`;
  return { pedido, concedido: 'dom', rechazado: true, motivo };
}

export interface RechazoDeAddon {
  /** The addon's class name, for the log only: no decision ever reads it. */
  addon: string;
  motivo: string;
}

/** An addon the drawer would need: a closed list, EMPTY today. Adding one is a decision taken in this file. */
const FABRICAS_PROPIAS: ReadonlyArray<() => ITerminalAddon> = Object.freeze<Array<() => ITerminalAddon>>([]);

export interface GuardiaDeAddons {
  /** Creates the drawer's own addons from the factory list and registers exactly those instances. */
  crearAddonsPropios(): ITerminalAddon[];
  /** `propio` only for an instance that crearAddonsPropios() made; everything else, whatever it is called, is `ajeno`. */
  clasificarAddon(addon: object): 'propio' | 'ajeno';
  /** The gate for a requested addon: refused (recorded, logged, never handed to loadAddon) unless it is propio. */
  cargarAddon(term: { loadAddon(addon: ITerminalAddon): void }, addon: ITerminalAddon): { cargado: boolean; motivo: string };
  /** Replaces term.loadAddon by an own, read-only property that applies the same decision. Returns the same terminal. */
  protegerTerminal<T extends { loadAddon(addon: ITerminalAddon): void }>(term: T): T;
  rechazos(): readonly RechazoDeAddon[];
  olvidarRechazos(): void;
}

function nombreParaElRegistro(addon: unknown): string {
  try {
    const nombre = (addon as { constructor?: { name?: unknown } } | null)?.constructor?.name;
    return typeof nombre === 'string' && nombre !== '' ? nombre : '(anonymous)';
  } catch {
    return '(unreadable)';
  }
}

/** A guard over a list of factories. The drawer has exactly one, over the empty list; a second one exists to test the allow-list. */
export function crearGuardia(fabricas: ReadonlyArray<() => ITerminalAddon>): GuardiaDeAddons {
  const propios = new WeakSet<object>();
  const registro: RechazoDeAddon[] = [];

  const clasificar = (addon: object): 'propio' | 'ajeno' => (propios.has(addon) ? 'propio' : 'ajeno');

  const rechazar = (addon: unknown): string => {
    const nombre = nombreParaElRegistro(addon);
    const motivo = 'this drawer loads no addon it did not create itself (deny by default): an addon may swap the DOM renderer for a canvas or WebGL layer, and a WebKitGTK compositor hang throws no catchable error (spec 6)';
    registro.push({ addon: nombre, motivo });
    console.warn(`termguard: refused ${nombre}: ${motivo}`);
    return motivo;
  };

  return {
    crearAddonsPropios: () =>
      fabricas.map((fabrica) => {
        const addon = fabrica();
        propios.add(addon);
        return addon;
      }),
    clasificarAddon: clasificar,
    cargarAddon(term, addon) {
      if (clasificar(addon) === 'ajeno') return { cargado: false, motivo: rechazar(addon) };
      term.loadAddon(addon);
      return { cargado: true, motivo: '' };
    },
    protegerTerminal(term) {
      const cargarDeVerdad = term.loadAddon.bind(term);
      Object.defineProperty(term, 'loadAddon', {
        value: (addon: ITerminalAddon): void => {
          if (clasificar(addon) === 'ajeno') {
            rechazar(addon);
            return;
          }
          cargarDeVerdad(addon);
        },
        writable: false,
        configurable: false,
        enumerable: false,
      });
      return term;
    },
    rechazos: () => registro,
    olvidarRechazos: () => {
      registro.length = 0;
    },
  };
}

const guardia = crearGuardia(FABRICAS_PROPIAS);

export const crearAddonsPropios = guardia.crearAddonsPropios;
export const clasificarAddon = guardia.clasificarAddon;
export const cargarAddon = guardia.cargarAddon;
export const protegerTerminal = guardia.protegerTerminal;

/** Every refusal of this page, in order. */
export function rechazosDeGuarda(): readonly RechazoDeAddon[] {
  return guardia.rechazos();
}

/** Forgets the recorded refusals (tests). */
export function olvidarRechazos(): void {
  guardia.olvidarRechazos();
}

/** What a terminal really renders with, read from its DOM: the DOM renderer's rows, or a canvas layer. */
export type RendererLeido = 'dom' | 'canvas' | 'desconocido';

/**
 * Line 2 of the guard. A canvas anywhere under the terminal's root (the DOM renderer never draws on one, and the
 * published addons append their layers to .xterm-screen) makes it a canvas terminal; the DOM renderer is recognised by
 * the rows it keeps in .xterm-screen. The reading is structural: it does not know which addon, if any, put the layer there.
 */
export function leerRenderer(raiz: Pick<Element, 'querySelector'>): RendererLeido {
  if (raiz.querySelector('canvas') !== null) return 'canvas';
  const pantalla = raiz.querySelector('.xterm-screen');
  if (pantalla === null) return 'desconocido';
  return pantalla.querySelector('.xterm-rows') !== null ? 'dom' : 'desconocido';
}

/**
 * Watches a terminal for the rest of its life: the moment a canvas layer is appended to the host, to .xterm or to
 * .xterm-screen (where the published addons put theirs) `alDetectar` is called once, with the reason. Only direct
 * children are watched, so the DOM renderer's own row updates never wake it. Returns the function that stops the watch.
 */
export function vigilarCapas(raiz: Pick<Element, 'querySelector'> & Element, alDetectar: (motivo: string) => void): () => void {
  if (typeof MutationObserver === 'undefined') return () => {};
  let avisado = false;
  const observador = new MutationObserver(() => {
    if (avisado || leerRenderer(raiz) !== 'canvas') return;
    avisado = true;
    observador.disconnect();
    alDetectar('a canvas layer appeared in the terminal: a renderer got past the guard');
  });
  const objetivos = [raiz, raiz.querySelector('.xterm'), raiz.querySelector('.xterm-screen')];
  for (const objetivo of objetivos) if (objetivo !== null) observador.observe(objetivo, { childList: true });
  return () => observador.disconnect();
}

// ---- scrollback and instance budget ----------------------------------------------------------------------------
/** Clamps a requested scrollback to the limit; a request that is not a non-negative integer is an error, not a default. */
export function limitarScrollback(pedido: number): number {
  if (!Number.isInteger(pedido) || pedido < 0) throw new RangeError(`scrollback ${String(pedido)} is not a non-negative integer`);
  return Math.min(pedido, SCROLLBACK_MAX);
}

/** The options of every terminal of the drawer. The scrollback always goes through the limit. */
export function opcionesDeTerminal(pedido: { scrollback?: number } = {}): ITerminalOptions {
  return {
    scrollback: limitarScrollback(pedido.scrollback ?? SCROLLBACK_MAX),
    fontFamily: FUENTE_MONO,
    fontSize: TAMANO_FUENTE,
    lineHeight: 1.2,
    cursorBlink: false,
    allowProposedApi: false,
    convertEol: false,
    theme: { ...TEMA_XTERM },
  };
}

export type ModoDePanel = 'interactivo' | 'texto';

/** Spec 6: the first 56 instances are interactive xterm.js terminals, the rest stream plain text. `indice` is 0-based. */
export function modoDeInstancia(indice: number): ModoDePanel {
  if (!Number.isInteger(indice) || indice < 0) throw new RangeError(`instance index ${String(indice)} is not a non-negative integer`);
  return indice < PANELES_INTERACTIVOS_MAX ? 'interactivo' : 'texto';
}

/** Plain-text streaming: what a degraded pane shows of a byte stream that carries terminal escape sequences. */
export function quitarAnsi(texto: string): string {
  return texto
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '')
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
    .replace(/\x1b[@-Z\\-_]/g, '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '');
}

// ---- three-pass focus -------------------------------------------------------------------------------------------
/**
 * Schedules the three focus passes (0, 120 and 400 ms). Each pass focuses only if the panel is still the one that
 * should hold the focus; the returned function cancels every pass that has not run yet.
 */
export function programarFoco(
  enfocar: () => void,
  sigueActivo: () => boolean,
  programar: (fn: () => void, ms: number) => unknown = (fn, ms) => setTimeout(fn, ms),
  cancelar: (manejador: unknown) => void = (m) => clearTimeout(m as ReturnType<typeof setTimeout>),
): () => void {
  const manejadores = PASOS_DE_FOCO_MS.map((ms) =>
    programar(() => {
      if (sigueActivo()) enfocar();
    }, ms),
  );
  return () => {
    for (const m of manejadores) cancelar(m);
  };
}
