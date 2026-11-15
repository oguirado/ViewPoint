import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import child_process from "node:child_process";

// Canonical JSON serializer matching Python json.dumps(..., ensure_ascii=True, sort_keys=True, separators=(',', ':'))
export function canonicalSerialize(val: any): string {
  if (val === null) return "null";
  if (typeof val === "boolean") return val ? "true" : "false";
  if (typeof val === "number") {
    if (!Number.isInteger(val)) {
      throw new Error(`Float detected in stdout canonical serialization: ${val}`);
    }
    return val.toString();
  }
  if (typeof val === "string") {
    let res = '"';
    for (let i = 0; i < val.length; i++) {
      const code = val.charCodeAt(i);
      if (code === 0x22) res += '\\"';
      else if (code === 0x5c) res += "\\\\";
      else if (code === 0x08) res += "\\b";
      else if (code === 0x0c) res += "\\f";
      else if (code === 0x0a) res += "\\n";
      else if (code === 0x0d) res += "\\r";
      else if (code === 0x09) res += "\\t";
      else if (code < 0x20 || code > 0x7f) {
        res += "\\u" + code.toString(16).padStart(4, "0");
      } else {
        res += val[i];
      }
    }
    return res + '"';
  }
  if (Array.isArray(val)) {
    return "[" + val.map(canonicalSerialize).join(",") + "]";
  }
  if (typeof val === "object") {
    const keys = Object.keys(val).sort();
    const parts = keys.map(
      (k) => canonicalSerialize(k) + ":" + canonicalSerialize(val[k])
    );
    return "{" + parts.join(",") + "}";
  }
  throw new Error(`Unsupported type for canonical serialization: ${typeof val}`);
}

// Box rule: closed integer box [x0, y0, x1, y1] or null
export function computeBox(
  x: number,
  y: number,
  w: number,
  h: number
): [number, number, number, number] | null {
  if (w <= 0 || h <= 0) return null;
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const x1 = Math.max(x0, Math.ceil(x + w - 1));
  const y1 = Math.max(y0, Math.ceil(y + h - 1));
  return [x0, y0, x1, y1];
}

// Actionable rule calculation
export function computeAct(
  box: [number, number, number, number] | null,
  disabled: boolean,
  tag: string,
  attrs: Record<string, string>,
  role: string
): boolean {
  if (box === null) return false;
  if (disabled) return false;

  const tagLower = tag.toLowerCase();
  if (
    (tagLower === "a" && attrs.href !== undefined) ||
    tagLower === "button" ||
    tagLower === "select" ||
    tagLower === "textarea" ||
    tagLower === "summary" ||
    (tagLower === "input" && (attrs.type || "").toLowerCase() !== "hidden")
  ) {
    return true;
  }

  const actRoles = new Set([
    "button",
    "link",
    "checkbox",
    "radio",
    "textbox",
    "searchbox",
    "combobox",
    "menuitem",
    "tab",
    "switch",
    "option",
    "slider",
    "spinbutton",
  ]);
  if (actRoles.has(role)) {
    return true;
  }

  return false;
}

// Bounded backoff delay generator / sleep helper
export function getBackoffDelays(maxMs: number): number[] {
  const delays: number[] = [];
  let current = 1;
  let spent = 0;
  while (spent < maxMs) {
    const d = Math.min(current, maxMs - spent);
    delays.push(d);
    spent += d;
    if (current < 64) {
      current = Math.min(current * 2, 64);
    } else {
      current = 64;
    }
  }
  return delays;
}

export function collapseWhitespaceAndTruncate(s: string): string {
  const collapsed = s.replace(/\s+/g, " ").trim();
  const codePoints = Array.from(collapsed);
  if (codePoints.length > 120) {
    return codePoints.slice(0, 120).join("");
  }
  return collapsed;
}

// Main execution block if called directly
async function main() {
  const argv = process.argv.slice(2);
  let pagePath = "";
  let stepsPath = "";
  let timingPath = "";
  let timeoutS = 30;

  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--page" && i + 1 < argv.length) {
      pagePath = argv[++i];
    } else if (argv[i] === "--steps" && i + 1 < argv.length) {
      stepsPath = argv[++i];
    } else if (argv[i] === "--timing" && i + 1 < argv.length) {
      timingPath = argv[++i];
    } else if (argv[i] === "--timeout-s" && i + 1 < argv.length) {
      timeoutS = parseFloat(argv[++i]);
    }
  }

  if (!pagePath || !stepsPath || !timingPath || isNaN(timeoutS)) {
    process.exit(2);
  }

  const chromeBin = process.env.CHROME_BIN;
  if (!chromeBin) {
    process.exit(3);
  }

  const absPagePath = path.resolve(pagePath);
  if (!fs.existsSync(absPagePath) || !fs.existsSync(stepsPath)) {
    process.exit(2);
  }

  let stepsData: any;
  try {
    stepsData = JSON.parse(fs.readFileSync(stepsPath, "utf8"));
  } catch {
    process.exit(2);
  }

  const stepsList: Array<{
    op: string;
    selector: string;
    text?: string;
    timeout_ms?: number;
  }> = stepsData.steps || [];

  const launchStart = process.hrtime.bigint();
  const tmpProfileDir = fs.mkdtempSync(path.join(os.tmpdir(), "x2-chrome-profile-"));

  let chromeProcess: child_process.ChildProcess;
  try {
    chromeProcess = child_process.spawn(
      chromeBin,
      [
        "--headless=new",
        "--remote-debugging-pipe",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1280,800",
        `--user-data-dir=${tmpProfileDir}`,
        "about:blank",
      ],
      {
        stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"],
      }
    );
  } catch {
    fs.rmSync(tmpProfileDir, { recursive: true, force: true });
    process.exit(3);
  }

  chromeProcess.on("error", () => {
    fs.rmSync(tmpProfileDir, { recursive: true, force: true });
    process.exit(3);
  });

  const writeStream = chromeProcess.stdio[3] as any;
  const readStream = chromeProcess.stdio[4] as any;

  if (!writeStream || !readStream) {
    fs.rmSync(tmpProfileDir, { recursive: true, force: true });
    process.exit(3);
  }

  let isCleanedUp = false;
  function cleanupAndExit(code: number) {
    if (isCleanedUp) return;
    isCleanedUp = true;
    try {
      if (writeStream && !writeStream.destroyed) {
        const id = reqId++;
        const closeMsg = JSON.stringify({ id, method: "Browser.close" }) + "\0";
        writeStream.write(closeMsg);
      }
    } catch {}

    setTimeout(() => {
      try {
        if (chromeProcess && chromeProcess.pid) {
          process.kill(-chromeProcess.pid, "SIGKILL");
        }
      } catch {
        try {
          if (chromeProcess) chromeProcess.kill("SIGKILL");
        } catch {}
      }
      try {
        fs.rmSync(tmpProfileDir, { recursive: true, force: true });
      } catch {}
      process.exit(code);
    }, 100);
  }

  // Global overall timeout timer
  const globalTimeoutTimer = setTimeout(() => {
    cleanupAndExit(5);
  }, timeoutS * 1000);

  let reqId = 1;
  const pendingRequests = new Map<
    number,
    { resolve: (msg: any) => void; reject: (err: any) => void }
  >();

  let onLoadEventFired: (() => void) | null = null;
  let inBuffer = Buffer.alloc(0);

  readStream.on("data", (chunk: Buffer) => {
    inBuffer = Buffer.concat([inBuffer, chunk]);
    while (true) {
      const nullIdx = inBuffer.indexOf(0);
      if (nullIdx === -1) break;

      const msgBuf = inBuffer.subarray(0, nullIdx);
      inBuffer = inBuffer.subarray(nullIdx + 1);

      let msg: any;
      try {
        msg = JSON.parse(msgBuf.toString("utf8"));
      } catch {
        cleanupAndExit(7);
        return;
      }

      // Rule R14 validation
      if (msg && typeof msg === "object" && msg.id !== undefined) {
        if (!Number.isInteger(msg.id)) {
          cleanupAndExit(7);
          return;
        }
        if (!pendingRequests.has(msg.id)) {
          cleanupAndExit(7);
          return;
        }
        if (!msg.result && !msg.error) {
          cleanupAndExit(7);
          return;
        }
        const handler = pendingRequests.get(msg.id)!;
        pendingRequests.delete(msg.id);
        handler.resolve(msg);
      } else if (msg && typeof msg === "object" && typeof msg.method === "string" && msg.id === undefined) {
        if (msg.method === "Page.loadEventFired" && onLoadEventFired) {
          onLoadEventFired();
          onLoadEventFired = null;
        }
      } else {
        cleanupAndExit(7);
        return;
      }
    }
  });

  readStream.on("end", () => {
    if (inBuffer.length > 0) {
      cleanupAndExit(6);
    }
  });

  readStream.on("error", () => {
    cleanupAndExit(3);
  });

  function sendCommand(method: string, params: any = {}, sessionId?: string): Promise<any> {
    return new Promise((resolve, reject) => {
      const id = reqId++;
      pendingRequests.set(id, { resolve, reject });
      const obj: any = { id, method, params };
      if (sessionId) obj.sessionId = sessionId;
      const payload = JSON.stringify(obj) + "\0";
      writeStream.write(payload, (err: any) => {
        if (err) {
          pendingRequests.delete(id);
          reject(err);
        }
      });
    });
  }

  // Execute CDP session setup
  let sessionId = "";
  try {
    const targetsRes = await sendCommand("Target.getTargets");
    const targetInfos = targetsRes.result?.targetInfos || [];
    const pageTarget = targetInfos.find((t: any) => t.type === "page");
    if (!pageTarget) {
      cleanupAndExit(4);
      return;
    }

    const attachRes = await sendCommand("Target.attachToTarget", {
      targetId: pageTarget.targetId,
      flatten: true,
    });
    sessionId = attachRes.result.sessionId;

    await sendCommand("Page.enable", {}, sessionId);
  } catch {
    cleanupAndExit(3);
    return;
  }

  const launchEnd = process.hrtime.bigint();
  const launchMs = Number(launchEnd - launchStart) / 1e6;

  const attachStart = process.hrtime.bigint();
  // Target attached
  const attachEnd = process.hrtime.bigint();
  const attachMs = Number(attachEnd - attachStart) / 1e6;

  const loadStart = process.hrtime.bigint();
  try {
    const loadPromise = new Promise<void>((resolve) => {
      onLoadEventFired = resolve;
    });

    const pageFileUrl = "file://" + absPagePath;
    const navRes = await sendCommand("Page.navigate", { url: pageFileUrl }, sessionId);
    if (navRes.error) {
      cleanupAndExit(4);
      return;
    }
    await loadPromise;
  } catch {
    cleanupAndExit(4);
    return;
  }
  const loadEnd = process.hrtime.bigint();
  const loadMs = Number(loadEnd - loadStart) / 1e6;

  // Settle helper
  async function settle() {
    const expr = `new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))`;
    await sendCommand(
      "Runtime.evaluate",
      { expression: expr, awaitPromise: true },
      sessionId
    );
  }

  // Capture helper
  const captureMsList: number[] = [];
  async function captureState() {
    const capStart = process.hrtime.bigint();
    await settle();

    const snapRes = await sendCommand(
      "DOMSnapshot.captureSnapshot",
      { computedStyles: [], includeDOMRects: true },
      sessionId
    );
    const axRes = await sendCommand("Accessibility.getFullAXTree", {}, sessionId);

    const doc0 = snapRes.result.documents[0];
    const strings: string[] = snapRes.result.strings;

    // Build AX map by backendDOMNodeId
    const axNodes = axRes.result?.nodes || [];
    const axMap = new Map<number, any>();
    for (const axNode of axNodes) {
      if (axNode.backendDOMNodeId !== undefined) {
        axMap.set(axNode.backendDOMNodeId, axNode);
      }
    }

    // Helper to get RareStringData
    function getRareString(rare: any, nodeIdx: number): string | undefined {
      if (!rare || !rare.index) return undefined;
      const pos = rare.index.indexOf(nodeIdx);
      if (pos === -1) return undefined;
      return strings[rare.value[pos]];
    }

    // Helper to get RareBooleanData
    function getRareBool(rare: any, nodeIdx: number): boolean {
      if (!rare || !rare.index) return false;
      return rare.index.includes(nodeIdx);
    }

    // Build layout map from nodeIndex to bounds
    const layoutNodeIndices: number[] = doc0.layout.nodeIndex;
    const layoutBounds: number[][] = doc0.layout.bounds;

    const layoutMap = new Map<number, number[]>();
    for (let lIdx = 0; lIdx < layoutNodeIndices.length; lIdx++) {
      const nodeIdx = layoutNodeIndices[lIdx];
      layoutMap.set(nodeIdx, layoutBounds[lIdx]);
    }

    const skippedTags = new Set([
      "html",
      "head",
      "script",
      "style",
      "meta",
      "link",
      "title",
      "noscript",
    ]);

    const emittedNodes: any[] = [];

    // Traverse nodes in snapshot document order
    for (let nodeIdx = 0; nodeIdx < doc0.nodes.nodeName.length; nodeIdx++) {
      if (!layoutMap.has(nodeIdx)) continue;

      const tagRaw = strings[doc0.nodes.nodeName[nodeIdx]];
      const tagLower = tagRaw.toLowerCase();

      if (skippedTags.has(tagLower) || tagLower.startsWith("#")) continue;

      const backendNodeId = doc0.nodes.backendNodeId[nodeIdx];
      const bounds = layoutMap.get(nodeIdx)!;
      const box = computeBox(bounds[0], bounds[1], bounds[2], bounds[3]);

      // Extract attributes
      const attrsRaw = doc0.nodes.attributes ? doc0.nodes.attributes[nodeIdx] : undefined;
      const attrs: Record<string, string> = {};
      if (attrsRaw) {
        for (let k = 0; k < attrsRaw.length; k += 2) {
          if (attrsRaw[k] !== -1) {
            attrs[strings[attrsRaw[k]]] = strings[attrsRaw[k + 1]];
          }
        }
      }

      const idAttr = attrs.id || "";
      const disabled = attrs.disabled !== undefined;

      // AX properties
      const axNode = axMap.get(backendNodeId);
      let role = "";
      let name = "";

      if (axNode && !axNode.ignored) {
        if (axNode.role && axNode.role.value) {
          role = axNode.role.value;
        }
        if (axNode.name && axNode.name.value !== undefined) {
          name = collapseWhitespaceAndTruncate(String(axNode.name.value));
        }
      }

      // Value property
      let value = "";
      if (tagLower === "input" || tagLower === "textarea" || tagLower === "select") {
        const rareVal = getRareString(doc0.nodes.inputValue, nodeIdx);
        if (rareVal !== undefined) {
          value = rareVal;
        } else if (attrs.value !== undefined) {
          value = attrs.value;
        }
      }

      // Checked property
      let checked = false;
      if (tagLower === "input") {
        const typeAttr = (attrs.type || "").toLowerCase();
        if (typeAttr === "checkbox" || typeAttr === "radio") {
          checked = getRareBool(doc0.nodes.inputChecked, nodeIdx);
        }
      }

      // Actionable
      const act = computeAct(box, disabled, tagLower, attrs, role);

      emittedNodes.push({
        i: emittedNodes.length,
        tag: tagLower,
        id: idAttr,
        role,
        name,
        value,
        checked,
        box,
        act,
      });
    }

    const capEnd = process.hrtime.bigint();
    captureMsList.push(Number(capEnd - capStart) / 1e6);
    return emittedNodes;
  }

  const states: any[] = [];

  // Initial capture step -1
  const initialNodes = await captureState();
  states.push({
    step: -1,
    op: "load",
    ok: true,
    error: null,
    nodes: initialNodes,
  });

  const stepsMsList: number[] = [];
  const attemptsList: number[] = [];

  // Helper for step target resolution
  async function resolveStepTarget(
    selector: string,
    timeoutMs: number
  ): Promise<{ nodeId: number; attempts: number } | null> {
    const docRes = await sendCommand("DOM.getDocument", { depth: 1 }, sessionId);
    const rootNodeId = docRes.result.root.nodeId;

    let spentMs = 0;
    let currentDelayIdx = 0;
    let attempts = 0;

    while (spentMs <= timeoutMs) {
      attempts++;
      try {
        const queryRes = await sendCommand(
          "DOM.querySelector",
          { nodeId: rootNodeId, selector },
          sessionId
        );
        const nodeId = queryRes.result?.nodeId || 0;

        if (nodeId !== 0) {
          const boxRes = await sendCommand(
            "DOM.getBoxModel",
            { nodeId },
            sessionId
          );
          if (boxRes.result && boxRes.result.model) {
            // Check disabled attribute
            const evalRes = await sendCommand(
              "DOM.describeNode",
              { nodeId },
              sessionId
            );
            const attrsList = evalRes.result?.node?.attributes || [];
            let isDisabled = false;
            for (let a = 0; a < attrsList.length; a += 2) {
              if (attrsList[a].toLowerCase() === "disabled") {
                isDisabled = true;
                break;
              }
            }
            if (!isDisabled) {
              return { nodeId, attempts };
            }
          }
        }
      } catch {}

      const delays = getBackoffDelays(timeoutMs);
      const delay = delays[Math.min(currentDelayIdx, delays.length - 1)];
      currentDelayIdx++;
      await new Promise((r) => setTimeout(r, delay));
      spentMs += delay;
    }

    return null;
  }

  // Execute steps
  for (let stepIdx = 0; stepIdx < stepsList.length; stepIdx++) {
    const step = stepsList[stepIdx];
    const op = step.op;
    const selector = step.selector;
    const timeoutMs = step.timeout_ms !== undefined ? step.timeout_ms : 2000;

    const stepStart = process.hrtime.bigint();
    const resolved = await resolveStepTarget(selector, timeoutMs);

    let ok = false;
    let errorStr: string | null = null;
    let attemptsCount = 1;

    if (!resolved) {
      ok = false;
      errorStr = "timeout";
      attemptsCount = Math.max(1, Math.ceil(timeoutMs / 64));
    } else {
      attemptsCount = resolved.attempts;
      const targetNodeId = resolved.nodeId;

      try {
        if (op === "click") {
          const boxRes = await sendCommand(
            "DOM.getBoxModel",
            { nodeId: targetNodeId },
            sessionId
          );
          const borderQuad = boxRes.result.model.border;
          const cx = Math.floor((borderQuad[0] + borderQuad[2] + borderQuad[4] + borderQuad[6]) / 4);
          const cy = Math.floor((borderQuad[1] + borderQuad[3] + borderQuad[5] + borderQuad[7]) / 4);

          await sendCommand(
            "Input.dispatchMouseEvent",
            { type: "mouseMoved", x: cx, y: cy, button: "left", clickCount: 1 },
            sessionId
          );
          await sendCommand(
            "Input.dispatchMouseEvent",
            { type: "mousePressed", x: cx, y: cy, button: "left", clickCount: 1 },
            sessionId
          );
          await sendCommand(
            "Input.dispatchMouseEvent",
            { type: "mouseReleased", x: cx, y: cy, button: "left", clickCount: 1 },
            sessionId
          );
          ok = true;
        } else if (op === "type") {
          await sendCommand("DOM.focus", { nodeId: targetNodeId }, sessionId);
          await sendCommand(
            "Input.insertText",
            { text: step.text || "" },
            sessionId
          );
          ok = true;
        } else if (op === "wait") {
          ok = true;
        }
      } catch {
        ok = false;
        errorStr = "timeout";
      }
    }

    const stepEnd = process.hrtime.bigint();
    stepsMsList.push(Number(stepEnd - stepStart) / 1e6);
    attemptsList.push(attemptsCount);

    const stepNodes = await captureState();
    states.push({
      step: stepIdx,
      op,
      ok,
      error: errorStr,
      nodes: stepNodes,
    });
  }

  // Write stdout output
  const outputDoc = {
    schema: "vqa.x2-state/v1",
    page: path.basename(absPagePath),
    viewport: [1280, 800],
    states,
  };

  const serializedStdout = canonicalSerialize(outputDoc) + "\n";
  process.stdout.write(serializedStdout);

  // Write timing file
  const x2Where = process.env.X2_WHERE || "unlabelled";
  const timingData = {
    lang: "ts",
    seed: "ts_w1",
    where: x2Where,
    launch_ms: Math.round(launchMs * 1000) / 1000,
    attach_ms: Math.round(attachMs * 1000) / 1000,
    load_ms: Math.round(loadMs * 1000) / 1000,
    capture_ms: captureMsList.map((m) => Math.round(m * 1000) / 1000),
    steps_ms: stepsMsList.map((m) => Math.round(m * 1000) / 1000),
    attempts: attemptsList,
  };

  try {
    fs.mkdirSync(path.dirname(timingPath), { recursive: true });
    fs.writeFileSync(timingPath, JSON.stringify(timingData, null, 2));
  } catch {}

  clearTimeout(globalTimeoutTimer);
  cleanupAndExit(0);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch(() => process.exit(3));
}
