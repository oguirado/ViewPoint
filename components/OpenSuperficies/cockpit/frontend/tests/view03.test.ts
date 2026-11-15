// Tests of VIEW-03: Collapsible Terminal Drawer & PTY Agent Console (CajonAgentes.svelte).
// Static contracts read from the source (runes exclusivity, geometry imports, permanent mounting, the guard's
// constants, the prompt line) and the module script's exports. The behaviour in a real browser is measured by
// tests/terminal.test.ts through scripts/ng/medir-terminal.mjs.

import { readFileSync } from 'node:fs';
import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

import { RECONEXIONES_MAX } from '../src/lib/terminal/CajonAgentes.svelte';
import { ALTURA_ASA, ALTURA_CABECERA, ALTURA_CAJON, PANELES_INTERACTIVOS_MAX, SCROLLBACK_MAX } from '../src/lib/terminal/termguard';
import { ALINEACION_CODEC } from '../src/lib/geometria';

const leer = (rutaRelativa: string): string => readFileSync(new URL(rutaRelativa, import.meta.url), 'utf8');

const CAJON = leer('../src/lib/terminal/CajonAgentes.svelte');
const SIN_COMENTARIOS = CAJON.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

describe('VIEW-03: Svelte 5 runes exclusivity (INV-01)', () => {
  it('uses runes only: $props with $bindable, $derived, $state, $effect with untrack; no legacy stores or let: directives', () => {
    expect(CAJON).not.toMatch(/from\s+['"]svelte\/store['"]/);
    expect(CAJON).not.toMatch(/\bwritable\s*\(/);
    expect(CAJON).not.toMatch(/(?<!\$)\bderived\s*\(/);
    expect(CAJON).not.toMatch(/\blet:[a-zA-Z0-9_]+/);
    expect(CAJON).toMatch(/\$props\(\)/);
    expect(CAJON).toMatch(/abierto = \$bindable\(true\)/);
    expect(CAJON).toMatch(/activo = \$bindable\(''\)/);
    expect(CAJON).toMatch(/\$derived\b/);
    expect(CAJON).toMatch(/\$state\b/);
    expect(CAJON).toMatch(/untrack\(/);
  });

  it('parses with the Svelte 5 compiler: module script, instance script and fragment', () => {
    const ast = parse(CAJON, { modern: true });
    expect(ast.module).toBeDefined();
    expect(ast.instance).toBeDefined();
    expect(ast.fragment).toBeDefined();
  });
});

describe('VIEW-03: macroblock geometry from the single sources (INV-02, H-6)', () => {
  it('imports the drawer heights from ./termguard and the canvas width from ../geometria, and binds them', () => {
    expect(CAJON).toMatch(/import\s*\{[^}]*\bALTURA_ASA\b[^}]*\}\s*from\s*'\.\/termguard'/);
    expect(CAJON).toMatch(/import\s*\{[^}]*\bALTURA_CABECERA\b[^}]*\}\s*from\s*'\.\/termguard'/);
    expect(CAJON).toMatch(/import\s*\{[^}]*\bALTURA_CAJON\b[^}]*\}\s*from\s*'\.\/termguard'/);
    expect(CAJON).toMatch(/import\s*\{[^}]*\bLIENZO_W\b[^}]*\}\s*from\s*'\.\.\/geometria'/);
    expect(CAJON).toMatch(/style:height="\{abierto \? ALTURA_CAJON : ALTURA_ASA\}px"/);
    expect(CAJON).toMatch(/style:max-width="\{LIENZO_W\}px"/);
    expect(CAJON).toMatch(/style:height="\{ALTURA_CABECERA\}px"/);
    expect(CAJON).toMatch(/const ALTURA_PROMPT = 2 \* ALINEACION_CODEC;/);
  });

  it('stacks the three bands into ALTURA_CAJON: header, valley and a two-macroblock prompt line', () => {
    const prompt = 2 * ALINEACION_CODEC;
    expect(ALTURA_CAJON % ALINEACION_CODEC).toBe(0);
    expect(ALTURA_CAJON / ALINEACION_CODEC).toBe(15);
    expect(ALTURA_CABECERA + (ALTURA_CAJON - ALTURA_CABECERA - prompt) + prompt).toBe(ALTURA_CAJON);
    expect(ALTURA_ASA).toBe(12);
    expect(SIN_COMENTARIOS.match(/(?<![0-9.])(1271|1264|1024|2080|2128|1064|1280|2000)(?![0-9])/g)).toBeNull();
  });
});

describe('VIEW-03: permanent mounting and termguard (INV-04, INV-05)', () => {
  it('never unmounts a panel: keyed each over the agents, no {#if} on the drawer state, hidden slates use the hidden attribute', () => {
    expect(CAJON).toMatch(/\{#each agentes as a, i \(a\.id\)\}/);
    expect(CAJON).not.toMatch(/\{#if[^}]*abierto/);
    expect(CAJON).not.toMatch(/\{#if[^}]*activo/);
    // display: none exists only for chrome (the handle, the slates, a pip, a scrollbar), never for anything that holds a terminal
    for (const bloque of SIN_COMENTARIOS.matchAll(/([^{}]+)\{[^{}]*display:\s*none[^{}]*\}/g)) expect(bloque[1].trim()).not.toMatch(/panel|anfitrion|cuerpo|valle|xterm|cajon\b/);
    expect(CAJON).toMatch(/visible=\{abierto && a\.id === activoEfectivo\}/);
  });

  it('keeps the guard\'s numbers and imports no renderer addon or clipboard path', () => {
    expect(SCROLLBACK_MAX).toBe(2000);
    expect(PANELES_INTERACTIVOS_MAX).toBe(56);
    expect(CAJON).toMatch(/scrollback = SCROLLBACK_MAX/);
    expect(CAJON).toMatch(/modo=\{modoDeInstancia\(i\)\}/);
    expect(CAJON).not.toMatch(/from\s+['"]@xterm\/addon-(canvas|webgl)['"]/);
    expect(CAJON).not.toMatch(/onpaste|clipboardData|navigator\.clipboard|execCommand/i);
    expect(CAJON).toMatch(/oncontextmenu=\{\(e\) => e\.preventDefault\(\)\}/);
  });
});

describe('VIEW-03: chrome, states and recovery (INV-03, INV-06, INV-07)', () => {
  it('declares the nine TEA states, a bounded respawn and the empty-slate pills', () => {
    for (const st of ['default', 'hover', 'active', 'focus', 'disabled', 'loading', 'empty', 'error', 'recovering']) {
      expect(CAJON).toMatch(new RegExp(`'${st}'`));
    }
    expect(RECONEXIONES_MAX).toBe(3);
    expect(CAJON).toMatch(/< RECONEXIONES_MAX/);
    for (const pill of ['[srf conectar]', '[claude]', '[gemini]']) expect(CAJON).toContain(pill);
    expect(CAJON).toMatch(/data-asa/);
    expect(CAJON).toMatch(/data-plegar/);
    expect(CAJON).toMatch(/data-pestana=\{a\.id\}/);
    expect(CAJON).toMatch(/outline: 2px solid var\(--c-cyan\);\s*outline-offset: 2px;/);
  });

  it('carries its own style block with the Obsidian tokens and no utility-class markup', () => {
    expect(CAJON).toMatch(/<style>[\s\S]*#0e131f[\s\S]*#00e5ff[\s\S]*<\/style>/i);
    expect(CAJON).not.toMatch(/class="[^"]*\b(flex|h-8|w-\[|bg-surface-|text-text-)/);
    expect(CAJON).toMatch(/'tnum' 1, 'zero' 1/);
  });
});
