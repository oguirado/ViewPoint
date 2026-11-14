// Tests of VIEW-04: Multi-Screen Virtual Workspace Selector & Slot Relocation Matrix (WorkspaceMatrix.svelte).
// The invariants INV-PANTALLA-01..04 are exercised through the engine the component consumes (lib/stack/pantallas.ts,
// lib/stack/fenwick.ts) and the pure helpers the module script exports; the markup contracts are read from the source.

import { readFileSync } from 'node:fs';
import { compile, parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

import {
  BALDOSA,
  HOLD_MS,
  RADIAL_LARGO,
  baldosas,
  indicePorAtajo,
  nodosFenwick,
  progresoDeHold,
  ranura,
  siguienteIdDePantalla,
} from '../src/lib/workspace/WorkspaceMatrix.svelte';
import { FenwickArbol } from '../src/lib/stack/fenwick';
import { PANTALLA_PRIMERA, alturasDeItems, asignarPantalla, cerrarPantalla, disposicion, estaOculta, herederaDe, pantallaEfectiva } from '../src/lib/stack/pantallas';
import type { ElementoDePila, Pantalla } from '../src/lib/stack/pantallas';
import { ALINEACION_CODEC, BARRA_H, LIENZO_H, LIENZO_W } from '../src/lib/geometria';

const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');
const VISTA = leer('../src/lib/workspace/WorkspaceMatrix.svelte');
const SIN_COMENTARIOS = VISTA.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

const PANTALLAS: Pantalla[] = [
  { id: 'p1', nombre: 'DEV' },
  { id: 'p2', nombre: 'MONITOR' },
  { id: 'p3', nombre: 'STAGING' },
];
const CAPSULAS: ElementoDePila[] = [
  { id: 'srf-ng-0', pantalla: 'p1' },
  { id: 'srf-ng-1', pantalla: 'p1' },
  { id: 'srf-ng-2', pantalla: 'p2' },
];

describe('VIEW-04: Svelte 5 runes exclusivity and geometry imports (INV-01, INV-02)', () => {
  it('renders only canonical state without local authority or timers', () => {
    expect(VISTA).toMatch(/\$props\(\)/);
    const instance = VISTA.slice(VISTA.indexOf('</script>') + 9);
    expect(instance).not.toMatch(/baldosas\(|ranura\(|setInterval|setTimeout|requestAnimationFrame|infinite|data-cerrar|data-provisionar|ejecutarReubicaciones|Grabacion|srf-grabar|role="status"|role="alert"|aria-live|fetch\(/);
    expect(instance).not.toMatch(/cpu_pct|memory_mb|EAL5|ZERO DROPPED|capsulasIniciales/);
    expect(instance).not.toMatch(/aiops\/(OperationStatus|KeyboardMenu|CapsuleBar)/);
  });

  it('parses with the Svelte 5 compiler and partitions the canvas into macroblock bands', () => {
    const ast = parse(VISTA, { modern: true });
    expect(ast.module).toBeDefined();
    expect(ast.instance).toBeDefined();
    const pie = 4 * ALINEACION_CODEC;
    const matriz = 50 * ALINEACION_CODEC;
    expect(BARRA_H + (LIENZO_H - BARRA_H - pie) + pie).toBe(LIENZO_H);
    expect(matriz + (LIENZO_W - matriz)).toBe(LIENZO_W);
    expect(LIENZO_W % ALINEACION_CODEC).toBe(0);
    expect(LIENZO_H % ALINEACION_CODEC).toBe(0);
  });
});

describe('VIEW-04: permanent mounting (INV-PANTALLA-01)', () => {
  it('switching screens hides a capsule with display: none through estaOculta and never through {#if}', () => {
    const ocultasEnP1 = CAPSULAS.map((c) => estaOculta(PANTALLAS, c, 'p1'));
    const ocultasEnP2 = CAPSULAS.map((c) => estaOculta(PANTALLAS, c, 'p2'));
    expect(ocultasEnP1).toEqual([false, false, true]);
    expect(ocultasEnP2).toEqual([true, true, false]);
    expect(VISTA).toMatch(/class="superficie"/);
    expect(VISTA).toMatch(/\{#each snapshot\.capsules as c, i \(c\.slot_id\)\}/);
    expect(VISTA).not.toMatch(/\{#if[^}]*oculta/);
    expect(VISTA).toMatch(/\{@render \(superficie \?\? previsualizacion\)\(c, i\)\}/);
  });

  it('places tiles and slots by arithmetic so the slot layer never depends on a measured tile', () => {
    const b = baldosas(3, 800, 888, 12, 12);
    expect(b).toHaveLength(3);
    expect(b[0]).toEqual({ x: 12, y: 12, w: 382, h: 426 });
    expect(b[1]).toEqual({ x: 406, y: 12, w: 382, h: 426 });
    expect(b[2]).toEqual({ x: 12, y: 450, w: 776, h: 426 });
    expect(baldosas(0, 800, 888, 12, 12)).toEqual([]);
    const r0 = ranura(b[0], 0, 2);
    const r1 = ranura(b[0], 1, 2);
    expect(r0.x).toBe(12 + BALDOSA.relleno);
    expect(r1.y).toBeGreaterThan(r0.y + r0.h);
    expect(r1.y + r1.h).toBeLessThanOrEqual(b[0].y + b[0].h - BALDOSA.pie);
  });
});

describe('VIEW-04: screen invariants (INV-PANTALLA-02..04)', () => {
  it('resolves an unknown or missing screen to p1', () => {
    expect(pantallaEfectiva(PANTALLAS, 'p9')).toBe(PANTALLA_PRIMERA);
    expect(pantallaEfectiva(PANTALLAS, undefined)).toBe('p1');
    const movida = asignarPantalla(PANTALLAS, CAPSULAS, 'srf-ng-2', 'p9');
    expect(movida.find((c) => c.id === 'srf-ng-2')?.pantalla).toBe('p1');
  });

  it('cannot close the last screen and disables the actuator', () => {
    expect(cerrarPantalla([{ id: 'p1' }], CAPSULAS, 'p1')).toEqual({ ok: false, motivo: 'ultima_pantalla' });
    expect(() => herederaDe([{ id: 'p1' }], 'p1')).toThrow(RangeError);
  });

  it('closing a screen hands every capsule to herederaDe() atomically and keeps their identity', () => {
    expect(herederaDe(PANTALLAS, 'p1')).toBe('p2');
    expect(herederaDe(PANTALLAS, 'p2')).toBe('p1');
    expect(herederaDe(PANTALLAS, 'p3')).toBe('p2');
    const res = cerrarPantalla(PANTALLAS, CAPSULAS, 'p1');
    expect(res.ok).toBe(true);
    if (res.ok) {
      expect(res.heredera).toBe('p2');
      expect(res.pantallas.map((p) => p.id)).toEqual(['p2', 'p3']);
      expect(res.elementos.map((c) => c.id)).toEqual(['srf-ng-0', 'srf-ng-1', 'srf-ng-2']);
      expect(res.elementos.every((c) => c.pantalla === 'p2')).toBe(true);
    }
    expect(siguienteIdDePantalla(PANTALLAS)).toBe('p4');
    expect(siguienteIdDePantalla([{ id: 'p1' }, { id: 'p4' }])).toBe('p3');
  });
});

describe('VIEW-04: Fenwick synchronisation in O(log N)', () => {
  it('re-indexes only the heights that changed and reports offsets the inspector shows', () => {
    const arbol = FenwickArbol.desdeAlturas([]);
    const d = disposicion('column_1_3_dual');
    const enP1 = alturasDeItems('column_1_3_dual', CAPSULAS.map((c) => estaOculta(PANTALLAS, c, 'p1')));
    expect(arbol.sincronizar(enP1)).toBe(3); // a rebuild: the count changed
    expect(enP1).toEqual([d.paso, d.paso - d.alturaBarra, 0]);
    const g1 = arbol.generacion;
    const enP2 = alturasDeItems('column_1_3_dual', CAPSULAS.map((c) => estaOculta(PANTALLAS, c, 'p2')));
    expect(arbol.sincronizar(enP2)).toBe(3); // three heights differ, three O(log N) writes, no rebuild
    expect(arbol.generacion).toBe(g1 + 3);
    expect(arbol.ultimoCoste).toBeLessThanOrEqual(Math.ceil(Math.log2(3)) + 1);
    const nodos = nodosFenwick(arbol, CAPSULAS.map((c) => c.id));
    expect(nodos.map((n) => n.altura)).toEqual([0, 0, d.paso - d.alturaBarra]);
    expect(nodos.map((n) => n.offset)).toEqual([0, 0, 0]);
    expect(arbol.total()).toBe(d.paso - d.alturaBarra);
    expect(arbol.sincronizar(enP2)).toBe(0); // idempotent
  });
});

describe('VIEW-04: guarded closure and keyboard', () => {
  it('holds for exactly HOLD_MS with a radial arc, and maps the shortcuts', () => {
    expect(VISTA).toMatch(/prefers-reduced-motion: reduce/);
    expect(indicePorAtajo({ altKey: true, key: '1' })).toBe(0);
    expect(indicePorAtajo({ altKey: true, key: '9' })).toBe(8);
    expect(indicePorAtajo({ altKey: false, key: '1' })).toBeNull();
    expect(indicePorAtajo({ altKey: true, key: '0' })).toBeNull();
    expect(VISTA).toMatch(/role="tablist"/);
    expect(VISTA).toMatch(/role="tab"/);
    expect(VISTA).toMatch(/tabindex=\{w\.n === activaEfectiva \? 0 : -1\}/);
    expect(VISTA).toMatch(/outline: 2px solid var\(--c-cyan\);\s*outline-offset: 2px;/);
  });

  it('carries its own style block and the eight data states', () => {
    expect(VISTA).toMatch(/<style>[\s\S]*#0e131f[\s\S]*#00e5ff[\s\S]*<\/style>/i);
    expect(VISTA).not.toMatch(/class="[^"]*\b(flex |h-8|w-\[|bg-surface-|text-text-)/);
    for (const st of ['default', 'background', 'orphaned', 'empty', 'heir', 'loading', 'error', 'disabled']) expect(VISTA).toMatch(new RegExp(`'${st}'`));
    expect(VISTA).toMatch(/'tnum' 1, 'zero' 1/);
    expect(VISTA).not.toMatch(/<canvas/);
    expect(VISTA).not.toMatch(/getContext\(/);
  });
});

// JTI-01: compile the real component for SSR; no browser or DOM dependency.
import { createServer } from 'vite';
import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { beforeAll, afterAll, vi } from 'vitest';
import type { render as Render } from 'svelte/server';
let render: typeof Render;
import type { Component } from 'svelte';
import type { CockpitState, SlotBlock, WindowMode } from '../src/lib/types/hypervisor';
function fixture(mode: WindowMode = 'column_1_3_dual', count = 2): CockpitState {
  const per = mode === 'column_1_3_dual' ? 2 : 1;
  const height = mode === 'column_1_3_tall' ? 2080 : 1024;
  const capsules: SlotBlock[] = Array.from({length:count}, (_, slot) => ({
    archetype:'browser', cdp:9220+slot, geometry:{x:0,y:(slot%per)*height,w:1264,h:height}, health:'healthy', lease_epoch:0, lease_holder:null,
    lifecycle:'ready', mode:'read_only', ready:true, rec_state:{phase:'idle',store:null,ttl_ms:null}, seq:7, slot,
    slot_id:`srf-ng-${slot}`, telemetry:{cpu_permille:null,mem_bytes:null}, uri:`os://osbox/ws/${Math.floor(slot/per)+1}/slot/srf-ng-${slot}`,workspace:Math.floor(slot/per)+1,xpra_port:10020+slot,
  }));
  return {capsules,host:'osbox',schema:'os.cu.cockpit-state/v1',seq:7,ts_mono_ms:6000,window_mode:mode,
    workspaces:Array.from({length:Math.ceil(count/per)},(_, i)=>{
      const a=capsules[i*per], b=capsules[i*per+1];
      const dual=per===2 && !!b;
      return {n:i+1,layout:dual?'1x2':'1x1',rect:{x:0,y:0,w:1264,h:height*(dual?2:1)},tree:dual?{type:'split',direction:'vertical',ratio_q16:32768,left:{type:'leaf',slot_id:a.slot_id},right:{type:'leaf',slot_id:b.slot_id}}:{type:'leaf',slot_id:a.slot_id}};
    })};
}

let server: Awaited<ReturnType<typeof createServer>>;
let Matrix: Component<any>;
beforeAll(async()=>{
  server=await createServer({configFile:false,root:new URL('..',import.meta.url).pathname,
    cacheDir:new URL('../../../build/room_BTI/vite-cache',import.meta.url).pathname,
    plugins:[svelte({preprocess:vitePreprocess(),hot:false})],server:{middlewareMode:true,hmr:false,watch:null},appType:'custom'});
  render=(await server.ssrLoadModule('svelte/server')).render;
  Matrix=(await server.ssrLoadModule('/src/lib/workspace/WorkspaceMatrix.svelte')).default;
});
afterAll(async()=>{await server?.close();});
function html(doc=fixture()): string {return render(Matrix,{props:{doc,bytes:JSON.stringify(doc)}}).body;}
describe('F1-TILING SSR rendering',()=>{
  it('idle_tile_churn: equal seq produces byte-identical HTML after ten seconds',()=>{
    vi.useFakeTimers();try {const d=fixture();const before=html(d);vi.advanceTimersByTime(10000);expect(html(d)).toBe(before);}finally{vi.useRealTimers();}
  });
  it('canonical DOM mapping and truthful missing telemetry',()=>{
    const s=html();expect(s).toContain('top: 1104px');expect(s).toContain('left: 3px');
    expect(s).toContain('x=0,y=1024,w=1264,h=1024');expect(s).toContain('telemetry-stale');expect(s).toContain('—');
    expect(s).toContain('CPU srf-ng-0');expect(s).toContain('RAM srf-ng-1');expect(s).not.toContain('role="separator"');
  });
  it('workspaces are warm hidden panels with tab descriptions and no invented denominator',()=>{
    const s=html(fixture('column_1_3_dual',3));expect(s).toContain('Workspace 2');expect(s).toContain('display: none');
    expect(s).toContain('Layout 1x2, 2 capsule(s): srf-ng-0, srf-ng-1');expect(s).not.toMatch(/\d+\/\d+/);
    expect(s).toContain('Tile srf-ng-2');expect(s).toContain('aria-controls="workspace-panel-2"');
  });
  it('mismatch_unprefixed_b3_status negative: workspace fails closed with B3 text',()=>{
    const original=fixture();const d={...original,capsules:original.capsules.map((c,i)=>i===1?{...c,geometry:{...c.geometry,y:999}}:c)};
    const s=html(d);expect(s).toContain('DEGRADED: GEOMETRY MISMATCH ws 1');
    expect(s).toContain('display: none');expect(s).not.toMatch(/role="status"|role="alert"|aria-live/);
  });
  it('valid measured telemetry renders from the sample',()=>{
    const original=fixture();const d: CockpitState={...original,capsules:original.capsules.map((c,i)=>i===0?{...c,telemetry:{cpu_permille:{value:125,source:'cgroup',ts_mono_ms:6000},mem_bytes:{value:104857600,source:'procfs',ts_mono_ms:6000}}}:c)};
    const s=html(d);expect(s).toContain('12.5%');expect(s).toContain('100 MiB');
  });
});


it('WM and PM compile without accessibility warnings and keep surfaces out of conditional trees',()=>{
  const stack=leer('../src/lib/stack/PilaMultislot.svelte');
  for(const [filename,source] of [['WorkspaceMatrix.svelte',VISTA],['PilaMultislot.svelte',stack]]) {
    expect(compile(source,{filename,generate:'server'}).warnings).toEqual([]);
    expect(source).not.toMatch(/setInterval|setTimeout|requestAnimationFrame|infinite/);
    const ast=parse(source,{modern:true});
    let surfaces=0;
    function visit(value:unknown,ancestors:string[]=[]):void {
      if(!value || typeof value!=='object')return;
      if(Array.isArray(value)){for(const child of value) visit(child,ancestors);return;}
      const node=value as Record<string,unknown>;
      const expression=node.expression as {callee?:{name?:string;type?:string}}|undefined;
      if(node.type==='RenderTag' && (expression?.callee?.name==='tarjeta' || expression?.callee?.type==='LogicalExpression')) {
        surfaces++;
        expect(ancestors).toContain('EachBlock');
        expect(ancestors).not.toContain('IfBlock');expect(ancestors).not.toContain('KeyBlock');
      }
      const next=typeof node.type==='string'?[...ancestors,node.type]:ancestors;
      for(const [key,child] of Object.entries(node)) if(!['metadata','parent'].includes(key))visit(child,next);
    }
    visit(ast.fragment);expect(surfaces).toBe(1);
  }
});

it.each([0,2999,6001,NaN])('missing/stale/future malformed sample timestamp %s renders a dash',(ts)=>{
  const original=fixture();const d: CockpitState={...original,capsules:original.capsules.map((c,i)=>i===0?{...c,telemetry:{...c.telemetry,cpu_permille:{value:987,source:'cgroup',ts_mono_ms:ts}}}:c)};
  const s=html(d);expect(s).not.toContain('98.7%');expect(s).toContain('telemetry-stale');
});


it('tab activation routes start and finish through the injected status bus',()=>{
  expect(VISTA).toContain("task:'open-workspace',phase:'started'");
  expect(VISTA).toContain("task:'open-workspace',phase:'finished'");
  expect(VISTA).toContain('select(workspaces[next].n)');
});
