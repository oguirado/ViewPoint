// Tests of the master shell (OPENDESIGN stage 7): App.svelte as source text (runes only, permanent mounting, the
// single WebGL2 bind, geometry from lib/geometria) and the pure rules it relies on (lib/shell.ts, lib/capsulas.ts).

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import {
  ESTADO_HUERFANA,
  ESTADO_INICIANDO,
  ESTADO_LISTO,
  PUERTO_CDP_BASE,
  PUERTO_XPRA_BASE,
  capsulaWorkspaceDeSuperficie,
  idDeSuperficie,
  mismaTabla,
  nivelDeClase,
  ordenarPorSlot,
  primerSlotLibre,
  ranuraDeSuperficie,
  saludDeEstado,
} from '../src/lib/capsulas';
import { REFRESCO_MS, cicloDeRanura, esAtajoCajon, saltoDeRanura, vistaInicial } from '../src/lib/shell';
import { ALTURA_ASA, ALTURA_CAJON } from '../src/lib/terminal/termguard';
import type { InfoSuperficieSrfd } from '../src/lib/types/hypervisor';

const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');
const APP = leer('../src/App.svelte');
const COCKPIT = leer('../src/lib/components/CockpitMasterView.svelte');
const MATRIZ = leer('../src/lib/workspace/WorkspaceMatrix.svelte');

const tecla = (key: string, mods: Partial<Pick<KeyboardEvent, 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'>> = {}) => ({
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  key,
  ...mods,
});

describe('App.svelte: Svelte 5 runes exclusively (SVELTE5-RUNES-ONLY)', () => {
  it('imports no legacy store and declares no reactive statement', () => {
    expect(APP).not.toMatch(/from\s+['"]svelte\/store['"]/);
    expect(APP).not.toMatch(/\bwritable\s*\(|\breadable\s*\(|(?<!\$)\bderived\s*\(/);
    expect(APP).not.toMatch(/^\s*\$:\s/m);
    expect(APP).toMatch(/\$state\(/);
    expect(APP).toMatch(/\$derived\(/);
    expect(APP).toMatch(/\$state\.raw[<(]/);
  });
});

describe('App.svelte: permanent mounting (PERMANENT-DOM-MOUNT, INV-PANTALLA-01)', () => {
  it('mounts CockpitMasterView and CajonAgentes once, neither under {#if}, and secondary views are absent', () => {
    for (const vista of ['CockpitMasterView', 'CajonAgentes']) {
      expect(APP.match(new RegExp(`<${vista}\\b`, 'g'))?.length).toBe(1);
      expect(APP).not.toMatch(new RegExp(`\\{#if[^}]*\\}[\\s\\S]{0,400}?<${vista}\\b`));
      expect(APP).not.toMatch(new RegExp(`\\{:else[^}]*\\}[\\s\\S]{0,400}?<${vista}\\b`));
    }
    expect(APP).not.toMatch(/<TelemetryMasterView\b/);
    expect(APP).toMatch(/class="workspace-origin"[\s\S]*?<WorkspaceMatrix\b/);
  });
  it('CockpitMasterView is permanently mounted without {#if} under the master header', () => {
    expect(APP).toMatch(/style:top="\{BARRA_H\}px"/);
    expect(APP).not.toMatch(/\{#key\b/);
  });
  it('hosts VIEW-01 under the master header with its own bar hidden and its built-in drawer collapsed', () => {
    expect(APP).toMatch(/<CockpitMasterView[^>]*barraPropia=\{false\}/);
    expect(APP).toMatch(/<CockpitMasterView[^>]*cajonInicial=\{false\}/);
    expect(COCKPIT).toMatch(/style:display=\{barraPropia \? null : 'none'\}/);
    expect(COCKPIT).toMatch(/style:top="\{barraPropia \? BARRA_H : 0\}px"/);
  });
});

describe('App.svelte: one WebGL2 context through the broker (SINGLE-WEBGL2-BROKER)', () => {
  it('binds exactly one canvas through MasterWebGL2Broker.getInstance().bindContext', () => {
    expect(APP.match(/bindContext\(/g)?.length).toBe(1);
    expect(APP).toMatch(/MasterWebGL2Broker\.getInstance\(\)\.bindContext\(lienzo\)/);
    expect(APP).not.toMatch(/getContext\(\s*['"]webgl/);
    expect(APP.match(/<canvas\b/g)?.length).toBe(1);
  });
});

describe('App.svelte: geometry from lib/geometria only (SINGLE-SOURCE-GEOMETRY)', () => {
  it('imports the shell figures and writes none of them as literals', () => {
    expect(APP).toMatch(/import\s*\{[^}]*\bBARRA_H\b[^}]*\}\s*from\s*'\.\/lib\/geometria'/);
    for (const nombre of ['LIENZO_W', 'LIENZO_H', 'DOM_W', 'GUTTER_IZQ']) {
      expect(APP).toMatch(new RegExp(`import\\s*\\{[^}]*\\b${nombre}\\b[^}]*\\}\\s*from\\s*'\\./lib/geometria'`));
    }
    const plantilla = APP.slice(APP.indexOf('</script>'), APP.indexOf('<style>'));
    expect(plantilla).not.toMatch(/\b(1271|1264|1024|2080)\b/);
    expect(APP).toMatch(/import\s*\{[^}]*\bALTURA_CAJON\b[^}]*\}\s*from\s*'\.\/lib\/terminal\/termguard'/);
    expect(ALTURA_CAJON).toBe(240);
    expect(ALTURA_ASA).toBe(12);
  });
});

describe('App.svelte: srfd heartbeat and live capsule pipeline (BACKEND-SRFD-PIPELINE)', () => {
  it('polls estado and listar together on the 5000 ms period', () => {
    expect(REFRESCO_MS).toBe(5000);
    expect(APP).toMatch(/Promise\.allSettled\(\[cliente\.estado\(\), cliente\.listar\(\)\]\)/);
    expect(APP).toMatch(/setInterval\(\(\) => void sondear\(\), REFRESCO_MS\)/);
  });
  it('hands the live table to VIEW-01 through its live props', () => {
    expect(APP).toMatch(/<CockpitMasterView[^>]*capsulas=\{ranuras\}/);
    expect(COCKPIT).toMatch(/capsulas\?: readonly CapsuleSlot\[\] \| null/);
  });
  it('replaces the fixture in place (VIEW-01)', () => {
    expect(COCKPIT).toMatch(/slots\.splice\(0, slots\.length, \.\.\./);
  });
});

describe('App.svelte: drawer docking and hotkeys (DRAWER-DOCKING)', () => {
  it('docks CajonAgentes bound to the shell\'s open state and binds the capture-phase key handler', () => {
    expect(APP).toMatch(/<CajonAgentes[^>]*bind:abierto=\{cajonAbierto\}/);
    expect(APP).toMatch(/addEventListener\('keydown', alTeclado, \{ capture: true \}\)/);
    expect(APP).toMatch(/removeEventListener\('keydown', alTeclado, \{ capture: true \}\)/);
  });
  it('Alt+1..9 jumps directly to container slot index 0..8; other keys return null', () => {
    expect(saltoDeRanura(tecla('1', { altKey: true }))).toBe(0);
    expect(saltoDeRanura(tecla('2', { altKey: true }))).toBe(1);
    expect(saltoDeRanura(tecla('9', { altKey: true }))).toBe(8);
    expect(saltoDeRanura(tecla('1'))).toBeNull();
    expect(saltoDeRanura(tecla('1', { altKey: true, ctrlKey: true }))).toBeNull();
    expect(saltoDeRanura(tecla('1', { altKey: true, shiftKey: true }))).toBeNull();
    expect(saltoDeRanura(tecla('x', { altKey: true }))).toBeNull();
  });
  it('Ctrl+PageUp/Down and Ctrl+Left/Right cycle container slots; other keys return null', () => {
    expect(cicloDeRanura(tecla('PageDown', { ctrlKey: true }))).toBe(1);
    expect(cicloDeRanura(tecla('PageUp', { ctrlKey: true }))).toBe(-1);
    expect(cicloDeRanura(tecla('ArrowRight', { ctrlKey: true }))).toBe(1);
    expect(cicloDeRanura(tecla('ArrowLeft', { ctrlKey: true }))).toBe(-1);
    expect(cicloDeRanura(tecla('PageDown'))).toBeNull();
    expect(cicloDeRanura(tecla('PageDown', { ctrlKey: true, altKey: true }))).toBeNull();
    expect(cicloDeRanura(tecla('ArrowRight', { ctrlKey: true, shiftKey: true }))).toBeNull();
  });
  it('Ctrl+` toggles the drawer and nothing else does', () => {
    expect(esAtajoCajon(tecla('`', { ctrlKey: true }))).toBe(true);
    expect(esAtajoCajon(tecla('`'))).toBe(false);
    expect(esAtajoCajon(tecla('`', { ctrlKey: true, altKey: true }))).toBe(false);
    expect(esAtajoCajon(tecla('c', { ctrlKey: true }))).toBe(false);
  });
  it('resolves the initial view to cockpit in single-view architecture', () => {
    expect(vistaInicial('')).toBe('cockpit');
    expect(vistaInicial('?view=01')).toBe('cockpit');
    expect(vistaInicial('?view=02')).toBe('cockpit');
    expect(vistaInicial('?view=04')).toBe('cockpit');
    expect(vistaInicial('?view=matrix')).toBe('cockpit');
    expect(vistaInicial('?view=99')).toBe('cockpit');
  });
});

describe('lib/capsulas.ts: the srfd table as the views read it', () => {
  const listo: InfoSuperficieSrfd = { slot: 2, clase: 'browser', contenedor: 'srf-b2', estado: ESTADO_LISTO, reserva: 3 * 1024 * 1024 * 1024, cpus: 2 };
  const kvm: InfoSuperficieSrfd = { slot: 0, clase: 'microvm', id: 'vm-0', estado: ESTADO_INICIANDO, puerto_xpra: 11000, puerto_cdp: 9999 };
  const huerfana: InfoSuperficieSrfd = { slot: 5, clase: 'desktop', estado: ESTADO_HUERFANA };

  it('names, tiers and health follow srfd, and an unknown state is never healthy', () => {
    expect(idDeSuperficie(listo)).toBe('srf-ng-2');
    expect(idDeSuperficie(kvm)).toBe('vm-0');
    expect(nivelDeClase('browser')).toBe('DOCKER L2');
    expect(nivelDeClase('desktop')).toBe('DOCKER L2');
    expect(nivelDeClase('microvm')).toBe('KVM HARDWARE');
    expect(nivelDeClase('pty')).toBe('PTY JAULA');
    expect(saludDeEstado(ESTADO_LISTO)).toBe('HEALTHY');
    expect(saludDeEstado(ESTADO_INICIANDO)).toBe('WARN');
    expect(saludDeEstado(ESTADO_HUERFANA)).toBe('CRIT');
    expect(saludDeEstado('whatever')).toBe('OFFLINE');
  });
  it('maps a surface to a cockpit slot: ports from srfd or the port contract, reserva in MiB, no invented load', () => {
    const r = ranuraDeSuperficie(listo);
    expect(r).toMatchObject({ id: 'srf-ng-2', slot: 2, tier: 'DOCKER L2', mode: 'OPERATOR-CONTROL', health: 'HEALTHY', memoryMb: 3072, cpuPercent: 0, queueDepth: 0, latencyMs: 0 });
    expect(r.xpraPort).toBe(PUERTO_XPRA_BASE + 2);
    expect(r.cdpPort).toBe(PUERTO_CDP_BASE + 2);
    expect(r.name).toContain('srf-b2');
    const k = ranuraDeSuperficie(kvm);
    expect(k).toMatchObject({ id: 'vm-0', tier: 'KVM HARDWARE', mode: 'READ-ONLY', health: 'WARN', xpraPort: 11000, cdpPort: 9999, memoryMb: 0 });
  });
  it('maps a surface to a workspace card with the matrix vocabulary and the screen left to the matrix', () => {
    expect(capsulaWorkspaceDeSuperficie(listo)).toMatchObject({ id: 'srf-ng-2', pantalla: '', isolation_tier: 'DOCKER_L2', health_status: 'HEALTHY', telemetry: { cpu_pct: 0, memory_mb: 3072 }, ports: { cdp: null, xpra: PUERTO_XPRA_BASE + 2 } });
    expect(capsulaWorkspaceDeSuperficie(kvm, 'p2')).toMatchObject({ pantalla: 'p2', isolation_tier: 'KVM_HARDWARE', health_status: 'WARNING', ports: { cdp: 9999, xpra: 11000 } });
    expect(capsulaWorkspaceDeSuperficie(huerfana)).toMatchObject({ isolation_tier: 'DOCKER_L2', health_status: 'CRITICAL' });
  });
  it('orders by slot, finds the first free slot and tells identical tables apart from changed ones', () => {
    expect(ordenarPorSlot([huerfana, listo, kvm]).map((s) => s.slot)).toEqual([0, 2, 5]);
    expect(primerSlotLibre([kvm, listo])).toBe(1);
    expect(primerSlotLibre([])).toBe(0);
    expect(primerSlotLibre([{ slot: 0, clase: 'browser', estado: ESTADO_LISTO }], 1)).toBe(-1);
    expect(mismaTabla([listo, kvm], [kvm, listo])).toBe(true);
    expect(mismaTabla([listo], [{ ...listo, estado: ESTADO_HUERFANA }])).toBe(false);
    expect(mismaTabla([listo], [])).toBe(false);
  });
});
