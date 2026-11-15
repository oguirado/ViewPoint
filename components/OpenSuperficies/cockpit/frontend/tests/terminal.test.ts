// Tests of the agents drawer (U-21): the guard and policy of src/lib/terminal/termguard.ts, the SRF2 codec, the flow
// control queue and the stream client of wsstream.ts (the stream against a real WebSocket server), the shape of
// TerminalPanel.svelte, the pure parts of scripts/ng/medir-terminal.mjs, and the drawer in a real headless Chromium
// through that instrument. Every expectation comes from a source OTHER than the code under test (the specification's
// text, app.css, an independent SRF2 codec on the server side, a second browser observation), and every test names a
// production symbol. evidencia/v2/u21/mutaciones-2.txt records, for each test, the production line that was neutered
// in a scratch copy and the red it produced (mutaciones-1.txt is attempt 1's).

import { execFile, spawn, spawnSync } from 'node:child_process';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LIENZO_H, LIENZO_W } from '../src/lib/geometria';
import {
  ALTURA_ASA,
  ALTURA_CABECERA,
  ALTURA_CAJON,
  PANELES_INTERACTIVOS_MAX,
  PASOS_DE_FOCO_MS,
  SCROLLBACK_MAX,
  cargarAddon,
  clasificarAddon,
  crearAddonsPropios,
  crearGuardia,
  decidirRenderer,
  leerRenderer,
  limitarScrollback,
  modoDeInstancia,
  olvidarRechazos,
  opcionesDeTerminal,
  programarFoco,
  protegerTerminal,
  quitarAnsi,
  rechazosDeGuarda,
  vigilarCapas,
} from '../src/lib/terminal/termguard';
import {
  ALTA_MARCA_BYTES,
  ColaDeEscritura,
  ESPERA_MAX_MS,
  ESTABLE_MS,
  ErrorDeTrama,
  FlujoWs,
  LOTE_MAX_BYTES,
  TAM_CABECERA,
  codificarTrama,
  decodificarTrama,
} from '../src/lib/terminal/wsstream';
import type { OpcionesDeFlujo } from '../src/lib/terminal/wsstream';
import {
  ALTURA_ASA_DECLARADA,
  ALTURA_CAJON_DECLARADA,
  LOTE_MAX_DECLARADO,
  PASOS_DE_FOCO_DECLARADOS,
  SCROLLBACK_DECLARADO,
  codificarTramaSrf2,
  compararMontajes,
  crearServidorPty,
  decidirRendererLeido,
  decodificarTramaSrf2,
  desacuerdosDeAddons,
  informeVacio,
  leerArgumentos,
  procedenciaDeTramos,
  remontajesDe,
  rendererDeConjunto,
  resumirScrollback,
  retencionDeBuffer,
} from '../../../scripts/ng/medir-terminal.mjs';

const RAIZ_REPO = fileURLToPath(new URL('../../../', import.meta.url));
const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');
const ESPEC = leer('../../../ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md');
const APP_CSS = leer('../src/app.css');
const PANEL = leer('../src/lib/terminal/TerminalPanel.svelte');
const CAJON = leer('../src/lib/terminal/CajonAgentes.svelte');
const DIR_TERMINAL = fileURLToPath(new URL('../src/lib/terminal/', import.meta.url));
const FUENTES_TERMINAL = readdirSync(DIR_TERMINAL).map((n) => ({ nombre: n, texto: readFileSync(join(DIR_TERMINAL, n), 'utf8') }));

const dormir = (ms: number): Promise<void> => new Promise((ok) => setTimeout(ok, ms));
async function esperarHasta(cond: () => boolean, ms = 5_000, que = 'the condition'): Promise<void> {
  const limite = Date.now() + ms;
  while (Date.now() < limite) {
    if (cond()) return;
    await dormir(10);
  }
  throw new Error(`timed out after ${ms} ms waiting for ${que}`);
}

// ---- termguard.ts: the constants are the specification's ---------------------------------------------------------
describe('the figures of the drawer (termguard.ts) against the specification and app.css', () => {
  it('holds the specification\'s scrollback, panel red line, drawer heights and focus passes', () => {
    // read from the specification's text and from app.css, not from the constants under test
    const espec6 = /## 6\. CLI Agent Terminal Drawer([\s\S]*?)\n## 7\./.exec(ESPEC)![1];
    const scroll = /Scrollback (\d),(\d{3}) lines/.exec(espec6)!;
    expect(SCROLLBACK_MAX).toBe(Number(scroll[1] + scroll[2]));
    expect(PANELES_INTERACTIVOS_MAX).toBe(Number(/beyond (\d+) interactive instances/.exec(espec6)![1]));
    expect(ALTURA_CAJON).toBe(Number(/\*\*(\d+) px\*\* overlay/.exec(espec6)![1]));
    expect(ALTURA_ASA).toBe(Number(/(\d+) px grab handle/.exec(espec6)![1]));
    expect(ALTURA_CAJON).toBe(Number(/--drawer-h:\s*(\d+)px/.exec(APP_CSS)![1]));
    expect(ALTURA_CABECERA).toBe(Number(/--overlay-head-h:\s*(\d+)px/.exec(APP_CSS)![1]));
    const foco = /three-pass focus transfer \((\d+) \/ (\d+) \/ (\d+) ms\)/.exec(ESPEC)!;
    expect([...PASOS_DE_FOCO_MS]).toEqual([Number(foco[1]), Number(foco[2]), Number(foco[3])]);
    // and the instrument declares the same figures on its own
    expect(ALTURA_CAJON_DECLARADA).toBe(ALTURA_CAJON);
    expect(ALTURA_ASA_DECLARADA).toBe(ALTURA_ASA);
    expect(SCROLLBACK_DECLARADO).toBe(SCROLLBACK_MAX);
    expect(PASOS_DE_FOCO_DECLARADOS).toEqual([...PASOS_DE_FOCO_MS]);
  });
});

describe('the renderer policy (termguard.ts decidirRenderer)', () => {
  it('grants the DOM renderer and refuses every other, whatever is asked', () => {
    expect(decidirRenderer('dom')).toEqual({ pedido: 'dom', concedido: 'dom', rechazado: false, motivo: '' });
    for (const pedido of ['canvas', 'webgl', 'CanvasAddon', 'gpu', '']) {
      const d = decidirRenderer(pedido);
      expect(d.concedido, pedido).toBe('dom');
      expect(d.rechazado, pedido).toBe(true);
      expect(d.motivo, pedido).not.toBe('');
    }
    expect(decidirRenderer('canvas').motivo).toMatch(/forbidden/);
    expect(decidirRenderer('webgl').motivo).toMatch(/forbidden/);
    expect(decidirRenderer('gpu').motivo).toMatch(/does not exist/);
  });
});

describe('the addon gate (termguard.ts): deny by default, whatever an addon is called', () => {
  // Stand-ins shaped like the published, MINIFIED renderer addons: their classes are called `a`, `l` and `p`, so no
  // class name identifies them. NONE of the names below is on any deny-list; the last one is the CONTROL, named as the
  // real class (the only kind of fixture attempt 1 had, which proved nothing about a minified bundle).
  // The real @xterm/addon-canvas 0.7.0 and @xterm/addon-webgl 0.19.0 bundles are NOT installed here: none of these is one.
  class a {
    activate(): void {}
    dispose(): void {}
  }
  class p {
    activate(): void {}
    dispose(): void {}
  }
  class l extends p {}
  const anonima = [class { activate(): void {} dispose(): void {} }][0];
  class Marcador extends Map<string, string> {
    activate(): void {}
    dispose(): void {}
  }
  class Interno {
    activate(): void {}
    dispose(): void {}
  }
  const ExportadoConOtroNombre = Interno; // a renamed export: the class keeps the name it was born with
  class CanvasAddon {
    activate(): void {}
    dispose(): void {}
  }
  const FIXTURES: Array<{ etiqueta: string; crear: () => object; nombre: string; control?: boolean }> = [
    { etiqueta: 'single letter, as addon-canvas is minified', crear: () => new a(), nombre: 'a' },
    { etiqueta: 'single letters in a chain, as addon-webgl is minified (l extends p)', crear: () => new l(), nombre: 'l' },
    { etiqueta: 'anonymous class', crear: () => new anonima(), nombre: '(anonymous)' },
    { etiqueta: 'subclass of an unrelated built-in', crear: () => new Marcador(), nombre: 'Marcador' },
    { etiqueta: 'renamed export', crear: () => new ExportadoConOtroNombre(), nombre: 'Interno' },
    { etiqueta: 'object literal with no class at all', crear: () => ({ activate() {}, dispose() {} }), nombre: 'Object' },
    { etiqueta: 'CONTROL: literally named CanvasAddon', crear: () => new CanvasAddon(), nombre: 'CanvasAddon', control: true },
  ];

  it('refuses every addon the drawer did not create, whatever its class is called, and never hands it to loadAddon', () => {
    olvidarRechazos();
    const aviso = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const cargados: unknown[] = [];
      const term = { loadAddon: (x: unknown) => void cargados.push(x) };
      for (const f of FIXTURES) {
        expect(clasificarAddon(f.crear()), f.etiqueta).toBe('ajeno');
        expect(cargarAddon(term, f.crear() as never), f.etiqueta).toMatchObject({ cargado: false }); // by identity, not by name
      }
      expect(cargados).toEqual([]); // not one of them reached loadAddon
      expect(rechazosDeGuarda().map((r) => r.addon)).toEqual(FIXTURES.map((f) => f.nombre)); // logged by the name each carries
      expect(rechazosDeGuarda()[0].motivo).toMatch(/deny by default/);
      expect(aviso.mock.calls.map((c) => String(c[0]))).toEqual(FIXTURES.map((f) => expect.stringMatching(new RegExp(`^termguard: refused ${f.nombre.replace(/[()]/g, '\\$&')}: `))));
      // the disguised fixtures are refused for the same reason as the control: none is one the drawer made
      expect(FIXTURES.filter((f) => f.control !== true).every((f) => !/Canvas|Webgl/i.test(f.nombre))).toBe(true);
    } finally {
      aviso.mockRestore();
      olvidarRechazos();
    }
  });

  it('a guarded terminal refuses a direct loadAddon of any addon, and the guard cannot be unset', () => {
    olvidarRechazos();
    const aviso = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const cargados: unknown[] = [];
      const term = { loadAddon: (x: unknown) => void cargados.push(x) };
      expect(protegerTerminal(term)).toBe(term);
      for (const f of FIXTURES) term.loadAddon(f.crear()); // the call site of a bypass: no cargarAddon() in between
      expect(cargados).toEqual([]);
      expect(rechazosDeGuarda()).toHaveLength(FIXTURES.length);
      const d = Object.getOwnPropertyDescriptor(term, 'loadAddon')!;
      expect(d.writable).toBe(false);
      expect(d.configurable).toBe(false);
      expect(() => {
        (term as { loadAddon: unknown }).loadAddon = (x: unknown) => void cargados.push(x);
      }).toThrow(TypeError);
      expect(() => Object.defineProperty(term, 'loadAddon', { value: () => {} })).toThrow(TypeError);
    } finally {
      aviso.mockRestore();
      olvidarRechazos();
    }
  });

  it('the drawer\'s allow-list is empty, and a guard over a factory list loads exactly the instances its factories made', () => {
    expect(crearAddonsPropios()).toEqual([]); // the drawer needs no addon
    const aviso = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const g = crearGuardia([() => new a()]);
      const [propio] = g.crearAddonsPropios();
      const intruso = new a(); // same class, same name, not made by the factory
      expect(g.clasificarAddon(propio)).toBe('propio');
      expect(g.clasificarAddon(intruso)).toBe('ajeno');
      const cargados: unknown[] = [];
      const term = { loadAddon: (x: unknown) => void cargados.push(x) };
      expect(g.cargarAddon(term, propio as never)).toEqual({ cargado: true, motivo: '' });
      expect(g.cargarAddon(term, intruso as never)).toMatchObject({ cargado: false });
      expect(cargados).toEqual([propio]);
      const guardado = g.protegerTerminal({ loadAddon: (x: unknown) => void cargados.push(x) });
      guardado.loadAddon(intruso as never);
      guardado.loadAddon(propio as never);
      expect(cargados).toEqual([propio, propio]); // the intruder never got through, the drawer's own instance did
      expect(g.rechazos().map((r) => r.addon)).toEqual(['a', 'a']);
      expect(rechazosDeGuarda()).toEqual([]); // the drawer's own guard heard nothing of it
    } finally {
      aviso.mockRestore();
    }
  });

  it('no production source names a renderer addon class or builds a guard of its own', () => {
    for (const { nombre, texto } of FUENTES_TERMINAL) {
      expect(texto, nombre).not.toMatch(/CanvasAddon|WebglAddon/); // a name-based deny-list needs the names
      if (nombre !== 'termguard.ts') expect(texto, nombre).not.toMatch(/crearGuardia\(/);
    }
    // the scan can see a culprit
    expect("const PROHIBIDOS = ['CanvasAddon'];").toMatch(/CanvasAddon|WebglAddon/);
  });
});

describe('the DOM line of the guard (termguard.ts leerRenderer, vigilarCapas)', () => {
  const sel = (mapa: Record<string, unknown>) => ({ querySelector: (s: string) => mapa[s] ?? null }) as unknown as Pick<Element, 'querySelector'>;

  it('reads the renderer from the structure of the DOM: a canvas ANYWHERE wins, the DOM rows are the DOM renderer', () => {
    expect(leerRenderer(sel({}))).toBe('desconocido');
    expect(leerRenderer(sel({ '.xterm-screen': sel({}) }))).toBe('desconocido');
    expect(leerRenderer(sel({ '.xterm-screen': sel({ '.xterm-rows': {} }) }))).toBe('dom');
    expect(leerRenderer(sel({ canvas: {}, '.xterm-screen': sel({ canvas: {} }) }))).toBe('canvas');
    expect(leerRenderer(sel({ canvas: {}, '.xterm-screen': sel({ canvas: {}, '.xterm-rows': {} }) }))).toBe('canvas');
    // a canvas BESIDE .xterm-screen (attempt 1's blind spot: it only looked inside) is a canvas terminal too
    expect(leerRenderer(sel({ canvas: {}, '.xterm-screen': sel({ '.xterm-rows': {} }) }))).toBe('canvas');
  });

  it('watches the host, .xterm and .xterm-screen for a canvas layer, calls back once, and can be stopped', () => {
    const observadores: Array<{ alCambiar: () => void; objetivos: Array<[unknown, unknown]>; desconectado: boolean }> = [];
    class FalsoObservador {
      objetivos: Array<[unknown, unknown]> = [];
      desconectado = false;
      constructor(public alCambiar: () => void) {
        observadores.push(this);
      }
      observe(objetivo: unknown, opciones: unknown): void {
        this.objetivos.push([objetivo, opciones]);
      }
      disconnect(): void {
        this.desconectado = true;
      }
    }
    vi.stubGlobal('MutationObserver', FalsoObservador);
    try {
      const xterm = { nombre: 'xterm' };
      const pantalla = { nombre: 'pantalla', querySelector: () => null };
      let hayCanvas = false;
      const raiz = { querySelector: (s: string) => (s === 'canvas' ? (hayCanvas ? {} : null) : s === '.xterm' ? xterm : s === '.xterm-screen' ? pantalla : null) } as unknown as Element;
      const avisos: string[] = [];
      const parar = vigilarCapas(raiz, (m) => avisos.push(m));
      const [o] = observadores;
      expect(o.objetivos).toEqual([[raiz, { childList: true }], [xterm, { childList: true }], [pantalla, { childList: true }]]); // direct children only: the DOM renderer's row updates never wake it
      o.alCambiar(); // a mutation with no canvas in it
      expect(avisos).toEqual([]);
      hayCanvas = true;
      o.alCambiar();
      o.alCambiar();
      expect(avisos).toHaveLength(1);
      expect(avisos[0]).toMatch(/canvas layer appeared/);
      expect(o.desconectado).toBe(true);
      parar();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('scrollback, options and the instance budget (termguard.ts)', () => {
  it('clamps the scrollback to the limit, refuses a request that is not a non-negative integer, and puts the clamp in every terminal\'s options', () => {
    expect(limitarScrollback(0)).toBe(0);
    expect(limitarScrollback(500)).toBe(500);
    expect(limitarScrollback(SCROLLBACK_DECLARADO)).toBe(SCROLLBACK_DECLARADO);
    expect(limitarScrollback(SCROLLBACK_DECLARADO + 1)).toBe(SCROLLBACK_DECLARADO);
    expect(limitarScrollback(5_000_000)).toBe(SCROLLBACK_DECLARADO);
    for (const malo of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) expect(() => limitarScrollback(malo), String(malo)).toThrow(RangeError);
    expect(opcionesDeTerminal().scrollback).toBe(SCROLLBACK_DECLARADO);
    expect(opcionesDeTerminal({ scrollback: 9_999 }).scrollback).toBe(SCROLLBACK_DECLARADO);
    expect(opcionesDeTerminal({ scrollback: 300 }).scrollback).toBe(300);
    expect(() => opcionesDeTerminal({ scrollback: -5 })).toThrow(RangeError);
    expect(opcionesDeTerminal().allowProposedApi).toBe(false);
  });

  it('makes the first 56 instances interactive and every later one plain text', () => {
    expect(modoDeInstancia(0)).toBe('interactivo');
    expect(modoDeInstancia(55)).toBe('interactivo');
    expect(modoDeInstancia(56)).toBe('texto');
    expect(modoDeInstancia(500)).toBe('texto');
    for (const malo of [-1, 2.5, Number.NaN]) expect(() => modoDeInstancia(malo), String(malo)).toThrow(RangeError);
    expect(Array.from({ length: 60 }, (_, i) => modoDeInstancia(i)).filter((m) => m === 'interactivo')).toHaveLength(56);
  });

  it('strips terminal escape sequences and carriage returns from a plain-text stream', () => {
    expect(quitarAnsi('\x1b[31mred\x1b[0m plain')).toBe('red plain');
    expect(quitarAnsi('a\x1b[2K\x1b[1;1Hb')).toBe('ab');
    expect(quitarAnsi('title\x1b]0;window title\x07 kept')).toBe('title kept');
    expect(quitarAnsi('line one\r\nline two\r')).toBe('line one\nline two');
    expect(quitarAnsi('\x1b[?25lhidden cursor')).toBe('hidden cursor');
  });
});

describe('the three-pass focus (termguard.ts programarFoco)', () => {
  it('schedules exactly the three passes at 0, 120 and 400 ms, focuses only while the panel is still active, and cancels the rest', () => {
    const programadas: Array<{ fn: () => void; ms: number }> = [];
    const canceladas: unknown[] = [];
    let activo = true;
    let enfoques = 0;
    const cancelar = programarFoco(
      () => enfoques++,
      () => activo,
      (fn, ms) => {
        programadas.push({ fn, ms });
        return programadas.length - 1;
      },
      (m) => canceladas.push(m),
    );
    expect(programadas.map((p) => p.ms)).toEqual([0, 120, 400]);
    programadas[0].fn();
    expect(enfoques).toBe(1);
    activo = false; // the drawer was closed or another tab took over
    programadas[1].fn();
    expect(enfoques).toBe(1);
    activo = true;
    programadas[2].fn();
    expect(enfoques).toBe(2);
    cancelar();
    expect(canceladas).toEqual([0, 1, 2]);
  });
});

// ---- wsstream.ts: the codec against the server's independent codec ----------------------------------------------------------
describe('the SRF2 codec (wsstream.ts codificarTrama / decodificarTrama)', () => {
  it('writes the 32-byte little-endian header of the specification and agrees byte for byte with the independent server codec', () => {
    const cuerpo = new Uint8Array([104, 105, 0, 255]);
    const mia = codificarTrama(cuerpo, 0x01020304, 0x0a0b0c0d);
    expect(mia.byteLength).toBe(TAM_CABECERA + 4);
    expect(TAM_CABECERA).toBe(32);
    expect(Buffer.from(mia.subarray(0, 4)).toString('latin1')).toBe('SRF2');
    expect([...mia.subarray(4, 8)]).toEqual([1, 0, 1, 0]); // version 1, stride 1
    expect([...mia.subarray(8, 12)]).toEqual([4, 0, 0, 0]); // count 4
    expect([...mia.subarray(12, 16)]).toEqual([4, 3, 2, 1]); // seq, little-endian
    expect([...mia.subarray(16, 20)]).toEqual([13, 12, 11, 10]); // ms, little-endian
    expect([...mia.subarray(20, 32)]).toEqual(new Array(12).fill(0)); // reserved
    expect([...mia.subarray(32)]).toEqual([...cuerpo]);
    // the server's codec was written separately, from the same paragraph of the specification
    const suya = codificarTramaSrf2(Buffer.from(cuerpo), 0x01020304, 0x0a0b0c0d);
    expect(Buffer.from(mia).equals(suya)).toBe(true);
    const leida = decodificarTrama(suya);
    expect(leida).toMatchObject({ version: 1, stride: 1, cuenta: 4, seq: 0x01020304, ms: 0x0a0b0c0d });
    expect([...leida.cuerpo]).toEqual([...cuerpo]);
    const dela = decodificarTramaSrf2(Buffer.from(mia));
    expect(dela).toMatchObject({ version: 1, stride: 1, cuenta: 4, seq: 0x01020304, ms: 0x0a0b0c0d });
    // records wider than a byte
    const anchos = codificarTrama(new Uint8Array(12), 7, 9, 4);
    expect(decodificarTrama(anchos)).toMatchObject({ stride: 4, cuenta: 3 });
    expect(Buffer.from(anchos).equals(codificarTramaSrf2(Buffer.alloc(12), 7, 9, 4))).toBe(true);
  });

  it('refuses a frame that does not hold the contract, and says why', () => {
    const buena = Buffer.from(codificarTrama(new Uint8Array([1, 2, 3]), 1, 1));
    const con = (f: (b: Buffer) => void): Buffer => {
      const b = Buffer.from(buena);
      f(b);
      return b;
    };
    expect(() => decodificarTrama(new Uint8Array(10))).toThrow(/shorter than the 32-byte header/);
    expect(() => decodificarTrama(con((b) => b.write('SRF3', 0, 'latin1')))).toThrow(/bad magic/);
    expect(() => decodificarTrama(con((b) => b.writeUInt16LE(2, 4)))).toThrow(/unsupported frame version 2/);
    expect(() => decodificarTrama(con((b) => b.writeUInt16LE(0, 6)))).toThrow(/stride 0/);
    expect(() => decodificarTrama(con((b) => b.writeUInt32LE(9, 8)))).toThrow(/count 9 x stride 1/);
    expect(() => decodificarTrama(con((b) => (b[25] = 1)))).toThrow(/reserved byte 25/);
    expect(() => decodificarTrama(buena.subarray(0, buena.length - 1))).toThrow(ErrorDeTrama);
    expect(() => codificarTrama(new Uint8Array(5), 0, 0, 2)).toThrow(RangeError);
    // the server's codec refuses the same frames
    expect(() => decodificarTramaSrf2(con((b) => b.write('SRF3', 0, 'latin1')))).toThrow(/bad magic/);
    expect(() => decodificarTramaSrf2(con((b) => b.writeUInt32LE(9, 8)))).toThrow(/count 9/);
  });
});

describe('the non-reactive queue and its flow control (wsstream.ts ColaDeEscritura)', () => {
  const bytes = (n: number, valor = 1): Uint8Array => new Uint8Array(n).fill(valor);

  it('hands back exactly the bytes it was given, in order, split at the frame limit', () => {
    const cola = new ColaDeEscritura({ altaMarca: 1_000_000, alPausar: () => {}, alReanudar: () => {} });
    const entrada: number[] = [];
    for (const [n, v] of [[7, 1], [1, 2], [300, 3], [5, 4], [64, 5]] as const) {
      cola.publicar(bytes(n, v));
      entrada.push(...bytes(n, v));
    }
    cola.publicar(new Uint8Array(0)); // an empty chunk is nothing
    expect(cola.bytesEnCola).toBe(entrada.length);
    const salida: number[] = [];
    for (let lote = cola.tomar(50); lote !== null; lote = cola.tomar(50)) {
      expect(lote.byteLength).toBeLessThanOrEqual(50);
      salida.push(...lote);
    }
    expect(salida).toEqual(entrada);
    expect(cola.bytesEnCola).toBe(0);
    expect(cola.bytesEnVuelo).toBe(entrada.length);
    expect(cola.pendientes).toBe(entrada.length);
    cola.confirmar(entrada.length);
    expect(cola.pendientes).toBe(0);
  });

  it('asks for PAUSE_FLOW once when the unparsed bytes pass the high-water mark and for RESUME_FLOW once when they fall under a quarter of it', () => {
    const eventos: string[] = [];
    const cola = new ColaDeEscritura({ altaMarca: 1_000, alPausar: () => eventos.push('pausar'), alReanudar: () => eventos.push('reanudar') });
    cola.publicar(bytes(1_000)); // at the mark, not over it
    expect(eventos).toEqual([]);
    cola.publicar(bytes(1)); // over
    expect(eventos).toEqual(['pausar']);
    cola.publicar(bytes(500)); // still over: no second request
    expect(eventos).toEqual(['pausar']);
    expect(cola.estaPausada).toBe(true);
    const lote = cola.tomar(1_200)!; // in flight still counts as pending
    expect(lote.byteLength).toBe(1_200);
    expect(cola.pendientes).toBe(1_501);
    cola.confirmar(700); // 801 pending: under the mark, far over a quarter of it
    expect(cola.pendientes).toBe(801);
    expect(eventos).toEqual(['pausar']);
    cola.confirmar(500); // 301 pending (all of it waiting): still over a quarter of the mark, which is 250
    expect(cola.pendientes).toBe(301);
    expect(eventos).toEqual(['pausar']);
    expect(cola.tomar(100)!.byteLength).toBe(100);
    cola.confirmar(100); // 201 pending: under a quarter
    expect(cola.pendientes).toBe(201);
    expect(eventos).toEqual(['pausar', 'reanudar']);
    expect(cola.pausas).toBe(1);
    expect(cola.reanudaciones).toBe(1);
    expect(ALTA_MARCA_BYTES).toBe(2 * 1_048_576);
  });
});

// ---- wsstream.ts against a real WebSocket server -----------------------------------------------------------------------------
describe('the stream client (wsstream.ts FlujoWs) against the stand-in PTY server', () => {
  let pty: Awaited<ReturnType<typeof crearServidorPty>>;
  const flujos: FlujoWs[] = [];
  beforeEach(async () => {
    // FlujoWs opens the browser's WebSocket: a Node without a global one (18, or 20 and 21 without a flag) cannot run these tests
    if (typeof WebSocket === 'undefined') throw new Error(`these tests need a Node with a global WebSocket (22 or newer); this is ${process.version}`);
    pty = await crearServidorPty();
  });
  afterEach(async () => {
    for (const f of flujos.splice(0)) f.cerrar();
    await pty.cerrar();
  });

  function abrir(id: string, extra: Partial<OpcionesDeFlujo> = {}) {
    const recibido: number[] = [];
    const fases: string[] = [];
    const errores: Error[] = [];
    const salidas: Array<number | null> = [];
    const f = new FlujoWs({
      url: `ws://127.0.0.1:${pty.puerto}/pty/${id}`,
      alDatos: (c) => void recibido.push(...c),
      alFase: (fase) => void fases.push(fase),
      alError: (e) => void errores.push(e),
      alSalida: (c) => void salidas.push(c),
      esperaBaseMs: 5,
      ...extra,
    });
    flujos.push(f);
    f.conectar();
    return { f, recibido, fases, errores, salidas, texto: () => Buffer.from(recibido).toString('utf8') };
  }
  const GREETING = (id: string): string => `srf stand-in ${id} ready\r\n$ `;

  it('carries binary frames in both directions: the greeting arrives, keystrokes reach the server decoded and in sequence, the echo comes back', async () => {
    const a = abrir('t1');
    await esperarHasta(() => a.f.fase === 'abierto' && a.texto() === GREETING('t1'), 5_000, 'the greeting');
    expect(a.f.contadores).toMatchObject({ aperturas: 1, tramas: 1, invalidas: 0, saltosDeSecuencia: 0 });
    expect(a.f.enviarTexto('ls -la\r')).toBe(true);
    expect(a.f.enviarEntrada(new Uint8Array([0xc3, 0xb1]))).toBe(true); // "ñ" as UTF-8
    await esperarHasta(() => pty.conexion('t1')!.entradas.length === 2, 5_000, 'both input frames at the server');
    const server = pty.conexion('t1')!;
    expect(Buffer.concat(server.entradas).toString('utf8')).toBe('ls -la\rñ');
    expect(server.seqsEntrada).toEqual([0, 1]); // one frame each, numbered from 0
    expect(server.tramasInvalidasRecibidas).toBe(0);
    await esperarHasta(() => a.texto() === `${GREETING('t1')}ls -la\rñ`, 5_000, 'the echo');
    expect(a.f.contadores).toMatchObject({ tramas: 3, invalidas: 0, saltosDeSecuencia: 0, enviadas: 2 });
    // input on a closed stream is counted and reported, never dropped unseen
    a.f.cerrar();
    expect(a.f.enviarTexto('x')).toBe(false);
    expect(a.f.contadores.descartadas).toBe(1);
    expect(a.errores.at(-1)!.message).toMatch(/input dropped/);
  });

  it('asks the server to pause and resume the flow, and sends the terminal size', async () => {
    const a = abrir('t2');
    await esperarHasta(() => a.f.fase === 'abierto' && a.texto() === GREETING('t2'), 5_000, 'the greeting');
    a.f.redimensionar(100, 30);
    a.f.pausar();
    a.f.pausar(); // idempotent
    await esperarHasta(() => pty.conexion('t2')!.pausado && pty.conexion('t2')!.dims !== null, 5_000, 'PAUSE_FLOW and RESIZE at the server');
    expect(pty.conexion('t2')!.dims).toEqual({ cols: 100, rows: 30 });
    pty.enviar('t2', Buffer.alloc(300_000, 'a'));
    await dormir(150);
    expect(a.recibido.length).toBe(GREETING('t2').length); // held back by the server
    expect(pty.conexion('t2')!.cola.length).toBeGreaterThan(0);
    a.f.reanudar();
    await esperarHasta(() => a.recibido.length === GREETING('t2').length + 300_000, 10_000, 'the held-back bytes');
    expect(pty.conexion('t2')!.pausas).toBe(1);
    expect(pty.conexion('t2')!.reanudaciones).toBe(1);
    expect(a.f.contadores).toMatchObject({ invalidas: 0, saltosDeSecuencia: 0 });
  });

  it('reconnects when the server cuts the stream, tells the new connection what the old one knew, and does not reconnect after cerrar()', async () => {
    const a = abrir('t3');
    await esperarHasta(() => a.f.fase === 'abierto', 5_000, 'the first connection');
    a.f.redimensionar(90, 20);
    a.f.pausar();
    await esperarHasta(() => pty.conexion('t3')!.pausado && pty.conexion('t3')!.dims !== null, 5_000, 'state at the first connection');
    pty.cerrarConexion('t3');
    await esperarHasta(() => pty.aceptadas('t3') === 2 && a.f.fase === 'abierto', 5_000, 'the reconnection');
    expect(a.fases).toContain('reconectando');
    expect(a.f.contadores).toMatchObject({ reconexiones: 1, aperturas: 2 });
    await esperarHasta(() => pty.conexion('t3')!.dims !== null && pty.conexion('t3')!.pausado, 5_000, 'the state re-sent on the new connection');
    expect(pty.conexion('t3')!.dims).toEqual({ cols: 90, rows: 20 });
    a.f.cerrar();
    await dormir(200);
    expect(a.f.fase).toBe('cerrado');
    expect(pty.aceptadas('t3')).toBe(2); // no third connection
  });

  it('counts and reports a malformed frame and a sequence gap instead of dropping them, and keeps delivering', async () => {
    const a = abrir('t4');
    await esperarHasta(() => a.f.fase === 'abierto' && a.texto() === GREETING('t4'), 5_000, 'the greeting');
    pty.enviarCrudo('t4', Buffer.from('this is not an SRF2 frame at all, and it is long enough'));
    await esperarHasta(() => a.f.contadores.invalidas === 1, 5_000, 'the malformed frame');
    expect(a.errores.some((e) => e instanceof ErrorDeTrama && /bad magic/.test(e.message))).toBe(true);
    pty.enviarCrudo('t4', codificarTramaSrf2(Buffer.from('X'), 99, 0));
    await esperarHasta(() => a.f.contadores.saltosDeSecuencia === 1, 5_000, 'the sequence gap');
    expect(a.texto().endsWith('X')).toBe(true);
    pty.enviarControl('t4', { type: 'STATUS' }); // an unknown control frame is counted, not fatal
    await esperarHasta(() => a.f.contadores.control === 1, 5_000, 'the control frame');
  });

  it('ends the session on EXIT without reconnecting, and gives up loudly when the server is gone', async () => {
    const a = abrir('t5');
    await esperarHasta(() => a.f.fase === 'abierto', 5_000, 'the connection');
    pty.enviarControl('t5', { type: 'EXIT', code: 3 });
    pty.cerrarConexion('t5', 1000);
    await esperarHasta(() => a.f.fase === 'cerrado', 5_000, 'the end of the session');
    expect(a.salidas).toEqual([3]);
    await dormir(150);
    expect(pty.aceptadas('t5')).toBe(1);

    const muerto = await crearServidorPty();
    const puerto = muerto.puerto;
    await muerto.cerrar();
    const b = abrir('t6', { url: `ws://127.0.0.1:${puerto}/pty/t6`, maxReconexiones: 2, esperaBaseMs: 1 });
    await esperarHasta(() => b.f.fase === 'fallido', 10_000, 'the give-up');
    expect(b.f.contadores.reconexiones).toBe(2);
    expect(b.errores.some((e) => /gave up reconnecting/.test(e.message))).toBe(true);
  });
});

// ---- wsstream.ts against a socket that does exactly what the test says, on a clock the test drives -------------------------------
class FalsoSocket {
  static todos: FalsoSocket[] = [];
  static ultimo(): FalsoSocket {
    return FalsoSocket.todos[FalsoSocket.todos.length - 1];
  }
  readyState = 0;
  binaryType = '';
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  constructor(public url: string) {
    FalsoSocket.todos.push(this);
  }
  send(): void {}
  close(): void {}
  abrir(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  soltar(code = 1006): void {
    this.readyState = 3;
    this.onclose?.({ code, reason: '' });
  }
  mensaje(datos: unknown): void {
    this.onmessage?.({ data: datos });
  }
}

function relojFalso() {
  const agenda: Array<{ id: number; fn: () => void; ms: number }> = [];
  const historial: number[] = [];
  let n = 0;
  return {
    agenda,
    /** every delay ever scheduled, in order */
    historial,
    programar: (fn: () => void, ms: number): unknown => {
      agenda.push({ id: ++n, fn, ms });
      historial.push(ms);
      return n;
    },
    cancelar: (id: unknown): void => {
      const i = agenda.findIndex((x) => x.id === id);
      if (i >= 0) agenda.splice(i, 1);
    },
    /** runs the oldest scheduled callback (or the oldest one of `ms`) */
    correr: (ms?: number): void => {
      const i = ms === undefined ? 0 : agenda.findIndex((x) => x.ms === ms);
      if (i < 0) throw new Error(`nothing is scheduled${ms === undefined ? '' : ` at ${ms} ms`}`);
      agenda.splice(i, 1)[0].fn();
    },
  };
}

describe('the stream client on a scripted socket (wsstream.ts FlujoWs)', () => {
  function flujo(extra: Partial<OpcionesDeFlujo> = {}) {
    FalsoSocket.todos = [];
    const reloj = relojFalso();
    const errores: Error[] = [];
    const datos: number[] = [];
    const f = new FlujoWs({
      url: 'ws://127.0.0.1:1/pty/x',
      crearSocket: (u) => new FalsoSocket(u) as unknown as WebSocket,
      programar: reloj.programar,
      cancelar: reloj.cancelar,
      alError: (e) => void errores.push(e),
      alDatos: (c) => void datos.push(...c),
      ...extra,
    });
    f.conectar();
    return { f, reloj, errores, datos };
  }
  /** the delays scheduled to REOPEN the stream when the waits stay under ESTABLE_MS (the stability timer is not one of them) */
  const esperas = (h: number[]): number[] => h.filter((ms) => ms !== ESTABLE_MS);

  it('doubles the wait before each reconnection up to the cap, and gives up after the last attempt', () => {
    const { f, reloj, errores } = flujo({ esperaBaseMs: 500, maxReconexiones: 8 });
    for (let i = 0; i < 8; i++) {
      FalsoSocket.ultimo().soltar(); // the socket never opens
      reloj.correr();
    }
    FalsoSocket.ultimo().soltar();
    expect(reloj.historial).toEqual([500, 1_000, 2_000, 2_000, 2_000, 2_000, 2_000, 2_000]); // no socket ever opens: every timer is a reconnection delay
    expect(ESPERA_MAX_MS).toBe(2_000);
    expect(f.fase).toBe('fallido');
    expect(errores.at(-1)!.message).toMatch(/gave up reconnecting .* after 8 attempts/);
  });

  it('retries a server that accepts the upgrade and drops at once with growing waits, then gives up; a connection that stays up restarts the backoff', () => {
    const { f, reloj, errores } = flujo({ esperaBaseMs: 50, maxReconexiones: 5 });
    for (let i = 0; i < 12 && f.fase !== 'fallido'; i++) {
      const s = FalsoSocket.ultimo();
      s.abrir(); // accepted...
      s.soltar(); // ...and dropped before it proved itself: the stability timer is cancelled with it
      expect(reloj.agenda.filter((x) => x.ms === ESTABLE_MS)).toEqual([]);
      if (f.fase !== 'fallido') reloj.correr();
    }
    expect(esperas(reloj.historial)).toEqual([50, 100, 200, 400, 800]);
    expect(f.fase).toBe('fallido');
    expect(errores.at(-1)!.message).toMatch(/gave up reconnecting/);
    expect(ESTABLE_MS).toBe(2_000);

    // a connection that stays up for ESTABLE_MS earns a fresh start
    const otro = flujo({ esperaBaseMs: 50, maxReconexiones: 5 });
    for (let i = 0; i < 2; i++) {
      FalsoSocket.ultimo().abrir();
      FalsoSocket.ultimo().soltar();
      otro.reloj.correr();
    }
    FalsoSocket.ultimo().abrir();
    otro.reloj.correr(ESTABLE_MS); // it stayed up
    FalsoSocket.ultimo().soltar();
    expect(esperas(otro.reloj.historial)).toEqual([50, 100, 50]);
    expect(otro.f.fase).toBe('reconectando');
  });

  it('counts a malformed control frame and drops it: nothing a server sends throws out of the message handler', () => {
    const { f, errores, datos } = flujo();
    const s = FalsoSocket.ultimo();
    s.abrir();
    expect(() => s.mensaje('null')).not.toThrow(); // the frame that used to raise a TypeError on msg.type
    expect(f.contadores.invalidas).toBe(1);
    expect(errores.at(-1)!.message).toMatch(/not an object/);
    for (const hostil of ['[1, 2]', '"text"', '5', 'true', '{"type": ']) expect(() => s.mensaje(hostil), hostil).not.toThrow();
    expect(f.contadores.invalidas).toBe(6);
    expect(() => s.mensaje(new ArrayBuffer(4))).not.toThrow();
    expect(() => s.mensaje(new Uint8Array(3))).not.toThrow();
    expect(f.contadores.invalidas).toBe(8);
    s.mensaje('{"type":"STATUS"}'); // a well-formed control frame of a type this client does not act on is only counted as control
    expect(f.contadores.invalidas).toBe(8);
    expect(f.contadores.control).toBe(7); // seven text frames; the two binary ones are not control frames
    s.mensaje(codificarTrama(new Uint8Array([7, 8, 9]), 0, 0).buffer); // and the stream keeps delivering
    expect(datos).toEqual([7, 8, 9]);
  });
});

// ---- TerminalPanel.svelte: the shape that the instrument then measures ------------------------------------------------------
type Nodo = { type: string; [k: string]: any };
function recorrer(nodo: unknown, visita: (n: Nodo) => void): void {
  if (Array.isArray(nodo)) {
    for (const x of nodo) recorrer(x, visita);
  } else if (nodo !== null && typeof nodo === 'object') {
    const n = nodo as Nodo;
    if (typeof n.type === 'string') visita(n);
    for (const [k, v] of Object.entries(n)) if (k !== 'parent' && k !== 'metadata') recorrer(v, visita);
  }
}
const llamadasA = (raiz: unknown, nombre: string): Nodo[] => {
  const salida: Nodo[] = [];
  recorrer(raiz, (n) => {
    if (n.type === 'CallExpression' && n.callee.type === 'Identifier' && n.callee.name === nombre) salida.push(n);
  });
  return salida;
};

describe('the mount of a panel (TerminalPanel.svelte)', () => {
  it('runs montar() only inside untrack(), and every $effect that mounts does so', () => {
    const ast = parse(PANEL, { modern: true });
    const efectos = llamadasA(ast.instance, '$effect');
    expect(efectos.length).toBeGreaterThanOrEqual(3);
    const montan = efectos.filter((e) => llamadasA(e, 'montar').length + llamadasA(e, 'montarTexto').length > 0);
    expect(montan).toHaveLength(2); // the terminal mount and the plain-text mount
    for (const efecto of montan) {
      // every call to a mount function inside the effect is inside an untrack(...) call
      const dentroDeUntrack = llamadasA(llamadasA(efecto, 'untrack'), 'montar').length + llamadasA(llamadasA(efecto, 'untrack'), 'montarTexto').length;
      const total = llamadasA(efecto, 'montar').length + llamadasA(efecto, 'montarTexto').length;
      expect(total).toBe(1);
      expect(dentroDeUntrack).toBe(1);
    }
    // the mount function does read and write reactive state: without untrack that is what loops
    expect(PANEL).toMatch(/montajes \+= 1;/);
  });

  it('keeps every panel mounted and offers no path to a renderer addon or a paste handler', () => {
    // no renderer addon is ever imported; the guard only names them as strings
    for (const { nombre, texto } of FUENTES_TERMINAL) {
      expect(texto, nombre).not.toMatch(/from\s+['"]@xterm\/addon-(canvas|webgl)['"]/);
      expect(texto, nombre).not.toMatch(/svelte\/store/);
      expect(texto, nombre).not.toMatch(/onpaste|['"`]paste['"`]|clipboardData|navigator\.clipboard|execCommand/i);
    }
    // the scan can see a culprit
    expect("import { CanvasAddon } from '@xterm/addon-canvas';").toMatch(/from\s+['"]@xterm\/addon-(canvas|webgl)['"]/);
    // a panel is hidden with visibility, never with display: none or a condition that would unmount its terminal
    const sinComentarios = PANEL.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
    expect(sinComentarios).toMatch(/visibility: hidden/);
    expect(sinComentarios).not.toMatch(/display:\s*none/);
    expect(CAJON).not.toMatch(/\{#if[^}]*abierto/);
    expect(CAJON).toMatch(/\{#each agentes as a, i \(a\.id\)\}/); // keyed: replacing the array never remounts a panel
  });
});

// ---- the pure parts of the instrument ----------------------------------------------------------------------------------------
describe('the instrument\'s own reading of a run (scripts/ng/medir-terminal.mjs)', () => {
  it('decides the renderer from structure, and of a set of terminals by what they all use', () => {
    expect(decidirRendererLeido(14, 0)).toBe('dom');
    expect(decidirRendererLeido(0, 4)).toBe('canvas');
    expect(decidirRendererLeido(14, 4)).toBe('canvas');
    expect(decidirRendererLeido(0, 0)).toBe('desconocido');
    expect(rendererDeConjunto(['dom', 'dom', 'dom'])).toBe('dom');
    expect(rendererDeConjunto(['canvas', 'canvas'])).toBe('canvas');
    expect(rendererDeConjunto(['dom', 'canvas'])).toBe('mixto');
    expect(rendererDeConjunto([])).toBe('ninguno');
  });

  it('counts remounts beyond the first mount of each panel and compares two counters panel by panel', () => {
    expect(remontajesDe({ a: 1, b: 1, c: 1 })).toEqual({ montajes_max: 1, remontajes_max: 0, remontajes_total: 0 });
    expect(remontajesDe({ a: 1, b: 4, c: 2 })).toEqual({ montajes_max: 4, remontajes_max: 3, remontajes_total: 4 });
    expect(remontajesDe({})).toEqual({ montajes_max: null, remontajes_max: null, remontajes_total: null });
    expect(compararMontajes({ a: 1, b: 2 }, { a: 1, b: 2 }, 'x', 'y')).toEqual([]);
    expect(compararMontajes({ a: 1, b: 2 }, { a: 1, b: 3, c: 1 }, 'observer', 'component')).toEqual([
      'mounts of b: observer 2 vs component 3',
      'mounts of c: observer 0 vs component 1',
    ]);
  });

  it('derives the retained scrollback from the buffer and checks it against the number of the first retained line', () => {
    // 5 rows + 3 scrollback = a buffer of 8; 20 numbered lines written: the last 8 of 21 are kept, the first is line 14
    expect(retencionDeBuffer({ largo: 8, filas: 5, primeraLinea: 'linea 14', escritas: 20 })).toEqual({ retenidas: 3, numero_primera: 14, esperada_primera: 14, coincide: true });
    expect(retencionDeBuffer({ largo: 8, filas: 5, primeraLinea: 'linea 13', escritas: 20 }).coincide).toBe(false);
    expect(retencionDeBuffer({ largo: 8, filas: 5, primeraLinea: 'something else', escritas: 20 })).toMatchObject({ numero_primera: null, coincide: false });
  });

  it('takes the oracle\'s scrollback from the MOST-retaining of all terminals, and reports what disagrees', () => {
    const lectura = (id: string, retenidas: number, opcion = 2_000, atributo = opcion) => ({ id, largo: retenidas + 12, filas: 12, primeraLinea: `linea ${2_612 + 2 - (retenidas + 12)}`, opcion, atributo });
    const buenas = Array.from({ length: 56 }, (_, i) => lectura(`srf-ng-${i}`, 2_000));
    expect(resumirScrollback(buenas, 2_612)).toMatchObject({ terminales: 56, retenidas_max: 2_000, retenidas_min: 2_000, coinciden: 56, desacuerdos: [] });
    // ONE terminal with a 50,000-line buffer among 56: the field is its count, not the first terminal's
    const una = buenas.map((l) => (l.id === 'srf-ng-9' ? lectura('srf-ng-9', 2_601, 50_000) : l));
    expect(resumirScrollback(una, 2_612)).toMatchObject({ retenidas_max: 2_601, retenidas_min: 2_000, opcion_max: 50_000, atributo_max: 50_000, mayor: 'srf-ng-9', desacuerdos: [] });
    // a component that publishes less than its terminal holds is a disagreement, not a pass
    const miente = buenas.map((l) => (l.id === 'srf-ng-3' ? lectura('srf-ng-3', 2_000, 2_000, 1_000) : l));
    expect(resumirScrollback(miente, 2_612).desacuerdos).toEqual(['scrollback of srf-ng-3: the component publishes 1000, the terminal option is 2000']);
    // a terminal that retains less than its option says, or whose first line is not the one the arithmetic gives
    expect(resumirScrollback([lectura('a', 1_500)], 2_612).desacuerdos[0]).toMatch(/retained 1500 vs option 2000/);
    expect(resumirScrollback([{ ...lectura('b', 2_000), primeraLinea: 'linea 5' }], 2_612).desacuerdos[0]).toMatch(/expected 602/);
    expect(resumirScrollback([], 10)).toMatchObject({ terminales: 0, retenidas_max: null });
  });

  it('judges the addon requests of a run: one refusal per request, by name, no stand-in that ran, no canvas anywhere', () => {
    const limpio = { solicitudes: ['a', '(anonymous)', 'Marcador', 'CanvasAddon'], rechazos: ['(anonymous)', 'a', 'CanvasAddon', 'Marcador'], activaciones: 0, lienzosFueraDeLaPantalla: 0, lienzosCdp: 0 };
    expect(desacuerdosDeAddons(limpio)).toEqual([]);
    expect(desacuerdosDeAddons({ solicitudes: [], rechazos: [], activaciones: 0, lienzosFueraDeLaPantalla: 0, lienzosCdp: 0 })).toEqual([]);
    // requested and never refused (a guard that let them through, or one that refused in silence)
    expect(desacuerdosDeAddons({ ...limpio, rechazos: [] })).toEqual(expect.arrayContaining(['the harness requested 4 addon(s) and the guard refused 0', 'addon "a": requested 1 time(s), refused 0']));
    // the right number of refusals, of the wrong addons
    expect(desacuerdosDeAddons({ ...limpio, rechazos: ['a', 'a', 'a', 'a'] })).toEqual(expect.arrayContaining(['addon "Marcador": requested 1 time(s), refused 0', 'addon "a": requested 1 time(s), refused 4']));
    expect(desacuerdosDeAddons({ ...limpio, activaciones: 3 })).toEqual(['3 stand-in addon(s) ran activate(): a request got past the guard']);
    expect(desacuerdosDeAddons({ ...limpio, lienzosFueraDeLaPantalla: 2 })[0]).toMatch(/outside \.xterm-screen/);
    expect(desacuerdosDeAddons({ ...limpio, lienzosCdp: 1 })[0]).toMatch(/CDP finds 1 canvas/);
  });

  it('labels every value under `tramos` with its provenance, and the declared and harness-computed ones say so', () => {
    const vacio = informeVacio();
    const etiquetas = procedenciaDeTramos(vacio.tramos);
    const hojas: string[] = [];
    const visitar = (n: unknown, ruta: string): void => {
      if (n !== null && typeof n === 'object' && !Array.isArray(n) && Object.keys(n).length > 0) for (const [k, v] of Object.entries(n)) visitar(v, ruta === '' ? k : `${ruta}.${k}`);
      else hojas.push(ruta);
    };
    visitar(vacio.tramos, '');
    expect(Object.keys(etiquetas).sort()).toEqual([...hojas].sort());
    expect(vacio.procedencia.tramos).toEqual(etiquetas); // and the report carries them
    for (const [hoja, etiqueta] of Object.entries(etiquetas)) expect(etiqueta, hoja).toMatch(/^(MEASURED|READ-BACK|DECLARED|HARNESS-COMPUTED)\b/);
    for (const declarada of ['churn.ciclos', 'churn.arrays_reemplazados', 'oculto.alta_marca', 'foco.pasos_declarados', 'pestanas.teclas', 'salida.codigo_enviado']) expect(etiquetas[declarada], declarada).toMatch(/^DECLARED/);
    for (const calculada of ['churn.pestanas_activadas', 'churn.flujos_cortados', 'oculto.bytes_ofrecidos_al_servidor', 'scrollback.escritas', 'scrollback.esperada_primera_linea']) expect(etiquetas[calculada], calculada).toMatch(/^HARNESS-COMPUTED/);
    expect(etiquetas['ajuste.no_caben']).toMatch(/^MEASURED/);
  });

  it('refuses a bad command line with rc 2 and etapa argumentos, and accepts the oracle\'s', () => {
    const rechazo = (argv: string[]): { rc?: number; etapa?: string; message: string } => {
      try {
        leerArgumentos(argv);
      } catch (e) {
        return e as { rc?: number; etapa?: string; message: string };
      }
      throw new Error(`leerArgumentos accepted ${JSON.stringify(argv)}`);
    };
    for (const [argv, mensaje] of [
      [[], /--paneles N is required/],
      [['--paneles', '1'], /2\.\.200/],
      [['--paneles', 'x'], /2\.\.200/],
      [['--paneles', '4', '--renderer', 'svg'], /dom, canvas or webgl/],
      [['--paneles', '4', '--bogus'], /unknown argument --bogus/],
      [['--paneles', '4', '--scrollback-pedido', '-1'], /non-negative integer/],
      [['--paneles'], /needs a value/],
      [['--paneles', '4', '--inyectar-scrollback', 'srf-ng-9=5'], /not an interactive panel/],
      [['--paneles', '4', '--inyectar-scrollback', 'nonsense'], /needs ID=N/],
      [['--paneles', '4', '--plazo-total-ms', '-1'], /non-negative number/],
    ] as const) {
      const e = rechazo([...argv]);
      expect(e.rc, JSON.stringify(argv)).toBe(2);
      expect(e.etapa, JSON.stringify(argv)).toBe('argumentos');
      expect(e.message, JSON.stringify(argv)).toMatch(mensaje);
    }
    expect(leerArgumentos(['--paneles', '56', '--json'])).toMatchObject({ paneles: 56, renderer: 'dom', json: true, scrollbackPedido: 2000 });
    expect(leerArgumentos(['--paneles', '56', '--renderer', 'canvas', '--json'])).toMatchObject({ renderer: 'canvas' });
    expect(leerArgumentos(['--paneles', '56', '--json'])).toMatchObject({ inyectarScrollback: null, inyectarCongelacion: false, plazoLectura: 5_000, plazoTotal: 300_000 });
    expect(leerArgumentos(['--paneles', '12', '--inyectar-scrollback', 'srf-ng-9=50000', '--inyectar-congelacion'])).toMatchObject({ inyectarScrollback: { id: 'srf-ng-9', indice: 9, n: 50_000 }, inyectarCongelacion: true });
    expect(informeVacio().ok).toBe(false);
  });
});

// ---- the drawer in a real headless Chromium, through the instrument -----------------------------------------------------------
const INSTRUMENTO = join(RAIZ_REPO, 'scripts', 'ng', 'medir-terminal.mjs');
type Informe = Record<string, any>;
interface Medicion {
  rc: number | null;
  stdout: string;
  stderr: string;
  informe: Informe;
}

function ejecutarProceso(args: string[], entorno: Record<string, string>, plazoMs = 170_000): Promise<{ rc: number | null; stdout: string; stderr: string }> {
  return new Promise((resolver) => {
    execFile(process.execPath, [INSTRUMENTO, ...args], { cwd: RAIZ_REPO, env: { ...process.env, ...entorno }, timeout: plazoMs, killSignal: 'SIGTERM', maxBuffer: 64 << 20 }, (error, stdout, stderr) => {
      const codigo = (error as (Error & { code?: unknown }) | null)?.code;
      resolver({ rc: error === null ? 0 : typeof codigo === 'number' ? codigo : null, stdout, stderr });
    });
  });
}

/** One run of the instrument as a subprocess, exactly as the oracle runs it; its stdout must always be ONE JSON report. */
async function medir(args: string[], entorno: Record<string, string> = {}, plazoMs?: number): Promise<Medicion> {
  const p = await ejecutarProceso(args, entorno, plazoMs);
  let informe: Informe;
  try {
    informe = JSON.parse(p.stdout);
  } catch {
    throw new Error(`medir-terminal wrote no single JSON report on stdout (rc ${p.rc}):\nstdout: ${p.stdout}\nstderr: ${p.stderr}`);
  }
  return { ...p, informe };
}

const memo = new Map<string, Promise<Medicion>>();
/** A measurement of the tree; the same arguments are measured once per run of this file. */
function real(...extra: string[]): Promise<Medicion> {
  const clave = extra.join(' ');
  let m = memo.get(clave);
  if (m === undefined) {
    m = medir([...extra, '--json']);
    memo.set(clave, m);
  }
  return m;
}
const ORACULO = ['--paneles', '56'];
const JQ_ORACULO = '.renderer=="dom" and .remontajes_max<=1 and .scrollback<=2000 and .altura_cajon==240';

/** The exit status of `jq -e <filtro>` on a report: how the oracle judges it (0 true, 1 false or null). */
function jq(filtro: string, texto: string): number | null {
  return spawnSync('jq', ['-e', filtro], { input: texto }).status;
}

/** The nested key structure of a report (arrays and scalars are leaves): the "same shape" of every path. */
const MAPAS_POR_PANEL = new Set(['variantes_solicitadas', 'variantes_rechazadas', 'montajes_por_panel', 'montajes_por_panel_componente', 'montajes_por_panel_callback', 'reconexiones_por_panel', 'conexiones_aceptadas_por_panel', 'fases_tras_reconectar', 'paneles_por_fase']);
function forma(v: unknown): unknown {
  if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
    // a map keyed by panel id (or by phase) has the keys of the run, not of the report: it is a leaf
    return Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([k, x]) => [k, MAPAS_POR_PANEL.has(k) ? 'hoja' : forma(x)]));
  }
  return 'hoja';
}

describe('the drawer in a real browser (scripts/ng/medir-terminal.mjs)', () => {
  it('oracle: 56 terminals on the DOM renderer, mounted once each, scrollback within the limit, drawer 240 px, every source agreeing', async () => {
    const m = await real(...ORACULO);
    const r = m.informe;
    expect(m.rc, m.stderr).toBe(0);
    expect(r.ok).toBe(true);
    expect(r.etapa).toBe('completa');
    expect(jq(JQ_ORACULO, m.stdout)).toBe(0); // the oracle's own predicate
    expect(r.acuerdo_entre_fuentes).toBe(true);
    expect(r.desacuerdos).toEqual([]);
    expect(r.dom).toBe('1271x1064');
    expect(r).toMatchObject({ renderer: 'dom', renderer_pedido: 'dom', paneles: 56, paneles_interactivos: 56, paneles_texto: 0, remontajes_max: 0, montajes_max: 1, remontajes_total: 0, scrollback: 2000, altura_cajon: 240, addons_solicitados: 0, rechazos_de_addon: 0, activaciones_de_addon: 0 });
    // the renderer, from three readings of the same DOM
    expect(r.primera_fuente).toMatchObject({ renderer: 'dom', lienzos_en_paneles: 0, lienzos_fuera_de_la_pantalla: 0 });
    expect(r.segunda_fuente).toMatchObject({ renderer_componente: 'dom', renderer_cdp: 'dom', filas_dom_cdp: 56, lienzos_cdp: 0, lienzos_cdp_en_paneles: 0, paneles_cdp: 56, conexiones_servidor: 56 });
    // the mounts, from the DOM observer and from the component, per panel
    const montajes = Object.values(r.primera_fuente.montajes_por_panel);
    expect(montajes).toHaveLength(56);
    expect(new Set(montajes)).toEqual(new Set([1]));
    expect(r.segunda_fuente.montajes_por_panel_componente).toEqual(r.primera_fuente.montajes_por_panel);
    expect(r.segunda_fuente.montajes_por_panel_callback).toEqual(r.primera_fuente.montajes_por_panel); // and the alMontar callbacks the harness received
    expect(r.segunda_fuente.inserciones_xterm_cdp).toBe(0);
    expect(r.segunda_fuente.inserciones_xterm_pagina_en_la_ventana).toBe(0);
    // through the churn: collapse cycles, every tab, a replaced array, two cut streams
    expect(r.tramos.montaje.paneles_listos).toBe(56);
    expect(r.tramos.churn).toMatchObject({ ciclos: 3, pestanas_activadas: 56, arrays_reemplazados: 1, flujos_cortados: 2, montajes_max_despues: 1 });
    expect(r.tramos.churn.reconexiones_por_panel).toEqual({ 'srf-ng-3': 1, 'srf-ng-17': 1 });
    expect(r.tramos.churn.conexiones_aceptadas_por_panel).toEqual({ 'srf-ng-3': 2, 'srf-ng-17': 2 });
    expect(r.tramos.churn.fases_tras_reconectar).toEqual({ 'srf-ng-3': 'abierto', 'srf-ng-17': 'abierto' });
    expect(r.servidor).toMatchObject({ conexiones_aceptadas: 58, tramas_invalidas_recibidas: 0, errores_de_socket: [] });
    expect(r.errores_de_cierre).toEqual([]); // nothing failed while the browser and the servers were closed
    // bytes typed reach the far side of the interface decoded, and come back
    expect(r.tramos.entrada).toMatchObject({ escrito: 'echo hola\r', recibido_por_el_servidor: 'echo hola\r', tramas_invalidas_en_el_servidor: 0, eco_en_el_terminal: true, tramas_enviadas_por_el_cliente: 10, tramas_recibidas_por_el_servidor: 10 });
  });

  it('contrast: asking every terminal for a renderer addon is refused by the guard, the run completes and still reports dom', async () => {
    const bueno = await real(...ORACULO);
    const m = await real(...ORACULO, '--renderer', 'canvas');
    const r = m.informe;
    // it fails for the right reason: the measurement completed, every source agrees, the request was made and refused
    expect(m.rc, m.stderr).toBe(0);
    expect(r.ok).toBe(true);
    expect(r.acuerdo_entre_fuentes).toBe(true);
    expect(r.renderer_pedido).toBe('canvas');
    expect(r.addons_solicitados).toBe(56);
    expect(r.rechazos_de_addon).toBe(56);
    expect(r.segunda_fuente.rechazos_consola).toBe(56);
    expect(r.consola_avisos.every((t: string) => /^termguard: refused .+: this drawer loads no addon it did not create itself/.test(t))).toBe(true);
    expect(r.renderer).toBe('dom');
    expect(r.primera_fuente.renderer).toBe('dom');
    expect(r.segunda_fuente).toMatchObject({ renderer_componente: 'dom', renderer_cdp: 'dom', filas_dom_cdp: 56, lienzos_cdp: 0 });
    expect(jq('.renderer=="canvas"', m.stdout)).toBe(1); // the contrast literal is false
    expect(jq(JQ_ORACULO, m.stdout)).toBe(0); // and nothing else about the run is broken
    expect(r.remontajes_max).toBe(bueno.informe.remontajes_max);
    expect(r.altura_cajon).toBe(240);
    expect(r.scrollback).toBe(2000);
  });

  it('refuses each requested addon once, by the name it carries, and none of them ran: requests, refusals, console and the stand-ins\' own counter agree', async () => {
    const r = (await real(...ORACULO, '--renderer', 'canvas')).informe;
    expect(r.activaciones_de_addon).toBe(0); // the stand-ins count their own activate(): none was called
    expect(r.variantes_solicitadas).toEqual({ control: 1, minificada: 14, anonima: 14, subclase: 14, renombrada: 13 });
    expect(r.variantes_rechazadas).toEqual({ CanvasAddon: 1, a: 14, '(anonymous)': 14, Marcador: 14, Interno: 13 });
    // 55 of the 56 refusals concern a class whose name is on no deny-list: only the control is named as the real one
    const sinNombreDeRenderer = Object.entries(r.variantes_rechazadas as Record<string, number>).filter(([nombre]) => nombre !== 'CanvasAddon').reduce((t, [, n]) => t + n, 0);
    expect(sinNombreDeRenderer).toBe(55);
    expect(r.primera_fuente).toMatchObject({ lienzos_en_paneles: 0, lienzos_fuera_de_la_pantalla: 0, rechazos_de_addon: 56 });
    expect(r.segunda_fuente).toMatchObject({ lienzos_cdp_en_paneles: 0, filas_dom_cdp: 56 });
    // the same with the WebGL stand-ins, on a smaller drawer
    const w = (await real('--paneles', '12', '--renderer', 'webgl')).informe;
    expect(w).toMatchObject({ ok: true, renderer: 'dom', renderer_pedido: 'webgl', addons_solicitados: 12, rechazos_de_addon: 12, activaciones_de_addon: 0 });
    expect(w.variantes_rechazadas).toMatchObject({ WebglAddon: 1, a: 3, '(anonymous)': 3, Marcador: 3, Interno: 2 });
  });

  it('degrades every terminal past the 56th to a plain-text pane that shows its stream', async () => {
    const m = await real('--paneles', '60');
    const r = m.informe;
    expect(m.rc, m.stderr).toBe(0);
    expect(r.ok).toBe(true);
    expect(r).toMatchObject({ paneles: 60, paneles_interactivos: 56, paneles_texto: 4, paneles_texto_con_contenido: 4, renderer: 'dom', remontajes_max: 0 });
    expect(r.segunda_fuente).toMatchObject({ filas_dom_cdp: 56, paneles_cdp: 60, conexiones_servidor: 60 });
    expect(r.tramos.texto).toMatchObject({ paneles: 4, con_contenido_tras_activar: 4, primera_muestra: 'srf stand-in srf-ng-56 ready\n$ ' });
  });

  it('keeps at most 2,000 lines in a plain-text pane: more were written, the oldest were dropped, and the first line kept is the one the arithmetic gives', async () => {
    const t = (await real('--paneles', '60')).informe.tramos.texto.tope;
    expect(t.escritas).toBe(2_600);
    expect(t.lineas_conservadas).toBe(2_000);
    expect(t.numero_primera).toBe(601); // line 601 of 2,600, read from the pane's own text
    expect(t.esperada_primera).toBe(601);
    expect(t.coincide).toBe(true);
  });

  it('is a 240 px overlay: 12 px when collapsed, and the surface behind it keeps the canvas size at every step', async () => {
    const r = (await real(...ORACULO)).informe;
    const c = r.tramos.cajon;
    expect(c).toMatchObject({ abierto_altura: 240, plegado_altura: 12, reabierto_altura: 240, plegado_altura_cdp: 12, cuerpo_altura: 212, estado_plegado: 'plegado' });
    const lienzo = `${LIENZO_W}x${LIENZO_H}`; // the U-18 canvas, from geometria.ts
    expect(c.superficie_antes).toBe(lienzo);
    expect(c.superficie_plegado).toBe(lienzo);
    expect(c.superficie_despues).toBe(lienzo);
    expect(c.superficie_cdp_antes).toBe(lienzo);
    expect(c.superficie_cdp_plegado).toBe(lienzo);
    expect(r.primera_fuente.altura_cajon).toBe(r.segunda_fuente.altura_cajon_cdp);
  });

  it('queues the bytes of a collapsed drawer without scheduling a frame, pauses the server at the high-water mark, and drains on reopening', async () => {
    const o = (await real(...ORACULO)).informe.tramos.oculto;
    expect(o.cuadros_de_pagina).toBe(0); // no requestAnimationFrame anywhere in the page while hidden
    expect(o.cuadros_del_panel).toBe(0);
    expect(o.lineas_nuevas_en_el_buffer).toBe(0); // nothing was handed to xterm
    expect(o.bytes_en_cola_al_final).toBeGreaterThan(o.alta_marca);
    expect(o.alta_marca).toBe(ALTA_MARCA_BYTES);
    expect(o.pausas_cliente).toBe(1);
    expect(o.pausas_servidor).toBe(1);
    expect(o.bytes_retenidos_por_el_servidor).toBeGreaterThan(0); // the server really held bytes back
    expect(o.bytes_enviados_por_el_servidor + o.bytes_retenidos_por_el_servidor).toBe(o.bytes_ofrecidos_al_servidor);
    expect(o.cuadros_de_pagina_al_reabrir).toBeGreaterThan(0);
    // sustained load can pause the stream again after a resume; whatever the count, both ends agree and every pause was resumed
    expect(o.pausas_al_final_cliente).toBeGreaterThanOrEqual(1);
    expect(o.pausas_al_final_cliente).toBe(o.pausas_al_final_servidor);
    expect(o.reanudaciones_cliente).toBe(o.reanudaciones_servidor);
    expect(o.reanudaciones_cliente).toBe(o.pausas_al_final_cliente);
    expect(o.bytes_recibidos_por_el_cliente).toBe(o.bytes_enviados_al_final); // client and server counted the same stream
    expect(o.cola_al_final).toBe(0);
  });

  it('queues a tab that is not the active one the same way, and drains it when it is activated', async () => {
    const i = (await real(...ORACULO)).informe.tramos.inactivo;
    expect(i.cuadros_del_panel).toBe(0);
    expect(i.cuadros_de_pagina).toBe(0);
    expect(i.bytes_en_cola).toBeGreaterThanOrEqual(10_500);
    expect(i.cuadros_tras_activar).toBeGreaterThan(0);
    expect(i.cola_tras_activar).toBe(0);
  });

  it('wins the focus back three times from an element that steals it twice', async () => {
    const f = (await real(...ORACULO)).informe.tramos.foco;
    expect(f.pasos_declarados).toEqual([0, 120, 400]);
    expect(f.pasadas_del_panel).toBe(3);
    expect(f.intrusos).toBe(2);
    // what the browser itself saw: the panel's textarea, the intruder, the panel, the intruder, the panel
    expect(f.secuencia).toEqual(['panel', 'intruso', 'panel', 'intruso', 'panel']);
    expect(f.foco_final_en_el_panel).toBe(true);
  });

  it('swallows the right button at capture phase and lets the left one through, and delivers a composed character once with the textarea cleared', async () => {
    const r = (await real(...ORACULO)).informe.tramos;
    expect(r.raton.derecho_llegado_al_destino).toBe(0);
    expect(r.raton.derecho_real_llegado_al_destino).toBe(0);
    expect(r.raton.derecho_cancelado).toBe(true);
    expect(r.raton.izquierdo_llegado_al_destino).toBeGreaterThan(0);
    expect(r.ime.recibido_por_el_servidor).toBe('ñ');
    expect(r.ime.textarea_tras_la_composicion).toBe('');
  });

  it('retains at most 2,000 lines whatever is requested: the buffer, the first retained line and the option agree', async () => {
    const s = (await real(...ORACULO)).informe;
    expect(s.scrollback).toBe(2000);
    expect(s.scrollback_opcion).toBe(2000);
    expect(s.tramos.scrollback).toMatchObject({ retenidas: 2000, opcion: 2000, coincide: true, bytes_cliente: s.tramos.scrollback.bytes_servidor });
    expect(s.tramos.scrollback.numero_primera_linea).toBe(s.tramos.scrollback.escritas + 2 - (s.tramos.scrollback.filas + 2000));
    expect(s.primera_fuente.retenidas).toBe(2000);
    expect(s.segunda_fuente.primera_linea_numero).toBe(s.tramos.scrollback.numero_primera_linea);
    // asked for 5,000, the guard still gives 2,000 (measured on the buffer after writing more than 5,000 lines)
    const m = await real('--paneles', '8', '--scrollback-pedido', '5000');
    expect(m.rc, m.stderr).toBe(0);
    expect(m.informe.scrollback_pedido).toBe(5000);
    expect(m.informe.scrollback).toBe(2000);
    expect(m.informe.scrollback_opcion).toBe(2000);
    expect(m.informe.tramos.scrollback.escritas).toBeGreaterThan(5_000);
    expect(jq('.scrollback<=2000', m.stdout)).toBe(0);
  });

  it('hands a backlog to xterm in bounded micro-batches: no write carries more than the bound, and every byte that arrived was written', async () => {
    const o = (await real(...ORACULO)).informe.tramos.oculto;
    expect(o.lote_max_declarado).toBe(LOTE_MAX_DECLARADO);
    expect(LOTE_MAX_BYTES).toBeLessThanOrEqual(LOTE_MAX_DECLARADO); // the client's own bound, against the instrument's declared one
    expect(o.escrituras_al_reabrir).toBeGreaterThan(20); // a backlog of several MiB takes many writes
    expect(o.lote_max_bytes).toBeGreaterThan(0);
    expect(o.lote_max_bytes).toBeLessThanOrEqual(LOTE_MAX_DECLARADO); // measured at the write() call of the terminal
    expect(o.bytes_escritos_a_xterm).toBe(o.bytes_recibidos_por_el_cliente);
  });

  it('fits every terminal to its host and tells the PTY the size it fitted to: rows fill the body to within one row, and the RESIZE at the server is the terminal\'s size', async () => {
    const a = (await real(...ORACULO)).informe.tramos.ajuste;
    expect(a.paneles_medidos).toBe(56);
    expect(a.no_caben).toBe(0); // no terminal overflows its host
    expect(a.no_llenan).toBe(0); // and none leaves a whole row unused
    expect(a.sin_resize_coherente).toBe(0); // what the server decoded is what the terminal is
    // three readings of one height: the host's box, a row's box and xterm's own row count
    expect(a.filas_min).toBe(Math.floor(a.anfitrion_alto / a.fila_alto));
    expect(a.filas_max).toBe(a.filas_min);
    expect(a.columnas_min).toBeGreaterThan(80); // 80 is what a terminal that was never fitted has
  });

  it('writes the [process exited] line into the terminal on EXIT, ends the session for good and does not reconnect', async () => {
    const x = (await real(...ORACULO)).informe.tramos.salida;
    expect(x).toMatchObject({ codigo_enviado: 7, linea_en_el_buffer: true, linea_en_el_dom: true, fase_final: 'cerrado', conexiones_aceptadas_antes: 1, conexiones_aceptadas_despues: 1 });
  });

  it('moves between tabs with the arrow keys and wraps around: ArrowRight goes to the next tab, ArrowLeft to the previous one', async () => {
    const t = (await real(...ORACULO)).informe.tramos.pestanas;
    // from the first of 56 tabs: right, right, left, left, left (which wraps to the last)
    const esperada = ['srf-ng-1', 'srf-ng-2', 'srf-ng-1', 'srf-ng-0', 'srf-ng-55'];
    expect(t.teclas).toEqual(['ArrowRight', 'ArrowRight', 'ArrowLeft', 'ArrowLeft', 'ArrowLeft']);
    expect(t.secuencia_dom).toEqual(esperada); // aria-selected in the DOM
    expect(t.secuencia_estado).toEqual(esperada); // the state bound to the drawer
    expect(t.coincide).toBe(true);
  });

  it('removing an agent unmounts its panel and closes its WebSocket, seen from both ends of the stream', async () => {
    const d = (await real(...ORACULO)).informe.tramos.desmontaje;
    expect(d).toMatchObject({ panel: 'srf-ng-55', panel_fuera_del_dom: true, desmontado_por_callback: true, sockets_abiertos_antes: 1, sockets_abiertos_despues: 0, conexiones_abiertas_antes: 1, conexiones_abiertas_despues: 0 });
  });

  it('fails a panel whose terminal grows a canvas layer after the mount, and closes its stream (line 2 of the guard)', async () => {
    const c = (await real(...ORACULO)).informe.tramos.capa_tardia;
    expect(c).toMatchObject({ panel: 'srf-ng-4', fase_final: 'fallido', terminal_desmontado: true, conexion_cerrada_en_el_servidor: true });
    expect(c.aviso_visible).toMatch(/canvas layer appeared/);
  });

  it('survives a control frame that is JSON but not an object: counted once, dropped, and no uncaught page error', async () => {
    const h = (await real(...ORACULO)).informe.tramos.hostil;
    expect(h.tramas_invalidas_despues - h.tramas_invalidas_antes).toBe(1);
    expect(h.errores_de_pagina_despues).toBe(h.errores_de_pagina_antes);
    expect(h.errores_de_pagina_despues).toBe(0);
  });

  it('takes the scrollback from ALL 56 terminals: each retains exactly its limit, and ONE terminal with a 50,000-line buffer turns the field and the oracle red', async () => {
    const buena = (await real(...ORACULO)).informe;
    expect(buena.tramos.scrollback.todas).toMatchObject({ terminales: 56, retenidas_max: 2_000, retenidas_min: 2_000, opcion_max: 2_000, atributo_max: 2_000, coinciden: 56 });
    expect(buena.scrollback).toBe(buena.tramos.scrollback.todas.retenidas_max);
    const m = await real('--paneles', '12', '--inyectar-scrollback', 'srf-ng-9=50000');
    const r = m.informe;
    expect(m.rc, m.stderr).toBe(0); // the measurement completed: the defect is a value, and the consumer judges it
    expect(r.ok).toBe(true);
    expect(r.inyeccion).toEqual({ scrollback: 'srf-ng-9=50000', congelacion: false });
    expect(r.tramos.scrollback.todas).toMatchObject({ terminales: 12, retenidas_min: 2_000, mayor: 'srf-ng-9', opcion_max: 50_000, atributo_max: 50_000, coinciden: 12 });
    expect(r.scrollback).toBeGreaterThan(2_000);
    expect(r.scrollback).toBe(r.tramos.scrollback.todas.retenidas_max);
    expect(jq('.scrollback<=2000', m.stdout)).toBe(1); // the field is red
    expect(jq(JQ_ORACULO, m.stdout)).toBe(1); // and so is the oracle's own predicate
  });

  it('ends a frozen page with a report inside its own bound: at 56 panels, ok false, etapa listo, rc 4, an error that says why, the oracle fields null', async () => {
    const m = await medir(['--paneles', '56', '--inyectar-congelacion', '--espera-listo-ms', '4000', '--plazo-lectura-ms', '1500', '--json'], {}, 90_000);
    expect(m.rc, m.stderr).toBe(4);
    expect(m.informe).toMatchObject({ ok: false, etapa: 'listo', rc: 4, inyeccion: { scrollback: null, congelacion: true }, plazos: { espera_listo_ms: 4_000, plazo_lectura_ms: 1_500 } });
    expect(m.informe.error).toMatch(/never set data-app-ready.*the page could not be read either: reading the panels timed out after 1500 ms/);
    expect(m.informe.duracion_ms).toBeLessThan(4_000 + 2 * 1_500 + 15_000 + 30_000); // the ready wait, the two read deadlines, the bound on closing the browser, and the build and launch
    for (const campo of ['renderer', 'remontajes_max', 'scrollback', 'altura_cajon', 'paneles']) expect(m.informe[campo], campo).toBeNull();
    expect(jq(JQ_ORACULO, m.stdout)).toBe(1);
    expect(forma(m.informe)).toEqual(forma((await real(...ORACULO)).informe));
  });

  it('labels every value under `tramos` of a real report with its provenance', async () => {
    const r = (await real(...ORACULO)).informe;
    const hojas: string[] = [];
    const visitar = (n: unknown, ruta: string): void => {
      if (n !== null && typeof n === 'object' && !Array.isArray(n) && Object.keys(n).length > 0 && !MAPAS_POR_PANEL.has(ruta.split('.').at(-1)!)) for (const [k, v] of Object.entries(n)) visitar(v, ruta === '' ? k : `${ruta}.${k}`);
      else hojas.push(ruta);
    };
    visitar(r.tramos, '');
    expect(Object.keys(r.procedencia.tramos).sort()).toEqual(hojas.sort());
    for (const [hoja, etiqueta] of Object.entries(r.procedencia.tramos as Record<string, string>)) expect(etiqueta, hoja).toMatch(/^(MEASURED|READ-BACK|DECLARED|HARNESS-COMPUTED)\b/);
  });

  it('writes ONE report of the same shape on every failure path, with the oracle fields null and the oracle predicate red', async () => {
    const buena = await real(...ORACULO);
    const casos: Array<{ nombre: string; args: string[]; env?: Record<string, string>; rc: number; etapa: string }> = [
      { nombre: 'no panel count', args: ['--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'one panel', args: ['--paneles', '1', '--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'unknown renderer', args: ['--paneles', '4', '--renderer', 'svg', '--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'unknown flag', args: ['--paneles', '4', '--bogus', '--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'no browser', args: ['--paneles', '4', '--json'], env: { PLAYWRIGHT_BROWSERS_PATH: '/nonexistent-srf-u21' }, rc: 1, etapa: 'navegador' },
      { nombre: 'page not ready in time', args: ['--paneles', '4', '--espera-listo-ms', '1', '--json'], rc: 4, etapa: 'listo' },
    ];
    for (const c of casos) {
      const m = await medir(c.args, c.env ?? {});
      expect(m.rc, c.nombre).toBe(c.rc);
      expect(m.informe.rc, c.nombre).toBe(c.rc);
      expect(m.informe.ok, c.nombre).toBe(false);
      expect(m.informe.etapa, c.nombre).toBe(c.etapa);
      expect(typeof m.informe.error, c.nombre).toBe('string');
      expect(m.stderr, c.nombre).toMatch(/^medir-terminal: /);
      expect(m.stdout.trim().split('\n'), c.nombre).toHaveLength(1);
      expect(forma(m.informe), c.nombre).toEqual(forma(buena.informe));
      for (const campo of ['renderer', 'remontajes_max', 'scrollback', 'altura_cajon', 'paneles']) expect(m.informe[campo], `${c.nombre}: ${campo}`).toBeNull();
      // a consumer that keeps the exit status cannot mistake a failure for a pass, and the predicate alone is red too
      expect(jq(JQ_ORACULO, m.stdout), c.nombre).toBe(1);
    }
  });

  /**
   * Starts a run, kills it with SIGTERM at the given moment and audits what it leaves behind. Before the browser exists
   * only the instrument's own handler can clean up; once it runs, the handler closes the browser too.
   */
  async function matarUnaEjecucion(momento: 'al crearse su raiz' | 'con el navegador lanzado'): Promise<{ rc: number | null; raiz: string }> {
    const antes = new Set(readdirSync(tmpdir()).filter((n) => n.startsWith('srf-medir-terminal-')));
    const hijo = spawn(process.execPath, [INSTRUMENTO, '--paneles', '8', '--json'], { cwd: RAIZ_REPO, stdio: ['ignore', 'pipe', 'pipe'] });
    const salida = new Promise<number | null>((ok) => hijo.on('exit', (codigo) => ok(codigo)));
    let raiz = '';
    const limite = Date.now() + 90_000;
    while (Date.now() < limite && raiz === '') {
      await dormir(momento === 'al crearse su raiz' ? 5 : 100);
      const nueva = readdirSync(tmpdir()).find((n) => n.startsWith('srf-medir-terminal-') && !antes.has(n));
      if (nueva === undefined) continue;
      const candidata = join(tmpdir(), nueva);
      // the browser has launched once it has written its profile under the run's private TMPDIR
      if (momento === 'al crearse su raiz' || (existsSync(join(candidata, 'tmp')) && readdirSync(join(candidata, 'tmp')).length > 0)) raiz = candidata;
    }
    expect(raiz, `the instrument never reached the moment "${momento}"`).not.toBe('');
    hijo.kill('SIGTERM');
    return { rc: await salida, raiz };
  }

  it.each(['al crearse su raiz', 'con el navegador lanzado'] as const)('cleans up when it is killed by SIGTERM %s: the private root and the browser go, the exit status is 143', async (momento) => {
    const { rc, raiz } = await matarUnaEjecucion(momento);
    expect(rc).toBe(143);
    expect(existsSync(raiz)).toBe(false);
    const procesos = spawnSync('ps', ['-eo', 'args']).stdout.toString();
    expect(procesos.split('\n').filter((l) => l.includes(raiz))).toEqual([]);
  });

  it('leaves no temporary directory behind, on the good runs or the failing ones', () => {
    const restos = readdirSync(tmpdir()).filter((n) => n.startsWith('srf-medir-terminal-'));
    expect(restos).toEqual([]);
  });
});
