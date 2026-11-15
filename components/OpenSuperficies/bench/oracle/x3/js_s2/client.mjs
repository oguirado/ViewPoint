import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const FAKE_URL = 'file:///opt/cu/scenes/v3/ff_form_01.html';
export const COMPUTED_STYLES = Object.freeze(['visibility', 'opacity', 'display']);
const ROOT = fileURLToPath(new URL('../../../../', import.meta.url));
const MAX_WAIT_MS = 10_000;
const AUTO_ATTACH = Object.freeze({
  autoAttach: true, waitForDebuggerOnStart: false, flatten: true,
});
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

export class ClientError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ClientError';
    this.code = code;
  }
}

function positiveInteger(value, label, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < 1 || value > maximum) {
    throw new ClientError(2, `${label} must be an integer in 1..${maximum}`);
  }
  return value;
}

function bounded(promise, timeoutMs, label) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new ClientError(7, `${label} timed out`)), timeoutMs);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

// The limit excludes the terminating NUL. Check before accumulating each slice.
export class NulFramer {
  constructor(maxFrameBytes) {
    this.limit = positiveInteger(maxFrameBytes, 'maxFrameBytes');
    this.parts = [];
    this.length = 0;
  }

  feed(chunk) {
    const frames = [];
    for (let start = 0; start < chunk.length;) {
      const end = chunk.indexOf(0, start);
      const stop = end < 0 ? chunk.length : end;
      const size = stop - start;
      if (this.length + size > this.limit) {
        this.parts = [];
        this.length = 0;
        throw new ClientError(5, 'oversize incoming frame');
      }
      if (size) this.parts.push(chunk.subarray(start, stop));
      this.length += size;
      if (end < 0) break;
      frames.push(Buffer.concat(this.parts, this.length));
      this.parts = [];
      this.length = 0;
      start = end + 1;
    }
    return frames;
  }
}

function equalValue(left, right) {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((v, i) => equalValue(v, right[i]));
  }
  if (!record(left) || !record(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length
    && keys.every(key => own(right, key) && equalValue(left[key], right[key]));
}

function allowedParams(rule, params) {
  if (!record(params) || !record(rule)) return false;
  const keys = Object.keys(params);
  if (rule.mode === 'empty') return keys.length === 0;
  if (rule.mode === 'exact') return equalValue(params, rule.value);
  if (rule.mode !== 'keys' || !record(rule.allowed) || !Array.isArray(rule.required)) return false;
  return rule.required.every(key => own(params, key)) && keys.every(key => {
    if (!own(rule.allowed, key)) return false;
    const expected = rule.allowed[key];
    const value = params[key];
    if (expected === 'int') return Number.isSafeInteger(value);
    if (expected === 'bool') return typeof value === 'boolean';
    return Array.isArray(expected) && Array.isArray(value)
      && value.every(item => typeof item === 'string' && expected.includes(item));
  });
}

/** This transport can launch only the bound Python fake, never a browser or socket. */
export class FakeCDPClient {
  #child;
  #allowlist;
  #framer;
  #timeoutMs;
  #pending = new Map();
  #nextId = 0;
  #sessionId = null;
  #attachment = null;
  #startPromise = null;
  #closePromise = null;
  #closed;
  #exitInfo = null;
  #fatal = null;
  #logDirectory;
  #logPath;
  #audit = null;

  constructor({ mode = 'echo', maxFrameBytes, fakeBytes = 4096, timeoutMs = MAX_WAIT_MS } = {}) {
    this.#timeoutMs = positiveInteger(timeoutMs, 'timeoutMs', MAX_WAIT_MS);
    if (!['echo', 'oversize', 'badjson', 'attach_blank', 'attach_other'].includes(mode)) {
      throw new ClientError(2, 'unknown fake mode');
    }
    positiveInteger(fakeBytes, 'fakeBytes');
    try {
      this.#allowlist = JSON.parse(readFileSync(join(ROOT, 'docker/cu/oracle/allowlist.json'), 'utf8'));
    } catch {
      throw new ClientError(5, 'allowlist unreadable or malformed');
    }
    if (this.#allowlist.schema !== 'os.cu.oracle-allowlist/v1'
        || !record(this.#allowlist.browser_methods) || !record(this.#allowlist.session_methods)) {
      throw new ClientError(5, 'invalid allowlist schema');
    }
    const ceiling = positiveInteger(this.#allowlist.max_frame_bytes, 'allowlist max_frame_bytes');
    this.#framer = new NulFramer(positiveInteger(maxFrameBytes ?? ceiling, 'maxFrameBytes', ceiling));
    this.#logDirectory = mkdtempSync(join(tmpdir(), 'os-x3-js-s2-'));
    this.#logPath = join(this.#logDirectory, 'fake.log');
    try {
      this.#child = spawn('python3', ['-B', 'docker/cu/tests/fixtures/cdp/fake_chromium.py'], {
        cwd: ROOT,
        // Parent writes stdio[3]; parent reads stdio[4]. No inherited stdio or socket listener.
        stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
        env: {
          PATH: process.env.PATH ?? '/usr/bin:/bin',
          FAKE_MODE: mode, FAKE_URL, FAKE_LOG: this.#logPath, FAKE_BYTES: String(fakeBytes),
        },
      });
    } catch {
      rmSync(this.#logDirectory, { recursive: true, force: true });
      throw new ClientError(5, 'fake spawn failed');
    }
    this.#closed = new Promise(resolve => {
      this.#child.once('close', (code, signal) => {
        this.#exitInfo = { code, signal };
        resolve(this.#exitInfo);
        if (!this.#closePromise) this.#fail(new ClientError(5, 'fake pipe closed'));
      });
    });
    this.#child.on('error', () => this.#fail(new ClientError(5, 'fake spawn failed')));
    this.#child.stdio[3].on('error', () => this.#fail(new ClientError(5, 'fake write failed')));
    this.#child.stdio[4].on('error', () => this.#fail(new ClientError(5, 'fake read failed')));
    this.#child.stdio[4].on('data', chunk => this.#receive(chunk));
  }

  get sessionId() { return this.#sessionId; }
  get pid() { return this.#child.pid; }
  get exitInfo() { return this.#exitInfo === null ? null : { ...this.#exitInfo }; }
  get logPath() { return this.#logPath; }
  get maxFrameBytes() { return this.#framer.limit; }

  auditLog() {
    if (this.#audit !== null) return this.#audit;
    try { return readFileSync(this.#logPath, 'utf8'); }
    catch (error) {
      if (error.code === 'ENOENT') return '';
      throw new ClientError(5, 'fake audit log unreadable');
    }
  }

  #fail(error) {
    if (this.#fatal) return;
    this.#fatal = error;
    for (const waiter of this.#pending.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(error);
    }
    this.#pending.clear();
    if (this.#attachment) {
      clearTimeout(this.#attachment.timer);
      this.#attachment.reject(error);
      this.#attachment = null;
    }
    if (this.#exitInfo === null) this.#child.kill('SIGKILL');
  }

  #receive(chunk) {
    if (this.#fatal || this.#closePromise) return;
    try {
      for (const frame of this.#framer.feed(chunk)) {
        const message = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(frame));
        if (!record(message)) throw new ClientError(5, 'incoming frame is not an object');
        if (own(message, 'method')) {
          const p = message.params;
          if (this.#attachment && this.#sessionId === null
              && message.method === 'Target.attachedToTarget'
              && record(p) && record(p.targetInfo) && p.targetInfo.type === 'page'
              && p.targetInfo.url === FAKE_URL && typeof p.sessionId === 'string' && p.sessionId) {
            this.#sessionId = p.sessionId;
            clearTimeout(this.#attachment.timer);
            this.#attachment.resolve(p.sessionId);
            this.#attachment = null;
          }
          continue; // Includes console events, info changes and all later attachments.
        }
        if (!Number.isSafeInteger(message.id)) continue;
        const waiter = this.#pending.get(message.id);
        if (!waiter) continue;
        if (!own(message, 'result') && !own(message, 'error')) {
          throw new ClientError(5, 'response has neither result nor error');
        }
        this.#pending.delete(message.id);
        clearTimeout(waiter.timer);
        if (own(message, 'error')) waiter.reject(new ClientError(5, 'CDP request returned an error'));
        else waiter.resolve(message.result ?? {});
      }
    } catch (error) {
      this.#fail(error instanceof ClientError ? error : new ClientError(5, 'invalid incoming JSON frame'));
    }
  }

  async request(method, params = {}) {
    // Policy precedes id allocation, serialization, pending waits, and every pipe write.
    if (typeof method !== 'string' || /^(Input|Runtime|Emulation|DOM)\./.test(method)
        || method === 'Page.navigate') {
      throw new ClientError(5, 'method refused locally');
    }
    const browser = own(this.#allowlist.browser_methods, method);
    const session = own(this.#allowlist.session_methods, method);
    if (!browser && !session) throw new ClientError(5, 'method not allowlisted');
    const spec = (browser ? this.#allowlist.browser_methods : this.#allowlist.session_methods)[method];
    if (!allowedParams(spec.params, params)) throw new ClientError(5, 'parameters refused locally');
    if (!browser && this.#sessionId === null) throw new ClientError(5, 'page session is not bound');
    if (this.#fatal) throw this.#fatal;
    if (this.#closePromise) throw new ClientError(5, 'client is closed');
    const id = positiveInteger(this.#nextId + 1, 'request id');
    const message = { id, method, params };
    if (!browser) message.sessionId = this.#sessionId;
    const payload = Buffer.from(JSON.stringify(message), 'utf8');
    if (payload.length > this.#framer.limit) throw new ClientError(5, 'oversize outgoing frame');
    this.#nextId = id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.#fail(new ClientError(7, 'CDP response timed out')), this.#timeoutMs);
      this.#pending.set(id, { resolve, reject, timer });
      try {
        this.#child.stdio[3].write(Buffer.concat([payload, Buffer.from([0])]), error => {
          if (error) this.#fail(new ClientError(5, 'fake write failed'));
        });
      } catch {
        this.#fail(new ClientError(5, 'fake write failed'));
      }
    });
  }

  start() {
    if (this.#startPromise) return this.#startPromise;
    if (this.#fatal || this.#closePromise) return Promise.reject(this.#fatal ?? new ClientError(5, 'client is closed'));
    const attachment = new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.#fail(new ClientError(7, 'page attachment timed out')), this.#timeoutMs);
      this.#attachment = { resolve, reject, timer };
    });
    // Install the attachment waiter before the fake can emit its event before the reply.
    this.#startPromise = Promise.all([attachment, this.request('Target.setAutoAttach', { ...AUTO_ATTACH })])
      .then(() => this.#sessionId)
      .catch(async error => { await this.close(); throw error; });
    return this.#startPromise;
  }

  async snapshot() {
    await this.start();
    const ax = await this.request('Accessibility.getFullAXTree');
    const snapshot = await this.request('DOMSnapshot.captureSnapshot', { computedStyles: [...COMPUTED_STYLES] });
    return { ax, snapshot };
  }

  close() {
    if (this.#closePromise) return this.#closePromise;
    this.#closePromise = (async () => {
      this.#fail(new ClientError(5, 'client closed'));
      if (this.#exitInfo === null) this.#child.kill('SIGKILL');
      this.#child.stdio[3].destroy();
      this.#child.stdio[4].destroy();
      try {
        // Node reaps the child before emitting close; a kill alone is insufficient.
        await bounded(this.#closed, MAX_WAIT_MS, 'child close');
        this.#audit = this.auditLog();
      } finally {
        rmSync(this.#logDirectory, { recursive: true, force: true });
      }
    })();
    return this.#closePromise;
  }
}

const text = value => typeof value === 'string' ? value : '';
const integer = value => typeof value === 'number' && Number.isFinite(value)
  && Number.isSafeInteger(Math.floor(value)) ? Math.floor(value) || 0 : null;

/** Pure decoding. AX nodeId and backendDOMNodeId are different identifiers. */
export function parseAXTree(result = {}) {
  return (Array.isArray(result?.nodes) ? result.nodes : []).filter(record).map(node => ({
    nodeId: node.nodeId ?? null,
    backendNodeId: Number.isSafeInteger(node.backendDOMNodeId) ? node.backendDOMNodeId : null,
    role: text(node.role?.value),
    name: text(node.name?.value),
    focused: (Array.isArray(node.properties) ? node.properties : [])
      .some(p => p?.name === 'focused' && p.value?.value === true),
  }));
}

/** Pure decoding in document/node order. Missing style or geometry fails closed. */
export function parseSnapshot(result = {}) {
  const strings = Array.isArray(result?.strings) ? result.strings : [];
  const stringAt = index => Number.isInteger(index) ? text(strings[index]) : '';
  const documents = Array.isArray(result?.documents) ? result.documents : [];
  return documents.flatMap((document, documentIndex) => {
    if (!record(document)) return [];
    const nodes = document.nodes ?? {};
    const layout = document.layout ?? {};
    const positions = new Map((Array.isArray(layout.nodeIndex) ? layout.nodeIndex : []).map((n, i) => [n, i]));
    const scrollOffsetX = integer(document.scrollOffsetX ?? 0);
    const scrollOffsetY = integer(document.scrollOffsetY ?? 0);
    return (Array.isArray(nodes.backendNodeId) ? nodes.backendNodeId : []).flatMap((backendNodeId, index) => {
      if (!Number.isSafeInteger(backendNodeId)) return [];
      const rawAttributes = Array.isArray(nodes.attributes?.[index]) ? nodes.attributes[index] : [];
      const entries = [];
      for (let i = 0; i + 1 < rawAttributes.length; i += 2) {
        const key = stringAt(rawAttributes[i]).toLowerCase();
        if (key) entries.push([key, stringAt(rawAttributes[i + 1])]);
      }
      const position = positions.get(index);
      const bounds = layout.bounds?.[position];
      const rect = Array.isArray(bounds) && bounds.length === 4 ? bounds.map(integer) : null;
      const values = layout.styles?.[position];
      const styles = Object.fromEntries(COMPUTED_STYLES.map((key, i) => [key, stringAt(values?.[i])]));
      const opacity = Number(styles.opacity);
      const visible = styles.visibility === 'visible' && styles.opacity.trim() !== ''
        && Number.isFinite(opacity) && opacity !== 0 && styles.display !== '' && styles.display !== 'none'
        && rect !== null && rect.every(v => v !== null) && rect[2] > 0 && rect[3] > 0
        && scrollOffsetX !== null && scrollOffsetY !== null;
      return [{ backendNodeId, attributes: Object.fromEntries(entries), rect, styles,
        scrollOffsetX, scrollOffsetY, documentIndex, visible }];
    });
  });
}

/** Accept a {ax, snapshot} bundle, or a bare DOMSnapshot/AX result for pure queries. */
export function parseTree(bundle = {}) {
  const input = record(bundle) ? bundle : {};
  const ax = parseAXTree(input.ax ?? (Array.isArray(input.nodes) ? input : {}));
  const dom = parseSnapshot(input.snapshot ?? input);
  const byBackend = new Map();
  for (const node of ax) {
    if (node.backendNodeId === null) continue;
    if (!byBackend.has(node.backendNodeId)) byBackend.set(node.backendNodeId, []);
    byBackend.get(node.backendNodeId).push(node);
  }
  const known = new Set(dom.map(node => node.backendNodeId));
  const joined = dom.flatMap(node => {
    const matches = byBackend.get(node.backendNodeId);
    return matches ? matches.map(a => ({ ...node, ...a }))
      : [{ ...node, nodeId: null, role: '', name: '', focused: false }];
  });
  for (const node of ax) {
    if (node.backendNodeId !== null && known.has(node.backendNodeId)) continue;
    joined.push({ attributes: {}, rect: null, styles: {}, scrollOffsetX: 0, scrollOffsetY: 0,
      documentIndex: null, visible: false, ...node });
  }
  return joined;
}

function checkQuery(query) {
  if (!record(query) || Object.keys(query).length !== 1
      || !((own(query, 'id') && typeof query.id === 'string')
        || (own(query, 'role') && typeof query.role === 'string')
        || (own(query, 'focused') && query.focused === true))) {
    throw new ClientError(2, 'query must be exactly {id: string}, {role: string}, or {focused: true}');
  }
}

export function findNode(tree, query) {
  checkQuery(query);
  return tree.find(node => own(query, 'id') ? node.attributes.id === query.id
    : own(query, 'role') ? node.role === query.role : node.focused) ?? null;
}

export function isRefused(node) {
  if (!node) return false;
  const attributes = node.attributes ?? {};
  if (/^(password|file)$/i.test(text(attributes.type).trim())) return true;
  const sensitive = /password|passwd|secret|token|cc-|card[\s\S]?num|cvv|cvc/i;
  return [attributes.id, attributes.name, node.name].some(value => sensitive.test(text(value)));
}

/** Plan only. retries is the total snapshot-attempt budget, including the first. */
export async function plan_click(query, snapshotProvider, retries = 3, { timeoutMs = MAX_WAIT_MS } = {}) {
  checkQuery(query);
  positiveInteger(retries, 'retries');
  positiveInteger(timeoutMs, 'timeoutMs', MAX_WAIT_MS);
  if (typeof snapshotProvider !== 'function') throw new ClientError(2, 'snapshotProvider must be a function');
  let reason = 'not_found';
  for (let attempts = 1; attempts <= retries; attempts++) {
    const bundle = await bounded(Promise.resolve().then(() => snapshotProvider(attempts)), timeoutMs, 'snapshot provider');
    const tree = parseTree(bundle);
    const node = findNode(tree, query);
    if (isRefused(node) || tree.some(candidate => candidate.focused && isRefused(candidate))) {
      return { kind: 'abort', reason: 'refused', attempts };
    }
    reason = node ? 'not_visible' : 'not_found';
    if (!node?.visible) continue;
    const [x, y, w, h] = node.rect;
    const xy = [x + Math.floor(w / 2), y + Math.floor(h / 2)];
    const viewport_xy = [xy[0] - node.scrollOffsetX, xy[1] - node.scrollOffsetY];
    if (![...xy, ...viewport_xy].every(Number.isSafeInteger)) continue;
    return { kind: 'click', xy, viewport_xy, attempts };
  }
  return { kind: 'abort', reason, attempts: retries };
}
