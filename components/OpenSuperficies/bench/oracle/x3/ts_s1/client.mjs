// 4a6420ff47df67f2415776072b5837fc6cc83be6215fa5567201c1a2460e23c2
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as child_process from 'node:child_process';

export function loadAllowlist(allowlistPath) {
  const p = allowlistPath || path.resolve(process.cwd(), 'docker/cu/oracle/allowlist.json');
  const content = fs.readFileSync(p, 'utf-8');
  return JSON.parse(content);
}

export class CDPClientError extends Error {
  constructor(message, code = 5) {
    super(message);
    this.name = 'CDPClientError';
    this.code = code;
  }
}

export class CDPClient {
  constructor(options = {}) {
    this.child = null;
    this.pipeRead = null;
    this.pipeWrite = null;
    this.defaultOptions = options;
    this.allowlist = loadAllowlist(options.allowlistPath);
    this.nextId = 1;
    this.sessionId = null;
    this.pendingRequests = new Map();
    this.buffer = Buffer.alloc(0);
    this.maxFrameBytes = options.maxFrameBytesOverride ?? this.allowlist.max_frame_bytes;
    this.timeoutMs = options.timeoutMs ?? 10000;
    this.isClosed = false;
  }

  getSessionId() {
    return this.sessionId;
  }

  async start(options = {}) {
    const opts = { ...this.defaultOptions, ...options };
    const pythonPath = opts.pythonPath || 'python3';
    const scriptPath = opts.scriptPath || path.resolve(process.cwd(), 'docker/cu/tests/fixtures/cdp/fake_chromium.py');
    const fakeMode = opts.fakeMode || process.env.FAKE_MODE || 'echo';
    const fakeUrl = opts.fakeUrl || process.env.FAKE_URL || 'file:///opt/cu/scenes/v3/ff_form_01.html';
    const fakeLog = opts.fakeLog || process.env.FAKE_LOG || '';
    const fakeBytes = opts.fakeBytes ?? process.env.FAKE_BYTES;

    const env = {
      ...process.env,
      FAKE_MODE: fakeMode,
      FAKE_URL: fakeUrl,
    };
    if (fakeLog) env.FAKE_LOG = fakeLog;
    if (fakeBytes !== undefined) env.FAKE_BYTES = String(fakeBytes);

    this.child = child_process.spawn(pythonPath, ['-B', scriptPath], {
      env,
      stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
    });

    this.child.on('exit', () => {
      this.handleError(new CDPClientError('Child process exited', 5));
    });

    const parentWriter = this.child.stdio[3];
    const parentReader = this.child.stdio[4];

    this.pipeWrite = parentWriter;
    this.pipeRead = parentReader;

    parentReader.on('data', (chunk) => this.onData(chunk));
    parentReader.on('error', (err) => this.handleError(err));
    this.child.on('error', (err) => this.handleError(err));

    if (fakeMode === 'oversize') {
      return new Promise((_, reject) => {
        const timer = setTimeout(() => {
          if (this.buffer.length > this.maxFrameBytes) {
            reject(new CDPClientError(`Frame byte size ${this.buffer.length} exceeds max_frame_bytes ${this.maxFrameBytes}`, 5));
          } else {
            reject(new CDPClientError('Oversize mode read completed', 5));
          }
        }, 100);
        timer.unref();
      });
    }

    const responsePromise = this.sendRaw('Target.setAutoAttach', {
      autoAttach: true,
      waitForDebuggerOnStart: false,
      flatten: true,
    });

    const handshakeTimeout = new Promise((_, reject) => {
      const timer = setTimeout(() => {
        reject(new CDPClientError('Handshake timed out waiting for Target.attachedToTarget', 5));
      }, this.timeoutMs);
      timer.unref();
    });

    const sessionPromise = new Promise((resolve) => {
      const checkInterval = setInterval(() => {
        if (this.sessionId) {
          clearInterval(checkInterval);
          resolve(this.sessionId);
        }
      }, 10);
    });

    await responsePromise;
    const boundSessionId = await Promise.race([sessionPromise, handshakeTimeout]);
    return boundSessionId;
  }

  onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);

    while (true) {
      const nulIdx = this.buffer.indexOf(0);
      if (nulIdx !== -1) {
        if (nulIdx > this.maxFrameBytes) {
          this.handleError(new CDPClientError(`Frame byte size ${nulIdx} exceeds max_frame_bytes ${this.maxFrameBytes}`, 5));
          return;
        }
        const frameBuf = this.buffer.subarray(0, nulIdx);
        this.buffer = this.buffer.subarray(nulIdx + 1);
        this.processFrame(frameBuf);
      } else {
        if (this.buffer.length > this.maxFrameBytes) {
          this.handleError(new CDPClientError(`Frame byte size ${this.buffer.length} exceeds max_frame_bytes ${this.maxFrameBytes}`, 5));
          return;
        }
        break;
      }
    }
  }

  processFrame(buf) {
    let msg;
    try {
      msg = JSON.parse(buf.toString('utf-8'));
    } catch (e) {
      return;
    }

    if (!msg || typeof msg !== 'object') return;

    if (msg.method === 'Target.attachedToTarget' && msg.params && msg.params.targetInfo) {
      const targetInfo = msg.params.targetInfo;
      const expectedUrl = process.env.FAKE_URL || 'file:///opt/cu/scenes/v3/ff_form_01.html';
      if (targetInfo.type === 'page' && targetInfo.url === expectedUrl) {
        if (!this.sessionId) {
          this.sessionId = msg.params.sessionId;
        }
      }
      return;
    }

    if (typeof msg.id === 'number' && this.pendingRequests.has(msg.id)) {
      const req = this.pendingRequests.get(msg.id);
      this.pendingRequests.delete(msg.id);
      clearTimeout(req.timer);
      if (msg.error) {
        req.reject(new CDPClientError(msg.error.message || 'CDP Error', 5));
      } else {
        req.resolve(msg.result);
      }
    }
  }

  isMethodAllowedLocally(method) {
    if (method === 'Page.navigate') return false;
    const inBrowser = Object.prototype.hasOwnProperty.call(this.allowlist.browser_methods, method);
    const inSession = Object.prototype.hasOwnProperty.call(this.allowlist.session_methods, method);
    return inBrowser || inSession;
  }

  send(method, params) {
    if (!this.isMethodAllowedLocally(method)) {
      return Promise.reject(new CDPClientError(`Method '${method}' is refused by local allowlist`, 5));
    }
    return this.sendRaw(method, params, this.sessionId ?? undefined);
  }

  sendRaw(method, params, sessionId) {
    if (!this.isMethodAllowedLocally(method)) {
      return Promise.reject(new CDPClientError(`Method '${method}' is refused by local allowlist`, 5));
    }

    const id = this.nextId++;
    const reqObj = { id, method };
    if (params !== undefined) reqObj.params = params;
    if (sessionId !== undefined) reqObj.sessionId = sessionId;

    const payload = Buffer.from(JSON.stringify(reqObj) + '\0', 'utf-8');

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingRequests.delete(id);
        reject(new CDPClientError(`Request ${method} (id=${id}) timed out after ${this.timeoutMs}ms`, 5));
      }, this.timeoutMs);
      timer.unref();

      this.pendingRequests.set(id, { resolve, reject, timer });

      if (this.pipeWrite && !this.pipeWrite.destroyed) {
        this.pipeWrite.write(payload, (err) => {
          if (err) {
            this.pendingRequests.delete(id);
            clearTimeout(timer);
            reject(new CDPClientError(`Failed to write to pipe: ${err.message}`, 5));
          }
        });
      } else {
        this.pendingRequests.delete(id);
        clearTimeout(timer);
        reject(new CDPClientError('Pipe is not writable', 5));
      }
    });
  }

  handleError(err) {
    for (const [id, req] of this.pendingRequests.entries()) {
      clearTimeout(req.timer);
      req.reject(err);
    }
    this.pendingRequests.clear();
    this.close();
  }

  close() {
    if (this.isClosed) return;
    this.isClosed = true;

    if (this.pipeWrite) {
      try { this.pipeWrite.end(); } catch (_) {}
    }
    if (this.pipeRead) {
      try { this.pipeRead.destroy(); } catch (_) {}
    }

    if (this.child) {
      try {
        this.child.kill('SIGKILL');
      } catch (_) {}
      this.child = null;
    }
  }
}

export function parseAXTree(axData) {
  return { nodes: axData.nodes || [] };
}

export function findAXFocusedNode(axData) {
  const nodes = axData.nodes || [];
  for (const node of nodes) {
    if (Array.isArray(node.properties)) {
      for (const prop of node.properties) {
        if (prop.name === 'focused') {
          const val = prop.value;
          if (val && typeof val === 'object' && val.value === true) {
            return node;
          }
          if (val === true) {
            return node;
          }
        }
      }
    }
  }
  return null;
}

export function findAXNodeByRole(axData, role) {
  const nodes = axData.nodes || [];
  for (const node of nodes) {
    const nodeRole = typeof node.role === 'object' ? node.role?.value : node.role;
    if (nodeRole === role) {
      return node;
    }
  }
  return null;
}

export function parseDOMSnapshot(snapshotData) {
  const parsedNodes = [];
  if (!snapshotData || !Array.isArray(snapshotData.documents)) {
    return parsedNodes;
  }

  const topStrings = snapshotData.strings || [];

  for (const doc of snapshotData.documents) {
    const strings = doc.strings || topStrings;
    const nodes = doc.nodes || {};
    const layout = doc.layout || {};
    const backendNodeIds = nodes.backendNodeId || [];
    const attributesList = nodes.attributes || [];
    const scrollOffsetX = doc.scrollOffsetX ?? 0;
    const scrollOffsetY = doc.scrollOffsetY ?? 0;

    const layoutNodeIndices = layout.nodeIndex || [];
    const layoutBounds = layout.bounds || [];
    const layoutStyles = layout.styles || [];

    const layoutMap = new Map();
    for (let i = 0; i < layoutNodeIndices.length; i++) {
      const nIdx = layoutNodeIndices[i];
      const bounds = layoutBounds[i];
      const styles = layoutStyles[i] || [];
      if (bounds) {
        layoutMap.set(nIdx, { bounds, styles });
      }
    }

    const computedStyleNames = layout.computedStyles || ['visibility', 'opacity', 'display'];

    for (let i = 0; i < backendNodeIds.length; i++) {
      const bNodeId = backendNodeIds[i];
      const rawAttrs = attributesList[i] || [];
      const attrMap = {};

      for (let j = 0; j < rawAttrs.length; j += 2) {
        const keyIdx = rawAttrs[j];
        const valIdx = rawAttrs[j + 1];
        if (keyIdx !== undefined && valIdx !== undefined) {
          const key = strings[keyIdx] ?? '';
          const val = strings[valIdx] ?? '';
          attrMap[key] = val;
        }
      }

      const layoutItem = layoutMap.get(i);
      let bounds = undefined;
      let computedStyle = undefined;

      if (layoutItem) {
        bounds = [
          Math.floor(layoutItem.bounds[0]),
          Math.floor(layoutItem.bounds[1]),
          Math.floor(layoutItem.bounds[2]),
          Math.floor(layoutItem.bounds[3]),
        ];

        const styleIndices = layoutItem.styles;
        computedStyle = {};
        for (let sPos = 0; sPos < styleIndices.length; sPos++) {
          const sIdx = styleIndices[sPos];
          const strVal = strings[sIdx];
          const styleName = computedStyleNames[sPos];
          if (strVal !== undefined) {
            if (styleName) {
              computedStyle[styleName] = strVal;
            } else {
              if (['visible', 'hidden', 'collapse'].includes(strVal)) {
                computedStyle.visibility = strVal;
              } else if (['block', 'inline', 'none', 'flex', 'grid'].includes(strVal)) {
                computedStyle.display = strVal;
              } else if (!isNaN(Number(strVal))) {
                computedStyle.opacity = strVal;
              }
            }
          }
        }
      }

      parsedNodes.push({
        backendNodeId: bNodeId,
        attributes: attrMap,
        bounds,
        computedStyle,
        scrollOffsetX,
        scrollOffsetY,
      });
    }
  }

  return parsedNodes;
}

export function isNodeVisible(node) {
  if (!node.bounds) return false;
  const [x, y, w, h] = node.bounds;
  if (w <= 0 || h <= 0) return false;

  const styles = node.computedStyle;
  if (!styles) return false;

  if (styles.visibility && styles.visibility !== 'visible') return false;
  if (styles.opacity !== undefined && (styles.opacity === '0' || Number(styles.opacity) === 0)) return false;
  if (styles.display && styles.display === 'none') return false;

  return true;
}

export function findNodeById(snapshotData, targetId) {
  const nodes = parseDOMSnapshot(snapshotData);
  for (const node of nodes) {
    if (node.attributes.id === targetId) {
      return node;
    }
  }
  return null;
}

const SENSITIVE_PATTERNS = [
  'password',
  'passwd',
  'secret',
  'token',
  'cvv',
  'cvc',
];

function isNameSensitive(val) {
  if (!val) return false;
  const lower = val.toLowerCase();
  for (const pattern of SENSITIVE_PATTERNS) {
    if (lower.includes(pattern)) return true;
  }
  if (/\b(cc-?|card)[a-z0-9_-]*/i.test(lower) || /card[_\-]?\d*/i.test(lower) || /card[_\-]?[a-z0-9]+/i.test(lower)) {
    return true;
  }
  return false;
}

export function isSensitiveNode(node, axNode) {
  const typeAttr = (node.attributes.type || '').toLowerCase();
  if (typeAttr === 'password' || typeAttr === 'file') {
    return true;
  }

  const idAttr = node.attributes.id || '';
  const nameAttr = node.attributes.name || '';
  if (isNameSensitive(idAttr) || isNameSensitive(nameAttr)) {
    return true;
  }

  if (axNode) {
    const axName = typeof axNode.name === 'object' ? axNode.name?.value : axNode.name;
    if (axName && isNameSensitive(String(axName))) {
      return true;
    }
  }

  return false;
}

export async function plan_click(selector, snapshotProvider, retryCount = 3) {
  let attempt = 0;
  let lastSeenNode = null;
  let lastSeenAXNode = null;

  while (attempt < retryCount) {
    attempt++;
    const { axData, snapshotData } = await snapshotProvider();

    const focusedAXNode = findAXFocusedNode(axData);
    if (focusedAXNode) {
      const domNodes = parseDOMSnapshot(snapshotData);
      const focusedDomNode = domNodes.find((n) => String(n.backendNodeId) === focusedAXNode.nodeId);
      if (focusedDomNode && isSensitiveNode(focusedDomNode, focusedAXNode)) {
        return {
          kind: 'abort',
          reason: 'refused',
        };
      }
    }

    let targetAXNode = null;
    if (selector.focused) {
      targetAXNode = focusedAXNode;
    } else if (selector.role) {
      targetAXNode = findAXNodeByRole(axData, selector.role);
    }

    let targetNode = null;
    if (selector.id) {
      targetNode = findNodeById(snapshotData, selector.id);
    } else if (targetAXNode) {
      const nodes = parseDOMSnapshot(snapshotData);
      const backendIdStr = targetAXNode.nodeId;
      targetNode = nodes.find((n) => String(n.backendNodeId) === backendIdStr) || null;
    }

    if (targetNode) {
      lastSeenNode = targetNode;
      lastSeenAXNode = targetAXNode;

      if (isSensitiveNode(targetNode, targetAXNode)) {
        return {
          kind: 'abort',
          reason: 'refused',
        };
      }

      if (isNodeVisible(targetNode)) {
        const [x, y, w, h] = targetNode.bounds;
        const cx = x + Math.floor(w / 2);
        const cy = y + Math.floor(h / 2);
        const vcx = cx - targetNode.scrollOffsetX;
        const vcy = cy - targetNode.scrollOffsetY;

        return {
          kind: 'click',
          xy: [cx, cy],
          viewport_xy: [vcx, vcy],
          attempts: attempt,
        };
      }
    }
  }

  if (lastSeenNode && isSensitiveNode(lastSeenNode, lastSeenAXNode)) {
    return {
      kind: 'abort',
      reason: 'refused',
    };
  }

  if (!lastSeenNode) {
    return {
      kind: 'abort',
      reason: 'not_found',
    };
  }

  return {
    kind: 'abort',
    reason: 'not_visible',
  };
}
