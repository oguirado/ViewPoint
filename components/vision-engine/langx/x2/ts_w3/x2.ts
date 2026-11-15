import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { spawn, ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";

// ---------------------------------------------------------------------------
// Pure helper functions (exported for unit tests in x2.test.ts)
// ---------------------------------------------------------------------------

export function canonicalJson(obj: unknown): string {
  if (obj === null || typeof obj !== "object") {
    if (typeof obj === "string") {
      let result = "\"";
      for (let i = 0; i < obj.length; i++) {
        const code = obj.charCodeAt(i);
        if (code === 0x22) result += "\\\"";
        else if (code === 0x5c) result += "\\\\";
        else if (code === 0x08) result += "\\b";
        else if (code === 0x0c) result += "\\f";
        else if (code === 0x0a) result += "\\n";
        else if (code === 0x0d) result += "\\r";
        else if (code === 0x09) result += "\\t";
        else if (code < 0x20 || code >= 0x7f) {
          result += "\\u" + code.toString(16).padStart(4, "0");
        } else {
          result += obj[i];
        }
      }
      result += "\"";
      return result;
    }
    if (typeof obj === "number") {
      if (!Number.isFinite(obj)) throw new Error("Invalid number in JSON");
      return String(obj);
    }
    if (typeof obj === "boolean") return obj ? "true" : "false";
    if (obj === undefined) return "null";
    return String(obj);
  }

  if (Array.isArray(obj)) {
    return "[" + obj.map(canonicalJson).join(",") + "]";
  }

  const keys = Object.keys(obj).sort();
  const pairs: string[] = [];
  for (const k of keys) {
    const val = (obj as Record<string, unknown>)[k];
    if (val !== undefined) {
      pairs.push(canonicalJson(k) + ":" + canonicalJson(val));
    }
  }
  return "{" + pairs.join(",") + "}";
}

export function computeClosedBox(x: number, y: number, width: number, height: number): [number, number, number, number] | null {
  if (width <= 0 || height <= 0) {
    return null;
  }
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const x1 = Math.max(x0, Math.ceil(x + width - 1));
  const y1 = Math.max(y0, Math.ceil(y + height - 1));
  return [x0, y0, x1, y1];
}

export function computeBackoffSequence(maxTimeoutMs: number): number[] {
  const delays: number[] = [];
  let current = 1;
  let elapsed = 0;
  while (elapsed < maxTimeoutMs) {
    const delay = Math.min(current, maxTimeoutMs - elapsed);
    delays.push(delay);
    elapsed += delay;
    current = Math.min(current * 2, 64);
  }
  return delays;
}

export interface SyntheticElement {
  box: [number, number, number, number] | null;
  disabled?: boolean;
  tag: string;
  href?: string | null;
  type?: string | null;
  role: string;
}

export function isActionable(elem: SyntheticElement): boolean {
  if (elem.box === null) return false;
  if (elem.disabled) return false;

  const tag = elem.tag.toLowerCase();
  const role = elem.role.toLowerCase();

  const actionableTags = new Set(["button", "select", "textarea", "summary"]);
  if (actionableTags.has(tag)) return true;
  if (tag === "a" && elem.href !== undefined && elem.href !== null) return true;
  if (tag === "input" && (elem.type || "").toLowerCase() !== "hidden") return true;

  const actionableRoles = new Set([
    "button", "link", "checkbox", "radio", "textbox", "searchbox",
    "combobox", "menuitem", "tab", "switch", "option", "slider", "spinbutton"
  ]);
  if (actionableRoles.has(role)) return true;

  return false;
}

export interface FrameReaderResult {
  messages: any[];
  remainingBuffer: Buffer;
  hasTornTail?: boolean;
}

export function parseFramedChunk(
  buffer: Buffer,
  chunk: Buffer,
  isEof: boolean,
  pendingIds: Set<number>
): { messages: any[]; newBuffer: Buffer; exitCode?: number } {
  const combined = Buffer.concat([buffer, chunk]);
  const messages: any[] = [];
  let start = 0;

  for (let i = 0; i < combined.length; i++) {
    if (combined[i] === 0) {
      const msgBuf = combined.subarray(start, i);
      start = i + 1;
      if (msgBuf.length === 0) continue;

      let parsed: any;
      try {
        parsed = JSON.parse(msgBuf.toString("utf8"));
      } catch {
        return { messages: [], newBuffer: Buffer.alloc(0), exitCode: 7 };
      }

      // Rule R14 validation
      if (parsed && typeof parsed === "object") {
        if ("id" in parsed) {
          const id = parsed.id;
          if (typeof id !== "number" || !Number.isInteger(id)) {
            return { messages: [], newBuffer: Buffer.alloc(0), exitCode: 7 };
          }
          if (!pendingIds.has(id)) {
            return { messages: [], newBuffer: Buffer.alloc(0), exitCode: 7 };
          }
          if (!("result" in parsed) && !("error" in parsed)) {
            return { messages: [], newBuffer: Buffer.alloc(0), exitCode: 7 };
          }
        } else if ("method" in parsed && typeof parsed.method === "string") {
          // Valid event message
        } else {
          return { messages: [], newBuffer: Buffer.alloc(0), exitCode: 7 };
        }
      } else {
        return { messages: [], newBuffer: Buffer.alloc(0), exitCode: 7 };
      }

      messages.push(parsed);
    }
  }

  const remaining = combined.subarray(start);
  if (isEof && remaining.length > 0) {
    return { messages, newBuffer: remaining, exitCode: 6 };
  }

  return { messages, newBuffer: remaining };
}

// ---------------------------------------------------------------------------
// CLI Execution Logic
// ---------------------------------------------------------------------------

interface NodeRecord {
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

interface StateRecord {
  step: number;
  op: string;
  ok: boolean;
  error: string | null;
  nodes: NodeRecord[];
}

interface StepInput {
  op: "click" | "type" | "wait";
  selector: string;
  text?: string;
  timeout_ms?: number;
}

function parseArgs(args: string[]) {
  let pagePath: string | undefined;
  let stepsPath: string | undefined;
  let timingPath: string | undefined;
  let timeoutS = 30;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--page" && i + 1 < args.length) {
      pagePath = args[++i];
    } else if (args[i] === "--steps" && i + 1 < args.length) {
      stepsPath = args[++i];
    } else if (args[i] === "--timing" && i + 1 < args.length) {
      timingPath = args[++i];
    } else if (args[i] === "--timeout-s" && i + 1 < args.length) {
      const parsed = parseInt(args[++i], 10);
      if (!isNaN(parsed)) timeoutS = parsed;
    }
  }

  if (!pagePath || !stepsPath || !timingPath) {
    process.exit(2);
  }

  return { pagePath, stepsPath, timingPath, timeoutS };
}

function hrtimeMs(startNs: bigint): number {
  const diff = process.hrtime.bigint() - startNs;
  return Number(diff) / 1e6;
}

function round3(num: number): number {
  return Math.round(num * 1000) / 1000;
}

async function main() {
  const startTimeNs = process.hrtime.bigint();
  const { pagePath, stepsPath, timingPath, timeoutS } = parseArgs(process.argv.slice(2));

  // Overall process timeout
  const globalTimer = setTimeout(() => {
    process.exit(5);
  }, timeoutS * 1000);
  globalTimer.unref();

  const chromeBin = process.env.CHROME_BIN;
  if (!chromeBin) {
    process.exit(3);
  }

  // Create temporary user-data-dir
  const tmpUserDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "x2-profile-"));

  let browserProcess: ChildProcess | undefined;

  const cleanupAndExit = (code: number) => {
    try {
      if (fs.existsSync(tmpUserDataDir)) {
        fs.rmSync(tmpUserDataDir, { recursive: true, force: true });
      }
    } catch {
      // Ignore cleanup error on exit
    }
    if (browserProcess && !browserProcess.killed) {
      try {
        browserProcess.kill("SIGKILL");
      } catch {
        // Ignore
      }
    }
    process.exit(code);
  };

  const launchStart = process.hrtime.bigint();
  try {
    browserProcess = spawn(chromeBin, [
      "--headless=new",
      "--remote-debugging-pipe",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--disable-extensions",
      "--hide-scrollbars",
      "--force-device-scale-factor=1",
      "--window-size=1280,800",
      `--user-data-dir=${tmpUserDataDir}`,
      "about:blank"
    ], {
      stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"],
      detached: true
    });
  } catch {
    cleanupAndExit(3);
    return;
  }

  if (!browserProcess || !browserProcess.stdio[3] || !browserProcess.stdio[4]) {
    cleanupAndExit(3);
    return;
  }

  const writeStream = browserProcess.stdio[3] as fs.WriteStream;
  const readStream = browserProcess.stdio[4] as fs.ReadStream;

  let nextReqId = 1;
  const pendingRequests = new Map<number, { resolve: (val: any) => void; reject: (err: any) => void }>();
  const pendingIdsSet = new Set<number>();
  let eventListeners: Array<(evt: any) => void> = [];

  let readBuffer = Buffer.alloc(0);

  readStream.on("data", (chunk: Buffer) => {
    const res = parseFramedChunk(readBuffer, chunk, false, pendingIdsSet);
    if (res.exitCode !== undefined) {
      cleanupAndExit(res.exitCode);
      return;
    }
    readBuffer = res.newBuffer;

    for (const msg of res.messages) {
      if (msg.id !== undefined) {
        const handler = pendingRequests.get(msg.id);
        if (handler) {
          pendingRequests.delete(msg.id);
          pendingIdsSet.delete(msg.id);
          if (msg.error) {
            handler.reject(msg.error);
          } else {
            handler.resolve(msg.result);
          }
        }
      } else if (msg.method) {
        for (const listener of eventListeners) {
          listener(msg);
        }
      }
    }
  });

  readStream.on("end", () => {
    if (readBuffer.length > 0) {
      cleanupAndExit(6);
    }
  });

  browserProcess.on("error", () => {
    cleanupAndExit(3);
  });

  function sendCommand(method: string, params: any = {}, sessionId?: string): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = nextReqId++;
      pendingIdsSet.add(id);
      pendingRequests.set(id, { resolve, reject });

      const msg: any = { id, method, params };
      if (sessionId) {
        msg.sessionId = sessionId;
      }

      const jsonStr = JSON.stringify(msg) + "\0";
      writeStream.write(jsonStr, (err) => {
        if (err) {
          pendingRequests.delete(id);
          pendingIdsSet.delete(id);
          reject(err);
        }
      });
    });
  }

  // Get Target & Attach
  const attachStart = process.hrtime.bigint();
  let targetSessionId: string;
  try {
    const targetsRes = await sendCommand("Target.getTargets");
    const pageTarget = targetsRes.targetInfos?.find((t: any) => t.type === "page");
    if (!pageTarget) {
      cleanupAndExit(4);
      return;
    }
    const attachRes = await sendCommand("Target.attachToTarget", { targetId: pageTarget.targetId, flatten: true });
    targetSessionId = attachRes.sessionId;
  } catch {
    cleanupAndExit(4);
    return;
  }
  const attachMs = hrtimeMs(attachStart);
  const launchMs = hrtimeMs(launchStart);

  // Navigate & Wait Load
  const loadStart = process.hrtime.bigint();
  const absolutePagePath = path.resolve(pagePath);
  const fileUrl = "file://" + absolutePagePath;

  const loadEventPromise = new Promise<void>((resolve) => {
    const listener = (evt: any) => {
      if (evt.method === "Page.loadEventFired") {
        eventListeners = eventListeners.filter((l) => l !== listener);
        resolve();
      }
    };
    eventListeners.push(listener);
  });

  try {
    await sendCommand("Page.enable", {}, targetSessionId);
    await sendCommand("Page.navigate", { url: fileUrl }, targetSessionId);
    await loadEventPromise;
  } catch {
    cleanupAndExit(4);
    return;
  }
  const loadMs = hrtimeMs(loadStart);

  // Function to settle before capture
  const settlePage = async () => {
    await sendCommand("Runtime.evaluate", {
      expression: "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))",
      awaitPromise: true
    }, targetSessionId);
  };

  // Helper for capturing nodes
  const captureTimesMs: number[] = [];
  const captureState = async (stepIdx: number, op: string, ok: boolean, errorStr: string | null): Promise<StateRecord> => {
    const capStart = process.hrtime.bigint();
    await settlePage();

    // Pipeline captureSnapshot and getFullAXTree
    const [snapshotRes, axRes] = await Promise.all([
      sendCommand("DOMSnapshot.captureSnapshot", { computedStyles: [], includeDOMRects: true }, targetSessionId),
      sendCommand("Accessibility.getFullAXTree", {}, targetSessionId)
    ]);

    captureTimesMs.push(round3(hrtimeMs(capStart)));

    const doc = snapshotRes.documents[0];
    const strings: string[] = snapshotRes.strings || [];
    const axNodes: any[] = axRes.nodes || [];

    // Map backendDOMNodeId -> AX node
    const axMap = new Map<number, any>();
    for (const ax of axNodes) {
      if (ax.backendDOMNodeId !== undefined) {
        axMap.set(ax.backendDOMNodeId, ax);
      }
    }

    // Set of node indices that have layout bounds
    const layoutNodeIndices = doc.layout?.nodeIndex || [];
    const layoutBounds = doc.layout?.bounds || [];
    const layoutMap = new Map<number, number[]>();
    for (let lIdx = 0; lIdx < layoutNodeIndices.length; lIdx++) {
      layoutMap.set(layoutNodeIndices[lIdx], layoutBounds[lIdx]);
    }

    const skippedTags = new Set(["html", "head", "script", "style", "meta", "link", "title", "noscript"]);
    const nodes: NodeRecord[] = [];

    const nodesData = doc.nodes;
    const count = nodesData.nodeName ? nodesData.nodeName.length : 0;

    // Helper to read tabular properties
    const getTabularVal = (tbl: any, idx: number) => {
      if (!tbl || !tbl.index) return undefined;
      const pos = tbl.index.indexOf(idx);
      if (pos === -1) return undefined;
      return tbl.value[pos];
    };

    let emittedCount = 0;
    for (let nodeIdx = 0; nodeIdx < count; nodeIdx++) {
      const nodeType = nodesData.nodeType[nodeIdx];
      // Only element nodes (type 1)
      if (nodeType !== 1) continue;

      // Must have layout entry
      const bounds = layoutMap.get(nodeIdx);
      if (!bounds) continue;

      const tagRaw = strings[nodesData.nodeName[nodeIdx]] || "";
      const tag = tagRaw.toLowerCase();
      if (skippedTags.has(tag)) continue;

      // Extract attributes
      const attrList = nodesData.attributes ? nodesData.attributes[nodeIdx] : [];
      let idAttr = "";
      let disabledAttr = false;
      let hrefAttr: string | null = null;
      let typeAttr: string | null = null;

      if (attrList) {
        for (let a = 0; a < attrList.length; a += 2) {
          const name = strings[attrList[a]];
          const val = strings[attrList[a + 1]] || "";
          if (name === "id") idAttr = val;
          if (name === "disabled") disabledAttr = true;
          if (name === "href") hrefAttr = val;
          if (name === "type") typeAttr = val;
        }
      }

      // AX tree info
      const backendId = nodesData.backendNodeId[nodeIdx];
      const axNode = axMap.get(backendId);

      let role = "";
      let name = "";
      if (axNode && !axNode.ignored) {
        if (axNode.role && typeof axNode.role.value === "string") {
          role = axNode.role.value;
        }
        if (axNode.name && typeof axNode.name.value === "string") {
          name = axNode.name.value;
        }
      }

      // If div/span/generic element has no name on its own AX node, inherit name from direct StaticText child if present
      if (!name && axNode && axNode.childIds) {
        for (const childId of axNode.childIds) {
          const childAx = axNodes.find((n: any) => n.nodeId === childId);
          if (childAx && !childAx.ignored && childAx.role?.value === "StaticText" && childAx.name?.value) {
            name = childAx.name.value;
            break;
          }
        }
      }

      // Process name: collapse whitespace, trim, cut to 120 code points
      if (name) {
        name = name.replace(/\s+/g, " ").trim();
        name = Array.from(name).slice(0, 120).join("");
      }

      // Value extraction
      let value = "";
      if (tag === "input" || tag === "textarea" || tag === "select") {
        const inpValIdx = getTabularVal(nodesData.inputValue, nodeIdx);
        if (inpValIdx !== undefined) {
          value = strings[inpValIdx] || "";
        } else {
          const textValIdx = getTabularVal(nodesData.textValue, nodeIdx);
          if (textValIdx !== undefined) {
            value = strings[textValIdx] || "";
          }
        }
      }

      // Checked extraction
      let checked = false;
      if (tag === "input" && (typeAttr === "checkbox" || typeAttr === "radio")) {
        const chkPos = nodesData.inputChecked?.index ? nodesData.inputChecked.index.indexOf(nodeIdx) : -1;
        if (chkPos !== -1) {
          checked = true;
        }
      }

      // Closed integer box calculation (Rule R10)
      const [x, y, w, h] = bounds;
      const box = computeClosedBox(x, y, w, h);

      // Actionable calculation
      const act = isActionable({
        box,
        disabled: disabledAttr,
        tag,
        href: hrefAttr,
        type: typeAttr,
        role
      });

      nodes.push({
        i: emittedCount++,
        tag,
        id: idAttr,
        role,
        name,
        value,
        checked,
        box,
        act
      });
    }

    return {
      step: stepIdx,
      op,
      ok,
      error: errorStr,
      nodes
    };
  };

  // Initial capture
  const initialCap = await captureState(-1, "load", true, null);
  const statesList: StateRecord[] = [initialCap];

  // Steps execution
  let stepsData: StepInput[] = [];
  try {
    const rawSteps = fs.readFileSync(stepsPath, "utf8");
    const parsedSteps = JSON.parse(rawSteps);
    if (parsedSteps && Array.isArray(parsedSteps.steps)) {
      stepsData = parsedSteps.steps;
    }
  } catch {
    // Bad steps file or missing
  }

  const stepsMsList: number[] = [];

  for (let sIdx = 0; sIdx < stepsData.length; sIdx++) {
    const step = stepsData[sIdx];
    const stepStart = process.hrtime.bigint();
    const maxTimeoutMs = step.timeout_ms ?? 2000;
    const delays = computeBackoffSequence(maxTimeoutMs);

    let targetNodeId = 0;
    let stepOk = false;
    let stepError: string | null = null;
    let targetQuad: number[] | null = null;

    for (const delay of delays) {
      try {
        const docRes = await sendCommand("DOM.getDocument", { depth: 1 }, targetSessionId);
        const rootNodeId = docRes.root.nodeId;
        const selectorRes = await sendCommand("DOM.querySelector", { nodeId: rootNodeId, selector: step.selector }, targetSessionId);

        if (selectorRes.nodeId && selectorRes.nodeId !== 0) {
          const nId = selectorRes.nodeId;

          // Check disabled attribute
          const attrsRes = await sendCommand("DOM.getAttributes", { nodeId: nId }, targetSessionId);
          const attrs: string[] = attrsRes.attributes || [];
          let isDisabled = false;
          for (let a = 0; a < attrs.length; a += 2) {
            if (attrs[a].toLowerCase() === "disabled") {
              isDisabled = true;
              break;
            }
          }

          if (!isDisabled) {
            const boxModelRes = await sendCommand("DOM.getBoxModel", { nodeId: nId }, targetSessionId);
            if (boxModelRes.model && boxModelRes.model.border) {
              targetNodeId = nId;
              targetQuad = boxModelRes.model.border;
              stepOk = true;
              break;
            }
          }
        }
      } catch {
        // Retry backoff
      }

      await new Promise((r) => setTimeout(r, delay));
    }

    if (!stepOk) {
      stepError = "timeout";
    } else {
      // Perform action
      try {
        if (step.op === "click") {
          if (targetQuad && targetQuad.length >= 8) {
            const cx = Math.floor((targetQuad[0] + targetQuad[2] + targetQuad[4] + targetQuad[6]) / 4);
            const cy = Math.floor((targetQuad[1] + targetQuad[3] + targetQuad[5] + targetQuad[7]) / 4);

            await sendCommand("Input.dispatchMouseEvent", { type: "mouseMoved", x: cx, y: cy }, targetSessionId);
            await sendCommand("Input.dispatchMouseEvent", { type: "mousePressed", x: cx, y: cy, button: "left", clickCount: 1 }, targetSessionId);
            await sendCommand("Input.dispatchMouseEvent", { type: "mouseReleased", x: cx, y: cy, button: "left", clickCount: 1 }, targetSessionId);
          }
        } else if (step.op === "type") {
          await sendCommand("DOM.focus", { nodeId: targetNodeId }, targetSessionId);
          await sendCommand("Input.insertText", { text: step.text || "" }, targetSessionId);
        } else if (step.op === "wait") {
          // Only waiting until ready
        }
      } catch {
        stepOk = false;
        stepError = "timeout";
      }
    }

    stepsMsList.push(round3(hrtimeMs(stepStart)));

    const capState = await captureState(sIdx, step.op, stepOk, stepError);
    statesList.push(capState);
  }

  // Close browser
  try {
    await sendCommand("Browser.close");
  } catch {
    // Ignore close errors
  }

  // Graceful exit delay
  await new Promise((r) => setTimeout(r, 100));

  // Output to stdout
  const pageBasename = path.basename(pagePath);
  const outputDoc = {
    page: pageBasename,
    schema: "vqa.x2-state/v1",
    states: statesList,
    viewport: [1280, 800]
  };

  const stdoutStr = canonicalJson(outputDoc) + "\n";
  process.stdout.write(stdoutStr);

  // Write timing file
  const timingData = {
    attempts: [1],
    attach_ms: round3(attachMs),
    capture_ms: captureTimesMs,
    lang: "ts",
    launch_ms: round3(launchMs),
    load_ms: round3(loadMs),
    seed: "ts_w3",
    steps_ms: stepsMsList,
    where: process.env.X2_WHERE || "unlabelled"
  };

  try {
    fs.writeFileSync(timingPath, canonicalJson(timingData) + "\n");
  } catch {
    // Ignore timing write errors
  }

  cleanupAndExit(0);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main();
}
