// Run: node --test js/tests   (node:test + node:assert only, no npm deps)
const test = require("node:test");
const assert = require("node:assert");
const { toInt, rectToBox, wenderViewport } = require("../inpage/viewport.js");

test("toInt rounds and maps non-finite to 0", () => {
  assert.strictEqual(toInt(2.6), 3);
  assert.strictEqual(toInt("x"), 0);
  assert.strictEqual(toInt(Infinity), 0);
});

test("rectToBox emits integer [x,y,w,h] and clamps negative size", () => {
  assert.deepStrictEqual(rectToBox({ x: 1.4, y: 2.5, width: 10.2, height: -3 }), [1, 3, 10, 0]);
  assert.strictEqual(rectToBox(null), null);
});

test("wenderViewport is integer-only", () => {
  const win = { innerWidth: 800, innerHeight: 600, scrollX: 0, scrollY: 12.7, devicePixelRatio: 1.5,
    document: { documentElement: { scrollWidth: 800, scrollHeight: 1600 } } };
  const v = wenderViewport(win);
  assert.deepStrictEqual(v, { w: 800, h: 600, scroll_x: 0, scroll_y: 13, doc_w: 800, doc_h: 1600, dpr_ppm: 1500000 });
  for (const k of Object.keys(v)) assert.ok(Number.isInteger(v[k]), k);
});
