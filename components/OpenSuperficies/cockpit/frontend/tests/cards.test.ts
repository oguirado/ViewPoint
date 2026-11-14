// Tests of the capsule cards (unit U-20): the logic of visor.ts, the four components in a real browser, the AAA token audit
// and the hot-switch instrument. Every expectation is read from a source OTHER than the code under test (the specification
// text, app.css, another process, the far side of an interface, a browser), so a drift in any one place turns a test red.
// evidencia/v2/u20/mutaciones-1.txt records, for each test, the production line that was neutered in a scratch copy and the
// red it produced. Temporary files live in one directory that afterAll removes.

import { execFile, spawnSync } from 'node:child_process';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';

import { LIENZO_H, LIENZO_H_ALTO, LIENZO_W } from '../src/lib/geometria';
import {
  ARQUETIPOS,
  AUTO_OCULTAR_MS,
  LIMITE_CONMUTACION_MS,
  PASOS_FOCO_MS,
  SANDBOX_XPRA,
  SONDEO_MS,
  TOKENS_DE_TEXTO,
  ZONA_HOVER_PX,
  cabeceraVisible,
  crearAdaptadorXpra,
  crearConmutacion,
  debeActivar,
  formatoCpu,
  formatoMemoria,
  formatoPuertos,
  franjaVisible,
  insigniaAislamiento,
  insigniaEscala,
  insigniaModo,
  insigniaSalud,
  pildorasGates,
  tamanoLienzo,
  transferirFoco,
  urlXpra,
} from '../src/lib/cards/visor';
import type { Insignia } from '../src/lib/cards/visor';
import {
  analizarTokens,
  auditarTokens,
  colorDeToken,
  construirHarness,
  dependenciasReales,
  ejecutar,
  informeVacio,
  razonContraste,
  servirHarness,
  sondearHttp,
  tokensDeApp,
} from '../../../scripts/ng/medir-contraste.mjs';

const RAIZ_FRONTEND = fileURLToPath(new URL('..', import.meta.url));
const RAIZ_REPO = fileURLToPath(new URL('../../../', import.meta.url));
const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');

const ESPEC = leer('../../../ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md');
const APP_CSS = leer('../src/app.css');
const DEGRADADOS = leer('../../../fixture/ng/tokens/degradados.css');
const DIR_CARDS = fileURLToPath(new URL('../src/lib/cards/', import.meta.url));
const FUENTES_CARDS = readdirSync(DIR_CARDS).map((n) => ({ nombre: n, texto: readFileSync(join(DIR_CARDS, n), 'utf8') }));

type Cualquiera = any; // eslint-disable-line @typescript-eslint/no-explicit-any

/** A section of the specification, from its heading to the next heading of the same or a higher level. */
function seccion(titulo: string): string {
  const inicio = ESPEC.indexOf(titulo);
  if (inicio < 0) throw new Error(`specification section not found: ${titulo}`);
  const resto = ESPEC.slice(inicio + titulo.length);
  const nivel = /^#+/.exec(titulo)![0].length;
  const fin = new RegExp(`\\n#{1,${nivel}} `).exec(resto);
  return fin === null ? resto : resto.slice(0, fin.index);
}

/** The backticked values of the row of a markdown table that starts with `| <etiqueta> |`. */
function valoresDeFila(tabla: string, etiqueta: string): string[] {
  const fila = tabla.split('\n').find((l) => l.toLowerCase().startsWith(`| ${etiqueta.toLowerCase()} |`));
  if (fila === undefined) throw new Error(`row not found: ${etiqueta}`);
  return [...(fila.split('|')[2] ?? '').matchAll(/`([^`]+)`/g)].map((m) => m[1]);
}

// ---- a fake clock: timers fire only when the test advances time ------------------------------------------------
function relojFalso() {
  let t = 0;
  const pendientes: Array<{ en: number; ok: () => void }> = [];
  const vaciar = () => new Promise<void>((r) => setImmediate(r));
  return {
    ahora: () => t,
    esperar: (ms: number) => new Promise<void>((ok) => pendientes.push({ en: t + ms, ok })),
    async avanzar(ms: number): Promise<void> {
      const objetivo = t + ms;
      await vaciar();
      for (;;) {
        pendientes.sort((a, b) => a.en - b.en);
        const siguiente = pendientes[0];
        if (siguiente === undefined || siguiente.en > objetivo) break;
        pendientes.shift();
        t = Math.max(t, siguiente.en);
        siguiente.ok();
        await vaciar();
      }
      t = objetivo;
      await vaciar();
    },
  };
}

function diferido<T>() {
  let ok!: (v: T) => void;
  let mal!: (e: unknown) => void;
  const promesa = new Promise<T>((a, b) => {
    ok = a;
    mal = b;
  });
  return { promesa, ok, mal };
}

// =====================================================================================================================
// visor.ts: what the specification fixes
// =====================================================================================================================

describe('canvas, frame and Xpra URL (spec 3.2)', () => {
  it('gives every card the fixed canvas of the specification: 1264 x 1024, 1264 x 2080 in the 1/3 tall column', () => {
    const m = /fixed CSS size (\d+) × (\d+) or (\d+) × (\d+)/.exec(ESPEC);
    expect(m).not.toBeNull();
    const [, w1, h1, w2, h2] = m!.map(Number);
    expect(tamanoLienzo('tile_1_6')).toEqual({ w: w1, h: h1 });
    expect(tamanoLienzo('column_1_3_dual')).toEqual({ w: w1, h: h1 });
    expect(tamanoLienzo('column_1_3_tall')).toEqual({ w: w2, h: h2 });
    expect([LIENZO_W, LIENZO_H, LIENZO_H_ALTO]).toEqual([w1, h1, h2]);
  });

  it('carries the sandbox of the specification and nothing else', () => {
    const m = /<iframe sandbox="([^"]+)">/.exec(ESPEC);
    expect(m).not.toBeNull();
    expect(SANDBOX_XPRA).toBe(m![1]);
    expect(SANDBOX_XPRA.split(' ').sort()).toEqual(['allow-pointer-lock', 'allow-same-origin', 'allow-scripts']);
  });

  it('builds a loopback Xpra URL with the clipboard switched off at the client-URL layer, and refuses a non-port', () => {
    const u = new URL(urlXpra(10026));
    expect([u.protocol, u.hostname, u.port]).toEqual(['http:', '127.0.0.1', '10026']);
    for (const p of ['clipboard', 'printing', 'file_transfer', 'sound', 'floating_menu']) expect(u.searchParams.get(p), p).toBe('false');
    expect(ESPEC).toContain('the clipboard is disabled at server, client URL, display and prompt layers');
    for (const malo of [0, -1, 65536, 1.5, Number.NaN]) expect(() => urlXpra(malo), String(malo)).toThrow(RangeError);
  });
});

describe('the four archetypes (spec 3, table)', () => {
  const tabla = seccion('## 3. Capsule Cards');
  const fila = (nombre: string): string[] => {
    const l = tabla.split('\n').find((x) => x.startsWith(`| ${nombre} |`));
    if (l === undefined) throw new Error(`archetype row not found: ${nombre}`);
    return l.split('|').map((c) => c.trim());
  };
  const casos: Array<[keyof typeof ARQUETIPOS, string, string, RegExp]> = [
    ['browser', 'Browser', 'docker_l2', /iframe/],
    ['desktop', 'Desktop', 'docker_l2', /Xpra/],
    ['microvm', 'MicroVM', 'kvm_hardware', /guest Xpra/],
    ['terminal', 'Terminal', 'pty_jaula', /xterm\.js/],
  ];

  for (const [clave, nombre, tier, ruta] of casos) {
    it(`${nombre}: isolation, render path and control-latency target match the specification row`, () => {
      const celdas = fila(nombre);
      const d = ARQUETIPOS[clave];
      expect(d.arquetipo).toBe(clave);
      expect(d.etiqueta).toBe(nombre);
      expect(d.tier).toBe(tier);
      expect(celdas[3]).toMatch(ruta); // render path column
      expect(d.ruta).toBe(clave === 'terminal' ? 'xterm_dom' : 'xpra_iframe');
      expect(d.latenciaObjetivoMs).toBe(Number(/≤\s*(\d+)\s*ms/.exec(celdas[6])![1]));
      expect(d.cdp).toBe(/authenticated CDP/.test(celdas[4]));
      expect(d.telemetriaPrimaria.replaceAll(' · ', ', ')).toBe(celdas[5]);
    });
  }
});

describe('badges (spec 3.3) and the AAA policy of their colours (spec 3.4)', () => {
  const tabla = seccion('### 3.3 Badges');
  const TIERS = ['docker_l2', 'kvm_hardware', 'pty_jaula'] as const;
  const MODOS = ['read_only', 'operator_controlled', 'agent_locked', 'switching'] as const;
  const SALUDES = ['healthy', 'attaching', 'degraded', 'breach', 'zombie'] as const;

  it('shows exactly the texts of the specification table for isolation, mode and health', () => {
    expect(TIERS.map((t) => insigniaAislamiento(t).texto)).toEqual(valoresDeFila(tabla, 'isolation tier'));
    expect(MODOS.map((m) => insigniaModo(m).texto)).toEqual(valoresDeFila(tabla, 'operation mode'));
    expect(SALUDES.map((h) => insigniaSalud(h).texto)).toEqual(valoresDeFila(tabla, 'health'));
    expect(insigniaEscala({ scaled: true, scale: 0.9494 })!.texto).toBe('SCALED ×0.95');
    expect(valoresDeFila(tabla, 'scale')[0]).toBe('SCALED ×0.nn');
    expect(insigniaEscala({ scaled: false, scale: 1 })).toBeNull();
  });

  it('paints the mode badges with the safety colours of the specification, resolved through app.css', () => {
    const filaModo = tabla.split('\n').find((l) => l.startsWith('| operation mode |'))!;
    const [ambar, esmeralda, carmesi] = [...filaModo.matchAll(/#[0-9A-Fa-f]{6}/g)].map((m) => m[0].toLowerCase());
    const tokens = analizarTokens(APP_CSS).oscuro;
    const hexDe = (i: Insignia): string => tokens.get(i.indicador)!.toLowerCase();
    expect(hexDe(insigniaModo('read_only'))).toBe(ambar);
    expect(hexDe(insigniaModo('operator_controlled'))).toBe(esmeralda);
    expect(hexDe(insigniaModo('agent_locked'))).toBe(carmesi);
    expect(insigniaModo('switching').indicador).toBe('--accent-amber');
    expect(insigniaModo('switching').animacion).toBe('pulso');
  });

  it('pulses SWITCHING… and ATTACHING…, blinks BREACH, and animates nothing else', () => {
    const todas: Insignia[] = [...TIERS.map(insigniaAislamiento), ...MODOS.map(insigniaModo), ...SALUDES.map(insigniaSalud)];
    const con = todas.filter((i) => i.animacion !== null).map((i) => `${i.texto}:${i.animacion}`);
    expect(con.sort()).toEqual(['ATTACHING…:pulso', 'BREACH:parpadeo', 'SWITCHING…:pulso']);
  });

  it('keeps crimson, violet and muted out of every label: they only paint the border and the dot', () => {
    const todas: Insignia[] = [
      ...TIERS.map(insigniaAislamiento),
      ...MODOS.map(insigniaModo),
      ...SALUDES.map(insigniaSalud),
      insigniaEscala({ scaled: true, scale: 0.9 })!,
      ...pildorasGates({ cdp: { auth: true, human: false, attach: true, submit: false, harvest: true }, microvm: { A: true, B: false, C: true, D: false, E: true, F: false, G: true, H: false, I: true } }),
    ];
    for (const i of todas) expect(TOKENS_DE_TEXTO.has(i.textoToken), `${i.texto} label token ${i.textoToken}`).toBe(true);
    const comoIndicador = new Set(todas.map((i) => i.indicador));
    for (const t of ['--accent-crimson', '--accent-violet', '--text-muted']) expect(comoIndicador.has(t as never), `${t} is used as an indicator`).toBe(true);
    for (const t of ['--accent-crimson', '--accent-violet', '--text-muted']) expect(TOKENS_DE_TEXTO.has(t as never), `${t} is not a text token`).toBe(false);
  });

  it('only uses as label tokens that reach AAA on the badge chip background, measured on app.css by the instrument', () => {
    const auditoria = auditarTokens(APP_CSS) as Cualquiera;
    for (const tema of auditoria.temas as string[]) {
      for (const token of TOKENS_DE_TEXTO) {
        const par = auditoria.pares.find((p: Cualquiera) => p.tema === tema && p.fg === token && p.bg === '--canvas-void' && p.papel === 'texto');
        expect(par, `${tema} ${token} on the chip background is audited`).toBeDefined();
        expect(par.razon_exacta, `${tema} ${token}`).toBeGreaterThanOrEqual(7);
      }
    }
  });

  it('builds the gate pills of the CDP ladder and the MicroVM gates from the capsule, emerald when they hold', () => {
    const cdp = pildorasGates({ cdp: { auth: true, human: false, attach: true, submit: false, harvest: true } });
    expect(cdp.map((p) => p.texto)).toEqual(valoresDeFila(tabla, 'gates').slice(0, 5));
    expect(cdp.map((p) => p.ok)).toEqual([true, false, true, false, true]);
    expect(cdp.map((p) => p.indicador)).toEqual(['--accent-emerald', '--text-muted', '--accent-emerald', '--text-muted', '--accent-emerald']);
    const vm = pildorasGates({ microvm: { A: true, B: true, C: true, D: true, E: true, F: true, G: true, H: true, I: false } });
    expect(vm.map((p) => p.texto)).toEqual(['V-A', 'V-B', 'V-C', 'V-D', 'V-E', 'V-F', 'V-G', 'V-H', 'V-I']);
    expect(vm.filter((p) => !p.ok).map((p) => p.texto)).toEqual(['V-I']);
    expect(pildorasGates({})).toEqual([]);
  });
});

describe('telemetry text and overlay visibility (spec 1.3, 3.1)', () => {
  it('writes CPU, RAM and the port pair as the bar of the specification does', () => {
    expect(formatoCpu(1.2)).toBe('CPU 1.2%');
    expect(formatoCpu(Number.NaN)).toBe('CPU —');
    expect(formatoMemoria(342 * 1024 * 1024)).toBe('RAM 342M');
    expect(formatoMemoria(1.5 * 1024 ** 3)).toBe('RAM 1.5G');
    expect(formatoMemoria(-1)).toBe('RAM —');
    const ejemplo = /ext 300 · (\d+)\/(\d+) · CPU ([\d.]+)% · RAM (\d+)M/.exec(ESPEC);
    expect(ejemplo).not.toBeNull();
    expect(formatoPuertos({ cdpPort: Number(ejemplo![1]), xpraPort: Number(ejemplo![2]) })).toBe(`${ejemplo![1]}/${ejemplo![2]}`);
    expect(formatoPuertos({ cdpPort: null, xpraPort: 10026 })).toBe('10026');
    expect(formatoCpu(Number(ejemplo![3]))).toBe(`CPU ${ejemplo![3]}%`);
    expect(formatoMemoria(Number(ejemplo![4]) * 1024 * 1024)).toBe(`RAM ${ejemplo![4]}M`);
  });

  it('auto-hides after the 2 s and reveals from the 64 px zone that the specification names', () => {
    expect(AUTO_OCULTAR_MS).toBe(Number(/auto-hides after (\d+) s/.exec(ESPEC)![1]) * 1000);
    expect(ZONA_HOVER_PX).toBe(Number(/pointer hover over the top (\d+) px/.exec(ESPEC)![1]));
  });

  const base = { fijado: false, sobreZona: false, foco: false, actividadReciente: false, salud: 'healthy', modo: 'read_only' } as const;
  it('shows the header when pinned, hovered, focused or recently used, and always while switching or unhealthy', () => {
    expect(cabeceraVisible(base)).toBe(false);
    for (const cambio of [{ fijado: true }, { sobreZona: true }, { foco: true }, { actividadReciente: true }, { modo: 'switching' }, { salud: 'degraded' }, { salud: 'breach' }] as const) {
      expect(cabeceraVisible({ ...base, ...cambio } as never), JSON.stringify(cambio)).toBe(true);
    }
  });

  it('pins the telemetry strip automatically whenever health is not healthy, and not for a mere switch', () => {
    expect(franjaVisible(base)).toBe(false);
    for (const salud of ['attaching', 'degraded', 'breach', 'zombie'] as const) expect(franjaVisible({ ...base, salud }), salud).toBe(true);
    expect(franjaVisible({ ...base, modo: 'switching' })).toBe(false);
    for (const cambio of [{ fijado: true }, { sobreZona: true }, { foco: true }, { actividadReciente: true }] as const) expect(franjaVisible({ ...base, ...cambio } as never)).toBe(true);
  });
});

// =====================================================================================================================
// the optimistic switch (spec 4)
// =====================================================================================================================

describe('optimistic switch controller', () => {
  const armar = (conmutar: () => Promise<{ readonly: boolean; ms: number; estado: string }>, sondear: () => Promise<number>) => {
    const reloj = relojFalso();
    const estados: Array<{ fase: string; modo: string; t: number }> = [];
    const asentados: string[] = [];
    let llamadasConmutar = 0;
    let sondeos = 0;
    const control = crearConmutacion({
      slot: 6,
      puertoXpra: 10026,
      modoInicial: 'read_only',
      conmutador: {
        conmutar: () => {
          llamadasConmutar++;
          return conmutar();
        },
        sondear: () => {
          sondeos++;
          return sondear();
        },
      },
      reloj,
      alCambiar: (e) => estados.push({ fase: e.fase, modo: e.modo, t: reloj.ahora() }),
      alAsentar: (d) => asentados.push(d),
    });
    return { reloj, control, estados, asentados, llamadas: () => llamadasConmutar, sondeos: () => sondeos };
  };

  it('flips to SWITCHING in the same tick, before the backend has answered', async () => {
    const backend = diferido<{ readonly: boolean; ms: number; estado: string }>();
    const c = armar(() => backend.promesa, async () => 200);
    const fin = c.control.conmutar('operador');
    expect(c.estados).toEqual([{ fase: 'conmutando', modo: 'switching', t: 0 }]); // synchronous, nothing awaited yet
    expect(c.control.estado().modo).toBe('switching');
    backend.ok({ readonly: false, ms: 640, estado: 'intervened' });
    await c.reloj.avanzar(0);
    const e = await fin;
    expect(e.fase).toBe('asentado');
    expect(e.modo).toBe('operator_controlled');
  });

  it('settles only after the backend read-back agrees and the port answers 200, probing at once and then every 100 ms', async () => {
    expect(SONDEO_MS).toBe(100);
    const codigos = [503, 503, 503, 200];
    const c = armar(async () => ({ readonly: false, ms: 640, estado: 'intervened' }), async () => codigos.shift() ?? 200);
    const fin = c.control.conmutar('operador');
    await c.reloj.avanzar(0);
    expect(c.sondeos()).toBe(1); // the first probe is immediate
    expect(c.control.estado().fase).toBe('conmutando');
    await c.reloj.avanzar(100);
    await c.reloj.avanzar(100);
    expect(c.control.estado().fase).toBe('conmutando');
    await c.reloj.avanzar(100);
    const e = await fin;
    expect(e).toMatchObject({ fase: 'asentado', modo: 'operator_controlled', ms: 300, error: '' });
    expect(c.sondeos()).toBe(4);
    expect(c.asentados).toEqual(['operador']);
  });

  it('settles a switch back to read-only as READ-ONLY', async () => {
    const c = armar(async () => ({ readonly: true, ms: 590, estado: 'shielded' }), async () => 200);
    const e = await c.control.conmutar('solo_lectura');
    expect(e).toMatchObject({ fase: 'asentado', modo: 'read_only' });
  });

  it('degrades after 3 s without a 200 and shows the literal probe error; it never reverts to the previous mode', async () => {
    expect(LIMITE_CONMUTACION_MS).toBe(3000);
    expect(ESPEC).toContain('After 3 s without success the badge becomes `DEGRADED`');
    const c = armar(async () => ({ readonly: false, ms: 640, estado: 'intervened' }), async () => 503);
    const fin = c.control.conmutar('operador');
    await c.reloj.avanzar(2900);
    expect(c.control.estado().fase).toBe('conmutando');
    await c.reloj.avanzar(200);
    const e = await fin;
    expect(e.fase).toBe('degradado');
    expect(e.salud).toBe('degraded');
    expect(e.error).toBe('Xpra port 10026 answered HTTP 503');
    expect(e.modo).not.toBe('read_only'); // the previous mode
    expect(e.modo).toBe('switching');
    expect(c.asentados).toEqual([]);
  });

  it('carries the literal error of a probe that fails outright', async () => {
    const c = armar(async () => ({ readonly: false, ms: 640, estado: 'intervened' }), async () => Promise.reject(new Error('connect ECONNREFUSED 127.0.0.1:10026')));
    const fin = c.control.conmutar('operador');
    await c.reloj.avanzar(3100);
    const e = await fin;
    expect(e.fase).toBe('degradado');
    expect(e.error).toBe('Xpra port 10026 probe failed: connect ECONNREFUSED 127.0.0.1:10026');
  });

  it('degrades with the literal backend error when the switch is refused', async () => {
    const c = armar(() => Promise.reject(new Error('srf-xpra intervenir rc 1: no such container')), async () => 200);
    const e = await c.control.conmutar('operador');
    expect(e).toMatchObject({ fase: 'degradado', salud: 'degraded', modo: 'switching' });
    expect(e.error).toBe('switch refused: srf-xpra intervenir rc 1: no such container');
    expect(c.sondeos()).toBe(0);
  });

  it('degrades when the read-back disagrees with the destination instead of trusting the request', async () => {
    const c = armar(async () => ({ readonly: true, ms: 600, estado: 'shielded' }), async () => 200);
    const e = await c.control.conmutar('operador');
    expect(e.fase).toBe('degradado');
    expect(e.error).toContain('readonly=true after a switch to operador');
  });

  it('degrades at 3 s when the backend never answers, and ignores its late answer', async () => {
    const backend = diferido<{ readonly: boolean; ms: number; estado: string }>();
    const c = armar(() => backend.promesa, async () => 200);
    const fin = c.control.conmutar('operador');
    await c.reloj.avanzar(3000);
    const e = await fin;
    expect(e).toMatchObject({ fase: 'degradado', error: 'no confirmation within 3000 ms' });
    const antes = c.estados.length;
    backend.ok({ readonly: false, ms: 5000, estado: 'intervened' });
    await c.reloj.avanzar(500);
    expect(c.estados.length).toBe(antes); // nothing changes after the close
    expect(c.asentados).toEqual([]);
  });

  it('is not degraded afterwards by its own 3 s deadline timer once it has settled', async () => {
    const c = armar(async () => ({ readonly: false, ms: 640, estado: 'intervened' }), async () => 200);
    const e = await c.control.conmutar('operador');
    expect(e.fase).toBe('asentado');
    const antes = c.estados.length;
    await c.reloj.avanzar(LIMITE_CONMUTACION_MS + 500); // the deadline timer of the switch fires in here
    expect(c.control.estado().fase).toBe('asentado');
    expect(c.estados.length).toBe(antes);
    expect(c.asentados).toEqual(['operador']);
  });

  it('refuses a second switch while one is under way: one backend call', async () => {
    const backend = diferido<{ readonly: boolean; ms: number; estado: string }>();
    const c = armar(() => backend.promesa, async () => 200);
    const primera = c.control.conmutar('operador');
    const segunda = await c.control.conmutar('solo_lectura');
    expect(segunda.fase).toBe('conmutando');
    expect(c.llamadas()).toBe(1);
    backend.ok({ readonly: false, ms: 640, estado: 'intervened' });
    await c.reloj.avanzar(0);
    await primera;
    expect(c.llamadas()).toBe(1);
  });

  it('shows, next to the settled mode, the failure of the step that follows a settled switch instead of swallowing it', async () => {
    const reloj = relojFalso();
    const estados: Array<{ fase: string; error: string }> = [];
    const control = crearConmutacion({
      slot: 6,
      puertoXpra: 10026,
      modoInicial: 'read_only',
      conmutador: { conmutar: async () => ({ readonly: false, ms: 640, estado: 'intervened' }), sondear: async () => 200 },
      reloj,
      alCambiar: (e) => estados.push({ fase: e.fase, error: e.error }),
      alAsentar: () => {
        throw new Error('frame is not mounted');
      },
    });
    const e = await control.conmutar('operador');
    expect(e.fase).toBe('asentado');
    expect(estados.at(-1)).toEqual({ fase: 'asentado', error: 'settled, but the step after the switch failed: frame is not mounted' });
  });

  it('transfers the focus in three passes at 0, 120 and 400 ms', async () => {
    expect([...PASOS_FOCO_MS]).toEqual([0, 120, 400]);
    expect(ESPEC).toContain('three-pass focus transfer (0 / 120 / 400 ms)');
    const reloj = relojFalso();
    const veces: number[] = [];
    const fin = transferirFoco(reloj, () => veces.push(reloj.ahora()));
    await reloj.avanzar(0);
    expect(veces).toEqual([0]);
    await reloj.avanzar(119);
    expect(veces).toEqual([0]);
    await reloj.avanzar(1);
    expect(veces).toEqual([0, 120]);
    await reloj.avanzar(280);
    await fin;
    expect(veces).toEqual([0, 120, 400]);
  });
});

describe('zero-click-loss and the frame adapter', () => {
  const marco = { nombre: 'frame' };
  const esperado = { origin: 'http://127.0.0.1:10026', source: marco };
  const msg = { origin: 'http://127.0.0.1:10026', source: marco, data: { tipo: 'superficie:pointerdown' } };

  it('activates on a pointerdown notice of THIS frame and refuses any other sender, message or an agent lease', () => {
    expect(debeActivar(msg, esperado, 'read_only')).toBe(true);
    expect(debeActivar(msg, esperado, 'operator_controlled')).toBe(true);
    expect(debeActivar(msg, esperado, 'agent_locked')).toBe(false);
    expect(debeActivar({ ...msg, source: { nombre: 'other' } }, esperado, 'read_only')).toBe(false);
    expect(debeActivar({ ...msg, origin: 'http://127.0.0.1:1' }, esperado, 'read_only')).toBe(false);
    expect(debeActivar({ ...msg, data: { tipo: 'otra' } }, esperado, 'read_only')).toBe(false);
    for (const data of [null, 'superficie:pointerdown', 7, undefined]) expect(debeActivar({ ...msg, data }, esperado, 'read_only'), String(data)).toBe(false);
  });

  function adaptadorDePrueba(scaled: boolean, scale: number) {
    const llamadas: string[] = [];
    const iframe: Cualquiera = { src: 'http://127.0.0.1:10026/', focus: () => llamadas.push('iframe.focus'), contentWindow: { focus: () => llamadas.push('window.focus') } };
    const envoltura: Cualquiera = { dataset: {}, style: { props: {} as Record<string, string>, setProperty(k: string, v: string) { this.props[k] = v; } } };
    const host: Cualquiera = { contains: (x: unknown) => x === iframe };
    const adaptador = crearAdaptadorXpra({ iframe: () => iframe, envoltura: () => envoltura, geometria: () => ({ mode: 'tile_1_6', domWidth: 1200, domHeight: 1064, canvasWidth: 1264, canvasHeight: 1024, scaled, scale }) });
    return { adaptador, iframe, envoltura, host, llamadas };
  }

  it('fits the frame locally only when the DOM is narrower than the canvas', () => {
    const a = adaptadorDePrueba(true, 0.9494);
    a.adaptador.fitLocal();
    expect(a.envoltura.style.props['--escala-local']).toBe('0.9494');
    const b = adaptadorDePrueba(false, 1);
    b.adaptador.fitLocal();
    expect(b.envoltura.style.props['--escala-local']).toBe('1');
  });

  it('focuses the frame and its window, pauses without unmounting, and navigates only on an explicit disconnect', async () => {
    const a = adaptadorDePrueba(false, 1);
    await a.adaptador.mount(a.host);
    a.adaptador.focus();
    expect(a.llamadas).toEqual(['iframe.focus', 'window.focus']);
    a.adaptador.pause(true);
    expect(a.envoltura.dataset.pausado).toBe('true');
    a.adaptador.setViewOnly(true);
    expect(a.envoltura.dataset.soloLectura).toBe('true');
    expect(a.iframe.src).toBe('http://127.0.0.1:10026/'); // nothing above navigated
    a.adaptador.disconnect();
    expect(a.iframe.src).toBe('about:blank');
    expect(a.envoltura.dataset.desconectado).toBe('true');
  });

  it('refuses to mount a frame that is not inside the host, or that is not mounted at all', async () => {
    const a = adaptadorDePrueba(false, 1);
    await expect(a.adaptador.mount({ contains: () => false } as never)).rejects.toThrow('not attached');
    const sin = crearAdaptadorXpra({ iframe: () => null, envoltura: () => null, geometria: () => ({}) as never });
    expect(() => sin.focus()).toThrow('not mounted');
  });
});

// =====================================================================================================================
// static scans of the card sources
// =====================================================================================================================

/** Source text without comments, so a scan reads code and not the prose that explains it. */
function sinComentarios(texto: string): string {
  return texto.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');
}

/** Every construct that reads or handles the clipboard. */
const PATRON_PEGADO = /onpaste|['"`]paste['"`]|\bpaste\b\s*[=:(]|clipboardData|navigator\.clipboard|execCommand|beforeinput|readText/i;

describe('static scans of the card sources', () => {
  it('finds no paste handler on any surface host (F-13), and the scan can see one', () => {
    expect(FUENTES_CARDS.length).toBeGreaterThanOrEqual(5);
    for (const f of FUENTES_CARDS) {
      const hallazgo = PATRON_PEGADO.exec(sinComentarios(f.texto));
      expect(hallazgo, `${f.nombre}: ${hallazgo?.[0]}`).toBeNull();
    }
    // positive control: the same scan does fire on each construct it names
    for (const culpable of ['<div onpaste={f}>', "el.addEventListener('paste', f)", 'ev.clipboardData.getData()', 'navigator.clipboard.readText()', "document.execCommand('paste')"]) {
      expect(PATRON_PEGADO.test(sinComentarios(culpable)), culpable).toBe(true);
    }
  });

  it('never colours a label with crimson, violet or muted: the CSS `color:` of the cards names text tokens only', () => {
    const permitidos = new Set([...TOKENS_DE_TEXTO].map((t) => t.slice(2)).concat(['rotulo']));
    let vistos = 0;
    for (const f of FUENTES_CARDS.filter((x) => x.nombre.endsWith('.svelte'))) {
      for (const m of sinComentarios(f.texto).matchAll(/(?<![-\w])color:\s*var\(--([a-z-]+)\)/g)) {
        vistos++;
        expect(permitidos.has(m[1]), `${f.nombre}: color: var(--${m[1]})`).toBe(true);
      }
    }
    expect(vistos).toBeGreaterThanOrEqual(6);
  });

  it('re-derives no geometry figure: the sources name no canvas, window or bar pixel literal', () => {
    for (const f of FUENTES_CARDS) {
      const hallazgo = /(?<![\w.#-])(1264|1024|2080|1271|1280|1064|2128)(?![\w])/.exec(sinComentarios(f.texto));
      expect(hallazgo, `${f.nombre}: ${hallazgo?.[0]}`).toBeNull();
    }
  });
});

// =====================================================================================================================
// the token audit (instrument: --tokens)
// =====================================================================================================================

/** The WCAG ratio of hex pairs computed by another process (python3) from the definition, not from this repository's code. */
function razonesEnPython(pares: Array<[string, string]>): number[] {
  const codigo = `
import json, sys
def lin(c):
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
def lum(h):
    h = h.lstrip('#')
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
out = []
for a, b in json.load(sys.stdin):
    la, lb = lum(a), lum(b)
    out.append((max(la, lb) + 0.05) / (min(la, lb) + 0.05))
print(json.dumps(out))`;
  const r = spawnSync('python3', ['-c', codigo], { input: JSON.stringify(pares), encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`python3 failed: ${r.stderr}`);
  return JSON.parse(r.stdout);
}

describe('AAA token audit', () => {
  it('computes the WCAG ratio like an independent implementation, on the pairs of app.css', () => {
    const auditoria = auditarTokens(APP_CSS) as Cualquiera;
    const pares = auditoria.pares.filter((p: Cualquiera) => p.tema === 'oscuro');
    const esperado = razonesEnPython(pares.map((p: Cualquiera) => [p.fg_hex, p.bg_hex] as [string, string]));
    expect(pares.length).toBeGreaterThanOrEqual(20);
    pares.forEach((p: Cualquiera, i: number) => expect(p.razon_exacta, `${p.fg} on ${p.bg}`).toBeCloseTo(esperado[i], 9));
    // anchor values, from the definition: white on black is 21:1
    expect(razonContraste([255, 255, 255], [0, 0, 0])).toBeCloseTo(21, 12);
    expect(razonContraste([0, 0, 0], [255, 255, 255])).toBeCloseTo(21, 12);
  });

  it('counts no AAA failure on app.css, the shipped tokens', () => {
    const auditoria = auditarTokens(APP_CSS) as Cualquiera;
    const fallos = auditoria.pares.filter((p: Cualquiera) => p.en_politica && !p.cumple);
    expect(fallos.map((p: Cualquiera) => `${p.tema} ${p.fg} on ${p.bg} ${p.razon}`)).toEqual([]);
    expect(auditoria.temas).toEqual(['oscuro', 'alto-contraste']);
  });

  it('judges a pair against 7:1 exactly: the grey that reaches it passes, one channel step below it fails', () => {
    // the smallest grey that reaches 7:1 on --surface-obsidian (#0e131f), found by the other process
    const candidatos = Array.from({ length: 256 }, (_, g) => `#${g.toString(16).padStart(2, '0').repeat(3)}`);
    const razones = razonesEnPython(candidatos.map((c) => [c, '#0e131f'] as [string, string]));
    const minimo = razones.findIndex((r) => r >= 7);
    expect(minimo).toBeGreaterThan(0);
    const conSilver = (g: number): string => APP_CSS.replace('--text-silver: #94a3b8;', `--text-silver: #${g.toString(16).padStart(2, '0').repeat(3)};`);
    const fallosDe = (g: number): string[] => (auditarTokens(conSilver(g)) as Cualquiera).pares.filter((p: Cualquiera) => p.en_politica && !p.cumple && p.tema === 'oscuro').map((p: Cualquiera) => `${p.fg} on ${p.bg}`);
    expect(fallosDe(minimo)).not.toContain('--text-silver on --surface-obsidian');
    expect(fallosDe(minimo - 1)).toContain('--text-silver on --surface-obsidian');
  });

  it('fails the degraded fixture, and only because of --text-silver (the mutator of the contract)', () => {
    const app = analizarTokens(APP_CSS) as Record<string, Map<string, string>>;
    const deg = analizarTokens(DEGRADADOS) as Record<string, Map<string, string>>;
    expect(Object.keys(deg)).toEqual(Object.keys(app));
    const diferencias: string[] = [];
    for (const tema of Object.keys(app)) {
      for (const [k, v] of app[tema]) if (deg[tema].get(k) !== v) diferencias.push(`${tema} ${k}: ${v} -> ${deg[tema].get(k)}`);
      for (const k of deg[tema].keys()) if (!app[tema].has(k)) diferencias.push(`${tema} ${k}: added`);
    }
    // the fixture copies the geometry tokens too; whatever app.css declares beyond the two token blocks is not in it
    expect(diferencias).toEqual(['oscuro --text-silver: #94a3b8 -> #59626e']);

    const auditoria = auditarTokens(DEGRADADOS) as Cualquiera;
    const fallos = auditoria.pares.filter((p: Cualquiera) => p.en_politica && !p.cumple);
    expect(fallos.map((p: Cualquiera) => `${p.tema} ${p.fg} on ${p.bg}`).sort()).toEqual(['oscuro --text-silver on --canvas-void', 'oscuro --text-silver on --surface-obsidian']);
    const enObsidian = fallos.find((p: Cualquiera) => p.bg === '--surface-obsidian');
    expect(enObsidian.razon_exacta).toBeGreaterThan(2.95);
    expect(enObsidian.razon_exacta).toBeLessThan(3.05); // "lowered to 3:1"
    // the same fixture with silver restored is clean: the failure is the token, not the file
    const restaurado = DEGRADADOS.replace(/--text-silver: #59626e;[^\n]*/, '--text-silver: #94a3b8;');
    expect((auditarTokens(restaurado) as Cualquiera).pares.filter((p: Cualquiera) => p.en_politica && !p.cumple)).toEqual([]);
  });

  it('parses var() chains and themes, and refuses what it cannot judge instead of guessing', () => {
    const css = ':root { --a: #fff; --b: var(--a); --c: #123; --d: rgba(0,0,0,.5); --e: var(--e); --f: var(--zz); }\n:root[data-tema="claro"] { --a: #000; }';
    const t = analizarTokens(css) as Record<string, Map<string, string>>;
    expect(colorDeToken(t.oscuro, '--b')).toEqual([255, 255, 255]);
    expect(colorDeToken(t.claro, '--b')).toEqual([0, 0, 0]); // the theme overrides --a, the chain follows it
    expect(colorDeToken(t.oscuro, '--c')).toEqual([17, 34, 51]);
    expect(() => colorDeToken(t.oscuro, '--d')).toThrow('not an opaque');
    expect(() => colorDeToken(t.oscuro, '--e')).toThrow('circular');
    expect(() => colorDeToken(t.oscuro, '--f')).toThrow('not declared');
  });

  it('extracts only the token blocks of app.css for the harness: no global rule of the shell reaches a card', () => {
    const css = tokensDeApp(APP_CSS) as string;
    expect(css).toContain('--text-silver: #94a3b8');
    expect(css).toContain("data-tema='alto-contraste'");
    expect(css).not.toMatch(/prefers-reduced-motion|\.barra|html\.initializing|body\s*\{/);
  });
});

// =====================================================================================================================
// the instrument: one report of one shape on every path
// =====================================================================================================================

/** The nested key structure of a report (arrays and scalars are leaves): the "same shape" of every path. */
function forma(v: unknown): unknown {
  if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
    return Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b)).map(([k, x]) => [k, forma(x)]));
  }
  return 'hoja';
}

function jq(filtro: string, texto: string): number | null {
  return spawnSync('jq', ['-e', filtro], { input: texto }).status;
}

describe('the instrument writes its report on every path', () => {
  let TMP = '';
  beforeAll(() => {
    TMP = mkdtempSync(join(tmpdir(), 'srf-u20-informe-'));
  });
  afterAll(() => {
    if (TMP !== '') rmSync(TMP, { recursive: true, force: true });
  });
  const forma0 = forma(informeVacio());

  const casos: Array<[string, () => string[], number, string, string]> = [
    ['no arguments', () => [], 2, 'argumentos', 'exactly one of'],
    ['both modes at once', () => ['--tokens', 'x.css', '--conmutar-slot', '6'], 2, 'argumentos', 'exactly one of'],
    ['an unknown argument', () => ['--tokens', 'x.css', '--nope'], 2, 'argumentos', 'unknown argument'],
    ['a slot out of range', () => ['--conmutar-slot', '80'], 2, 'argumentos', 'a slot is an integer'],
    ['a token file that does not exist', () => ['--tokens', join(TMP, 'no-existe.css')], 2, 'precondicion', 'no such token file'],
    ['a file without tokens', () => ['--tokens', escribir('vacio.css', 'body { color: red }')], 1, 'tokens', 'declares no'],
    ['a token that is not an opaque colour', () => ['--tokens', escribir('rgba.css', APP_CSS.replace('--text-polar: #f4f6fc;', '--text-polar: rgba(244,246,252,.9);'))], 1, 'tokens', 'not an opaque'],
    ['a required token that is missing', () => ['--tokens', escribir('falta.css', APP_CSS.replace(/--accent-cyan:[^;]+;/, ''))], 1, 'tokens', '--accent-cyan is not declared'],
  ];
  function escribir(nombre: string, texto: string): string {
    const ruta = join(TMP, nombre);
    writeFileSync(ruta, texto);
    return ruta;
  }

  for (const [nombre, args, rc, etapa, causa] of casos) {
    it(`reports ${nombre} in the one shape, ok:false, rc ${rc}, and no measured value`, async () => {
      const { informe, rc: salida } = (await ejecutar(args())) as Cualquiera;
      expect(salida).toBe(rc);
      expect(informe.rc).toBe(rc);
      expect(informe.ok).toBe(false);
      expect(informe.etapa).toBe(etapa);
      expect(informe.error).toContain(causa);
      expect(forma(informe)).toEqual(forma0);
      for (const campo of ['aaa_fallos', 'ms', 'iframe_recargado', 'iframe_desmontado']) expect(informe[campo], campo).toBeNull();
      // jq -e '.aaa_fallos==0' must not be satisfiable by a report that measured nothing
      expect(jq('.aaa_fallos==0', JSON.stringify(informe))).not.toBe(0);
      expect(jq('.ms<800 and .iframe_recargado==false and .iframe_desmontado==false', JSON.stringify(informe))).not.toBe(0);
    });
  }

  it('reports the port plan and the container disagreeing, or no capsule at all, as a precondition failure (rc 2)', async () => {
    const dependencias = (puertos: () => Promise<unknown>) => ({ backend: { conmutar: async () => ({}), sondear: async () => 200 }, puertos, estado: async () => ({ readonly: true }) });
    let r = (await ejecutar(['--conmutar-slot', '6', '--xpra-bin', process.execPath], dependencias(async () => ({ plan: 10026, cdp: 9226, publicado: 10027 })))) as Cualquiera;
    expect([r.rc, r.informe.etapa]).toEqual([2, 'precondicion']);
    expect(r.informe.error).toContain('publishes 10027');
    r = (await ejecutar(['--conmutar-slot', '6', '--xpra-bin', process.execPath], dependencias(async () => Promise.reject(new Error('no such container')))) ) as Cualquiera;
    expect([r.rc, r.informe.etapa]).toEqual([2, 'precondicion']);
    expect(r.informe.error).toContain('no such container');
    expect(forma(r.informe)).toEqual(forma0);
    // the real readers, on a slot nobody owns: a precondition failure whatever is or is not built yet
    r = (await ejecutar(['--conmutar-slot', '79'], dependenciasReales())) as Cualquiera;
    expect([r.rc, r.informe.ok, r.informe.etapa]).toEqual([2, false, 'precondicion']);
    expect(r.informe.ms).toBeNull();
  });
});

// =====================================================================================================================
// the cards in a real browser, built from the sources under test into a temporary directory (never dist/)
// =====================================================================================================================

const CDP_FALSO_PUERTO = 9226;

/** A stand-in for the capsule's Xpra HTTP server: counts how many times a FRAME loaded it and how many times something probed it. */
async function xpraFalso(opciones: { recargarse?: boolean } = {}) {
  let cargasDeMarco = 0;
  let sondeos = 0;
  const servidor: Server = createServer((req, res) => {
    if (req.headers['sec-fetch-dest'] === 'iframe') cargasDeMarco++;
    else sondeos++;
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end(`<!doctype html><title>fake xpra</title><body style="margin:0;background:#fff"><script>
      window.addEventListener('pointerdown', () => parent.postMessage({ tipo: 'superficie:pointerdown' }, '*'));
      ${opciones.recargarse ? "if (!sessionStorage.getItem('r')) setTimeout(() => { sessionStorage.setItem('r', '1'); location.reload(); }, 150);" : ''}
    </script>`);
  });
  await new Promise<void>((ok) => servidor.listen(0, '127.0.0.1', ok));
  return {
    puerto: (servidor.address() as { port: number }).port,
    cargasDeMarco: () => cargasDeMarco,
    sondeos: () => sondeos,
    cerrar: () => new Promise<void>((ok) => servidor.close(() => ok())),
  };
}

function capsulaDePrueba(archetype: string, xpraPort: number, extra: Record<string, unknown> = {}): Cualquiera {
  const tier = archetype === 'microvm' ? 'kvm_hardware' : archetype === 'terminal' ? 'pty_jaula' : 'docker_l2';
  return {
    id: `srf-ng-${extra.slot ?? 6}`,
    name: `srf-ng-${extra.slot ?? 6} · ${archetype}`,
    archetype,
    tier,
    mode: 'read_only',
    health: 'healthy',
    slot: 6,
    xpraPort,
    cdpPort: archetype === 'browser' ? CDP_FALSO_PUERTO : null,
    telemetry: { cpuPercent: 1.2, memoryBytes: 342 * 1024 * 1024, memoryLimitBytes: 2 * 1024 ** 3, networkRxRateBps: 0, networkTxRateBps: 0 },
    gates:
      archetype === 'browser'
        ? { cdp: { auth: true, human: true, attach: false, submit: false, harvest: true } }
        : archetype === 'microvm'
          ? { microvm: { A: true, B: true, C: false, D: true, E: true, F: true, G: false, H: true, I: true } }
          : {},
    recording: { recording: false, sessionId: '', targetXid: '', sinceMonoMs: 0, offsetMs: null, verdict: '' },
    pbx: null,
    poppedOut: false,
    occluded: false,
    createdAt: '',
    ...extra,
  };
}

describe.skipIf(!process.env.OSCU_BROWSER_TESTS)('cards in a real browser', () => {
  let TMP = '';
  let DIST = '';
  let navegador: Cualquiera = null;
  let tmpAnterior: string | undefined;
  const abiertos: Array<() => Promise<void>> = [];
  const verificaciones: Array<() => void> = [];

  // A page error is asserted here, after the test that provoked it, so a failure is reported against that test instead of
  // being lost in a cleanup hook.
  afterEach(() => {
    for (const verificar of verificaciones.splice(0)) verificar();
  });

  beforeAll(async () => {
    TMP = mkdtempSync(join(tmpdir(), 'srf-u20-'));
    tmpAnterior = process.env.TMPDIR;
    process.env.TMPDIR = TMP; // Playwright's profile and artifact folders land in the directory afterAll removes
    DIST = await construirHarness({ padre: TMP });
    const pw = createRequire(join(RAIZ_FRONTEND, 'package.json'))('playwright-core');
    navegador = await pw.chromium.launch({ headless: true });
  });

  afterAll(async () => {
    for (const cerrar of abiertos.reverse()) await cerrar().catch(() => {});
    if (navegador !== null) await navegador.close().catch(() => {});
    if (tmpAnterior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpAnterior;
    if (TMP !== '') rmSync(TMP, { recursive: true, force: true });
  });

  interface Escena {
    pagina: Cualquiera;
    web: Cualquiera;
    xpra: Cualquiera;
    cdp: Cualquiera;
    navegados: string[];
    separados: string[];
    llamadas: () => number;
  }

  /** One harness page over a fake Xpra and a fake switch backend. */
  async function escena(opciones: {
    capsulas: (xpraPort: number) => Cualquiera[];
    viewport?: { width: number; height: number };
    reducedMotion?: 'reduce' | 'no-preference';
    fijado?: boolean;
    conBotones?: boolean;
    retardoConmutar?: number;
    conmutar?: (slot: number, destino: string) => Promise<unknown>;
    sondear?: (puerto: number) => Promise<number>;
    xpraRecargable?: boolean;
  }): Promise<Escena> {
    const xpra = await xpraFalso({ recargarse: opciones.xpraRecargable });
    let readonly = true;
    let llamadas = 0;
    const backend = {
      conmutar:
        opciones.conmutar ??
        (async (_slot: number, destino: string) => {
          llamadas++;
          await new Promise((r) => setTimeout(r, opciones.retardoConmutar ?? 250));
          readonly = destino === 'solo_lectura';
          return { readonly, ms: opciones.retardoConmutar ?? 250, estado: readonly ? 'shielded' : 'intervened' };
        }),
      sondear: opciones.sondear ?? ((puerto: number) => sondearHttp(puerto)),
    };
    const web = await servirHarness({
      dist: DIST,
      cfg: { fijado: opciones.fijado ?? true, autoOcultarMs: 2000, conBotones: opciones.conBotones ?? false, capsulas: opciones.capsulas(xpra.puerto) },
      tokensCss: tokensDeApp(APP_CSS),
      backend,
    });
    const contexto = await navegador.newContext({ viewport: opciones.viewport ?? { width: 1271, height: 1064 }, deviceScaleFactor: 1, colorScheme: 'dark', reducedMotion: opciones.reducedMotion ?? 'no-preference' });
    const pagina = await contexto.newPage();
    const cdp = await contexto.newCDPSession(pagina);
    const navegados: string[] = [];
    const separados: string[] = [];
    cdp.on('Page.frameNavigated', (e: Cualquiera) => {
      if (e.frame.parentId !== undefined) navegados.push(e.frame.url);
    });
    cdp.on('Page.frameDetached', (e: Cualquiera) => separados.push(e.frameId));
    await cdp.send('Page.enable');
    const errores: string[] = [];
    pagina.on('pageerror', (e: Error) => errores.push(e.message));
    abiertos.push(async () => {
      await contexto.close();
      await web.cerrar();
      await xpra.cerrar();
    });
    verificaciones.push(() => expect(errores, 'uncaught errors in the harness page').toEqual([]));
    await pagina.goto(`http://127.0.0.1:${web.puerto}/`, { waitUntil: 'load' });
    await pagina.waitForFunction(() => document.documentElement.dataset.appReady === 'true');
    if ((await pagina.locator('iframe').count()) > 0) await pagina.frames().find((f: Cualquiera) => f !== pagina.mainFrame())?.waitForLoadState('load');
    return { pagina, web, xpra, cdp, navegados, separados, llamadas: () => llamadas };
  }

  const cajaCdp = async (cdp: Cualquiera, selector: string): Promise<{ x: number; y: number; w: number; h: number }> => {
    const doc = await cdp.send('DOM.getDocument', { depth: 0 });
    const nodo = await cdp.send('DOM.querySelector', { nodeId: doc.root.nodeId, selector });
    if (!nodo.nodeId) throw new Error(`CDP: ${selector} not found`);
    const { model } = await cdp.send('DOM.getBoxModel', { nodeId: nodo.nodeId });
    const q = model.border;
    return { x: Math.min(q[0], q[2], q[4], q[6]), y: Math.min(q[1], q[3], q[5], q[7]), w: model.width, h: model.height };
  };

  const ARQUETIPOS_TODOS = ['browser', 'desktop', 'microvm', 'terminal'] as const;
  const altoDeOverlay = Number(/--overlay-head-h:\s*(\d+)px/.exec(ESPEC)![1]);

  for (const [modo, viewport, alto] of [
    ['1/6', { width: 1271, height: 1064 }, LIENZO_H],
    ['1/3 tall', { width: 1271, height: 2128 }, LIENZO_H_ALTO],
  ] as const) {
    for (const arquetipo of ARQUETIPOS_TODOS) {
      it(`lays the ${arquetipo} card out at ${LIENZO_W} x ${alto} with both overlays INSIDE the canvas (${modo})`, async () => {
        const e = await escena({ capsulas: (p) => [capsulaDePrueba(arquetipo, p)], viewport });
        const enPagina = await e.pagina.evaluate(() => {
          const r = (s: string) => {
            const el = document.querySelector(s);
            if (el === null) return null;
            const b = el.getBoundingClientRect();
            return { x: b.x, y: b.y, w: b.width, h: b.height };
          };
          return { tarjeta: r('[data-tarjeta]'), cabecera: r('[data-cabecera]'), franja: r('[data-franja]'), anfitrion: r('[data-superficie-host]'), marco: r('iframe'), hayMarco: document.querySelectorAll('iframe').length };
        });
        // the origin (3, 40) is the canvas frame of the shell: 3 px gutter, 40 px bar
        expect(enPagina.tarjeta).toEqual({ x: 3, y: 40, w: LIENZO_W, h: alto });
        expect(enPagina.cabecera).toEqual({ x: 3, y: 40, w: LIENZO_W, h: altoDeOverlay });
        expect(enPagina.franja).toEqual({ x: 3, y: 40 + alto - altoDeOverlay, w: LIENZO_W, h: altoDeOverlay });
        expect(enPagina.anfitrion).toEqual({ x: 3, y: 40, w: LIENZO_W, h: alto }); // the framebuffer is never shrunk by the chrome
        if (arquetipo === 'terminal') expect(enPagina.hayMarco).toBe(0);
        else expect(enPagina.marco).toEqual({ x: 3, y: 40, w: LIENZO_W, h: alto });
        // second source: Chromium's own box model of the same elements
        for (const [selector, esperada] of [['[data-tarjeta]', enPagina.tarjeta], ['[data-cabecera]', enPagina.cabecera], ['[data-franja]', enPagina.franja]] as const) {
          expect(await cajaCdp(e.cdp, selector), `CDP ${selector}`).toEqual({ x: esperada!.x, y: esperada!.y, w: esperada!.w, h: esperada!.h });
        }
      });
    }
  }

  it('embeds the Xpra surface in ONE sandboxed iframe at the fixed CSS size, on the capsule port, and the terminal in none', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p), capsulaDePrueba('terminal', p, { slot: 7, id: 'srf-ng-7' })] });
    const marcos = await e.pagina.evaluate(() =>
      [...document.querySelectorAll('iframe')].map((f) => ({ sandbox: f.getAttribute('sandbox'), tokens: [...f.sandbox], src: f.src, css: [f.style.width, f.style.height], ancho: f.offsetWidth, alto: f.offsetHeight, titulo: f.title })),
    );
    expect(marcos).toHaveLength(1);
    expect(marcos[0].sandbox).toBe(SANDBOX_XPRA);
    expect([...marcos[0].tokens].sort()).toEqual(['allow-pointer-lock', 'allow-same-origin', 'allow-scripts']);
    expect(marcos[0].css).toEqual([`${LIENZO_W}px`, `${LIENZO_H}px`]);
    expect([marcos[0].ancho, marcos[0].alto]).toEqual([LIENZO_W, LIENZO_H]);
    expect(marcos[0].src).toBe(urlXpra(e.xpra.puerto));
    expect(marcos[0].titulo).toContain('srf-ng-6');
    const terminal = await e.pagina.evaluate(() => {
      const t = document.querySelector('[data-tarjeta][data-arquetipo="terminal"]')!;
      const host = t.querySelector('[data-superficie-host]')!;
      return { dentro: host.querySelector('[data-anfitrion-contenido]') !== null, conmutar: t.querySelector('[data-accion="conmutar"]') !== null, iframes: t.querySelectorAll('iframe').length };
    });
    expect(terminal).toEqual({ dentro: true, conmutar: false, iframes: 0 });
  });

  it('shows the badges of every archetype with the specification texts and the safety colours as rendered', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p, { slot: 6 }), capsulaDePrueba('microvm', p, { slot: 7, id: 'srf-ng-7', mode: 'operator_controlled' }), capsulaDePrueba('desktop', p, { slot: 8, id: 'srf-ng-8', mode: 'agent_locked' }), capsulaDePrueba('terminal', p, { slot: 9, id: 'srf-ng-9', health: 'zombie' })] });
    const filas = await e.pagina.evaluate(() =>
      [...document.querySelectorAll('[data-tarjeta]')].map((t) => {
        const marco = getComputedStyle(t.querySelector('[data-marco-modo]')!);
        const modo = t.querySelector('[data-insignia][data-clase="modo"]') as HTMLElement;
        return {
          id: (t as HTMLElement).dataset.id,
          textos: [...t.querySelectorAll('[data-cabecera] [data-insignia] .texto')].map((x) => x.textContent),
          marco: [marco.borderTopWidth, marco.borderTopColor],
          bordeInsignia: getComputedStyle(modo).borderTopColor,
          pildoras: [...t.querySelectorAll('[data-franja] [data-insignia][data-clase="gate"] .texto')].map((x) => x.textContent),
        };
      }),
    );
    const tabla = seccion('### 3.3 Badges');
    const [ambar, esmeralda, carmesi] = [...tabla.split('\n').find((l) => l.startsWith('| operation mode |'))!.matchAll(/#([0-9A-Fa-f]{6})/g)].map((m) => m[1].toLowerCase());
    const rgb = (h: string): string => `rgb(${[0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(', ')})`;
    const espec4 = /`OPERATOR-CONTROL` \((\d) px emerald border\) or `READ-ONLY` \((\d) px amber border\)/.exec(seccion('## 4. Read-Only ⇄ Operator Hot Switch'))!;
    expect(filas.map((f: Cualquiera) => f.textos)).toEqual([
      ['DOCKER L2', 'READ-ONLY', 'HEALTHY'],
      ['KVM HARDWARE', 'OPERATOR-CONTROL', 'HEALTHY'],
      ['DOCKER L2', 'AGENT-HELD', 'HEALTHY'],
      ['PTY JAULA', 'READ-ONLY', 'ZOMBIE'],
    ]);
    expect(filas[0].marco).toEqual([`${espec4[2]}px`, rgb(ambar)]); // READ-ONLY: 1 px amber
    expect(filas[1].marco).toEqual([`${espec4[1]}px`, rgb(esmeralda)]); // OPERATOR-CONTROL: 2 px emerald
    expect(filas[2].marco[1]).toBe(rgb(carmesi));
    expect(filas[0].bordeInsignia).toBe(rgb(ambar));
    expect(filas[1].bordeInsignia).toBe(rgb(esmeralda));
    expect(filas[2].bordeInsignia).toBe(rgb(carmesi));
    expect(filas[0].pildoras).toEqual(['G-AUTH', 'G-HUMAN', 'G-ATTACH', 'G-SUBMIT', 'G-COSECHA']);
    expect(filas[1].pildoras).toEqual(['V-A', 'V-B', 'V-C', 'V-D', 'V-E', 'V-F', 'V-G', 'V-H', 'V-I']);
    expect(filas[2].pildoras).toEqual([]);
  });

  it('reaches WCAG AAA (7:1) on every label the cards render, over the worst backdrop the 88 % overlays can meet (F-08)', async () => {
    const modos = ['read_only', 'operator_controlled', 'agent_locked', 'switching'];
    const saludes = ['healthy', 'attaching', 'degraded', 'breach', 'zombie'];
    const capsulas = (p: number): Cualquiera[] => [
      ...modos.map((m, i) => capsulaDePrueba('browser', p, { slot: 10 + i, id: `srf-ng-${10 + i}`, mode: m })),
      ...saludes.map((h, i) => capsulaDePrueba('microvm', p, { slot: 20 + i, id: `srf-ng-${20 + i}`, health: h })),
      capsulaDePrueba('terminal', p, { slot: 30, id: 'srf-ng-30' }),
    ];
    const e = await escena({ capsulas, viewport: { width: 1200, height: 1064 } }); // narrow: the SCALED badge is rendered too
    const informe = await e.pagina.evaluate(() => {
      const analizar = (s: string): number[] => {
        let m = /^rgba?\(([^)]+)\)/.exec(s);
        if (m !== null) {
          const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
          return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
        }
        m = /^color\(srgb ([^)]+)\)/.exec(s);
        if (m !== null) {
          const [rgb, a] = m[1].split('/');
          const p = rgb.trim().split(/\s+/).map(Number);
          return [p[0] * 255, p[1] * 255, p[2] * 255, a === undefined ? 1 : Number(a)];
        }
        throw new Error(`unparsed colour ${s}`);
      };
      const lin = (c: number): number => ((c /= 255) <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
      const lum = (c: number[]): number => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
      const razon = (a: number[], b: number[]): number => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05);
      const sobre = (capa: number[], fondo: number[]): number[] => [0, 1, 2].map((i) => capa[3] * capa[i] + (1 - capa[3]) * fondo[i]);
      const peores: Array<{ texto: string; razon: number }> = [];
      let revisados = 0;
      for (const tarjeta of document.querySelectorAll('[data-tarjeta]')) {
        for (const el of tarjeta.querySelectorAll('*')) {
          if (el.closest('iframe') !== null || el.tagName === 'IFRAME') continue;
          const propio = [...el.childNodes].some((n) => n.nodeType === 3 && (n.textContent ?? '').trim() !== '');
          if (!propio) continue;
          // the layers under the text: from the element up to the first opaque one; the card's own background is
          // covered by the remote surface, so the rest of the backdrop is the worst case (white or black)
          const capas: number[][] = [];
          for (let n: Element | null = el; n !== null && n !== tarjeta; n = n.parentElement) {
            const c = analizar(getComputedStyle(n).backgroundColor);
            if (c[3] > 0) capas.push(c);
            if (c[3] >= 0.999) break;
          }
          const texto = analizar(getComputedStyle(el).color);
          const opacidad = Number(getComputedStyle(el).opacity);
          let peor = Infinity;
          for (const fondo of [[255, 255, 255], [0, 0, 0]]) {
            let base = fondo;
            for (const capa of capas.reverse()) base = sobre(capa, base);
            capas.reverse();
            peor = Math.min(peor, razon(sobre(texto, base), base));
          }
          revisados++;
          if (opacidad === 1 && peor < 7) peores.push({ texto: `${(el.textContent ?? '').trim().slice(0, 30)} [${el.className}]`, razon: Math.round(peor * 100) / 100 });
        }
      }
      return { revisados, peores, escalado: document.querySelectorAll('[data-insignia][data-clase="escala"]').length };
    });
    expect(informe.escalado).toBeGreaterThan(0);
    expect(informe.revisados).toBeGreaterThan(150);
    expect(informe.peores).toEqual([]);
  });

  it('removes every pulse, sweep and blink under prefers-reduced-motion and keeps them without it (F-12)', async () => {
    const capsulas = (p: number): Cualquiera[] => [
      capsulaDePrueba('browser', p, { slot: 10, id: 'srf-ng-10', mode: 'switching' }),
      capsulaDePrueba('desktop', p, { slot: 11, id: 'srf-ng-11', health: 'attaching' }),
      capsulaDePrueba('microvm', p, { slot: 12, id: 'srf-ng-12', health: 'breach' }),
      capsulaDePrueba('browser', p, { slot: 13, id: 'srf-ng-13' }), // read-only: the click below starts a switch that never answers, so the sweep stays
    ];
    const medir = async (reducedMotion: 'reduce' | 'no-preference') => {
      const e = await escena({ capsulas, reducedMotion, conmutar: () => new Promise(() => {}) });
      await e.pagina.click('[data-tarjeta][data-id="srf-ng-13"] [data-accion="conmutar"]');
      await e.pagina.waitForSelector('[data-tarjeta][data-id="srf-ng-13"] [data-barrido]', { state: 'attached', timeout: 1500 });
      return e.pagina.evaluate(() => {
        const limpio = (n: string): string => n.replace(/^svelte-[a-z0-9]+-/, '');
        const elementos = [...document.querySelectorAll('[data-insignia], [data-marco-modo], [data-barrido]')];
        return {
          vivas: document.getAnimations().map((a) => limpio((a as CSSAnimation).animationName)).sort(),
          declaradas: [...new Set(elementos.map((el) => limpio(getComputedStyle(el).animationName)))].sort(),
          anillo: getComputedStyle(document.querySelector('[data-insignia][data-animacion="pulso"]')!).boxShadow,
        };
      });
    };
    const con = await medir('no-preference');
    expect(con.declaradas).toEqual(['barrido', 'marco-pulso', 'none', 'parpadeo', 'pulso']); // the four loops exist ('none' is every static element)
    expect(con.vivas).toEqual(expect.arrayContaining(['barrido', 'marco-pulso', 'pulso']));
    const sin = await medir('reduce');
    expect(sin.vivas).toEqual([]);
    expect(sin.declaradas).toEqual(['none']);
    expect(sin.anillo).not.toBe('none'); // the pulse is replaced by a static ring, not by nothing
  });

  it('navigates the frame only when the capsule port changes: the load probe sees that one reload and the far side counts it', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p)] });
    const otro = await xpraFalso();
    abiertos.push(() => otro.cerrar());
    await e.pagina.evaluate(() => {
      (window as Cualquiera).__nodo = document.querySelector('iframe');
      (window as Cualquiera).__cargas = 0;
      document.addEventListener('load', (ev) => { if (ev.target === (window as Cualquiera).__nodo) (window as Cualquiera).__cargas++; }, true);
    });
    await e.pagina.evaluate((puerto: number) => (window as Cualquiera).__srfHarness.poner('srf-ng-6', { xpraPort: puerto }), otro.puerto);
    await e.pagina.waitForFunction(() => (window as Cualquiera).__cargas === 1, null, { timeout: 3000 });
    expect(otro.cargasDeMarco()).toBe(1); // the far side of the new port saw the frame
    expect(e.xpra.cargasDeMarco()).toBe(1); // and the old one saw no more
    expect(await e.pagina.evaluate(() => document.querySelector('iframe') === (window as Cualquiera).__nodo)).toBe(true); // the same element, navigated
    expect(new URL(await e.pagina.evaluate(() => document.querySelector('iframe')!.src)).port).toBe(String(otro.puerto));
  });

  it('flips the badge to SWITCHING… before the backend answers, then settles without touching the frame (spec 4)', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p)], retardoConmutar: 1200 });
    await e.pagina.evaluate(() => {
      (window as Cualquiera).__nodo = document.querySelector('iframe');
      (window as Cualquiera).__cargas = 0;
      document.addEventListener('load', (ev) => { if (ev.target === (window as Cualquiera).__nodo) (window as Cualquiera).__cargas++; }, true);
      (window as Cualquiera).__quitados = 0;
      new MutationObserver((rs) => { for (const r of rs) for (const n of r.removedNodes) if (n === (window as Cualquiera).__nodo || (n as Element).contains?.((window as Cualquiera).__nodo)) (window as Cualquiera).__quitados++; }).observe(document.documentElement, { childList: true, subtree: true });
    });
    const cargasAntes = e.xpra.cargasDeMarco();
    expect(cargasAntes).toBe(1);
    const navegadosAntes = e.navegados.length;
    await e.pagina.click('[data-accion="conmutar"]');
    await e.pagina.waitForFunction(() => document.querySelector('[data-insignia][data-clase="modo"] .texto')?.textContent === 'SWITCHING…', null, { timeout: 700 });
    // the backend is still under way when the badge already says SWITCHING…: the flip did not wait for it
    const enVuelo = e.web.llamadas.filter((c: Cualquiera) => c.tipo === 'conmutar' && c.t_respondido === null).length;
    expect(enVuelo).toBe(1);
    expect(await e.pagina.evaluate(() => document.querySelector('[data-tarjeta]')!.getAttribute('data-fase'))).toBe('conmutando');
    await e.pagina.waitForFunction(() => document.querySelector('[data-tarjeta]')!.getAttribute('data-fase') === 'asentado', null, { timeout: 4000 });
    const despues = await e.pagina.evaluate(() => ({
      modo: document.querySelector('[data-insignia][data-clase="modo"] .texto')!.textContent,
      mismo: document.querySelector('iframe') === (window as Cualquiera).__nodo,
      conectado: (window as Cualquiera).__nodo.isConnected,
      cargas: (window as Cualquiera).__cargas,
      quitados: (window as Cualquiera).__quitados,
      marco: getComputedStyle(document.querySelector('[data-marco-modo]')!).borderTopWidth,
      etiqueta: document.querySelector('[data-accion="conmutar"]')!.textContent,
    }));
    expect(despues).toEqual({ modo: 'OPERATOR-CONTROL', mismo: true, conectado: true, cargas: 0, quitados: 0, marco: '2px', etiqueta: 'Read-only' });
    expect(e.xpra.cargasDeMarco()).toBe(cargasAntes); // the far side: the Xpra server saw no second load of the frame
    expect(e.navegados.length).toBe(navegadosAntes); // Chromium: the child frame navigated no more
    expect(e.separados).toEqual([]);
    // three-pass focus transfer: the operator now has the frame
    await e.pagina.waitForFunction(() => document.activeElement === document.querySelector('iframe'), null, { timeout: 1500 });
    // and back to read-only, still the same frame
    await e.pagina.click('[data-accion="conmutar"]');
    await e.pagina.waitForFunction(() => document.querySelector('[data-tarjeta]')!.getAttribute('data-fase') === 'asentado' && document.querySelector('[data-tarjeta]')!.getAttribute('data-modo') === 'read_only', null, { timeout: 4000 });
    expect(await e.pagina.evaluate(() => document.querySelector('iframe') === (window as Cualquiera).__nodo)).toBe(true);
    expect(e.xpra.cargasDeMarco()).toBe(cargasAntes);
    expect(e.llamadas()).toBe(2);
  });

  it('shows DEGRADED and the literal backend error when the switch fails, keeps the frame and never reverts silently', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p)], conmutar: async () => Promise.reject(new Error('srf-xpra intervenir rc 1: error intervening slot 6: no such container')) });
    await e.pagina.evaluate(() => { (window as Cualquiera).__nodo = document.querySelector('iframe'); });
    await e.pagina.click('[data-accion="conmutar"]');
    await e.pagina.waitForFunction(() => document.querySelector('[data-tarjeta]')!.getAttribute('data-fase') === 'degradado', null, { timeout: 3000 });
    const r = await e.pagina.evaluate(() => ({
      salud: document.querySelector('[data-insignia][data-clase="salud"] .texto')!.textContent,
      modo: document.querySelector('[data-insignia][data-clase="modo"] .texto')!.textContent,
      aviso: document.querySelector('[data-aviso]')!.textContent,
      papel: document.querySelector('[data-aviso]')!.getAttribute('role'),
      mismo: document.querySelector('iframe') === (window as Cualquiera).__nodo,
      reintento: (document.querySelector('[data-accion="conmutar"]') as HTMLButtonElement).disabled,
    }));
    expect(r.salud).toBe('DEGRADED');
    expect(r.modo).toBe('SWITCHING…'); // never back to READ-ONLY as if nothing had happened
    expect(r.aviso).toBe('switch refused: srf-xpra intervenir rc 1: error intervening slot 6: no such container');
    expect(r.papel).toBe('alert');
    expect(r.mismo).toBe(true);
    expect(r.reintento).toBe(false); // the operator can try again
    expect(e.xpra.cargasDeMarco()).toBe(1);
  });

  it('aspect-fits a canvas the DOM cannot hold, with the SCALED badge, and keeps the frame at its fixed CSS size (spec 1.6)', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p)], viewport: { width: 1200, height: 1064 } });
    const r = await e.pagina.evaluate(() => {
      const f = document.querySelector('iframe')!;
      const b = f.getBoundingClientRect();
      const t = document.querySelector('[data-tarjeta]')!.getBoundingClientRect();
      return { css: [f.offsetWidth, f.offsetHeight], visible: [b.width, b.height], tarjeta: [t.width, t.height], escala: (document.querySelector('[data-xpra-envoltura]') as HTMLElement).style.getPropertyValue('--escala-local'), insignia: [...document.querySelectorAll('[data-franja] [data-insignia][data-clase="escala"] .texto')].map((x) => x.textContent) };
    });
    const escala = 1200 / LIENZO_W;
    expect(r.css).toEqual([LIENZO_W, LIENZO_H]);
    expect(Number(r.escala)).toBeCloseTo(escala, 6);
    expect(r.visible[0]).toBeCloseTo(1200, 3);
    expect(r.visible[1]).toBeCloseTo(LIENZO_H * escala, 3);
    expect(r.tarjeta[0]).toBeCloseTo(1200, 3);
    expect(r.insignia).toEqual([`SCALED ×${escala.toFixed(2)}`]);
  });

  it('activates and focuses the card on a pointerdown notice of its frame, but never under an agent lease or from another sender (zero-click-loss)', async () => {
    // 1900 px tall: below the 1/3 column threshold, so both cards are 1024 px and the second one lies inside the viewport
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p, { mode: 'operator_controlled' }), capsulaDePrueba('desktop', p, { slot: 8, id: 'srf-ng-8', mode: 'agent_locked' })], viewport: { width: 1271, height: 1900 } });
    const activadas = () => e.pagina.evaluate(() => (window as Cualquiera).__srfHarness.activadasIds());
    // a message from this very window is not a message from the frame
    await e.pagina.evaluate(() => window.postMessage({ tipo: 'superficie:pointerdown' }, '*'));
    await e.pagina.waitForTimeout(50);
    expect(await activadas()).toEqual([]);
    // a real click inside the first frame: the fake viewer posts the notice
    await e.pagina.mouse.click(600, 300);
    await e.pagina.waitForFunction(() => (window as Cualquiera).__srfHarness.activadasIds().length > 0, null, { timeout: 2000 });
    expect(await activadas()).toEqual(['srf-ng-6']);
    // the second card is held by an agent: its frame notifies (from the SAME origin as the first card's frame), yet the
    // second card does not activate and the first one, whose frame did not send it, does not either
    await e.pagina.mouse.click(600, 40 + LIENZO_H + 300);
    await e.pagina.waitForTimeout(150);
    expect(e.xpra.cargasDeMarco()).toBe(2); // both frames loaded once
    expect(await activadas()).toEqual(['srf-ng-6']);
  });

  it('reveals the overlays from the edge zone or the keyboard, hides them after 2 s, never swallows a click outside their own bounds (spec 3.1)', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p)], fijado: false });
    const estado = () => e.pagina.evaluate(() => ({ cabecera: document.querySelector('[data-cabecera]')!.getAttribute('data-visible'), franja: document.querySelector('[data-franja]')!.getAttribute('data-visible') }));
    const golpe = (x: number, y: number) => e.pagina.evaluate(([px, py]: number[]) => { const el = document.elementFromPoint(px, py)!; return el.tagName === 'IFRAME' ? 'iframe' : (el.getAttribute('data-zona') ?? el.tagName.toLowerCase()); }, [x, y]);
    expect(await estado()).toEqual({ cabecera: 'false', franja: 'false' });
    expect(await e.pagina.evaluate(() => getComputedStyle(document.querySelector('[data-cabecera]')!).pointerEvents)).toBe('none');
    // hidden: the strip's own bounds hold the armed edge zone, not the remote surface
    expect(await golpe(600, 40 + LIENZO_H - 10)).toBe('inferior');
    await e.pagina.mouse.move(600, 60); // into the top 64 px
    await e.pagina.waitForFunction(() => document.querySelector('[data-cabecera]')!.getAttribute('data-visible') === 'true', null, { timeout: 1000 });
    expect(await golpe(600, 40 + 28 + 12)).toBe('iframe'); // just below the header's 28 px: the remote surface, never swallowed
    expect(await golpe(600, 40 + 12)).toBe('header'); // inside its own bounds it may capture
    expect(await e.pagina.evaluate(() => getComputedStyle(document.querySelector('[data-franja]')!).pointerEvents)).toBe('none'); // the strip has no control: clicks pass through it
    await e.pagina.mouse.move(600, 500); // out of the header, over the remote surface
    await e.pagina.waitForFunction(() => document.querySelector('[data-cabecera]')!.getAttribute('data-visible') === 'false', null, { timeout: 4000 }); // auto-hide after 2 s
    // keyboard: a control of the hidden header takes focus and reveals it
    await e.pagina.evaluate(() => (document.querySelector('[data-accion="fijar"]') as HTMLElement).focus());
    await e.pagina.waitForFunction(() => document.querySelector('[data-cabecera]')!.getAttribute('data-visible') === 'true', null, { timeout: 1000 });
  });

  it('pins the telemetry strip whenever health is not healthy and lets it go when it recovers', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p)], fijado: false });
    const visible = () => e.pagina.evaluate(() => document.querySelector('[data-franja]')!.getAttribute('data-visible'));
    expect(await visible()).toBe('false');
    await e.pagina.evaluate(() => (window as Cualquiera).__srfHarness.poner('srf-ng-6', { health: 'degraded' }));
    expect(await visible()).toBe('true');
    await e.pagina.evaluate(() => (window as Cualquiera).__srfHarness.poner('srf-ng-6', { health: 'healthy' }));
    expect(await visible()).toBe('false');
  });

  it('repaints every affected card from AGENT-HELD to OPERATOR-CONTROL in the same frame (F-11)', async () => {
    const e = await escena({ capsulas: (p) => ['browser', 'desktop', 'microvm'].map((a, i) => capsulaDePrueba(a, p, { slot: 40 + i, id: `srf-ng-${40 + i}`, mode: 'agent_locked' })) });
    const antes = await e.pagina.evaluate(() => [...document.querySelectorAll('[data-insignia][data-clase="modo"] .texto')].map((x) => x.textContent));
    expect(antes).toEqual(['AGENT-HELD', 'AGENT-HELD', 'AGENT-HELD']);
    const r = await e.pagina.evaluate(() => {
      (window as Cualquiera).__srfHarness.ponerTodas({ mode: 'operator_controlled' }); // one synchronous batch: the barge-in
      const leido = [...document.querySelectorAll('[data-tarjeta]')].map((t) => ({ modo: (t as HTMLElement).dataset.modo, badge: t.querySelector('[data-insignia][data-clase="modo"] .texto')!.textContent, marco: getComputedStyle(t.querySelector('[data-marco-modo]')!).borderTopColor }));
      return { leido };
    });
    // read in the SAME task as the change, before any rAF: nothing waited for a later frame
    expect(r.leido).toEqual(Array(3).fill({ modo: 'operator_controlled', badge: 'OPERATOR-CONTROL', marco: 'rgb(0, 230, 118)' }));
  });

  it('keeps the same iframe through fold-like re-renders: geometry and health changes never navigate it', async () => {
    const e = await escena({ capsulas: (p) => [capsulaDePrueba('browser', p)] });
    await e.pagina.evaluate(() => { (window as Cualquiera).__nodo = document.querySelector('iframe'); });
    for (const cambio of [{ health: 'degraded' }, { mode: 'agent_locked' }, { mode: 'operator_controlled', health: 'healthy' }, { name: 'renamed' }]) {
      await e.pagina.evaluate((c: Cualquiera) => (window as Cualquiera).__srfHarness.poner('srf-ng-6', c), cambio);
    }
    expect(await e.pagina.evaluate(() => document.querySelector('iframe') === (window as Cualquiera).__nodo)).toBe(true);
    expect(e.xpra.cargasDeMarco()).toBe(1);
    expect(e.navegados.length).toBe(1); // the initial load only
  });
});

// =====================================================================================================================
// the hot-switch instrument against stand-ins, and the proof that it can go red on the property it measures
// =====================================================================================================================

describe.skipIf(!process.env.OSCU_BROWSER_TESTS)('hot-switch instrument (--conmutar-slot) against stand-ins', () => {
  let TMP = '';
  let tmpAnterior: string | undefined;
  beforeAll(() => {
    TMP = mkdtempSync(join(tmpdir(), 'srf-u20-conm-'));
    tmpAnterior = process.env.TMPDIR;
    process.env.TMPDIR = TMP;
  });
  afterAll(() => {
    if (tmpAnterior === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = tmpAnterior;
    if (TMP !== '') rmSync(TMP, { recursive: true, force: true });
  });

  function dependenciasFalsas(puerto: number, opciones: { conmutar?: () => Promise<unknown>; readonlyInicial?: boolean; msDeclarado?: number; construir?: (o: Cualquiera) => Promise<string> } = {}) {
    let ro = opciones.readonlyInicial ?? true;
    return {
      backend: {
        conmutar:
          opciones.conmutar ??
          (async (_slot: number, destino: string) => {
            await new Promise((r) => setTimeout(r, 120));
            ro = destino === 'solo_lectura';
            return { readonly: ro, ms: opciones.msDeclarado ?? 110, estado: ro ? 'shielded' : 'intervened' };
          }),
        sondear: (p: number) => sondearHttp(p),
      },
      puertos: async () => ({ plan: puerto, cdp: 9226, publicado: puerto }),
      estado: async () => ({ readonly: ro }),
      ...(opciones.construir !== undefined ? { construirHarness: opciones.construir } : {}),
    };
  }
  const ARGS = ['--conmutar-slot', '6', '--json', '--xpra-bin', process.execPath];
  const ORACULO = '.ms<800 and .iframe_recargado==false and .iframe_desmontado==false';

  it('measures a switch there and back: every field from two sources, the frame neither reloaded nor unmounted, jq green', async () => {
    const xpra = await xpraFalso();
    try {
      const { informe, rc } = (await ejecutar(ARGS, dependenciasFalsas(xpra.puerto))) as Cualquiera;
      expect([rc, informe.ok, informe.etapa, informe.error]).toEqual([0, true, 'completa', null]);
      expect(informe.acuerdo_entre_fuentes).toBe(true);
      expect(informe.desacuerdos).toEqual([]);
      expect(forma(informe)).toEqual(forma(informeVacio()));
      expect(informe.conmutaciones.map((c: Cualquiera) => c.sentido)).toEqual(['operador', 'solo_lectura']);
      for (const c of informe.conmutaciones) {
        expect(c.ms_backend_go).toBeLessThanOrEqual(c.ms_servidor + 1);
        expect(c.ms_servidor).toBeLessThanOrEqual(c.ms_pagina + 1);
        expect(c.ms_pagina).toBeLessThanOrEqual(c.ms_exterior + 1);
        expect(c.ms_pagina).toBeGreaterThan(100); // the stand-in backend takes 120 ms
        expect(c.iframe).toMatchObject({ mismo_nodo: true, conectado: true, src_igual: true, cargas_en_pagina: 0, removidos_en_pagina: 0, cdp_marcos_navegados: 0, cdp_marcos_separados: 0, time_origin_igual: true, marco_playwright_mismo: true });
        expect(c.readonly_despues).toBe(c.sentido === 'solo_lectura');
      }
      expect(informe.ms).toBe(Math.max(...informe.conmutaciones.map((c: Cualquiera) => c.ms_pagina)));
      expect([informe.iframe_recargado, informe.iframe_desmontado]).toEqual([false, false]);
      expect(jq(ORACULO, JSON.stringify(informe))).toBe(0);
      expect(xpra.cargasDeMarco()).toBe(1); // the far side: one load of the frame across two switches
    } finally {
      await xpra.cerrar();
    }
  });

  it('reports a switch the backend refuses as ok:false with the literal error and no measured value', async () => {
    const xpra = await xpraFalso();
    try {
      const { informe, rc } = (await ejecutar(ARGS, dependenciasFalsas(xpra.puerto, { conmutar: async () => Promise.reject(new Error('srf-xpra intervenir rc 1: boom')) }))) as Cualquiera;
      expect(rc).toBe(1);
      expect([informe.ok, informe.etapa]).toEqual([false, 'conmutacion:operador']);
      expect(informe.error).toContain('switch refused: srf-xpra intervenir rc 1: boom');
      expect([informe.ms, informe.iframe_recargado, informe.iframe_desmontado]).toEqual([null, null, null]);
      expect(forma(informe)).toEqual(forma(informeVacio()));
      expect(jq(ORACULO, JSON.stringify(informe))).not.toBe(0);
    } finally {
      await xpra.cerrar();
    }
  });

  it('reports rc 3 and no ms when the backend and the container disagree about the readonly flag', async () => {
    const xpra = await xpraFalso();
    try {
      const dependencias = dependenciasFalsas(xpra.puerto);
      dependencias.estado = async () => ({ readonly: true }); // the container never leaves read-only, whatever the backend answers
      const { informe, rc } = (await ejecutar(ARGS, dependencias)) as Cualquiera;
      expect([rc, informe.ok, informe.etapa]).toEqual([3, false, 'acuerdo_entre_fuentes']);
      expect(informe.campos_en_desacuerdo).toContain('readonly');
      expect(informe.desacuerdos.join(' ')).toContain('the container reads readonly=true afterwards');
      expect(forma(informe)).toEqual(forma(informeVacio()));
    } finally {
      await xpra.cerrar();
    }
  });

  it('reports rc 3 and null ms when the backend claims a longer switch than the endpoint around it observed', async () => {
    const xpra = await xpraFalso();
    try {
      const { informe, rc } = (await ejecutar(ARGS, dependenciasFalsas(xpra.puerto, { msDeclarado: 5000 }))) as Cualquiera;
      expect([rc, informe.ok]).toEqual([3, false]);
      expect(informe.campos_en_desacuerdo).toContain('ms');
      expect(informe.ms).toBeNull();
      expect(informe.desacuerdos.join(' ')).toContain("srf-xpra's own 5000 ms exceeds");
    } finally {
      await xpra.cerrar();
    }
  });

  it('turns red on the property it measures: a viewer that reloads itself during the switch is reported as reloaded', async () => {
    const xpra = await xpraFalso({ recargarse: true });
    try {
      const { informe, rc } = (await ejecutar(ARGS, dependenciasFalsas(xpra.puerto))) as Cualquiera;
      // both sources see the reload, so they agree: the instrument reports it instead of failing
      expect([rc, informe.ok]).toEqual([0, true]);
      expect(informe.iframe_recargado).toBe(true);
      expect(informe.iframe_desmontado).toBe(false);
      expect(jq(ORACULO, JSON.stringify(informe))).not.toBe(0);
      const c = informe.conmutaciones[0];
      expect(c.iframe.cargas_en_pagina + c.iframe.cdp_marcos_navegados).toBeGreaterThan(0);
    } finally {
      await xpra.cerrar();
    }
  });

  /** A scratch tree with the sources under test, in which `mutar` neuters or breaks one production line. */
  function arbolMutado(nombre: string, fichero: string, de: string, a: string): string {
    const raiz = join(TMP, nombre);
    mkdirSync(join(raiz, 'cockpit', 'frontend'), { recursive: true });
    cpSync(join(RAIZ_FRONTEND, 'src'), join(raiz, 'cockpit', 'frontend', 'src'), { recursive: true });
    symlinkSync(join(RAIZ_FRONTEND, 'node_modules'), join(raiz, 'cockpit', 'frontend', 'node_modules'), 'dir');
    const ruta = join(raiz, 'cockpit', 'frontend', 'src', 'lib', 'cards', fichero);
    const texto = readFileSync(ruta, 'utf8');
    if (!texto.includes(de)) throw new Error(`the line to mutate is not in ${fichero}: ${de}`);
    writeFileSync(ruta, texto.replace(de, a));
    return raiz;
  }

  it('turns red when the card re-mounts its frame on a mode change: unmounted and reloaded, from both sources', async () => {
    const raiz = arbolMutado(
      'mut-key',
      'CapsuleCard.svelte',
      '<XpraFrame puerto={capsula.xpraPort} tamano={lienzo} titulo={capsula.name} bind:iframe bind:envoltura />',
      '{#key modo}<XpraFrame puerto={capsula.xpraPort} tamano={lienzo} titulo={capsula.name} bind:iframe bind:envoltura />{/key}',
    );
    const xpra = await xpraFalso();
    try {
      const { informe, rc } = (await ejecutar(ARGS, dependenciasFalsas(xpra.puerto, { construir: (o) => construirHarness({ ...o, raiz }) }))) as Cualquiera;
      expect([rc, informe.ok, informe.acuerdo_entre_fuentes]).toEqual([0, true, true]);
      expect(informe.iframe_desmontado).toBe(true);
      expect(informe.iframe_recargado).toBe(true);
      expect(informe.conmutaciones[0].iframe).toMatchObject({ mismo_nodo: false });
      expect(jq(ORACULO, JSON.stringify(informe))).not.toBe(0);
      expect(xpra.cargasDeMarco()).toBeGreaterThan(1); // the far side saw the frame load again
    } finally {
      await xpra.cerrar();
    }
  });
});
