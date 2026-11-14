import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { parse } from 'svelte/compiler';
const app = readFileSync(new URL('../src/App.svelte', import.meta.url), 'utf8');
function violations(source: string): string[] {
  const errors: string[] = [];
  for (const component of ['StateMirror', 'OperationStatus', 'WorkspaceMatrix', 'CajonAgentes', 'CockpitMasterView']) {
    if ((source.match(new RegExp(`<${component}\\b`, 'g')) ?? []).length !== 1) errors.push(component);
  }
  const visit = (value: unknown, offset = false): void => {
    if (!value || typeof value !== 'object') return;
    const node = value as Record<string, unknown>;
    const attributes = node.attributes as {name?:string;value?:{data?:string}[]}[] | undefined;
    const className = attributes?.find(a=>a.name==='class')?.value?.map(v=>v.data??'').join('')??'';
    const nestedOffset = offset || /(?:^|\s)(?:marco|valle)(?:\s|$)/.test(className);
    if (node.name === 'WorkspaceMatrix' && nestedOffset) errors.push('origin');
    for (const child of Object.values(node)) {
      if (Array.isArray(child)) child.forEach(item=>visit(item,nestedOffset));
      else if (child && typeof child === 'object') visit(child,nestedOffset);
    }
  };
  try { visit(parse(source, {modern:true})); } catch { errors.push('syntax'); }
  if (!source.includes('class="workspace-origin"') || !source.includes('superficie={master?.superficie}')) errors.push('origin');
  if (!source.includes('label="Workload menu"') || !source.includes('label={`Layout ws ${ws}`}')) errors.push('keyboard');
  if (!source.includes('lado="left" anunciar={bus.emit}')) errors.push('hub');
  if (/selector-rec|setInterval\(ponerHora|119\.88|0\.14 ms|p99 < 0\.40|role="(?:status|alert)"/.test(source)) errors.push('legacy');
  return errors;
}
describe('WIRE shell admission', () => {
  it('matrix_full_origin_and_c4_c5_clean', () => expect(violations(app)).toEqual([]));
  it('rejects unmounted and duplicated mounts', () => {
    for (const component of ['StateMirror', 'OperationStatus', 'WorkspaceMatrix', 'CajonAgentes', 'CockpitMasterView']) {
      expect(violations(app.replace(`<${component}`, '<Removed'))).toContain(component);
      expect(violations(`${app}\n<${component} />`)).toContain(component);
    }
  });
  it('rejects a Matrix nested under a canvas-offset host', () => {
    for (const host of ['marco','valle']) {
      const nested = app.replace('<div class="workspace-origin">', `<div class="workspace-origin ${host}">`);
      expect(violations(nested)).toContain('origin');
    }
  });
  it('rejects fixture clocks, duplicate status and the old recorder', () => {
    for (const bad of ['setInterval(ponerHora', '119.88', 'role="status"', 'selector-rec'])
      expect(violations(`${app}\n${bad}`)).toContain('legacy');
  });
  it('removes legacy REC from agent wrappers and stubs', () => {
    for (const path of ['../src/lib/srfd.ts', '../wailsjs/go/main/App.js', '../wailsjs/go/main/App.d.ts']) {
      const source = readFileSync(new URL(path, import.meta.url), 'utf8');
      expect(source).not.toMatch(/\b(?:IniciarGrabacion|DetenerGrabacion|iniciarGrabacion|detenerGrabacion)\b/);
    }
  });
});

import { get } from 'svelte/store';
import { vi } from 'vitest';
import { createShellWire, cooperationBridge, recordingBridge } from '../src/lib/aiops/wire';
const canonicalBytes = JSON.stringify({
  capsules:[{archetype:'browser',cdp:9220,geometry:{h:1024,w:1264,x:0,y:0},health:'healthy',lease_epoch:0,
    lease_holder:null,lifecycle:'ready',mode:'operator_controlled',ready:true,rec_state:{phase:'idle',store:null,ttl_ms:null},
    seq:7,slot:0,slot_id:'srf-ng-0',telemetry:{cpu_permille:null,mem_bytes:null},uri:'os://osbox/ws/1/slot/srf-ng-0',workspace:1,xpra_port:10020}],
  host:'osbox',schema:'os.cu.cockpit-state/v1',seq:7,ts_mono_ms:6000,window_mode:'tile_1_6',
  workspaces:[{layout:'1x1',n:1,rect:{h:1024,w:1264,x:0,y:0},tree:{slot_id:'srf-ng-0',type:'leaf'}}],
});
describe('canonical shell wire', () => {
  it('preserves producer bytes, coalesces concurrent reads and ingests state', async () => {
    const read = vi.fn(async () => canonicalBytes);
    const wire = createShellWire({EstadoCanonico:read,BloqueSlot:async()=>'',StateURL:async()=>''});
    const first = wire.refresh();
    expect(wire.refresh()).toBe(first);
    expect((await first)?.bytes).toBe(canonicalBytes);
    expect(read).toHaveBeenCalledTimes(1);
    expect(get(wire.bus.lines)).toEqual([]);
    const changed = JSON.parse(canonicalBytes); changed.seq=8; changed.capsules[0].seq=8; changed.capsules[0].lifecycle='busy'; changed.capsules[0].ready=false;
    read.mockResolvedValueOnce(JSON.stringify(changed));
    await wire.refresh();
    expect(get(wire.bus.lines).at(-1)?.text).toContain('busy');
    wire.dispose();
    await expect(wire.refresh()).resolves.toBeNull();
    expect(read).toHaveBeenCalledTimes(2);
  });
  it('rejects invalid producer bytes and routes authority errors to STATUS', async () => {
    for (const response of [async()=>'{"schema":"invented"}', async()=>{throw new Error('REFUSED: slot OPERATOR-AUTHORITY');}]) {
      const wire = createShellWire({EstadoCanonico:response,BloqueSlot:async()=>'',StateURL:async()=>''});
      await expect(wire.refresh()).resolves.toBeNull();
      expect(get(wire.bus.lines).at(-1)?.text).toMatch(/DEGRADED/);
      wire.dispose();
    }
    const missing = createShellWire(null);
    await expect(missing.refresh()).resolves.toBeNull();
    expect(get(missing.bus.lines).at(-1)?.text).toContain('AUSENTE');
  });
  it('ignores a pending read after unmount', async () => {
    let release!: (bytes:string)=>void;
    const wire = createShellWire({EstadoCanonico:()=>new Promise(resolve=>{release=resolve;}),BloqueSlot:async()=>'',StateURL:async()=>''});
    const pending = wire.refresh();
    wire.dispose(); release(canonicalBytes);
    await expect(pending).resolves.toBeNull();
    expect(get(wire.bus.lines)).toEqual([]);
  });
  it('requires the real bridge and preserves receivers and credentials', async () => {
    expect(cooperationBridge({})).toBeNull();
    expect(recordingBridge({})).toBeNull();
    const app = {AbortarTodo:vi.fn(),CopiarSlot:vi.fn(), CederControl:vi.fn(),TomarControl:vi.fn(),CerrarSlot:vi.fn(),
      FijarDisposicion:vi.fn(function(this:unknown){expect(this).toBe(app);}),EstadoCanonico:async()=>canonicalBytes};
    const bridge = cooperationBridge({go:{main:{App:app}}});
    const credential = {holder:'ai-a',epoch:3,token:'test-only'};
    await bridge!.FijarDisposicion(1,'1x1',7,credential);
    expect(app.FijarDisposicion).toHaveBeenCalledWith(1,'1x1',7,credential);
    await bridge!.CopiarSlot('srf-ng-2',{kind:'uri',credential});
    expect(app.CopiarSlot).toHaveBeenCalledWith('srf-ng-2',{kind:'uri',credential});
    await bridge!.CerrarSlot('srf-ng-2',{epoch:3,credential});
    expect(app.CerrarSlot).toHaveBeenCalledWith('srf-ng-2',{epoch:3,credential});
    await bridge!.CopiarSlot('srf-ng-2','uri');
    expect(app.CopiarSlot).toHaveBeenLastCalledWith('srf-ng-2','uri');
  });
});

import { crearClienteSrfd, transporteWails } from '../src/lib/srfd';
it('forwards launch holder in the fixed-arity Wails DTO and retains only validated backend credentials', async () => {
  const credential = {holder:'cockpit-ui',epoch:3,token:'a'.repeat(32)};
  const issued: unknown[] = [];
  const send = vi.fn(async()=>({ok:true,rc:0,datos:{slot:2},lease:credential}));
  const client = crearClienteSrfd(transporteWails({go:{main:{App:{Enviar:send}}}}),(slot,lease)=>issued.push({slot,lease}));
  await client.iniciar('browser',2,'cockpit-ui');
  expect(send).toHaveBeenCalledWith('iniciar','browser',{slot:2,holder:'cockpit-ui'});
  expect(issued).toEqual([{slot:2,lease:credential}]);
  send.mockResolvedValueOnce({ok:true,rc:0,datos:{slot:2},lease:{...credential,holder:'another-ai'}});
  await expect(client.iniciar('browser',2,'cockpit-ui')).rejects.toThrow(/lease/);
});
