import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawn, ChildProcess } from "node:child_process";
import { pathToFileURL } from "node:url";

export interface NodeRecord {
  i: number;
  tag: string;
  id: string;
  role: string;
  name: string;
  value: string;
  checked: boolean;
  box: [number, number, number, number] | null;
  act: boolean;
}

export interface CaptureState {
  step: number;
  op: string;
  ok: boolean;
  error: string | null;
  nodes: NodeRecord[];
}

export interface OutputDocument {
  schema: string;
  page: string;
  viewport: [number, number];
  states: CaptureState[];
}

export interface StepObject {
  op: "click" | "type" | "wait";
  selector: string;
  text?: string;
  timeout_ms?: number;
}

export function computeBox(x: number, y: number, width: number, height: number): [number, number, number, number] | null {
  if (width <= 0 || height <= 0) {
    return null;
  }
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const x1 = Math.max(x0, Math.ceil(x + width - 1));
  const y1 = Math.max(y0, Math.ceil(y + height - 1));
  return [x0, y0, x1, y1];
}

export function computeBackoffDelays(timeoutMs: number): number[] {
  const delays: number[] = [];
  let remaining = timeoutMs;
  let nextDelay = 1;
  while (remaining > 0) {
    const delay = Math.min(nextDelay, remaining);
    delays.push(delay);
    remaining -= delay;
    nextDelay = Math.min(nextDelay * 2, 64);
  }
  return delays;
}

export function isActionable(
  box: [number, number, number, number] | null,
  disabled: boolean,
  tag: string,
  hasHref: boolean,
  typeAttr: string | null,
  role: string
): boolean {
  if (box === null || disabled) {
    return false;
  }
  const tagLower = tag.toLowerCase();
  if (tagLower === "a" && hasHref) return true;
  if (["button", "select", "textarea", "summary"].includes(tagLower)) return true;
  if (tagLower === "input" && typeAttr !== "hidden") return true;

  const actionableRoles = new Set([
    "button", "link", "checkbox", "radio", "textbox", "searchbox",
    "combobox", "menuitem", "tab", "switch", "option", "slider", "spinbutton"
  ]);
  if (actionableRoles.has(role)) return true;

  return false;
}

export function canonicalJson(obj: any): string {
  if (obj === null || typeof obj !== "object") {
    return JSON.stringify(obj);
  }
  if (Array.isArray(obj)) {
    return "[" + obj.map(canonicalJson).join(",") + "]";
  }
  const keys = Object.keys(obj).sort();
  const parts = keys.map(k => JSON.stringify(k) + ":" + canonicalJson(obj[k]));
  return "{" + parts.join(",") + "}";
}

export function serializeCanonical(obj: any): string {
  const jsonStr = canonicalJson(obj);
  let res = "";
  for (let i = 0; i < jsonStr.length; i++) {
    const code = jsonStr.charCodeAt(i);
    if (code >= 0x20 && code <= 0x7e) {
      if (jsonStr[i] === "\\") {
        res += "\\";
      } else {
        res += jsonStr[i];
      }
    } else {
      if (jsonStr[i] === "\b") res += "\\b";
      else if (jsonStr[i] === "\f") res += "\\f";
      else if (jsonStr[i] === "\n") res += "\\n";
      else if (jsonStr[i] === "\r") res += "\\r";
      else if (jsonStr[i] === "\t") res += "\\t";
      else {
        const hex = code.toString(16).padStart(4, "0");
        res += "\\u" + hex;
      }
    }
  }
  return res;
}

export class FramingReader {
  private buffer: Buffer = Buffer.alloc(0);

  public push(chunk: Buffer): string[] {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    const messages: string[] = [];
    let idx: number;
    while ((idx = this.buffer.indexOf(0)) !== -1) {
      const msgBuf = this.buffer.subarray(0, idx);
      this.buffer = this.buffer.subarray(idx + 1);
      messages.push(msgBuf.toString("utf8"));
    }
    return messages;
  }

  public hasPartial(): boolean {
    return this.buffer.length > 0;
  }
}

export function parseArgs(argv: string[]) {
  let pagePath: string | null = null;
  let stepsPath: string | null = null;
  let timingPath: string | null = null;
  let timeoutS = 30;

  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--page" && i + 1 < argv.length) {
      pagePath = argv[++i];
    } else if (arg === "--steps" && i + 1 < argv.length) {
      stepsPath = argv[++i];
    } else if (arg === "--timing" && i + 1 < argv.length) {
      timingPath = argv[++i];
    } else if (arg === "--timeout-s" && i + 1 < argv.length) {
      timeoutS = parseFloat(argv[++i]);
    }
  }

  if (!pagePath || !stepsPath || !timingPath) {
    process.exit(2);
  }

  return { pagePath, stepsPath, timingPath, timeoutS };
}

function collapseWhitespaceAndTruncate(str: string): string {
  if (!str) return "";
  const collapsed = str.replace(/\s+/g, " ").trim();
  return Array.from(collapsed).slice(0, 120).join("");
}

export async function run() {
  const { pagePath, stepsPath, timingPath, timeoutS } = parseArgs(process.argv);

  const chromeBin = process.env.CHROME_BIN;
  if (!chromeBin) {
    process.exit(3);
  }

  const absolutePagePath = path.resolve(pagePath);
  if (!fs.existsSync(absolutePagePath)) {
    process.exit(2);
  }

  const absStepsPath = path.resolve(stepsPath);
  if (!fs.existsSync(absStepsPath)) {
    process.exit(2);
  }

  let stepsData: { steps: StepObject[] };
  try {
    stepsData = JSON.parse(fs.readFileSync(absStepsPath, "utf8"));
  } catch (e) {
    process.exit(2);
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "x2-ts-w2-"));

  const launchStart = process.hrtime.bigint();

  let chromeProcess: ChildProcess;
  try {
    chromeProcess = spawn(chromeBin, [
      "--headless=new",
      "--remote-debugging-pipe",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--disable-extensions",
      "--hide-scrollbars",
      "--force-device-scale-factor=1",
      "--window-size=1280,800",
      `--user-data-dir=${tmpDir}`,
      "about:blank"
    ], {
      stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"],
      detached: true
    });
  } catch (e) {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    process.exit(3);
  }

  const writePipe = chromeProcess.stdio[3] as any;
  const readPipe = chromeProcess.stdio[4] as any;

  if (!writePipe || !readPipe) {
    try { process.kill(-chromeProcess.pid!); } catch (e) {}
    fs.rmSync(tmpDir, { recursive: true, force: true });
    process.exit(3);
  }

  const reader = new FramingReader();
  const pendingRequests = new Map<number, (res: any) => void>();
  const eventQueue: any[] = [];
  const eventListeners: ((evt: any) => void)[] = [];
  let reqId = 1;

  function shutdownAndExit(code: number) {
    try {
      if (writePipe && !writePipe.destroyed) {
        const closeMsg = JSON.stringify({ id: reqId++, method: "Browser.close", params: {} }) + "\0";
        writePipe.write(Buffer.from(closeMsg, "utf8"));
      }
    } catch (e) {}

    const timer = setTimeout(() => {
      try { process.kill(-chromeProcess.pid!); } catch (e) {}
      fs.rmSync(tmpDir, { recursive: true, force: true });
      process.exit(code);
    }, 2000);

    chromeProcess.on("exit", () => {
      clearTimeout(timer);
      fs.rmSync(tmpDir, { recursive: true, force: true });
      process.exit(code);
    });
  }

  // Setup timeout
  const globalTimer = setTimeout(() => {
    shutdownAndExit(5);
  }, timeoutS * 1000);

  readPipe.on("data", (chunk: Buffer) => {
    const messages = reader.push(chunk);
    for (const msgStr of messages) {
      let msg: any;
      try {
        msg = JSON.parse(msgStr);
      } catch (e) {
        shutdownAndExit(7);
        return;
      }

      // Validation R14
      if (typeof msg !== "object" || msg === null) {
        shutdownAndExit(7);
        return;
      }

      if (typeof msg.id === "number" && Number.isInteger(msg.id)) {
        if (!msg.result && !msg.error) {
          shutdownAndExit(7);
          return;
        }
        const handler = pendingRequests.get(msg.id);
        if (!handler) {
          shutdownAndExit(7);
          return;
        }
        pendingRequests.delete(msg.id);
        handler(msg);
      } else if (typeof msg.method === "string" && (msg.id === undefined || msg.id === null)) {
        if (eventListeners.length > 0) {
          const listener = eventListeners.shift()!;
          listener(msg);
        } else {
          if (eventQueue.length < 1000) {
            eventQueue.push(msg);
          }
        }
      } else {
        shutdownAndExit(7);
        return;
      }
    }
  });

  readPipe.on("end", () => {
    if (reader.hasPartial()) {
      shutdownAndExit(6);
    }
  });

  readPipe.on("error", () => {});
  writePipe.on("error", () => {});

  function sendCommand(method: string, params: any = {}, sessionId?: string): Promise<any> {
    return new Promise((resolve) => {
      const id = reqId++;
      pendingRequests.set(id, resolve);
      const msgObj: any = { id, method, params };
      if (sessionId) msgObj.sessionId = sessionId;
      const str = JSON.stringify(msgObj) + "\0";
      writePipe.write(Buffer.from(str, "utf8"));
    });
  }

  function waitForEvent(methodName: string, sessionId?: string): Promise<any> {
    return new Promise((resolve) => {
      const checkQueue = () => {
        for (let i = 0; i < eventQueue.length; i++) {
          const evt = eventQueue[i];
          if (evt.method === methodName && (!sessionId || evt.sessionId === sessionId)) {
            eventQueue.splice(i, 1);
            return evt;
          }
        }
        return null;
      };

      const existing = checkQueue();
      if (existing) {
        resolve(existing);
        return;
      }

      const listener = (evt: any) => {
        if (evt.method === methodName && (!sessionId || evt.sessionId === sessionId)) {
          resolve(evt);
        } else {
          eventListeners.push(listener);
        }
      };
      eventListeners.push(listener);
    });
  }

  // Measure launch
  const targetsRes = await sendCommand("Target.getTargets");
  const launchEnd = process.hrtime.bigint();
  const launchMs = Number(launchEnd - launchStart) / 1e6;

  // Attach session
  const attachStart = process.hrtime.bigint();
  if (!targetsRes.result || !Array.isArray(targetsRes.result.targetInfos)) {
    shutdownAndExit(4);
    return;
  }
  const pageTarget = targetsRes.result.targetInfos.find((t: any) => t.type === "page");
  if (!pageTarget) {
    shutdownAndExit(4);
    return;
  }

  const attachRes = await sendCommand("Target.attachToTarget", { targetId: pageTarget.targetId, flatten: true });
  if (!attachRes.result || !attachRes.result.sessionId) {
    shutdownAndExit(4);
    return;
  }
  const sessionId: string = attachRes.result.sessionId;
  const attachEnd = process.hrtime.bigint();
  const attachMs = Number(attachEnd - attachStart) / 1e6;

  // Load page
  const loadStart = process.hrtime.bigint();
  await sendCommand("Page.enable", {}, sessionId);

  const fileUrl = pathToFileURL(absolutePagePath).href;
  const loadPromise = waitForEvent("Page.loadEventFired", sessionId);
  const navRes = await sendCommand("Page.navigate", { url: fileUrl }, sessionId);
  if (navRes.error) {
    shutdownAndExit(4);
    return;
  }

  await loadPromise;
  const loadEnd = process.hrtime.bigint();
  const loadMs = Number(loadEnd - loadStart) / 1e6;

  const captureMsList: number[] = [];
  const stepsMsList: number[] = [];
  const attemptsList: number[] = [];

  // Helper for settle & capture
  async function settleAndCapture(stepIndex: number, opName: string, stepOk: boolean, stepError: string | null): Promise<CaptureState> {
    const capStart = process.hrtime.bigint();

    // Settle promise: 2 consecutive rAF callbacks
    const settleExpr = `new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`;
    await sendCommand("Runtime.evaluate", { expression: settleExpr, awaitPromise: true }, sessionId);

    // Capture snapshot & AX tree
    const snapshotRes = await sendCommand("DOMSnapshot.captureSnapshot", { computedStyles: [], includeDOMRects: true }, sessionId);
    const axRes = await sendCommand("Accessibility.getFullAXTree", {}, sessionId);

    const strings: string[] = snapshotRes.result?.strings || [];
    const doc0 = snapshotRes.result?.documents?.[0];
    const axNodes: any[] = axRes.result?.nodes || [];

    // Map backendDOMNodeId -> AX Node
    const axByBackendId = new Map<number, any>();
    for (const axNode of axNodes) {
      if (typeof axNode.backendDOMNodeId === "number") {
        axByBackendId.set(axNode.backendDOMNodeId, axNode);
      }
    }

    const emittedNodes: NodeRecord[] = [];
    const skipTags = new Set(["html", "head", "script", "style", "meta", "link", "title", "noscript"]);

    if (doc0 && doc0.nodes && doc0.layout) {
      const nodes = doc0.nodes;
      const layout = doc0.layout;

      // Rare data maps
      const inputValueMap = new Map<number, string>();
      if (nodes.inputValue && Array.isArray(nodes.inputValue.index) && Array.isArray(nodes.inputValue.value)) {
        for (let k = 0; k < nodes.inputValue.index.length; k++) {
          const nodeIdx = nodes.inputValue.index[k];
          const strIdx = nodes.inputValue.value[k];
          if (strIdx !== -1 && strIdx !== undefined) {
            inputValueMap.set(nodeIdx, strings[strIdx] || "");
          }
        }
      }

      const inputCheckedSet = new Set<number>();
      if (nodes.inputChecked && Array.isArray(nodes.inputChecked.index)) {
        for (const nodeIdx of nodes.inputChecked.index) {
          inputCheckedSet.add(nodeIdx);
        }
      }

      // Layout mapping by nodeIndex
      const layoutByNodeIndex = new Map<number, any>();
      if (Array.isArray(layout.nodeIndex)) {
        for (let lIdx = 0; lIdx < layout.nodeIndex.length; lIdx++) {
          const nIdx = layout.nodeIndex[lIdx];
          const bounds = layout.bounds ? layout.bounds[lIdx] : null;
          layoutByNodeIndex.set(nIdx, { bounds });
        }
      }

      const nodeCount = nodes.nodeName ? nodes.nodeName.length : 0;
      for (let nIdx = 0; nIdx < nodeCount; nIdx++) {
        const layoutEntry = layoutByNodeIndex.get(nIdx);
        if (!layoutEntry) continue; // Must have a layout entry

        const nameStrIdx = nodes.nodeName[nIdx];
        const rawNodeName = strings[nameStrIdx] || "";
        const tagLower = rawNodeName.toLowerCase();

        if (skipTags.has(tagLower)) continue;

        // Extract attributes
        let idVal = "";
        let disabledAttr = false;
        let hrefAttr = false;
        let typeAttr: string | null = null;

        const attrIndices: number[] = nodes.attributes ? nodes.attributes[nIdx] || [] : [];
        for (let a = 0; a < attrIndices.length; a += 2) {
          const k = strings[attrIndices[a]];
          const v = strings[attrIndices[a + 1]];
          if (k === "id") idVal = v || "";
          if (k === "disabled") disabledAttr = true;
          if (k === "href") hrefAttr = true;
          if (k === "type") typeAttr = v || "";
        }

        const backendId = nodes.backendNodeId ? nodes.backendNodeId[nIdx] : null;
        const axNode = backendId !== null ? axByBackendId.get(backendId) : null;

        // Role & Name from AX node
        let roleVal = "";
        let nameVal = "";
        if (axNode && !axNode.ignored) {
          if (axNode.role && typeof axNode.role.value === "string") {
            roleVal = axNode.role.value;
          }
          if (axNode.name && typeof axNode.name.value === "string") {
            nameVal = collapseWhitespaceAndTruncate(axNode.name.value);
          }
        }

        // Value
        let valueVal = "";
        if (["input", "textarea", "select"].includes(tagLower)) {
          if (inputValueMap.has(nIdx)) {
            valueVal = inputValueMap.get(nIdx)!;
          } else if (axNode && axNode.value && typeof axNode.value.value === "string") {
            valueVal = axNode.value.value;
          }
        }

        // Checked
        let checkedVal = false;
        if (tagLower === "input" && (typeAttr === "checkbox" || typeAttr === "radio")) {
          checkedVal = inputCheckedSet.has(nIdx);
        }

        // Box
        let boxVal: [number, number, number, number] | null = null;
        if (layoutEntry.bounds && layoutEntry.bounds.length >= 4) {
          const [bx, by, bw, bh] = layoutEntry.bounds;
          boxVal = computeBox(bx, by, bw, bh);
        }

        // Act
        const actVal = isActionable(boxVal, disabledAttr, tagLower, hrefAttr, typeAttr, roleVal);

        emittedNodes.push({
          i: emittedNodes.length,
          tag: tagLower,
          id: idVal,
          role: roleVal,
          name: nameVal,
          value: valueVal,
          checked: checkedVal,
          box: boxVal,
          act: actVal
        });
      }
    }

    const capEnd = process.hrtime.bigint();
    captureMsList.push(Number(capEnd - capStart) / 1e6);

    return {
      step: stepIndex,
      op: opName,
      ok: stepOk,
      error: stepError,
      nodes: emittedNodes
    };
  }

  // Initial capture (step -1)
  const initialCapture = await settleAndCapture(-1, "load", true, null);
  const states: CaptureState[] = [initialCapture];

  // Execute steps
  const stepsList = stepsData.steps || [];
  for (let sIdx = 0; sIdx < stepsList.length; sIdx++) {
    const step = stepsList[sIdx];
    const stepStart = process.hrtime.bigint();
    const timeoutMs = step.timeout_ms ?? 2000;
    const delays = computeBackoffDelays(timeoutMs);

    let stepOk = false;
    let stepError: string | null = null;
    let attemptsCount = 0;

    for (const delay of delays) {
      attemptsCount++;

      // Resolve node
      const docRes = await sendCommand("DOM.getDocument", { depth: 1 }, sessionId);
      const rootNodeId = docRes.result?.root?.nodeId;

      if (!rootNodeId) {
        await new Promise(r => setTimeout(r, delay));
        continue;
      }

      const queryRes = await sendCommand("DOM.querySelector", { nodeId: rootNodeId, selector: step.selector }, sessionId);
      const targetNodeId = queryRes.result?.nodeId;

      if (!targetNodeId || targetNodeId === 0) {
        await new Promise(r => setTimeout(r, delay));
        continue;
      }

      const boxModelRes = await sendCommand("DOM.getBoxModel", { nodeId: targetNodeId }, sessionId);
      if (boxModelRes.error || !boxModelRes.result?.model) {
        await new Promise(r => setTimeout(r, delay));
        continue;
      }

      // Check disabled attribute
      const attrRes = await sendCommand("DOM.getAttributes", { nodeId: targetNodeId }, sessionId);
      const attrs: string[] = attrRes.result?.attributes || [];
      let isDisabled = false;
      for (let a = 0; a < attrs.length; a += 2) {
        if (attrs[a] === "disabled") {
          isDisabled = true;
          break;
        }
      }

      if (isDisabled) {
        await new Promise(r => setTimeout(r, delay));
        continue;
      }

      // Target is ready! Perform op.
      if (step.op === "click") {
        const borderQuad: number[] = boxModelRes.result.model.border;
        // 4 points (8 numbers: x0,y0, x1,y1, x2,y2, x3,y3)
        const meanX = (borderQuad[0] + borderQuad[2] + borderQuad[4] + borderQuad[6]) / 4;
        const meanY = (borderQuad[1] + borderQuad[3] + borderQuad[5] + borderQuad[7]) / 4;
        const cx = Math.floor(meanX);
        const cy = Math.floor(meanY);

        await sendCommand("Input.dispatchMouseEvent", { type: "mouseMoved", x: cx, y: cy, button: "left", clickCount: 1 }, sessionId);
        await sendCommand("Input.dispatchMouseEvent", { type: "mousePressed", x: cx, y: cy, button: "left", clickCount: 1 }, sessionId);
        await sendCommand("Input.dispatchMouseEvent", { type: "mouseReleased", x: cx, y: cy, button: "left", clickCount: 1 }, sessionId);
      } else if (step.op === "type") {
        await sendCommand("DOM.focus", { nodeId: targetNodeId }, sessionId);
        await sendCommand("Input.insertText", { text: step.text || "" }, sessionId);
      } else if (step.op === "wait") {
        // Ready condition met, nothing extra needed
      }

      stepOk = true;
      break;
    }

    if (!stepOk) {
      stepError = "timeout";
    }

    const stepEnd = process.hrtime.bigint();
    stepsMsList.push(Number(stepEnd - stepStart) / 1e6);
    attemptsList.push(attemptsCount);

    const capState = await settleAndCapture(sIdx, step.op, stepOk, stepError);
    states.push(capState);
  }

  // Generate output document
  const outDoc: OutputDocument = {
    schema: "vqa.x2-state/v1",
    page: path.basename(absolutePagePath),
    viewport: [1280, 800],
    states
  };

  const serializedOut = serializeCanonical(outDoc);
  process.stdout.write(serializedOut + "\n");

  // Write timing file
  const whereVal = process.env.X2_WHERE || "unlabelled";
  const timingObj = {
    lang: "ts",
    seed: "ts_w2",
    where: whereVal,
    launch_ms: Math.round(launchMs * 1000) / 1000,
    attach_ms: Math.round(attachMs * 1000) / 1000,
    load_ms: Math.round(loadMs * 1000) / 1000,
    capture_ms: captureMsList.map(v => Math.round(v * 1000) / 1000),
    steps_ms: stepsMsList.map(v => Math.round(v * 1000) / 1000),
    attempts: attemptsList
  };

  fs.writeFileSync(path.resolve(timingPath), JSON.stringify(timingObj, null, 2) + "\n");

  clearTimeout(globalTimer);
  shutdownAndExit(0);
}

if (process.argv[1] && process.argv[1].endsWith("x2.ts")) {
  run().catch(() => process.exit(7));
}
