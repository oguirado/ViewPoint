#!/usr/bin/env node
// scripts/ng/medir-contraste.mjs — U-20 instrument: AAA contrast of the cockpit tokens and the hot switch of a live capsule.
//
//   node scripts/ng/medir-contraste.mjs --tokens <css> [--json]
//   node scripts/ng/medir-contraste.mjs --conmutar-slot <n> [--json] [--sin-vuelta] [--xpra-bin bin/srf-xpra]
//        [--espera-listo-ms 15000] [--espera-conmutacion-ms 6000]
//
// --tokens: parses the custom properties of the file (the `:root` block and every `:root[data-tema='x']` override, with
//   var() chains), computes the WCAG 2.x contrast ratio of every declared foreground/background pair of POLITICA and
//   counts the failures in `aaa_fallos`. Formula: L = 0.2126 R + 0.7152 G + 0.0722 B with c <= 0.03928 ? c/12.92 :
//   ((c+0.055)/1.055)^2.4 on the sRGB channels, ratio = (L1 + 0.05)/(L2 + 0.05), L1 the lighter; compared unrounded.
//   Second source: headless Chromium loads THE SAME FILE byte for byte as a stylesheet, paints one swatch per token and
//   per theme, and the ratio is recomputed from the PIXELS of a screenshot (plus getComputedStyle); Chromium's CSS engine
//   and rasteriser share nothing with the parser and the maths above. Exit 0 when the measurement completed and both
//   sources agree, whether or not the tokens pass: the verdict is `aaa_fallos`, judged by the caller (jq -e).
//
// --conmutar-slot: drives the Read-Only <-> Operator switch of the live capsule of that slot THROUGH THE CARD: it builds
//   a harness page from the real cards (CapsuleCard.svelte and friends) into a temporary directory (never dist/), opens it
//   in Playwright 1.58.0 Chromium headless, clicks the card's switch control and measures. The card's backend is the real
//   `bin/srf-xpra intervenir|blindar` behind a loopback HTTP endpoint of this script (the seam of a Wails bridge that does
//   not exist yet). By default it switches there and back, so the capsule is left as it was found; `ms` is the LARGEST
//   wall time of the switches made.
//
// PROVENANCE OF EVERY FIELD (published in `procedencia`, A-59): see PROCEDENCIA below.
//
// EVERY run, success or failure, writes ONE report of the SAME shape to stdout (A-53): ok, etapa, error, rc, and null in
// every field that was not measured or on which two sources disagree. `null < 800` is true in jq: a consumer keeps the
// exit code (bash -o pipefail) and reads the booleans too.
//
// Exit codes: 0 measured and every source agrees · 1 anything else (browser cannot start, the switch failed) ·
// 2 usage or instrument precondition (no capsule, no browser, pinned Playwright missing) · 3 two sources of one field
// disagree · 4 the harness page never reached its ready contract (or threw).

import { createRequire } from 'node:module';
import { createServer, get as httpGet } from 'node:http';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, accessSync, constants, mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, extname, join, normalize, relative, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { inflateSync } from 'node:zlib';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = join(RAIZ, 'cockpit', 'frontend');
const PLAYWRIGHT_PIN = '1.58.0';

// ---- WCAG maths ----------------------------------------------------------------------------------------------
const FORMULA = 'WCAG 2.x relative luminance L = 0.2126 R + 0.7152 G + 0.0722 B (channel c/255 linearised: c <= 0.03928 ? c/12.92 : ((c+0.055)/1.055)^2.4); ratio = (L1+0.05)/(L2+0.05) with L1 the lighter; compared with the minimum unrounded';

function canalLineal(c8) {
  const c = c8 / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** Relative luminance of an [r, g, b] triple of 0..255 channels. */
export function luminancia([r, g, b]) {
  return 0.2126 * canalLineal(r) + 0.7152 * canalLineal(g) + 0.0722 * canalLineal(b);
}

/** WCAG contrast ratio of two colours, in either order. */
export function razonContraste(a, b) {
  const la = luminancia(a);
  const lb = luminancia(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** `alfa` of `frente` over `fondo`, per channel, as a browser composites an opaque backdrop. */
export function mezclar(frente, alfa, fondo) {
  return frente.map((c, i) => alfa * c + (1 - alfa) * fondo[i]);
}

// ---- the policy: which pairs the cards actually use ----------------------------------------------------------
// papel `texto`: normal-size text, WCAG AAA 1.4.6 = 7:1. papel `indicador`: border, dot or glow that carries meaning,
// WCAG 1.4.11 non-text contrast = 3:1. The cards keep crimson, violet and muted out of text (visor.ts
// TOKENS_DE_TEXTO; cards.test.ts requires every rendered label to reach 7:1) and --text-silver off the cockpit surface;
// the pairs left out are published under `informativos`, never dropped silently. Overlay text sits on the cockpit
// surface at 88 % opacity over an arbitrary remote picture: the worst backdrop for light text is white.
const MINIMO = { texto: 7, indicador: 3 };
const SUPERFICIES = ['--canvas-void', '--surface-obsidian', '--surface-cockpit'];
const SUPERFICIE_OVERLAY = { id: 'overlay:--surface-cockpit@88%/blanco', base: '--surface-cockpit', alfa: 0.88, sobre: [255, 255, 255] };
export const POLITICA = [
  { fg: '--text-polar', papel: 'texto', bg: [...SUPERFICIES, SUPERFICIE_OVERLAY.id] },
  { fg: '--text-silver', papel: 'texto', bg: ['--canvas-void', '--surface-obsidian'] },
  { fg: '--accent-amber', papel: 'texto', bg: SUPERFICIES },
  { fg: '--accent-emerald', papel: 'texto', bg: SUPERFICIES },
  { fg: '--accent-cyan', papel: 'texto', bg: SUPERFICIES },
  { fg: '--accent-crimson', papel: 'indicador', bg: SUPERFICIES },
  { fg: '--accent-violet', papel: 'indicador', bg: SUPERFICIES },
  { fg: '--text-muted', papel: 'indicador', bg: SUPERFICIES },
];
/** Pairs measured for the record and NOT counted in aaa_fallos. */
export const INFORMATIVOS = [
  { fg: '--text-silver', papel: 'texto', bg: ['--surface-cockpit'], nota: 'silver on the cockpit surface: the cards keep silver off it (their overlay text is polar), but the shell bar (app.css .barra) uses it' },
  { fg: '--text-muted', papel: 'texto', bg: SUPERFICIES, nota: 'muted as normal text (7:1): the specification allows it for large text only; the cards never use it as text' },
  { fg: '--text-muted', papel: 'texto_grande', bg: SUPERFICIES, minimo: 4.5, nota: 'muted as large text (4.5:1): the specification cites 4.6:1' },
  { fg: '--accent-crimson', papel: 'texto', bg: SUPERFICIES, nota: 'crimson as text: the cards paint only the border and the dot with it' },
  { fg: '--accent-violet', papel: 'texto', bg: SUPERFICIES, nota: 'violet as text: the cards paint only the border and the dot with it' },
];

// ---- token file parsing (independent of the browser) ---------------------------------------------------------
function bloquesCss(texto) {
  const limpio = texto.replace(/\/\*[\s\S]*?\*\//g, '');
  const bloques = [];
  let i = 0;
  while (i < limpio.length) {
    const abre = limpio.indexOf('{', i);
    if (abre < 0) break;
    let prof = 1;
    let j = abre + 1;
    while (j < limpio.length && prof > 0) {
      if (limpio[j] === '{') prof++;
      else if (limpio[j] === '}') prof--;
      j++;
    }
    if (prof !== 0) throw new Error('unbalanced braces in the token file');
    bloques.push({ prelude: limpio.slice(i, abre).split(/[;}]/).pop().trim(), cuerpo: limpio.slice(abre + 1, j - 1) });
    i = j;
  }
  return bloques;
}

/** The custom properties of a token file per theme: `oscuro` is `:root`, any other theme is `:root[data-tema='x']` over it. */
export function analizarTokens(texto) {
  const base = new Map();
  const temas = new Map();
  for (const { prelude, cuerpo } of bloquesCss(texto)) {
    const decl = [...cuerpo.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);?/gi)].map((m) => [m[1], m[2].trim()]);
    if (prelude === ':root') {
      for (const [k, v] of decl) base.set(k, v);
      continue;
    }
    const m = /^:root\[data-tema=(?:'([a-z0-9-]+)'|"([a-z0-9-]+)"|([a-z0-9-]+))\]$/.exec(prelude);
    if (m !== null) {
      const nombre = m[1] ?? m[2] ?? m[3];
      const mapa = temas.get(nombre) ?? new Map();
      for (const [k, v] of decl) mapa.set(k, v);
      temas.set(nombre, mapa);
    }
  }
  const resultado = { oscuro: new Map(base) };
  for (const [nombre, mapa] of temas) resultado[nombre] = new Map([...base, ...mapa]);
  return resultado;
}

function resolverVar(mapa, nombre, pila = []) {
  if (pila.includes(nombre)) throw new Error(`token ${nombre}: circular var() chain ${[...pila, nombre].join(' -> ')}`);
  const valor = mapa.get(nombre);
  if (valor === undefined) throw new Error(`token ${nombre} is not declared`);
  const ref = /^var\(\s*(--[a-z0-9-]+)\s*(?:,[^)]*)?\)$/i.exec(valor);
  return ref === null ? valor : resolverVar(mapa, ref[1], [...pila, nombre]);
}

/** A `#rgb` or `#rrggbb` token as 0..255 channels; anything else (alpha, functions, names) is refused, never guessed. */
export function colorDeToken(mapa, nombre) {
  const v = resolverVar(mapa, nombre);
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
  if (m === null) throw new Error(`token ${nombre}: "${v}" is not an opaque #rgb or #rrggbb colour`);
  const h = m[1].length === 3 ? [...m[1]].map((c) => c + c).join('') : m[1];
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}

const hex = (rgb) => `#${rgb.map((c) => Math.round(c).toString(16).padStart(2, '0')).join('')}`;
const redondear = (x, d = 4) => Math.round(x * 10 ** d) / 10 ** d;

function fondoDeId(mapa, id) {
  // a composite is what a screen shows: 8-bit channels, so it is quantised like a pixel
  if (id === SUPERFICIE_OVERLAY.id) return mezclar(colorDeToken(mapa, SUPERFICIE_OVERLAY.base), SUPERFICIE_OVERLAY.alfa, SUPERFICIE_OVERLAY.sobre).map(Math.round);
  return colorDeToken(mapa, id);
}

function pares(reglas, mapa, tema, enPolitica) {
  const salida = [];
  for (const r of reglas) {
    const minimo = r.minimo ?? MINIMO[r.papel];
    for (const bg of r.bg) {
      const fg = colorDeToken(mapa, r.fg);
      const fondo = fondoDeId(mapa, bg);
      const razon = razonContraste(fg, fondo);
      salida.push({ tema, fg: r.fg, bg, papel: r.papel, minimo, fg_hex: hex(fg), bg_hex: hex(fondo), razon: redondear(razon), razon_exacta: razon, cumple: razon >= minimo, en_politica: enPolitica, ...(r.nota ? { nota: r.nota } : {}) });
    }
  }
  return salida;
}

/** The first-source audit of a token file's text: every theme, every pair of POLITICA, plus the informative ones. */
export function auditarTokens(texto) {
  const temas = analizarTokens(texto);
  if (!temas.oscuro || temas.oscuro.size === 0) throw new Error('the token file declares no `:root` custom properties');
  const lista = [];
  for (const [tema, mapa] of Object.entries(temas)) {
    lista.push(...pares(POLITICA, mapa, tema, true));
    lista.push(...pares(INFORMATIVOS, mapa, tema, false));
  }
  return { temas: Object.keys(temas), pares: lista };
}

// ---- PNG decoding (the second source reads pixels) ---------------------------------------------------------
/** Minimal PNG decoder: 8-bit truecolour with or without alpha, non-interlaced (what Chromium screenshots are). */
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
    if (tipo === 'IHDR') ihdr = { width: cuerpo.readUInt32BE(0), height: cuerpo.readUInt32BE(4), prof: cuerpo[8], tipoColor: cuerpo[9], entrelazado: cuerpo[12] };
    else if (tipo === 'IDAT') idat.push(cuerpo);
    else if (tipo === 'IEND') break;
    pos += 12 + largo;
  }
  if (ihdr === null) throw new Error('png: no IHDR');
  if (ihdr.prof !== 8 || (ihdr.tipoColor !== 2 && ihdr.tipoColor !== 6) || ihdr.entrelazado !== 0) throw new Error(`png: unsupported format depth=${ihdr.prof} colour=${ihdr.tipoColor} interlace=${ihdr.entrelazado}`);
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

/** The RGB of one pixel of a decoded PNG. */
export function pixel(png, x, y) {
  const i = (y * png.width + x) * png.canales;
  return [png.datos[i], png.datos[i + 1], png.datos[i + 2]];
}

// ---- the report ----------------------------------------------------------------------------------------------
const PROCEDENCIA = {
  tokens: 'READ from the file named on the command line (path relative to the repository when inside it; sha256 published)',
  aaa_fallos: 'MEASURED: WCAG ratio computed here from the token colours (see `formula`) for every pair of `politica`, per theme; the count of pairs below their minimum',
  razon_navegador: 'MEASURED: second source, the ratio recomputed from the pixels of a Chromium screenshot of one swatch per token (the browser parses the same file itself); getComputedStyle is a third read',
  ms: 'MEASURED in the page: performance.now() at the click on the card\'s switch control (capture-phase listener installed by this script) to the instant a MutationObserver sees the card reach data-fase="asentado"; the largest of the switches made',
  ms_exterior: 'MEASURED outside the page: this process\'s clock from before Playwright dispatches the click to the instant its rAF poll sees the settled state (contains ms)',
  ms_servidor: 'MEASURED by the loopback endpoint of this script: request received to response sent around the srf-xpra process (contained in ms)',
  ms_backend_go: 'READ from srf-xpra\'s own JSON: the Go monotonic span from Docker exec creation to the read-back of the Xpra layer (contained in ms_servidor)',
  ms_componente: 'READ from the card\'s own clock (data-ms of the card, set by crearConmutacion): a third read of the same switch by different code',
  iframe_desmontado: 'MEASURED in the page: the iframe node captured before the switch is still the querySelector result, still connected, and a MutationObserver saw no removal of it or of an ancestor; second source: CDP Page.frameDetached events, the Playwright Frame object identity and the frame count',
  iframe_recargado: 'MEASURED in the page: no capture-phase load event on the iframe after the baseline and its src attribute unchanged; second source: CDP Page.frameNavigated events of the child frame, and performance.timeOrigin read inside the child realm (a reload would give a new one)',
  readonly: 'READ BACK from a separate process: `srf-xpra estado`, which asks the container which readonly flag its Xpra layer runs with, before and after the switch',
  puerto_xpra: 'READ from `srf-puertos par` (the port plan of unit U-01); second source: `docker port` of the container (the daemon\'s view of the published port)',
};

const NO_PROBADO = [
  'a live remote picture: the Xpra HTML5 client served by the capsule image cannot initialise (see errores_del_marco: its js/lib/jquery.js answers 404), so the frame never opens an Xpra WebSocket; what is measured is the identity and navigation history of the frame across a real recycle of the Xpra layer',
  'the Wails bridge of the switch: srfd has no switch verb, so the harness implements the card\'s ConmutadorCapsula over srf-xpra and an HTTP probe (the seam is real, the production binding is not part of this unit)',
  'the real Wails/WebKitGTK window: Chromium headless is a measurement proxy (DEC-18, F-14: operator smoke check)',
  'the telemetry and gate values shown in the harness card: they are placeholders, not measurements',
];

export function informeVacio() {
  return {
    ok: false,
    etapa: 'inicio',
    error: null,
    rc: null,
    esquema: 1,
    modo: null,
    // --tokens
    tokens: null,
    tokens_sha256: null,
    formula: FORMULA,
    politica: null,
    temas: null,
    pares: null,
    aaa_fallos: null,
    aaa_fallos_detalle: null,
    informativos: null,
    // --conmutar-slot
    slot: null,
    puerto_xpra: null,
    ms: null,
    iframe_recargado: null,
    iframe_desmontado: null,
    conmutaciones: null,
    errores_del_marco: null,
    errores_de_pagina: [],
    fuentes: { llamadas_al_backend: null, peticiones_http_del_arnes: null },
    acuerdo_entre_fuentes: null,
    desacuerdos: [],
    campos_en_desacuerdo: [],
    procedencia: { ...PROCEDENCIA },
    no_probado: [...NO_PROBADO],
    navegador: { chromium: null, playwright: null },
  };
}

class ErrorMedicion extends Error {
  constructor(etapa, rc, mensaje, causa) {
    super(mensaje, causa === undefined ? undefined : { cause: causa });
    this.etapa = etapa;
    this.rc = rc;
  }
}

const primeraLinea = (t) => String(t).split('\n')[0];

function leerArgumentos(argv) {
  const o = { tokens: null, conmutarSlot: null, json: false, sinVuelta: false, xpraBin: join(RAIZ, 'bin', 'srf-xpra'), esperaListo: 15000, esperaConmutacion: 6000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const valor = () => {
      if (i + 1 >= argv.length) throw new ErrorMedicion('argumentos', 2, `${a} needs a value`);
      return argv[++i];
    };
    if (a === '--tokens') o.tokens = valor();
    else if (a === '--conmutar-slot') o.conmutarSlot = valor();
    else if (a === '--json') o.json = true;
    else if (a === '--sin-vuelta') o.sinVuelta = true;
    else if (a === '--xpra-bin') o.xpraBin = resolve(valor());
    else if (a === '--espera-listo-ms') o.esperaListo = Number(valor());
    else if (a === '--espera-conmutacion-ms') o.esperaConmutacion = Number(valor());
    else throw new ErrorMedicion('argumentos', 2, `unknown argument ${a}`);
  }
  if ((o.tokens === null) === (o.conmutarSlot === null)) throw new ErrorMedicion('argumentos', 2, 'exactly one of --tokens <css> or --conmutar-slot <n> is required');
  if (o.conmutarSlot !== null) {
    const n = Number(o.conmutarSlot);
    if (!/^\d+$/.test(o.conmutarSlot) || !Number.isInteger(n) || n < 0 || n > 79) throw new ErrorMedicion('argumentos', 2, `--conmutar-slot ${o.conmutarSlot}: a slot is an integer 0..79`);
    o.conmutarSlot = n;
  }
  for (const [k, v] of [['--espera-listo-ms', o.esperaListo], ['--espera-conmutacion-ms', o.esperaConmutacion]]) {
    if (!Number.isFinite(v) || v <= 0) throw new ErrorMedicion('argumentos', 2, `${k} must be a positive number`);
  }
  return o;
}

function cargarPlaywright() {
  const requerir = createRequire(join(FRONTEND, 'package.json'));
  let pw;
  try {
    pw = requerir('playwright-core');
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `playwright-core is not installed under cockpit/frontend: ${primeraLinea(e.message)}`, e);
  }
  const version = requerir('playwright-core/package.json').version;
  if (version !== PLAYWRIGHT_PIN) throw new ErrorMedicion('precondicion', 2, `playwright-core ${version} found, ${PLAYWRIGHT_PIN} is pinned: any other version would try to download a browser`);
  return { pw, version };
}

function conPlazo(promesa, ms, crearError) {
  let temporizador;
  const limite = new Promise((_, mal) => {
    temporizador = setTimeout(() => mal(crearError()), ms);
  });
  return Promise.race([promesa, limite]).finally(() => clearTimeout(temporizador));
}

function ejecutarProceso(orden, args, opciones = {}) {
  return new Promise((ok) => {
    execFile(orden, args, { cwd: opciones.cwd ?? RAIZ, timeout: opciones.timeout ?? 60000, maxBuffer: 64 << 20, env: opciones.env ?? process.env }, (error, stdout, stderr) => {
      const codigo = error?.code;
      ok({ rc: error === null ? 0 : typeof codigo === 'number' ? codigo : null, stdout, stderr, error: error === null ? null : primeraLinea(error.message) });
    });
  });
}

// ---- --tokens: the browser second source -------------------------------------------------------------------
const TOLERANCIA_RAZON = 0.005;
const TOLERANCIA_RAZON_OVERLAY = 0.03; // Chromium composites opacity .88 with an 8-bit alpha (224/255)

async function servirTokens(textoOriginal) {
  const servidor = createServer((req, res) => {
    const ruta = new URL(req.url, 'http://127.0.0.1').pathname;
    if (ruta === '/tokens.css') {
      res.writeHead(200, { 'content-type': 'text/css; charset=utf-8', 'cache-control': 'no-store' });
      res.end(textoOriginal);
    } else if (ruta === '/') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' });
      res.end('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/tokens.css"><body></body>');
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((ok, mal) => {
    servidor.once('error', mal);
    servidor.listen(0, '127.0.0.1', ok);
  });
  return servidor;
}

const TAM_MUESTRA = 12;

/** Swatches of every token and of the overlay composite, per theme, read back as pixels and as computed colours. */
async function leerNavegadorTokens(pw, informe, texto, temas) {
  const servidor = await servirTokens(texto);
  const raizTemporal = mkdtempSync(join(tmpdir(), 'srf-medir-contraste-'));
  const tmpAnterior = process.env.TMPDIR;
  process.env.TMPDIR = raizTemporal;
  let navegador = null;
  try {
    try {
      navegador = await pw.chromium.launch({ headless: true });
    } catch (e) {
      throw new ErrorMedicion('navegador', 1, `the browser could not be launched: ${primeraLinea(e.message)}`, e);
    }
    informe.navegador.chromium = navegador.version();
    const contexto = await navegador.newContext({ viewport: { width: 640, height: 240 }, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: 'reduce' });
    const pagina = await contexto.newPage();
    await pagina.goto(`http://127.0.0.1:${servidor.address().port}/`, { waitUntil: 'load' });
    const nombres = [...new Set(POLITICA.flatMap((r) => [r.fg, ...r.bg]).filter((b) => !b.startsWith('overlay:')))];
    const lectura = {};
    for (const tema of temas) {
      const dibujo = await pagina.evaluate(
        ({ tema, nombres, tam, overlay }) => {
          document.documentElement.dataset.tema = tema;
          document.documentElement.style.cssText = 'margin:0;background:#fff';
          document.body.style.cssText = 'margin:0;background:#fff;position:relative';
          document.body.replaceChildren();
          const puestos = [];
          nombres.forEach((n, i) => {
            const d = document.createElement('div');
            d.style.cssText = `position:absolute;left:${i * (tam + 4)}px;top:0;width:${tam}px;height:${tam}px;background:var(${n})`;
            document.body.appendChild(d);
            puestos.push({ id: n, x: i * (tam + 4) + tam / 2, y: tam / 2, computado: getComputedStyle(d).backgroundColor });
          });
          // The overlay composite: the cockpit surface at 88 % opacity over a white backdrop.
          const fondo = document.createElement('div');
          fondo.style.cssText = `position:absolute;left:0;top:${tam + 8}px;width:${tam + 8}px;height:${tam + 8}px;background:#fff`;
          const capa = document.createElement('div');
          capa.style.cssText = `position:absolute;inset:0;opacity:${overlay.alfa};background:var(${overlay.base})`;
          fondo.appendChild(capa);
          document.body.appendChild(fondo);
          puestos.push({ id: overlay.id, x: (tam + 8) / 2, y: tam + 8 + (tam + 8) / 2, computado: getComputedStyle(capa).backgroundColor });
          return puestos;
        },
        { tema, nombres, tam: TAM_MUESTRA, overlay: SUPERFICIE_OVERLAY },
      );
      const png = decodificarPng(await pagina.screenshot({ type: 'png' }));
      lectura[tema] = new Map(dibujo.map((p) => [p.id, { pixel: pixel(png, Math.round(p.x), Math.round(p.y)), computado: p.computado }]));
    }
    return lectura;
  } finally {
    if (navegador !== null) await navegador.close().catch(() => {});
    await new Promise((ok) => servidor.close(ok));
    if (tmpAnterior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpAnterior;
    rmSync(raizTemporal, { recursive: true, force: true });
  }
}

function rgbDeComputado(texto) {
  const m = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(texto);
  return m === null ? null : [Number(m[1]), Number(m[2]), Number(m[3])];
}

async function medirTokens(o, informe) {
  informe.etapa = 'precondicion';
  const ruta = resolve(o.tokens);
  if (!existsSync(ruta)) throw new ErrorMedicion('precondicion', 2, `${o.tokens}: no such token file`);
  let texto;
  try {
    texto = readFileSync(ruta, 'utf8');
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `${o.tokens}: cannot be read: ${primeraLinea(e.message)}`, e);
  }
  informe.tokens = ruta.startsWith(RAIZ + sep) ? relative(RAIZ, ruta) : ruta;
  informe.tokens_sha256 = createHash('sha256').update(texto).digest('hex');
  informe.politica = POLITICA.map((r) => ({ fg: r.fg, papel: r.papel, minimo: MINIMO[r.papel], bg: r.bg }));

  informe.etapa = 'tokens';
  let auditoria;
  try {
    auditoria = auditarTokens(texto);
  } catch (e) {
    throw new ErrorMedicion('tokens', 1, `the token file cannot be audited: ${primeraLinea(e.message)}`, e);
  }
  informe.temas = auditoria.temas;

  informe.etapa = 'navegador';
  const { pw, version } = cargarPlaywright();
  informe.navegador.playwright = version;
  const lectura = await leerNavegadorTokens(pw, informe, texto, auditoria.temas);

  informe.etapa = 'acuerdo_entre_fuentes';
  const anotar = (campo, t) => {
    informe.desacuerdos.push(t);
    if (!informe.campos_en_desacuerdo.includes(campo)) informe.campos_en_desacuerdo.push(campo);
  };
  const lista = auditoria.pares.map((p) => {
    const enBrowser = lectura[p.tema];
    const fg = enBrowser.get(p.fg);
    const bg = enBrowser.get(p.bg.startsWith('overlay:') ? SUPERFICIE_OVERLAY.id : p.bg);
    let razonNavegador = null;
    if (fg !== undefined && bg !== undefined) {
      razonNavegador = razonContraste(fg.pixel, bg.pixel);
      const tol = p.bg.startsWith('overlay:') ? TOLERANCIA_RAZON_OVERLAY : TOLERANCIA_RAZON;
      if (Math.abs(razonNavegador - p.razon_exacta) > tol) anotar('aaa_fallos', `${p.tema} ${p.fg} on ${p.bg}: tokens ${redondear(p.razon_exacta)} vs Chromium pixels ${redondear(razonNavegador)}`);
      // the computed colour is a third read of the foreground and of the plain surfaces
      const cf = rgbDeComputado(fg.computado);
      if (cf === null || cf.some((c, i) => c !== fg.pixel[i])) anotar('aaa_fallos', `${p.tema} ${p.fg}: getComputedStyle ${fg.computado} vs pixel ${hex(fg.pixel)}`);
    } else {
      anotar('aaa_fallos', `${p.tema} ${p.fg} on ${p.bg}: the browser painted no swatch`);
    }
    const { razon_exacta: _, ...publico } = p;
    return { ...publico, razon_navegador: razonNavegador === null ? null : redondear(razonNavegador) };
  });
  informe.pares = lista;
  informe.informativos = lista.filter((p) => !p.en_politica);
  const enPolitica = lista.filter((p) => p.en_politica);
  const fallos = enPolitica.filter((p) => !p.cumple);
  informe.aaa_fallos_detalle = fallos.map((p) => ({ tema: p.tema, fg: p.fg, bg: p.bg, papel: p.papel, razon: p.razon, minimo: p.minimo, fg_hex: p.fg_hex, bg_hex: p.bg_hex }));
  informe.aaa_fallos = informe.campos_en_desacuerdo.includes('aaa_fallos') ? null : fallos.length;
  informe.acuerdo_entre_fuentes = informe.desacuerdos.length === 0;
  if (!informe.acuerdo_entre_fuentes) throw new ErrorMedicion('acuerdo_entre_fuentes', 3, `two sources of one field disagree: ${informe.desacuerdos.join('; ')}`);
}

// ---- --conmutar-slot: harness, server and the switch ---------------------------------------------------------
const TIPOS = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json', '.map': 'application/json' };

const HARNESS_SVELTE = `<script lang="ts">
  // Measurement harness of U-20: mounts the REAL cards over the token stylesheet. It owns no behaviour of its own.
  import { flushSync } from 'svelte';
  import CapsuleCard from '@cards/CapsuleCard.svelte';
  import { geometriaDeVentana } from '@lib/geometria';

  let { cfg }: { cfg: any } = $props();
  let geo = $state(geometriaDeVentana(window.innerWidth, window.innerHeight));
  let capsulas = $state<any[]>(cfg.capsulas);
  let activadas = $state<string[]>([]);

  async function llamar(ruta: string, cuerpo: unknown): Promise<any> {
    const r = await fetch(ruta, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(cuerpo) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);
    return j;
  }
  const conmutador = cfg.sinConmutador
    ? null
    : {
        conmutar: async (slot: number, destino: string) => (await llamar('/api/conmutar', { slot, destino })).resultado,
        sondear: async (puerto: number) => (await llamar('/api/sondear', { puerto })).status,
      };

  export function poner(id: string, campos: Record<string, unknown>): void {
    const c = capsulas.find((x) => x.id === id);
    if (c !== undefined) Object.assign(c, campos);
    flushSync();
  }
  export function ponerTodas(campos: Record<string, unknown>): void {
    for (const c of capsulas) Object.assign(c, campos);
    flushSync();
  }
  export function geometria(w: number, h: number): void {
    geo = geometriaDeVentana(w, h);
    flushSync();
  }
  export function activadasIds(): string[] {
    return [...activadas];
  }
</script>

<div id="pila" data-pila>
  {#each capsulas as c (c.id)}
    <CapsuleCard
      capsula={c}
      geometria={geo}
      {conmutador}
      fijado={cfg.fijado}
      autoOcultarMs={cfg.autoOcultarMs}
      onactivar={(id: string) => activadas.push(id)}
      onpop={cfg.conBotones ? () => {} : undefined}
      onfold={cfg.conBotones ? () => {} : undefined}
      onclose={cfg.conBotones ? () => {} : undefined}
    >
      {#if c.archetype === 'terminal'}<span data-anfitrion-contenido>terminal host</span>{/if}
    </CapsuleCard>
  {/each}
</div>
`;

const HARNESS_MAIN = `import { mount } from 'svelte';
import Harness from './Harness.svelte';

const cfg = JSON.parse(document.getElementById('cfg')!.textContent!);
(window as any).__srfHarness = mount(Harness, { target: document.getElementById('raiz')!, props: { cfg } });
requestAnimationFrame(() => requestAnimationFrame(() => { document.documentElement.dataset.appReady = 'true'; }));
`;

const HARNESS_HTML = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>U-20 card harness</title>
    <link rel="stylesheet" href="/tokens.css" />
    <style>
      html, body { margin: 0; background: var(--canvas-void); color: var(--text-silver); }
      #raiz { position: absolute; left: 3px; top: 40px; width: 1264px; }
      #pila { display: flex; flex-direction: column; }
    </style>
    <!--CFG-->
  </head>
  <body>
    <div id="raiz" data-lienzo></div>
    <script type="module" src="./main.ts"></script>
  </body>
</html>
`;

/**
 * Builds the card harness into a private directory under \`padre\`: the real cards from \`raiz\`, a stylesheet-only page,
 * never the app's dist/. Returns the directory of the build. Throws with vite's own output when the build fails.
 */
export async function construirHarness({ raiz = RAIZ, padre }) {
  const dir = join(padre, 'harness');
  const frontend = join(raiz, 'cockpit', 'frontend');
  mkdirSync(dir, { recursive: true });
  symlinkSync(join(frontend, 'node_modules'), join(dir, 'node_modules'), 'dir');
  writeFileSync(join(dir, 'Harness.svelte'), HARNESS_SVELTE);
  writeFileSync(join(dir, 'main.ts'), HARNESS_MAIN);
  writeFileSync(join(dir, 'index.html'), HARNESS_HTML);
  writeFileSync(
    join(dir, 'vite.config.mjs'),
    `import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
export default {
  root: ${JSON.stringify(dir)},
  base: './',
  logLevel: 'warn',
  clearScreen: false,
  plugins: [svelte({ preprocess: vitePreprocess() })],
  resolve: { alias: { '@cards': ${JSON.stringify(join(frontend, 'src', 'lib', 'cards'))}, '@lib': ${JSON.stringify(join(frontend, 'src', 'lib'))} } },
  build: { outDir: ${JSON.stringify(join(dir, 'dist'))}, emptyOutDir: true, target: 'es2022', minify: false, sourcemap: false, reportCompressedSize: false },
};
`,
  );
  const r = await ejecutarProceso(process.execPath, [join(frontend, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--config', join(dir, 'vite.config.mjs')], { cwd: dir, timeout: 170000 });
  if (r.rc !== 0) throw new Error(`vite build of the card harness failed (rc ${r.rc}):\n${r.stdout}\n${r.stderr}`);
  return join(dir, 'dist');
}

/** The `:root` blocks of the app stylesheet (tokens and their themes) and nothing else: no global rule of the shell. */
export function tokensDeApp(textoCss) {
  return bloquesCss(textoCss)
    .filter((b) => b.prelude === ':root' || /^:root\[data-tema=/.test(b.prelude))
    .map((b) => `${b.prelude} {${b.cuerpo}}`)
    .join('\n');
}

const JSON_ERR = (res, codigo, ok, cuerpo) => {
  res.writeHead(codigo, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(JSON.stringify({ ok, ...cuerpo }));
};

/** The switch backend of a real capsule: `srf-xpra intervenir|blindar` and an HTTP GET of the Xpra port. */
export function backendReal({ xpraBin, cwd = RAIZ }) {
  return {
    async conmutar(slot, destino) {
      const verbo = destino === 'operador' ? 'intervenir' : 'blindar';
      const r = await ejecutarProceso(xpraBin, [verbo, '--slot', String(slot), '--json'], { cwd, timeout: 20000 });
      if (r.rc !== 0) throw new Error(`srf-xpra ${verbo} rc ${r.rc}: ${primeraLinea(r.stderr || r.stdout || r.error || 'no output')}`);
      let j;
      try {
        j = JSON.parse(r.stdout);
      } catch (e) {
        throw new Error(`srf-xpra ${verbo}: unreadable JSON: ${primeraLinea(e.message)}`);
      }
      if (typeof j.readonly !== 'boolean' || typeof j.ms !== 'number') throw new Error(`srf-xpra ${verbo}: answer without readonly/ms: ${r.stdout.slice(0, 120)}`);
      return { readonly: j.readonly, ms: j.ms, estado: String(j.estado) };
    },
    sondear: (puerto) => sondearHttp(puerto),
  };
}

export function sondearHttp(puerto, ruta = '/') {
  return new Promise((ok, mal) => {
    const req = httpGet({ host: '127.0.0.1', port: puerto, path: ruta, agent: false, timeout: 1500 }, (res) => {
      res.resume();
      res.on('end', () => ok(res.statusCode));
      res.on('error', mal);
    });
    req.on('timeout', () => req.destroy(new Error(`GET 127.0.0.1:${puerto}${ruta} timed out`)));
    req.on('error', mal);
  });
}

/**
 * Serves the harness: the built page (with \`cfg\` injected), the token stylesheet and the two endpoints the card's backend
 * uses. Every backend call is recorded here, on this side of the interface, with this process's clock.
 */
export async function servirHarness({ dist, cfg, tokensCss, backend }) {
  const html = readFileSync(join(dist, 'index.html'), 'utf8');
  const entregada = html.replace('<!--CFG-->', '').replace('</head>', `<script type="application/json" id="cfg">${JSON.stringify(cfg).replace(/</g, '\\u003c')}</script></head>`);
  const llamadas = [];
  const peticiones = [];
  const servidor = createServer(async (req, res) => {
    const ruta = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
    peticiones.push(`${req.method} ${ruta}`);
    if (req.method === 'POST' && (ruta === '/api/conmutar' || ruta === '/api/sondear')) {
      const t0 = performance.now();
      let cuerpo = '';
      for await (const trozo of req) cuerpo += trozo;
      try {
        const p = JSON.parse(cuerpo);
        if (ruta === '/api/conmutar') {
          const registro = { tipo: 'conmutar', slot: p.slot, destino: p.destino, t_recibido: t0, t_respondido: null, ok: false, resultado: null, error: null };
          llamadas.push(registro);
          try {
            registro.resultado = await backend.conmutar(p.slot, p.destino);
            registro.ok = true;
          } catch (e) {
            registro.error = e instanceof Error ? e.message : String(e);
          }
          registro.t_respondido = performance.now();
          JSON_ERR(res, 200, registro.ok, registro.ok ? { resultado: registro.resultado } : { error: registro.error });
        } else {
          const estado = await backend.sondear(p.puerto);
          JSON_ERR(res, 200, true, { status: estado });
        }
      } catch (e) {
        JSON_ERR(res, 200, false, { error: e instanceof Error ? e.message : String(e) });
      }
      return;
    }
    if (ruta === '/' || ruta === '/index.html') {
      res.writeHead(200, { 'content-type': TIPOS['.html'], 'cache-control': 'no-store' });
      res.end(entregada);
      return;
    }
    if (ruta === '/tokens.css') {
      res.writeHead(200, { 'content-type': TIPOS['.css'], 'cache-control': 'no-store' });
      res.end(tokensCss);
      return;
    }
    const destino = normalize(join(dist, ruta));
    if (!destino.startsWith(dist + sep) || !existsSync(destino)) {
      res.writeHead(404, { 'content-type': 'text/plain' });
      res.end('not found');
      return;
    }
    res.writeHead(200, { 'content-type': TIPOS[extname(destino)] ?? 'application/octet-stream', 'cache-control': 'no-store' });
    res.end(await readFile(destino));
  });
  await new Promise((ok, mal) => {
    servidor.once('error', mal);
    servidor.listen(0, '127.0.0.1', ok);
  });
  return { servidor, puerto: servidor.address().port, llamadas, peticiones, cerrar: () => new Promise((ok) => servidor.close(ok)) };
}

// page-side instrumentation: installed BEFORE any page script, in every frame, by a context-wide init script
function instrumentarPagina() {
  if (window.top !== window) return;
  const med = { clics: [], fases: [], cargas: [], removidos: [], base: null };
  window.__srfMed = med;
  document.addEventListener('click', () => med.clics.push(performance.now()), true);
  // A load event of an element never reaches the window (the document is the end of its path), so the capture listener is the
  // document's. It counts the load of ANY card frame after the baseline: a re-mounted frame is a new element and a new load.
  document.addEventListener('load', (e) => med.cargas.push({ t: performance.now(), esNuestro: e.target instanceof HTMLIFrameElement && e.target.hasAttribute('data-xpra') }), true);
  const observador = new MutationObserver((registros) => {
    const t = performance.now();
    for (const r of registros) {
      if (r.type === 'attributes' && r.target.matches && r.target.matches('[data-tarjeta]') && r.attributeName === 'data-fase') med.fases.push({ t, fase: r.target.dataset.fase, modo: r.target.dataset.modo });
      if (r.type === 'childList' && med.base !== null) {
        for (const n of r.removedNodes) if (n === med.base.nodo || (n.contains && n.contains(med.base.nodo))) med.removidos.push({ t });
      }
    }
  });
  const arrancar = () => observador.observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-fase'] });
  if (document.documentElement) arrancar();
  else document.addEventListener('DOMContentLoaded', arrancar, { once: true });
}

function leerLineaBase() {
  const nodo = document.querySelector('iframe[data-xpra]');
  window.__srfMed.base = { nodo, src: nodo ? nodo.getAttribute('src') : null };
  window.__srfMed.cargas.length = 0;
  window.__srfMed.removidos.length = 0;
  window.__srfMed.fases.length = 0;
  window.__srfMed.clics.length = 0;
  return { hay: nodo !== null, src: nodo ? nodo.getAttribute('src') : null };
}

function leerDespues() {
  const med = window.__srfMed;
  const nodo = document.querySelector('iframe[data-xpra]');
  const tarjeta = document.querySelector('[data-tarjeta]');
  const insigniaModo = tarjeta ? tarjeta.querySelector('[data-insignia][data-clase="modo"] .texto') : null;
  const aviso = tarjeta ? tarjeta.querySelector('[data-aviso]') : null;
  const asentada = med.fases.find((f) => f.fase === 'asentado');
  const conmutando = med.fases.find((f) => f.fase === 'conmutando');
  return {
    mismoNodo: nodo !== null && nodo === med.base.nodo,
    conectado: med.base.nodo ? med.base.nodo.isConnected : false,
    srcIgual: nodo !== null && nodo.getAttribute('src') === med.base.src,
    cargasDelMarco: med.cargas.filter((c) => c.esNuestro).length,
    removidos: med.removidos.length,
    fase: tarjeta ? tarjeta.dataset.fase : null,
    modoTarjeta: tarjeta ? tarjeta.dataset.modo : null,
    insigniaModo: insigniaModo ? insigniaModo.textContent : null,
    aviso: aviso ? aviso.textContent : null,
    msComponente: tarjeta && tarjeta.dataset.ms !== '' ? Number(tarjeta.dataset.ms) : null,
    tClic: med.clics.length > 0 ? med.clics[0] : null,
    tConmutando: conmutando ? conmutando.t : null,
    tAsentado: asentada ? asentada.t : null,
  };
}

async function medirConmutacion(o, informe, deps) {
  informe.etapa = 'precondicion';
  const { pw, version } = cargarPlaywright();
  informe.navegador.playwright = version;
  const slot = o.conmutarSlot;
  informe.slot = slot;
  try {
    accessSync(o.xpraBin, constants.X_OK);
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `${o.xpraBin} is not an executable file: build it first (go build ./cmd/srf-xpra)`, e);
  }
  const desacuerdo = (campo, t) => {
    informe.desacuerdos.push(t);
    if (!informe.campos_en_desacuerdo.includes(campo)) informe.campos_en_desacuerdo.push(campo);
  };

  // the port plan and its second source
  let puertos;
  try {
    puertos = await deps.puertos(slot);
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `the port of slot ${slot} cannot be read: ${primeraLinea(e.message)}`, e);
  }
  if (puertos.plan !== puertos.publicado) throw new ErrorMedicion('precondicion', 2, `the port plan says ${puertos.plan} for slot ${slot} but the container publishes ${puertos.publicado}`);
  informe.puerto_xpra = puertos.plan;
  const backend = deps.backend;
  try {
    const st = await backend.sondear(puertos.plan);
    if (st !== 200) throw new Error(`HTTP ${st}`);
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `no live capsule answers on Xpra port ${puertos.plan} (slot ${slot}): ${primeraLinea(e.message)}`, e);
  }
  let estadoInicial;
  try {
    estadoInicial = await deps.estado(slot);
  } catch (e) {
    throw new ErrorMedicion('precondicion', 2, `the readonly state of slot ${slot} cannot be read back: ${primeraLinea(e.message)}`, e);
  }

  informe.etapa = 'harness';
  const raizTemporal = mkdtempSync(join(tmpdir(), 'srf-medir-contraste-'));
  const tmpAnterior = process.env.TMPDIR;
  process.env.TMPDIR = raizTemporal;
  let navegador = null;
  let web = null;
  try {
    const dist = await (deps.construirHarness ?? construirHarness)({ padre: raizTemporal }).catch((e) => {
      throw new ErrorMedicion('harness', 1, `the card harness could not be built: ${primeraLinea(e.message)}`, e);
    });
    const tokensCss = tokensDeApp(readFileSync(join(FRONTEND, 'src', 'app.css'), 'utf8'));
    const cfg = {
      fijado: true,
      autoOcultarMs: 2000,
      capsulas: [
        {
          id: `srf-ng-${slot}`, name: `srf-ng-${slot} · browser`, archetype: 'browser', tier: 'docker_l2', mode: estadoInicial.readonly ? 'read_only' : 'operator_controlled', health: 'healthy', slot,
          xpraPort: puertos.plan, cdpPort: puertos.cdp ?? null,
          telemetry: { cpuPercent: 0, memoryBytes: 0, memoryLimitBytes: 0, networkRxRateBps: 0, networkTxRateBps: 0 },
          gates: {}, recording: { recording: false, sessionId: '', targetXid: '', sinceMonoMs: 0, offsetMs: null, verdict: '' }, pbx: null, poppedOut: false, occluded: false, createdAt: '',
        },
      ],
    };
    web = await servirHarness({ dist, cfg, tokensCss, backend });

    informe.etapa = 'navegador';
    try {
      navegador = await pw.chromium.launch({ headless: true });
    } catch (e) {
      throw new ErrorMedicion('navegador', 1, `the browser could not be launched: ${primeraLinea(e.message)}`, e);
    }
    informe.navegador.chromium = navegador.version();
    const contexto = await navegador.newContext({ viewport: { width: 1271, height: 1064 }, screen: { width: 3840, height: 2160 }, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: 'no-preference' });
    await contexto.addInitScript(instrumentarPagina);
    const pagina = await contexto.newPage();
    const cdp = await contexto.newCDPSession(pagina);
    const origenHarness = `http://127.0.0.1:${web.puerto}`;
    const navegados = [];
    const separados = [];
    cdp.on('Page.frameNavigated', (e) => {
      if (e.frame.parentId !== undefined) navegados.push({ id: e.frame.id, url: e.frame.url });
    });
    cdp.on('Page.frameDetached', (e) => separados.push({ id: e.frameId, razon: e.reason }));
    const excepciones = [];
    const contextos = new Map();
    cdp.on('Runtime.executionContextCreated', (e) => contextos.set(e.context.id, e.context.auxData?.frameId ?? null));
    cdp.on('Runtime.exceptionThrown', (e) => {
      const d = e.exceptionDetails;
      excepciones.push({ url: d.url ?? d.stackTrace?.callFrames?.[0]?.url ?? '', frameId: contextos.get(d.executionContextId) ?? null, texto: primeraLinea(d.exception?.description ?? d.text ?? '') });
    });
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    const idMarcoPrincipal = (await cdp.send('Page.getFrameTree')).frameTree.frame.id;

    informe.etapa = 'carga';
    try {
      await pagina.goto(`${origenHarness}/`, { waitUntil: 'load' });
    } catch (e) {
      throw new ErrorMedicion('carga', 1, `the harness page could not be loaded: ${primeraLinea(e.message)}`, e);
    }
    informe.etapa = 'listo';
    // An uncaught error belongs to the page under test when it was thrown in the top frame's realm; the Xpra client's own
    // errors (a child frame) are reported apart. An error whose realm is unknown counts as the page's: never hidden.
    const propios = () => excepciones.filter((x) => x.frameId === null || x.frameId === idMarcoPrincipal);
    try {
      await pagina.waitForFunction(() => document.documentElement.dataset.appReady === 'true', null, { timeout: o.esperaListo });
    } catch (e) {
      const dentro = propios();
      throw new ErrorMedicion('listo', 4, dentro.length > 0 ? `the harness page threw before it was ready: ${dentro.map((x) => x.texto).join(' | ')}` : `the harness page never set data-app-ready="true": ${primeraLinea(e.message)}`, e);
    }
    const marcoDe = () => pagina.frames().find((f) => f !== pagina.mainFrame());
    let marco = marcoDe();
    if (marco === undefined) throw new ErrorMedicion('listo', 4, 'the card mounted no iframe');
    await conPlazo(marco.waitForLoadState('load'), o.esperaListo, () => new ErrorMedicion('listo', 4, 'the Xpra frame never finished loading'));
    await pagina.evaluate(async () => {
      await document.fonts.ready;
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    });

    const conmutaciones = [];
    const destinos = estadoInicial.readonly ? ['operador', 'solo_lectura'] : ['solo_lectura', 'operador'];
    const hechas = o.sinVuelta ? destinos.slice(0, 1) : destinos;
    let readonlyAntes = estadoInicial.readonly;

    for (const destino of hechas) {
      informe.etapa = `conmutacion:${destino}`;
      const base = await pagina.evaluate(leerLineaBase);
      if (!base.hay) throw new ErrorMedicion('conmutacion', 4, 'the card has no iframe[data-xpra] to observe');
      marco = marcoDe();
      let origenAntes;
      try {
        origenAntes = await marco.evaluate(() => performance.timeOrigin);
      } catch (e) {
        desacuerdo('iframe_recargado', `the child realm cannot be read before the switch: ${primeraLinea(e.message)}`);
        origenAntes = null;
      }
      const marcoAntes = marco;
      const marcosAntes = pagina.frames().length;
      const navegadosAntes = navegados.length;
      const separadosAntes = separados.length;
      const llamadasAntes = web.llamadas.length;

      const boton = pagina.locator('[data-accion="conmutar"]');
      const etiqueta = await boton.textContent();
      const esperada = destino === 'operador' ? 'Take control' : 'Read-only';
      if (etiqueta !== esperada) throw new ErrorMedicion('conmutacion', 1, `the card offers "${etiqueta}" where the ${destino} switch expects "${esperada}"`);
      const t0 = performance.now();
      await boton.click({ timeout: o.esperaConmutacion });
      try {
        await pagina.waitForFunction(() => {
          const t = document.querySelector('[data-tarjeta]');
          return t !== null && (t.dataset.fase === 'asentado' || t.dataset.fase === 'degradado');
        }, null, { timeout: o.esperaConmutacion, polling: 'raf' });
      } catch (e) {
        const p = await pagina.evaluate(leerDespues).catch(() => null);
        throw new ErrorMedicion('conmutacion', 1, `the card never settled after the ${destino} switch (fase ${p?.fase ?? 'unreadable'}): ${primeraLinea(e.message)}`, e);
      }
      const t1 = performance.now();
      const p = await pagina.evaluate(leerDespues);
      if (p.fase === 'degradado') throw new ErrorMedicion('conmutacion', 1, `the ${destino} switch degraded: ${p.aviso}`);

      // ---- reads from outside the page ----
      const llamada = web.llamadas.slice(llamadasAntes).find((c) => c.tipo === 'conmutar');
      if (llamada === undefined || !llamada.ok) throw new ErrorMedicion('conmutacion', 1, `the loopback endpoint saw no successful switch call (${llamada?.error ?? 'none'})`);
      const marcoDespues = marcoDe();
      let origenDespues = null;
      try {
        origenDespues = await marcoDespues.evaluate(() => performance.timeOrigin);
      } catch (e) {
        desacuerdo('iframe_recargado', `the child realm cannot be read after the switch: ${primeraLinea(e.message)}`);
      }
      let despues;
      try {
        despues = await deps.estado(slot);
      } catch (e) {
        throw new ErrorMedicion('conmutacion', 1, `the readonly state of slot ${slot} cannot be read back after the switch: ${primeraLinea(e.message)}`, e);
      }

      const msPagina = p.tClic !== null && p.tAsentado !== null ? p.tAsentado - p.tClic : null;
      const msExterior = t1 - t0;
      const msServidor = llamada.t_respondido - llamada.t_recibido;
      const msGo = llamada.resultado.ms;
      const registro = {
        sentido: destino,
        ms_pagina: msPagina === null ? null : redondear(msPagina, 1),
        ms_exterior: redondear(msExterior, 1),
        ms_servidor: redondear(msServidor, 1),
        ms_backend_go: msGo,
        ms_componente: p.msComponente === null ? null : redondear(p.msComponente, 1),
        readonly_antes: readonlyAntes,
        readonly_despues: despues.readonly,
        readonly_segun_backend: llamada.resultado.readonly,
        insignia_de_modo: p.insigniaModo,
        modo_de_la_tarjeta: p.modoTarjeta,
        iframe: {
          mismo_nodo: p.mismoNodo,
          conectado: p.conectado,
          src_igual: p.srcIgual,
          cargas_en_pagina: p.cargasDelMarco,
          removidos_en_pagina: p.removidos,
          cdp_marcos_navegados: navegados.length - navegadosAntes,
          cdp_marcos_separados: separados.length - separadosAntes,
          time_origin_igual: origenAntes === null || origenDespues === null ? null : origenAntes === origenDespues,
          marco_playwright_mismo: marcoDespues === marcoAntes,
          marcos_antes: marcosAntes,
          marcos_despues: pagina.frames().length,
        },
      };
      conmutaciones.push(registro);

      // ---- agreement between sources ----
      const ctx = `${destino} switch`;
      if (msPagina === null) desacuerdo('ms', `${ctx}: the page observer saw no click or no settle`);
      else {
        if (!(msGo <= msServidor + 1)) desacuerdo('ms', `${ctx}: srf-xpra's own ${msGo} ms exceeds the endpoint's ${redondear(msServidor, 1)} ms that contains it`);
        if (!(msServidor <= msPagina + 1)) desacuerdo('ms', `${ctx}: the endpoint's ${redondear(msServidor, 1)} ms exceeds the page's ${redondear(msPagina, 1)} ms that contains it`);
        if (!(msPagina <= msExterior + 1)) desacuerdo('ms', `${ctx}: the page's ${redondear(msPagina, 1)} ms exceeds the outside clock's ${redondear(msExterior, 1)} ms that contains it`);
        if (msExterior - msPagina > 1000) desacuerdo('ms', `${ctx}: page ${redondear(msPagina, 1)} ms vs outside clock ${redondear(msExterior, 1)} ms differ by more than 1000 ms`);
        if (p.msComponente !== null && Math.abs(p.msComponente - msPagina) > 100) desacuerdo('ms', `${ctx}: page ${redondear(msPagina, 1)} ms vs the card's own clock ${redondear(p.msComponente, 1)} ms`);
      }
      if (despues.readonly !== (destino === 'solo_lectura')) desacuerdo('readonly', `${ctx}: the container reads readonly=${despues.readonly} afterwards`);
      if (llamada.resultado.readonly !== despues.readonly) desacuerdo('readonly', `${ctx}: srf-xpra answered readonly=${llamada.resultado.readonly}, the container reads readonly=${despues.readonly}`);
      const badgeEsperado = destino === 'operador' ? 'OPERATOR-CONTROL' : 'READ-ONLY';
      if (p.insigniaModo !== badgeEsperado) desacuerdo('readonly', `${ctx}: the card shows "${p.insigniaModo}", expected "${badgeEsperado}"`);
      const recargaPagina = p.cargasDelMarco > 0 || !p.srcIgual;
      const recargaFuera = registro.iframe.cdp_marcos_navegados > 0 || registro.iframe.time_origin_igual === false;
      if (recargaPagina !== recargaFuera) desacuerdo('iframe_recargado', `${ctx}: the page says reloaded=${recargaPagina}, CDP and the child realm say ${recargaFuera}`);
      const desmontadoPagina = !p.mismoNodo || !p.conectado || p.removidos > 0;
      const desmontadoFuera = registro.iframe.cdp_marcos_separados > 0 || !registro.iframe.marco_playwright_mismo || registro.iframe.marcos_despues !== marcosAntes;
      if (desmontadoPagina !== desmontadoFuera) desacuerdo('iframe_desmontado', `${ctx}: the page says unmounted=${desmontadoPagina}, CDP and Playwright say ${desmontadoFuera}`);
      readonlyAntes = despues.readonly;
    }

    informe.etapa = 'acuerdo_entre_fuentes';
    informe.conmutaciones = conmutaciones;
    informe.errores_de_pagina = propios().map((x) => x.texto);
    informe.errores_del_marco = excepciones.filter((x) => !propios().includes(x)).map((x) => ({ url: x.url, texto: x.texto }));
    informe.fuentes = { llamadas_al_backend: web.llamadas.length, peticiones_http_del_arnes: web.peticiones.length };
    const en = (campo) => informe.campos_en_desacuerdo.includes(campo);
    const todas = (f) => conmutaciones.map(f);
    informe.ms = en('ms') ? null : Math.max(...todas((c) => c.ms_pagina));
    informe.iframe_recargado = en('iframe_recargado') ? null : todas((c) => c.iframe.cargas_en_pagina > 0 || !c.iframe.src_igual).some(Boolean);
    informe.iframe_desmontado = en('iframe_desmontado') ? null : todas((c) => !c.iframe.mismo_nodo || !c.iframe.conectado || c.iframe.removidos_en_pagina > 0).some(Boolean);
    informe.acuerdo_entre_fuentes = informe.desacuerdos.length === 0;
    if (informe.errores_de_pagina.length > 0) throw new ErrorMedicion('acuerdo_entre_fuentes', 4, `the harness page threw ${informe.errores_de_pagina.length} uncaught error(s): ${informe.errores_de_pagina.join(' | ')}`);
    if (!informe.acuerdo_entre_fuentes) throw new ErrorMedicion('acuerdo_entre_fuentes', 3, `two sources of one field disagree: ${informe.desacuerdos.join('; ')}`);
  } finally {
    if (navegador !== null) await navegador.close().catch(() => {});
    if (web !== null) await web.cerrar();
    if (tmpAnterior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpAnterior;
    rmSync(raizTemporal, { recursive: true, force: true });
  }
}

/** The real port and readonly readers: tools outside this script, the far side of the interface. */
export function dependenciasReales({ xpraBin = join(RAIZ, 'bin', 'srf-xpra') } = {}) {
  return {
    backend: backendReal({ xpraBin }),
    async puertos(slot) {
      const plan = await ejecutarProceso(join(RAIZ, 'bin', 'srf-puertos'), ['par', '--slot', String(slot), '--json']);
      if (plan.rc !== 0) throw new Error(`srf-puertos par rc ${plan.rc}: ${primeraLinea(plan.stderr || plan.error || '')}`);
      const p = JSON.parse(plan.stdout);
      const dk = await ejecutarProceso('docker', ['port', `srf-ng-${slot}`, '10000/tcp']);
      if (dk.rc !== 0) throw new Error(`docker port srf-ng-${slot} rc ${dk.rc}: ${primeraLinea(dk.stderr || dk.error || 'no such container')}`);
      const publicado = Number(/:(\d+)\s*$/m.exec(dk.stdout)?.[1]);
      return { plan: p.xpra, cdp: p.cdp, publicado };
    },
    async estado(slot) {
      const r = await ejecutarProceso(xpraBin, ['estado', '--slot', String(slot), '--json']);
      if (r.rc !== 0) throw new Error(`srf-xpra estado rc ${r.rc}: ${primeraLinea(r.stderr || r.error || '')}`);
      const j = JSON.parse(r.stdout);
      if (typeof j.readonly !== 'boolean') throw new Error(`srf-xpra estado: no readonly flag in ${r.stdout.slice(0, 100)}`);
      return { readonly: j.readonly };
    },
  };
}

/** Runs one measurement; returns the report and the exit code. Never throws. */
export async function ejecutar(argv, deps) {
  const informe = informeVacio();
  informe.etapa = 'argumentos';
  let rc = 1;
  try {
    const o = leerArgumentos(argv);
    informe.modo = o.tokens !== null ? 'tokens' : 'conmutar';
    if (o.tokens !== null) await medirTokens(o, informe);
    else await medirConmutacion(o, informe, deps ?? dependenciasReales({ xpraBin: o.xpraBin }));
    informe.ok = true;
    informe.etapa = 'completa';
    rc = 0;
  } catch (e) {
    informe.ok = false;
    informe.error = e instanceof ErrorMedicion ? e.message : `measurement failed: ${primeraLinea(e && e.message ? e.message : e)}`;
    if (e instanceof ErrorMedicion && e.etapa !== undefined && e.etapa !== informe.etapa && !String(informe.etapa).startsWith('conmutacion')) informe.etapa = e.etapa;
    rc = e instanceof ErrorMedicion ? e.rc : 1;
  }
  informe.rc = rc;
  return { informe, rc };
}

async function main() {
  const argv = process.argv.slice(2);
  const { informe, rc } = await ejecutar(argv);
  process.stdout.write(`${argv.includes('--json') ? JSON.stringify(informe) : JSON.stringify(informe, null, 2)}\n`);
  if (rc !== 0) process.stderr.write(`medir-contraste: ${informe.etapa}: ${informe.error}\n`);
  return rc;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main();
}
