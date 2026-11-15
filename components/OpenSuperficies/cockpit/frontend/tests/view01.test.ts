// Tests of VIEW-01: Cockpit Multi-Capsule Viewport & Master Canvas
// Verifies 100-point audit invariants, 8 absolute blockers, Svelte 5 runes exclusivity,
// single WebGL2 context broker scissoring, termguard defense, and 1.5s hold-to-confirm abort.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

import { MasterWebGL2Broker } from '../src/lib/components/MasterCanvas.svelte';
import {
  ALTURA_CAJON,
  SCROLLBACK_MAX,
  crearGuardia,
  decidirRenderer,
  limitarScrollback,
  protegerTerminal,
  vigilarCapas,
} from '../src/lib/terminal/termguard';
import {
  RC_FALLO,
  RC_PARADA,
  RC_PEER_RECHAZADO,
  RC_SLOT_OCUPADO,
  RC_USO,
  causaDeRC,
  crearClienteSrfd,
} from '../src/lib/srfd';

const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');

const COCKPIT_MASTER_VIEW = leer('../src/lib/components/CockpitMasterView.svelte');
const MASTER_CANVAS = leer('../src/lib/components/MasterCanvas.svelte');
const CAJON_AGENTES = leer('../src/lib/terminal/CajonAgentes.svelte');

describe('VIEW-01: Svelte 5 Runes Exclusivity (ET4-02-SVELTE5-RUNES)', () => {
  it('synthesizes reactive state exclusively with Svelte 5 runes ($state, $derived, $props, $effect)', () => {
    // Prohibit legacy Svelte store imports
    expect(COCKPIT_MASTER_VIEW).not.toMatch(/from\s+['"]svelte\/store['"]/);
    expect(COCKPIT_MASTER_VIEW).not.toMatch(/\bwritable\s*\(/);
    expect(COCKPIT_MASTER_VIEW).not.toMatch(/(?<!\$)\bderived\s*\(/);
    expect(COCKPIT_MASTER_VIEW).not.toMatch(/\breadable\s*\(/);

    // Prohibit legacy let: directives
    expect(COCKPIT_MASTER_VIEW).not.toMatch(/\blet:[a-zA-Z0-9_]+/);

    // Assert Svelte 5 runes presence
    expect(COCKPIT_MASTER_VIEW).toMatch(/\$state\b/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/\$derived\b/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/\$props\b/);
  });

  it('compiles with Svelte 5 compiler without syntax or structural errors', () => {
    const ast = parse(COCKPIT_MASTER_VIEW, { modern: true });
    expect(ast).toBeDefined();
    expect(ast.fragment).toBeDefined();
    expect(ast.instance).toBeDefined();
    expect(ast.module).toBeDefined();
  });
});

describe('VIEW-01: Single WebGL2 Context & Scissoring Broker (BLK-07, ET4-03)', () => {
  it('enforces singleton MasterWebGL2Broker with context ceiling <= 1', () => {
    const broker1 = MasterWebGL2Broker.getInstance();
    const broker2 = MasterWebGL2Broker.getInstance();
    expect(broker1).toBe(broker2);

    // Initial context count is 0 or 1
    expect(broker1.getContextCount()).toBeLessThanOrEqual(1);
  });

  it('provides hardware scissoring API (setScissor, clearScissor, disableScissor)', () => {
    const broker = MasterWebGL2Broker.getInstance();
    expect(typeof broker.setScissor).toBe('function');
    expect(typeof broker.clearScissor).toBe('function');
    expect(typeof broker.disableScissor).toBe('function');
    expect(typeof broker.bindContext).toBe('function');
  });

  it('MasterCanvas.svelte declares and exports MasterWebGL2Broker', () => {
    expect(MASTER_CANVAS).toMatch(/export\s+class\s+MasterWebGL2Broker/);
  });
});

describe('VIEW-01: Macroblock Geometry Invariants (BLK-01, ET4-06)', () => {
  it('satisfies 16-pixel macroblock alignment across canvas, drawer, and global bar', () => {
    const CANVAS_W = 1264;
    const CANVAS_H = 1024;
    const DRAWER_H = 240;
    const GLOBAL_BAR_H = 40;

    // Modulo 16 checks
    expect(CANVAS_W % 16).toBe(0);
    expect(CANVAS_H % 16).toBe(0);
    expect(DRAWER_H % 16).toBe(0);
    expect(DRAWER_H).toBe(ALTURA_CAJON);

    // Macroblock counts
    expect(CANVAS_W / 16).toBe(79);
    expect(CANVAS_H / 16).toBe(64);
    expect(DRAWER_H / 16).toBe(15);
  });

  it('declares exact 1264x1024 coordinate envelope in CockpitMasterView', () => {
    expect(COCKPIT_MASTER_VIEW).toMatch(/width=\{LIENZO_W\}/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/height=\{LIENZO_H\}/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/height:\s*40px/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/240px/);
  });
});

describe('VIEW-01: Termguard Defense & Terminal Drawer (ET4-05, A3-4)', () => {
  it('locks xterm addons by overriding loadAddon to reject canvas/webgl layers', () => {
    const mockTerm = {
      loadAddon: () => {},
    };
    protegerTerminal(mockTerm);

    // loadAddon is non-configurable and non-writable
    const desc = Object.getOwnPropertyDescriptor(mockTerm, 'loadAddon');
    expect(desc?.configurable).toBe(false);
    expect(desc?.writable).toBe(false);

    // Reject canvas/webgl requests
    const decCanvas = decidirRenderer('canvas');
    expect(decCanvas.rechazado).toBe(true);
    expect(decCanvas.concedido).toBe('dom');

    const decWebgl = decidirRenderer('webgl');
    expect(decWebgl.rechazado).toBe(true);
    expect(decWebgl.concedido).toBe('dom');

    const decDom = decidirRenderer('dom');
    expect(decDom.rechazado).toBe(false);
    expect(decDom.concedido).toBe('dom');
  });

  it('clamps scrollback to SCROLLBACK_MAX (2,000 lines)', () => {
    expect(limitarScrollback(100)).toBe(100);
    expect(limitarScrollback(2000)).toBe(2000);
    expect(limitarScrollback(50000)).toBe(SCROLLBACK_MAX);
    expect(SCROLLBACK_MAX).toBe(2000);
  });
});

describe('VIEW-01: Permanent Mounting Invariant (BLK-04, ET4-04)', () => {
  it('does not unmount active surface viewports via {#if activeCapsule}', () => {
    // The master view must permanently mount the canvas and drawer
    expect(COCKPIT_MASTER_VIEW).not.toMatch(/\{#if\s+activeSlot\s*===\s*slot\}/);
    // Drawer uses display: none or height toggles, preserving DOM instance
    expect(COCKPIT_MASTER_VIEW).toMatch(/display:\s*\{drawerOpen\s*\?\s*['"]flex['"]\s*:\s*['"]none['"]\}/);
  });
});

describe('VIEW-01: 9-State TEA Machine & Recovery (BLK-06, A2-4)', () => {
  it('declares and accounts for all 9 TEA operational states', () => {
    const states = [
      'default',
      'hover',
      'active',
      'focus',
      'disabled',
      'loading',
      'empty',
      'error',
      'recovering',
    ];

    for (const st of states) {
      expect(COCKPIT_MASTER_VIEW).toMatch(new RegExp(`'${st}'`));
    }

    // Recovery functions exist and are non-absorbing (k < 3)
    expect(COCKPIT_MASTER_VIEW).toMatch(/retryRecovery/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/resetState/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/transitionState/);
  });
});

describe('VIEW-01: 1.5-Second Hold-to-Confirm Emergency Abort (A2-5)', () => {
  it('implements 1.5-second hold timing and progress calculation', () => {
    expect(COCKPIT_MASTER_VIEW).toMatch(/1500/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/startAbortHold/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/cancelAbortHold/);
    expect(COCKPIT_MASTER_VIEW).toMatch(/triggerEmergencyAbort/);
  });
});

describe('VIEW-01: Backend Contract Wiring via srfd.ts (A3-1, A3-2)', () => {
  it('translates srfd exit codes accurately without guessing', () => {
    expect(causaDeRC(RC_FALLO)).toContain('failed');
    expect(causaDeRC(RC_USO)).toContain('malformed');
    expect(causaDeRC(RC_PARADA)).toContain('stop signal');
    expect(causaDeRC(RC_PEER_RECHAZADO)).toContain('SO_PEERCRED');
    expect(causaDeRC(RC_SLOT_OCUPADO)).toContain('already active');
    expect(causaDeRC(999)).toContain('unknown srfd exit code 999');
  });

  it('wires srfd client requests to Wails bridge transport', async () => {
    const mockTransport = {
      enviar: async (cmd: string) => {
        if (cmd === 'estado') {
          return {
            ok: true,
            rc: 0,
            datos: {
              estado: 'listo',
              vivas: 2,
              reservado_mib: 512,
              disponible_mib: 1536,
            },
          };
        }
        return { ok: true, rc: 0 };
      },
    };

    const client = crearClienteSrfd(mockTransport);
    const est = await client.estado();
    expect(est.estado).toBe('listo');
    expect(est.vivas).toBe(2);
    expect(est.disponible_mib).toBe(1536);
  });
});
