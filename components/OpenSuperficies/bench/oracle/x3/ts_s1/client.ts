import * as fs from 'node:fs';
import * as path from 'node:path';
import * as child_process from 'node:child_process';

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

export function loadAllowlist(allowlistPath?: string): Allowlist {
  const p = allowlistPath || path.resolve(process.cwd(), 'docker/cu/oracle/allowlist.json');
  const content = fs.readFileSync(p, 'utf-8');
  return JSON.parse(content) as Allowlist;
}

export class CDPClientError extends Error {
  code: number;
  constructor(message: string, code: number = 5) {
    super(message);
    this.name = 'CDPClientError';
    this.code = code;
  }
}

export interface ClientOptions {
  pythonPath?: string;
  scriptPath?: string;
  allowlistPath?: string;
  fakeMode?: string;
  fakeUrl?: string;
  fakeBytes?: number;
  fakeLog?: string;
  maxFrameBytesOverride?: number;
  timeoutMs?: number;
}

export class CDPClient {
  private child: child_process.ChildProcess | null = null;
  private pipeRead: fs.ReadStream | null = null;
  private pipeWrite: fs.WriteStream | null = null;
  private allowlist: Allowlist;
  private nextId: number = 1;
  private sessionId: string | null = null;
  private pendingRequests: Map<number, { resolve: (res: any) => void; reject: (err: any) => void; timer: NodeJS.Timeout }> = new Map();
  private buffer: Buffer = Buffer.alloc(0);
  private maxFrameBytes: number;
  private timeoutMs: number;
  private isClosed: boolean = false;
  private defaultOptions: ClientOptions;

  constructor(options: ClientOptions = {}) {
    this.defaultOptions = options;
    this.allowlist = loadAllowlist(options.allowlistPath);
    this.maxFrameBytes = options.maxFrameBytesOverride ?? this.allowlist.max_frame_bytes;
    this.timeoutMs = options.timeoutMs ?? 10000;
  }

  public getSessionId(): string | null {
    return this.sessionId;
  }

  public async start(options: ClientOptions = {}): Promise<string> {
    const opts = { ...this.defaultOptions, ...options };
    const pythonPath = opts.pythonPath || 'python3';
    const scriptPath = opts.scriptPath || path.resolve(process.cwd(), 'docker/cu/tests/fixtures/cdp/fake_chromium.py');
    const fakeMode = opts.fakeMode || process.env.FAKE_MODE || 'echo';
    const fakeUrl = opts.fakeUrl || process.env.FAKE_URL || 'file:///opt/cu/scenes/v3/ff_form_01.html';
    const fakeLog = opts.fakeLog || process.env.FAKE_LOG || '';
    const fakeBytes = opts.fakeBytes ?? process.env.FAKE_BYTES;

    const env: Record<string, string> = {
      ...process.env as Record<string, string>,
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

    const parentWriter = this.child.stdio[3] as unknown as fs.WriteStream;
    const parentReader = this.child.stdio[4] as unknown as fs.ReadStream;

    this.pipeWrite = parentWriter;
    this.pipeRead = parentReader;

    parentReader.on('data', (chunk: Buffer) => this.onData(chunk));
    parentReader.on('error', (err) => this.handleError(err));
    this.child.on('error', (err) => this.handleError(err));

    if (fakeMode === 'oversize') {
      return new Promise<string>((_, reject) => {
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

    const handshakeTimeout = new Promise<string>((_, reject) => {
      const timer = setTimeout(() => {
        reject(new CDPClientError('Handshake timed out waiting for Target.attachedToTarget', 5));
      }, this.timeoutMs);
      timer.unref();
    });

    const sessionPromise = new Promise<string>((resolve) => {
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

  private onData(chunk: Buffer): void {
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

  private processFrame(buf: Buffer): void {
    let msg: any;
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
      const req = this.pendingRequests.get(msg.id)!;
      this.pendingRequests.delete(msg.id);
      clearTimeout(req.timer);
      if (msg.error) {
        req.reject(new CDPClientError(msg.error.message || 'CDP Error', 5));
      } else {
        req.resolve(msg.result);
      }
    }
  }

  public isMethodAllowedLocally(method: string): boolean {
    if (method === 'Page.navigate') return false;
    const inBrowser = Object.prototype.hasOwnProperty.call(this.allowlist.browser_methods, method);
    const inSession = Object.prototype.hasOwnProperty.call(this.allowlist.session_methods, method);
    return inBrowser || inSession;
  }

  public send(method: string, params?: Record<string, any>): Promise<any> {
    if (!this.isMethodAllowedLocally(method)) {
      return Promise.reject(new CDPClientError(`Method '${method}' is refused by local allowlist`, 5));
    }
    return this.sendRaw(method, params, this.sessionId ?? undefined);
  }

  private sendRaw(method: string, params?: Record<string, any>, sessionId?: string): Promise<any> {
    if (!this.isMethodAllowedLocally(method)) {
      return Promise.reject(new CDPClientError(`Method '${method}' is refused by local allowlist`, 5));
    }

    const id = this.nextId++;
    const reqObj: any = { id, method };
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

  private handleError(err: any): void {
    for (const [id, req] of this.pendingRequests.entries()) {
      clearTimeout(req.timer);
      req.reject(err);
    }
    this.pendingRequests.clear();
    this.close();
  }

  public close(): void {
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

// Pure function interfaces & queries
export interface AXNode {
  nodeId: string;
  role?: { type: string; value: string } | string;
  name?: { type: string; value: string } | string;
  properties?: Array<{ name: string; value: any }>;
}

export interface AXTreeData {
  nodes?: AXNode[];
}

export interface DOMSnapshotData {
  strings?: string[];
  documents?: Array<{
    contentWidth?: number;
    contentHeight?: number;
    scrollOffsetX?: number;
    scrollOffsetY?: number;
    nodes?: {
      backendNodeId?: number[];
      attributes?: number[][];
      nodeName?: number[];
      nodeType?: number[];
      nodeValue?: number[];
      parentIndex?: number[];
    };
    layout?: {
      nodeIndex?: number[];
      bounds?: number[][];
      styles?: number[][];
    };
    strings?: string[];
  }>;
}

export interface ParsedNode {
  backendNodeId: number;
  attributes: Record<string, string>;
  bounds?: [number, number, number, number];
  computedStyle?: {
    visibility?: string;
    opacity?: string;
    display?: string;
  };
  scrollOffsetX: number;
  scrollOffsetY: number;
}

export function parseAXTree(axData: AXTreeData): { nodes: AXNode[] } {
  return { nodes: axData.nodes || [] };
}

export function findAXFocusedNode(axData: AXTreeData): AXNode | null {
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

export function findAXNodeByRole(axData: AXTreeData, role: string): AXNode | null {
  const nodes = axData.nodes || [];
  for (const node of nodes) {
    const nodeRole = typeof node.role === 'object' ? node.role?.value : node.role;
    if (nodeRole === role) {
      return node;
    }
  }
  return null;
}

export function parseDOMSnapshot(snapshotData: DOMSnapshotData): ParsedNode[] {
  const parsedNodes: ParsedNode[] = [];
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

    const layoutMap = new Map<number, { bounds: [number, number, number, number]; styles: number[] }>();
    for (let i = 0; i < layoutNodeIndices.length; i++) {
      const nIdx = layoutNodeIndices[i];
      const bounds = layoutBounds[i] as [number, number, number, number];
      const styles = layoutStyles[i] || [];
      if (bounds) {
        layoutMap.set(nIdx, { bounds, styles });
      }
    }

    const computedStyleNames = (layout as any).computedStyles || ['visibility', 'opacity', 'display'];

    for (let i = 0; i < backendNodeIds.length; i++) {
      const bNodeId = backendNodeIds[i];
      const rawAttrs = attributesList[i] || [];
      const attrMap: Record<string, string> = {};

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
      let bounds: [number, number, number, number] | undefined = undefined;
      let computedStyle: { visibility?: string; opacity?: string; display?: string } | undefined = undefined;

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
              (computedStyle as any)[styleName] = strVal;
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

export function isNodeVisible(node: ParsedNode): boolean {
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

export function findNodeById(snapshotData: DOMSnapshotData, targetId: string): ParsedNode | null {
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

function isNameSensitive(val: string): boolean {
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

export function isSensitiveNode(node: ParsedNode, axNode?: AXNode | null): boolean {
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

export interface ActionPlanResult {
  kind: 'click' | 'abort';
  xy?: [number, number];
  viewport_xy?: [number, number];
  attempts?: number;
  reason?: 'not_found' | 'not_visible' | 'refused';
}

export interface QueryTargetSelector {
  id?: string;
  role?: string;
  focused?: boolean;
}

export async function plan_click(
  selector: QueryTargetSelector,
  snapshotProvider: () => Promise<{ axData: AXTreeData; snapshotData: DOMSnapshotData }>,
  retryCount: number = 3
): Promise<ActionPlanResult> {
  let attempt = 0;
  let lastSeenNode: ParsedNode | null = null;
  let lastSeenAXNode: AXNode | null = null;

  while (attempt < retryCount) {
    attempt++;
    const { axData, snapshotData } = await snapshotProvider();

    // Check focused AX node and DOM node for sensitivity
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

    let targetAXNode: AXNode | null = null;
    if (selector.focused) {
      targetAXNode = focusedAXNode;
    } else if (selector.role) {
      targetAXNode = findAXNodeByRole(axData, selector.role);
    }

    let targetNode: ParsedNode | null = null;
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
        const [x, y, w, h] = targetNode.bounds!;
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
