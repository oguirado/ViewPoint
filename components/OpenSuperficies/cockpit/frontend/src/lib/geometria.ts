// Single source of the cockpit geometry (ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md section 1, DEC-13).
// Every figure the shell shows or checks (1271, 1264, 1024, 2080, ...) is derived here from the
// measured host facts; no component, style or script re-derives them. app.css mirrors the results
// as custom properties and tests/geometria.test.ts compares that mirror with this module.

import type { Geometry, PreferenciasCockpit, TemaCockpit, WindowMode } from './types/hypervisor';

// ---- host facts (spec 1.1, MEASURED) -------------------------------------------------------------
export const PANTALLA_W = 3840;
export const PANTALLA_H = 2160;
export const COLUMNAS = 3;
export const FILAS = 2;
export const BARRA_GNOME_H = 32;
/** Lateral border the window manager takes: innerWidth is 1271 in a 1280 window (spec 1.1, HAL-U13-33). */
export const MARGEN_WM_W = 9;
/** H.264 macroblocks and Xpra encoders are 16-aligned (spec 1.2). */
export const ALINEACION_CODEC = 16;

// ---- derived geometry (spec 1.2) -----------------------------------------------------------------
export const BARRA_H = 40;
export const VENTANA_W = PANTALLA_W / COLUMNAS; // 1280
export const VENTANA_H = (PANTALLA_H - BARRA_GNOME_H) / FILAS; // 1064
export const DOM_W = VENTANA_W - MARGEN_WM_W; // 1271
export const DOM_H_SEXTO = VENTANA_H; // 1064
export const DOM_H_TERCIO = 2 * VENTANA_H; // 2128

const multiploInferior = (n: number): number => Math.floor(n / ALINEACION_CODEC) * ALINEACION_CODEC;

export const LIENZO_W = multiploInferior(DOM_W); // 1264
export const LIENZO_H = multiploInferior(DOM_H_SEXTO - BARRA_H); // 1024
export const LIENZO_H_ALTO = multiploInferior(DOM_H_TERCIO - BARRA_H); // 2080
export const GUTTER_IZQ = 3;
export const GUTTER_DER = DOM_W - LIENZO_W - GUTTER_IZQ; // 4

/** DOM height from which the window is the 1/3 column (spec 1.5: `>= 2000 -> 1/3`). */
export const UMBRAL_TERCIO_H = 2000;
/** Below this DOM width the canvas is no longer pixel-exact (spec 1.6). */
export const ANCHO_EXACTO_MIN = LIENZO_W;

export type ModoCli = '1/6' | '1/3';

/** Mode of the window by DOM height. */
export function modoPorAltura(domHeight: number): WindowMode {
  return domHeight >= UMBRAL_TERCIO_H ? 'column_1_3_tall' : 'tile_1_6';
}

/** Data-attribute value of the mode, as the bootstrap and the CSS spell it. */
export function etiquetaModo(mode: WindowMode): 'sexto' | 'tercio' {
  return mode === 'tile_1_6' ? 'sexto' : 'tercio';
}

/** Geometry of a DOM viewport: the mode by height, the canvas of that mode, aspect-fit below 1264 px. */
export function geometriaDeVentana(domWidth: number, domHeight: number): Geometry {
  const mode = modoPorAltura(domHeight);
  const scaled = domWidth < ANCHO_EXACTO_MIN;
  return {
    mode,
    domWidth,
    domHeight,
    canvasWidth: LIENZO_W as 1264,
    canvasHeight: (mode === 'tile_1_6' ? LIENZO_H : LIENZO_H_ALTO) as 1024 | 2080,
    scaled,
    scale: scaled ? domWidth / LIENZO_W : 1,
  };
}

/** The DOM viewport a given CLI mode nominally has. */
export function domNominal(modo: ModoCli): { w: number; h: number } {
  return { w: DOM_W, h: modo === '1/6' ? DOM_H_SEXTO : DOM_H_TERCIO };
}

// ---- preferences (theme and font scale), resolved before first paint ----------------------------
// The synchronous <head> script of index.html cannot import this module, so it carries a copy of the
// four constants below and of the resolution rule; tests/geometria.test.ts executes that script and
// requires it to agree with resolverPreferencias on every stored value it tries.

export const CLAVE_PREFERENCIAS = 'srf.cockpit.prefs.v1';
export const TEMAS: readonly TemaCockpit[] = ['oscuro', 'alto-contraste'];
export const ESCALAS: readonly number[] = [1, 1.125, 1.25];
export const PREFERENCIAS_DEFECTO: PreferenciasCockpit = { tema: 'oscuro', escala: 1 };

export interface ResolucionPreferencias {
  prefs: PreferenciasCockpit;
  /** Why the stored value was not honoured (storage unreadable, or not JSON); '' when it was read cleanly. */
  error: string;
}

/**
 * Resolves the persisted preferences. `bruto` is what localStorage returned (null when nothing is
 * stored). A field that is not one of the closed values falls back to its default; unreadable JSON is
 * reported in `error` and never swallowed.
 */
export function resolverPreferencias(bruto: string | null): ResolucionPreferencias {
  if (bruto === null) return { prefs: { ...PREFERENCIAS_DEFECTO }, error: '' };
  let p: unknown;
  try {
    p = JSON.parse(bruto);
  } catch (e) {
    return { prefs: { ...PREFERENCIAS_DEFECTO }, error: e instanceof Error ? e.message : String(e) };
  }
  const o = (typeof p === 'object' && p !== null ? p : {}) as Record<string, unknown>;
  const tema = TEMAS.find((t) => t === o.tema) ?? PREFERENCIAS_DEFECTO.tema;
  const escala = ESCALAS.find((s) => s === o.escala) ?? PREFERENCIAS_DEFECTO.escala;
  return { prefs: { tema, escala }, error: '' };
}

/** Reads the preferences from a Storage; a storage that throws is reported, not hidden. */
export function leerPreferencias(almacen: Pick<Storage, 'getItem'>): ResolucionPreferencias {
  let bruto: string | null;
  try {
    bruto = almacen.getItem(CLAVE_PREFERENCIAS);
  } catch (e) {
    return { prefs: { ...PREFERENCIAS_DEFECTO }, error: e instanceof Error ? e.message : String(e) };
  }
  return resolverPreferencias(bruto);
}

/** Applies preferences and mode to the root element: exactly the three writes the head bootstrap makes. */
export function aplicarPreferencias(raiz: HTMLElement, prefs: PreferenciasCockpit, domHeight: number): void {
  raiz.dataset.tema = prefs.tema;
  raiz.style.setProperty('--escala', String(prefs.escala));
  raiz.dataset.modo = etiquetaModo(modoPorAltura(domHeight));
}
