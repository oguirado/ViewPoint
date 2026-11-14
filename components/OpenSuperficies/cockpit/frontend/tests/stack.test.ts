// Tests of the multislot stack (U-19): the Fenwick height index, the window-mode layout and the virtual screens of
// src/lib/stack/, the shape of PilaMultislot.svelte, the pure parts of scripts/ng/medir-scroll.mjs, and the stack in a
// real headless Chromium through that instrument. Every expectation comes from a source OTHER than the code under test
// (a naive scan, the specification's text, hand-computed vectors, a second browser observation), and every test names a
// production symbol. evidencia/v2/u19/mutaciones-1.txt records, for each test, the production line that was neutered in
// a scratch copy and the red it produced.

import { execFile, spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

import { domNominal, modoPorAltura } from '../src/lib/geometria';
import { FenwickArbol } from '../src/lib/stack/fenwick';
import {
  PANTALLA_PRIMERA,
  alturasDeItems,
  asignarPantalla,
  cerrarPantalla,
  disposicion,
  estaOculta,
  herederaDe,
  modoDePila,
  paradaVecina,
  pantallaEfectiva,
  tarjetasPorVista,
  ultimaParada,
} from '../src/lib/stack/pantallas';
import {
  acuerdoDeP95,
  alturaEsperada,
  aplicarMutanteIf,
  compararCensos,
  derivarDisposicion,
  desacuerdosDeDesmontajes,
  duracionesDeEtapa,
  estadisticas,
  percentil,
  segmentosDeTraza,
} from '../../../scripts/ng/medir-scroll.mjs';

const RAIZ_REPO = fileURLToPath(new URL('../../../', import.meta.url));
const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');
const ESPEC = leer('../../../ESPEC-FRONTEND-COCKPIT-CLAUDE-DESIGN.md');
const COMPONENTE = leer('../src/lib/stack/PilaMultislot.svelte');

// ---- a naive oracle for the height column, written here and not borrowed from the code under test ---------------
function prng(semilla: number): () => number {
  let a = semilla >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const sumaPrevia = (h: number[], i: number): number => h.slice(0, i).reduce((a, b) => a + b, 0);
/** The card that contains offset y: the first whose end lies beyond y; the length when y is at or past the end. */
const indiceNaive = (h: number[], y: number): number => {
  let acumulado = 0;
  for (let i = 0; i < h.length; i++) {
    if (y < acumulado + h[i]) return i;
    acumulado += h[i];
  }
  return h.length;
};

function comprobarContra(arbol: FenwickArbol, h: number[], rnd: () => number): void {
  expect(arbol.longitud).toBe(h.length);
  for (let i = 0; i <= h.length; i++) expect(arbol.offsetDe(i)).toBe(sumaPrevia(h, i));
  expect(arbol.total()).toBe(sumaPrevia(h, h.length));
  const total = sumaPrevia(h, h.length);
  const consultas = new Set<number>([0, total, total + 7]);
  for (let i = 0; i <= h.length; i++) {
    const borde = sumaPrevia(h, i);
    consultas.add(borde);
    consultas.add(borde + 1);
    if (borde > 0) consultas.add(borde - 1);
  }
  for (let k = 0; k < 20; k++) consultas.add(Math.floor(rnd() * (total + 3)));
  for (const y of consultas) expect(arbol.indicePorOffset(y), `indicePorOffset(${y}) over ${JSON.stringify(h)}`).toBe(indiceNaive(h, y));
}

describe('FenwickArbol (src/lib/stack/fenwick.ts)', () => {
  it('answers offsetDe and indicePorOffset like a naive scan through random builds, updates and syncs', () => {
    const rnd = prng(1994);
    for (let prueba = 0; prueba < 60; prueba++) {
      const n = 1 + Math.floor(rnd() * 40);
      const h = Array.from({ length: n }, () => (rnd() < 0.3 ? 0 : (1 + Math.floor(rnd() * 5)) * 128));
      const arbol = FenwickArbol.desdeAlturas(h);
      comprobarContra(arbol, h, rnd);
      for (let u = 0; u < 15; u++) {
        const i = Math.floor(rnd() * n);
        const nueva = rnd() < 0.3 ? 0 : (1 + Math.floor(rnd() * 5)) * 128;
        arbol.fijar(i, nueva);
        h[i] = nueva;
        expect(arbol.altura(i)).toBe(nueva);
        comprobarContra(arbol, h, rnd);
      }
      const otra = h.map((x) => (rnd() < 0.4 ? (1 + Math.floor(rnd() * 5)) * 64 : x));
      const cambiadas = arbol.sincronizar(otra);
      expect(cambiadas).toBe(otra.filter((x, i) => x !== h[i]).length);
      comprobarContra(arbol, otra, rnd);
      const mas = [...otra, 300, 0, 500];
      arbol.sincronizar(mas);
      comprobarContra(arbol, mas, rnd);
      arbol.sincronizar(mas.slice(0, 2));
      comprobarContra(arbol, mas.slice(0, 2), rnd);
    }
    const vacio = FenwickArbol.desdeAlturas([]);
    expect(vacio.total()).toBe(0);
    expect(vacio.indicePorOffset(0)).toBe(0);
  });

  it('touches at most log2(n) + 1 tree nodes per operation', () => {
    for (const n of [24, 1000, 1 << 15]) {
      const arbol = FenwickArbol.desdeAlturas(Array.from({ length: n }, () => 1024));
      const cota = Math.floor(Math.log2(n)) + 1;
      arbol.offsetDe(n);
      expect(arbol.ultimoCoste, `offsetDe(n) with n = ${n}`).toBeLessThanOrEqual(cota);
      arbol.offsetDe(n - 1);
      expect(arbol.ultimoCoste).toBeLessThanOrEqual(cota);
      arbol.indicePorOffset(arbol.total() - 1);
      expect(arbol.ultimoCoste, `indicePorOffset with n = ${n}`).toBeLessThanOrEqual(cota);
      arbol.fijar(0, 2048);
      expect(arbol.ultimoCoste, `fijar(0) with n = ${n}`).toBeLessThanOrEqual(cota);
      expect(arbol.ultimoCoste).toBeGreaterThan(0);
    }
  });

  it('never answers a card of height 0, and answers the length at and past the end', () => {
    const arbol = FenwickArbol.desdeAlturas([0, 10, 0, 0, 10, 0]);
    expect(arbol.indicePorOffset(0)).toBe(1);
    expect(arbol.indicePorOffset(9)).toBe(1);
    expect(arbol.indicePorOffset(10)).toBe(4);
    expect(arbol.indicePorOffset(19)).toBe(4);
    expect(arbol.indicePorOffset(20)).toBe(6);
    expect(arbol.indicePorOffset(1e9)).toBe(6);
    expect(arbol.offsetDe(3)).toBe(10);
    expect(arbol.offsetDe(4)).toBe(10);
  });

  it('refuses heights that cannot be summed and indexes out of range, and a bad sync changes nothing', () => {
    expect(() => FenwickArbol.desdeAlturas([1, -1])).toThrow(RangeError);
    expect(() => FenwickArbol.desdeAlturas([Number.NaN])).toThrow(RangeError);
    expect(() => FenwickArbol.desdeAlturas([Number.POSITIVE_INFINITY])).toThrow(RangeError);
    const arbol = FenwickArbol.desdeAlturas([5, 6, 7]);
    expect(() => arbol.fijar(0, -3)).toThrow(RangeError);
    expect(() => arbol.fijar(3, 1)).toThrow(RangeError);
    expect(() => arbol.altura(1.5)).toThrow(RangeError);
    expect(() => arbol.offsetDe(4)).toThrow(RangeError);
    expect(() => arbol.offsetDe(-1)).toThrow(RangeError);
    expect(() => arbol.indicePorOffset(-1)).toThrow(RangeError);
    expect(() => arbol.indicePorOffset(Number.NaN)).toThrow(RangeError);
    const antes = [arbol.offsetDe(0), arbol.offsetDe(1), arbol.offsetDe(2), arbol.offsetDe(3), arbol.generacion];
    expect(() => arbol.sincronizar([9, 9, -9])).toThrow(RangeError);
    expect(() => arbol.sincronizar([9, -9])).toThrow(RangeError);
    expect([arbol.offsetDe(0), arbol.offsetDe(1), arbol.offsetDe(2), arbol.offsetDe(3), arbol.generacion]).toEqual(antes);
  });
});

// ---- the layout of a window mode, against the specification's own sentences ----------------------------------------
const numero = (m: RegExpExecArray | null, grupo: number, que: string): number => {
  expect(m, `the specification has no ${que}`).not.toBeNull();
  return Number(m![grupo]);
};

describe('layout of a window mode (src/lib/stack/pantallas.ts)', () => {
  it('takes the layout of every mode from the specification figures (1.2, 1.3, 2.2)', () => {
    const utilSexto = /\| \*\*1\/6\*\* — one tile \|[^|]*\|[^|]*\|[^|]*\| (\d+) × (\d+) \|/.exec(ESPEC);
    const utilAlto = /\| \*\*1\/3 tall\*\*[^|]*\|[^|]*\|[^|]*\|[^|]*\| (\d+) × (\d+) \|/.exec(ESPEC);
    const lienzoSexto = /\| 1\/6 \|[^\n]*each card is exactly (\d+) px tall/.exec(ESPEC);
    const pasoDual = /\| 1\/3 dual \|[^\n]*snap points every (\d+) px[^\n]*(two) cards visible/.exec(ESPEC);
    const alturaAlto = /\| 1\/3 tall \| one card per viewport, (\d+) px tall/.exec(ESPEC);
    const anchoLienzo = /\| \*\*1\/6\*\* — one tile[^\n]*\*\*(\d+) × (\d+)\*\* \|/.exec(ESPEC);
    expect(ESPEC).toMatch(/In 1\/6 mode the stack shows \*\*exactly one card per viewport\*\*/);

    const sexto = disposicion('tile_1_6');
    expect(sexto.alturaVista).toBe(numero(utilSexto, 2, 'usable viewport of 1/6'));
    expect(sexto.alturaTarjeta).toBe(numero(lienzoSexto, 1, 'card height of 1/6'));
    expect(sexto.paso).toBe(numero(lienzoSexto, 1, 'card height of 1/6'));
    expect(sexto.alturaBarra).toBe(0);
    expect(sexto.tarjetasPorVista).toBe(1);
    expect(sexto.ancho).toBe(numero(utilSexto, 1, 'usable width'));

    const dual = disposicion('column_1_3_dual');
    expect(dual.alturaVista).toBe(numero(utilAlto, 2, 'usable viewport of 1/3'));
    expect(dual.paso).toBe(numero(pasoDual, 1, 'snap step of 1/3 dual'));
    expect(dual.alturaTarjeta).toBe(numero(lienzoSexto, 1, 'card height'));
    expect(dual.alturaBarra).toBe(dual.paso - dual.alturaTarjeta);
    expect(dual.tarjetasPorVista).toBe(pasoDual![2] === 'two' ? 2 : -1);

    const alto = disposicion('column_1_3_tall');
    expect(alto.alturaVista).toBe(numero(utilAlto, 2, 'usable viewport of 1/3'));
    expect(alto.alturaTarjeta).toBe(numero(alturaAlto, 1, 'tall card height'));
    expect(alto.paso).toBe(alto.alturaVista);
    expect(alto.tarjetasPorVista).toBe(1);
    expect(alto.alturaTarjeta).toBeLessThanOrEqual(alto.alturaVista);

    expect(sexto.anchoTarjeta).toBe(numero(anchoLienzo, 1, 'canvas width'));
    for (const d of [sexto, dual, alto]) {
      expect(tarjetasPorVista(d.modo)).toBe(d.tarjetasPorVista);
      // the cards of a view and the bars between them fill the viewport exactly (dual) or leave the spec's slack (tall)
      const ocupado = d.tarjetasPorVista * d.alturaTarjeta + (d.tarjetasPorVista - 1) * d.alturaBarra;
      expect(ocupado).toBeLessThanOrEqual(d.alturaVista);
    }
    expect(dual.tarjetasPorVista * dual.alturaTarjeta + (dual.tarjetasPorVista - 1) * dual.alturaBarra).toBe(dual.alturaVista);
  });

  it('maps the CLI 1/3 to the dual layout while geometria only knows the tall column', () => {
    expect(modoDePila('1/6')).toBe('tile_1_6');
    expect(modoDePila('1/3')).toBe('column_1_3_dual');
    // The documented gap: a DOM height cannot tell one surface from two, so geometria.modoPorAltura says `tall` for 1/3.
    expect(modoPorAltura(domNominal('1/3').h)).toBe('column_1_3_tall');
  });

  it('gives a hidden item no height and the last visible item no card bar', () => {
    expect(alturasDeItems('column_1_3_dual', [false, true, false, false])).toEqual([1064, 0, 1064, 1024]);
    expect(alturasDeItems('column_1_3_dual', [false, false, true])).toEqual([1064, 1024, 0]);
    expect(alturasDeItems('tile_1_6', [false, false])).toEqual([1024, 1024]);
    expect(alturasDeItems('column_1_3_tall', [false, false])).toEqual([2088, 2088]);
    expect(alturasDeItems('column_1_3_dual', [true, true])).toEqual([0, 0]);
    expect(alturasDeItems('tile_1_6', [])).toEqual([]);
  });

  it('pages over the height index: last stop, neighbours, hidden cards skipped, never past either end', () => {
    const dual = FenwickArbol.desdeAlturas(alturasDeItems('column_1_3_dual', Array(24).fill(false)));
    const vistaDual = disposicion('column_1_3_dual').alturaVista;
    expect(ultimaParada(dual, vistaDual)).toBe(22); // the last view starts at card 22 and shows 22 and 23
    expect(paradaVecina(dual, 0, 1, vistaDual)).toBe(1);
    expect(paradaVecina(dual, 21, 1, vistaDual)).toBe(22);
    expect(paradaVecina(dual, 22, 1, vistaDual)).toBe(22);
    expect(paradaVecina(dual, 0, -1, vistaDual)).toBe(0);
    expect(paradaVecina(dual, 5, -1, vistaDual)).toBe(4);
    const sexto = FenwickArbol.desdeAlturas(alturasDeItems('tile_1_6', Array(24).fill(false)));
    expect(ultimaParada(sexto, disposicion('tile_1_6').alturaVista)).toBe(23);
    const ocultas = Array(10).fill(false);
    ocultas[3] = true;
    ocultas[4] = true;
    const conHuecos = FenwickArbol.desdeAlturas(alturasDeItems('tile_1_6', ocultas));
    const vista = disposicion('tile_1_6').alturaVista;
    expect(paradaVecina(conHuecos, 2, 1, vista)).toBe(5);
    expect(paradaVecina(conHuecos, 5, -1, vista)).toBe(2);
    expect(ultimaParada(FenwickArbol.desdeAlturas([]), vista)).toBe(0);
    expect(paradaVecina(FenwickArbol.desdeAlturas([]), 0, 1, vista)).toBe(0);
  });
});

describe('virtual screens (src/lib/stack/pantallas.ts)', () => {
  const pantallas = [{ id: 'p1' }, { id: 'p2' }, { id: 'p3' }];
  const tarjetas = [
    { id: 'a', pantalla: 'p1' },
    { id: 'b', pantalla: 'p2' },
    { id: 'c', pantalla: 'p3' },
    { id: 'd', pantalla: 'p2', plegada: true },
    { id: 'e' },
    { id: 'f', pantalla: 'zz' },
  ];

  it('INV-PANTALLA-02: a card with a missing or unknown screen belongs to the first one', () => {
    expect(PANTALLA_PRIMERA).toBe('p1');
    expect(pantallaEfectiva(pantallas, undefined)).toBe('p1');
    expect(pantallaEfectiva(pantallas, 'zz')).toBe('p1');
    expect(pantallaEfectiva(pantallas, 'p3')).toBe('p3');
    expect(pantallaEfectiva([{ id: 'x' }, { id: 'y' }], 'zz')).toBe('x'); // the first screen of the list, whatever its id
    expect(pantallaEfectiva([], 'zz')).toBe(PANTALLA_PRIMERA);
  });

  it('INV-PANTALLA-01: a card is hidden when it is folded or on another screen, and only then', () => {
    const ocultas = (activa: string) => tarjetas.map((t) => estaOculta(pantallas, t, activa));
    expect(ocultas('p1')).toEqual([false, true, true, true, false, false]); // e and f are orphans: they are on p1
    expect(ocultas('p2')).toEqual([true, false, true, true, true, true]); // d is on p2 but folded
    expect(ocultas('p3')).toEqual([true, true, false, true, true, true]);
    expect(estaOculta(pantallas, { id: 'g', pantalla: 'p1', plegada: false }, 'p1')).toBe(false);
  });

  it('INV-PANTALLA-03: the last screen cannot be closed and an unknown one is refused', () => {
    const una = [{ id: 'p1' }];
    expect(cerrarPantalla(una, [{ id: 'a', pantalla: 'p1' }], 'p1')).toEqual({ ok: false, motivo: 'ultima_pantalla' });
    expect(cerrarPantalla(pantallas, tarjetas, 'p9')).toEqual({ ok: false, motivo: 'pantalla_desconocida' });
    expect(() => herederaDe(una, 'p1')).toThrow(RangeError);
    expect(() => herederaDe(pantallas, 'p9')).toThrow(RangeError);
  });

  it('INV-PANTALLA-04: closing a screen hands its cards to the heir and keeps their identity', () => {
    expect(herederaDe(pantallas, 'p2')).toBe('p1'); // the previous screen
    expect(herederaDe(pantallas, 'p1')).toBe('p2'); // the first screen has no previous one: the next
    expect(herederaDe(pantallas, 'p3')).toBe('p2');
    const antes = JSON.stringify({ pantallas, tarjetas });
    const r = cerrarPantalla(pantallas, tarjetas, 'p2');
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.heredera).toBe('p1');
    expect(r.pantallas.map((p) => p.id)).toEqual(['p1', 'p3']);
    expect(r.elementos.map((t) => t.id)).toEqual(tarjetas.map((t) => t.id)); // nothing dropped, same order
    expect(r.elementos.map((t) => t.pantalla)).toEqual(['p1', 'p1', 'p3', 'p1', undefined, 'zz']);
    expect(r.elementos[3].plegada).toBe(true); // a fold survives the move
    expect(JSON.stringify({ pantallas, tarjetas })).toBe(antes); // pure
  });

  it('asignarPantalla moves one card, sends an unknown screen to the first, and refuses an unknown card', () => {
    const r = asignarPantalla(pantallas, tarjetas, 'a', 'p3');
    expect(r.map((t) => t.pantalla)).toEqual(['p3', 'p2', 'p3', 'p2', undefined, 'zz']);
    expect(asignarPantalla(pantallas, tarjetas, 'c', 'nope')[2].pantalla).toBe('p1');
    expect(tarjetas[0].pantalla).toBe('p1'); // the input is untouched
    expect(() => asignarPantalla(pantallas, tarjetas, 'nope', 'p1')).toThrow(RangeError);
  });
});

// ---- the component's shape ---------------------------------------------------------------------------------------
type Nodo = { type: string; [k: string]: unknown };
function recorrer(n: unknown, ancestros: Nodo[], f: (n: Nodo, ancestros: Nodo[]) => void): void {
  if (n === null || typeof n !== 'object') return;
  if (Array.isArray(n)) {
    for (const x of n) recorrer(x, ancestros, f);
    return;
  }
  const nodo = n as Nodo;
  if (typeof nodo.type === 'string') {
    f(nodo, ancestros);
    ancestros = [...ancestros, nodo];
  }
  for (const [k, v] of Object.entries(nodo)) if (k !== 'metadata' && k !== 'parent') recorrer(v, ancestros, f);
}

describe('PilaMultislot.svelte (permanent mounting, spec 2.1)', () => {
  it('renders the live surface with no conditional ancestor and keys its cards by id', () => {
    const ast = parse(COMPONENTE, { modern: true }) as unknown as { fragment: Nodo };
    const superficies: Array<{ ancestros: string[]; clave: boolean }> = [];
    recorrer(ast.fragment, [], (n, ancestros) => {
      const llamada = (n.expression as { callee?: { name?: string } } | undefined)?.callee?.name;
      if (n.type === 'RenderTag' && llamada === 'tarjeta') {
        const cadaUno = [...ancestros].reverse().find((a) => a.type === 'EachBlock');
        superficies.push({ ancestros: ancestros.map((a) => a.type), clave: cadaUno !== undefined && cadaUno.key !== undefined && cadaUno.key !== null });
      }
    });
    expect(superficies).toHaveLength(1);
    const { ancestros, clave } = superficies[0];
    for (const condicional of ['IfBlock', 'KeyBlock', 'AwaitBlock', 'SnippetBlock']) expect(ancestros, `the surface sits under a ${condicional}`).not.toContain(condicional);
    expect(ancestros).toContain('EachBlock');
    expect(clave, 'the each block that mounts the cards must be keyed').toBe(true);
    expect(COMPONENTE.split('<!-- SRF-SUPERFICIE-INICIO -->')).toHaveLength(2);
    expect(COMPONENTE.split('<!-- SRF-SUPERFICIE-FIN -->')).toHaveLength(2);
  });
});

// ---- the pure parts of the instrument ------------------------------------------------------------------------------
describe('scripts/ng/medir-scroll.mjs (pure parts)', () => {
  it('derives the viewport and layout by its own arithmetic, equal to what pantallas.ts derives', () => {
    for (const modo of ['1/6', '1/3'] as const) {
      const i = derivarDisposicion(modo);
      const p = disposicion(modoDePila(modo));
      expect(i.modo_pila).toBe(p.modo);
      expect(i.paso).toBe(p.paso);
      expect(i.alturaVista).toBe(p.alturaVista);
      expect(i.alturaBarra).toBe(p.alturaBarra);
      expect(i.lienzo).toEqual({ w: p.anchoTarjeta, h: p.alturaTarjeta });
      expect(i.tarjetasPorVista).toBe(p.tarjetasPorVista);
      expect(i.dom).toEqual(domNominal(modo));
      expect(i.cierra).toBe(true);
      for (const visibles of [0, 1, 2, 5, 24]) {
        const alturas = alturasDeItems(p.modo, Array(visibles).fill(false));
        expect(alturaEsperada(i, visibles), `${modo} with ${visibles} visible`).toBe(FenwickArbol.desdeAlturas(alturas).total());
      }
    }
    expect(() => derivarDisposicion('1/2')).toThrow(RangeError);
  });

  it('takes nearest-rank percentiles and counts the frames over the budget', () => {
    const veinte = Array.from({ length: 20 }, (_, i) => i + 1);
    expect(percentil(veinte, 0.95)).toBe(19); // rank ceil(0.95 x 20) = 19
    expect(percentil(veinte, 0.5)).toBe(10);
    expect(percentil(veinte, 1)).toBe(20);
    expect(percentil([5], 0.95)).toBe(5);
    expect(percentil([3, 1, 2], 0.5)).toBe(2);
    expect(percentil([], 0.5)).toBeNull();
    const e = estadisticas([1, 2, 3, 16.6, 16.7, 40]);
    expect(e.n).toBe(6);
    expect(e.sobre_presupuesto).toBe(2); // 16.6 is on the budget, not over it
    expect(e.max_ms).toBe(40);
    expect(e.p95_ms).toBe(40);
  });

  it('judges agreement between sources: the p95 of two views, the unmount counters, the surface census', () => {
    expect(acuerdoDeP95(1.8, 1.06)).toBe(true);
    expect(acuerdoDeP95(45.7, 31.0)).toBe(true);
    expect(acuerdoDeP95(1.0, 20)).toBe(false);
    expect(acuerdoDeP95(null, 1)).toBe(false);
    expect(acuerdoDeP95(1, null)).toBe(false);

    const censo0 = { intactas: 24, cambiadas: 0, perdidas: 0, nuevas: 0 };
    const buena = { estable: true, dom: 0, svelte: 0, marcos: 0, vivasAlArmar: 24, montados: 0, vivasAlFinal: 24, censo: censo0 };
    expect(desacuerdosDeDesmontajes(buena)).toEqual([]);
    expect(desacuerdosDeDesmontajes({ ...buena, svelte: 1 }).join('|')).toMatch(/DOM observer 0 vs Svelte teardown 1/);
    expect(desacuerdosDeDesmontajes({ ...buena, marcos: 2 }).join('|')).toMatch(/frames detached \(Playwright\) 2/);
    expect(desacuerdosDeDesmontajes({ ...buena, estable: false }).join('|')).toMatch(/did not settle/);
    expect(desacuerdosDeDesmontajes({ ...buena, vivasAlFinal: 23 }).join('|')).toMatch(/bookkeeping/);
    expect(desacuerdosDeDesmontajes({ ...buena, censo: { ...censo0, cambiadas: 1 } }).join('|')).toMatch(/no unmount was counted, yet 1 surface/);
    // three sources that agree on 5 unmounts: consistent, whatever the census says
    expect(desacuerdosDeDesmontajes({ estable: true, dom: 5, svelte: 5, marcos: 5, vivasAlArmar: 3, montados: 7, vivasAlFinal: 5, censo: { intactas: 0, cambiadas: 3, perdidas: 0, nuevas: 2 } })).toEqual([]);

    expect(compararCensos([{ id: 'a', nonce: 'x' }, { id: 'b', nonce: 'y' }, { id: 'c', nonce: 'z' }], [{ id: 'a', nonce: 'x' }, { id: 'b', nonce: 'Q' }, { id: 'd', nonce: 'w' }])).toEqual({ intactas: 1, cambiadas: 1, perdidas: 1, nuevas: 1 });
    expect(compararCensos([{ id: 'a', nonce: null }], [{ id: 'a', nonce: null }])).toEqual({ intactas: 0, cambiadas: 1, perdidas: 0, nuevas: 0 }); // a surface that cannot show a nonce is not vouched for
  });

  it('reads the scroll segments and the compositor stage durations out of a trace', () => {
    const marca = (ts: number, mensaje: string) => ({ name: 'TimeStamp', ph: 'I', ts, args: { data: { message: mensaje } } });
    const etapa = (ph: string, ts: number, id: string, nombre = 'SendBeginMainFrameToCommit') => ({ name: nombre, ph, ts, id2: { local: id } });
    const eventos = [
      marca(1000, 'srf:ini:1'),
      etapa('b', 1100, '0xa'),
      etapa('e', 1600, '0xa'),
      etapa('b', 1700, '0xb'),
      etapa('e', 2400, '0xb'), // begins inside, ends after the segment: counted by its begin
      marca(2000, 'srf:fin:1'),
      etapa('b', 5000, '0xc'), // outside any segment
      etapa('e', 5900, '0xc'),
      marca(3000, 'srf:ini:2'), // never closed: not a segment
      etapa('b', 1200, '0xd', 'BeginImplFrameToSendBeginMainFrame'), // another stage
      etapa('e', 1900, '0xd', 'BeginImplFrameToSendBeginMainFrame'),
      { name: 'TimeStamp', ph: 'I', ts: 10, args: { data: { message: 'something else' } } },
    ];
    const segmentos = segmentosDeTraza(eventos);
    expect(segmentos).toEqual([{ id: 1, ini: 1000, fin: 2000 }]);
    expect(duracionesDeEtapa(eventos, 'SendBeginMainFrameToCommit', segmentos)).toEqual([0.5, 0.7]);
    expect(duracionesDeEtapa(eventos, 'BeginImplFrameToSendBeginMainFrame', segmentos)).toEqual([0.7]);
    expect(duracionesDeEtapa(eventos, 'SendBeginMainFrameToCommit', [])).toEqual([]);
  });

  it('wraps only the surface in {#if visible} and refuses a source without exactly one marked surface', () => {
    const mutado = aplicarMutanteIf(COMPONENTE);
    expect(mutado).not.toBe(COMPONENTE);
    const dentro = /<!-- SRF-SUPERFICIE-INICIO -->([\s\S]*?)<!-- SRF-SUPERFICIE-FIN -->/.exec(mutado)![1].trim();
    expect(dentro).toMatch(/^\{#if visible\}[\s\S]*\{@render tarjeta\(t, i\)\}[\s\S]*\{\/if\}$/);
    // nothing else was touched: undoing the two insertions gives the original back
    expect(mutado.replace('<!-- SRF-SUPERFICIE-INICIO -->{#if visible}', '<!-- SRF-SUPERFICIE-INICIO -->').replace('{/if}<!-- SRF-SUPERFICIE-FIN -->', '<!-- SRF-SUPERFICIE-FIN -->')).toBe(COMPONENTE);

    const rechazo = (fuente: string): { rc?: number; etapa?: string; message: string } => {
      try {
        aplicarMutanteIf(fuente);
      } catch (e) {
        return e as { rc?: number; etapa?: string; message: string };
      }
      throw new Error('aplicarMutanteIf accepted a source it must refuse');
    };
    const sinMarcadores = rechazo(COMPONENTE.replace('<!-- SRF-SUPERFICIE-INICIO -->', ''));
    expect(sinMarcadores.rc).toBe(2);
    expect(sinMarcadores.etapa).toBe('precondicion');
    expect(sinMarcadores.message).toMatch(/exactly once/);
    const duplicado = COMPONENTE + '\n<!-- SRF-SUPERFICIE-INICIO -->x<!-- SRF-SUPERFICIE-FIN -->';
    expect(rechazo(duplicado).message).toMatch(/found 2/);
    const vacio = rechazo(COMPONENTE.replace(/(<!-- SRF-SUPERFICIE-INICIO -->)[\s\S]*?(<!-- SRF-SUPERFICIE-FIN -->)/, '$1 $2'));
    expect(vacio.message).toMatch(/do not enclose/);
  });
});

// ---- the stack in a real headless Chromium, through the instrument -------------------------------------------------
const INSTRUMENTO = join(RAIZ_REPO, 'scripts', 'ng', 'medir-scroll.mjs');
type Informe = Record<string, any>;
interface Medicion {
  rc: number | null;
  stdout: string;
  stderr: string;
  informe: Informe;
}

function ejecutarProceso(args: string[], entorno: Record<string, string>): Promise<{ rc: number | null; stdout: string; stderr: string }> {
  return new Promise((resolver) => {
    execFile(process.execPath, [INSTRUMENTO, ...args], { cwd: RAIZ_REPO, env: { ...process.env, ...entorno }, timeout: 170_000, killSignal: 'SIGTERM', maxBuffer: 64 << 20 }, (error, stdout, stderr) => {
      const codigo = (error as (Error & { code?: unknown }) | null)?.code;
      resolver({ rc: error === null ? 0 : typeof codigo === 'number' ? codigo : null, stdout, stderr });
    });
  });
}

/** One run of the instrument as a subprocess, exactly as the oracle runs it; its stdout must always be ONE JSON report. */
async function medir(args: string[], entorno: Record<string, string> = {}): Promise<Medicion> {
  const p = await ejecutarProceso(args, entorno);
  let informe: Informe;
  try {
    informe = JSON.parse(p.stdout);
  } catch {
    throw new Error(`medir-scroll wrote no single JSON report on stdout (rc ${p.rc}):\nstdout: ${p.stdout}\nstderr: ${p.stderr}`);
  }
  return { ...p, informe };
}

const memo = new Map<string, Promise<Medicion>>();
/** A measurement of the tree; the same arguments are measured once per run of this file. */
function real(modo: '1/6' | '1/3', extra: string[] = []): Promise<Medicion> {
  const clave = [modo, ...extra].join(' ');
  let m = memo.get(clave);
  if (m === undefined) {
    m = medir(['--tarjetas', '24', '--modo', modo, ...extra, '--json']);
    memo.set(clave, m);
  }
  return m;
}

/** The exit status of `jq -e <filtro>` on a report: how the oracle judges it (0 true, 1 false or null). */
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

/** p95 by nearest rank, written here from the definition and not borrowed from the instrument. */
function p95DeMuestras(muestras: number[]): number {
  const orden = [...muestras].sort((a, b) => a - b);
  return orden[Math.ceil(0.95 * orden.length) - 1];
}

describe.skipIf(!process.env.OSCU_BROWSER_TESTS)('the stack in a real browser (scripts/ng/medir-scroll.mjs)', () => {
  it('1/6: 24 cards stay mounted, one is in view, and every source agrees (the oracle predicate holds)', async () => {
    const m = await real('1/6');
    const r = m.informe;
    expect(m.rc, m.stderr).toBe(0);
    expect(r.ok).toBe(true);
    expect(r.etapa).toBe('completa');
    expect(jq('.p95_frame_ms<=16.6 and .desmontajes==0 and .tarjetas_por_vista==1', m.stdout)).toBe(0);
    expect(r.acuerdo_entre_fuentes).toBe(true);
    expect(r.desacuerdos).toEqual([]);
    // the geometry the page really has, at the size the instrument set
    expect(r.dom).toBe('1271x1064');
    expect(r.tarjetas).toBe(24);
    expect(r.tramos.paginado.ancho_de_la_pila).toBe(1271);
    expect(r.tramos.paginado.alto_de_la_pila).toBe(1024);
    expect(r.tramos.paginado.scroll_height).toBe(24 * 1024);
    expect(r.tramos.paginado.tarjetas_vistas_al_menos_una_vez).toBe(24);
    // three counters of one event, none of them the DOM-free bookkeeping of the component
    expect(r.desmontajes_fuentes).toEqual({ dom_mutation_observer: 0, svelte_desmontados: 0, marcos_desprendidos_playwright: 0 });
    expect(r.desmontajes).toBe(0);
    expect(r.montajes.vivas_al_final).toBe(24);
    expect(r.superficies).toMatchObject({ al_inicio: 24, al_final: 24, intactas: 24, cambiadas: 0, perdidas: 0, nuevas: 0, no_leidas: 0 });
    expect(r.desmontajes_por_tramo).toEqual({ paginado: 0, teclado: 0, plegado: 0, pantallas: 0 });
    // cards per view: two observations at every stop
    expect(r.tarjetas_por_vista_fuentes.por_parada_pagina).toHaveLength(24);
    expect(new Set(r.tarjetas_por_vista_fuentes.por_parada_pagina)).toEqual(new Set([1]));
    expect(new Set(r.tarjetas_por_vista_fuentes.por_parada_cdp)).toEqual(new Set([1]));
    // the component's height index against the browser's layout, and the snap arithmetic
    expect(r.fenwick_contra_dom.comparaciones).toBeGreaterThan(500);
    expect(r.fenwick_contra_dom.discrepancias).toBe(0);
    expect(r.paradas).toMatchObject({ esperadas: 24, alcanzadas: 24, desalineadas: 0 });
    expect(r.indice_actual).toMatchObject({ comprobaciones: 24, discrepancias: 0 });
    // fold and screen swap only hide
    expect(r.tramos.plegado).toMatchObject({ ocultas_en_dom: 2, superficies_en_dom_plegado: 24, scroll_height_plegado: 22 * 1024, scroll_height_despues: 24 * 1024 });
    expect(r.tramos.pantallas.estados.map((e: Record<string, number>) => e.visibles)).toEqual([12, 12, 12, 24]);
    expect(r.tramos.pantallas.estados.every((e: Record<string, number>) => e.superficies_en_dom === 24)).toBe(true);
    // the frame figure: derived from the published samples, and seen by the compositor too
    const muestras: number[] = r.frames.muestras_pagina_ms;
    expect(muestras.length).toBe(r.frames.pagina.n);
    expect(muestras.length).toBeGreaterThanOrEqual(60);
    expect(r.p95_frame_ms).toBe(p95DeMuestras(muestras));
    expect(r.frames.traza.n).toBeGreaterThanOrEqual(60);
    expect(r.frames.traza.etapa).toBe('SendBeginMainFrameToCommit');
    expect(r.frames.acuerdo_p95).toBe(true);
    expect(r.frames.segmentos).toEqual({ esperados: 23, en_la_traza: 23 });
    expect(r.frames.cadencia.perdidos).toBe(0);
    expect(r.mutante).toMatchObject({ pedido: false, aplicado: false });
    expect(r.mutante.componente_sha256).toBe(createHash('sha256').update(COMPONENTE).digest('hex'));
    expect(r.mutante.compilado_sha256).toBe(r.mutante.componente_sha256);
  });

  it('occludes what is more than a viewport away with content-visibility hidden and the pause callback, and keeps it mounted', async () => {
    const r = (await real('1/6')).informe;
    expect(r.oclusion.comprobaciones).toBeGreaterThan(400);
    expect(r.oclusion.discrepancias_atributo).toBe(0);
    expect(r.oclusion.discrepancias_css).toBe(0);
    expect(r.oclusion.incoherentes_con_el_dom).toBe(0);
    expect(r.oclusion.llamadas_al_adaptador).toBeGreaterThan(24); // cards entered and left the band while paging
    expect(r.oclusion.ocluidas_al_final).toBeGreaterThan(15);
    // occluded, and still mounted
    expect(r.desmontajes).toBe(0);
    expect(r.montajes.vivas_al_final).toBe(24);
  });

  it('1/3: two cards are in view and the layout steps by card plus card bar', async () => {
    const m = await real('1/3');
    const r = m.informe;
    expect(m.rc, m.stderr).toBe(0);
    expect(r.ok).toBe(true);
    expect(jq('.desmontajes==0 and .tarjetas_por_vista==2', m.stdout)).toBe(0);
    expect(r.dom).toBe('1271x2128');
    expect(r.modo_pila).toBe('column_1_3_dual');
    expect(r.esperado).toMatchObject({ tarjetas_por_vista: 2, paso: 1064, alto_vista: 2088, cierra: true });
    expect(r.tramos.paginado.alto_de_la_pila).toBe(2088);
    expect(r.tramos.paginado.scroll_height).toBe(23 * 1064 + 1024);
    expect(r.paradas).toMatchObject({ esperadas: 23, alcanzadas: 23, desalineadas: 0 });
    expect(new Set(r.tarjetas_por_vista_fuentes.por_parada_pagina)).toEqual(new Set([2]));
    expect(new Set(r.tarjetas_por_vista_fuentes.por_parada_cdp)).toEqual(new Set([2]));
    expect(r.tramos.paginado.tarjetas_vistas_al_menos_una_vez).toBe(24);
    expect(r.tramos.teclado).toMatchObject({ pulsaciones: 6, fuera_de_lugar: 0 });
    expect(r.fenwick_contra_dom.discrepancias).toBe(0);
    expect(r.desmontajes_fuentes).toEqual({ dom_mutation_observer: 0, svelte_desmontados: 0, marcos_desprendidos_playwright: 0 });
    expect(r.superficies.intactas).toBe(24);
    expect(r.acuerdo_entre_fuentes).toBe(true);
  });

  it('the contrast: wrapping the surface in {#if visible} makes the unmount counter rise, on the real component path', async () => {
    const bueno = await real('1/6');
    const m = await real('1/6', ['--envolver-if']);
    const r = m.informe;
    // it fails for the right reason: the measurement completed and every source agrees; only the property is gone
    expect(m.rc, m.stderr).toBe(0);
    expect(r.ok).toBe(true);
    expect(r.acuerdo_entre_fuentes).toBe(true);
    expect(jq('.desmontajes==0', m.stdout)).toBe(1);
    expect(r.desmontajes).toBeGreaterThan(20);
    expect(r.desmontajes_fuentes.dom_mutation_observer).toBe(r.desmontajes);
    expect(r.desmontajes_fuentes.svelte_desmontados).toBe(r.desmontajes);
    expect(r.desmontajes_fuentes.marcos_desprendidos_playwright).toBe(r.desmontajes);
    expect(r.montajes.superficies_anadidas_al_dom).toBeGreaterThan(20);
    // and it rises while paging: that is where the mutator says it must
    expect(r.desmontajes_por_tramo.paginado).toBeGreaterThan(15);
    expect(Object.values(r.desmontajes_por_tramo).reduce((a: number, b) => a + (b as number), 0)).toBeLessThanOrEqual(r.desmontajes);
    expect(r.superficies.cambiadas + r.superficies.perdidas).toBeGreaterThan(0);
    // the mutation is in the component that was compiled, not in a number, and the file on disk was never touched
    expect(r.mutante).toMatchObject({ pedido: true, aplicado: true, marcadores: 1 });
    expect(r.mutante.compilado_sha256).not.toBe(r.mutante.componente_sha256);
    expect(r.mutante.componente_sha256).toBe(createHash('sha256').update(readFileSync(join(RAIZ_REPO, 'cockpit', 'frontend', 'src', 'lib', 'stack', 'PilaMultislot.svelte'))).digest('hex'));
    expect(r.mutante.componente_sha256).toBe(bueno.informe.mutante.componente_sha256);
    // everything else still holds: the same layout, the same cards per view, the same snap points
    expect(r.tarjetas_por_vista).toBe(1);
    expect(r.paradas.desalineadas).toBe(0);
    expect(r.fenwick_contra_dom.discrepancias).toBe(0);
  });

  it('the frame gate can go red: a 30 ms stall in the scroll handler raises the p95 over the budget, seen by both sources', async () => {
    const m = await real('1/6', ['--estres-ms', '30']);
    const r = m.informe;
    expect(m.rc, m.stderr).toBe(0);
    expect(r.ok).toBe(true);
    expect(r.estres_ms).toBe(30);
    expect(r.p95_frame_ms).toBeGreaterThan(16.6);
    expect(jq('.p95_frame_ms<=16.6', m.stdout)).toBe(1);
    expect(r.frames.traza.p95_ms).toBeGreaterThan(16.6);
    expect(r.frames.acuerdo_p95).toBe(true);
    expect(r.frames.pagina.sobre_presupuesto).toBeGreaterThan(50);
    expect(r.desmontajes).toBe(0); // the stall does not touch the mounting
  });

  it('writes ONE report of the same shape on every failure path, with the measured fields null', async () => {
    const buena = await real('1/6');
    const casos: Array<{ nombre: string; args: string[]; env?: Record<string, string>; rc: number; etapa: string }> = [
      { nombre: 'no mode', args: ['--tarjetas', '24', '--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'zero cards', args: ['--modo', '1/6', '--tarjetas', '0', '--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'unknown mode', args: ['--modo', '1/2', '--tarjetas', '24', '--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'unknown flag', args: ['--modo', '1/6', '--tarjetas', '24', '--bogus', '--json'], rc: 2, etapa: 'argumentos' },
      { nombre: 'no browser', args: ['--modo', '1/6', '--tarjetas', '24', '--json'], env: { PLAYWRIGHT_BROWSERS_PATH: '/nonexistent-srf-u19' }, rc: 1, etapa: 'navegador' },
    ];
    for (const c of casos) {
      const m = await medir(c.args, c.env ?? {});
      expect(m.rc, c.nombre).toBe(c.rc);
      expect(m.informe.rc, c.nombre).toBe(c.rc);
      expect(m.informe.ok, c.nombre).toBe(false);
      expect(m.informe.etapa, c.nombre).toBe(c.etapa);
      expect(typeof m.informe.error, c.nombre).toBe('string');
      expect(m.stderr, c.nombre).toMatch(/^medir-scroll: /);
      expect(m.stdout.trim().split('\n'), c.nombre).toHaveLength(1);
      expect(forma(m.informe), c.nombre).toEqual(forma(buena.informe));
      for (const campo of ['p95_frame_ms', 'desmontajes', 'tarjetas_por_vista', 'tarjetas']) expect(m.informe[campo], `${c.nombre}: ${campo}`).toBeNull();
      // a consumer that keeps the exit status cannot mistake a failure for a pass
      expect(jq('.desmontajes==0', m.stdout), c.nombre).toBe(1);
    }
  });

  /**
   * Starts a run, kills it with SIGTERM at the given moment and audits what it leaves behind. Before the browser exists
   * only the instrument's own handler can clean up; once it runs, Playwright's handler closes the browser too.
   */
  async function matarUnaEjecucion(momento: 'al crearse su raiz' | 'con el navegador lanzado'): Promise<{ rc: number | null; raiz: string }> {
    const antes = new Set(readdirSync(tmpdir()).filter((n) => n.startsWith('srf-medir-scroll-')));
    const hijo = spawn(process.execPath, [INSTRUMENTO, '--tarjetas', '24', '--modo', '1/6', '--json'], { cwd: RAIZ_REPO, stdio: ['ignore', 'pipe', 'pipe'] });
    const salida = new Promise<number | null>((ok) => hijo.on('exit', (codigo) => ok(codigo)));
    const espera = (ms: number) => new Promise<void>((ok) => setTimeout(ok, ms));
    let raiz = '';
    const limite = Date.now() + 90_000;
    while (Date.now() < limite && raiz === '') {
      await espera(momento === 'al crearse su raiz' ? 5 : 100);
      const nueva = readdirSync(tmpdir()).find((n) => n.startsWith('srf-medir-scroll-') && !antes.has(n));
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
    const restos = readdirSync(tmpdir()).filter((n) => n.startsWith('srf-medir-scroll-'));
    expect(restos).toEqual([]);
  });
});
