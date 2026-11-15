import fs from 'node:fs';
import child_process from 'node:child_process';

// Load allowlist
export function loadAllowlist(allowlistPath) {
  const content = fs.readFileSync(allowlistPath, 'utf8');
  return JSON.parse(content);
}

const DEFAULT_ALLOWLIST_PATH = 'docker/cu/oracle/allowlist.json';

export class CdpClient {
  constructor(options = {}) {
    this.allowlistPath = options.allowlistPath || DEFAULT_ALLOWLIST_PATH;
    this.allowlist = options.allowlist || loadAllowlist(this.allowlistPath);
    this.maxFrameBytes = options.maxFrameBytes || this.allowlist.max_frame_bytes || 104857600;
    this.fakeUrl = options.fakeUrl || process.env.FAKE_URL || 'file:///opt/cu/scenes/v3/ff_form_01.html';
    this.fakeMode = options.fakeMode || process.env.FAKE_MODE || 'echo';
    this.fakeLog = options.fakeLog || process.env.FAKE_LOG || null;
    this.fakeBytes = options.fakeBytes || process.env.FAKE_BYTES || null;
    this.pythonPath = options.pythonPath || 'python3';
    this.scriptPath = options.scriptPath || 'docker/cu/tests/fixtures/cdp/fake_chromium.py';

    this.child = null;
    this.nextId = 1;
    this.sessionId = null;
    this.pendingRequests = new Map();
    this.buffer = Buffer.alloc(0);
    this.closed = false;
  }

  start() {
    const env = {
      ...process.env,
      FAKE_MODE: this.fakeMode,
      FAKE_URL: this.fakeUrl,
    };
    if (this.fakeLog) {
      env.FAKE_LOG = this.fakeLog;
    }
    if (this.fakeBytes !== null) {
      env.FAKE_BYTES = String(this.fakeBytes);
    }

    // stdio: [stdin, stdout, stderr, pipe(fd3 write), pipe(fd4 read)]
    this.child = child_process.spawn(this.pythonPath, ['-B', this.scriptPath], {
      stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
      env,
    });

    this.writeStream = this.child.stdio[3];
    this.readStream = this.child.stdio[4];

    this._dataHandler = (chunk) => this._onData(chunk);
    this._errorHandler = () => {};

    this.readStream.on('data', this._dataHandler);
    this.readStream.on('error', this._errorHandler);
    if (this.writeStream) {
      this.writeStream.on('error', this._errorHandler);
    }

    this.child.on('exit', () => {
      this.closed = true;
    });
  }

  _onData(chunk) {
    if (this.closed) return;
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (true) {
      const idx = this.buffer.indexOf(0);
      if (idx !== -1) {
        if (idx > this.maxFrameBytes) {
          this._handleOversize();
          return;
        }
        const frameBuf = this.buffer.subarray(0, idx);
        this.buffer = this.buffer.subarray(idx + 1);
        this._handleFrame(frameBuf.toString('utf8'));
      } else {
        if (this.buffer.length > this.maxFrameBytes) {
          this._handleOversize();
          return;
        }
        break;
      }
    }
  }

  _handleOversize() {
    this.closed = true;
    // Reject all pending requests with error code 5
    for (const [id, { reject }] of this.pendingRequests.entries()) {
      const err = new Error('Oversize frame');
      err.code = 5;
      reject(err);
    }
    this.pendingRequests.clear();
    this.close();
  }

  _handleFrame(frameStr) {
    let msg;
    try {
      msg = JSON.parse(frameStr);
    } catch (e) {
      return;
    }

    if (msg.id !== undefined && this.pendingRequests.has(msg.id)) {
      const { resolve } = this.pendingRequests.get(msg.id);
      this.pendingRequests.delete(msg.id);
      resolve(msg.result !== undefined ? msg.result : msg);
      return;
    }

    if (msg.method === 'Target.attachedToTarget') {
      const params = msg.params || {};
      const ti = params.targetInfo || {};
      if (ti.type === 'page' && ti.url === this.fakeUrl && !this.sessionId) {
        this.sessionId = params.sessionId;
        if (this.onBound) {
          this.onBound(this.sessionId);
        }
      }
    }
  }

  async handshake(timeoutMs = 10000) {
    if (!this.child) {
      this.start();
    }

    const boundPromise = new Promise((resolve) => {
      if (this.sessionId) {
        resolve(this.sessionId);
      } else {
        this.onBound = resolve;
      }
    });

    // Send Target.setAutoAttach
    await this.sendRequest('Target.setAutoAttach', {
      autoAttach: true,
      waitForDebuggerOnStart: false,
      flatten: true,
    }, timeoutMs, false);

    const timer = new Promise((_, reject) => {
      setTimeout(() => {
        const err = new Error('Handshake timeout');
        err.code = 5;
        reject(err);
      }, timeoutMs);
    });

    return Promise.race([boundPromise, timer]);
  }

  checkAllowlist(method, params, hasSession) {
    if (method === 'Page.navigate') {
      return false;
    }
    if (method.startsWith('Input.') || method.startsWith('Runtime.') || method.startsWith('Emulation.') || method.startsWith('DOM.')) {
      return false;
    }

    if (hasSession) {
      return Object.prototype.hasOwnProperty.call(this.allowlist.session_methods, method);
    } else {
      return Object.prototype.hasOwnProperty.call(this.allowlist.browser_methods, method);
    }
  }

  async sendRequest(method, params = null, timeoutMs = 10000, useSession = true) {
    const hasSession = useSession && Boolean(this.sessionId);
    if (!this.checkAllowlist(method, params, hasSession)) {
      const err = new Error(`Method ${method} refused by local allowlist`);
      err.code = 5;
      throw err;
    }

    const id = this.nextId++;
    const req = { id, method };
    if (params !== null && params !== undefined) {
      req.params = params;
    }
    if (hasSession) {
      req.sessionId = this.sessionId;
    }

    const payload = Buffer.from(JSON.stringify(req) + '\0', 'utf8');

    return new Promise((resolve, reject) => {
      if (this.closed) {
        const err = new Error('Client closed');
        err.code = 5;
        return reject(err);
      }

      const timer = setTimeout(() => {
        this.pendingRequests.delete(id);
        const err = new Error(`Request ${id} (${method}) timed out`);
        err.code = 5;
        reject(err);
      }, timeoutMs);

      this.pendingRequests.set(id, {
        resolve: (res) => {
          clearTimeout(timer);
          resolve(res);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });

      try {
        this.writeStream.write(payload);
      } catch (e) {
        this.pendingRequests.delete(id);
        clearTimeout(timer);
        const err = new Error('Failed to write request');
        err.code = 5;
        reject(err);
      }
    });
  }

  close() {
    this.closed = true;
    if (this.readStream) {
      this.readStream.removeAllListeners();
    }
    if (this.writeStream) {
      this.writeStream.removeAllListeners();
      try {
        this.writeStream.destroy();
      } catch (e) {}
    }
    if (this.child) {
      try {
        this.child.kill('SIGKILL');
      } catch (e) {}
      this.child = null;
    }
  }
}

// Pure Query Functions
export function parseAXTree(axData) {
  if (!axData || !Array.isArray(axData.nodes)) {
    return [];
  }
  return axData.nodes;
}

export function findAXFocusedNode(nodes) {
  if (!Array.isArray(nodes)) return null;
  for (const node of nodes) {
    if (Array.isArray(node.properties)) {
      for (const prop of node.properties) {
        if (prop.name === 'focused' && prop.value && prop.value.value === true) {
          return {
            nodeId: node.nodeId,
            role: node.role ? node.role.value : null,
            raw: node,
          };
        }
      }
    }
  }
  return null;
}

export function findAXNodeByRole(nodes, role) {
  if (!Array.isArray(nodes)) return null;
  for (const node of nodes) {
    if (node.role && node.role.value === role) {
      return {
        nodeId: node.nodeId,
        role: node.role.value,
        raw: node,
      };
    }
  }
  return null;
}

export function parseDOMSnapshot(snapshotData) {
  if (!snapshotData || !Array.isArray(snapshotData.documents) || snapshotData.documents.length === 0) {
    return null;
  }
  const doc = snapshotData.documents[0];
  const strings = snapshotData.strings || [];
  const nodes = doc.nodes || {};
  const layout = doc.layout || {};
  const scrollOffsetX = doc.scrollOffsetX || 0;
  const scrollOffsetY = doc.scrollOffsetY || 0;

  const domNodes = [];
  const backendMap = new Map();
  const idMap = new Map();

  const attributesArr = nodes.attributes || [];
  const backendNodeIdArr = nodes.backendNodeId || [];

  for (let i = 0; i < backendNodeIdArr.length; i++) {
    const backendId = backendNodeIdArr[i];
    const rawAttrs = attributesArr[i] || [];
    const attrs = {};
    for (let a = 0; a < rawAttrs.length; a += 2) {
      const name = strings[rawAttrs[a]];
      const val = strings[rawAttrs[a + 1]];
      if (name !== undefined) {
        attrs[name] = val;
      }
    }

    const nodeObj = {
      domIndex: i,
      backendNodeId: backendId,
      attributes: attrs,
      layout: null,
      visible: false,
    };

    domNodes.push(nodeObj);
    backendMap.set(backendId, nodeObj);
    if (attrs.id) {
      idMap.set(attrs.id, nodeObj);
    }
  }

  // Process layout
  const layoutNodeIndex = layout.nodeIndex || [];
  const boundsArr = layout.bounds || [];
  const stylesArr = layout.styles || [];

  for (let l = 0; l < layoutNodeIndex.length; l++) {
    const domIdx = layoutNodeIndex[l];
    const bounds = boundsArr[l] || [0, 0, 0, 0];
    const styleIdxs = stylesArr[l] || [];

    // computedStyles requested: ["visibility", "opacity", "display"]
    const visibility = strings[styleIdxs[0]] || '';
    const opacity = strings[styleIdxs[1]] || '';
    const display = strings[styleIdxs[2]] || '';

    const [x, y, w, h] = bounds;
    const isVisible = visibility === 'visible' && opacity !== '0' && display !== 'none' && w > 0 && h > 0;

    const layoutObj = {
      bounds: [x, y, w, h],
      visibility,
      opacity,
      display,
      x,
      y,
      w,
      h,
      cx: x + Math.floor(w / 2),
      cy: y + Math.floor(h / 2),
      viewportCx: (x + Math.floor(w / 2)) - scrollOffsetX,
      viewportCy: (y + Math.floor(h / 2)) - scrollOffsetY,
    };

    if (domNodes[domIdx]) {
      domNodes[domIdx].layout = layoutObj;
      domNodes[domIdx].visible = isVisible;
    }
  }

  return {
    nodes: domNodes,
    backendMap,
    idMap,
    scrollOffsetX,
    scrollOffsetY,
  };
}

// Sensitive Field Refusal Check
export function isSensitive(typeVal, idVal, nameVal, axNameVal) {
  if (typeVal) {
    const t = String(typeVal).toLowerCase();
    if (t === 'password' || t === 'file') return true;
  }

  const checkStr = (val) => {
    if (!val) return false;
    const v = String(val).toLowerCase();
    if (v.includes('password') || v.includes('passwd') || v.includes('secret') || v.includes('token')) return true;
    if (v.includes('cc-')) return true;
    if (v.includes('cvv') || v.includes('cvc')) return true;
    if (v.includes('card')) return true;
    return false;
  };

  return checkStr(idVal) || checkStr(nameVal) || checkStr(axNameVal);
}

// Action Planning
export async function plan_click(query, snapshotProvider, retryCount = 3) {
  let lastFoundNode = null;

  for (let attempt = 1; attempt <= retryCount; attempt++) {
    const data = await snapshotProvider();
    if (!data) continue;

    const axTree = parseAXTree(data.axTree || data);
    const domParsed = parseDOMSnapshot(data.snapshot || data);

    const focusedAx = findAXFocusedNode(axTree);

    // Check refusal on focused AX node or target node
    if (focusedAx) {
      const axRaw = focusedAx.raw || {};
      const axName = axRaw.name ? axRaw.name.value : null;
      if (isSensitive(null, null, null, axName)) {
        return { kind: 'abort', reason: 'refused' };
      }
    }

    let targetNode = null;
    if (domParsed) {
      if (query.id) {
        targetNode = domParsed.idMap.get(query.id);
      } else if (query.backendNodeId) {
        targetNode = domParsed.backendMap.get(query.backendNodeId);
      } else if (query.role && axTree) {
        const axMatch = findAXNodeByRole(axTree, query.role);
        if (axMatch && axMatch.nodeId) {
          targetNode = domParsed.nodes.find(n => String(n.backendNodeId) === String(axMatch.nodeId));
        }
      }
    }

    if (targetNode) {
      lastFoundNode = targetNode;
      const attrs = targetNode.attributes || {};
      const axMatchForTarget = axTree ? axTree.find(n => String(n.nodeId) === String(targetNode.backendNodeId)) : null;
      const axName = axMatchForTarget && axMatchForTarget.name ? axMatchForTarget.name.value : null;

      if (isSensitive(attrs.type, attrs.id, attrs.name, axName)) {
        return { kind: 'abort', reason: 'refused' };
      }

      if (targetNode.visible && targetNode.layout) {
        return {
          kind: 'click',
          xy: [targetNode.layout.cx, targetNode.layout.cy],
          viewport_xy: [targetNode.layout.viewportCx, targetNode.layout.viewportCy],
          attempts: attempt,
        };
      }
    }
  }

  if (lastFoundNode) {
    return { kind: 'abort', reason: 'not_visible' };
  }
  return { kind: 'abort', reason: 'not_found' };
}
