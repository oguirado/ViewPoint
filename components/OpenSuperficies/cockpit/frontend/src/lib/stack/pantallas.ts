// Window mode -> stack layout, and the virtual screens of the stack (spec 2.1 and 2.2; SUB_03 section 2.4).
//
// Layout. Every figure comes from lib/geometria.ts: nothing is re-derived and no pixel literal lives here. The stack
// shows the cards in a scroll viewport that starts under the 40 px global bar:
//
//   1/6            one card per view; the viewport is the canvas; snap step = one canvas
//   1/3 dual       two cards per view; the viewport is 2 x (canvas + card bar) - card bar: canvas A, the bar that
//                  carries card B's identity, canvas B; snap step = canvas + card bar (spec 2.2)
//   1/3 tall       one card per view; the viewport is the whole column under the global bar, and the card is the
//                  tall canvas with the spec's slack at the bottom; snap step = the viewport
//
// `--modo 1/3` of scripts/ng/medir-scroll.mjs is the DUAL layout (two cards visible). geometria.modoPorAltura only
// ever answers `column_1_3_tall` for a 1/3 window (a DOM height cannot tell one surface from two), so the stack names
// the dual layout itself: `modoDePila`.
//
// Screens. INV-PANTALLA-01..04 of the OpenNewton engine this file is named after: switching screens only hides
// cards (display: none, never an unmount), a card with no valid screen belongs to the first one, closing a screen
// hands its cards to an heir, and the workspace never drops below one screen. The corpus does not spell the heir rule
// ("AUSENTE - no inventar"): this module decides it (the previous screen, or the next one when the first is closed).

import type { WindowMode } from '../types/hypervisor';
import { BARRA_H, DOM_H_SEXTO, DOM_H_TERCIO, DOM_W, GUTTER_IZQ, LIENZO_H, LIENZO_H_ALTO, LIENZO_W } from '../geometria';
import type { ModoCli } from '../geometria';
import type { FenwickArbol } from './fenwick';

// ---- layout of a window mode ----------------------------------------------------------------------------------

export interface DisposicionPila {
  modo: WindowMode;
  /** Height of one card canvas. */
  alturaTarjeta: number;
  /** Height of the card bar that sits under a card and carries the identity of the next one (0 outside the dual layout). */
  alturaBarra: number;
  /** Height of the scroll viewport of the stack: the DOM height minus the global bar. */
  alturaVista: number;
  /** Distance between two consecutive snap points, and the height of every card but the last. */
  paso: number;
  /** Cards whose canvas is fully inside one view. */
  tarjetasPorVista: number;
  /** Width of the scroll viewport, and the canvas inside it. */
  ancho: number;
  anchoTarjeta: number;
  /** Left margin of the canvas; what is left on the right is painted with the void colour. */
  gutterIzq: number;
}

/** The stack layout a CLI/measurement mode asks for: `1/3` is the dual layout (see the header). */
export function modoDePila(modo: ModoCli): WindowMode {
  return modo === '1/6' ? 'tile_1_6' : 'column_1_3_dual';
}

/** Layout of a window mode; every number is geometria.ts's. */
export function disposicion(modo: WindowMode): DisposicionPila {
  const alturaVista = (modo === 'tile_1_6' ? DOM_H_SEXTO : DOM_H_TERCIO) - BARRA_H;
  const alturaTarjeta = modo === 'column_1_3_tall' ? LIENZO_H_ALTO : LIENZO_H;
  const alturaBarra = modo === 'column_1_3_dual' ? BARRA_H : 0;
  // A tall card leaves the spec's slack under the canvas: its item is the whole viewport, so a snap never shows a sliver of the next card.
  const paso = modo === 'column_1_3_tall' ? alturaVista : alturaTarjeta + alturaBarra;
  return {
    modo,
    alturaTarjeta,
    alturaBarra,
    alturaVista,
    paso,
    // The last card of a view has no bar under it, so the view holds one more bar-less card than whole steps.
    tarjetasPorVista: Math.floor((alturaVista + alturaBarra) / paso),
    ancho: DOM_W,
    anchoTarjeta: LIENZO_W,
    gutterIzq: GUTTER_IZQ,
  };
}

/** Cards per view of a window mode: the number `medir-scroll.mjs` publishes as `tarjetas_por_vista`. */
export function tarjetasPorVista(modo: WindowMode): number {
  return disposicion(modo).tarjetasPorVista;
}

/**
 * Height of every item of the stack, in order. A hidden item (folded, or on another screen) is 0 px: it owns no
 * offset and no snap point. Every visible item is one snap step, except the last visible one, which has no card under
 * it and so no bar under it.
 */
export function alturasDeItems(modo: WindowMode, ocultas: readonly boolean[]): number[] {
  const d = disposicion(modo);
  let ultima = -1;
  ocultas.forEach((oculta, i) => {
    if (!oculta) ultima = i;
  });
  return ocultas.map((oculta, i) => (oculta ? 0 : i === ultima ? d.paso - d.alturaBarra : d.paso));
}

// ---- paging over the height index ------------------------------------------------------------------------------

/** The last card a view can start at: the scroll offset that puts the end of the column at the end of the viewport. */
export function ultimaParada(arbol: FenwickArbol, alturaVista: number): number {
  if (arbol.longitud === 0) return 0;
  return Math.min(arbol.longitud - 1, arbol.indicePorOffset(Math.max(0, arbol.total() - alturaVista)));
}

/** The card a page up (-1) or a page down (+1) lands on from card `actual`, skipping hidden cards; never past either end. */
export function paradaVecina(arbol: FenwickArbol, actual: number, sentido: -1 | 1, alturaVista: number): number {
  if (arbol.longitud === 0) return 0;
  const tope = ultimaParada(arbol, alturaVista);
  if (sentido === 1) {
    const siguiente = arbol.indicePorOffset(arbol.offsetDe(actual) + arbol.altura(actual));
    return Math.min(tope, siguiente);
  }
  const inicio = arbol.offsetDe(actual);
  return inicio === 0 ? Math.min(actual, tope) : arbol.indicePorOffset(inicio - 1);
}

// ---- virtual screens -------------------------------------------------------------------------------------------

export const PANTALLA_PRIMERA = 'p1';

export interface Pantalla {
  id: string;
  nombre?: string;
}

/** What the stack needs to know about a card. A capsule's fold and screen live with the shell; the stack only reads them. */
export interface ElementoDePila {
  id: string;
  pantalla?: string;
  plegada?: boolean;
}

/** INV-PANTALLA-02: a card whose screen is missing or unknown belongs to the first screen. */
export function pantallaEfectiva(pantallas: readonly Pantalla[], id: string | undefined): string {
  if (id !== undefined && pantallas.some((p) => p.id === id)) return id;
  return pantallas.length > 0 ? pantallas[0].id : PANTALLA_PRIMERA;
}

/** INV-PANTALLA-01: a card off the active screen, or folded, is hidden (display: none). It stays mounted. */
export function estaOculta(pantallas: readonly Pantalla[], e: ElementoDePila, pantallaActiva: string): boolean {
  return e.plegada === true || pantallaEfectiva(pantallas, e.pantalla) !== pantallaEfectiva(pantallas, pantallaActiva);
}

/** INV-PANTALLA-04: the screen that inherits the cards of a closed one. Throws when the screen is unknown or the only one. */
export function herederaDe(pantallas: readonly Pantalla[], idCerrada: string): string {
  const i = pantallas.findIndex((p) => p.id === idCerrada);
  if (i < 0) throw new RangeError(`herederaDe: unknown screen ${idCerrada}`);
  if (pantallas.length <= 1) throw new RangeError('herederaDe: the last screen has no heir');
  return i > 0 ? pantallas[i - 1].id : pantallas[1].id;
}

export type ResultadoCierre<T extends ElementoDePila> =
  | { ok: true; pantallas: Pantalla[]; elementos: T[]; heredera: string }
  | { ok: false; motivo: 'ultima_pantalla' | 'pantalla_desconocida' };

/**
 * Closes a screen without destroying anything: its cards move to the heir and keep their identity (INV-PANTALLA-04);
 * the last screen cannot be closed (INV-PANTALLA-03). Pure: the inputs are not modified.
 */
export function cerrarPantalla<T extends ElementoDePila>(pantallas: readonly Pantalla[], elementos: readonly T[], idCerrada: string): ResultadoCierre<T> {
  if (!pantallas.some((p) => p.id === idCerrada)) return { ok: false, motivo: 'pantalla_desconocida' };
  if (pantallas.length <= 1) return { ok: false, motivo: 'ultima_pantalla' };
  const heredera = herederaDe(pantallas, idCerrada);
  return {
    ok: true,
    pantallas: pantallas.filter((p) => p.id !== idCerrada),
    elementos: elementos.map((e) => (pantallaEfectiva(pantallas, e.pantalla) === idCerrada ? { ...e, pantalla: heredera } : e)),
    heredera,
  };
}

/** Moves one card to a screen (an unknown screen leaves it on the first one, INV-PANTALLA-02). Throws when the card does not exist. */
export function asignarPantalla<T extends ElementoDePila>(pantallas: readonly Pantalla[], elementos: readonly T[], id: string, pantalla: string): T[] {
  if (!elementos.some((e) => e.id === id)) throw new RangeError(`asignarPantalla: unknown card ${id}`);
  const destino = pantallaEfectiva(pantallas, pantalla);
  return elementos.map((e) => (e.id === id ? { ...e, pantalla: destino } : e));
}
