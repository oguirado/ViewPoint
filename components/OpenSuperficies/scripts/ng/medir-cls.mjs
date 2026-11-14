#!/usr/bin/env node
// scripts/ng/medir-cls.mjs — U-18 instrument: geometry, CLS and WebGL context count of the cockpit shell.
//
//   node scripts/ng/medir-cls.mjs --modo 1/6|1/3 [--json] [--sin-arranque-sincrono]
//        [--escala 1.25] [--tema alto-contraste] [--latencia-js-ms 300] [--reposo-ms 500]
//        [--espera-listo-ms 15000] [--dist <dir>]
//
// It serves cockpit/frontend/dist from a loopback HTTP server, opens it in Playwright 1.58.0 Chromium headless
// (DEC-18) with a fresh context whose viewport and deviceScaleFactor are fixed BEFORE navigation, seeds a NON-DEFAULT
// persisted preference (font scale and theme in localStorage) and measures the cold start.
//
// --sin-arranque-sincrono is the contrast: the server delivers dist/index.html WITHOUT the synchronous <head> script
// (the block carrying data-arranque-sincrono). Nothing else changes: same page, same JS, same seeded preference,
// same JS latency. The theme and font scale are then applied by hydration, after a first paint made with the
// defaults, and the browser itself reports the layout shifts. No shift is synthesised anywhere in this file.
// The red of the contrast DEPENDS ON THE DISCLOSED INJECTED LATENCY (--latencia-js-ms, default 300, published as
// latencia_js_ms): every .js response is delayed so the static shell is painted before hydration in BOTH runs. With a
// latency of 0 the first paint and the hydration race each other and the contrast is not deterministic.
//
// PROVENANCE OF EVERY FIELD (published in the report's `procedencia`, A-59):
//   ventana             DECLARED    the tiling arithmetic; a headless page has no window frame
//   dom_solicitado      DECLARED    the viewport this harness asks the browser to emulate
//   dom                 READ-BACK   the emulated viewport read back (innerWidth x innerHeight). It is NOT an independent
//                                   measurement: dom_cdp and dom_captura are consequences of the same emulation setting
//   lienzo, desplazamiento          MEASURED layout the page produces at that viewport, each with an independent read
//                                   (in page vs CDP DOM.getBoxModel / Page.getLayoutMetrics / screenshot pixels)
//   cls                 MEASURED    in-page PerformanceObserver vs Chromium trace events (Tracing domain)
//   contextos_webgl     MEASURED    across every realm: in-realm source (interceptors in the main frame and every child
//                                   frame, live WebGL objects in every dedicated worker) vs the GPU-service trace, which
//                                   counts contexts outside any JS realm and so cannot share the in-realm blind spots
// The real Wails/WebKitGTK window is never opened here: its size is UNTESTED by this instrument (see no_probado).
//
// EVERY run, success or failure, writes ONE report of the SAME shape to stdout (A-53): ok, etapa (the stage reached or
// the stage that failed), error, and null in every field that was not measured or on which two sources disagree.
// A consequence to keep in mind when reading a failed report with jq: `null <= 1` is true in jq, so a consumer must
// keep the exit code (bash -o pipefail) and not read the value alone.
//
// Exit codes: 0 measured and every source agrees · 1 anything else (browser cannot start, protocol failure) ·
// 2 usage or instrument precondition · 3 two sources of one field disagree · 4 the page never reached its ready
// contract (or threw).

import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { extname, join, normalize, resolve, dirname, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { inflateSync } from 'node:zlib';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

// ---- host facts (spec 1.1/1.2, MEASURED) — an independent derivation of what src/lib/geometria.ts derives ----
const PANTALLA = { w: 3840, h: 2160 };
const COLUMNAS = 3;
const FILAS = 2;
const BARRA_GNOME_H = 32;
const MARGEN_WM_W = 9;
const CLAVE_PREFERENCIAS = 'srf.cockpit.prefs.v1';
const COLOR_LIENZO = [0x0e, 0x13, 0x1f]; // --surface-obsidian: the canvas frame colour, distinct from the void around it
const PLAYWRIGHT_PIN = '1.58.0';
const CATEGORIA_GPU = 'disabled-by-default-gpu.service';
const CATEGORIAS_TRAZA = ['loading', 'devtools.timeline', 'disabled-by-default-devtools.timeline', CATEGORIA_GPU];

/** Window (outer) and DOM (inner) viewport of a mode, from the tiling arithmetic alone. */
export function derivarViewport(modo) {
  const ventana = { w: PANTALLA.w / COLUMNAS, h: (PANTALLA.h - BARRA_GNOME_H) / FILAS };
  if (modo === '1/3') ventana.h *= 2;
  else if (modo !== '1/6') throw new RangeError(`unknown mode ${JSON.stringify(modo)}: use 1/6 or 1/3`);
  return { ventana, dom: { w: ventana.w - MARGEN_WM_W, h: ventana.h } };
}

/**
 * CLS of a list of layout shifts: the LARGEST session window (consecutive shifts less than 1 s apart, window
 * shorter than 5 s), not the sum. Entries with recent input are excluded. `t` is in milliseconds.
 */
export function clsDeEntradas(entradas) {
  const validas = entradas.filter((e) => !e.hadRecentInput).sort((a, b) => a.t - b.t);
  let mejor = 0;
  let ventana = 0;
  let inicio = 0;
  let previo = 0;
  let abierta = false;
  for (const e of validas) {
    if (abierta && e.t - previo < 1000 && e.t - inicio < 5000) {
      ventana += e.value;
    } else {
      ventana = e.value;
      inicio = e.t;
      abierta = true;
    }
    previo = e.t;
    mejor = Math.max(mejor, ventana);
  }
  return { cls: mejor, contadas: validas.length };
}

/**
 * WebGL contexts seen by the GPU service, read from the browser's trace: the GPU process names the asynchronous
 * events of each context `WebGL-0x<pointer>`, so the number of DISTINCT names is the number of contexts created
 * while tracing, whatever realm (main frame, child frame, any worker) asked for them. Two contexts that reuse one
 * address would be counted once: that undercount shows up as a disagreement with the in-realm source, never as a pass.
 */
export function contextosDeTraza(eventos) {
  const nombres = new Set();
  let eventosGpu = 0;
  for (const e of eventos) {
    if (typeof e.cat !== 'string' || !e.cat.split(',').includes(CATEGORIA_GPU)) continue;
    eventosGpu++;
    if (typeof e.name === 'string' && /^WebGL-0x[0-9a-f]+$/i.test(e.name)) nombres.add(e.name);
  }
  return { contextos: nombres.size, eventos_gpu: eventosGpu, nombres: [...nombres].sort() };
}

/**
 * The scope checks of the WebGL count, as text: what the in-realm source could not cover. A frame whose creation
 * interceptor is gone, a dedicated worker that could not be read, a worker count that Playwright and the CDP attachment
 * do not share, and a second page in the browser context are each a reason not to vouch for the count.
 */
export function desacuerdosDeAlcance({ marcos, trabajadores, trabajadoresCdp, trabajadoresPlaywright, paginas }) {
  const salida = [];
  const sinInterceptor = marcos.filter((m) => !m.instalado);
  if (sinInterceptor.length > 0) salida.push(`webgl scope: ${sinInterceptor.length} frame(s) without the creation interceptor: ${sinInterceptor.map((m) => m.url).join(', ')}`);
  for (const t of trabajadores.filter((w) => w.error)) salida.push(`webgl scope: a dedicated worker could not be read (${t.url.slice(0, 60)}): ${t.error}`);
  if (trabajadoresCdp !== trabajadoresPlaywright) salida.push(`webgl scope: dedicated workers attached over CDP ${trabajadoresCdp} vs listed by Playwright ${trabajadoresPlaywright}`);
  if (paginas !== 1) salida.push(`webgl scope: ${paginas} pages are open in the browser context, only the measured one is read in-realm`);
  return salida;
}

/** Bounding box of the pixels of one colour; null when there is none. `png` is decodificarPng's result. */
export function cajaDeColor(png, [r, g, b]) {
  const { width, height, canales, datos } = png;
  let x0 = width;
  let y0 = height;
  let x1 = -1;
  let y1 = -1;
  for (let y = 0; y < height; y++) {
    const fila = y * width * canales;
    for (let x = 0; x < width; x++) {
      const i = fila + x * canales;
      if (datos[i] === r && datos[i + 1] === g && datos[i + 2] === b) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
  }
  return x1 < 0 ? null : { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 };
}

/** Minimal PNG decoder: 8-bit truecolour, with or without alpha, non-interlaced (what Chromium screenshots are). */
export function decodificarPng(buf) {
  const firma = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (buf.length < 8 || !buf.subarray(0, 8).equals(firma)) throw new Error('png: bad signature');
  let pos = 8;
  let ihdr = null;
  const idat = [];
  while (pos + 8 <= buf.length) {
    const largo = buf.readUInt32BE(pos);
    const tipo = buf.toString('latin1', pos + 4, pos + 8);
    const cuerpo = buf.subarray(pos + 8, pos + 8 + largo);
    if (tipo === 'IHDR') {
      ihdr = { width: cuerpo.readUInt32BE(0), height: cuerpo.readUInt32BE(4), prof: cuerpo[8], tipoColor: cuerpo[9], entrelazado: cuerpo[12] };
    } else if (tipo === 'IDAT') {
      idat.push(cuerpo);
    } else if (tipo === 'IEND') {
      break;
    }
    pos += 12 + largo;
  }
  if (ihdr === null) throw new Error('png: no IHDR');
  if (ihdr.prof !== 8 || (ihdr.tipoColor !== 2 && ihdr.tipoColor !== 6) || ihdr.entrelazado !== 0) {
    throw new Error(`png: unsupported format depth=${ihdr.prof} colour=${ihdr.tipoColor} interlace=${ihdr.entrelazado}`);
  }
  const canales = ihdr.tipoColor === 6 ? 4 : 3;
  const zancada = ihdr.width * canales;
  const crudo = inflateSync(Buffer.concat(idat));
  if (crudo.length !== (zancada + 1) * ihdr.height) throw new Error('png: unexpected decoded length');
  const datos = Buffer.alloc(zancada * ihdr.height);
  for (let y = 0; y < ihdr.height; y++) {
    const filtro = crudo[y * (zancada + 1)];
    const src = y * (zancada + 1) + 1;
    const dst = y * zancada;
    for (let i = 0; i < zancada; i++) {
      const a = i >= canales ? datos[dst + i - canales] : 0;
      const b = y > 0 ? datos[dst - zancada + i] : 0;
      const c = i >= canales && y > 0 ? datos[dst - zancada + i - canales] : 0;
      let pred;
      switch (filtro) {
        case 0: pred = 0; break;
        case 1: pred = a; break;
        case 2: pred = b; break;
        case 3: pred = (a + b) >> 1; break;
        case 4: {
          const p = a + b - c;
          const pa = Math.abs(p - a);
          const pb = Math.abs(p - b);
          const pc = Math.abs(p - c);
          pred = pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
          break;
        }
        default: throw new Error(`png: bad filter ${filtro}`);
      }
      datos[dst + i] = (crudo[src + i] + pred) & 0xff;
    }
  }
  return { width: ihdr.width, height: ihdr.height, canales, datos };
}

// ---- the report --------------------------------------------------------------------------------------------
const PROCEDENCIA = {
  ventana: 'DECLARED: tiling arithmetic (3840/3 x (2160-32)/2); a headless page has no window frame, so this is not a browser reading',
  dom_solicitado: 'DECLARED: the viewport this harness asks the browser to emulate (ventana minus the 9 px window-manager border)',
  dom: 'READ-BACK of the emulated viewport (innerWidth x innerHeight after this harness set it through Playwright emulation); NOT an independent measurement: dom_cdp and dom_captura are consequences of the same emulation setting',
  lienzo: 'MEASURED layout the page produces at that viewport (getBoundingClientRect of [data-lienzo]); independent reads: CDP DOM.getBoxModel and the pixel bounding box of the canvas colour in a screenshot. The CSS constants behind it are compared with geometria.ts by the tests',
  desplazamiento: 'MEASURED layout the page produces at that viewport (documentElement.scrollWidth x scrollHeight); independent read: CDP Page.getLayoutMetrics cssContentSize',
  cls: 'MEASURED in page (PerformanceObserver layout-shift, largest session window); second source: Chromium trace events LayoutShift read through the Tracing domain, outside the page code',
  contextos_webgl: 'MEASURED across every realm: in-realm source = creation interceptors in the main frame and every child frame (context-wide init script) plus live WebGL objects in every dedicated worker (CDP Target auto-attach, Runtime.queryObjects); second source = GPU-service trace events WebGL-0x<pointer> (distinct contexts, independent of any JS realm)',
  ventana_wails_real: 'UNTESTED here: the real Wails/WebKitGTK window (1280x1064 outer, 1271 DOM) is never opened by this instrument (F-14, operator smoke check)',
};

const NO_PROBADO = [
  'the real Wails/WebKitGTK window size and its 9 px window-manager border (F-14: operator smoke check)',
  'WebKitGTK rendering and its WebGL context eviction: Chromium headless is a measurement proxy (DEC-18)',
  'shared workers and service workers are outside the in-realm scope: a WebGL context there is seen only by the GPU-service source and turns the run red through the disagreement (rc 3)',
];

const ALCANCE_WEBGL = {
  cubre: 'the measured page: main frame, every child frame (context-wide init script, read from every frame), every dedicated worker including nested ones (CDP Target auto-attach); the GPU-service source covers every context of the browser',
  no_cubre: 'shared workers and service workers in the in-realm source; contexts created and destroyed before the read in the in-realm source (the GPU-service source still counts them)',
};

/** The one shape of the report. Fields not measured stay null; nested objects keep their keys. */
export function informeVacio(o = {}) {
  return {
    ok: false,
    etapa: 'inicio',
    error: null,
    rc: null,
    esquema: 2,
    modo: o.modo ?? null,
    arranque_sincrono_solicitado: o.sinArranque === undefined ? null : !o.sinArranque,
    arranque_sincrono_presente_en_la_pagina: null,
    ventana: null,
    dom_solicitado: null,
    dom: null,
    lienzo: null,
    lienzo_origen: null,
    desplazamiento: null,
    cls: null,
    entradas_contadas: null,
    contextos_webgl: null,
    preferencia_sembrada: { tema: null, escala: null },
    estado_de_la_pagina: { arranque: null, arranqueError: null, tema: null, modo: null, escalaCss: null, tamanoRaizPx: null, listo: null, inicializando: null, gpu: null, srfd: null },
    desplazamientos: null,
    primera_fuente: {
      dom: null,
      lienzo: null,
      lienzo_origen: null,
      desplazamiento: null,
      cls: null,
      entradas_contadas: null,
      contextos_webgl: null,
    },
    segunda_fuente: {
      cls_traza_chromium: null,
      entradas_traza_chromium: null,
      eventos_de_traza_totales: null,
      dom_cdp: null,
      dom_captura: null,
      lienzo_cdp: null,
      lienzo_origen_cdp: null,
      lienzo_pixeles: null,
      lienzo_origen_pixeles: null,
      desplazamiento_cdp: null,
      contextos_webgl_gpu: null,
    },
    webgl: {
      alcance: {
        cubre: ALCANCE_WEBGL.cubre,
        no_cubre: ALCANCE_WEBGL.no_cubre,
        paginas: null,
        marcos: null,
        marcos_con_interceptor: null,
        trabajadores_dedicados_cdp: null,
        trabajadores_dedicados_playwright: null,
      },
      fuentes: {
        en_los_reinos: { marcos: null, trabajadores: null, total: null },
        servicio_gpu: { contextos: null, eventos_gpu: null, nombres: null },
      },
      marcos: null,
      trabajadores: null,
    },
    gl_lienzo: { w: null, h: null, css: { w: null, h: null }, origen: null },
    errores_de_pagina: [],
    procedencia: { ...PROCEDENCIA },
    no_probado: [...NO_PROBADO],
    navegador: { chromium: null, playwright: null },
    latencia_js_ms: o.latenciaJs ?? null,
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

// ---- command line ------------------------------------------------------------------------------------------
function leerArgumentos(argv) {
  const o = { modo: null, json: false, sinArranque: false, escala: 1.25, tema: 'alto-contraste', latenciaJs: 300, reposo: 500, esperaListo: 15000, dist: join(RAIZ, 'cockpit', 'frontend', 'dist') };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const valor = () => {
      if (i + 1 >= argv.length) throw new ErrorMedicion('argumentos', 2, `${a} needs a value`);
      return argv[++i];
    };
    if (a === '--modo') o.modo = valor();
    else if (a === '--json') o.json = true;
    else if (a === '--sin-arranque-sincrono') o.sinArranque = true;
    else if (a === '--escala') o.escala = Number(valor());
    else if (a === '--tema') o.tema = valor();
    else if (a === '--latencia-js-ms') o.latenciaJs = Number(valor());
    else if (a === '--reposo-ms') o.reposo = Number(valor());
    else if (a === '--espera-listo-ms') o.esperaListo = Number(valor());
    else if (a === '--dist') o.dist = resolve(valor());
    else throw new ErrorMedicion('argumentos', 2, `unknown argument ${a}`);
  }
  if (o.modo === null) throw new ErrorMedicion('argumentos', 2, '--modo 1/6|1/3 is required');
  for (const [k, v] of [['--escala', o.escala], ['--latencia-js-ms', o.latenciaJs], ['--reposo-ms', o.reposo], ['--espera-listo-ms', o.esperaListo]]) {
    if (!Number.isFinite(v) || v < 0) throw new ErrorMedicion('argumentos', 2, `${k} must be a non-negative number`);
  }
  try {
    derivarViewport(o.modo);
  } catch (e) {
    throw new ErrorMedicion('argumentos', 2, e.message, e);
  }
  return o;
}

// ---- static server -----------------------------------------------------------------------------------------
const TIPOS = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json' };
const BLOQUE_ARRANQUE = /[ \t]*<script data-arranque-sincrono>[\s\S]*?<\/script>[ \t]*\r?\n?/;

async function servir(dist, { sinArranque, latenciaJs }) {
  if (!existsSync(join(dist, 'index.html'))) throw new ErrorMedicion('precondicion', 2, `${dist}/index.html not found: run the frontend build first`);
  const html = await readFile(join(dist, 'index.html'), 'utf8');
  if (!BLOQUE_ARRANQUE.test(html)) throw new ErrorMedicion('precondicion', 2, 'the synchronous bootstrap block (<script data-arranque-sincrono>) is not in dist/index.html');
  const entregado = sinArranque ? html.replace(BLOQUE_ARRANQUE, '') : html;
  if (sinArranque && BLOQUE_ARRANQUE.test(entregado)) throw new ErrorMedicion('precondicion', 2, 'the bootstrap block could not be removed from the delivered page');
  const peticiones = [];
  const servidor = createServer(async (req, res) => {
    const ruta = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    peticiones.push(ruta);
    const cabeceras = (tipo) => ({ 'content-type': tipo, 'cache-control': 'no-store' });
    if (ruta === '/' || ruta === '/index.html') {
      res.writeHead(200, cabeceras(TIPOS['.html']));
      res.end(entregado);
      return;
    }
    const destino = normalize(join(dist, ruta));
    if (!destino.startsWith(dist + sep) || !existsSync(destino)) {
      res.writeHead(404, cabeceras('text/plain'));
      res.end('not found');
      return;
    }
    // A slow cold start (disk, first launch): the script arrives late in BOTH runs, so the shell is painted first.
    if (extname(destino) === '.js' && latenciaJs > 0) await new Promise((r) => setTimeout(r, latenciaJs));
    res.writeHead(200, cabeceras(TIPOS[extname(destino)] ?? 'application/octet-stream'));
    res.end(await readFile(destino));
  });
  await new Promise((ok, mal) => {
    servidor.once('error', mal);
    servidor.listen(0, '127.0.0.1', ok);
  });
  return { servidor, puerto: servidor.address().port, peticiones };
}

// ---- page-side instrumentation (installed by a context-wide init script: it runs in every frame, before any page script)
function instrumentarPagina({ clave, prefs }) {
  const med = { entradas: [], contextos: [], vistos: new WeakSet(), observador: null };
  window.__srfMedicion = med;
  const descr = (n) => {
    const el = n && n.nodeType === 1 ? n : n && n.parentElement;
    if (!el) return null;
    return { etiqueta: el.tagName.toLowerCase(), clase: el.className && el.className.baseVal === undefined ? String(el.className) : '', texto: (el.textContent || '').trim().slice(0, 40) };
  };
  const rect = (r) => (r ? { x: r.x, y: r.y, w: r.width, h: r.height } : null);
  // Seeding the preference and observing layout shifts concern the top document only.
  if (window.top === window) {
    try {
      window.localStorage.setItem(clave, JSON.stringify(prefs));
    } catch (e) {
      window.__srfSiembraError = String(e);
    }
    try {
      med.observador = new PerformanceObserver((lista) => {
        for (const e of lista.getEntries()) {
          med.entradas.push({ t: e.startTime, value: e.value, hadRecentInput: e.hadRecentInput, fuentes: (e.sources || []).map((s) => ({ nodo: descr(s.node), antes: rect(s.previousRect), despues: rect(s.currentRect) })) });
        }
      });
      med.observador.observe({ type: 'layout-shift', buffered: true });
    } catch (e) {
      med.errorObservador = String(e);
    }
  }
  // Every WebGL context creation that returns a NEW context, from any canvas (attached or not) and any library. A second
  // getContext on the same canvas returns the same object and is not a second context.
  const envolver = (proto, etiqueta) => {
    if (!proto || !proto.getContext) return;
    const original = proto.getContext;
    proto.getContext = function (tipo, ...resto) {
      const ctx = original.call(this, tipo, ...resto);
      if (ctx && /webgl/i.test(String(tipo)) && !med.vistos.has(ctx)) {
        med.vistos.add(ctx);
        med.contextos.push({ tipo: String(tipo), fuente: etiqueta, ctx });
      }
      return ctx;
    };
  };
  envolver(window.HTMLCanvasElement && HTMLCanvasElement.prototype, 'HTMLCanvasElement');
  envolver(window.OffscreenCanvas && OffscreenCanvas.prototype, 'OffscreenCanvas');
}

/** Runs inside every frame after the ready contract: the WebGL contexts this frame created. */
function leerContextosMarco() {
  const med = window.__srfMedicion;
  if (!med) return { instalado: false, contextos: [] };
  return { instalado: true, contextos: med.contextos.map((c) => ({ tipo: c.tipo, fuente: c.fuente, perdido: c.ctx.isContextLost() })) };
}

/** Runs inside the top frame after the ready contract: drains the observer and reads the geometry. */
function leerPagina() {
  const med = window.__srfMedicion;
  if (med.observador) {
    for (const e of med.observador.takeRecords()) {
      med.entradas.push({ t: e.startTime, value: e.value, hadRecentInput: e.hadRecentInput, fuentes: (e.sources || []).map((s) => ({ nodo: s.node && s.node.nodeType === 1 ? { etiqueta: s.node.tagName.toLowerCase(), clase: String(s.node.className), texto: (s.node.textContent || '').trim().slice(0, 40) } : null, antes: s.previousRect ? { x: s.previousRect.x, y: s.previousRect.y, w: s.previousRect.width, h: s.previousRect.height } : null, despues: s.currentRect ? { x: s.currentRect.x, y: s.currentRect.y, w: s.currentRect.width, h: s.currentRect.height } : null })) });
    }
    med.observador.disconnect();
  }
  const raiz = document.documentElement;
  const lienzo = document.querySelector('[data-lienzo]');
  const caja = lienzo ? lienzo.getBoundingClientRect() : null;
  const gl = document.querySelector('canvas[data-gl]');
  const cajaGl = gl ? gl.getBoundingClientRect() : null;
  return {
    entradas: med.entradas,
    errorObservador: med.errorObservador || null,
    siembraError: window.__srfSiembraError || null,
    dom: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio },
    desplazamiento: { w: raiz.scrollWidth, h: raiz.scrollHeight },
    lienzo: caja ? { x: caja.x, y: caja.y, w: caja.width, h: caja.height } : null,
    glLienzo: gl ? { w: gl.width, h: gl.height, css: { w: cajaGl.width, h: cajaGl.height }, origen: { x: cajaGl.x, y: cajaGl.y } } : null,
    datos: { arranque: raiz.dataset.arranque ?? null, arranqueError: raiz.dataset.arranqueError ?? null, tema: raiz.dataset.tema ?? null, modo: raiz.dataset.modo ?? null, escalaCss: raiz.style.getPropertyValue('--escala') || null, tamanoRaizPx: parseFloat(getComputedStyle(raiz).fontSize), listo: raiz.dataset.appReady ?? null, inicializando: raiz.classList.contains('initializing'), gpu: (document.querySelector('[data-gpu]') || {}).dataset?.gpu ?? null, srfd: (document.querySelector('[data-srfd]') || {}).dataset?.srfd ?? null },
  };
}

// ---- outside sources ---------------------------------------------------------------------------------------
async function cajaDomCdp(cdp) {
  const doc = await cdp.send('DOM.getDocument', { depth: 0 });
  const nodo = await cdp.send('DOM.querySelector', { nodeId: doc.root.nodeId, selector: '[data-lienzo]' });
  if (!nodo.nodeId) throw new Error('DOM.querySelector: [data-lienzo] not found');
  const { model } = await cdp.send('DOM.getBoxModel', { nodeId: nodo.nodeId });
  const q = model.border;
  return { x: Math.min(q[0], q[2], q[4], q[6]), y: Math.min(q[1], q[3], q[5], q[7]), w: model.width, h: model.height };
}

function entradasDeTraza(eventos) {
  return eventos
    .filter((e) => e.name === 'LayoutShift' && e.args && e.args.data && e.args.data.is_main_frame === true)
    .map((e) => ({ t: e.ts / 1000, value: e.args.data.score, hadRecentInput: e.args.data.had_recent_input === true }));
}

/**
 * Dedicated workers of the page, at any depth. A worker cannot be reached by an init script and Playwright resumes it
 * before any second session could instrument it, so its WebGL objects are enumerated AFTER the fact: the CDP Target
 * domain auto-attaches (non-flattened, so the traffic stays inside this session), and each worker's live
 * WebGLRenderingContext / WebGL2RenderingContext instances are counted with Runtime.queryObjects. A nested worker is
 * reached through its parent's session.
 */
async function vigilarTrabajadores(cdp) {
  let n = 0;
  const pendientes = new Map();
  const sesiones = new Map(); // sessionId -> { url, padre }
  function enviar(sessionId, method, params = {}) {
    return new Promise((ok, mal) => {
      const id = ++n;
      pendientes.set(id, { ok, mal });
      let mensaje = { id, method, params };
      const cadena = [];
      for (let s = sessionId; s !== null && s !== undefined; s = sesiones.get(s)?.padre ?? null) cadena.push(s);
      for (let i = 0; i < cadena.length - 1; i++) {
        mensaje = { id: ++n, method: 'Target.sendMessageToTarget', params: { sessionId: cadena[i], message: JSON.stringify(mensaje) } };
      }
      cdp.send('Target.sendMessageToTarget', { sessionId: cadena[cadena.length - 1], message: JSON.stringify(mensaje) }).catch((e) => {
        pendientes.delete(id);
        mal(e);
      });
    });
  }
  const alta = (sessionId, info, padre) => {
    if (info.type !== 'worker') return; // frames are read through Playwright; shared and service workers are not children of the page
    sesiones.set(sessionId, { url: info.url, padre });
    enviar(sessionId, 'Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: false }).catch(() => {});
  };
  const procesar = (sessionId, texto) => {
    const m = JSON.parse(texto);
    if (m.id !== undefined && pendientes.has(m.id)) {
      const p = pendientes.get(m.id);
      pendientes.delete(m.id);
      if (m.error) p.mal(new Error(m.error.message));
      else p.ok(m.result);
    } else if (m.method === 'Target.attachedToTarget') {
      alta(m.params.sessionId, m.params.targetInfo, sessionId);
    } else if (m.method === 'Target.detachedFromTarget') {
      sesiones.delete(m.params.sessionId);
    } else if (m.method === 'Target.receivedMessageFromTarget') {
      procesar(m.params.sessionId, m.params.message);
    }
  };
  cdp.on('Target.receivedMessageFromTarget', ({ sessionId, message }) => procesar(sessionId, message));
  cdp.on('Target.attachedToTarget', ({ sessionId, targetInfo }) => alta(sessionId, targetInfo, null));
  cdp.on('Target.detachedFromTarget', ({ sessionId }) => sesiones.delete(sessionId));
  await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: false });

  async function contar(sessionId) {
    let total = 0;
    const porTipo = {};
    for (const nombre of ['WebGL2RenderingContext', 'WebGLRenderingContext']) {
      const proto = await enviar(sessionId, 'Runtime.evaluate', { expression: `typeof ${nombre} === 'undefined' ? undefined : ${nombre}.prototype` });
      if (proto.exceptionDetails) throw new Error(`Runtime.evaluate ${nombre}.prototype failed`);
      if (!proto.result.objectId) {
        porTipo[nombre] = 0; // the interface is not exposed in this realm: no context of this kind can exist here
        continue;
      }
      const vivos = await enviar(sessionId, 'Runtime.queryObjects', { prototypeObjectId: proto.result.objectId });
      const largo = await enviar(sessionId, 'Runtime.callFunctionOn', { objectId: vivos.objects.objectId, functionDeclaration: 'function () { return this.length; }', returnByValue: true });
      if (largo.exceptionDetails) throw new Error(`Runtime.queryObjects ${nombre} failed`);
      porTipo[nombre] = largo.result.value;
      total += largo.result.value;
    }
    return { total, por_tipo: porTipo };
  }
  return {
    cuantos: () => sesiones.size,
    async leer() {
      const resultado = [];
      for (const [sessionId, info] of [...sesiones]) {
        try {
          resultado.push({ url: info.url, anidado: info.padre !== null, ...(await conPlazo(contar(sessionId), 10000, () => new Error('reading a worker timed out after 10000 ms'))) });
        } catch (e) {
          resultado.push({ url: info.url, anidado: info.padre !== null, total: null, por_tipo: null, error: e.message });
        }
      }
      return resultado;
    },
  };
}

const fmt = (w, h) => (w === null || w === undefined || h === null || h === undefined ? null : `${w}x${h}`);
const primeraLinea = (t) => String(t).split('\n')[0];

/** Races a promise against a deadline and always clears the timer, so a finished run never waits for it. */
function conPlazo(promesa, ms, crearError) {
  let temporizador;
  const limite = new Promise((_, mal) => {
    temporizador = setTimeout(() => mal(crearError()), ms);
  });
  return Promise.race([promesa, limite]).finally(() => clearTimeout(temporizador));
}

// ---- measurement -------------------------------------------------------------------------------------------
/** Measures into `informe` (created by main) so that a failure at any stage leaves what was read before it. */
async function medir(o, informe) {
  informe.etapa = 'precondicion';
  const requerir = createRequire(join(RAIZ, 'cockpit', 'frontend', 'package.json'));
  let pw;
  try {
    pw = requerir('playwright-core');
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `playwright-core is not installed under cockpit/frontend (run npm ci): ${primeraLinea(e.message)}`, e);
  }
  const version = requerir('playwright-core/package.json').version;
  informe.navegador.playwright = version;
  if (version !== PLAYWRIGHT_PIN) throw new ErrorMedicion('precondicion', 2, `playwright-core ${version} found, ${PLAYWRIGHT_PIN} is pinned: any other version would try to download a browser`);

  const { ventana, dom } = derivarViewport(o.modo);
  const prefs = { tema: o.tema, escala: o.escala };
  informe.ventana = fmt(ventana.w, ventana.h);
  informe.dom_solicitado = fmt(dom.w, dom.h);
  Object.assign(informe.preferencia_sembrada, prefs);
  const web = await servir(o.dist, o);
  let navegador = null;
  // Playwright creates the browser profile and artifacts folders under os.tmpdir() and does not remove them when the
  // launch itself fails (browser missing). A private temporary root that this run always removes leaves nothing behind.
  const raizTemporal = await mkdtemp(join(tmpdir(), 'srf-medir-'));
  const tmpAnterior = process.env.TMPDIR;
  process.env.TMPDIR = raizTemporal;
  try {
    informe.etapa = 'navegador';
    try {
      navegador = await pw.chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
    } catch (e) {
      throw new ErrorMedicion('navegador', 1, `the browser could not be launched: ${primeraLinea(e.message)}`, e);
    }
    informe.navegador.chromium = navegador.version();
    const contexto = await navegador.newContext({ viewport: { width: dom.w, height: dom.h }, screen: { width: PANTALLA.w, height: PANTALLA.h }, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: 'no-preference' });
    await contexto.addInitScript(instrumentarPagina, { clave: CLAVE_PREFERENCIAS, prefs });
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
    const cdp = await contexto.newCDPSession(pagina);

    const eventosTraza = [];
    cdp.on('Tracing.dataCollected', (p) => eventosTraza.push(...p.value));
    const trazaCompleta = new Promise((ok) => cdp.once('Tracing.tracingComplete', ok));
    await cdp.send('Tracing.start', { transferMode: 'ReportEvents', traceConfig: { recordMode: 'recordUntilFull', includedCategories: CATEGORIAS_TRAZA } });
    const trabajadores = await vigilarTrabajadores(cdp);

    informe.etapa = 'carga';
    try {
      await pagina.goto(`http://127.0.0.1:${web.puerto}/`, { waitUntil: 'load' });
    } catch (e) {
      throw new ErrorMedicion('carga', 1, `the page could not be loaded: ${primeraLinea(e.message)}`, e);
    }

    // Ready contract: the app's own data-app-ready, document.fonts.ready, two animation frames, then a quiet period.
    informe.etapa = 'listo';
    try {
      await Promise.race([pagina.waitForFunction(() => document.documentElement.dataset.appReady === 'true', null, { timeout: o.esperaListo }), falloDePagina]);
    } catch (e) {
      if (e instanceof ErrorMedicion) throw e;
      throw new ErrorMedicion('listo', 4, `the page never set data-app-ready="true": ${primeraLinea(e.message)}`, e);
    }
    await pagina.evaluate(async () => {
      await document.fonts.ready;
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    });
    if (o.reposo > 0) await pagina.waitForTimeout(o.reposo);

    informe.etapa = 'lectura';
    const local = await pagina.evaluate(leerPagina);
    if (local.lienzo === null) throw new ErrorMedicion('lectura', 4, 'the page has no [data-lienzo] element: its layout cannot be read');
    const marcos = [];
    for (const marco of pagina.frames()) {
      const lectura = await marco.evaluate(leerContextosMarco);
      marcos.push({ url: marco.url().slice(0, 80), principal: marco === pagina.mainFrame(), instalado: lectura.instalado, contextos: lectura.contextos.length, perdidos: lectura.contextos.filter((c) => c.perdido).length });
    }
    const lecturaTrabajadores = await trabajadores.leer();
    const metricas = await cdp.send('Page.getLayoutMetrics');
    const cajaCdp = await cajaDomCdp(cdp);
    const png = decodificarPng(await pagina.screenshot({ type: 'png', fullPage: false }));
    await cdp.send('Tracing.end');
    await conPlazo(trazaCompleta, 20000, () => new ErrorMedicion('lectura', 1, 'the trace did not complete within 20 s'));
    const paginas = contexto.pages().length;
    const trabajadoresPlaywright = pagina.workers().length;

    // ---- assemble ----
    informe.etapa = 'acuerdo_entre_fuentes';
    const sesion = clsDeEntradas(local.entradas);
    const traza = clsDeEntradas(entradasDeTraza(eventosTraza));
    const cajaPx = cajaDeColor(png, COLOR_LIENZO);
    const gpu = contextosDeTraza(eventosTraza);
    const enMarcos = marcos.reduce((s, m) => s + m.contextos, 0);
    const enTrabajadores = lecturaTrabajadores.reduce((s, t) => s + (t.total ?? 0), 0);
    const reinos = enMarcos + enTrabajadores;

    const primera = {
      dom: fmt(local.dom.w, local.dom.h),
      lienzo: fmt(local.lienzo.w, local.lienzo.h),
      lienzo_origen: fmt(local.lienzo.x, local.lienzo.y),
      desplazamiento: fmt(local.desplazamiento.w, local.desplazamiento.h),
      cls: sesion.cls,
      entradas_contadas: sesion.contadas,
      contextos_webgl: reinos,
    };
    const segunda = {
      cls_traza_chromium: traza.cls,
      entradas_traza_chromium: traza.contadas,
      eventos_de_traza_totales: eventosTraza.length,
      dom_cdp: fmt(metricas.cssLayoutViewport.clientWidth, metricas.cssLayoutViewport.clientHeight),
      dom_captura: fmt(png.width, png.height),
      lienzo_cdp: fmt(cajaCdp.w, cajaCdp.h),
      lienzo_origen_cdp: fmt(cajaCdp.x, cajaCdp.y),
      lienzo_pixeles: cajaPx ? fmt(cajaPx.w, cajaPx.h) : null,
      lienzo_origen_pixeles: cajaPx ? fmt(cajaPx.x, cajaPx.y) : null,
      desplazamiento_cdp: fmt(Math.round(metricas.cssContentSize.width), Math.round(metricas.cssContentSize.height)),
      contextos_webgl_gpu: gpu.contextos,
    };
    Object.assign(informe.primera_fuente, primera);
    Object.assign(informe.segunda_fuente, segunda);
    Object.assign(informe.webgl.alcance, {
      paginas,
      marcos: marcos.length,
      marcos_con_interceptor: marcos.filter((m) => m.instalado).length,
      trabajadores_dedicados_cdp: trabajadores.cuantos(),
      trabajadores_dedicados_playwright: trabajadoresPlaywright,
    });
    informe.webgl.fuentes.en_los_reinos = { marcos: enMarcos, trabajadores: enTrabajadores, total: reinos };
    informe.webgl.fuentes.servicio_gpu = { contextos: gpu.contextos, eventos_gpu: gpu.eventos_gpu, nombres: gpu.nombres };
    informe.webgl.marcos = marcos;
    informe.webgl.trabajadores = lecturaTrabajadores;
    informe.arranque_sincrono_presente_en_la_pagina = local.datos.arranque === 'sincrono';
    Object.assign(informe.estado_de_la_pagina, local.datos);
    informe.desplazamientos = local.entradas
      .filter((e) => !e.hadRecentInput)
      .map((e) => ({ t_ms: Math.round(e.t * 100) / 100, valor: e.value, movidos: e.fuentes.filter((f) => f.nodo !== null) }));
    if (local.glLienzo) informe.gl_lienzo = { w: local.glLienzo.w, h: local.glLienzo.h, css: local.glLienzo.css, origen: fmt(local.glLienzo.origen.x, local.glLienzo.origen.y) };
    informe.peticiones = web.peticiones;

    // ---- cross-source agreement: a field on which two sources disagree is published as null ----
    const anotar = (campo, texto) => {
      informe.desacuerdos.push(texto);
      if (!informe.campos_en_desacuerdo.includes(campo)) informe.campos_en_desacuerdo.push(campo);
    };
    const cerca = (a, b) => Math.abs(a - b) <= 1e-9;
    if (local.errorObservador) anotar('cls', `PerformanceObserver could not observe layout-shift: ${local.errorObservador}`);
    if (local.siembraError) anotar('preferencia', `the preference could not be seeded in localStorage: ${local.siembraError}`);
    if (eventosTraza.length === 0) anotar('cls', 'the trace returned no events at all');
    if (!cerca(primera.cls, segunda.cls_traza_chromium)) anotar('cls', `cls: observer ${primera.cls} vs trace ${segunda.cls_traza_chromium}`);
    if (primera.entradas_contadas !== segunda.entradas_traza_chromium) anotar('cls', `counted shifts: observer ${primera.entradas_contadas} vs trace ${segunda.entradas_traza_chromium}`);
    if (primera.dom !== segunda.dom_cdp) anotar('dom', `dom: page ${primera.dom} vs CDP ${segunda.dom_cdp}`);
    if (primera.dom !== segunda.dom_captura) anotar('dom', `dom: page ${primera.dom} vs screenshot ${segunda.dom_captura}`);
    if (primera.lienzo !== segunda.lienzo_cdp) anotar('lienzo', `lienzo: page ${primera.lienzo} vs CDP ${segunda.lienzo_cdp}`);
    if (primera.lienzo !== segunda.lienzo_pixeles) anotar('lienzo', `lienzo: page ${primera.lienzo} vs pixels ${segunda.lienzo_pixeles}`);
    if (primera.lienzo_origen !== segunda.lienzo_origen_cdp) anotar('lienzo', `lienzo origin: page ${primera.lienzo_origen} vs CDP ${segunda.lienzo_origen_cdp}`);
    if (primera.lienzo_origen !== segunda.lienzo_origen_pixeles) anotar('lienzo', `lienzo origin: page ${primera.lienzo_origen} vs pixels ${segunda.lienzo_origen_pixeles}`);
    if (primera.desplazamiento !== segunda.desplazamiento_cdp) anotar('desplazamiento', `scroll size: page ${primera.desplazamiento} vs CDP ${segunda.desplazamiento_cdp}`);
    if (reinos !== gpu.contextos) anotar('contextos_webgl', `webgl contexts: in-realm ${reinos} (frames ${enMarcos}, workers ${enTrabajadores}) vs GPU service ${gpu.contextos}`);
    for (const texto of desacuerdosDeAlcance({ marcos, trabajadores: lecturaTrabajadores, trabajadoresCdp: trabajadores.cuantos(), trabajadoresPlaywright, paginas })) anotar('contextos_webgl', texto);
    const arranquePresente = local.datos.arranque === 'sincrono';
    if (arranquePresente !== !o.sinArranque) anotar('arranque', `bootstrap: requested ${!o.sinArranque} but the page reports ${arranquePresente}`);
    if (erroresDePagina.length > 0) throw new ErrorMedicion('lectura', 4, `the page threw ${erroresDePagina.length} uncaught error(s): ${erroresDePagina.join(' | ')}`);

    // The vouched fields: what both sources agree on. A field with a disagreement stays null.
    const enDesacuerdo = (campo) => informe.campos_en_desacuerdo.includes(campo);
    informe.dom = enDesacuerdo('dom') ? null : primera.dom;
    informe.lienzo = enDesacuerdo('lienzo') ? null : primera.lienzo;
    informe.lienzo_origen = enDesacuerdo('lienzo') ? null : primera.lienzo_origen;
    informe.desplazamiento = enDesacuerdo('desplazamiento') ? null : primera.desplazamiento;
    informe.cls = enDesacuerdo('cls') ? null : primera.cls;
    informe.entradas_contadas = enDesacuerdo('cls') ? null : primera.entradas_contadas;
    informe.contextos_webgl = enDesacuerdo('contextos_webgl') ? null : primera.contextos_webgl;
    informe.acuerdo_entre_fuentes = informe.desacuerdos.length === 0;
  } finally {
    if (navegador !== null) await navegador.close().catch(() => {});
    await new Promise((ok) => web.servidor.close(ok));
    if (tmpAnterior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpAnterior;
    await rm(raizTemporal, { recursive: true, force: true });
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
    informe.arranque_sincrono_solicitado = !o.sinArranque;
    informe.latencia_js_ms = o.latenciaJs;
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
  if (rc !== 0) process.stderr.write(`medir-cls: ${informe.etapa}: ${informe.error}\n`);
  return rc;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
