// TypeScript source; strict, erasable types. Runtime dependencies: Node/Python stdlib.
import { Buffer } from 'node:buffer';
import { spawn, execFileSync } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import { constants, closeSync, mkdtempSync, openSync, readFileSync, readSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Duplex } from 'node:stream';
import { fileURLToPath } from 'node:url';

// BEGIN TYPES (no runtime declarations in this section)
type ObjectValue = Record<string, unknown>;
export type XY = readonly [number, number];
export type Rect = readonly [number, number, number, number];
export type Query = { by: 'id' | 'role'; value: string } | { by: 'focused' };
export interface AXNode {
  nodeId: string;
  backendNodeId: number | null;
  role: string;
  name: string;
  focused: boolean;
  ignored: boolean;
}
export interface QueryNode {
  nodeId: string | null;
  backendNodeId: number | null;
  documentIndex: number | null;
  attributes: Readonly<Record<string, string>>;
  role: string;
  axName: string;
  focused: boolean;
  ignored: boolean;
  rect: Rect | null;
  scroll: XY;
  visible: boolean;
}
export interface SnapshotPair { ax: unknown; snapshot: unknown }
export type SnapshotProvider = () => SnapshotPair | Promise<SnapshotPair>;
export type Plan =
  | { kind: 'click'; xy: XY; viewport_xy: XY; attempts: number }
  | { kind: 'abort'; reason: 'not_found' | 'not_visible' | 'refused'; attempts: number };
export interface ClientOptions {
  mode?: 'echo' | 'oversize' | 'badjson' | 'attach_blank' | 'attach_other';
  maxFrameBytes?: number;
  fakeBytes?: number;
  timeoutMs?: number;
}
type Timer = ReturnType<typeof setTimeout>;
interface Pending {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
  timer: Timer;
}
interface Binding {
  resolve: (session: string) => void;
  reject: (error: Error) => void;
  timer: Timer;
}
// END TYPES

export const FAKE_URL = 'file:///opt/cu/scenes/v3/ff_form_01.html';
export const COMPUTED_STYLES = ['visibility', 'opacity', 'display'];
const ROOT = fileURLToPath(new URL('../../../../', import.meta.url));

export class ClientError extends Error {
  readonly code: 2 | 5;
  constructor(message: string, code: 2 | 5 = 5) {
    super(message);
    this.name = 'ClientError';
    this.code = code;
  }
}

function isObject(value: unknown): value is ObjectValue {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function object(value: unknown): ObjectValue { return isObject(value) ? value : {}; }
function array(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }
function string(value: unknown): string { return typeof value === 'string' ? value : ''; }
function index(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}
function integer(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  const n = Math.trunc(value);
  return Number.isSafeInteger(n) ? n : null;
}
function tableString(strings: unknown[], value: unknown): string {
  const i = index(value);
  return i === null ? '' : string(strings[i]);
}

export function parse_ax(result: unknown): AXNode[] {
  return array(object(result).nodes).flatMap((value) => {
    const n = object(value);
    if (typeof n.nodeId !== 'string') return [];
    return [{
      nodeId: n.nodeId, backendNodeId: index(n.backendDOMNodeId),
      role: string(object(n.role).value), name: string(object(n.name).value),
      focused: array(n.properties).some((p) => object(p).name === 'focused' && object(object(p).value).value === true),
      ignored: n.ignored === true,
    }];
  });
}

export function focused_node(ax: unknown): AXNode | null {
  return parse_ax(ax).find((n) => n.focused) ?? null;
}

export function parse_snapshot(snapshot: unknown, ax: unknown = {}): QueryNode[] {
  const source = object(snapshot);
  const strings = array(source.strings);
  const axNodes = parse_ax(ax);
  const byBackend = new Map<number, AXNode>();
  for (const n of axNodes) {
    if (n.backendNodeId !== null && !byBackend.has(n.backendNodeId)) byBackend.set(n.backendNodeId, n);
  }
  const joined = new Set<AXNode>();
  const result: QueryNode[] = [];
  array(source.documents).forEach((value, documentIndex) => {
    const doc = object(value), nodes = object(doc.nodes), layout = object(doc.layout);
    const attributes = array(nodes.attributes), bounds = array(layout.bounds), styles = array(layout.styles);
    const rows = new Map<number, number>();
    array(layout.nodeIndex).forEach((value, row) => {
      const i = index(value);
      if (i !== null && !rows.has(i)) rows.set(i, row);
    });
    const scroll: XY = [integer(doc.scrollOffsetX) ?? 0, integer(doc.scrollOffsetY) ?? 0];
    array(nodes.backendNodeId).forEach((value, nodeIndex) => {
      const backendNodeId = index(value);
      if (backendNodeId === null) return;
      const attr: Record<string, string> = Object.create(null);
      const flat = array(attributes[nodeIndex]);
      for (let i = 0; i + 1 < flat.length; i += 2) {
        const key = tableString(strings, flat[i]).toLowerCase();
        if (key) attr[key] = tableString(strings, flat[i + 1]);
      }
      const row = rows.get(nodeIndex);
      const raw = row === undefined ? [] : array(bounds[row]);
      const coords = raw.map(integer);
      const rect: Rect | null = coords.length === 4 && coords.every((v) => v !== null)
        ? [coords[0]!, coords[1]!, coords[2]!, coords[3]!] : null;
      const css = row === undefined ? [] : array(styles[row]);
      const visibility = tableString(strings, css[0]);
      const opacity = tableString(strings, css[1]);
      const display = tableString(strings, css[2]);
      const a = byBackend.get(backendNodeId);
      if (a) joined.add(a);
      result.push({
        nodeId: a?.nodeId ?? null, backendNodeId, documentIndex, attributes: attr,
        role: a?.role ?? '', axName: a?.name ?? '', focused: a?.focused ?? false,
        ignored: a?.ignored ?? false, rect, scroll,
        visible: rect !== null && rect[2] > 0 && rect[3] > 0 && visibility === 'visible'
          && opacity.trim() !== '' && Number.isFinite(Number(opacity)) && Number(opacity) !== 0
          && display !== '' && display !== 'none',
      });
    });
  });
  // An unjoined AX node can still carry focus/sensitive-name evidence, but no rectangle.
  for (const a of axNodes) {
    if (!joined.has(a)) result.push({
      nodeId: a.nodeId, backendNodeId: a.backendNodeId, documentIndex: null, attributes: {},
      role: a.role, axName: a.name, focused: a.focused, ignored: a.ignored,
      rect: null, scroll: [0, 0], visible: false,
    });
  }
  return result;
}

export function find_node(nodes: readonly QueryNode[], query: Query): QueryNode | null {
  if (query.by === 'focused') return nodes.find((n) => n.focused) ?? null;
  if ((query.by !== 'id' && query.by !== 'role') || typeof query.value !== 'string') {
    throw new ClientError('invalid query', 2);
  }
  return nodes.find((n) => query.by === 'id'
    ? n.attributes.id === query.value : !n.ignored && n.role === query.value) ?? null;
}

export function refused(node: QueryNode): boolean {
  const type = (node.attributes.type ?? '').trim().toLowerCase();
  const sensitive = /password|passwd|secret|token|cc-|card.?num|cvv|cvc/i;
  return type === 'password' || type === 'file'
    || [node.attributes.id ?? '', node.attributes.name ?? '', node.axName].some((v) => sensitive.test(v));
}

async function bounded<T>(work: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  let timer: Timer | undefined;
  try {
    return await Promise.race([work, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new ClientError(label + ' timeout')), timeoutMs);
    })]);
  } finally { if (timer !== undefined) clearTimeout(timer); }
}

export async function plan_click(query: Query, provider: SnapshotProvider, retries: number = 3): Promise<Plan> {
  if (!Number.isSafeInteger(retries) || retries < 1) throw new ClientError('retries must be a positive integer', 2);
  let reason: 'not_found' | 'not_visible' = 'not_found';
  for (let attempts = 1; attempts <= retries; attempts++) {
    const pair = await bounded(Promise.resolve().then(provider), 10000, 'snapshot provider');
    const nodes = parse_snapshot(pair.snapshot, pair.ax);
    const target = find_node(nodes, query);
    if (nodes.some((n) => n.focused && refused(n)) || (target !== null && refused(target))) {
      return { kind: 'abort', reason: 'refused', attempts };
    }
    if (target === null) { reason = 'not_found'; continue; }
    if (!target.visible || target.rect === null) { reason = 'not_visible'; continue; }
    const [x, y, w, h] = target.rect;
    const xy: XY = [x + Math.floor(w / 2), y + Math.floor(h / 2)];
    const viewport_xy: XY = [xy[0] - target.scroll[0], xy[1] - target.scroll[1]];
    if (![...xy, ...viewport_xy].every(Number.isSafeInteger)) throw new ClientError('coordinate overflow');
    return { kind: 'click', xy, viewport_xy, attempts };
  }
  return { kind: 'abort', reason, attempts: retries };
}

/** Payload limit excludes the NUL delimiter and applies before retaining each segment. */
export class NulFramer {
  readonly max: number;
  private parts: Uint8Array[] = [];
  private size: number = 0;
  constructor(max: number) {
    if (!Number.isSafeInteger(max) || max < 1) throw new ClientError('invalid frame limit', 2);
    this.max = max;
  }
  feed(chunk: Uint8Array): Buffer[] {
    const frames: Buffer[] = [];
    let start = 0;
    while (start < chunk.length) {
      const nul = chunk.indexOf(0, start), end = nul < 0 ? chunk.length : nul;
      if (this.size + end - start > this.max) throw new ClientError('oversize incoming frame');
      if (end > start) { this.parts.push(chunk.slice(start, end)); this.size += end - start; }
      if (nul < 0) break;
      frames.push(Buffer.concat(this.parts, this.size));
      this.parts = []; this.size = 0; start = nul + 1;
    }
    return frames;
  }
  finish(): void { if (this.size !== 0) throw new ClientError('truncated incoming frame'); }
}

function equalJSON(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((v, i) => equalJSON(v, right[i]));
  }
  if (!isObject(left) || !isObject(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length
    && keys.every((key) => Object.hasOwn(right, key) && equalJSON(left[key], right[key]));
}

function paramsAllowed(ruleValue: unknown, params: ObjectValue): boolean {
  const rule = object(ruleValue), keys = Object.keys(params);
  if (rule.mode === 'empty') return keys.length === 0;
  if (rule.mode === 'exact') return equalJSON(params, rule.value);
  if (rule.mode !== 'keys') return false;
  const allowed = object(rule.allowed);
  return array(rule.required).every((k) => typeof k === 'string' && Object.hasOwn(params, k))
    && keys.every((key) => {
      if (!Object.hasOwn(allowed, key)) return false;
      const kind = allowed[key], value = params[key];
      if (kind === 'int') return typeof value === 'number' && Number.isSafeInteger(value);
      if (kind === 'bool') return typeof value === 'boolean';
      return Array.isArray(kind) && Array.isArray(value)
        && value.every((v) => typeof v === 'string' && kind.includes(v));
    });
}

/** Fake-only transport. No executable, URL, arbitrary environment, or policy-path override. */
export class FakeCdpClient {
  readonly maxFrameBytes: number;
  readonly timeoutMs: number;
  readonly logPath: string;
  private readonly policy: ObjectValue;
  private readonly child: ChildProcess;
  private readonly input: Duplex;
  private readonly output: Duplex;
  private readonly framer: NulFramer;
  private readonly directory: string;
  private readonly reaped: Promise<void>;
  private logFd: number | null;
  private logText: string = '';
  private nextId: number = 1;
  private session: string | null = null;
  private pending: Map<number, Pending> = new Map();
  private binding: Binding | null = null;
  private handshakeTask: Promise<string> | null = null;
  private closeTask: Promise<void> | null = null;
  private failure: Error | null = null;
  private closed: boolean = false;
  private childReaped: boolean = false;

  constructor(options: ClientOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? 2000;
    if (!Number.isInteger(this.timeoutMs) || this.timeoutMs < 1 || this.timeoutMs > 10000) {
      throw new ClientError('timeoutMs must be 1..10000', 2);
    }
    try { this.policy = object(JSON.parse(readFileSync(join(ROOT, 'docker/cu/oracle/allowlist.json'), 'utf8'))); }
    catch { throw new ClientError('allow-list cannot be read', 2); }
    const limit = index(this.policy.max_frame_bytes);
    if (this.policy.schema !== 'os.cu.oracle-allowlist/v1' || limit === null || limit === 0
      || !isObject(this.policy.browser_methods) || !isObject(this.policy.session_methods)) {
      throw new ClientError('invalid allow-list', 2);
    }
    this.maxFrameBytes = options.maxFrameBytes ?? limit;
    if (!Number.isSafeInteger(this.maxFrameBytes) || this.maxFrameBytes < 1 || this.maxFrameBytes > limit) {
      throw new ClientError('frame limit must not exceed the allow-list', 2);
    }
    const mode = options.mode ?? 'echo', fakeBytes = options.fakeBytes ?? 5000;
    if (!['echo', 'oversize', 'badjson', 'attach_blank', 'attach_other'].includes(mode)
      || !Number.isSafeInteger(fakeBytes) || fakeBytes < 1 || fakeBytes > limit + 1) {
      throw new ClientError('invalid fake options', 2);
    }
    this.framer = new NulFramer(this.maxFrameBytes);
    this.directory = mkdtempSync(join(tmpdir(), 'os-x3-ts-s2-'));
    this.logPath = join(this.directory, 'fake.log');
    this.logFd = null;
    try {
      // A temporary FIFO is a file, but its frame bytes remain in kernel memory.
      execFileSync('python3', ['-B', '-c', 'import os,sys; os.mkfifo(sys.argv[1], 0o600)', this.logPath],
        { timeout: this.timeoutMs, killSignal: 'SIGKILL', stdio: 'ignore' });
      this.logFd = openSync(this.logPath, constants.O_RDONLY | constants.O_NONBLOCK);
      this.child = spawn('python3', ['-B', 'docker/cu/tests/fixtures/cdp/fake_chromium.py'], {
        cwd: ROOT, stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
        env: { PATH: process.env.PATH, FAKE_MODE: mode, FAKE_URL,
          FAKE_LOG: this.logPath, FAKE_BYTES: String(fakeBytes) },
      });
    } catch {
      if (this.logFd !== null) closeSync(this.logFd);
      rmSync(this.directory, { recursive: true, force: true });
      throw new ClientError('fake setup failed');
    }
    this.input = this.child.stdio[3] as Duplex;
    this.output = this.child.stdio[4] as Duplex;
    this.reaped = new Promise<void>((resolve) => {
      this.child.once('close', () => {
        this.childReaped = true; resolve();
        if (!this.closed) this.fail(new ClientError('fake child closed'));
      });
    });
    this.child.on('error', () => this.fail(new ClientError('fake spawn failed')));
    this.input.on('error', () => this.fail(new ClientError('CDP write failed')));
    this.output.on('error', () => this.fail(new ClientError('CDP read failed')));
    this.output.on('data', (chunk: Buffer) => {
      if (this.failure !== null) return;
      try {
        this.read_log();
        for (const frame of this.framer.feed(chunk)) this.receive(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(frame)));
      } catch (error) { this.fail(error instanceof ClientError ? error : new ClientError('invalid incoming frame')); }
    });
    this.output.on('end', () => {
      if (this.closed) return;
      try { this.framer.finish(); this.fail(new ClientError('CDP EOF')); }
      catch (error) { this.fail(error instanceof Error ? error : new ClientError('CDP EOF')); }
    });
  }

  get sessionId(): string | null { return this.session; }
  get pid(): number | undefined { return this.child.pid; }
  get isReaped(): boolean { return this.childReaped; }

  static async open(options: ClientOptions = {}): Promise<FakeCdpClient> {
    const client = new FakeCdpClient(options);
    try { await client.handshake(); return client; }
    catch (error) { await client.close(); throw error; }
  }

  handshake(): Promise<string> {
    if (this.handshakeTask !== null) return this.handshakeTask;
    this.handshakeTask = (async () => {
      if (this.failure !== null) throw this.failure;
      const binding = new Promise<string>((resolve, reject) => {
        this.binding = { resolve, reject,
          timer: setTimeout(() => this.fail(new ClientError('attachment timeout')), this.timeoutMs) };
      });
      const [, session] = await Promise.all([
        this.request('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }), binding,
      ]);
      return session;
    })();
    return this.handshakeTask;
  }

  async request(method: string, params: ObjectValue = {}): Promise<unknown> {
    // Complete local policy check BEFORE allocating an ID or writing bytes.
    const browser = object(this.policy.browser_methods), sessions = object(this.policy.session_methods);
    const browserMethod = Object.hasOwn(browser, method);
    if (method === 'Page.navigate' || /^(Input|Runtime|Emulation|DOM)\./.test(method)
      || (!browserMethod && !Object.hasOwn(sessions, method))) throw new ClientError('method refused');
    if (!isObject(params) || !paramsAllowed(object(browserMethod ? browser[method] : sessions[method]).params, params)) {
      throw new ClientError('parameters refused');
    }
    if (!browserMethod && this.session === null) throw new ClientError('session not bound');
    if (this.failure !== null) throw this.failure;
    if (!Number.isSafeInteger(this.nextId)) throw new ClientError('request ID exhausted');
    const id = this.nextId;
    const request: ObjectValue = { id, method, params };
    if (!browserMethod) request.sessionId = this.session;
    const bytes = Buffer.from(JSON.stringify(request) + '\0');
    if (bytes.length - 1 > this.maxFrameBytes) throw new ClientError('oversize outgoing frame');
    this.nextId++;
    return new Promise<unknown>((resolve, reject) => {
      this.pending.set(id, { resolve, reject,
        timer: setTimeout(() => this.fail(new ClientError('response timeout')), this.timeoutMs) });
      this.input.write(bytes, (error) => { if (error) this.fail(new ClientError('CDP write failed')); });
    });
  }

  private receive(value: unknown): void {
    if (!isObject(value)) throw new ClientError('incoming frame must be an object');
    if (typeof value.method === 'string') {
      if (value.method === 'Target.attachedToTarget' && this.session === null && this.binding !== null) {
        const params = object(value.params), target = object(params.targetInfo);
        if (target.type === 'page' && target.url === FAKE_URL && typeof params.sessionId === 'string' && params.sessionId !== '') {
          this.session = params.sessionId;
          clearTimeout(this.binding.timer); this.binding.resolve(this.session); this.binding = null;
        }
      }
      return; // Every other event, including targetInfoChanged and Runtime.*, is ignored.
    }
    const id = index(value.id), pending = id === null ? undefined : this.pending.get(id);
    if (id === null || pending === undefined) return;
    clearTimeout(pending.timer); this.pending.delete(id);
    if (Object.hasOwn(value, 'error')) pending.reject(new ClientError('CDP response error'));
    else if (Object.hasOwn(value, 'result')) pending.resolve(value.result);
    else pending.reject(new ClientError('response missing result'));
  }

  private fail(error: Error): void {
    if (this.failure !== null) return;
    this.failure = error;
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(error); }
    this.pending.clear();
    if (this.binding !== null) { clearTimeout(this.binding.timer); this.binding.reject(error); this.binding = null; }
    if (!this.childReaped) this.child.kill('SIGKILL');
  }

  async snapshot(): Promise<SnapshotPair> {
    const ax = await this.request('Accessibility.getFullAXTree');
    const snapshot = await this.request('DOMSnapshot.captureSnapshot', { computedStyles: [...COMPUTED_STYLES] });
    return { ax, snapshot };
  }

  /** The fake's received-request log, drained from its FIFO and held only in memory. */
  read_log(): string {
    if (this.logFd === null) return this.logText;
    const chunk = Buffer.alloc(65536);
    for (;;) {
      try {
        const n = readSync(this.logFd, chunk, 0, chunk.length, null);
        if (n === 0) break;
        this.logText += chunk.subarray(0, n).toString('utf8');
      } catch (error) {
        const code = object(error).code;
        if (code === 'EAGAIN' || code === 'EWOULDBLOCK') break;
        throw new ClientError('fake log read failed');
      }
    }
    return this.logText;
  }

  close(): Promise<void> {
    if (this.closeTask !== null) return this.closeTask;
    this.closeTask = (async () => {
      this.closed = true;
      this.fail(new ClientError('client closed'));
      if (!this.childReaped) this.child.kill('SIGKILL');
      this.input.destroy(); this.output.destroy();
      try { await bounded(this.reaped, this.timeoutMs, 'child reap'); }
      finally {
        try { this.read_log(); }
        finally {
          if (this.logFd !== null) { closeSync(this.logFd); this.logFd = null; }
          rmSync(this.directory, { recursive: true, force: true });
        }
      }
    })();
    return this.closeTask;
  }
}
