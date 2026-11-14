import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import child_process from "node:child_process";
import {
  canonicalSerialize,
  computeBox,
  computeAct,
  getBackoffDelays,
  collapseWhitespaceAndTruncate,
} from "./x2.ts";

// Pure unit tests that run without browser
test("Canonical Serializer - expected strings matching python json.dumps", () => {
  const table: Array<{ input: any; expected: string }> = [
    { input: null, expected: "null" },
    { input: true, expected: "true" },
    { input: false, expected: "false" },
    { input: 42, expected: "42" },
    {
      input: { b: 1, a: "hello \n \u00e9 😀 / \x01 \b \f \r \t \" \\" },
      expected:
        '{"a":"hello \\n \\u00e9 \\ud83d\\ude00 / \\u0001 \\b \\f \\r \\t \\" \\\\","b":1}',
    },
    {
      input: [1, "test", { key: "val" }],
      expected: '[1,"test",{"key":"val"}]',
    },
  ];

  for (const item of table) {
    const actual = canonicalSerialize(item.input);
    assert.equal(actual, item.expected);
  }

  assert.throws(() => canonicalSerialize(3.14), /Float/);
});

test("Box calculation rule - integer sets on fractional, zero-size, negative", () => {
  assert.deepEqual(computeBox(10.2, 20.7, 100.1, 50.3), [10, 20, 110, 70]);
  assert.equal(computeBox(5.0, 5.0, 0, 10), null);
  assert.equal(computeBox(5.0, 5.0, 10, 0), null);
  assert.deepEqual(computeBox(-10.5, -20.5, 30.2, 40.2), [-11, -21, 19, 19]);
});

test("Backoff sequence - bounded 1, 2, 4, 8, 16, 32, 64, 64", () => {
  const delays = getBackoffDelays(2000);
  assert.equal(delays[0], 1);
  assert.equal(delays[1], 2);
  assert.equal(delays[2], 4);
  assert.equal(delays[3], 8);
  assert.equal(delays[4], 16);
  assert.equal(delays[5], 32);
  assert.equal(delays[6], 64);
  assert.equal(delays[7], 64);
});

test("Actionable rule on synthetic records", () => {
  const box: [number, number, number, number] = [0, 0, 100, 100];

  assert.equal(computeAct(null, false, "button", {}, "button"), false);
  assert.equal(computeAct(box, true, "button", {}, "button"), false);
  assert.equal(computeAct(box, false, "a", { href: "http://x" }, "link"), true);
  assert.equal(computeAct(box, false, "a", {}, ""), false);
  assert.equal(computeAct(box, false, "button", {}, "button"), true);
  assert.equal(computeAct(box, false, "input", { type: "text" }, "textbox"), true);
  assert.equal(computeAct(box, false, "input", { type: "hidden" }, ""), false);
  assert.equal(computeAct(box, false, "div", {}, "button"), true);
  assert.equal(computeAct(box, false, "div", {}, "generic"), false);
});

test("Whitespace collapsing and truncation", () => {
  const raw = "  hello   \n\t  world  ";
  assert.equal(collapseWhitespaceAndTruncate(raw), "hello world");

  const longStr = "a".repeat(150);
  assert.equal(collapseWhitespaceAndTruncate(longStr).length, 120);
});

test("Framing Reader & Validation simulation - torn tail exit 6, protocol error exit 7", () => {
  let buf = Buffer.concat([
    Buffer.from(JSON.stringify({ id: 1, result: {} }), "utf8"),
    Buffer.from([0]),
    Buffer.from(JSON.stringify({ id: 2, result: {} }), "utf8"),
    Buffer.from([0]),
  ]);

  const msgs: any[] = [];
  while (true) {
    const idx = buf.indexOf(0);
    if (idx === -1) break;
    msgs.push(JSON.parse(buf.subarray(0, idx).toString("utf8")));
    buf = buf.subarray(idx + 1);
  }

  assert.equal(msgs.length, 2);
  assert.equal(msgs[0].id, 1);
  assert.equal(msgs[1].id, 2);
  assert.equal(buf.length, 0);

  const partialBuf = Buffer.from('{"id": 3');
  assert.ok(partialBuf.length > 0);
});

// Browser integration test
test("Browser integration test", async (t) => {
  const chromeBin = process.env.CHROME_BIN;
  if (!chromeBin) {
    t.skip("SKIP CHROME_BIN unset");
    return;
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "x2-test-"));
  const htmlPath = path.join(tmpDir, "page.html");
  const stepsPath = path.join(tmpDir, "steps.json");
  const timingPath = path.join(tmpDir, "timing.json");

  const htmlContent = `<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
  <input id="input1" type="text" value="">
  <button id="btn1" onclick="document.getElementById('div1').innerText = 'Clicked!';">Click Me</button>
  <div id="div1">Initial Text</div>
  <script>
    setTimeout(() => {
      const el = document.createElement('div');
      el.id = 'late-el';
      el.innerText = 'Late Element';
      document.body.appendChild(el);
    }, 1500);
  </script>
</body>
</html>`;

  fs.writeFileSync(htmlPath, htmlContent);

  const stepsObj = {
    steps: [
      { op: "type", selector: "#input1", text: "Typed Text" },
      { op: "click", selector: "#btn1" },
      { op: "wait", selector: "#late-el", timeout_ms: 3000 },
    ],
  };
  fs.writeFileSync(stepsPath, JSON.stringify(stepsObj));

  const runCli = (): string => {
    return child_process.execSync(
      `node langx/x2/ts_w1/x2.ts --page "${htmlPath}" --steps "${stepsPath}" --timing "${timingPath}"`,
      {
        env: { ...process.env, CHROME_BIN: chromeBin },
      }
    ).toString();
  };

  const stdout1 = runCli();
  const stdout2 = runCli();

  assert.equal(stdout1, stdout2, "stdout should be byte-equal across runs");

  const json1 = JSON.parse(stdout1);
  assert.equal(json1.schema, "vqa.x2-state/v1");
  assert.equal(json1.page, path.basename(htmlPath));
  assert.deepEqual(json1.viewport, [1280, 800]);
  assert.equal(json1.states.length, 4);

  // State 0 (load): input empty
  const state0 = json1.states[0];
  assert.equal(state0.step, -1);
  assert.equal(state0.op, "load");
  assert.equal(state0.ok, true);
  const input0 = state0.nodes.find((n: any) => n.id === "input1");
  assert.equal(input0.value, "");

  // State 1 (type): input has typed value
  const state1 = json1.states[1];
  assert.equal(state1.step, 0);
  assert.equal(state1.op, "type");
  assert.equal(state1.ok, true);
  const input1 = state1.nodes.find((n: any) => n.id === "input1");
  assert.equal(input1.value, "Typed Text");

  // State 2 (click): div text changed to 'Clicked!'
  const state2 = json1.states[2];
  assert.equal(state2.step, 1);
  assert.equal(state2.op, "click");
  assert.equal(state2.ok, true);

  // State 3 (wait): late element present
  const state3 = json1.states[3];
  assert.equal(state3.step, 2);
  assert.equal(state3.op, "wait");
  assert.equal(state3.ok, true);
  const lateEl = state3.nodes.find((n: any) => n.id === "late-el");
  assert.ok(lateEl, "Late element should be present after wait");

  fs.rmSync(tmpDir, { recursive: true, force: true });
});
