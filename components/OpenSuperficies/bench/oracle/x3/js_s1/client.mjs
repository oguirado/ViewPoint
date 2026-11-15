import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

export function loadAllowlist(allowlistPath) {
  const p = allowlistPath || join(__dirname, '../../../..', 'docker/cu/oracle/allowlist.json');
  const content = readFileSync(p, 'utf8');
  return JSON.parse(content);
}

export class CDPClient {
  constructor(options = {}) {
    this.allowlistPath = options.allowlistPath || join(__dirname, '../../../..', 'docker/cu/oracle/allowlist.json');
    this.fakeChromiumPath = options.fakeChromiumPath || join(__dirname, '../../../..', 'docker/cu/tests/fixtures/cdp/fake_chromium.py');
    this.fakeUrl = options.fakeUrl || 'file:///opt/cu/scenes/v3/ff_form_01.html';
    this.fakeMode = options.fakeMode || 'echo';
    this.fakeLog = options.fakeLog || null;
    this.fakeBytes = options.fakeBytes || null;
    this.maxFrameBytes = options.maxFrameBytes || null;
    this.pythonBin = options.pythonBin || 'python3';

    this.allowlist = null;
    this.child = null;
    this.sessionId = null;
    this.nextId = 1;
    this.pending = new Map();
    this.readBuffer = Buffer.alloc(0);
    this.handshakeResolved = false;
    this.handshakePromise = null;
    this.handshakeResolve = null;
    this.handshakeReject = null;
    this.closed = false;
  }

  async connect() {
    this.allowlist = loadAllowlist(this.allowlistPath);
    const maxAllowed = this.maxFrameBytes || this.allowlist.max_frame_bytes || 104857600;

    const env = { ...process.env };
    env.FAKE_MODE = this.fakeMode;
    env.FAKE_URL = this.fakeUrl;
    if (this.fakeLog) {
      env.FAKE_LOG = this.fakeLog;
    }
    if (this.fakeBytes) {
      env.FAKE_BYTES = String(this.fakeBytes);
    }

    this.handshakePromise = new Promise((resolve, reject) => {
      this.handshakeResolve = resolve;
      this.handshakeReject = reject;
    });

    // Spawn child process with fd 3 (write) and fd 4 (read)
    this.child = spawn(this.pythonBin, ['-B', this.fakeChromiumPath], {
      stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
      env
    });

    const inPipe = this.child.stdio[3];
    const outPipe = this.child.stdio[4];

    // Handle stream errors to prevent uncaughtException on abrupt child exit
    inPipe.on('error', () => {});
    outPipe.on('error', () => {});

    const handshakeTimeout = setTimeout(() => {
      if (!this.handshakeResolved) {
        const err = new Error('Handshake timeout');
        err.code = 5;
        this.handshakeReject(err);
        this.close();
      }
    }, 10000);

    outPipe.on('data', (chunk) => {
      this.readBuffer = Buffer.concat([this.readBuffer, chunk]);

      while (true) {
        const nulIndex = this.readBuffer.indexOf(0x00);
        if (nulIndex >= 0) {
          if (nulIndex > maxAllowed) {
            const err = new Error(`Frame size ${nulIndex} exceeds max_frame_bytes ${maxAllowed}`);
            err.code = 5;
            if (!this.handshakeResolved) {
              this.handshakeReject(err);
            }
            this.close();
            return;
          }
          const frameBuffer = this.readBuffer.subarray(0, nulIndex);
          this.readBuffer = this.readBuffer.subarray(nulIndex + 1);

          let msg;
          try {
            msg = JSON.parse(frameBuffer.toString('utf8'));
          } catch (e) {
            continue;
          }

          this._handleIncomingMessage(msg);
        } else {
          if (this.readBuffer.length > maxAllowed) {
            const err = new Error(`Buffer size ${this.readBuffer.length} exceeds max_frame_bytes ${maxAllowed}`);
            err.code = 5;
            if (!this.handshakeResolved) {
              this.handshakeReject(err);
            }
            this.close();
            return;
          }
          break;
        }
      }
    });

    this.child.on('error', (err) => {
      if (!this.handshakeResolved) {
        this.handshakeReject(err);
      }
    });

    this.child.on('exit', () => {
      if (!this.handshakeResolved) {
        const err = new Error('Child process exited before handshake completed');
        err.code = 5;
        this.handshakeReject(err);
      }
    });

    // Send Target.setAutoAttach handshake request
    const setAutoAttachReq = {
      id: this.nextId++,
      method: 'Target.setAutoAttach',
      params: {
        autoAttach: true,
        waitForDebuggerOnStart: false,
        flatten: true
      }
    };

    inPipe.write(JSON.stringify(setAutoAttachReq) + '\0');

    await this.handshakePromise;
    clearTimeout(handshakeTimeout);
  }

  _handleIncomingMessage(msg) {
    if (msg.method === 'Target.attachedToTarget') {
      const ti = msg.params?.targetInfo;
      if (ti && ti.type === 'page' && ti.url === this.fakeUrl) {
        this.sessionId = msg.params.sessionId;
        if (!this.handshakeResolved) {
          this.handshakeResolved = true;
          this.handshakeResolve(this.sessionId);
        }
      }
      return;
    }

    if (msg.id !== undefined && this.pending.has(msg.id)) {
      const { resolve, reject, timeoutTimer } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      clearTimeout(timeoutTimer);
      if (msg.error) {
        const err = new Error(msg.error.message || 'CDP Error');
        err.code = msg.error.code || 5;
        reject(err);
      } else {
        resolve(msg.result);
      }
    }
  }

  call(method, params = null) {
    if (this.closed) {
      const err = new Error('Client is closed');
      err.code = 5;
      throw err;
    }

    // Local allow-list refusal
    if (method === 'Page.navigate') {
      const err = new Error('Page.navigate is refused locally');
      err.code = 5;
      throw err;
    }

    const browserMethods = this.allowlist.browser_methods || {};
    const sessionMethods = this.allowlist.session_methods || {};

    let isAllowed = false;
    if (method in browserMethods || method in sessionMethods) {
      isAllowed = true;
    }

    if (!isAllowed) {
      const err = new Error(`Method ${method} is not on allow-list`);
      err.code = 5;
      throw err;
    }

    const id = this.nextId++;
    const req = { id, method };
    if (params !== null && params !== undefined) {
      req.params = params;
    }
    if (this.sessionId && method in sessionMethods) {
      req.sessionId = this.sessionId;
    }

    return new Promise((resolve, reject) => {
      const timeoutTimer = setTimeout(() => {
        this.pending.delete(id);
        const err = new Error(`Request ${method} timed out`);
        err.code = 5;
        reject(err);
      }, 10000);

      this.pending.set(id, { resolve, reject, timeoutTimer });

      try {
        const inPipe = this.child.stdio[3];
        inPipe.write(JSON.stringify(req) + '\0');
      } catch (e) {
        this.pending.delete(id);
        clearTimeout(timeoutTimer);
        const err = new Error(`Failed to write to pipe: ${e.message}`);
        err.code = 5;
        reject(err);
      }
    });
  }

  close() {
    if (this.closed) return;
    this.closed = true;

    for (const [id, { reject, timeoutTimer }] of this.pending.entries()) {
      clearTimeout(timeoutTimer);
      const err = new Error('Client closed');
      err.code = 5;
      reject(err);
    }
    this.pending.clear();

    if (this.child) {
      try {
        this.child.kill('SIGKILL');
      } catch (e) {}
    }
  }
}

export function parseSnapshot(snapshot) {
  if (!snapshot || !snapshot.documents || !snapshot.documents.length) {
    return { nodes: [], scrollOffsetX: 0, scrollOffsetY: 0 };
  }

  const doc = snapshot.documents[0];
  const strings = snapshot.strings || [];
  const scrollOffsetX = doc.scrollOffsetX || 0;
  const scrollOffsetY = doc.scrollOffsetY || 0;

  const rawNodes = doc.nodes || {};
  const attributesList = rawNodes.attributes || [];
  const backendNodeIds = rawNodes.backendNodeId || [];
  const layout = doc.layout || {};
  const boundsList = layout.bounds || [];
  const nodeIndices = layout.nodeIndex || [];
  const stylesList = layout.styles || [];

  const layoutMap = new Map();
  for (let i = 0; i < nodeIndices.length; i++) {
    const nodeIdx = nodeIndices[i];
    const bounds = boundsList[i] || [0, 0, 0, 0];
    const styles = stylesList[i] || [];
    layoutMap.set(nodeIdx, { bounds, styles });
  }

  const parsedNodes = [];
  const totalNodes = backendNodeIds.length;

  for (let i = 0; i < totalNodes; i++) {
    const backendNodeId = backendNodeIds[i];
    const rawAttrs = attributesList[i] || [];
    const attributes = {};
    for (let a = 0; a < rawAttrs.length; a += 2) {
      const kIdx = rawAttrs[a];
      const vIdx = rawAttrs[a + 1];
      if (kIdx !== undefined && vIdx !== undefined) {
        const k = strings[kIdx];
        const v = strings[vIdx];
        if (k !== undefined && v !== undefined) {
          attributes[k.toLowerCase()] = v;
        }
      }
    }

    const layoutData = layoutMap.get(i);
    let isVisible = false;
    let bounds = [0, 0, 0, 0];
    let computedStyles = { visibility: '', opacity: '', display: '' };

    if (layoutData) {
      bounds = layoutData.bounds;
      const styleIndices = layoutData.styles;
      const vis = strings[styleIndices[0]] || '';
      const op = strings[styleIndices[1]] || '';
      const disp = strings[styleIndices[2]] || '';
      computedStyles = { visibility: vis, opacity: op, display: disp };

      const visOk = vis === 'visible';
      const opOk = op !== '0' && parseFloat(op) !== 0;
      const dispOk = disp !== 'none';
      const rectOk = bounds[2] > 0 && bounds[3] > 0;

      isVisible = visOk && opOk && dispOk && rectOk;
    }

    parsedNodes.push({
      index: i,
      backendNodeId,
      attributes,
      bounds,
      computedStyles,
      isVisible
    });
  }

  return {
    nodes: parsedNodes,
    scrollOffsetX,
    scrollOffsetY
  };
}

export function findNodeById(snapshot, targetId) {
  const parsed = parseSnapshot(snapshot);
  const targetIdLower = String(targetId).toLowerCase();
  for (const node of parsed.nodes) {
    if (node.attributes.id && node.attributes.id.toLowerCase() === targetIdLower) {
      return { node, scrollOffsetX: parsed.scrollOffsetX, scrollOffsetY: parsed.scrollOffsetY };
    }
  }
  return null;
}

export function findNodeByRole(axTree, snapshot, role) {
  if (!axTree || !axTree.nodes) return null;
  const targetRoleLower = String(role).toLowerCase();

  let targetAXNode = null;
  for (const axNode of axTree.nodes) {
    const rVal = typeof axNode.role === 'object' ? axNode.role?.value : axNode.role;
    if (rVal && String(rVal).toLowerCase() === targetRoleLower) {
      targetAXNode = axNode;
      break;
    }
  }

  if (!targetAXNode) return null;

  const parsed = parseSnapshot(snapshot);
  const targetBackendId = targetAXNode.backendDOMNodeId ?? Number(targetAXNode.nodeId);

  for (const node of parsed.nodes) {
    if (node.backendNodeId === targetBackendId) {
      return { node, axNode: targetAXNode, scrollOffsetX: parsed.scrollOffsetX, scrollOffsetY: parsed.scrollOffsetY };
    }
  }
  return null;
}

export function findFocusedNode(axTree, snapshot) {
  if (!axTree || !axTree.nodes) return null;

  let focusedAXNode = null;
  for (const axNode of axTree.nodes) {
    const props = axNode.properties || [];
    for (const p of props) {
      if (p.name === 'focused') {
        const val = typeof p.value === 'object' ? p.value?.value : p.value;
        if (val === true) {
          focusedAXNode = axNode;
          break;
        }
      }
    }
    if (focusedAXNode) break;
  }

  if (!focusedAXNode) return null;

  const parsed = parseSnapshot(snapshot);
  const targetBackendId = focusedAXNode.backendDOMNodeId ?? Number(focusedAXNode.nodeId);

  for (const node of parsed.nodes) {
    if (node.backendNodeId === targetBackendId) {
      return { node, axNode: focusedAXNode, scrollOffsetX: parsed.scrollOffsetX, scrollOffsetY: parsed.scrollOffsetY };
    }
  }

  return { node: null, axNode: focusedAXNode, scrollOffsetX: parsed.scrollOffsetX, scrollOffsetY: parsed.scrollOffsetY };
}

function checkSensitive(node, axNode) {
  // Check type attribute
  if (node && node.attributes) {
    const t = node.attributes.type ? node.attributes.type.toLowerCase() : '';
    if (t === 'password' || t === 'file') {
      return true;
    }
  }

  // Sensitive patterns for id, name, AX name
  // password, passwd, secret, token, cc-, card followed by optional char and num, cvv or cvc
  const sensitiveRegex = /password|passwd|secret|token|cc[-_]|card|cvv|cvc/i;

  if (node && node.attributes) {
    for (const key of ['id', 'name']) {
      const val = node.attributes[key];
      if (val && sensitiveRegex.test(val)) {
        return true;
      }
    }
  }

  if (axNode) {
    const axName = typeof axNode.name === 'object' ? axNode.name?.value : axNode.name;
    if (axName && sensitiveRegex.test(String(axName))) {
      return true;
    }
  }

  return false;
}

export async function plan_click(provider, targetSpec, retries = 3) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    const { axTree, snapshot } = await provider();

    // Find focused node to check for refusal
    const focusedResult = findFocusedNode(axTree, snapshot);
    if (focusedResult && checkSensitive(focusedResult.node, focusedResult.axNode)) {
      return { kind: 'abort', reason: 'refused' };
    }

    // Locate target node based on targetSpec
    let found = null;
    if (typeof targetSpec === 'string' || targetSpec.id) {
      const targetId = typeof targetSpec === 'string' ? targetSpec : targetSpec.id;
      found = findNodeById(snapshot, targetId);
    } else if (targetSpec.role) {
      found = findNodeByRole(axTree, snapshot, targetSpec.role);
    } else if (targetSpec.focused) {
      found = focusedResult;
    }

    if (found && checkSensitive(found.node, found.axNode)) {
      return { kind: 'abort', reason: 'refused' };
    }

    if (!found || !found.node) {
      if (attempt < retries) continue;
      return { kind: 'abort', reason: 'not_found' };
    }

    if (!found.node.isVisible) {
      if (attempt < retries) continue;
      return { kind: 'abort', reason: 'not_visible' };
    }

    // Node exists and is visible! Calculate coordinates.
    const [x, y, w, h] = found.node.bounds;
    const cx = x + Math.floor(w / 2);
    const cy = y + Math.floor(h / 2);
    const vx = cx - found.scrollOffsetX;
    const vy = cy - found.scrollOffsetY;

    return {
      kind: 'click',
      xy: [cx, cy],
      viewport: [vx, vy],
      viewport_xy: [vx, vy],
      attempts: attempt
    };
  }

  return { kind: 'abort', reason: 'not_found' };
}
