import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
it('exposes exact producer bytes in an AX reachable region', () => {
 const s = readFileSync(new URL('../src/lib/aiops/StateMirror.svelte', import.meta.url), 'utf8');
 expect(s).toContain('aria-label="cockpit-state"');
 expect(s).toContain('aria-live="off"');
 expect(s).toContain('<pre>{bytes}</pre>');
 expect(s).not.toMatch(/aria-hidden|display:\s*none|visibility:\s*hidden|JSON.stringify|{@html/);
});

import { spawnSync } from 'node:child_process';
import { canonical, readState, readSlot, stateBridge } from '../src/lib/aiops/state';

it('renders literal Go bytes as pre text without reconstructing the JSON', () => {
  const source = readFileSync(new URL('../src/lib/aiops/StateMirror.svelte', import.meta.url), 'utf8');
  const script = `
    import { compile } from 'svelte/compiler';
    import { render } from 'svelte/server';
    import { readFileSync } from 'node:fs';
    try {
      const input = JSON.parse(readFileSync(0, 'utf8'));
      const js = compile(input.source, { generate:'server', filename:'StateMirror.svelte' }).js.code
        .replace("'svelte/internal/server'", JSON.stringify(import.meta.resolve('svelte/internal/server')));
      const { default: component } = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
      console.log(JSON.stringify(input.samples.map(bytes => render(component, {props:{bytes}}).body)));
    } catch(e) { console.error(String(e)); process.exitCode = 5; }
  `;
  const samples = ['{"capsules":[],"schema":"os.cu.cockpit-state/v1"}', '{"text":"<>&","seq":7}', ''];
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    cwd: new URL('..', import.meta.url), input: JSON.stringify({source, samples}), encoding:'utf8', timeout:10000,
  });
  expect(child.status, child.stderr).toBe(0);
  const rendered = JSON.parse(child.stdout) as string[];
  for (let i = 0; i < samples.length; i++) {
    const inner = /<pre>([\s\S]*?)<\/pre>/.exec(rendered[i])?.[1];
    expect(inner).toBeDefined();
    const text = inner!.replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
    expect(text).toBe(samples[i]);
    expect(rendered[i]).toContain('role="region"');
  }
});

it('binds only the three read-only bridge operations', async () => {
  const bridge = stateBridge({ go: { main: { App: {
    EstadoCanonico: async () => '', BloqueSlot: async () => '{"slot_id":"srf-ng-0"}', StateURL: async () => 'http://127.0.0.1:10000/state',
  } } } });
  expect(bridge).not.toBeNull();
  expect(Object.keys(bridge!).sort()).toEqual(['BloqueSlot', 'EstadoCanonico', 'StateURL']);
  expect(await readSlot(bridge!, 'srf-ng-0')).toBe('{"slot_id":"srf-ng-0"}');
  await expect(readState(bridge!)).rejects.toThrow();
  expect(stateBridge({})).toBeNull();
  expect(() => canonical({ seq: 0 } as unknown as string)).toThrow();
});
