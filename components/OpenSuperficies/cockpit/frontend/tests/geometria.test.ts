// Tests of the cockpit shell's single geometry source and of what must agree with it. Every expectation is read
// from a source OTHER than the code under test (the specification text, app.css, the Go files, index.html run in a
// sandbox), so a figure that drifts in any one place turns a test red. evidencia/v2/u18/mutaciones-1.txt (attempt 1) and
// mutaciones-2.txt (completion round) record, for each test, the production line that was neutered and the red it produced.

import { execFile, spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';
import vm from 'node:vm';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import {
  ANCHO_EXACTO_MIN,
  BARRA_H,
  CLAVE_PREFERENCIAS,
  DOM_H_SEXTO,
  DOM_H_TERCIO,
  DOM_W,
  GUTTER_DER,
  GUTTER_IZQ,
  LIENZO_H,
  LIENZO_H_ALTO,
  LIENZO_W,
  VENTANA_H,
  VENTANA_W,
  aplicarPreferencias,
  domNominal,
  etiquetaModo,
  geometriaDeVentana,
  leerPreferencias,
  modoPorAltura,
  resolverPreferencias,
} from '../src/lib/geometria';
import {
  RC_FALLO,
  RC_PARADA,
  RC_PEER_RECHAZADO,
  RC_SLOT_OCUPADO,
  RC_USO,
  SrfdError,
  causaDeRC,
  crearClienteSrfd,
  transporteWails,
} from '../src/lib/srfd';
import { cajaDeColor, clsDeEntradas, contextosDeTraza, decodificarPng, derivarViewport, desacuerdosDeAlcance } from '../../../scripts/ng/medir-cls.mjs';

const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');

const ESPEC = leer('../../../ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md');
const CSS = leer('../src/app.css');
const HTML = leer('../index.html');
const MAIN_GO = leer('../../main.go');
const UDS_GO = leer('../../../src/surfacebook/manager/uds.go');
const PARADA_GO = leer('../../../src/surfacebook/manager/parada.go');

/** `--nombre: 123px;` declarations of a block of text, as numbers. */
function tokensPx(texto: string): Record<string, number> {
  const t: Record<string, number> = {};
  for (const m of texto.matchAll(/--([a-z0-9-]+):\s*(\d+)px;/g)) t[m[1]] = Number(m[2]);
  return t;
}

/** The value of `NAME = 123` (Go constant) in a source text. */
function constanteGo(texto: string, nombre: string): number {
  const m = new RegExp(`\\b${nombre}\\s*=\\s*(\\d+)`).exec(texto);
  if (m === null) throw new Error(`constant ${nombre} not found`);
  return Number(m[1]);
}

const TOKENS_GEOMETRIA: Record<string, number> = {
  'win-w': VENTANA_W,
  'dom-w': DOM_W,
  'bar-h': BARRA_H,
  'canvas-w': LIENZO_W,
  'canvas-h': LIENZO_H,
  'canvas-h-tall': LIENZO_H_ALTO,
  'gutter-left': GUTTER_IZQ,
  'gutter-right': GUTTER_DER,
};

describe('geometry derived from the host facts', () => {
  it('equals the figures of the specification (section 1.5 tokens and section 1.2 table)', () => {
    const seccion = /### 1\.5 Geometry tokens([\s\S]*?)###/.exec(ESPEC);
    expect(seccion).not.toBeNull();
    const espec = tokensPx(seccion![1]);
    for (const [nombre, valor] of Object.entries(TOKENS_GEOMETRIA)) {
      expect(espec[nombre], `spec token --${nombre}`).toBe(valor);
    }
    // Section 1.2 table, in prose form: the four figures the oracle asserts.
    expect(`${VENTANA_W}x${VENTANA_H}`).toBe('1280x1064');
    expect(`${DOM_W}x${DOM_H_SEXTO}`).toBe('1271x1064');
    expect(`${DOM_W}x${DOM_H_TERCIO}`).toBe('1271x2128');
    expect(`${LIENZO_W}x${LIENZO_H}`).toBe('1264x1024');
    expect(`${LIENZO_W}x${LIENZO_H_ALTO}`).toBe('1264x2080');
    // The 1/3 dual arithmetic closes exactly: 40 + 1024 + 40 + 1024 = 2128.
    expect(BARRA_H + LIENZO_H + BARRA_H + LIENZO_H).toBe(DOM_H_TERCIO);
    // 7 px of horizontal slack, split 3 / 4.
    expect(GUTTER_IZQ + LIENZO_W + GUTTER_DER).toBe(DOM_W);
  });

  it('keeps every canvas dimension a multiple of the codec alignment and never larger than its viewport', () => {
    for (const d of [LIENZO_W, LIENZO_H, LIENZO_H_ALTO]) expect(d % 16).toBe(0);
    expect(LIENZO_W).toBeLessThanOrEqual(DOM_W);
    expect(BARRA_H + LIENZO_H).toBeLessThanOrEqual(DOM_H_SEXTO);
    expect(BARRA_H + LIENZO_H_ALTO).toBeLessThanOrEqual(DOM_H_TERCIO);
  });
});

describe('app.css mirrors geometria', () => {
  it('declares every geometry token with the value the module derives', () => {
    const raiz = /:root\s*\{([\s\S]*?)\n\}/.exec(CSS);
    expect(raiz).not.toBeNull();
    const css = tokensPx(raiz![1]);
    for (const [nombre, valor] of Object.entries(TOKENS_GEOMETRIA)) {
      expect(css[nombre], `app.css --${nombre}`).toBe(valor);
    }
  });
});

describe('mode and geometry of a DOM viewport', () => {
  it('selects the 1/3 column from a DOM height of 2000 and the 1/6 tile below it', () => {
    expect(modoPorAltura(1064)).toBe('tile_1_6');
    expect(modoPorAltura(1999)).toBe('tile_1_6');
    expect(modoPorAltura(2000)).toBe('column_1_3_tall');
    expect(modoPorAltura(2128)).toBe('column_1_3_tall');
    expect(etiquetaModo('tile_1_6')).toBe('sexto');
    expect(etiquetaModo('column_1_3_tall')).toBe('tercio');
  });

  it('returns the pixel-exact canvas at nominal size and an aspect-fit scale below 1264 px', () => {
    expect(geometriaDeVentana(1271, 1064)).toEqual({ mode: 'tile_1_6', domWidth: 1271, domHeight: 1064, canvasWidth: 1264, canvasHeight: 1024, scaled: false, scale: 1 });
    expect(geometriaDeVentana(1271, 2128)).toEqual({ mode: 'column_1_3_tall', domWidth: 1271, domHeight: 2128, canvasWidth: 1264, canvasHeight: 2080, scaled: false, scale: 1 });
    expect(geometriaDeVentana(1264, 1064).scaled).toBe(false);
    const estrecha = geometriaDeVentana(1200, 1064);
    expect(estrecha.scaled).toBe(true);
    expect(estrecha.scale).toBeCloseTo(1200 / 1264, 12);
    expect(ANCHO_EXACTO_MIN).toBe(1264);
  });

  it('gives the nominal DOM viewport of each CLI mode', () => {
    expect(domNominal('1/6')).toEqual({ w: 1271, h: 1064 });
    expect(domNominal('1/3')).toEqual({ w: 1271, h: 2128 });
  });
});

describe('persisted preferences', () => {
  const casos: Array<[string, string | null, { tema: string; escala: number }, boolean]> = [
    ['nothing stored', null, { tema: 'oscuro', escala: 1 }, false],
    ['a valid non-default preference', '{"tema":"alto-contraste","escala":1.25}', { tema: 'alto-contraste', escala: 1.25 }, false],
    ['a valid scale with the default theme', '{"tema":"oscuro","escala":1.125}', { tema: 'oscuro', escala: 1.125 }, false],
    ['values outside the closed sets', '{"tema":"neon","escala":9}', { tema: 'oscuro', escala: 1 }, false],
    ['a scale stored as a string', '{"escala":"1.25"}', { tema: 'oscuro', escala: 1 }, false],
    ['only the theme', '{"tema":"alto-contraste"}', { tema: 'alto-contraste', escala: 1 }, false],
    ['JSON that is not an object', '5', { tema: 'oscuro', escala: 1 }, false],
    ['JSON null', 'null', { tema: 'oscuro', escala: 1 }, false],
    ['text that is not JSON', 'not json', { tema: 'oscuro', escala: 1 }, true],
  ];

  for (const [nombre, bruto, esperado, conError] of casos) {
    it(`resolves ${nombre}`, () => {
      const r = resolverPreferencias(bruto);
      expect(r.prefs).toEqual(esperado);
      expect(r.error !== '').toBe(conError);
    });
  }

  it('reports a storage that throws instead of hiding it', () => {
    const r = leerPreferencias({
      getItem: () => {
        throw new Error('storage blocked');
      },
    });
    expect(r.prefs).toEqual({ tema: 'oscuro', escala: 1 });
    expect(r.error).toContain('storage blocked');
  });

  it('reads the key the bootstrap reads', () => {
    let pedida = '';
    leerPreferencias({
      getItem: (k) => {
        pedida = k;
        return null;
      },
    });
    expect(pedida).toBe(CLAVE_PREFERENCIAS);
    expect(pedida).toBe('srf.cockpit.prefs.v1');
  });
});

// ---- the synchronous head bootstrap of index.html, executed as it ships ----

interface RaizFalsa {
  dataset: Record<string, string>;
  style: { props: Record<string, string>; setProperty(k: string, v: string): void };
  classList: { clases: Set<string>; add(c: string): void };
}

function raizFalsa(): RaizFalsa {
  return {
    dataset: {},
    style: {
      props: {},
      setProperty(k, v) {
        this.props[k] = v;
      },
    },
    classList: {
      clases: new Set<string>(),
      add(c) {
        this.clases.add(c);
      },
    },
  };
}

function ejecutarBootstrap(getItem: (k: string) => string | null, innerHeight: number): RaizFalsa {
  const bloque = /<script data-arranque-sincrono>([\s\S]*?)<\/script>/.exec(HTML);
  if (bloque === null) throw new Error('bootstrap block not found in index.html');
  const raiz = raizFalsa();
  vm.runInNewContext(bloque[1], { document: { documentElement: raiz }, window: { localStorage: { getItem }, innerHeight } });
  return raiz;
}

describe('synchronous head bootstrap (index.html)', () => {
  const almacenados: Array<string | null> = [null, '{"tema":"alto-contraste","escala":1.25}', '{"tema":"oscuro","escala":1.125}', '{"tema":"neon","escala":9}', '{"escala":"1.25"}', '{"tema":"alto-contraste"}', '5', 'null', '[]', 'not json'];

  for (const bruto of almacenados) {
    for (const altura of [1064, 2128]) {
      it(`agrees with resolverPreferencias for ${JSON.stringify(bruto)} at height ${altura}`, () => {
        const raiz = ejecutarBootstrap(() => bruto, altura);
        const { prefs, error } = resolverPreferencias(bruto);
        expect(raiz.dataset.tema).toBe(prefs.tema);
        expect(raiz.style.props['--escala']).toBe(String(prefs.escala));
        expect(raiz.dataset.modo).toBe(etiquetaModo(modoPorAltura(altura)));
        expect(raiz.dataset.arranque).toBe('sincrono');
        expect(raiz.classList.clases.has('initializing')).toBe(true);
        expect(raiz.dataset.arranqueError !== undefined).toBe(error !== '');
      });
    }
  }

  it('applies a non-default preference before anything else can (the value the CLS measurement seeds)', () => {
    const raiz = ejecutarBootstrap((k) => (k === 'srf.cockpit.prefs.v1' ? '{"tema":"alto-contraste","escala":1.25}' : null), 1064);
    expect(raiz.dataset.tema).toBe('alto-contraste');
    expect(raiz.style.props['--escala']).toBe('1.25');
  });

  it('reports an unreadable storage in data-arranque-error and still applies the defaults', () => {
    const raiz = ejecutarBootstrap(() => {
      throw new Error('storage blocked');
    }, 1064);
    expect(raiz.dataset.arranqueError).toContain('storage blocked');
    expect(raiz.dataset.tema).toBe('oscuro');
    expect(raiz.style.props['--escala']).toBe('1');
  });
});

describe('aplicarPreferencias (the hydration fallback)', () => {
  it('writes exactly the three things the bootstrap writes', () => {
    for (const altura of [1064, 2128]) {
      const prefs = { tema: 'alto-contraste' as const, escala: 1.25 };
      const porBootstrap = ejecutarBootstrap(() => JSON.stringify(prefs), altura);
      const porApp = raizFalsa();
      aplicarPreferencias(porApp as unknown as HTMLElement, prefs, altura);
      expect(porApp.dataset.tema).toBe(porBootstrap.dataset.tema);
      expect(porApp.dataset.modo).toBe(porBootstrap.dataset.modo);
      expect(porApp.style.props['--escala']).toBe(porBootstrap.style.props['--escala']);
    }
  });
});

// ---- the srfd client ----

describe('srfd client', () => {
  const enviado: Array<[string, string, number]> = [];
  const transporte = (respuesta: unknown) => ({
    enviar: async (c: string, k: string, s: number) => {
      enviado.push([c, k, s]);
      if (respuesta instanceof Error) throw respuesta;
      return respuesta;
    },
  });
  const ESTADO = { estado: 'ok', parada: 'LIBRE', vivas: 0, reservado_mib: 0, disponible_mib: 1234, soberana_mib: 4096 };

  it('returns the data of an ok answer and sends the verb, class and slot', async () => {
    enviado.length = 0;
    const c = crearClienteSrfd(transporte({ ok: true, rc: 0, datos: ESTADO }));
    expect(await c.estado()).toEqual(ESTADO);
    expect(enviado.at(-1)).toEqual(['estado', '', 0]);
    const iniciar = crearClienteSrfd(transporte({ ok: true, rc: 0, datos: { slot: 3, clase: 'browser', estado: 'listo' } }));
    expect((await iniciar.iniciar('browser', 3)).slot).toBe(3);
    expect(enviado.at(-1)).toEqual(['iniciar', 'browser', 3]);
  });

  it('turns a refusal into a SrfdError carrying the exit code and the daemon text', async () => {
    const c = crearClienteSrfd(transporte({ ok: false, rc: RC_PARADA, error: 'stop signal is effective' }));
    const e = await c.estado().then(() => null, (x: unknown) => x);
    expect(e).toBeInstanceOf(SrfdError);
    expect((e as SrfdError).codigo).toBe('rechazado');
    expect((e as SrfdError).rc).toBe(3);
    expect((e as SrfdError).message).toContain('stop signal is effective');
  });

  it('never lets a refusal carry rc 0', async () => {
    const c = crearClienteSrfd(transporte({ ok: false, rc: 0 }));
    const e = (await c.listar().then(() => null, (x: unknown) => x)) as SrfdError;
    expect(e.codigo).toBe('rechazado');
    expect(e.rc).toBe(RC_FALLO);
  });

  it('surfaces a transport failure with its cause', async () => {
    const causa = new Error('dial unix /run/user/1000/opensuperficies/srfd.sock: connect: no such file');
    const c = crearClienteSrfd(transporte(causa));
    const e = (await c.estado().then(() => null, (x: unknown) => x)) as SrfdError;
    expect(e.codigo).toBe('transporte');
    expect(e.message).toContain('no such file');
    expect(e.cause).toBe(causa);
  });

  it('refuses an answer that is not the srfd envelope and an ok answer without data', async () => {
    for (const rara of ['ok', 7, null, { ok: 'yes', rc: 0 }, { rc: 0 }]) {
      const e = (await crearClienteSrfd(transporte(rara)).estado().then(() => null, (x: unknown) => x)) as SrfdError;
      expect(e.codigo, JSON.stringify(rara)).toBe('respuesta_invalida');
    }
    const sinDatos = (await crearClienteSrfd(transporte({ ok: true, rc: 0 })).estado().then(() => null, (x: unknown) => x)) as SrfdError;
    expect(sinDatos.codigo).toBe('respuesta_invalida');
    const noLista = (await crearClienteSrfd(transporte({ ok: true, rc: 0, datos: { a: 1 } })).listar().then(() => null, (x: unknown) => x)) as SrfdError;
    expect(noLista.codigo).toBe('respuesta_invalida');
    expect(await crearClienteSrfd(transporte({ ok: true, rc: 0, datos: [] })).listar()).toEqual([]);
  });

  it('reports the missing bridge of a plain browser as sin_puente', async () => {
    const e = (await crearClienteSrfd(null).estado().then(() => null, (x: unknown) => x)) as SrfdError;
    expect(e.codigo).toBe('sin_puente');
    expect(transporteWails({})).toBeNull();
    expect(transporteWails({ go: { main: { App: {} } } })).toBeNull();
    const llamadas: unknown[][] = [];
    const t = transporteWails({ go: { main: { App: { Enviar: async (...a: unknown[]) => (llamadas.push(a), { ok: true, rc: 0 }) } } } });
    expect(t).not.toBeNull();
    await t!.enviar('detener', 'browser', 2);
    expect(llamadas).toEqual([['detener', 'browser', 2]]);
  });

  it('has the exit codes srfd assigns (read from the Go sources)', () => {
    expect(RC_FALLO).toBe(constanteGo(UDS_GO, 'RCFallo'));
    expect(RC_USO).toBe(constanteGo(UDS_GO, 'RCUso'));
    expect(RC_PEER_RECHAZADO).toBe(constanteGo(UDS_GO, 'RCPeerRechazado'));
    expect(RC_SLOT_OCUPADO).toBe(constanteGo(UDS_GO, 'RCSlotOcupado'));
    expect(RC_PARADA).toBe(constanteGo(PARADA_GO, 'RCParada'));
    expect(causaDeRC(999)).toContain('unknown srfd exit code 999');
  });
});

// ---- the Go shell and the measurement instrument agree with the module ----

describe('cockpit/main.go window', () => {
  it('opens the window at the 1/6 tile size geometria derives', () => {
    expect(constanteGo(MAIN_GO, 'ventanaAncho')).toBe(VENTANA_W);
    expect(constanteGo(MAIN_GO, 'ventanaAlto')).toBe(VENTANA_H);
  });
});

describe('scripts/ng/medir-cls.mjs', () => {
  it('derives the same viewports as geometria, by its own arithmetic', () => {
    const sexto = derivarViewport('1/6');
    expect(sexto.dom).toEqual({ w: DOM_W, h: DOM_H_SEXTO });
    expect(sexto.ventana).toEqual({ w: VENTANA_W, h: VENTANA_H });
    expect(derivarViewport('1/3').dom).toEqual({ w: DOM_W, h: DOM_H_TERCIO });
    expect(() => derivarViewport('1/2')).toThrow(RangeError);
  });

  it('computes CLS as the largest session window, excluding shifts after input', () => {
    // 0.1 at 0 ms and 0.2 at 500 ms form one window (0.3); 0.25 at 2000 ms starts another.
    expect(clsDeEntradas([{ t: 0, value: 0.1, hadRecentInput: false }, { t: 500, value: 0.2, hadRecentInput: false }, { t: 2000, value: 0.25, hadRecentInput: false }])).toEqual({ cls: 0.30000000000000004, contadas: 3 });
    // A shift after recent input is not counted at all.
    expect(clsDeEntradas([{ t: 0, value: 0.5, hadRecentInput: true }, { t: 10, value: 0.01, hadRecentInput: false }])).toEqual({ cls: 0.01, contadas: 1 });
    // A gap of exactly 1000 ms closes the window.
    expect(clsDeEntradas([{ t: 0, value: 0.1, hadRecentInput: false }, { t: 1000, value: 0.1, hadRecentInput: false }]).cls).toBe(0.1);
    // A window never spans 5 s: a shift every 900 ms restarts the window at 5400 ms.
    const cadena = Array.from({ length: 8 }, (_, i) => ({ t: i * 900, value: 0.01, hadRecentInput: false }));
    expect(clsDeEntradas(cadena).cls).toBeCloseTo(0.06, 12);
    expect(clsDeEntradas([])).toEqual({ cls: 0, contadas: 0 });
  });

  it('decodes a PNG with every filter type and finds the bounding box of a colour', () => {
    // 6 x 5 RGB image of non-trivial content (every byte in 100..199, so every filter predicts something) with a
    // 3 x 2 block of (14,19,31) at x=2..4, y=1..2.
    const ancho = 6;
    const alto = 5;
    const px = Buffer.alloc(ancho * alto * 3);
    for (let i = 0; i < px.length; i++) px[i] = 100 + ((i * 37 + Math.floor(i / (ancho * 3)) * 11) % 100);
    for (let y = 1; y <= 2; y++) for (let x = 2; x <= 4; x++) px.set([14, 19, 31], (y * ancho + x) * 3);
    const filas: Buffer[] = [];
    for (let y = 0; y < alto; y++) {
      const filtro = y % 5; // rows use filters 0, 1, 2, 3, 4
      const fila = Buffer.alloc(1 + ancho * 3);
      fila[0] = filtro;
      for (let i = 0; i < ancho * 3; i++) {
        const a = i >= 3 ? px[y * ancho * 3 + i - 3] : 0;
        const b = y > 0 ? px[(y - 1) * ancho * 3 + i] : 0;
        const c = i >= 3 && y > 0 ? px[(y - 1) * ancho * 3 + i - 3] : 0;
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        const paeth = pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
        const pred = [0, a, b, (a + b) >> 1, paeth][filtro];
        fila[1 + i] = (px[y * ancho * 3 + i] - pred) & 0xff;
      }
      filas.push(fila);
    }
    const crc = Buffer.alloc(4); // the decoder does not verify CRCs
    const trozo = (tipo: string, cuerpo: Buffer) => {
      const largo = Buffer.alloc(4);
      largo.writeUInt32BE(cuerpo.length);
      return Buffer.concat([largo, Buffer.from(tipo, 'latin1'), cuerpo, crc]);
    };
    const ihdr = Buffer.alloc(13);
    ihdr.writeUInt32BE(ancho, 0);
    ihdr.writeUInt32BE(alto, 4);
    ihdr.set([8, 2, 0, 0, 0], 8);
    const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), trozo('IHDR', ihdr), trozo('IDAT', deflateSync(Buffer.concat(filas))), trozo('IEND', Buffer.alloc(0))]);
    const imagen = decodificarPng(png);
    expect(imagen.width).toBe(6);
    expect(imagen.height).toBe(5);
    expect(Buffer.compare(imagen.datos, px)).toBe(0);
    expect(cajaDeColor(imagen, [14, 19, 31])).toEqual({ x: 2, y: 1, w: 3, h: 2 });
    expect(cajaDeColor(imagen, [1, 2, 3])).toBeNull();
  });
});

// =====================================================================================================================
// Round 2 (completion of U-18). Everything below runs the REAL pieces: it builds the app from the tree under test into a
// temporary directory, drives headless Chromium through scripts/ng/medir-cls.mjs and compiles cockpit/app.go against a
// fake srfd. evidencia/v2/u18/mutaciones-2.txt records, for each test, the production line that was neutered and the red
// it produced. Temporary files live in one directory that afterAll removes.
// =====================================================================================================================

const RAIZ_REPO = fileURLToPath(new URL('../../../', import.meta.url));
const RAIZ_FRONTEND = fileURLToPath(new URL('..', import.meta.url));
const INSTRUMENTO = join(RAIZ_REPO, 'scripts', 'ng', 'medir-cls.mjs');
const FUENTE_PRUEBAS_GO = join(RAIZ_REPO, 'evidencia', 'v2', 'u18', 'app_test.go.txt');

type Informe = Record<string, any>;
interface Proceso {
  rc: number | null;
  stdout: string;
  stderr: string;
}
interface Medicion extends Proceso {
  informe: Informe;
}

function ejecutarProceso(orden: string, args: string[], opciones: { cwd: string; env?: NodeJS.ProcessEnv; timeout?: number }): Promise<Proceso> {
  return new Promise((resolver) => {
    execFile(
      orden,
      args,
      { cwd: opciones.cwd, env: opciones.env ?? process.env, timeout: opciones.timeout ?? 170_000, killSignal: 'SIGTERM', maxBuffer: 256 << 20 },
      (error, stdout, stderr) => {
        const codigo = (error as (Error & { code?: unknown }) | null)?.code;
        resolver({ rc: error === null ? 0 : typeof codigo === 'number' ? codigo : null, stdout, stderr });
      },
    );
  });
}

/** One run of the instrument, as a subprocess exactly as the oracle runs it; its stdout must always be a report. */
async function medir(args: string[], entorno: Record<string, string> = {}): Promise<Medicion> {
  const p = await ejecutarProceso(process.execPath, [INSTRUMENTO, ...args], { cwd: RAIZ_REPO, env: { ...process.env, ...entorno } });
  let informe: Informe;
  try {
    informe = JSON.parse(p.stdout);
  } catch {
    throw new Error(`medir-cls wrote no JSON report on stdout (rc ${p.rc}):\nstdout: ${p.stdout}\nstderr: ${p.stderr}`);
  }
  return { ...p, informe };
}

/** The exit status of `jq -e <filtro>` on a report: how the oracle and the invariants judge it. */
function jq(filtro: string, texto: string): number | null {
  return spawnSync('jq', ['-e', filtro], { input: texto }).status;
}

/** The nested key structure of a report (arrays and scalars are leaves): the "same shape" of every path. */
function forma(v: unknown): unknown {
  if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
    return Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([k, x]) => [k, forma(x)]));
  }
  return 'hoja';
}

const CAMPOS_MEDIDOS = ['dom', 'lienzo', 'lienzo_origen', 'desplazamiento', 'cls', 'entradas_contadas', 'contextos_webgl'];

describe.skipIf(!process.env.OSCU_BROWSER_TESTS)('cockpit shell in a real browser, a real build and a real Go compile', () => {
  let TMP = '';
  let DIST = ''; // the app built from the tree under test
  const memo = new Map<string, Promise<Medicion>>();

  beforeAll(async () => {
    TMP = mkdtempSync(join(tmpdir(), 'srf-u18-'));
    DIST = join(TMP, 'dist');
    const construir = await ejecutarProceso(process.execPath, [join(RAIZ_FRONTEND, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', DIST, '--emptyOutDir'], { cwd: RAIZ_FRONTEND });
    if (construir.rc !== 0) throw new Error(`vite build of the tree under test failed (rc ${construir.rc}):\n${construir.stdout}\n${construir.stderr}`);
  });

  afterAll(() => {
    if (TMP !== '') rmSync(TMP, { recursive: true, force: true });
  });

  /** A measurement of the app built from the tree; the same arguments are measured once. */
  function real(modo: '1/6' | '1/3', extra: string[] = []): Promise<Medicion> {
    const clave = [modo, ...extra].join(' ');
    let m = memo.get(clave);
    if (m === undefined) {
      m = medir(['--modo', modo, '--json', '--dist', DIST, ...extra]);
      memo.set(clave, m);
    }
    return m;
  }

  /** A page that carries the real synchronous bootstrap and a [data-lienzo] frame, plus `cuerpo`. It calls srfListo() when ready. */
  function fixtura(nombre: string, cuerpo: string, conArranque = true, extras: Record<string, string> = {}): string {
    const dir = join(TMP, `fixtura-${nombre}`);
    mkdirSync(dir, { recursive: true });
    for (const [fichero, contenido] of Object.entries(extras)) writeFileSync(join(dir, fichero), contenido);
    const arranque = /<script data-arranque-sincrono>[\s\S]*?<\/script>/.exec(HTML)![0];
    writeFileSync(
      join(dir, 'index.html'),
      `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>fixture ${nombre}</title>
${conArranque ? arranque : ''}
<script>function srfListo() { requestAnimationFrame(() => requestAnimationFrame(() => { document.documentElement.dataset.appReady = 'true'; })); }</script>
</head><body style="margin:0"><main data-lienzo style="position:absolute;left:3px;top:40px;width:1264px;height:1024px;background:#0e131f"></main>
<script>${cuerpo}</script></body></html>`,
    );
    return dir;
  }

  const CUERPO_IFRAME_Y_TRABAJADOR = `
    window.__glPrincipal = document.createElement('canvas').getContext('webgl2');
    const marco = document.createElement('iframe');
    const marcoListo = new Promise((ok) => { marco.onload = ok; });
    marco.srcdoc = '<canvas id="c"></canvas><script>window.__gl = document.getElementById("c").getContext("webgl2");<\\/script>';
    document.body.appendChild(marco);
    const obrero = new Worker(URL.createObjectURL(new Blob(['self.gl = new OffscreenCanvas(8, 8).getContext("webgl2"); postMessage(!!self.gl);'])));
    const obreroListo = new Promise((ok) => { obrero.onmessage = ok; });
    Promise.all([marcoListo, obreroListo]).then(srfListo);`;
  const CUERPO_TRABAJADOR_ANIDADO = `
    const interior = 'self.gl = new OffscreenCanvas(8, 8).getContext("webgl2"); postMessage("interior");';
    const exterior = 'self.gl = new OffscreenCanvas(8, 8).getContext("webgl2"); const w = new Worker(URL.createObjectURL(new Blob([' + JSON.stringify(interior) + ']))); w.onmessage = () => postMessage("anidado");';
    const obrero = new Worker(URL.createObjectURL(new Blob([exterior])));
    obrero.onmessage = () => srfListo();`;
  const CUERPO_DOS_EN_EL_MARCO = `
    window.__a = document.createElement('canvas').getContext('webgl2');
    window.__b = document.createElement('canvas').getContext('webgl');
    srfListo();`;
  const CUERPO_MISMO_LIENZO_DOS_VECES = `
    const lienzo = document.createElement('canvas');
    window.__a = lienzo.getContext('webgl2');
    window.__b = lienzo.getContext('webgl2');
    srfListo();`;
  const CUERPO_MARCO_OTRO_ORIGEN = `
    const marco = document.createElement('iframe');
    marco.src = 'http://localhost:' + location.port + '/hijo.html';
    window.addEventListener('message', () => srfListo());
    document.body.appendChild(marco);`;
  const HIJO_OTRO_ORIGEN = `<!doctype html><html><body><script>
    window.gl = document.createElement('canvas').getContext('webgl2');
    const obrero = new Worker(URL.createObjectURL(new Blob(['self.gl = new OffscreenCanvas(8, 8).getContext("webgl2"); postMessage("ok");'])));
    obrero.onmessage = () => parent.postMessage('listo', '*');
  </script></body></html>`;
  const CUERPO_VENTANA_EMERGENTE = `
    window.open('about:blank');
    srfListo();`;
  const CUERPO_MARCO_SIN_INTERCEPTOR = `
    const marco = document.createElement('iframe');
    marco.srcdoc = '<p>a frame whose creation interceptor is removed by the page</p>';
    marco.onload = () => { marco.contentWindow.__srfMedicion = undefined; srfListo(); };
    document.body.appendChild(marco);`;
  const CUERPO_ERROR_TARDIO = `
    srfListo();
    setTimeout(() => { throw new Error('late boom'); }, 300);`;
  const CUERPO_TRABAJADOR_QUE_MUERE = `
    const obrero = new Worker(URL.createObjectURL(new Blob(['self.gl = new OffscreenCanvas(8, 8).getContext("webgl2"); postMessage("hecho"); self.close();'])));
    obrero.onmessage = () => setTimeout(srfListo, 200);`;

  // ---- H-1: every failure path writes the report --------------------------------------------------------------
  describe('the instrument writes its report on every failure path (H-1)', () => {
    /** A failed report: ok false, the stage, the error, the exit code echoed, and every measured field null. */
    function esperarFallo(m: Medicion, rc: number, etapa: string, texto: string): void {
      expect(m.rc, m.stderr).toBe(rc);
      expect(m.informe.ok).toBe(false);
      expect(m.informe.etapa).toBe(etapa);
      expect(m.informe.rc).toBe(rc);
      expect(String(m.informe.error)).toContain(texto);
      for (const campo of CAMPOS_MEDIDOS) expect(m.informe[campo], campo).toBeNull();
      expect(m.stderr).toContain(texto);
    }

    it('a usage error: rc 2, etapa argumentos, one JSON line on stdout', async () => {
      const m = await medir(['--json']);
      esperarFallo(m, 2, 'argumentos', '--modo');
      expect(m.stdout.trim().split('\n')).toHaveLength(1);
      expect(m.informe.modo).toBeNull();
      const modoMalo = await medir(['--modo', '1/2', '--json']);
      esperarFallo(modoMalo, 2, 'argumentos', 'unknown mode');
    });

    it('without --json the same report is written, indented', async () => {
      const m = await medir(['--modo', '1/2']);
      esperarFallo(m, 2, 'argumentos', 'unknown mode');
      expect(m.stdout.trim().split('\n').length).toBeGreaterThan(1);
    });

    it('the dist directory is missing: rc 2, etapa precondicion', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', join(TMP, 'no-existe')]);
      esperarFallo(m, 2, 'precondicion', 'index.html not found');
      expect(m.informe.ventana).toBe('1280x1064');
      expect(m.informe.dom_solicitado).toBe('1271x1064');
    });

    it('a page without the synchronous bootstrap block is refused, not measured: rc 2', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('sin-arranque', 'srfListo();', false)]);
      esperarFallo(m, 2, 'precondicion', 'synchronous bootstrap block');
    });

    it('the browser is missing: rc 1, etapa navegador, and the report still names what was known', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('vacia', 'srfListo();')], { PLAYWRIGHT_BROWSERS_PATH: join(TMP, 'sin-navegadores') });
      esperarFallo(m, 1, 'navegador', 'could not be launched');
      expect(m.informe.navegador).toEqual({ chromium: null, playwright: '1.58.0' });
    });

    it('the page throws before it is ready: rc 4, etapa listo, the error is in the report', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--espera-listo-ms', '8000', '--dist', fixtura('lanza', "throw new Error('fixture boom');")]);
      esperarFallo(m, 4, 'listo', 'fixture boom');
      expect(m.informe.errores_de_pagina.join(' ')).toContain('fixture boom');
      expect(m.informe.navegador.chromium).not.toBeNull();
    });

    it('the page never reaches its ready contract: rc 4, etapa listo', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--espera-listo-ms', '1500', '--dist', fixtura('nunca-listo', '/* never calls srfListo */')]);
      esperarFallo(m, 4, 'listo', 'never set data-app-ready');
    });

    it('a page that throws after it is ready still fails the run: rc 4, etapa lectura', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('error-tardio', CUERPO_ERROR_TARDIO)]);
      esperarFallo(m, 4, 'lectura', 'late boom');
      expect(m.informe.errores_de_pagina.join(' ')).toContain('late boom');
    });

    it('two sources of one field disagree: rc 3, the report is on stdout, the field is null and both readings are kept', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('desacuerdo', CUERPO_TRABAJADOR_QUE_MUERE)]);
      expect(m.rc, m.stderr).toBe(3);
      expect(m.informe.ok).toBe(false);
      expect(m.informe.etapa).toBe('acuerdo_entre_fuentes');
      expect(m.informe.acuerdo_entre_fuentes).toBe(false);
      expect(m.informe.campos_en_desacuerdo).toEqual(['contextos_webgl']);
      expect(m.informe.contextos_webgl).toBeNull();
      expect(m.informe.primera_fuente.contextos_webgl).toBe(0);
      expect(m.informe.segunda_fuente.contextos_webgl_gpu).toBe(1);
      expect(m.informe.desacuerdos.join(' ')).toContain('GPU service 1');
      expect(m.informe.dom).toBe('1271x1064'); // the fields that agree keep their value
    });

    it('every failure path has exactly the shape of a good run', async () => {
      const bueno = forma((await real('1/6')).informe);
      const caminos: Array<Promise<Medicion>> = [
        medir(['--json']),
        medir(['--modo', '1/6', '--json', '--dist', join(TMP, 'no-existe')]),
        medir(['--modo', '1/6', '--json', '--dist', fixtura('vacia', 'srfListo();')], { PLAYWRIGHT_BROWSERS_PATH: join(TMP, 'sin-navegadores') }),
        medir(['--modo', '1/6', '--json', '--espera-listo-ms', '8000', '--dist', fixtura('lanza', "throw new Error('fixture boom');")]),
        medir(['--modo', '1/6', '--json', '--dist', fixtura('desacuerdo', CUERPO_TRABAJADOR_QUE_MUERE)]),
      ];
      for (const m of await Promise.all(caminos)) expect(forma(m.informe), m.informe.etapa).toEqual(bueno);
    });
  });

  // ---- H-2: WebGL contexts in every realm ---------------------------------------------------------------------
  describe('WebGL contexts are counted in the main frame, every child frame and every dedicated worker (H-2)', () => {
    it('reads the GPU-service source from the trace: one distinct WebGL-0x<pointer> name per context', () => {
      const gpu = 'disabled-by-default-gpu.service';
      const eventos = [
        { cat: gpu, name: 'WebGL-0x100', ph: 'b' },
        { cat: gpu, name: 'WebGL-0x100', ph: 'e' },
        { cat: gpu, name: 'WebGL-0x2a0', ph: 'b' },
        { cat: gpu, name: 'GPUTask-0x7', ph: 'b' }, // a GPU event that is not a WebGL context
        { cat: 'gpu', name: 'WebGL-0x999', ph: 'X' }, // another category: ignored
        { cat: `${gpu},toplevel`, name: 'WebGL-0x300', ph: 'b' },
      ];
      expect(contextosDeTraza(eventos)).toEqual({ contextos: 3, eventos_gpu: 5, nombres: ['WebGL-0x100', 'WebGL-0x2a0', 'WebGL-0x300'] });
      expect(contextosDeTraza([])).toEqual({ contextos: 0, eventos_gpu: 0, nombres: [] });
    });

    it('the real app creates EXACTLY one context, and both sources say so (<= 1 alone would accept zero)', async () => {
      const { informe: i, stdout } = await real('1/6');
      expect(i.ok).toBe(true);
      expect(i.contextos_webgl).toBe(1);
      expect(i.webgl.fuentes.en_los_reinos).toEqual({ marcos: 1, trabajadores: 0, total: 1 });
      expect(i.webgl.fuentes.servicio_gpu.contextos).toBe(1);
      expect(i.estado_de_la_pagina.gpu).toBe('ok'); // the app's own data-gpu: the context was opened and painted
      expect(i.webgl.alcance).toMatchObject({ paginas: 1, marcos: 1, marcos_con_interceptor: 1, trabajadores_dedicados_cdp: 0, trabajadores_dedicados_playwright: 0 });
      expect(jq('.contextos_webgl<=1', stdout)).toBe(0);
    });

    it('a context in an iframe and one in a worker turn the gate red: 3 contexts, both sources agree', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('iframe-y-trabajador', CUERPO_IFRAME_Y_TRABAJADOR)]);
      const i = m.informe;
      expect(m.rc, m.stderr).toBe(0);
      expect(i.contextos_webgl).toBe(3);
      expect(i.webgl.fuentes.en_los_reinos).toEqual({ marcos: 2, trabajadores: 1, total: 3 });
      expect(i.webgl.fuentes.servicio_gpu.contextos).toBe(3);
      expect(i.webgl.marcos.map((f: Informe) => f.contextos)).toEqual([1, 1]);
      expect(i.webgl.marcos[1].principal).toBe(false);
      expect(i.webgl.trabajadores).toHaveLength(1);
      expect(i.webgl.alcance).toMatchObject({ marcos: 2, marcos_con_interceptor: 2, trabajadores_dedicados_cdp: 1, trabajadores_dedicados_playwright: 1 });
      expect(jq('.contextos_webgl<=1', m.stdout)).toBe(1); // the oracle's own filter goes red
    });

    it('the scope checks name what the in-realm source could not cover', () => {
      const marco = { url: 'http://127.0.0.1/', principal: true, instalado: true, contextos: 0, perdidos: 0 };
      const base = { marcos: [marco], trabajadores: [], trabajadoresCdp: 0, trabajadoresPlaywright: 0, paginas: 1 };
      expect(desacuerdosDeAlcance(base)).toEqual([]);
      expect(desacuerdosDeAlcance({ ...base, marcos: [marco, { ...marco, url: 'about:srcdoc', principal: false, instalado: false }] })).toEqual(['webgl scope: 1 frame(s) without the creation interceptor: about:srcdoc']);
      expect(desacuerdosDeAlcance({ ...base, trabajadores: [{ url: 'blob:w', total: null, error: 'gone' }], trabajadoresCdp: 1, trabajadoresPlaywright: 1 })).toEqual(['webgl scope: a dedicated worker could not be read (blob:w): gone']);
      expect(desacuerdosDeAlcance({ ...base, trabajadoresCdp: 1, trabajadoresPlaywright: 2 })).toEqual(['webgl scope: dedicated workers attached over CDP 1 vs listed by Playwright 2']);
      expect(desacuerdosDeAlcance({ ...base, paginas: 2 })).toEqual(['webgl scope: 2 pages are open in the browser context, only the measured one is read in-realm']);
    });

    it('a context in a cross-origin iframe and one in that iframe worker are counted, by both sources', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('otro-origen', CUERPO_MARCO_OTRO_ORIGEN, true, { 'hijo.html': HIJO_OTRO_ORIGEN })]);
      expect(m.rc, m.stderr).toBe(0);
      expect(m.informe.contextos_webgl).toBe(2);
      expect(m.informe.webgl.marcos[1].url).toContain('localhost');
      expect(m.informe.webgl.fuentes.en_los_reinos).toEqual({ marcos: 1, trabajadores: 1, total: 2 });
      expect(m.informe.webgl.fuentes.servicio_gpu.contextos).toBe(2);
    });

    it('a second page opened in the browser context is a scope the in-realm source does not read: the run is red', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('ventana-emergente', CUERPO_VENTANA_EMERGENTE)]);
      expect(m.rc, m.stderr).toBe(3);
      expect(m.informe.campos_en_desacuerdo).toContain('contextos_webgl');
      expect(m.informe.contextos_webgl).toBeNull();
      expect(m.informe.webgl.alcance.paginas).toBe(2);
      expect(m.informe.desacuerdos.join(' ')).toContain('2 pages are open');
    });

    it('a frame whose creation interceptor is gone is reported, not assumed to be empty', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('marco-sin-interceptor', CUERPO_MARCO_SIN_INTERCEPTOR)]);
      expect(m.rc, m.stderr).toBe(3);
      expect(m.informe.webgl.alcance).toMatchObject({ marcos: 2, marcos_con_interceptor: 1 });
      expect(m.informe.desacuerdos.join(' ')).toContain('without the creation interceptor');
      expect(m.informe.contextos_webgl).toBeNull();
    });

    it('a nested dedicated worker is counted too', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('anidado', CUERPO_TRABAJADOR_ANIDADO)]);
      expect(m.rc, m.stderr).toBe(0);
      expect(m.informe.contextos_webgl).toBe(2);
      expect(m.informe.webgl.trabajadores.map((w: Informe) => w.anidado).sort()).toEqual([false, true]);
      expect(m.informe.webgl.alcance).toMatchObject({ trabajadores_dedicados_cdp: 2, trabajadores_dedicados_playwright: 2 });
      expect(m.informe.webgl.fuentes.servicio_gpu.contextos).toBe(2);
    });

    it('two contexts on detached canvases of the main frame are two contexts', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('dos-en-el-marco', CUERPO_DOS_EN_EL_MARCO)]);
      expect(m.rc, m.stderr).toBe(0);
      expect(m.informe.contextos_webgl).toBe(2);
      expect(jq('.contextos_webgl<=1', m.stdout)).toBe(1);
    });

    it('getContext twice on one canvas returns the same context: it is counted once', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('mismo-lienzo', CUERPO_MISMO_LIENZO_DOS_VECES)]);
      expect(m.rc, m.stderr).toBe(0);
      expect(m.informe.contextos_webgl).toBe(1);
      expect(m.informe.webgl.fuentes.servicio_gpu.contextos).toBe(1);
    });

    it('a page with no context reports 0, which the real-app test above refuses', async () => {
      const m = await medir(['--modo', '1/6', '--json', '--dist', fixtura('sin-contexto', 'srfListo();')]);
      expect(m.rc, m.stderr).toBe(0);
      expect(m.informe.contextos_webgl).toBe(0);
      expect(m.informe.webgl.fuentes.servicio_gpu.contextos).toBe(0);
    });
  });

  // ---- H-3 and H-4: provenance, and the layout the page produces ----------------------------------------------
  describe('provenance and layout of the good run (H-3, H-4)', () => {
    it('labels dom as a READ-BACK of the emulated viewport and never as measured; the real window is untested', async () => {
      const { informe: i } = await real('1/6');
      expect(i.procedencia.dom).toMatch(/^READ-BACK/);
      expect(i.procedencia.dom).not.toMatch(/^MEASURED/);
      expect(i.procedencia.ventana).toMatch(/^DECLARED/);
      expect(i.procedencia.dom_solicitado).toMatch(/^DECLARED/);
      expect(i.procedencia.lienzo).toMatch(/^MEASURED layout/);
      expect(i.procedencia.desplazamiento).toMatch(/^MEASURED layout/);
      expect(i.procedencia.ventana_wails_real).toMatch(/^UNTESTED/);
      expect(i.no_probado.join(' ')).toContain('Wails');
      // the read-back is the value that was asked for, and the report says so by publishing both
      expect(i.dom).toBe(i.dom_solicitado);
      expect(i.ventana).toBe(`${VENTANA_W}x${VENTANA_H}`);
    });

    it('the canvas frame sits at 3x40 with the size geometria derives, the GL canvas on top of it, and nothing scrolls (1/6)', async () => {
      const { informe: i } = await real('1/6');
      expect(i.dom).toBe(`${DOM_W}x${DOM_H_SEXTO}`);
      expect(i.lienzo).toBe(`${LIENZO_W}x${LIENZO_H}`);
      expect(i.lienzo_origen).toBe(`${GUTTER_IZQ}x${BARRA_H}`); // 3x40
      expect(i.segunda_fuente.lienzo_origen_cdp).toBe(`${GUTTER_IZQ}x${BARRA_H}`);
      expect(i.segunda_fuente.lienzo_origen_pixeles).toBe(`${GUTTER_IZQ}x${BARRA_H}`);
      expect(i.gl_lienzo.origen).toBe(`${GUTTER_IZQ}x${BARRA_H}`);
      expect(`${i.gl_lienzo.w}x${i.gl_lienzo.h}`).toBe(`${LIENZO_W}x${LIENZO_H}`);
      expect(i.desplazamiento).toBe(`${DOM_W}x${DOM_H_SEXTO}`);
      expect(i.estado_de_la_pagina.modo).toBe(etiquetaModo(modoPorAltura(DOM_H_SEXTO)));
    });

    it('the same holds in mode 1/3 (tall canvas, 8 px of vertical slack)', async () => {
      const { informe: i } = await real('1/3');
      expect(i.dom).toBe(`${DOM_W}x${DOM_H_TERCIO}`);
      expect(i.lienzo).toBe(`${LIENZO_W}x${LIENZO_H_ALTO}`);
      expect(i.lienzo_origen).toBe(`${GUTTER_IZQ}x${BARRA_H}`);
      expect(i.gl_lienzo.origen).toBe(`${GUTTER_IZQ}x${BARRA_H}`);
      expect(i.desplazamiento).toBe(`${DOM_W}x${DOM_H_TERCIO}`);
      expect(i.contextos_webgl).toBe(1);
      // The bootstrap decides the mode before first paint; hydration would repair a wrong decision, but only as a shift.
      expect(i.estado_de_la_pagina.modo).toBe(etiquetaModo(modoPorAltura(DOM_H_TERCIO)));
      expect(i.cls).toBe(0);
      expect(i.entradas_contadas).toBe(0);
      expect(i.segunda_fuente.cls_traza_chromium).toBe(0);
    });

    it('app.css places the canvas frame and the GL canvas by the gutter and the bar tokens', () => {
      const bloque = (selector: string): string => new RegExp(`(?:^|\\n)${selector.replace('.', '\\.')}\\s*\\{([^}]*)\\}`).exec(CSS)?.[1] ?? '';
      expect(bloque('.lienzo')).toMatch(/margin-left:\s*var\(--gutter-left\)/);
      expect(bloque('.gl')).toMatch(/top:\s*var\(--bar-h\)/);
      expect(bloque('.gl')).toMatch(/left:\s*var\(--gutter-left\)/);
    });
  });

  // ---- the contrast and its mutants (H-5, H-7) ----------------------------------------------------------------
  describe('the synchronous bootstrap is what keeps the layout still (contrast, H-5, H-7)', () => {
    it('with the bootstrap the seeded non-default preference shifts nothing: CLS 0 from both sources', async () => {
      const { informe: i, stdout } = await real('1/6');
      expect(i.arranque_sincrono_presente_en_la_pagina).toBe(true);
      expect(i.estado_de_la_pagina).toMatchObject({ tema: 'alto-contraste', escalaCss: '1.25', tamanoRaizPx: 20, arranque: 'sincrono' });
      expect(i.cls).toBe(0);
      expect(i.entradas_contadas).toBe(0);
      expect(i.segunda_fuente.cls_traza_chromium).toBe(0);
      expect(i.segunda_fuente.entradas_traza_chromium).toBe(0);
      expect(jq('.cls==0', stdout)).toBe(0);
    });

    it('without it the browser itself reports a layout shift, and the trace source reads the same one', async () => {
      const m = await real('1/6', ['--sin-arranque-sincrono']);
      const i = m.informe;
      expect(m.rc, m.stderr).toBe(0);
      expect(i.ok).toBe(true);
      expect(i.arranque_sincrono_presente_en_la_pagina).toBe(false);
      expect(i.cls).toBeGreaterThan(0);
      expect(i.entradas_contadas).toBeGreaterThanOrEqual(1);
      expect(i.segunda_fuente.cls_traza_chromium).toBeCloseTo(i.cls, 12);
      expect(i.segunda_fuente.entradas_traza_chromium).toBe(i.entradas_contadas);
      expect(i.desplazamientos.length).toBeGreaterThanOrEqual(1);
      expect(i.desplazamientos[0].movidos.length).toBeGreaterThanOrEqual(1);
      expect(i.desplazamientos[0].movidos.map((n: Informe) => n.nodo.etiqueta)).toContain('span');
      // the red of the contrast depends on the injected latency, and the report discloses it
      expect(i.latencia_js_ms).toBe(300);
      expect(jq('.cls==0', m.stdout)).toBe(1);
    });

    it('a bootstrap that is present but no longer applies the font scale still shifts (the partial-bootstrap mutant)', async () => {
      const dir = join(TMP, 'dist-parcial');
      cpSync(DIST, dir, { recursive: true });
      const original = readFileSync(join(dir, 'index.html'), 'utf8');
      const mutado = original.replace(/^.*raiz\.style\.setProperty\('--escala', String\(escala\)\);\n/m, '');
      expect(mutado).not.toBe(original); // the mutation target exists
      writeFileSync(join(dir, 'index.html'), mutado);
      const m = await medir(['--modo', '1/6', '--json', '--dist', dir]);
      expect(m.rc, m.stderr).toBe(0);
      expect(m.informe.arranque_sincrono_presente_en_la_pagina).toBe(true);
      expect(m.informe.cls).toBeGreaterThan(0);
      expect(jq('.cls==0', m.stdout)).toBe(1);
    });
  });

  // ---- H-6: the geometry literals live in geometria.ts only ---------------------------------------------------
  describe('geometry literals outside geometria.ts (H-6)', () => {
    const LITERAL = /(?<![0-9.])(1271|1264|1024|2080|2128|1064|1280|2000)(?![0-9])/g;
    // The mirrors that are allowed to exist, each guarded by a test above: app.css tokens (declares every geometry token),
    // the bootstrap threshold in index.html (agrees with resolverPreferencias), the specification's literal types in
    // hypervisor.ts, and the Go window constants (opens the window at the 1/6 tile size).
    const ESPEJOS: Record<string, Record<string, number>> = {
      'cockpit/frontend/src/app.css': { '1024': 1, '1264': 1, '1271': 1, '1280': 1, '2080': 2 },
      'cockpit/frontend/src/lib/types/hypervisor.ts': { '1024': 1, '1064': 1, '1264': 2, '1271': 1, '2080': 1, '2128': 1 },
      'cockpit/frontend/index.html': { '2000': 1 },
      'cockpit/main.go': { '1064': 1, '1280': 1 },
    };

    function ficheros(dir: string): string[] {
      return readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? ficheros(join(dir, e.name)) : [join(dir, e.name)]));
    }

    function censo(): Record<string, Record<string, number>> {
      const raiz = (r: string): string => join(RAIZ_REPO, r);
      const candidatos = [...ficheros(raiz('cockpit/frontend/src')), raiz('cockpit/frontend/index.html'), raiz('cockpit/frontend/playwright.config.ts'), raiz('cockpit/frontend/vite.config.ts'), raiz('cockpit/main.go'), raiz('cockpit/app.go')];
      const salida: Record<string, Record<string, number>> = {};
      for (const ruta of candidatos) {
        const rel = relative(RAIZ_REPO, ruta);
        if (rel === 'cockpit/frontend/src/lib/geometria.ts') continue; // the one place that may derive them
        const conteo: Record<string, number> = {};
        for (const m of readFileSync(ruta, 'utf8').matchAll(LITERAL)) conteo[m[1]] = (conteo[m[1]] ?? 0) + 1;
        if (Object.keys(conteo).length > 0) salida[rel] = conteo;
      }
      return salida;
    }

    it('no component, script or config repeats a geometry figure beyond the guarded mirrors', () => {
      expect(censo()).toEqual(ESPEJOS);
    });

    it('playwright.config.ts takes its viewport from geometria.ts and carries no figure of its own', () => {
      const config = leer('../playwright.config.ts');
      expect(config).toMatch(/from '\.\/src\/lib\/geometria'/);
      expect(config).toMatch(/viewport:\s*\{\s*width:\s*DOM_W,\s*height:\s*DOM_H_SEXTO\s*\}/);
      expect(config.match(LITERAL)).toBeNull();
    });
  });

  // ---- H-4: the detener path of the srfd client ---------------------------------------------------------------
  describe('srfd client detener forwards the slot and the class (H-4)', () => {
    it('sends verb, class and slot to the transport, and an ok answer without data is a success', async () => {
      const enviado: Array<[string, string, number]> = [];
      const c = crearClienteSrfd({
        enviar: async (comando, clase, slot) => {
          enviado.push([comando, clase, slot]);
          return { ok: true, rc: 0 };
        },
      });
      await expect(c.detener(7, 'browser')).resolves.toBeUndefined();
      await c.detener(65535);
      await c.detener(0, 'microvm');
      expect(enviado).toEqual([
        ['detener', 'browser', 7],
        ['detener', '', 65535],
        ['detener', 'microvm', 0],
      ]);
    });

    it('a refusal of detener is a SrfdError with srfd exit code, not a silent success', async () => {
      const c = crearClienteSrfd({ enviar: async () => ({ ok: false, rc: RC_PARADA, error: 'stop signal is effective' }) });
      const e = (await c.detener(3).then(() => null, (x: unknown) => x)) as SrfdError;
      expect(e).toBeInstanceOf(SrfdError);
      expect(e.codigo).toBe('rechazado');
      expect(e.rc).toBe(RC_PARADA);
    });
  });

  // ---- H-4: cockpit/app.go against a fake srfd, compiled through go test -overlay ------------------------------
  describe('cockpit/app.go against a fake srfd on a real unix socket (H-4)', () => {
    const PRUEBAS_GO = [
      'TestEnviarOK',
      'TestEnviarListarVacioEsUnaListaVacia',
      'TestEnviarRechazoPasaSinReescribir',
      'TestEnviarSinSocket',
      'TestEnviarLineaBasura',
      'TestEnviarCierreSinRespuesta',
      'TestEnviarPayloadNoDecodificable',
      'TestEnviarDetenerReenviaClaseYSlot',
      'TestEnviarIniciarReenviaClaseYSlot',
      'TestPlazosSonLosDelClienteSrf',
      'TestEnviarUsaElPlazoDelComando',
      'TestSocketEsElDelCliente',
    ];
    let ejecucion: Promise<Proceso & { veredictos: Map<string, string> }> | null = null;

    // U-18's writes allowlist has no slot for cockpit/app_test.go: the Go tests live in evidencia/v2/u18/app_test.go.txt and are
    // added to the real package by go test -overlay. Nothing is written into cockpit/, and the REAL app.go is what compiles.
    function lanzarGo(): Promise<Proceso & { veredictos: Map<string, string> }> {
      if (ejecucion === null) {
        // main.go embeds frontend/dist: the overlay also carries a placeholder file there, so the package compiles whether or
        // not a build has run, and still nothing is written into the tree.
        const overlay = join(TMP, 'overlay-go.json');
        const relleno = join(TMP, 'relleno-embed.txt');
        writeFileSync(relleno, 'placeholder so that //go:embed all:frontend/dist matches without a build\n');
        writeFileSync(
          overlay,
          JSON.stringify({ Replace: { [join(RAIZ_REPO, 'cockpit', 'app_test.go')]: FUENTE_PRUEBAS_GO, [join(RAIZ_REPO, 'cockpit', 'frontend', 'dist', '.relleno-overlay')]: relleno } }),
        );
        ejecucion = ejecutarProceso('go', ['test', '-count=1', '-json', '-tags', 'cockpit webkit2_41', '-overlay', overlay, './...'], {
          cwd: join(RAIZ_REPO, 'cockpit'),
          env: { ...process.env, CGO_ENABLED: '1', GOPROXY: 'off', GOFLAGS: '', GOTOOLCHAIN: 'local' },
          timeout: 280_000,
        }).then((p) => {
          const veredictos = new Map<string, string>();
          for (const linea of p.stdout.split('\n')) {
            if (!linea.startsWith('{')) continue;
            try {
              const e = JSON.parse(linea) as { Action?: string; Test?: string };
              if (e.Test !== undefined && (e.Action === 'pass' || e.Action === 'fail' || e.Action === 'skip')) veredictos.set(e.Test, e.Action);
            } catch {
              /* a non-JSON line of the toolchain: not a verdict */
            }
          }
          return { ...p, veredictos };
        });
      }
      return ejecucion;
    }

    for (const nombre of PRUEBAS_GO) {
      it(`go test ${nombre} passes`, async () => {
        const r = await lanzarGo();
        expect(r.veredictos.get(nombre), `go test -json output:\n${r.stdout}\n${r.stderr}`).toBe('pass');
      });
    }

    it('the whole Go package passes and no test of the list was skipped or renamed away', async () => {
      const r = await lanzarGo();
      expect(r.rc, `go test -json output:\n${r.stdout}\n${r.stderr}`).toBe(0);
      expect([...r.veredictos.values()].filter((v) => v === 'fail')).toEqual([]);
      for (const nombre of PRUEBAS_GO) expect(r.veredictos.has(nombre), nombre).toBe(true);
    });
  });
});
