import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import {
  canonicalJson,
  computeClosedBox,
  computeBackoffSequence,
  isActionable,
  parseFramedChunk
} from "./x2.ts";

// ---------------------------------------------------------------------------
// Pure unit tests
// ---------------------------------------------------------------------------

test("NUL framing reader - message split byte by byte", () => {
  const pendingIds = new Set([1]);
  const jsonStr = JSON.stringify({ id: 1, result: { ok: true } }) + "\0";
  const fullBuf = Buffer.from(jsonStr, "utf8");

  let buf = Buffer.alloc(0);
  const collectedMsgs: any[] = [];

  for (let i = 0; i < fullBuf.length; i++) {
    const chunk = fullBuf.subarray(i, i + 1);
    const res = parseFramedChunk(buf, chunk, false, pendingIds);
    assert.equal(res.exitCode, undefined);
    buf = res.newBuffer;
    if (res.messages.length > 0) {
      collectedMsgs.push(...res.messages);
    }
  }

  assert.equal(collectedMsgs.length, 1);
  assert.deepEqual(collectedMsgs[0], { id: 1, result: { ok: true } });
  assert.equal(buf.length, 0);
});

test("NUL framing reader - three messages in one chunk", () => {
  const pendingIds = new Set([1, 2, 3]);
  const msg1 = JSON.stringify({ id: 1, result: { a: 1 } });
  const msg2 = JSON.stringify({ id: 2, result: { b: 2 } });
  const msg3 = JSON.stringify({ id: 3, result: { c: 3 } });
  const chunk = Buffer.from(`${msg1}\0${msg2}\0${msg3}\0`, "utf8");

  const res = parseFramedChunk(Buffer.alloc(0), chunk, false, pendingIds);
  assert.equal(res.exitCode, undefined);
  assert.equal(res.messages.length, 3);
  assert.equal(res.messages[0].id, 1);
  assert.equal(res.messages[1].id, 2);
  assert.equal(res.messages[2].id, 3);
  assert.equal(res.newBuffer.length, 0);
});

test("NUL framing reader - torn tail at EOF gives exit code 6", () => {
  const pendingIds = new Set([1]);
  const partialChunk = Buffer.from('{"id": 1, "result":', "utf8");

  const res = parseFramedChunk(Buffer.alloc(0), partialChunk, true, pendingIds);
  assert.equal(res.exitCode, 6);
});

test("NUL framing reader - unknown response id gives exit code 7", () => {
  const pendingIds = new Set([1]);
  const msg = JSON.stringify({ id: 99, result: { ok: true } }) + "\0";
  const chunk = Buffer.from(msg, "utf8");

  const res = parseFramedChunk(Buffer.alloc(0), chunk, false, pendingIds);
  assert.equal(res.exitCode, 7);
});

test("Canonical serializer table", () => {
  const table: Array<{ input: unknown; expected: string }> = [
    { input: { b: 1, a: 2 }, expected: '{"a":2,"b":1}' },
    { input: { text: "hello / world" }, expected: '{"text":"hello / world"}' },
    { input: { accent: "á" }, expected: '{"accent":"\\u00e1"}' },
    { input: { emoji: "😀" }, expected: '{"emoji":"\\ud83d\\ude00"}' },
    { input: { ctrl: "\x01\x08\x0a\x0d\x09\x7f" }, expected: '{"ctrl":"\\u0001\\b\\n\\r\\t\\u007f"}' },
    { input: [1, "test", true, null], expected: '[1,"test",true,null]' }
  ];

  for (const item of table) {
    const result = canonicalJson(item.input);
    assert.equal(result, item.expected);
  }
});

test("Box rule on fractional, zero-size, and negative coordinates", () => {
  // Fractional coords
  assert.deepEqual(computeClosedBox(10.2, 20.8, 100.5, 50.1), [10, 20, 110, 70]);

  // Zero-size coords
  assert.equal(computeClosedBox(10, 20, 0, 50), null);
  assert.equal(computeClosedBox(10, 20, 50, 0), null);

  // Negative size/coords
  assert.equal(computeClosedBox(10, 20, -5, 10), null);
  assert.deepEqual(computeClosedBox(-10.5, -20.2, 30.1, 40.5), [-11, -21, 19, 20]);
});

test("Backoff sequence", () => {
  const seq = computeBackoffSequence(200);
  assert.deepEqual(seq, [1, 2, 4, 8, 16, 32, 64, 64, 9]);
});

test("Actionable rule on synthetic records", () => {
  // Valid box required
  assert.equal(isActionable({ box: null, tag: "button", role: "" }), false);

  // Disabled element not actionable
  assert.equal(isActionable({ box: [0, 0, 10, 10], disabled: true, tag: "button", role: "" }), false);

  // Actionable tags
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "button", role: "" }), true);
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "select", role: "" }), true);
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "textarea", role: "" }), true);
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "summary", role: "" }), true);

  // Anchor with/without href
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "a", href: "http://x", role: "" }), true);
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "a", role: "" }), false);

  // Input type hidden vs other
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "input", type: "text", role: "" }), true);
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "input", type: "hidden", role: "" }), false);

  // Actionable roles
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "div", role: "button" }), true);
  assert.equal(isActionable({ box: [0, 0, 10, 10], tag: "div", role: "generic" }), false);
});

// ---------------------------------------------------------------------------
// Browser integration tests
// ---------------------------------------------------------------------------

test("Browser integration test", async (t) => {
  const chromeBin = process.env.CHROME_BIN;
  if (!chromeBin) {
    t.skip("SKIP CHROME_BIN unset");
    return;
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "x2-test-run-"));

  try {
    const htmlPath = path.join(tmpDir, "page.html");
    const stepsPath = path.join(tmpDir, "steps.json");
    const timingPath1 = path.join(tmpDir, "timing1.json");
    const timingPath2 = path.join(tmpDir, "timing2.json");

    fs.writeFileSync(htmlPath, `<!DOCTYPE html>
<html>
<head><title>Test Page</title><style>body { margin: 0; }</style></head>
<body>
  <input id="my-input" type="text" value="" />
  <button id="my-button" onclick="document.getElementById('out').textContent = 'Clicked!'">Click</button>
  <div id="out"></div>
  <script>
    setTimeout(() => {
      const el = document.createElement('div');
      el.id = 'late-element';
      el.textContent = 'Late Arrival';
      document.body.appendChild(el);
    }, 1200);
  </script>
</body>
</html>`);

    fs.writeFileSync(stepsPath, JSON.stringify({
      steps: [
        { op: "type", selector: "#my-input", text: "Hello Agent" },
        { op: "click", selector: "#my-button" },
        { op: "wait", selector: "#late-element", timeout_ms: 3000 }
      ]
    }));

    const x2ScriptPath = fileURLToPath(new URL("./x2.ts", import.meta.url));

    // Run 1
    const stdout1 = execFileSync(process.execPath, [
      "--experimental-strip-types",
      x2ScriptPath,
      "--page", htmlPath,
      "--steps", stepsPath,
      "--timing", timingPath1,
      "--timeout-s", "30"
    ], { env: process.env }).toString("utf8");

    // Run 2 (for byte-equal stdout check)
    const stdout2 = execFileSync(process.execPath, [
      "--experimental-strip-types",
      x2ScriptPath,
      "--page", htmlPath,
      "--steps", stepsPath,
      "--timing", timingPath2,
      "--timeout-s", "30"
    ], { env: process.env }).toString("utf8");

    // Byte-equal check across runs
    assert.equal(stdout1, stdout2);

    const doc = JSON.parse(stdout1);
    assert.equal(doc.schema, "vqa.x2-state/v1");
    assert.equal(doc.page, "page.html");
    assert.deepEqual(doc.viewport, [1280, 800]);

    // Should have 4 states: load (step -1), type (step 0), click (step 1), wait (step 2)
    assert.equal(doc.states.length, 4);

    // Initial state
    assert.equal(doc.states[0].step, -1);
    assert.equal(doc.states[0].op, "load");
    assert.equal(doc.states[0].ok, true);

    // State after type
    const stateType = doc.states[1];
    assert.equal(stateType.step, 0);
    assert.equal(stateType.op, "type");
    assert.equal(stateType.ok, true);
    const inputNode = stateType.nodes.find((n: any) => n.id === "my-input");
    assert.ok(inputNode);
    assert.equal(inputNode.value, "Hello Agent");

    // State after click
    const stateClick = doc.states[2];
    assert.equal(stateClick.step, 1);
    assert.equal(stateClick.op, "click");
    assert.equal(stateClick.ok, true);
    const outNode = stateClick.nodes.find((n: any) => n.id === "out");
    assert.ok(outNode);
    assert.equal(outNode.name, "Clicked!");

    // Late element check
    const initialLate = doc.states[0].nodes.find((n: any) => n.id === "late-element");
    assert.equal(initialLate, undefined);

    const stateWait = doc.states[3];
    assert.equal(stateWait.step, 2);
    assert.equal(stateWait.op, "wait");
    assert.equal(stateWait.ok, true);
    const lateNode = stateWait.nodes.find((n: any) => n.id === "late-element");
    assert.ok(lateNode);
    assert.equal(lateNode.name, "Late Arrival");

  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
