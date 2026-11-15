import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawnSync } from "node:child_process";
import {
  FramingReader,
  serializeCanonical,
  computeBox,
  computeBackoffDelays,
  isActionable
} from "./x2.ts";

test("NUL framing reader - message split byte by byte", () => {
  const reader = new FramingReader();
  const msgStr = JSON.stringify({ id: 1, result: { ok: true } });
  const fullBuf = Buffer.from(msgStr + "\0", "utf8");

  const received: string[] = [];
  for (let i = 0; i < fullBuf.length; i++) {
    const chunk = fullBuf.subarray(i, i + 1);
    const msgs = reader.push(chunk);
    received.push(...msgs);
  }

  assert.equal(received.length, 1);
  assert.equal(received[0], msgStr);
  assert.equal(reader.hasPartial(), false);
});

test("NUL framing reader - three messages in one chunk", () => {
  const reader = new FramingReader();
  const m1 = JSON.stringify({ id: 1 });
  const m2 = JSON.stringify({ id: 2 });
  const m3 = JSON.stringify({ id: 3 });
  const chunk = Buffer.from(m1 + "\0" + m2 + "\0" + m3 + "\0", "utf8");

  const msgs = reader.push(chunk);
  assert.deepEqual(msgs, [m1, m2, m3]);
  assert.equal(reader.hasPartial(), false);
});

test("NUL framing reader - torn tail at end giving exit code 6", () => {
  const reader = new FramingReader();
  const chunk = Buffer.from('{"id": 1}\0{"id": 2, "partial": tr', "utf8");
  const msgs = reader.push(chunk);
  assert.equal(msgs.length, 1);
  assert.equal(reader.hasPartial(), true);

  // Integration test with process exit code 6
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mock-chrome-"));
  try {
    const mockChromePath = path.join(tmpDir, "mock-chrome.js");
    fs.writeFileSync(mockChromePath, `#!/usr/bin/env node
const fs = require("fs");
const writePipe = fs.createWriteStream(null, { fd: 4 });
writePipe.write('{"id": 1, "result":');
setTimeout(() => process.exit(0), 100);
`, { mode: 0o755 });

    const htmlPath = path.join(tmpDir, "page.html");
    fs.writeFileSync(htmlPath, "<html></html>");
    const stepsPath = path.join(tmpDir, "steps.json");
    fs.writeFileSync(stepsPath, JSON.stringify({ steps: [] }));
    const timingPath = path.join(tmpDir, "timing.json");

    const cliPath = path.resolve("langx/x2/ts_w2/x2.ts");
    const env = { ...process.env, CHROME_BIN: mockChromePath };

    const res = spawnSync(process.execPath, [
      cliPath,
      "--page", htmlPath,
      "--steps", stepsPath,
      "--timing", timingPath
    ], { encoding: "utf8", env });

    assert.equal(res.status, 6);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("Unknown response id / protocol error gives exit code 7", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mock-chrome-"));
  try {
    const mockChromePath = path.join(tmpDir, "mock-chrome.js");
    fs.writeFileSync(mockChromePath, `#!/usr/bin/env node
const fs = require("fs");
const writePipe = fs.createWriteStream(null, { fd: 4 });
writePipe.write(JSON.stringify({ id: 999, result: {} }) + "\\0");
setTimeout(() => process.exit(0), 100);
`, { mode: 0o755 });

    const htmlPath = path.join(tmpDir, "page.html");
    fs.writeFileSync(htmlPath, "<html></html>");
    const stepsPath = path.join(tmpDir, "steps.json");
    fs.writeFileSync(stepsPath, JSON.stringify({ steps: [] }));
    const timingPath = path.join(tmpDir, "timing.json");

    const cliPath = path.resolve("langx/x2/ts_w2/x2.ts");
    const env = { ...process.env, CHROME_BIN: mockChromePath };

    const res = spawnSync(process.execPath, [
      cliPath,
      "--page", htmlPath,
      "--steps", stepsPath,
      "--timing", timingPath
    ], { encoding: "utf8", env });

    assert.equal(res.status, 7);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("Canonical serializer table", () => {
  const table = [
    {
      input: { b: "hello\nworld", a: "Café 😃", c: [1, 2, 3] },
      expected: '{"a":"Caf\\u00e9 \\ud83d\\ude03","b":"hello\\nworld","c":[1,2,3]}'
    },
    {
      input: { z: "a/b\\c\"d", a: "\x01\x02\b\f\n\r\t\x1f" },
      expected: '{"a":"\\u0001\\u0002\\b\\f\\n\\r\\t\\u001f","z":"a/b\\\\c\\"d"}'
    },
    {
      input: { control: "\x00\x1b", emoji: "😃", unicode: "Café" },
      expected: '{"control":"\\u0000\\u001b","emoji":"\\ud83d\\ude03","unicode":"Caf\\u00e9"}'
    },
    {
      input: { list: [1, -2, true, false, null, "str"] },
      expected: '{"list":[1,-2,true,false,null,"str"]}'
    }
  ];

  for (const tc of table) {
    const actual = serializeCanonical(tc.input);
    assert.equal(actual, tc.expected);
  }
});

test("Box rule on fractional, zero-size and negative coordinates", () => {
  // Zero size
  assert.equal(computeBox(10.5, 20.2, 0, 10), null);
  assert.equal(computeBox(10.5, 20.2, 10, 0), null);
  assert.equal(computeBox(10.5, 20.2, -5, 10), null);

  // Fractional box
  // x=10.2, y=20.7, w=100.4, h=50.1
  // x0 = floor(10.2) = 10
  // y0 = floor(20.7) = 20
  // x1 = max(10, ceil(10.2 + 100.4 - 1)) = max(10, ceil(109.6)) = 110
  // y1 = max(20, ceil(20.7 + 50.1 - 1)) = max(20, ceil(69.8)) = 70
  assert.deepEqual(computeBox(10.2, 20.7, 100.4, 50.1), [10, 20, 110, 70]);

  // Negative coordinates
  // x=-10.5, y=-5.2, w=20, h=10
  // x0 = floor(-10.5) = -11
  // y0 = floor(-5.2) = -6
  // x1 = max(-11, ceil(-10.5 + 20 - 1)) = max(-11, ceil(8.5)) = 9
  // y1 = max(-6, ceil(-5.2 + 10 - 1)) = max(-6, ceil(3.8)) = 4
  assert.deepEqual(computeBox(-10.5, -5.2, 20, 10), [-11, -6, 9, 4]);
});

test("Backoff sequence 1, 2, 4, 8, 16, 32, 64, 64", () => {
  const delays = computeBackoffDelays(2000);
  assert.equal(delays[0], 1);
  assert.equal(delays[1], 2);
  assert.equal(delays[2], 4);
  assert.equal(delays[3], 8);
  assert.equal(delays[4], 16);
  assert.equal(delays[5], 32);
  assert.equal(delays[6], 64);
  assert.equal(delays[7], 64);

  const sum = delays.reduce((a, b) => a + b, 0);
  assert.equal(sum, 2000);
});

test("Actionable rule on synthetic records", () => {
  const box: [number, number, number, number] = [0, 0, 100, 30];

  // Null box -> false
  assert.equal(isActionable(null, false, "button", false, null, "button"), false);

  // Disabled -> false
  assert.equal(isActionable(box, true, "button", false, null, "button"), false);

  // Tag a with href -> true, without href -> false
  assert.equal(isActionable(box, false, "a", true, null, ""), true);
  assert.equal(isActionable(box, false, "a", false, null, ""), false);

  // Buttons, select, textarea, summary -> true
  assert.equal(isActionable(box, false, "button", false, null, ""), true);
  assert.equal(isActionable(box, false, "select", false, null, ""), true);
  assert.equal(isActionable(box, false, "textarea", false, null, ""), true);
  assert.equal(isActionable(box, false, "summary", false, null, ""), true);

  // Input non-hidden -> true, hidden -> false
  assert.equal(isActionable(box, false, "input", false, "text", ""), true);
  assert.equal(isActionable(box, false, "input", false, "hidden", ""), false);

  // Roles
  assert.equal(isActionable(box, false, "div", false, null, "button"), true);
  assert.equal(isActionable(box, false, "div", false, null, "textbox"), true);
  assert.equal(isActionable(box, false, "div", false, null, "generic"), false);
});

test("Browser tests", (t) => {
  const chromeBin = process.env.CHROME_BIN;
  if (!chromeBin) {
    t.skip("SKIP CHROME_BIN unset");
    return;
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "x2-test-browser-"));

  try {
    const htmlPath = path.join(tmpDir, "page.html");
    const htmlContent = `<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <input id="my-input" type="text" value="" />
  <input id="my-chk" type="checkbox" checked />
  <button id="my-btn">Click Me</button>
  <div id="output">Initial</div>
  <script>
    document.getElementById("my-btn").addEventListener("click", () => {
      document.getElementById("output").innerText = "Clicked!";
    });
    setTimeout(() => {
      const p = document.createElement("p");
      p.id = "late-elem";
      p.innerText = "Late Element";
      document.body.appendChild(p);
    }, 1500);
  </script>
</body>
</html>`;
    fs.writeFileSync(htmlPath, htmlContent, "utf8");

    const stepsPath = path.join(tmpDir, "steps.json");
    const stepsData = {
      steps: [
        { op: "type", selector: "#my-input", text: "Hello X2" },
        { op: "click", selector: "#my-btn" },
        { op: "wait", selector: "#late-elem", timeout_ms: 3000 }
      ]
    };
    fs.writeFileSync(stepsPath, JSON.stringify(stepsData), "utf8");

    const timingPath1 = path.join(tmpDir, "timing1.json");
    const timingPath2 = path.join(tmpDir, "timing2.json");

    const cliPath = path.resolve("langx/x2/ts_w2/x2.ts");

    // First run
    const run1 = spawnSync(process.execPath, [
      cliPath,
      "--page", htmlPath,
      "--steps", stepsPath,
      "--timing", timingPath1
    ], { encoding: "utf8", env: process.env });

    assert.equal(run1.status, 0, `Run 1 failed stderr: ${run1.stderr}`);

    // Second run
    const run2 = spawnSync(process.execPath, [
      cliPath,
      "--page", htmlPath,
      "--steps", stepsPath,
      "--timing", timingPath2
    ], { encoding: "utf8", env: process.env });

    assert.equal(run2.status, 0, `Run 2 failed stderr: ${run2.stderr}`);

    // Assert byte-equal stdout
    assert.equal(run1.stdout, run2.stdout, "Outputs across two runs must be byte-equal");

    // Assert schema & states
    const outObj = JSON.parse(run1.stdout);
    assert.equal(outObj.schema, "vqa.x2-state/v1");
    assert.equal(outObj.page, "page.html");
    assert.deepEqual(outObj.viewport, [1280, 800]);
    assert.equal(outObj.states.length, 4);

    // Initial state (step -1)
    const stateLoad = outObj.states[0];
    assert.equal(stateLoad.step, -1);
    assert.equal(stateLoad.op, "load");
    assert.equal(stateLoad.ok, true);

    const chkNode = stateLoad.nodes.find((n: any) => n.id === "my-chk");
    assert.ok(chkNode);
    assert.equal(chkNode.checked, true, "Checked checkbox must have checked: true");

    // Step 0: type
    const stateType = outObj.states[1];
    assert.equal(stateType.step, 0);
    assert.equal(stateType.op, "type");
    assert.equal(stateType.ok, true);
    const inputNode = stateType.nodes.find((n: any) => n.id === "my-input");
    assert.ok(inputNode);
    assert.equal(inputNode.value, "Hello X2");

    // Step 1: click
    const stateClick = outObj.states[2];
    assert.equal(stateClick.step, 1);
    assert.equal(stateClick.op, "click");
    assert.equal(stateClick.ok, true);

    // Step 2: wait
    const stateWait = outObj.states[3];
    assert.equal(stateWait.step, 2);
    assert.equal(stateWait.op, "wait");
    assert.equal(stateWait.ok, true);
    const lateNode = stateWait.nodes.find((n: any) => n.id === "late-elem");
    assert.ok(lateNode);

    // Late node must NOT be present in initial state
    const lateNodeInInitial = stateLoad.nodes.find((n: any) => n.id === "late-elem");
    assert.equal(lateNodeInInitial, undefined);

  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
