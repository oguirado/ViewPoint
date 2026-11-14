import * as fs from 'node:fs';
import * as child_process from 'node:child_process';

export class CDPError extends Error {
  code: number;
  constructor(message: string, code = 5) {
    super(message);
    this.name = 'CDPError';
    this.code = code;
  }
}

export interface Allowlist {
  schema: string;
  max_frame_bytes: number;
  frame_keys: string[];
  browser_methods: Record<string, any>;
  session_methods: Record<string, any>;
  event_domains: string[];
  event_methods: string[];
  refused_explicit: string[];
}

export interface AXNode {
  nodeId: string;
  role?: { type: string; value: string } | string;
  backendDOMNodeId?: number;
  name?: { type?: string; value?: string } | string;
  properties?: Array<{ name: string; value: any }>;
  [key: string]: any;
}

export interface DOMSnapshotNode {
  nodeIndex: number;
  backendNodeId: number;
  nodeName: string;
  nodeType: number;
  attributes: Record<string, string>;
  bounds: [number, number, number, number];
  computedStyles: {
    visibility?: string;
    opacity?: string;
    display?: string;
  };
}

export interface DOMSnapshotDocument {
  scrollOffsetX: number;
  scrollOffsetY: number;
  nodes: DOMSnapshotNode[];
}

export interface ActionClick {
  kind: 'click';
  xy: [number, number];
  viewport_xy: [number, number];
  attempts: number;
}

export interface ActionAbort {
  kind: 'abort';
  reason: 'not_found' | 'not_visible' | 'refused';
  attempts?: number;
}

export type ActionPlan = ActionClick | ActionAbort;

export function loadAllowlist(filePathOrObject: string | Allowlist): Allowlist {
  if (typeof filePathOrObject === 'string') {
    const raw = fs.readFileSync(filePathOrObject, 'utf-8');
    return JSON.parse(raw);
  }
  return filePathOrObject;
}

export function validateMethod(method: string, allowlist: Allowlist, isSession = false): void {
  if (method === 'Page.navigate') {
    throw new CDPError('Page.navigate is disallowed by local policy', 5);
  }
  if (isSession) {
    if (!allowlist.session_methods || !allowlist.session_methods[method]) {
      throw new CDPError(`Session method ${method} not in allowlist`, 5);
    }
  } else {
    if (!allowlist.browser_methods || !allowlist.browser_methods[method]) {
      throw new CDPError(`Browser method ${method} not in allowlist`, 5);
    }
  }
}

export class Framer {
  private buffer = Buffer.alloc(0);
  private maxFrameBytes: number;

  constructor(maxFrameBytes = 104857600) {
    this.maxFrameBytes = maxFrameBytes;
  }

  feed(data: Buffer): Buffer[] {
    this.buffer = Buffer.concat([this.buffer, data]);
    const frames: Buffer[] = [];

    while (true) {
      const nulIdx = this.buffer.indexOf(0);
      if (nulIdx !== -1) {
        if (nulIdx > this.maxFrameBytes) {
          throw new CDPError(`Frame size ${nulIdx} exceeds max frame limit ${this.maxFrameBytes}`, 5);
        }
        const frame = this.buffer.subarray(0, nulIdx);
        this.buffer = this.buffer.subarray(nulIdx + 1);
        frames.push(frame);
      } else {
        if (this.buffer.length > this.maxFrameBytes) {
          throw new CDPError(`Buffer length ${this.buffer.length} exceeds max frame limit ${this.maxFrameBytes}`, 5);
        }
        break;
      }
    }

    return frames;
  }
}

export class CDPClient {
  private allowlist: Allowlist;
  private child: child_process.ChildProcess | null = null;
  private nextId = 1;
  private sessionId: string | null = null;
  private pendingRequests = new Map<number, { resolve: (res: any) => void; reject: (err: any) => void; timer: NodeJS.Timeout }>();
  private framer: Framer;
  private maxFrameBytes: number;

  constructor(allowlistPathOrObj: string | Allowlist, maxFrameBytes?: number) {
    this.allowlist = loadAllowlist(allowlistPathOrObj);
    this.maxFrameBytes = maxFrameBytes || this.allowlist.max_frame_bytes || 104857600;
    this.framer = new Framer(this.maxFrameBytes);
  }

  async connect(fakeChromiumScript: string, fakeUrl: string, fakeLog?: string, fakeMode = 'echo', fakeBytes?: string): Promise<string> {
    const env: Record<string, string> = { ...process.env, FAKE_MODE: fakeMode, FAKE_URL: fakeUrl };
    if (fakeLog) env.FAKE_LOG = fakeLog;
    if (fakeBytes) env.FAKE_BYTES = fakeBytes;

    this.child = child_process.spawn('python3', ['-B', fakeChromiumScript], {
      stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
      env
    });

    const fd3Write = this.child.stdio[3] as any;
    const fd4Read = this.child.stdio[4] as any;

    if (!fd3Write || !fd4Read) {
      throw new CDPError('Failed to establish pipes to child process', 5);
    }

    fd4Read.on('error', () => {});

    return new Promise((resolve, reject) => {
      let handshakeDone = false;

      const timeout = setTimeout(() => {
        if (!handshakeDone) {
          this.close();
          reject(new CDPError('Handshake timed out', 5));
        }
      }, 10000);

      fd4Read.on('data', (chunk: Buffer) => {
        try {
          const frames = this.framer.feed(chunk);
          for (const frame of frames) {
            if (frame.length === 0) continue;
            let msg: any;
            try {
              msg = JSON.parse(frame.toString('utf-8'));
            } catch {
              continue;
            }

            if (msg.method === 'Target.attachedToTarget' && msg.params) {
              const ti = msg.params.targetInfo;
              if (ti && ti.type === 'page' && ti.url === fakeUrl) {
                this.sessionId = msg.params.sessionId;
                handshakeDone = true;
                clearTimeout(timeout);
                resolve(this.sessionId!);
              }
            }

            if (msg.id && this.pendingRequests.has(msg.id)) {
              const req = this.pendingRequests.get(msg.id)!;
              this.pendingRequests.delete(msg.id);
              clearTimeout(req.timer);
              if (msg.error) {
                req.reject(new CDPError(msg.error.message || 'CDP Request failed', 5));
              } else {
                req.resolve(msg.result);
              }
            }
          }
        } catch (err) {
          clearTimeout(timeout);
          this.close();
          reject(err);
        }
      });

      fd4Read.on('error', (err: any) => {
        clearTimeout(timeout);
        this.close();
        reject(err);
      });

      const handshakeReq = {
        id: this.nextId++,
        method: 'Target.setAutoAttach',
        params: {
          autoAttach: true,
          waitForDebuggerOnStart: false,
          flatten: true
        }
      };

      validateMethod(handshakeReq.method, this.allowlist, false);

      const payload = Buffer.from(JSON.stringify(handshakeReq) + '\0');
      fd3Write.write(payload);
    });
  }

  async send(method: string, params: any = {}): Promise<any> {
    if (!this.child || !this.child.stdio[3]) {
      throw new CDPError('Child process not connected', 5);
    }

    const isSessionMethod = method in (this.allowlist.session_methods || {});
    validateMethod(method, this.allowlist, isSessionMethod);

    const id = this.nextId++;
    const reqObj: any = { id, method, params };
    if (isSessionMethod && this.sessionId) {
      reqObj.sessionId = this.sessionId;
    }

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingRequests.delete(id);
        reject(new CDPError(`Request ${method} timed out`, 5));
      }, 10000);

      this.pendingRequests.set(id, { resolve, reject, timer });

      const payload = Buffer.from(JSON.stringify(reqObj) + '\0');
      (this.child!.stdio[3] as any).write(payload);
    });
  }

  close(): void {
    if (this.child) {
      if (this.child.stdio[3]) {
        try { (this.child.stdio[3] as any).destroy(); } catch {}
      }
      if (this.child.stdio[4]) {
        (this.child.stdio[4] as any).on('error', () => {});
        try { (this.child.stdio[4] as any).destroy(); } catch {}
      }
      try {
        this.child.kill('SIGKILL');
      } catch {}
      this.child = null;
    }
  }
}

export function parseAXTree(axResult: any): AXNode[] {
  if (!axResult || !Array.isArray(axResult.nodes)) {
    return [];
  }
  return axResult.nodes;
}

export function findAXNodeByRole(nodes: AXNode[], roleName: string): AXNode | null {
  for (const node of nodes) {
    const r = typeof node.role === 'object' ? node.role?.value : node.role;
    if (r === roleName) {
      return node;
    }
  }
  return null;
}

export function findFocusedAXNode(nodes: AXNode[]): AXNode | null {
  for (const node of nodes) {
    if (Array.isArray(node.properties)) {
      for (const prop of node.properties) {
        if (prop.name === 'focused' && (prop.value === true || prop.value?.value === true)) {
          return node;
        }
      }
    }
  }
  return null;
}

export function parseSnapshot(snapshotResult: any): DOMSnapshotDocument | null {
  if (!snapshotResult || !Array.isArray(snapshotResult.documents) || snapshotResult.documents.length === 0) {
    return null;
  }

  const doc = snapshotResult.documents[0];
  const strings: string[] = Array.isArray(snapshotResult.strings) ? snapshotResult.strings : [];

  const rawNodes = doc.nodes || {};
  const backendNodeIds: number[] = rawNodes.backendNodeId || [];
  const attributesList: number[][] = rawNodes.attributes || [];
  const nodeTypes: number[] = rawNodes.nodeType || [];
  const nodeNames: number[] = rawNodes.nodeName || [];

  const layout = doc.layout || {};
  const nodeIndices: number[] = layout.nodeIndex || [];
  const boundsList: number[][] = layout.bounds || [];
  const stylesList: number[][] = layout.styles || [];

  const layoutMap = new Map<number, { bounds: [number, number, number, number]; computedStyles: Record<string, string> }>();
  for (let k = 0; k < nodeIndices.length; k++) {
    const nIdx = nodeIndices[k];
    const bounds = (boundsList[k] || [0, 0, 0, 0]) as [number, number, number, number];
    const styleIndices = stylesList[k] || [];

    const computedStyles: Record<string, string> = {
      visibility: styleIndices[0] !== undefined ? (strings[styleIndices[0]] || 'visible') : 'visible',
      opacity: styleIndices[1] !== undefined ? (strings[styleIndices[1]] || '1') : '1',
      display: styleIndices[2] !== undefined ? (strings[styleIndices[2]] || 'block') : 'block'
    };

    layoutMap.set(nIdx, { bounds, computedStyles });
  }

  const parsedNodes: DOMSnapshotNode[] = [];
  for (let i = 0; i < backendNodeIds.length; i++) {
    const backendNodeId = backendNodeIds[i];
    const attrsRaw = attributesList[i] || [];
    const attrsObj: Record<string, string> = {};
    for (let a = 0; a < attrsRaw.length; a += 2) {
      const k = strings[attrsRaw[a]];
      const v = strings[attrsRaw[a + 1]];
      if (k !== undefined) {
        attrsObj[k] = v || '';
      }
    }

    const layoutData = layoutMap.get(i) || {
      bounds: [0, 0, 0, 0],
      computedStyles: { visibility: 'hidden', opacity: '0', display: 'none' }
    };

    parsedNodes.push({
      nodeIndex: i,
      backendNodeId,
      nodeName: strings[nodeNames[i]] || '',
      nodeType: nodeTypes[i] || 1,
      attributes: attrsObj,
      bounds: layoutData.bounds,
      computedStyles: layoutData.computedStyles
    });
  }

  return {
    scrollOffsetX: doc.scrollOffsetX || 0,
    scrollOffsetY: doc.scrollOffsetY || 0,
    nodes: parsedNodes
  };
}

export function findSnapshotNodeById(doc: DOMSnapshotDocument, idValue: string): DOMSnapshotNode | null {
  for (const node of doc.nodes) {
    if (node.attributes && node.attributes.id === idValue) {
      return node;
    }
  }
  return null;
}

export function isNodeVisible(node: DOMSnapshotNode): boolean {
  if (!node) return false;
  const [x, y, w, h] = node.bounds;
  if (w <= 0 || h <= 0) return false;

  const vis = (node.computedStyles.visibility || 'visible').toLowerCase();
  const opStr = node.computedStyles.opacity ?? '1';
  const op = parseFloat(opStr);
  const disp = (node.computedStyles.display || 'block').toLowerCase();

  if (vis !== 'visible') return false;
  if (isNaN(op) || op === 0) return false;
  if (disp === 'none') return false;

  return true;
}

export function isRefusedNode(attrs: Record<string, string> = {}, axName = ''): boolean {
  const typeVal = (attrs.type || '').toLowerCase();
  if (typeVal === 'password' || typeVal === 'file') {
    return true;
  }

  const sensitivePattern = /(password|passwd|secret|token|cc-|card|cvv|cvc)/i;

  const idVal = attrs.id || '';
  const nameVal = attrs.name || '';

  if (sensitivePattern.test(idVal) || sensitivePattern.test(nameVal) || sensitivePattern.test(axName)) {
    return true;
  }

  return false;
}

export async function plan_click(
  query: { id?: string; role?: string; focused?: boolean },
  snapshotProvider: () => Promise<any> | any,
  maxRetries = 3,
  axTreeProvider?: () => Promise<any> | any
): Promise<ActionPlan> {
  if (query.id && isRefusedNode({ id: query.id })) {
    return { kind: 'abort', reason: 'refused' };
  }

  let attempts = 0;
  let lastReason: 'not_found' | 'not_visible' = 'not_found';

  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    attempts = attempt;
    const rawSnapshot = await snapshotProvider();
    const doc = parseSnapshot(rawSnapshot);

    if (!doc) {
      lastReason = 'not_found';
      continue;
    }

    let targetNode: DOMSnapshotNode | null = null;
    let axName = '';

    if (query.id) {
      targetNode = findSnapshotNodeById(doc, query.id);
    } else if (query.role || query.focused) {
      if (axTreeProvider) {
        const rawAx = await axTreeProvider();
        const axNodes = parseAXTree(rawAx);
        let targetAxNode: AXNode | null = null;
        if (query.role) {
          targetAxNode = findAXNodeByRole(axNodes, query.role);
        } else if (query.focused) {
          targetAxNode = findFocusedAXNode(axNodes);
        }

        if (targetAxNode) {
          axName = typeof targetAxNode.name === 'object' ? targetAxNode.name?.value || '' : targetAxNode.name || '';
          const targetBackendId = targetAxNode.backendDOMNodeId || parseInt(targetAxNode.nodeId, 10);
          targetNode = doc.nodes.find(n => n.backendNodeId === targetBackendId) || null;
        }
      }
    }

    if (!targetNode) {
      lastReason = 'not_found';
      continue;
    }

    if (isRefusedNode(targetNode.attributes, axName)) {
      return { kind: 'abort', reason: 'refused' };
    }

    if (!isNodeVisible(targetNode)) {
      lastReason = 'not_visible';
      continue;
    }

    const [x, y, w, h] = targetNode.bounds;
    const cx = Math.floor(x + Math.floor(w / 2));
    const cy = Math.floor(y + Math.floor(h / 2));
    const vx = cx - doc.scrollOffsetX;
    const vy = cy - doc.scrollOffsetY;

    return {
      kind: 'click',
      xy: [cx, cy],
      viewport_xy: [vx, vy],
      attempts
    };
  }

  return {
    kind: 'abort',
    reason: lastReason
  };
}
