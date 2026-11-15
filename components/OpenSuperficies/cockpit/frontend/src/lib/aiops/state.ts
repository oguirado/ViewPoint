import type { CapsuleRect, CockpitState, SlotBlock, SplitNode } from '../types/hypervisor';
export type { CapsuleRect, CockpitState, SlotBlock, SplitNode, Metric } from '../types/hypervisor';

const HOST = '[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?';
const SLOT = 'srf-ng-(?:[0-9]|[1-7][0-9])';
const URI = new RegExp(`^os://(${HOST})/ws/([1-9][0-9]{0,2})/slot/(${SLOT})$`);
export interface SlotIdentity { host: string; workspace: number; slot_id: string }
export function parseSlotURI(uri: string): SlotIdentity {
  const m = URI.exec(uri);
  if (!m) throw new Error('Invalid slot URI');
  return { host: m[1], workspace: Number(m[2]), slot_id: m[3] };
}
export function buildSlotURI(host: string, workspace: number, slot_id: string): string {
  const uri = `os://${host}/ws/${workspace}/slot/${slot_id}`;
  parseSlotURI(uri);
  return uri;
}
/** Go owns canonicalisation. This function is intentionally an identity on producer bytes. */
export function canonical(bytes: string): string {
  if (typeof bytes !== 'string') throw new TypeError('Canonical state requires Go bytes');
  return bytes;
}

export interface StateBridge {
  EstadoCanonico(): Promise<string>;
  BloqueSlot(slot_id: string): Promise<string>;
  StateURL(): Promise<string>;
}
export function stateBridge(global: unknown = globalThis): StateBridge | null {
  const app = (global as { go?: { main?: { App?: Partial<StateBridge> } } } | null)?.go?.main?.App;
  if (!app || typeof app.EstadoCanonico !== 'function' || typeof app.BloqueSlot !== 'function' || typeof app.StateURL !== 'function') return null;
  return {
    EstadoCanonico: app.EstadoCanonico.bind(app),
    BloqueSlot: app.BloqueSlot.bind(app),
    StateURL: app.StateURL.bind(app),
  };
}
export async function readState(bridge: StateBridge): Promise<{ bytes: string; document: CockpitState }> {
  const bytes = canonical(await bridge.EstadoCanonico());
  const document: unknown = JSON.parse(bytes);
  assertState(document);
  return { bytes, document };
}
export async function readSlot(bridge: StateBridge, slot_id: string): Promise<string> {
  if (!new RegExp(`^${SLOT}$`).test(slot_id)) throw new Error('Invalid slot identity');
  return canonical(await bridge.BloqueSlot(slot_id));
}

// Consumer-side contract checks. These never publish geometry or serialize a snapshot.
type Obj = Record<string, unknown>;
function fail(reason: string): never { throw new Error(`Invalid cockpit state: ${reason}`); }
function obj(value: unknown, fields: string[]): Obj {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) fail('object');
  const o = value as Obj;
  const keys = Object.keys(o);
  if (keys.length !== fields.length || fields.some(k => !Object.prototype.hasOwnProperty.call(o, k))) fail('fields');
  return o;
}
function integer(v: unknown, min: number, max = Number.MAX_SAFE_INTEGER): asserts v is number {
  if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < min || v > max) fail('integer');
}
function one(v: unknown, choices: readonly unknown[]): void { if (!choices.includes(v)) fail('enum'); }
function rect(v: unknown): CapsuleRect {
  const r = obj(v, ['x', 'y', 'w', 'h']);
  integer(r.x, 0, 65535); integer(r.y, 0, 65535); integer(r.w, 1, 65535); integer(r.h, 1, 65535);
  return r as unknown as CapsuleRect;
}
function sameRect(a: CapsuleRect, b: CapsuleRect): boolean { return a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h; }
const CANVAS: Record<string, readonly [number, number]> = {
  'tile_1_6/1x1': [1264, 1024], 'column_1_3_tall/1x1': [1264, 2080],
  'column_1_3_dual/1x1': [1264, 1024], 'column_1_3_dual/1x2': [1264, 2048],
};
export function assertState(value: unknown): asserts value is CockpitState {
  const d = obj(value, ['capsules', 'host', 'schema', 'seq', 'ts_mono_ms', 'window_mode', 'workspaces']);
  if (!['os.cu.cockpit-state/v1','os.cu.cockpit-state/v2'].includes(d.schema as string) || typeof d.host !== 'string' || !new RegExp(`^${HOST}$`).test(d.host)) fail('schema/host');
  integer(d.seq, 0); integer(d.ts_mono_ms, 1);
  one(d.window_mode, ['tile_1_6', 'column_1_3_tall', 'column_1_3_dual']);
  if (!Array.isArray(d.capsules) || d.capsules.length > 80 || !Array.isArray(d.workspaces) || d.workspaces.length < 1 || d.workspaces.length > 999) fail('collections');
  const geometry = new Map<string, { n: number; rect: CapsuleRect }>();
  let prevN = 0;
  for (const raw of d.workspaces) {
    const w = obj(raw, ['layout', 'n', 'rect', 'tree']);
    integer(w.n, 1, 999); if (w.n <= prevN) fail('workspace order'); prevN = w.n;
    one(w.layout, ['1x1', '1x2', '2x2']);
    const r = rect(w.rect); const canvas = d.schema === 'os.cu.cockpit-state/v2' && w.layout === '2x2' ? [1264,1024] : CANVAS[`${d.window_mode}/${w.layout}`];
    if (!canvas || r.x !== 0 || r.y !== 0 || r.w !== canvas[0] || r.h !== canvas[1]) fail('canvas');
    let leaves = 0;
    const visit = (node: unknown, area: CapsuleRect, depth: number): void => {
      if (typeof node !== 'object' || node === null) fail('tree');
      if ((node as Obj).type === 'leaf') {
        const t = obj(node, d.schema === 'os.cu.cockpit-state/v2' ? ['slot_id','type','scale_q16'] : ['slot_id','type']);
        if (typeof t.slot_id !== 'string' || !new RegExp(`^${SLOT}$`).test(t.slot_id)) fail('leaf identity');
        if (geometry.has(t.slot_id) || area.w < (d.schema === 'os.cu.cockpit-state/v2' ? 600 : 1264) || area.h < (d.schema === 'os.cu.cockpit-state/v2' ? 420 : 512)) fail('duplicate/useful leaf');
        if (d.schema === 'os.cu.cockpit-state/v2' && t.scale_q16 !== Math.min(65536,Math.floor(area.w*65536/1280),Math.floor(area.h*65536/900))) fail('framebuffer scale');
        geometry.set(t.slot_id, { n: w.n as number, rect: area }); leaves++; return;
      }
      const t = obj(node, ['direction', 'left', 'ratio_q16', 'right', 'type']);
      if (t.type !== 'split' || depth >= 2) fail('split/depth');
      one(t.direction, ['vertical', 'horizontal']); integer(t.ratio_q16, 4096, 61440);
      const a = { ...area }, b = { ...area };
      if (t.direction === 'vertical') { a.h = Math.floor(area.h * t.ratio_q16 / 65536); b.y += a.h; b.h -= a.h; }
      else { a.w = Math.floor(area.w * t.ratio_q16 / 65536); b.x += a.w; b.w -= a.w; }
      visit(t.left, a, depth + 1); visit(t.right, b, depth + 1);
    };
    visit(w.tree, r, 0);
    if (leaves !== ({ '1x1': 1, '1x2': 2, '2x2': 4 } as Record<string, number>)[w.layout as string]) fail('arity');
  }
  if (geometry.size !== d.capsules.length) fail('bijection');
  let prevSlot = -1;
  const ids = new Set<string>();
  for (const raw of d.capsules) {
    const c = obj(raw, ['archetype', 'cdp', 'geometry', 'health', 'lease_epoch', 'lease_holder', 'lifecycle', 'mode', 'ready', 'rec_state', 'seq', 'slot', 'slot_id', 'telemetry', 'uri', 'workspace', 'xpra_port']);
    integer(c.slot, 0, 79); integer(c.workspace, 1, 999); integer(c.seq, 0); integer(c.lease_epoch, 0);
    if (c.slot <= prevSlot || c.slot_id !== `srf-ng-${c.slot}` || ids.has(c.slot_id as string)) fail('slot identity/order');
    prevSlot = c.slot; ids.add(c.slot_id as string);
    one(c.archetype, ['browser', 'desktop', 'microvm', 'terminal']);
    one(c.health, ['healthy', 'attaching', 'degraded', 'breach', 'zombie']);
    one(c.lifecycle, ['booting', 'ready', 'busy', 'stopping', 'stopped', 'failed']);
    one(c.mode, ['read_only', 'operator_controlled', 'agent_locked', 'switching']);
    if (c.xpra_port !== 10020 + c.slot || c.cdp !== (c.archetype === 'browser' ? 9220 + c.slot : null)) fail('ports');
    if (c.mode === 'agent_locked') {
      if (typeof c.lease_holder !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(c.lease_holder) || c.lease_epoch < 1) fail('lease');
    } else if (c.lease_holder !== null) fail('lease holder');
    if (c.ready !== (c.lifecycle === 'ready') || c.seq !== d.seq) fail('ready/seq');
    if (c.uri !== buildSlotURI(d.host, c.workspace, c.slot_id as string)) fail('uri');
    const p = geometry.get(c.slot_id as string);
    if (!p || p.n !== c.workspace || !sameRect(rect(c.geometry), p.rect)) fail('geometry');
    const rec = obj(c.rec_state, ['phase', 'store', 'ttl_ms']);
    one(rec.phase, ['idle', 'starting', 'recording', 'stopping', 'sealed', 'error']); one(rec.store, ['ram', 'disk', null]);
    if (rec.store === 'ram') integer(rec.ttl_ms, 0); else if (rec.ttl_ms !== null) fail('ttl');
    if (rec.phase === 'idle' && rec.store !== null || ['starting', 'recording', 'stopping'].includes(rec.phase as string) && rec.store !== 'ram' || rec.store === 'disk' && rec.phase !== 'sealed' || rec.phase === 'sealed' && rec.store === null) fail('recording state machine');
    const telemetry = obj(c.telemetry, ['cpu_permille', 'mem_bytes']);
    for (const value of Object.values(telemetry)) {
      if (value === null) continue;
      const m = obj(value, ['source', 'ts_mono_ms', 'value']);
      one(m.source, ['cgroup', 'procfs', 'srfd', 'sysfs', 'vadapt']); integer(m.ts_mono_ms, 1); integer(m.value, 0);
      if (!(m.ts_mono_ms <= d.ts_mono_ms && d.ts_mono_ms <= m.ts_mono_ms + 3000)) fail('stale/future metric');
    }
  }
}
