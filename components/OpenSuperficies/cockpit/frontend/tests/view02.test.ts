// Tests of VIEW-02: Telemetry Strip & Hardware Isolation Gauges (TelemetryMasterView.svelte).
// Static contracts read from the source (runes exclusivity, geometry imports, hold timing, the nine states, the
// strip integration, no WebGL) and the pure helpers the module script exports.

import { readFileSync } from 'node:fs';
import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

import {
  ABORT_HOLD_MS,
  CLAVES_GATE,
  RECOVERY_MAX,
  agregadoCpu,
  contarTripped,
  progresoDeHold,
  segmentosRam,
} from '../src/lib/components/TelemetryMasterView.svelte';

const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');

const VISTA = leer('../src/lib/components/TelemetryMasterView.svelte');
const TIPOS = leer('../src/lib/types/hypervisor.ts');

describe('VIEW-02: Svelte 5 runes exclusivity (INV-01)', () => {
  it('uses runes only: no svelte/store, no legacy stores, no let: directives', () => {
    expect(VISTA).not.toMatch(/from\s+['"]svelte\/store['"]/);
    expect(VISTA).not.toMatch(/\bwritable\s*\(/);
    expect(VISTA).not.toMatch(/(?<!\$)\bderived\s*\(/);
    expect(VISTA).not.toMatch(/\breadable\s*\(/);
    expect(VISTA).not.toMatch(/\blet:[a-zA-Z0-9_]+/);
    expect(VISTA).toMatch(/\$state\b/);
    expect(VISTA).toMatch(/\$derived\b/);
    expect(VISTA).toMatch(/\$props\b/);
    expect(VISTA).toMatch(/\$effect\b/);
    expect(VISTA).toMatch(/untrack\(/);
  });

  it('parses with the Svelte 5 compiler: module script, instance script and fragment', () => {
    const ast = parse(VISTA, { modern: true });
    expect(ast.module).toBeDefined();
    expect(ast.instance).toBeDefined();
    expect(ast.fragment).toBeDefined();
  });
});

describe('VIEW-02: geometry and single-source imports (INV-02, H-6)', () => {
  it('imports LIENZO_W, LIENZO_H and BARRA_H from ../geometria and binds them, with height: 40px for the bar', () => {
    expect(VISTA).toMatch(/import\s*\{[^}]*\bLIENZO_W\b[^}]*\}\s*from\s*'\.\.\/geometria'/);
    expect(VISTA).toMatch(/import\s*\{[^}]*\bLIENZO_H\b[^}]*\}\s*from\s*'\.\.\/geometria'/);
    expect(VISTA).toMatch(/import\s*\{[^}]*\bBARRA_H\b[^}]*\}\s*from\s*'\.\.\/geometria'/);
    expect(VISTA).toMatch(/style:width="\{LIENZO_W\}px"/);
    expect(VISTA).toMatch(/style:height="\{LIENZO_H\}px"/);
    expect(VISTA).toMatch(/height:\s*40px/);
  });

  it('repeats no geometry figure (the census of tests/geometria.test.ts covers the whole src tree)', () => {
    expect(VISTA.match(/(?<![0-9.])(1271|1264|1024|2080|2128|1064|1280|2000)(?![0-9])/g)).toBeNull();
  });
});

describe('VIEW-02: single WebGL2 broker awareness (INV-04)', () => {
  it('never asks any canvas for a WebGL context; the sparkline is 2D', () => {
    expect(VISTA).not.toMatch(/getContext\(\s*['"]webgl/);
    expect(VISTA).toMatch(/getContext\('2d'\)/);
    expect(VISTA).toMatch(/standalone\s*=\s*false/);
    expect(VISTA).toMatch(/onGateTrip/);
    expect(VISTA).toMatch(/onEmergencyHalt/);
  });
});

describe('VIEW-02: 1.5 s hold-to-confirm isolation trip (INV-05)', () => {
  it('declares the 1500 ms hold and the three handlers', () => {
    expect(ABORT_HOLD_MS).toBe(1500);
    expect(VISTA).toMatch(/1500/);
    expect(VISTA).toMatch(/startAbortHold/);
    expect(VISTA).toMatch(/cancelAbortHold/);
    expect(VISTA).toMatch(/triggerEmergencyAbort/);
    expect(VISTA).toMatch(/onpointerleave=\{cancelAbortHold\}/);
    expect(VISTA).toMatch(/onpointercancel=\{cancelAbortHold\}/);
  });

  it('progress is 0 before the hold, proportional during it and 1 at exactly 1500 ms', () => {
    expect(progresoDeHold(0)).toBe(0);
    expect(progresoDeHold(-5)).toBe(0);
    expect(progresoDeHold(750)).toBeCloseTo(0.5, 12);
    expect(progresoDeHold(1499)).toBeLessThan(1);
    expect(progresoDeHold(1500)).toBe(1);
    expect(progresoDeHold(9000)).toBe(1);
  });
});

describe('VIEW-02: 9-state TEA machine and bounded recovery (INV-06)', () => {
  it('names all nine states and the recovery functions', () => {
    for (const st of ['default', 'hover', 'active', 'focus', 'disabled', 'loading', 'empty', 'error', 'recovering']) {
      expect(VISTA).toMatch(new RegExp(`'${st}'`));
    }
    expect(RECOVERY_MAX).toBe(3);
    expect(VISTA).toMatch(/retryRecovery/);
    expect(VISTA).toMatch(/acknowledgeError/);
    expect(VISTA).toMatch(/resetState/);
    expect(VISTA).toMatch(/transitionState/);
    expect(VISTA).toMatch(/recoveryAttempt >= RECOVERY_MAX/);
  });
});

describe('VIEW-02: permanent mounting and strip integration (INV-07)', () => {
  it('mounts the six modules and the strip once, banners toggle by class, the strip component is TelemetryStrip.svelte', () => {
    for (const mod of ['01', '02', '03', '04', '05', '06']) expect(VISTA).toMatch(new RegExp(`data-mod="${mod}"`));
    expect(VISTA).not.toMatch(/\{#if\s+active\s*\}/);
    expect(VISTA).toMatch(/import TelemetryStrip from '\.\.\/cards\/TelemetryStrip\.svelte'/);
    expect(VISTA).toMatch(/<TelemetryStrip capsula=\{capsulaResumen\} geometria=\{geo\} visible=\{true\} \/>/);
    expect(VISTA).toMatch(/class:visible=\{viewState === 'error'\}/);
  });

  it('declares the VIEW-02 contracts in hypervisor.ts', () => {
    for (const t of ['CoreLoad', 'PhysicalMemoryAllocation', 'GpuBlackwellTelemetry', 'GateKey', 'MicroVmGate', 'PbxLease', 'ForensicRecordState']) {
      expect(TIPOS).toMatch(new RegExp(`export (interface|type) ${t}\\b`));
    }
  });
});

describe('VIEW-02: pure helpers of the module script', () => {
  it('aggregates cores and counts tripped gates without dividing by zero', () => {
    expect(agregadoCpu([])).toBe(0);
    expect(agregadoCpu([{ coreId: 0, cluster: 0, percent: 10, freqGhz: 3, tempC: 40 }, { coreId: 1, cluster: 0, percent: 30, freqGhz: 3, tempC: 40 }])).toBe(20);
    expect(contarTripped([{ key: 'A', name: 'x', status: 'nominal' }, { key: 'B', name: 'y', status: 'tripped' }])).toBe(1);
    expect(CLAVES_GATE).toEqual(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I']);
  });

  it('splits the RAM ceiling into three segments that add up, and yields zeros for a zero ceiling', () => {
    const s = segmentosRam({ soberanaMib: 32768, reservadoMib: 49152, disponibleMib: 42024, techoTotalMib: 123944 });
    expect(s.soberanaPct + s.reservadoPct + s.disponiblePct).toBeCloseTo(100, 6);
    expect(s.usadoPct).toBeCloseTo(100 - s.disponiblePct, 9);
    expect(segmentosRam({ soberanaMib: 1, reservadoMib: 1, disponibleMib: 1, techoTotalMib: 0 })).toEqual({ soberanaPct: 0, reservadoPct: 0, disponiblePct: 0, usadoPct: 0 });
  });
});
