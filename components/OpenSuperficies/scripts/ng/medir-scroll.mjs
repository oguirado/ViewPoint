#!/usr/bin/env node
// scripts/ng/medir-scroll.mjs — U-19 instrument: paging, unmounts and frame time of the multislot stack.
//
//   node scripts/ng/medir-scroll.mjs --tarjetas N --modo 1/6|1/3 [--json] [--envolver-if]
//        [--estres-ms 30] [--espera-listo-ms 15000] [--plazo-parada-ms 8000]
//
// WHAT IT DOES. It builds a harness page into a private temporary directory (never cockpit/frontend/dist: U-18's
// tests and medir-cls.mjs read it) that mounts the real cockpit/frontend/src/lib/stack/PilaMultislot.svelte with N
// synthetic cards, each holding a live surface: an <iframe srcdoc> with its own document, nonce and ticker, the shape
// of an Xpra HTML5 viewer. It serves the harness from a loopback port (0, read back), opens it in Playwright 1.58.0
// Chromium headless (DEC-18) with the viewport fixed BEFORE navigation, and runs four legs on the running page:
//
//   paginado   the page is scrolled programmatically (smooth) to every stop, one card (1/6) or one snap step (1/3)
//              at a time; at each stop the layout is read; the frames of the scroll animations are sampled
//   teclado    PageDown x3 then PageUp x3 on the focused stack, checked against the snap arithmetic
//   plegado    two cards are folded and unfolded (display: none) and the layout is checked
//   pantallas  half of the cards move to a second virtual screen, the screen is swapped and swapped back
//
// --envolver-if is the contrast: the instrument compiles a copy of PilaMultislot.svelte in which the surface between
// its SRF-SUPERFICIE markers is wrapped in `{#if visible}` (what the mutator names), through the same Vite/Svelte
// pipeline, and runs the same four legs. The counters are the ones of the good run; nothing is edited afterwards.
// The precondition fails (rc 2) when the markers are not found exactly once, so a refactor cannot turn the contrast
// into a no-op. --estres-ms N is an instrument self-test, never used by the oracle: a busy loop of N ms in the
// stack's scroll handler, to show that the frame-time gate can go red.
//
// PROVENANCE OF EVERY FIELD (published in the report's `procedencia`, A-59):
//   ventana, dom_solicitado      DECLARED   the tiling arithmetic and the viewport this harness asks the browser to emulate
//   dom                          READ-BACK  innerWidth x innerHeight of the emulated viewport (NOT an independent measurement)
//   desmontajes                  MEASURED   surface elements removed from the DOM (MutationObserver in the page); second
//                                           sources: the teardown count of the harness surface component (Svelte) and
//                                           the frame detachments the browser reports to Playwright (outside page code)
//   tarjetas_por_vista           MEASURED   cards whose canvas lies fully inside the viewport at every stop (bounding
//                                           boxes in the page); second source: CDP DOM.getBoxModel
//   p95_frame_ms                 MEASURED   p95 (nearest rank) of the main-thread time of each frame during the scroll
//                                           animations: from the frame's BeginFrame time (the rAF timestamp) to a task
//                                           posted from the rAF callback, which runs after that frame's rendering update;
//                                           second source: the compositor scheduler's SendBeginMainFrameToCommit stage
//                                           of the same frames, read from the Chromium trace
//   desmontajes_por_tramo        MEASURED   the DOM observer's removals, attributed to the leg during whose closing read they
//                                           were seen (shows WHERE the counter rises: paging, keys, fold, screens)
//   indice de Fenwick            MEASURED   the component's height index (data-desplazamiento) against the browser's layout (offsetTop)
//   oclusion                     MEASURED   a card more than one viewport away carries data-visible=false and a computed
//                                           content-visibility of hidden, a card inside that band does not (the band is the
//                                           instrument's own geometry); the pause callback the stack reports is compared with the DOM
// THE FRAME FIGURE IS A WORK BUDGET, NOT A CADENCE. Headless Chromium is locked to a 60 Hz vsync: the interval between
// frames is 16.67 ms by construction (published as cadencia_*), so a p95 of intervals could never be <= 16.6. The gated
// figure is how long a frame's main-thread work takes, the quantity the spec's 16.6 ms budget is about.
//
// EVERY run, success or failure, writes ONE report of the SAME shape to stdout (A-53): ok, etapa, error, rc, and null in
// every field that was not measured or on which two sources disagree. A consequence for jq: `null == 0` is false and
// `null <= 16.6` is true, so a consumer keeps the exit code (bash -o pipefail) and reads ok as well.
//
// Exit codes: 0 measured and every source agrees (whatever the values are: the properties are judged by the consumer) ·
// 1 anything else (browser cannot start, protocol failure) · 2 usage or instrument precondition (bad argument, Playwright
// missing, harness cannot be built, mutant markers not found) · 3 two sources of one field disagree, or a source produced
// too few samples to be compared · 4 the page never reached its ready contract, threw, or did not stop where it was told to ·
// 129 / 130 / 143 killed by SIGHUP / SIGINT / SIGTERM (the private temporary root and the browser are removed first).

import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { existsSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { extname, join, normalize, resolve, dirname, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = join(RAIZ, 'cockpit', 'frontend');
const COMPONENTE = join(FRONTEND, 'src', 'lib', 'stack', 'PilaMultislot.svelte');
const PLAYWRIGHT_PIN = '1.58.0';

// ---- host facts (spec 1.1/1.2, MEASURED) — an independent derivation of what src/lib/geometria.ts derives ----
const PANTALLA = { w: 3840, h: 2160 };
const COLUMNAS = 3;
const FILAS = 2;
const BARRA_GNOME_H = 32;
const MARGEN_WM_W = 9;
const BARRA_H = 40;
const ALINEACION_CODEC = 16;
/** The spec's frame budget (F-05) and its stretch target, which is reported and not gated. */
const PRESUPUESTO_FRAME_MS = 16.6;
const OBJETIVO_FRAME_MS = 8.34;
/** The nominal vsync period of the headless compositor: a frame that took more than 1.5 of them missed its deadline. */
const PERIODO_VSYNC_MS = 1000 / 60;
const MIN_FRAMES = 60;
const MARCADOR_INICIO = '<!-- SRF-SUPERFICIE-INICIO -->';
const MARCADOR_FIN = '<!-- SRF-SUPERFICIE-FIN -->';
const CATEGORIAS_TRAZA = ['devtools.timeline', 'disabled-by-default-devtools.timeline.frame'];
const ETAPA_TRAZA = 'SendBeginMainFrameToCommit';
const ETIQUETAS_HISTOGRAMA = ['<1', '1-2', '2-4', '4-8', '8-16.6', '16.6-33.2', '>33.2'];

/** Window (outer), DOM (inner) viewport and stack layout of a CLI mode, from the tiling arithmetic and the spec alone. */
export function derivarDisposicion(modo) {
  const sexto = { w: PANTALLA.w / COLUMNAS, h: (PANTALLA.h - BARRA_GNOME_H) / FILAS };
  let ventana;
  if (modo === '1/6') ventana = { ...sexto };
  else if (modo === '1/3') ventana = { w: sexto.w, h: sexto.h * 2 };
  else throw new RangeError(`unknown mode ${JSON.stringify(modo)}: use 1/6 or 1/3`);
  const dom = { w: ventana.w - MARGEN_WM_W, h: ventana.h };
  const multiplo = (n) => Math.floor(n / ALINEACION_CODEC) * ALINEACION_CODEC;
  const lienzo = { w: multiplo(dom.w), h: multiplo(sexto.h - BARRA_H) };
  const alturaBarra = modo === '1/3' ? BARRA_H : 0;
  const alturaVista = dom.h - BARRA_H;
  // DECLARED by the spec (1.3 and 1.4): one card per viewport at 1/6, two at 1/3 dual. The arithmetic must close on it.
  const tarjetasPorVista = modo === '1/6' ? 1 : 2;
  return {
    modo,
    modo_pila: modo === '1/6' ? 'tile_1_6' : 'column_1_3_dual',
    ventana,
    dom,
    lienzo,
    alturaBarra,
    alturaVista,
    paso: lienzo.h + alturaBarra,
    tarjetasPorVista,
    cierra: tarjetasPorVista * lienzo.h + (tarjetasPorVista - 1) * alturaBarra === alturaVista,
  };
}

/** Height of a stack of `visibles` cards: every card is one step but the last, which has no bar under it. */
export function alturaEsperada(disp, visibles) {
  return visibles <= 0 ? 0 : (visibles - 1) * disp.paso + disp.lienzo.h;
}

/** Nearest-rank percentile: the value at rank ceil(p x n) of the sorted samples. */
export function percentil(muestras, p) {
  if (muestras.length === 0) return null;
  const orden = [...muestras].sort((a, b) => a - b);
  return orden[Math.min(orden.length, Math.max(1, Math.ceil(p * orden.length))) - 1];
}

const redondear = (v, d = 3) => (v === null || v === undefined ? null : Math.round(v * 10 ** d) / 10 ** d);

export function estadisticas(muestras) {
  if (muestras.length === 0) return { n: 0, p50_ms: null, p95_ms: null, p99_ms: null, max_ms: null, sobre_presupuesto: null };
  return {
    n: muestras.length,
    p50_ms: redondear(percentil(muestras, 0.5)),
    p95_ms: redondear(percentil(muestras, 0.95)),
    p99_ms: redondear(percentil(muestras, 0.99)),
    max_ms: redondear(muestras.reduce((m, x) => (x > m ? x : m), 0)),
    sobre_presupuesto: muestras.filter((x) => x > PRESUPUESTO_FRAME_MS).length,
  };
}

/** The same figures without the count over the work budget: an interval between frames is not work, so it is not compared with it. */
export function estadisticasSinPresupuesto(muestras) {
  const { sobre_presupuesto: _omitido, ...resto } = estadisticas(muestras);
  return resto;
}

/**
 * Do two observations of the same frames agree on their p95? The trace stage is the compositor's view of the main-thread
 * work of a frame and the page's figure also counts the scheduling delay before the rAF callback and the task hop after
 * it, so the page is allowed to be larger; both must stay within 2 ms or half of the larger, whichever is more.
 */
export function acuerdoDeP95(pagina, traza) {
  if (pagina === null || traza === null) return false;
  return Math.abs(pagina - traza) <= Math.max(2, 0.5 * Math.max(pagina, traza));
}

/**
 * The contrast: the surface between the SRF-SUPERFICIE markers wrapped in `{#if visible}`. Throws (rc 2) unless the
 * markers are found exactly once and in order.
 */
export function contarMarcadores(fuente) {
  return [...fuente.matchAll(new RegExp(`(${MARCADOR_INICIO})([\\s\\S]*?)(${MARCADOR_FIN})`, 'g'))].length;
}

export function aplicarMutanteIf(fuente) {
  const re = new RegExp(`(${MARCADOR_INICIO})([\\s\\S]*?)(${MARCADOR_FIN})`, 'g');
  const hallazgos = [...fuente.matchAll(re)];
  if (hallazgos.length !== 1) {
    throw new ErrorMedicion('precondicion', 2, `the contrast needs the ${MARCADOR_INICIO} ... ${MARCADOR_FIN} pair exactly once in PilaMultislot.svelte, found ${hallazgos.length}`);
  }
  if (!/\{@render\s+tarjeta\s*\(/.test(hallazgos[0][2])) {
    throw new ErrorMedicion('precondicion', 2, 'the SRF-SUPERFICIE markers do not enclose the {@render tarjeta(...)} of the live surface');
  }
  return fuente.replace(re, (_, inicio, cuerpo, fin) => `${inicio}{#if visible}${cuerpo}{/if}${fin}`);
}

/** The `console.timeStamp` marks of the scroll segments: { id -> { ini, fin } } in trace microseconds. */
export function segmentosDeTraza(eventos) {
  const segmentos = new Map();
  for (const e of eventos) {
    if (e.name !== 'TimeStamp') continue;
    const m = /^srf:(ini|fin):(\d+)$/.exec(String(e.args?.data?.message ?? ''));
    if (m === null) continue;
    const s = segmentos.get(Number(m[2])) ?? { ini: null, fin: null };
    s[m[1]] = e.ts;
    segmentos.set(Number(m[2]), s);
  }
  return [...segmentos.entries()].filter(([, s]) => s.ini !== null && s.fin !== null).map(([id, s]) => ({ id, ...s }));
}

/** Durations (ms) of one compositor pipeline stage whose begin lies inside a scroll segment. */
export function duracionesDeEtapa(eventos, nombre, segmentos) {
  const abiertas = new Map();
  const duraciones = [];
  for (const e of eventos) {
    if (e.name !== nombre) continue;
    const id = e.id2?.local ?? e.id;
    if (e.ph === 'b') abiertas.set(id, e.ts);
    else if (e.ph === 'e' && abiertas.has(id)) {
      const inicio = abiertas.get(id);
      abiertas.delete(id);
      if (segmentos.some((s) => inicio >= s.ini && inicio <= s.fin)) duraciones.push((e.ts - inicio) / 1000);
    }
  }
  return duraciones;
}

// ---- the report --------------------------------------------------------------------------------------------
const PROCEDENCIA = {
  ventana: 'DECLARED: tiling arithmetic (3840/3 x (2160-32)/2); a headless page has no window frame, so this is not a browser reading',
  dom_solicitado: 'DECLARED: the viewport this harness asks the browser to emulate (ventana minus the 9 px window-manager border)',
  dom: 'READ-BACK of the emulated viewport (innerWidth x innerHeight after Playwright set it); NOT an independent measurement',
  desmontajes: 'MEASURED: surface elements ([data-superficie]) removed from the DOM during the run, counted by a MutationObserver in the page (removed subtrees included); second sources: the teardown count of the harness surface component (Svelte onMount cleanup) and the frame detachments the browser reports to Playwright, both outside the DOM observer',
  tarjetas_por_vista: 'MEASURED: canvases ([data-tarjeta]) fully inside the stack viewport at every stop of the paging leg, from bounding boxes in the page; second source: CDP DOM.getBoxModel of the same nodes',
  p95_frame_ms: 'MEASURED: p95 (nearest rank) of the main-thread time of each frame during the programmatic scroll animations: from the frame BeginFrame time (the rAF timestamp) to a MessageChannel task posted from the rAF callback, which runs after the rendering update of that frame; second source: the compositor scheduler stage SendBeginMainFrameToCommit of the same frames, read from the Chromium trace (outside page code)',
  cadencia: 'MEASURED in page: rAF-to-rAF intervals of the same frames. Headless Chromium is locked to a 60 Hz vsync, so the interval is 16.67 ms by construction; it is reported, not gated',
  fenwick_contra_dom: 'MEASURED: the offset of every visible item the component computes from its Fenwick index (data-desplazamiento) against the offset the browser lays out (offsetTop), at every rest point of every leg',
  indice_actual: 'MEASURED: the card index the component derives from the scroll offset through the Fenwick index (data-indice-actual) against the stop the instrument scrolled to (its own arithmetic)',
  oclusion: 'MEASURED: a card more than one viewport away from the stack viewport must carry data-visible=false and a computed content-visibility of hidden, a card inside that band must not; the band is the instrument\'s own geometry (bounding boxes), the computed style is the browser\'s; cards touching the edge of the band are not judged. The pause callback the stack reports is compared with the DOM at the end of the paging leg',
  paradas: 'MEASURED: scrollTop at rest against k x snap step, the instrument\'s own arithmetic from the spec (section 2.2)',
  superficies: 'MEASURED: each surface iframe carries a nonce created when its document loads; the census compares the nonces read through Playwright frames before and after the run',
  ventana_wails_real: 'UNTESTED here: the real Wails/WebKitGTK window (1280x1064 outer, 1271 DOM) is never opened by this instrument (F-14, operator smoke check)',
};

const NO_PROBADO = [
  'the real Wails/WebKitGTK window and its compositor (F-14: operator smoke check); Chromium headless with its software renderer is a measurement proxy (DEC-18)',
  'the frame figure covers the main-thread work of a frame; raster, GPU and presentation time are not measured, and the cadence is locked to the headless 60 Hz vsync, not the 119.88 Hz panel',
  'live Xpra surfaces: the harness surfaces are light iframes with a 10 Hz text ticker, not video streams, so their paint cost is not the cost of a live desktop',
  'the cockpit shell: src/App.svelte does not mount PilaMultislot yet, so this proves the component in a harness page and nothing about the shell around it',
  'a WebSocket per surface: the harness surfaces open none, so the 16.2 s reconnect itself is not exercised; the unmount counters measure the event that would cause it',
];

/** The one shape of the report. Fields not measured stay null; nested objects keep their keys. */
export function informeVacio(o = {}) {
  return {
    ok: false,
    etapa: 'inicio',
    error: null,
    rc: null,
    esquema: 1,
    modo: o.modo ?? null,
    modo_pila: null,
    tarjetas_solicitadas: o.tarjetas ?? null,
    envolver_if: o.envolverIf ?? null,
    estres_ms: o.estresMs ?? null,
    p95_frame_ms: null,
    desmontajes: null,
    tarjetas_por_vista: null,
    tarjetas: null,
    presupuesto_frame_ms: PRESUPUESTO_FRAME_MS,
    objetivo_estiramiento_ms: OBJETIVO_FRAME_MS,
    cumple_objetivo_estiramiento: null,
    ventana: null,
    dom_solicitado: null,
    dom: null,
    esperado: { tarjetas_por_vista: null, paso: null, lienzo: null, alto_vista: null, cierra: null },
    frames: {
      metodo: 'per-frame main-thread time (rAF timestamp -> task after the frame rendering update), scroll animations only; nearest-rank percentiles',
      segmentos: { esperados: null, en_la_traza: null },
      pagina: estadisticas([]),
      traza: { etapa: ETAPA_TRAZA, ...estadisticas([]) },
      cadencia: { ...estadisticasSinPresupuesto([]), periodo_vsync_ms: redondear(PERIODO_VSYNC_MS), perdidos: null, no_gateada: true },
      histograma_pagina_ms: Object.fromEntries(ETIQUETAS_HISTOGRAMA.map((e) => [e, null])),
      muestras_pagina_ms: null,
      muestras_traza_ms: null,
      acuerdo_p95: null,
    },
    desmontajes_fuentes: { dom_mutation_observer: null, svelte_desmontados: null, marcos_desprendidos_playwright: null },
    desmontajes_por_tramo: { paginado: null, teclado: null, plegado: null, pantallas: null },
    montajes: { svelte_montados: null, superficies_anadidas_al_dom: null, vivas_al_final: null },
    superficies: { al_inicio: null, al_final: null, intactas: null, cambiadas: null, perdidas: null, nuevas: null, no_leidas: null },
    paradas: { esperadas: null, alcanzadas: null, desalineadas: null, detalle: null },
    fenwick_contra_dom: { comparaciones: null, discrepancias: null, ejemplos: [] },
    indice_actual: { comprobaciones: null, discrepancias: null },
    oclusion: { comprobaciones: null, discrepancias_atributo: null, discrepancias_css: null, llamadas_al_adaptador: null, ocluidas_al_final: null, incoherentes_con_el_dom: null },
    tramos: {
      paginado: { paradas: null, tarjetas_vistas_al_menos_una_vez: null, scroll_height: null, scroll_height_esperado: null, ancho_de_la_pila: null, alto_de_la_pila: null },
      teclado: { pulsaciones: null, fuera_de_lugar: null, teclas: null },
      plegado: { omitido: null, tarjetas_plegadas: null, ocultas_en_dom: null, scroll_height_antes: null, scroll_height_plegado: null, scroll_height_plegado_esperado: null, scroll_height_despues: null, superficies_en_dom_plegado: null },
      pantallas: { omitido: null, movidas: null, estados: null },
    },
    tarjetas_por_vista_fuentes: { pagina: null, cdp: null, por_parada_pagina: null, por_parada_cdp: null, declarado_por_el_componente: null },
    mutante: { pedido: o.envolverIf ?? null, aplicado: null, marcadores: null, componente_sha256: null, compilado_sha256: null },
    errores_de_pagina: [],
    procedencia: { ...PROCEDENCIA },
    no_probado: [...NO_PROBADO],
    navegador: { chromium: null, playwright: null },
    peticiones: null,
    acuerdo_entre_fuentes: null,
    desacuerdos: [],
    campos_en_desacuerdo: [],
  };
}

/** A failure that knows its stage and its exit code. */
class ErrorMedicion extends Error {
  constructor(etapa, rc, mensaje, causa) {
    super(mensaje, causa === undefined ? undefined : { cause: causa });
    this.etapa = etapa;
    this.rc = rc;
  }
}

const primeraLinea = (t) => String(t).split('\n')[0];
const fmt = (w, h) => (w === null || w === undefined || h === null || h === undefined ? null : `${w}x${h}`);

/** Races a promise against a deadline and always clears the timer, so a finished run never waits for it. */
function conPlazo(promesa, ms, crearError) {
  let temporizador;
  const limite = new Promise((_, mal) => {
    temporizador = setTimeout(() => mal(crearError()), ms);
  });
  return Promise.race([promesa, limite]).finally(() => clearTimeout(temporizador));
}

// ---- command line ------------------------------------------------------------------------------------------
function leerArgumentos(argv) {
  const o = { tarjetas: null, modo: null, json: false, envolverIf: false, estresMs: 0, esperaListo: 15000, plazoParada: 8000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const valor = () => {
      if (i + 1 >= argv.length) throw new ErrorMedicion('argumentos', 2, `${a} needs a value`);
      return argv[++i];
    };
    if (a === '--tarjetas') o.tarjetas = Number(valor());
    else if (a === '--modo') o.modo = valor();
    else if (a === '--json') o.json = true;
    else if (a === '--envolver-if') o.envolverIf = true;
    else if (a === '--estres-ms') o.estresMs = Number(valor());
    else if (a === '--espera-listo-ms') o.esperaListo = Number(valor());
    else if (a === '--plazo-parada-ms') o.plazoParada = Number(valor());
    else throw new ErrorMedicion('argumentos', 2, `unknown argument ${a}`);
  }
  if (o.modo === null) throw new ErrorMedicion('argumentos', 2, '--modo 1/6|1/3 is required');
  if (o.tarjetas === null) throw new ErrorMedicion('argumentos', 2, '--tarjetas N is required');
  if (!Number.isInteger(o.tarjetas) || o.tarjetas < 1 || o.tarjetas > 200) throw new ErrorMedicion('argumentos', 2, '--tarjetas must be an integer between 1 and 200');
  for (const [k, v] of [['--estres-ms', o.estresMs], ['--espera-listo-ms', o.esperaListo], ['--plazo-parada-ms', o.plazoParada]]) {
    if (!Number.isFinite(v) || v < 0) throw new ErrorMedicion('argumentos', 2, `${k} must be a non-negative number`);
  }
  try {
    derivarDisposicion(o.modo);
  } catch (e) {
    throw new ErrorMedicion('argumentos', 2, e.message, e);
  }
  return o;
}

// ---- the harness page --------------------------------------------------------------------------------------
// Sources written into a private temporary directory. They import the REAL component, geometry and screen modules by
// absolute path, so the page exercises the tree under test and nothing else.
const FUENTE_SUPERFICIE = `<script>
  import { onMount } from 'svelte';
  let { id } = $props();
  // Second source of the unmount count that does not read the DOM: the component instance's own teardown.
  onMount(() => {
    const med = window.__srfMedicion;
    med.montados++;
    return () => {
      med.desmontados++;
    };
  });
  const contenido =
    '<!doctype html><html><head><meta charset="utf-8"><title>surface</title></head>' +
    '<body style="margin:0;background:linear-gradient(135deg,#1b2a41,#324a5f);color:#dbe7ff;font:14px monospace">' +
    '<div id="t">0</div><script>window.__nonce=Math.random().toString(36).slice(2)+Date.now().toString(36);' +
    'var n=0;setInterval(function(){document.getElementById("t").textContent=++n},100)<\\/script></body></html>';
</script>

<iframe data-superficie data-id={id} title={'surface ' + id} sandbox="allow-scripts allow-same-origin" srcdoc={contenido}></iframe>

<style>
  iframe {
    display: block;
    border: 0;
    width: 100%;
    height: 100%;
  }
</style>
`;

const fuenteHarness = () => `<script>
  import { onMount } from 'svelte';
  import PilaMultislot from ${JSON.stringify(COMPONENTE)};
  import { asignarPantalla } from ${JSON.stringify(join(FRONTEND, 'src', 'lib', 'stack', 'pantallas.ts'))};
  import { BARRA_H } from ${JSON.stringify(join(FRONTEND, 'src', 'lib', 'geometria.ts'))};
  import Superficie from './Superficie.svelte';

  let { cuantas, modo } = $props();
  const pantallas = [{ id: 'p1' }, { id: 'p2' }];
  let tarjetas = $state(Array.from({ length: cuantas }, (_, i) => ({ id: 'srf-ng-' + i, pantalla: 'p1', plegada: false })));
  let pantallaActiva = $state('p1');
  let indice = $state(0);
  let pila;
  // The surface adapter's pause(ocluida): the harness only records what the stack reports.
  const alCambiarOclusion = (id, ocluida) => {
    const oclusion = window.__srfMedicion.oclusion;
    oclusion.llamadas++;
    oclusion.estado[id] = ocluida;
  };

  onMount(() => {
    window.__srfPagina = {
      plegar(i, valor) {
        tarjetas[i].plegada = valor;
      },
      moverAPantalla(i, id) {
        tarjetas = asignarPantalla(pantallas, tarjetas, tarjetas[i].id, id);
      },
      activarPantalla(id) {
        pantallaActiva = id;
      },
      irA(i, suave) {
        pila.irA(i, suave);
      },
    };
    // Ready contract: two frames after mount, as the cockpit shell declares it.
    requestAnimationFrame(() => requestAnimationFrame(() => (document.documentElement.dataset.appReady = 'true')));
    return () => {
      delete window.__srfPagina;
    };
  });
</script>

<header class="barra" data-barra style:height="{BARRA_H}px"><span>srf medir-scroll harness</span></header>
<PilaMultislot bind:this={pila} {tarjetas} {modo} {pantallas} {pantallaActiva} {alCambiarOclusion} bind:indice>
  {#snippet tarjeta(t)}
    <Superficie id={t.id} />
  {/snippet}
  {#snippet barra(t)}
    <span class="rotulo" data-rotulo>{t.id}</span>
  {/snippet}
</PilaMultislot>

<style>
  .barra {
    box-sizing: border-box;
    display: flex;
    align-items: center;
    padding: 0 12px;
    background: #161d2e;
    color: #94a3b8;
    font: 12px monospace;
  }
  .rotulo {
    display: block;
    padding: 0 12px;
    color: #94a3b8;
    font: 12px/1.5 monospace;
  }
</style>
`;

const FUENTE_ENTRADA = `import { mount } from 'svelte';
import Harness from './Harness.svelte';
const q = new URLSearchParams(location.search);
const estres = Number(q.get('estres') || 0);
mount(Harness, { target: document.body, props: { cuantas: Number(q.get('n')), modo: q.get('modo') } });
if (estres > 0) {
  // Instrument self-test: a busy loop in the stack's scroll handler, so that the frame-time gate can be shown to go red.
  document.querySelector('[data-pila]').addEventListener('scroll', () => {
    const t = performance.now();
    while (performance.now() - t < estres);
  });
}
`;

const HTML_HARNESS = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>srf medir-scroll harness</title>
    <link rel="stylesheet" href="/harness.css" />
    <style>
      html, body { margin: 0; background: #07090e; }
      body { width: 100%; height: 100vh; overflow: hidden; }
    </style>
    <script type="module" src="/harness.js"></script>
  </head>
  <body></body>
</html>
`;

/** Loads a module of the frontend's own node_modules (Vite and the Svelte plugin are ESM-only and not resolvable from here). */
async function importarDelFrontend(...partes) {
  const ruta = join(FRONTEND, 'node_modules', ...partes);
  if (!existsSync(ruta)) throw new ErrorMedicion('precondicion', 2, `${ruta} not found: cockpit/frontend has no installed dependencies (run npm ci there)`);
  return import(pathToFileURL(ruta).href);
}

/**
 * Builds the harness into `destino/out` with Vite's Node API. Never cockpit/frontend/dist. Returns the hashes of the
 * component as written and as compiled, and whether the contrast was applied.
 */
async function construirHarness(destino, { envolverIf, informe }) {
  if (resolve(destino).startsWith(join(FRONTEND, 'dist'))) throw new ErrorMedicion('precondicion', 2, 'refusing to build into cockpit/frontend/dist');
  const vite = await importarDelFrontend('vite', 'dist', 'node', 'index.js');
  const { svelte, vitePreprocess } = await importarDelFrontend('@sveltejs', 'vite-plugin-svelte', 'src', 'index.js');
  const fuentes = join(destino, 'src');
  await mkdir(fuentes, { recursive: true });
  await writeFile(join(fuentes, 'Superficie.svelte'), FUENTE_SUPERFICIE);
  await writeFile(join(fuentes, 'Harness.svelte'), fuenteHarness());
  await writeFile(join(fuentes, 'entrada.js'), FUENTE_ENTRADA);

  const original = await readFile(COMPONENTE, 'utf8');
  informe.mutante.componente_sha256 = createHash('sha256').update(original).digest('hex');
  let compilado = original;
  if (envolverIf) {
    compilado = aplicarMutanteIf(original);
  }
  informe.mutante.marcadores = contarMarcadores(original);
  informe.mutante.compilado_sha256 = createHash('sha256').update(compilado).digest('hex');
  informe.mutante.aplicado = envolverIf ? compilado !== original : false;

  const plugins = [
    {
      // The harness files live outside the frontend: their bare imports (svelte) resolve as if written inside it.
      name: 'srf-resolver-desde-el-frontend',
      enforce: 'pre',
      async resolveId(fuente, importador, opciones) {
        if (!importador || !importador.startsWith(fuentes) || fuente.startsWith('.') || fuente.startsWith('/') || fuente.startsWith('\0')) return null;
        return this.resolve(fuente, join(FRONTEND, 'src', 'x.js'), { ...opciones, skipSelf: true });
      },
    },
    {
      // The contrast, applied in memory to the source Svelte compiles: the file on disk is never touched.
      name: 'srf-envolver-if',
      enforce: 'pre',
      transform(codigo, id) {
        // Only the component module itself: Svelte also asks for `<file>?svelte&type=style...`, which must stay untouched.
        if (!envolverIf || id !== COMPONENTE) return null;
        return { code: aplicarMutanteIf(codigo), map: null };
      },
    },
    svelte({ preprocess: vitePreprocess() }),
  ];
  try {
    await vite.build({
      root: FRONTEND,
      configFile: false,
      logLevel: 'silent',
      cacheDir: join(destino, 'cache'),
      plugins,
      build: {
        outDir: join(destino, 'out'),
        emptyOutDir: false,
        target: 'es2022',
        sourcemap: false,
        minify: false,
        rollupOptions: { input: join(fuentes, 'entrada.js'), output: { entryFileNames: 'harness.js', assetFileNames: 'harness[extname]' } },
      },
    });
  } catch (e) {
    throw new ErrorMedicion('construccion', 2, `the harness page could not be built: ${primeraLinea(e.message)}`, e);
  }
  return join(destino, 'out');
}

// ---- static server -----------------------------------------------------------------------------------------
const TIPOS = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };

async function servir(dist) {
  const peticiones = [];
  const servidor = createServer(async (req, res) => {
    const ruta = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    peticiones.push(ruta);
    const cabeceras = (tipo) => ({ 'content-type': tipo, 'cache-control': 'no-store' });
    if (ruta === '/' || ruta === '/index.html') {
      res.writeHead(200, cabeceras(TIPOS['.html']));
      res.end(HTML_HARNESS);
      return;
    }
    const destino = normalize(join(dist, ruta));
    if (!destino.startsWith(dist + sep) || !existsSync(destino)) {
      res.writeHead(404, cabeceras('text/plain'));
      res.end('not found');
      return;
    }
    res.writeHead(200, cabeceras(TIPOS[extname(destino)] ?? 'application/octet-stream'));
    res.end(await readFile(destino));
  });
  await new Promise((ok, mal) => {
    servidor.once('error', mal);
    servidor.listen(0, '127.0.0.1', ok);
  });
  return { servidor, puerto: servidor.address().port, peticiones };
}

// ---- page-side instrumentation (installed by a context-wide init script, before any page script) ----------------
function instrumentarPagina() {
  if (window.top !== window) return; // the surfaces' own documents are read through Playwright, not instrumented
  const med = {
    montados: 0,
    desmontados: 0,
    armado: false,
    base: { montados: 0, desmontados: 0 },
    retirados: 0,
    anadidos: 0,
    trabajo: [],
    cadencia: [],
    muestreando: false,
    previo: null,
    segmentos: 0,
    fin: null,
    observador: null,
    oclusion: { llamadas: 0, estado: {} },
  };
  window.__srfMedicion = med;
  const contarSuperficies = (nodo) => {
    if (!nodo || nodo.nodeType !== 1) return 0;
    return (nodo.matches('[data-superficie]') ? 1 : 0) + nodo.querySelectorAll('[data-superficie]').length;
  };
  const drenar = () => {
    for (const r of med.observador.takeRecords()) {
      for (const n of r.removedNodes) med.retirados += contarSuperficies(n);
      for (const n of r.addedNodes) med.anadidos += contarSuperficies(n);
    }
  };
  med.observador = new MutationObserver((registros) => {
    if (!med.armado) return;
    for (const r of registros) {
      for (const n of r.removedNodes) med.retirados += contarSuperficies(n);
      for (const n of r.addedNodes) med.anadidos += contarSuperficies(n);
    }
  });
  med.observador.observe(document, { childList: true, subtree: true });

  // Frame sampler: one rAF per frame; the task posted from the callback runs after that frame's rendering update.
  const canal = new MessageChannel();
  canal.port1.onmessage = (ev) => {
    med.trabajo.push(performance.now() - ev.data);
  };
  const tick = (t) => {
    if (!med.muestreando) return;
    if (med.previo !== null) med.cadencia.push(t - med.previo);
    med.previo = t;
    canal.port2.postMessage(t);
    requestAnimationFrame(tick);
  };
  const cuadro = () => new Promise((ok) => requestAnimationFrame(() => ok()));
  const tiempo = (ms) => new Promise((ok) => setTimeout(() => ok(false), ms));
  const pila = () => document.querySelector('[data-pila]');

  med.armar = () => {
    drenar();
    med.armado = true;
    med.base = { montados: med.montados, desmontados: med.desmontados };
    med.retirados = 0;
    med.anadidos = 0;
  };
  med.contadores = () => {
    drenar();
    return {
      retirados: med.retirados,
      anadidos: med.anadidos,
      montados: med.montados - med.base.montados,
      desmontados: med.desmontados - med.base.desmontados,
      vivas: document.querySelectorAll('[data-superficie]').length,
    };
  };
  /** One programmatic scroll to `top`; its frames are sampled between two trace marks the trace reader finds again. */
  med.paginar = async (top, plazo) => {
    const r = pila();
    const id = ++med.segmentos;
    const fin = new Promise((ok) => r.addEventListener('scrollend', () => ok(true), { once: true }));
    med.previo = null;
    console.timeStamp(`srf:ini:${id}`);
    med.muestreando = true;
    requestAnimationFrame(tick);
    r.scrollTo({ top, behavior: 'smooth' });
    const senal = await Promise.race([fin, tiempo(plazo)]);
    await cuadro();
    await cuadro();
    med.muestreando = false;
    console.timeStamp(`srf:fin:${id}`);
    await cuadro(); // the message of the last sampled frame
    return { id, senal, terminado: Math.abs(r.scrollTop - top) < 1, scrollTop: r.scrollTop };
  };
  /** For a scroll the caller starts itself (a key press): arm before, wait after. */
  med.armarFin = () => {
    med.fin = new Promise((ok) => pila().addEventListener('scrollend', () => ok(true), { once: true }));
  };
  med.esperarFin = async (plazo, top) => {
    const senal = await Promise.race([med.fin, tiempo(plazo)]);
    await cuadro();
    await cuadro();
    const r = pila();
    return { senal, terminado: Math.abs(r.scrollTop - top) < 1, scrollTop: r.scrollTop };
  };
  med.cuadros = async (n) => {
    for (let i = 0; i < n; i++) await cuadro();
  };
  /** Waits until no surface was added or removed for `ms`, or for the deadline: the counters are final after this. */
  med.esperarQuietud = async (ms, plazo) => {
    const limite = performance.now() + plazo;
    let previo = JSON.stringify(med.contadores());
    let desde = performance.now();
    while (performance.now() < limite) {
      await cuadro();
      const actual = JSON.stringify(med.contadores());
      if (actual !== previo) {
        previo = actual;
        desde = performance.now();
      } else if (performance.now() - desde >= ms) return true;
    }
    return false;
  };
  med.frames = () => ({ trabajo: med.trabajo, cadencia: med.cadencia });
  med.leerReposo = () => {
    const r = pila();
    const vr = r.getBoundingClientRect();
    const items = [...r.querySelectorAll('[data-item]')].map((el) => ({
      id: el.dataset.id,
      oculto: getComputedStyle(el).display === 'none',
      offsetTop: el.offsetTop,
      altura: el.offsetHeight,
      fenwick: Number(el.dataset.desplazamiento),
    }));
    const tarjetas = [...r.querySelectorAll('[data-tarjeta]')].map((el) => {
      const b = el.getBoundingClientRect();
      return { id: el.dataset.id, top: b.top, bottom: b.bottom, h: b.height, visible: el.dataset.visible, contentVisibility: getComputedStyle(el).contentVisibility };
    });
    return {
      scrollTop: r.scrollTop,
      scrollHeight: r.scrollHeight,
      clientWidth: r.clientWidth,
      clientHeight: r.clientHeight,
      vista: { top: vr.top, bottom: vr.bottom },
      indice: Number(r.dataset.indiceActual),
      tarjetasPorVistaDeclaradas: Number(r.dataset.tarjetasPorVista),
      items,
      tarjetas,
      oclusion: { llamadas: med.oclusion.llamadas, estado: { ...med.oclusion.estado } },
      superficies: document.querySelectorAll('[data-superficie]').length,
    };
  };
  med.datosDePagina = () => ({
    dom: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio },
    listo: document.documentElement.dataset.appReady ?? null,
    hayPila: pila() !== null,
  });
}

// ---- measurement helpers -------------------------------------------------------------------------------------
/** Census of the surfaces through Playwright: id and the nonce each iframe document created when it loaded. */
async function censoDeSuperficies(pagina) {
  const filas = [];
  for (const marco of pagina.frames()) {
    if (marco === pagina.mainFrame()) continue;
    try {
      const elemento = await marco.frameElement();
      const id = await elemento.getAttribute('data-id');
      const nonce = await marco.evaluate(() => window.__nonce ?? null);
      filas.push({ id, nonce });
    } catch (e) {
      // A frame that cannot be read is not silently dropped from the census: it is counted and turns the run red.
      filas.push({ id: null, nonce: null, error: primeraLinea(e.message) });
    }
  }
  return filas;
}

async function cajaCdp(cdp, nodeId) {
  try {
    const { model } = await cdp.send('DOM.getBoxModel', { nodeId });
    const q = model.border;
    return { top: Math.min(q[1], q[3], q[5], q[7]), bottom: Math.max(q[1], q[3], q[5], q[7]), h: model.height };
  } catch (e) {
    if (/box model/i.test(String(e && e.message))) return null; // display: none has no box; any other protocol error is a failure
    throw e;
  }
}

const dentro = (caja, vista) => caja !== null && caja.h > 0 && caja.top >= vista.top - 0.5 && caja.bottom <= vista.bottom + 0.5;

function histograma(muestras) {
  const limites = [1, 2, 4, 8, PRESUPUESTO_FRAME_MS, 2 * PRESUPUESTO_FRAME_MS];
  const etiquetas = ETIQUETAS_HISTOGRAMA;
  const cuentas = new Array(etiquetas.length).fill(0);
  for (const m of muestras) {
    let i = limites.findIndex((l) => m < l);
    if (i < 0) i = limites.length;
    cuentas[i]++;
  }
  return Object.fromEntries(etiquetas.map((e, i) => [e, cuentas[i]]));
}

/** Census comparison: what the surfaces looked like before the run against after it. */
export function compararCensos(antes, despues) {
  const previo = new Map(antes.filter((s) => s.id !== null).map((s) => [s.id, s.nonce]));
  const ahora = new Map(despues.filter((s) => s.id !== null).map((s) => [s.id, s.nonce]));
  let intactas = 0;
  let cambiadas = 0;
  let perdidas = 0;
  let nuevas = 0;
  for (const [id, nonce] of previo) {
    if (!ahora.has(id)) perdidas++;
    else if (ahora.get(id) === nonce && nonce !== null) intactas++;
    else cambiadas++;
  }
  for (const id of ahora.keys()) if (!previo.has(id)) nuevas++;
  return { intactas, cambiadas, perdidas, nuevas };
}

/**
 * Where the observations of the unmount count contradict each other. `f` holds the DOM observer's removals, the harness
 * component's teardown count and the frame detachments Playwright saw, the number of mounts and of live surfaces, and the
 * census comparison. An empty list means every source tells the same story.
 */
export function desacuerdosDeDesmontajes(f) {
  const salida = [];
  if (!f.estable) salida.push('the surface counters did not settle within 8 s of the last leg, so they are not final');
  const fuentes = [f.dom, f.svelte, f.marcos];
  const unanimes = new Set(fuentes).size === 1;
  if (!unanimes) salida.push(`unmounts: DOM observer ${f.dom} vs Svelte teardown ${f.svelte} vs frames detached (Playwright) ${f.marcos}`);
  const vivasEsperadas = f.vivasAlArmar + f.montados - f.svelte;
  if (f.vivasAlFinal !== vivasEsperadas) salida.push(`bookkeeping: ${f.vivasAlArmar} surfaces alive when armed + ${f.montados} mounted - ${f.svelte} destroyed = ${vivasEsperadas}, but the DOM holds ${f.vivasAlFinal}`);
  if (unanimes && fuentes[0] === 0 && (f.censo.cambiadas > 0 || f.censo.perdidas > 0)) {
    salida.push(`no unmount was counted, yet ${f.censo.cambiadas} surface(s) changed nonce and ${f.censo.perdidas} vanished between the census before and after the run`);
  }
  return salida;
}

// ---- measurement -------------------------------------------------------------------------------------------
/** Measures into `informe` (created by main) so that a failure at any stage leaves what was read before it. */
async function medir(o, informe) {
  informe.etapa = 'precondicion';
  const requerir = createRequire(join(FRONTEND, 'package.json'));
  let pw;
  try {
    pw = requerir('playwright-core');
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `playwright-core is not installed under cockpit/frontend (run npm ci): ${primeraLinea(e.message)}`, e);
  }
  const version = requerir('playwright-core/package.json').version;
  informe.navegador.playwright = version;
  if (version !== PLAYWRIGHT_PIN) throw new ErrorMedicion('precondicion', 2, `playwright-core ${version} found, ${PLAYWRIGHT_PIN} is pinned: any other version would try to download a browser`);

  const disp = derivarDisposicion(o.modo);
  informe.modo_pila = disp.modo_pila;
  informe.ventana = fmt(disp.ventana.w, disp.ventana.h);
  informe.dom_solicitado = fmt(disp.dom.w, disp.dom.h);
  Object.assign(informe.esperado, { tarjetas_por_vista: disp.tarjetasPorVista, paso: disp.paso, lienzo: fmt(disp.lienzo.w, disp.lienzo.h), alto_vista: disp.alturaVista, cierra: disp.cierra });

  // One private root for everything this run writes (harness sources, build output, Vite cache, the browser profile):
  // the run always removes it, so nothing is left behind whatever stage fails. A killed run (a timeout's SIGTERM,
  // Ctrl-C) must not leave its browser or its root behind either: the handlers are installed BEFORE the root exists and
  // the root is created synchronously, so no signal can find the directory without a handler that knows about it.
  let raizTemporal = null;
  let web = null;
  let navegador = null;
  let lanzamiento = null; // the browser launch while it is in flight: a signal must wait for it before it removes the profile under the root
  const CODIGOS_DE_SENAL = { SIGHUP: 129, SIGINT: 130, SIGTERM: 143 };
  const alSenal = (senal) => {
    // The removal is synchronous and followed at once by the exit: a build still running cannot write into the root in between.
    const salir = () => {
      try {
        if (raizTemporal !== null) rmSync(raizTemporal, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
      } catch (e) {
        process.stderr.write(`medir-scroll: the private root ${raizTemporal} could not be removed after ${senal}: ${primeraLinea(e.message)}\n`);
      }
      process.exit(CODIGOS_DE_SENAL[senal]);
    };
    const pendiente = navegador !== null ? navegador : lanzamiento;
    if (pendiente === null) salir();
    else Promise.resolve(pendiente).then((n) => n.close()).then(salir, salir);
  };
  for (const senal of Object.keys(CODIGOS_DE_SENAL)) process.once(senal, alSenal);
  const tmpAnterior = process.env.TMPDIR;
  try {
    raizTemporal = mkdtempSync(join(tmpdir(), 'srf-medir-scroll-'));
    const tmpNavegador = join(raizTemporal, 'tmp');
    mkdirSync(tmpNavegador);
    process.env.TMPDIR = tmpNavegador;
    informe.etapa = 'construccion';
    const dist = await construirHarness(raizTemporal, { envolverIf: o.envolverIf, informe });
    web = await servir(dist);

    informe.etapa = 'navegador';
    try {
      // Playwright's own SIGINT/SIGTERM/SIGHUP handlers are off: the one above is the only one, and it waits for this launch.
      lanzamiento = pw.chromium.launch({ headless: true, handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false });
      navegador = await lanzamiento;
    } catch (e) {
      throw new ErrorMedicion('navegador', 1, `the browser could not be launched: ${primeraLinea(e.message)}`, e);
    }
    informe.navegador.chromium = navegador.version();
    const contexto = await navegador.newContext({ viewport: { width: disp.dom.w, height: disp.dom.h }, screen: { width: PANTALLA.w, height: PANTALLA.h }, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: 'no-preference' });
    await contexto.addInitScript(instrumentarPagina);
    const pagina = await contexto.newPage();
    const erroresDePagina = informe.errores_de_pagina;
    let alFallar = null;
    const falloDePagina = new Promise((_, mal) => {
      alFallar = mal;
    });
    falloDePagina.catch(() => {}); // consumed only while waiting for the ready contract
    pagina.on('pageerror', (e) => {
      erroresDePagina.push(primeraLinea(e.message));
      alFallar(new ErrorMedicion('listo', 4, `the page threw before reaching its ready contract: ${primeraLinea(e.message)}`, e));
    });
    let contandoMarcos = false;
    let marcosDesprendidos = 0;
    pagina.on('framedetached', () => {
      if (contandoMarcos) marcosDesprendidos++;
    });
    const cdp = await contexto.newCDPSession(pagina);

    informe.etapa = 'carga';
    try {
      await pagina.goto(`http://127.0.0.1:${web.puerto}/?n=${o.tarjetas}&modo=${disp.modo_pila}&estres=${o.estresMs}`, { waitUntil: 'load' });
    } catch (e) {
      throw new ErrorMedicion('carga', 1, `the page could not be loaded: ${primeraLinea(e.message)}`, e);
    }

    informe.etapa = 'listo';
    try {
      await Promise.race([pagina.waitForFunction(() => document.documentElement.dataset.appReady === 'true', null, { timeout: o.esperaListo }), falloDePagina]);
    } catch (e) {
      if (e instanceof ErrorMedicion) throw e;
      throw new ErrorMedicion('listo', 4, `the page never set data-app-ready="true": ${primeraLinea(e.message)}`, e);
    }
    await pagina.evaluate(async () => {
      await document.fonts.ready;
      await window.__srfMedicion.cuadros(2);
    });
    const datos = await pagina.evaluate(() => window.__srfMedicion.datosDePagina());
    informe.dom = fmt(datos.dom.w, datos.dom.h);
    if (!datos.hayPila) throw new ErrorMedicion('listo', 4, 'the page has no [data-pila] element: the component did not render');
    await pagina.evaluate(() => window.__srfMedicion.cuadros(20)); // the surfaces' documents load, the occlusion observer reports once

    // ---- before anything moves: the surfaces, the nodes to read through CDP, the trace ----
    const antes = await censoDeSuperficies(pagina);
    await cdp.send('DOM.enable');
    const documento = await cdp.send('DOM.getDocument', { depth: 0 });
    const nodoPila = (await cdp.send('DOM.querySelector', { nodeId: documento.root.nodeId, selector: '[data-pila]' })).nodeId;
    const nodosTarjeta = (await cdp.send('DOM.querySelectorAll', { nodeId: documento.root.nodeId, selector: '[data-tarjeta]' })).nodeIds;
    const eventosTraza = [];
    cdp.on('Tracing.dataCollected', (p) => eventosTraza.push(...p.value));
    const trazaCompleta = new Promise((ok) => cdp.once('Tracing.tracingComplete', ok));
    await cdp.send('Tracing.start', { transferMode: 'ReportEvents', traceConfig: { recordMode: 'recordUntilFull', includedCategories: CATEGORIAS_TRAZA } });
    await pagina.evaluate(() => window.__srfMedicion.armar());
    contandoMarcos = true;
    const alArmar = await pagina.evaluate(() => window.__srfMedicion.contadores());

    const evaluar = (fn, arg) => pagina.evaluate(fn, arg);
    const leer = () => evaluar(() => window.__srfMedicion.leerReposo());
    const cuadros = (n) => evaluar((k) => window.__srfMedicion.cuadros(k), n);
    const comparaciones = { n: 0, malas: 0, ejemplos: [] };
    // Where the unmount counter rises: the DOM observer's removals, attributed to the leg during whose closing read it saw them.
    let retiradosPrevios = 0;
    const cerrarTramo = async (nombre) => {
      const c = await evaluar(() => window.__srfMedicion.contadores());
      informe.desmontajes_por_tramo[nombre] = c.retirados - retiradosPrevios;
      retiradosPrevios = c.retirados;
    };
    const compararFenwick = (lectura, donde) => {
      for (const it of lectura.items) {
        if (it.oculto) continue;
        comparaciones.n++;
        if (Math.abs(it.offsetTop - it.fenwick) > 0.5) {
          comparaciones.malas++;
          if (comparaciones.ejemplos.length < 5) comparaciones.ejemplos.push({ donde, id: it.id, offsetTop_dom: it.offsetTop, desplazamiento_fenwick: it.fenwick });
        }
      }
    };

    // ---- leg 1: paging ----
    informe.etapa = 'paginado';
    const paradas = Math.max(1, o.tarjetas - disp.tarjetasPorVista + 1);
    const detalle = [];
    const vistasPagina = [];
    const vistasCdp = [];
    const vistas = new Set();
    let desalineadas = 0;
    let indiceMalo = 0;
    const oclusion = { n: 0, atributo: 0, css: 0 };
    for (let k = 0; k < paradas; k++) {
      const destino = k * disp.paso;
      if (k > 0) {
        const r = await evaluar(([top, plazo]) => window.__srfMedicion.paginar(top, plazo), [destino, o.plazoParada]);
        if (!r.terminado) throw new ErrorMedicion('paginado', 4, `the stack did not stop at card ${k} (scrollTop ${r.scrollTop}, wanted ${destino}) within ${o.plazoParada} ms`);
      }
      const l = await leer();
      const enPagina = l.tarjetas.filter((t) => dentro(t, l.vista));
      enPagina.forEach((t) => vistas.add(t.id));
      const caja = await cajaCdp(cdp, nodoPila);
      let enCdp = 0;
      if (caja !== null) for (const nodo of nodosTarjeta) if (dentro(await cajaCdp(cdp, nodo), caja)) enCdp++;
      vistasPagina.push(enPagina.length);
      vistasCdp.push(enCdp);
      if (Math.abs(l.scrollTop - destino) > 1) desalineadas++;
      if (l.indice !== k) indiceMalo++;
      compararFenwick(l, `paginado:${k}`);
      const alturaVista = l.vista.bottom - l.vista.top;
      for (const t of l.tarjetas) {
        const banda = { top: l.vista.top - alturaVista, bottom: l.vista.bottom + alturaVista };
        const dentroDeLaBanda = t.bottom > banda.top + 1 && t.top < banda.bottom - 1;
        const fueraDeLaBanda = t.bottom < banda.top - 1 || t.top > banda.bottom + 1;
        if (!dentroDeLaBanda && !fueraDeLaBanda) continue;
        oclusion.n++;
        if ((t.visible === 'true') !== dentroDeLaBanda) oclusion.atributo++;
        if ((t.contentVisibility !== 'hidden') !== dentroDeLaBanda) oclusion.css++;
      }
      detalle.push({ k, scroll_top: l.scrollTop, esperado: destino, indice_componente: l.indice, tarjetas_en_vista_pagina: enPagina.length, tarjetas_en_vista_cdp: enCdp });
    }
    const fin1 = await leer();
    const incoherentes = fin1.tarjetas.filter((t) => (fin1.oclusion.estado[t.id] ?? false) !== (t.visible === 'false')).length;
    informe.oclusion = { comprobaciones: oclusion.n, discrepancias_atributo: oclusion.atributo, discrepancias_css: oclusion.css, llamadas_al_adaptador: fin1.oclusion.llamadas, ocluidas_al_final: fin1.tarjetas.filter((t) => t.visible === 'false').length, incoherentes_con_el_dom: incoherentes };
    Object.assign(informe.tramos.paginado, { paradas, tarjetas_vistas_al_menos_una_vez: vistas.size, scroll_height: fin1.scrollHeight, scroll_height_esperado: alturaEsperada(disp, o.tarjetas), ancho_de_la_pila: fin1.clientWidth, alto_de_la_pila: fin1.clientHeight });
    informe.paradas = { esperadas: paradas, alcanzadas: paradas - desalineadas, desalineadas, detalle };
    informe.indice_actual = { comprobaciones: paradas, discrepancias: indiceMalo };
    await cerrarTramo('paginado');

    // ---- leg 2: keyboard ----
    informe.etapa = 'teclado';
    await evaluar(() => window.__srfPagina.irA(0, false));
    await cuadros(3);
    await pagina.focus('[data-pila]');
    const pulsaciones = Math.min(3, Math.max(0, paradas - 1));
    const teclas = [];
    for (const [tecla, sentido] of [['PageDown', 1], ['PageUp', -1]]) {
      for (let j = 1; j <= pulsaciones; j++) {
        const partida = (await leer()).scrollTop;
        const objetivo = partida + sentido * disp.paso;
        await evaluar(() => window.__srfMedicion.armarFin());
        await pagina.keyboard.press(tecla);
        const r = await evaluar(([plazo, top]) => window.__srfMedicion.esperarFin(plazo, top), [o.plazoParada, objetivo]);
        const l = await leer();
        compararFenwick(l, `teclado:${tecla}:${j}`);
        teclas.push({ tecla, n: j, scroll_top: l.scrollTop, esperado: objetivo, en_su_sitio: r.terminado });
      }
    }
    Object.assign(informe.tramos.teclado, { pulsaciones: teclas.length, fuera_de_lugar: teclas.filter((t) => !t.en_su_sitio).length, teclas });
    await cerrarTramo('teclado');

    // ---- legs 3 and 4: fold, and a swap of virtual screens. Both only hide: nothing may unmount. ----
    if (o.tarjetas >= 4) {
      informe.etapa = 'plegado';
      const a = Math.floor(o.tarjetas / 3);
      const b = a + 1;
      const base = await leer();
      await evaluar(([x, y]) => { window.__srfPagina.plegar(x, true); window.__srfPagina.plegar(y, true); }, [a, b]);
      await cuadros(4);
      const plegadas = await leer();
      compararFenwick(plegadas, 'plegado');
      await evaluar(([x, y]) => { window.__srfPagina.plegar(x, false); window.__srfPagina.plegar(y, false); }, [a, b]);
      await cuadros(4);
      const devueltas = await leer();
      compararFenwick(devueltas, 'desplegado');
      Object.assign(informe.tramos.plegado, {
        tarjetas_plegadas: [a, b],
        ocultas_en_dom: plegadas.items.filter((it) => it.oculto).length,
        scroll_height_antes: base.scrollHeight,
        scroll_height_plegado: plegadas.scrollHeight,
        scroll_height_plegado_esperado: alturaEsperada(disp, o.tarjetas - 2),
        scroll_height_despues: devueltas.scrollHeight,
        superficies_en_dom_plegado: plegadas.superficies,
      });
      await cerrarTramo('plegado');

      informe.etapa = 'pantallas';
      const mitad = Math.floor(o.tarjetas / 2);
      const estados = [];
      const paso = async (nombre, accion, visiblesEsperadas) => {
        await evaluar(accion.fn, accion.arg);
        await cuadros(4);
        const l = await leer();
        compararFenwick(l, `pantallas:${nombre}`);
        estados.push({ estado: nombre, visibles: l.items.filter((it) => !it.oculto).length, visibles_esperadas: visiblesEsperadas, scroll_height: l.scrollHeight, scroll_height_esperado: alturaEsperada(disp, visiblesEsperadas), superficies_en_dom: l.superficies });
      };
      await paso('mitad_movida_a_p2_activa_p1', { fn: ([d, h]) => { for (let i = d; i < h; i++) window.__srfPagina.moverAPantalla(i, 'p2'); }, arg: [mitad, o.tarjetas] }, mitad);
      await paso('activa_p2', { fn: () => window.__srfPagina.activarPantalla('p2') }, o.tarjetas - mitad);
      await paso('activa_p1_otra_vez', { fn: () => window.__srfPagina.activarPantalla('p1') }, mitad);
      await paso('mitad_devuelta_a_p1', { fn: ([d, h]) => { for (let i = d; i < h; i++) window.__srfPagina.moverAPantalla(i, 'p1'); }, arg: [mitad, o.tarjetas] }, o.tarjetas);
      Object.assign(informe.tramos.pantallas, { movidas: o.tarjetas - mitad, estados });
      await cerrarTramo('pantallas');
    } else {
      informe.tramos.plegado.omitido = 'fewer than 4 cards';
      informe.tramos.pantallas.omitido = 'fewer than 4 cards';
    }

    // ---- the readings: the counters once nothing moves, the census, the trace ----
    informe.etapa = 'lectura';
    const quieto = await evaluar(([ms, plazo]) => window.__srfMedicion.esperarQuietud(ms, plazo), [600, 8000]);
    await pagina.evaluate(() => 0); // a round trip: every frame detachment the browser announced before it has been delivered
    const final = await evaluar(() => window.__srfMedicion.contadores());
    contandoMarcos = false;
    const despues = await censoDeSuperficies(pagina);
    const muestras = await evaluar(() => window.__srfMedicion.frames());
    await cdp.send('Tracing.end');
    await conPlazo(trazaCompleta, 20000, () => new ErrorMedicion('lectura', 1, 'the trace did not complete within 20 s'));

    // ---- assemble ----
    informe.etapa = 'acuerdo_entre_fuentes';
    const anotar = (campo, texto) => {
      informe.desacuerdos.push(texto);
      if (!informe.campos_en_desacuerdo.includes(campo)) informe.campos_en_desacuerdo.push(campo);
    };
    informe.peticiones = web.peticiones;

    // desmontajes: three independent counters of the same event, and the bookkeeping identity between them and the DOM
    informe.desmontajes_fuentes = { dom_mutation_observer: final.retirados, svelte_desmontados: final.desmontados, marcos_desprendidos_playwright: marcosDesprendidos };
    informe.montajes = { svelte_montados: final.montados, superficies_anadidas_al_dom: final.anadidos, vivas_al_final: final.vivas };
    const censo = compararCensos(antes, despues);
    const sinLeer = [...antes, ...despues].filter((f) => f.error !== undefined);
    informe.superficies = { al_inicio: antes.length, al_final: despues.length, ...censo, no_leidas: sinLeer.length };
    if (sinLeer.length > 0) anotar('desmontajes', `${sinLeer.length} surface frame(s) could not be read in the census: ${sinLeer.map((f) => f.error).join(' | ')}`);
    for (const texto of desacuerdosDeDesmontajes({ estable: quieto, dom: final.retirados, svelte: final.desmontados, marcos: marcosDesprendidos, vivasAlArmar: alArmar.vivas, montados: final.montados, vivasAlFinal: final.vivas, censo })) anotar('desmontajes', texto);
    informe.desmontajes = informe.campos_en_desacuerdo.includes('desmontajes') ? null : final.retirados;

    // tarjetas y tarjetas_por_vista
    const enPaginaUnica = new Set(vistasPagina).size === 1 ? vistasPagina[0] : null;
    const enCdpUnica = new Set(vistasCdp).size === 1 ? vistasCdp[0] : null;
    informe.tarjetas_por_vista_fuentes = { pagina: enPaginaUnica, cdp: enCdpUnica, por_parada_pagina: vistasPagina, por_parada_cdp: vistasCdp, declarado_por_el_componente: fin1.tarjetasPorVistaDeclaradas };
    if (enPaginaUnica === null || enCdpUnica === null || enPaginaUnica !== enCdpUnica) {
      anotar('tarjetas_por_vista', `cards per view: page ${JSON.stringify(vistasPagina)} vs CDP ${JSON.stringify(vistasCdp)}: not one number on which both sources agree at every stop`);
    }
    if (enPaginaUnica !== null && fin1.tarjetasPorVistaDeclaradas !== enPaginaUnica) {
      anotar('tarjetas_por_vista', `cards per view: the component declares ${fin1.tarjetasPorVistaDeclaradas} (data-tarjetas-por-vista) but the browser shows ${enPaginaUnica}`);
    }
    informe.tarjetas_por_vista = informe.campos_en_desacuerdo.includes('tarjetas_por_vista') ? null : enPaginaUnica;
    if (fin1.tarjetas.length !== nodosTarjeta.length) anotar('tarjetas', `cards: page ${fin1.tarjetas.length} vs CDP ${nodosTarjeta.length}`);
    informe.tarjetas = informe.campos_en_desacuerdo.includes('tarjetas') ? null : fin1.tarjetas.length;

    // p95_frame_ms: the page's frames against the compositor's view of the same frames
    const segmentos = segmentosDeTraza(eventosTraza);
    const trazaMs = duracionesDeEtapa(eventosTraza, ETAPA_TRAZA, segmentos);
    const estPagina = estadisticas(muestras.trabajo);
    const estTraza = { etapa: ETAPA_TRAZA, ...estadisticas(trazaMs) };
    const perdidos = muestras.cadencia.filter((x) => x > 1.5 * PERIODO_VSYNC_MS).length;
    informe.frames = {
      ...informe.frames,
      segmentos: { esperados: paradas - 1, en_la_traza: segmentos.length },
      pagina: estPagina,
      traza: estTraza,
      cadencia: { ...estadisticasSinPresupuesto(muestras.cadencia), periodo_vsync_ms: redondear(PERIODO_VSYNC_MS), perdidos, no_gateada: true },
      histograma_pagina_ms: histograma(muestras.trabajo),
      muestras_pagina_ms: muestras.trabajo.map((x) => redondear(x)),
      muestras_traza_ms: trazaMs.map((x) => redondear(x)),
    };
    if (segmentos.length !== paradas - 1) anotar('p95_frame_ms', `scroll segments: ${paradas - 1} run, ${segmentos.length} marked in the trace`);
    if (estPagina.n < MIN_FRAMES || estTraza.n < MIN_FRAMES) anotar('p95_frame_ms', `too few frames to take a p95: page ${estPagina.n}, trace ${estTraza.n} (at least ${MIN_FRAMES} needed)`);
    else {
      if (Math.abs(estPagina.n - estTraza.n) > Math.max(5, 0.05 * estPagina.n)) anotar('p95_frame_ms', `frames sampled: page ${estPagina.n} vs trace ${estTraza.n}`);
      const acuerdo = acuerdoDeP95(estPagina.p95_ms, estTraza.p95_ms);
      informe.frames.acuerdo_p95 = acuerdo;
      if (!acuerdo) anotar('p95_frame_ms', `frame p95: page ${estPagina.p95_ms} ms vs trace ${estTraza.p95_ms} ms differ by more than max(2 ms, half of the larger)`);
    }
    informe.p95_frame_ms = informe.campos_en_desacuerdo.includes('p95_frame_ms') ? null : estPagina.p95_ms;
    informe.cumple_objetivo_estiramiento = informe.p95_frame_ms === null ? null : informe.p95_frame_ms <= OBJETIVO_FRAME_MS;

    // layout: the component's height index against the browser's layout, the snap arithmetic, the legs
    informe.fenwick_contra_dom = { comparaciones: comparaciones.n, discrepancias: comparaciones.malas, ejemplos: comparaciones.ejemplos };
    if (comparaciones.malas > 0) anotar('fenwick', `Fenwick offsets differ from the browser layout in ${comparaciones.malas} of ${comparaciones.n} comparisons`);
    if (informe.oclusion.discrepancias_atributo > 0 || informe.oclusion.discrepancias_css > 0) anotar('oclusion', `occlusion: ${informe.oclusion.discrepancias_atributo} data-visible and ${informe.oclusion.discrepancias_css} computed content-visibility readings out of ${informe.oclusion.comprobaciones} contradict the band one viewport around the stack`);
    if (informe.oclusion.incoherentes_con_el_dom > 0) anotar('oclusion', `${informe.oclusion.incoherentes_con_el_dom} card(s) whose last pause callback differs from the DOM`);
    if (desalineadas > 0) anotar('paradas', `${desalineadas} of ${paradas} stops did not rest on the snap point the spec's arithmetic gives`);
    if (indiceMalo > 0) anotar('indice_actual', `the component's current index differed from the stop in ${indiceMalo} of ${paradas} stops`);
    if (informe.tramos.teclado.fuera_de_lugar > 0) anotar('teclado', `${informe.tramos.teclado.fuera_de_lugar} key press(es) did not page one snap step`);
    const p = informe.tramos.plegado;
    if (p.omitido === null && (p.scroll_height_plegado !== p.scroll_height_plegado_esperado || p.scroll_height_despues !== p.scroll_height_antes || p.ocultas_en_dom !== 2)) {
      anotar('plegado', `fold: ${p.ocultas_en_dom} hidden, height ${p.scroll_height_antes} -> ${p.scroll_height_plegado} (expected ${p.scroll_height_plegado_esperado}) -> ${p.scroll_height_despues}`);
    }
    for (const e of informe.tramos.pantallas.estados ?? []) {
      if (e.visibles !== e.visibles_esperadas || e.scroll_height !== e.scroll_height_esperado) anotar('pantallas', `screens ${e.estado}: ${e.visibles} visible (expected ${e.visibles_esperadas}), height ${e.scroll_height} (expected ${e.scroll_height_esperado})`);
    }
    if (informe.tramos.paginado.scroll_height !== informe.tramos.paginado.scroll_height_esperado) anotar('paradas', `stack height ${informe.tramos.paginado.scroll_height} vs ${informe.tramos.paginado.scroll_height_esperado} expected`);
    if (informe.tramos.paginado.tarjetas_vistas_al_menos_una_vez !== o.tarjetas) anotar('tarjetas_por_vista', `only ${informe.tramos.paginado.tarjetas_vistas_al_menos_una_vez} of ${o.tarjetas} cards were fully in view at some stop`);
    informe.acuerdo_entre_fuentes = informe.desacuerdos.length === 0;
    if (erroresDePagina.length > 0) throw new ErrorMedicion('lectura', 4, `the page threw ${erroresDePagina.length} uncaught error(s): ${erroresDePagina.join(' | ')}`);
    void alFallar;
  } finally {
    for (const senal of Object.keys(CODIGOS_DE_SENAL)) process.off(senal, alSenal);
    if (navegador !== null) await navegador.close().catch((e) => process.stderr.write(`medir-scroll: the browser did not close cleanly: ${primeraLinea(e.message)}\n`));
    if (web !== null) await new Promise((ok) => web.servidor.close(ok));
    if (tmpAnterior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpAnterior;
    if (raizTemporal !== null) await rm(raizTemporal, { recursive: true, force: true });
  }
}

/** Runs one measurement; returns the report and the exit code. Never throws. */
export async function ejecutar(argv) {
  const informe = informeVacio();
  informe.etapa = 'argumentos';
  let rc = 1;
  try {
    const o = leerArgumentos(argv);
    informe.modo = o.modo;
    informe.tarjetas_solicitadas = o.tarjetas;
    informe.envolver_if = o.envolverIf;
    informe.estres_ms = o.estresMs;
    informe.mutante.pedido = o.envolverIf;
    await medir(o, informe);
    if (informe.acuerdo_entre_fuentes) {
      informe.ok = true;
      informe.etapa = 'completa';
      rc = 0;
    } else {
      informe.ok = false;
      informe.etapa = 'acuerdo_entre_fuentes';
      informe.error = `two sources of one field disagree: ${informe.desacuerdos.join('; ')}`;
      rc = 3;
    }
  } catch (e) {
    informe.ok = false;
    informe.error = e instanceof ErrorMedicion ? e.message : `measurement failed: ${primeraLinea(e && e.message ? e.message : e)}`;
    if (e instanceof ErrorMedicion && e.etapa !== informe.etapa && e.etapa !== undefined) informe.etapa = e.etapa;
    rc = e instanceof ErrorMedicion ? e.rc : 1;
  }
  informe.rc = rc;
  return { informe, rc };
}

async function main() {
  const argv = process.argv.slice(2);
  const { informe, rc } = await ejecutar(argv);
  process.stdout.write(`${argv.includes('--json') ? JSON.stringify(informe) : JSON.stringify(informe, null, 2)}\n`);
  if (rc !== 0) process.stderr.write(`medir-scroll: ${informe.etapa}: ${informe.error}\n`);
  return rc;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
