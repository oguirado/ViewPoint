// @module {"id":"wa.js.inpage.extract","layer":0,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}

const test = require("node:test");
const assert = require("node:assert/strict");

let extract;
let loadFailed = false;

try {
  extract = require("../inpage/extract.js");
} catch (err) {
  if (process.env.WA1_PIN_ALLOW_SKIP === "1") {
    loadFailed = true;
  } else {
    throw err;
  }
}

function registerTest(name, fn) {
  test(name, { skip: loadFailed ? "wa1-spark-inpage-extract" : false }, fn);
}

function element(tag, children = [], rect = {}) {
  const el = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    children,
    getBoundingClientRect: () => ({ x: 0, y: 0, width: 20, height: 10, ...rect }),
    contains(hit) {
      if (hit === el) return true;
      for (const child of children) {
        if (child === hit || (child && typeof child.contains === "function" && child.contains(hit))) {
          return true;
        }
      }
      return false;
    },
  };
  return el;
}

function document(...children) {
  return { nodeType: 9, children };
}

// 1. Attack list item: shadow root inside an iframe inside a shadow root
registerTest("shadow root inside an iframe inside a shadow root", () => {
  // Kills mutant: failing to descend into a shadow root inside an iframe inside a shadow root, or failing to accumulate iframe offsets through shadow roots.
  const button = element("button", [], { x: 1, y: 1, width: 10, height: 10 });
  const host2 = element("host2", [], { x: 2, y: 2, width: 30, height: 30 });
  host2.shadowRoot = { children: [button] };

  const iframe = element("iframe", [], { x: 5, y: 5, width: 50, height: 50 });
  iframe.contentDocument = document(host2);

  const host1 = element("host1", [], { x: 10, y: 10, width: 100, height: 100 });
  host1.shadowRoot = { children: [iframe] };

  const root = document(host1);
  const result = extract.walkComposed(root);

  assert.deepEqual(
    result.elements.map((e) => e.tag),
    ["host1", "iframe", "host2", "button"]
  );
  // host1 offset: 0,0 -> box [10, 10, 100, 100]
  // iframe offset: 0,0 -> box [5, 5, 50, 50]
  // iframe contentDocument offset accumulates iframe rect (x+5, y+5)
  // host2 box: [5+2, 5+2, 30, 30] = [7, 7, 30, 30]
  // button box inside host2 shadowRoot: [5+1, 5+1, 10, 10] = [6, 6, 10, 10]
  assert.deepEqual(
    result.elements.map((e) => e.box),
    [
      [10, 10, 100, 100],
      [5, 5, 50, 50],
      [7, 7, 30, 30],
      [6, 6, 10, 10],
    ]
  );
});

// 2. Attack list item: iframe whose contentDocument getter throws
registerTest("iframe whose contentDocument getter throws", () => {
  // Kills mutant: failing to catch throwing contentDocument getter, resulting in uncaught exception or incorrect cross_origin frame classification.
  const throwingIframe = element("iframe");
  Object.defineProperty(throwingIframe, "contentDocument", {
    get() {
      throw new Error("SecurityError: Blocked frame from accessing cross-origin frame.");
    },
  });

  const root = document(throwingIframe);
  const result = extract.walkComposed(root);

  assert.equal(result.cross_origin, 1);
  assert.equal(result.frames.length, 1);
  assert.equal(result.frames[0].cross_origin, 1);
  assert.deepEqual(
    result.elements.map((e) => e.tag),
    ["iframe"]
  );
});

// 3. Attack list item: display contents
registerTest("display contents handling", () => {
  // Kills mutant: excluding display: contents elements in walkComposed or incorrectly treating display: contents as hidden in isVisible when rect/children exist.
  const contentsEl = element("div", [element("span")], { x: 0, y: 0, width: 20, height: 10 });
  const win = {
    innerWidth: 100,
    innerHeight: 100,
    getComputedStyle: () => ({ display: "contents", visibility: "visible", opacity: "1" }),
  };

  assert.equal(extract.isVisible(contentsEl, win), true);

  const root = document(contentsEl);
  const result = extract.walkComposed(root);
  assert.deepEqual(
    result.elements.map((e) => e.tag),
    ["div", "span"]
  );
});

// 4. Attack list item: opacity as the string 0.0
registerTest("opacity as string 0.0", () => {
  // Kills mutant: using strict equality like style.opacity === 0 or style.opacity === "0" that fails to convert string "0.0" to number 0.
  const el = element("button", [], { x: 0, y: 0, width: 20, height: 10 });
  const win00 = {
    innerWidth: 100,
    innerHeight: 100,
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "0.0" }),
  };
  assert.equal(extract.isVisible(el, win00), false);

  const win0 = {
    innerWidth: 100,
    innerHeight: 100,
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "0" }),
  };
  assert.equal(extract.isVisible(el, win0), false);
});

// 5. Attack list item: a rect touching the viewport edge
registerTest("a rect touching the viewport edge", () => {
  // Kills mutant: using <= or >= for viewport boundary checks instead of strictly < and > in isVisible.
  const win = { innerWidth: 100, innerHeight: 100, getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }) };

  // Touching right edge (x = 100) -> outside
  assert.equal(extract.isVisible(element("btn", [], { x: 100, y: 0, width: 10, height: 10 }), win), false);
  // Touching bottom edge (y = 100) -> outside
  assert.equal(extract.isVisible(element("btn", [], { x: 0, y: 100, width: 10, height: 10 }), win), false);
  // Touching left edge (x + width = 0) -> outside
  assert.equal(extract.isVisible(element("btn", [], { x: -10, y: 0, width: 10, height: 10 }), win), false);
  // Touching top edge (y + height = 0) -> outside
  assert.equal(extract.isVisible(element("btn", [], { x: 0, y: -10, width: 10, height: 10 }), win), false);

  // Partially inside edges
  assert.equal(extract.isVisible(element("btn", [], { x: 99, y: 0, width: 10, height: 10 }), win), true);
  assert.equal(extract.isVisible(element("btn", [], { x: -9, y: 0, width: 10, height: 10 }), win), true);
});

// 6. Attack list item: fractional rects rounding
registerTest("fractional rects rounding", () => {
  // Kills mutant: using Math.floor or Math.trunc instead of Math.round / toInt for fractional bounding rect coordinates and negative size clamping.
  const el = element("div", [], { x: 1.4, y: 2.5, width: 10.6, height: -3.2 });
  const root = document(el);
  const result = extract.walkComposed(root);

  // x: 1.4 -> 1, y: 2.5 -> 3, width: 10.6 -> 11, height: -3.2 -> clamped to 0
  assert.deepEqual(result.elements[0].box, [1, 3, 11, 0]);
});

// 7. Attack list item: elementFromPoint returning null
registerTest("elementFromPoint returning null", () => {
  // Kills mutant: treating elementFromPoint returning null as exposed instead of covered.
  const el = element("button", [], { x: 10, y: 10, width: 20, height: 20 });
  const docNull = {
    elementFromPoint() {
      return null;
    },
  };
  assert.equal(extract.occlusion(el, docNull), 5);
});

// 8. Attack list item: a covering child counted as exposed
registerTest("a covering ancestor or parent counted as exposed", () => {
  // Kills mutant: checking hit.contains(el) instead of el.contains(hit), which incorrectly treats a covering ancestor/parent as an exposed descendant.
  const child = element("span", [], { x: 10, y: 10, width: 20, height: 20 });
  const parent = element("div", [child], { x: 0, y: 0, width: 100, height: 100 });

  // When testing occlusion of child, if elementFromPoint returns parent (e.g. parent overlays child)
  const docParentHit = {
    elementFromPoint() {
      return parent;
    },
  };
  // child does NOT contain parent (parent is ancestor), so hit is covered.
  assert.equal(extract.occlusion(child, docParentHit), 5);
});

// 9. Attack list item: maxNodes exactly hit
registerTest("maxNodes exactly hit", () => {
  // Kills mutant: setting truncated to true when total nodes equals maxNodes, or failing to set truncated when additional nodes remain.
  const root3 = element("root", [element("a"), element("b")]);

  // Total nodes in tree = 3. maxNodes = 3.
  const resExact = extract.walkComposed(root3, { maxNodes: 3 });
  assert.equal(resExact.elements.length, 3);
  assert.equal(resExact.truncated, false);

  // Total nodes in tree = 3. maxNodes = 2.
  const resTrunc = extract.walkComposed(root3, { maxNodes: 2 });
  assert.equal(resTrunc.elements.length, 2);
  assert.equal(resTrunc.truncated, true);
});

// 10. Attack list item: output with no non-integer numbers
registerTest("output with no non-integer numbers", () => {
  // Kills mutant: outputting floating point numbers or non-integer coordinates/counts in extract outputs.
  const iframe = element("iframe", [], { x: 1.2, y: 3.8, width: 20.4, height: 10.1 });
  iframe.contentDocument = document(element("p", [], { x: 0.5, y: 0.5, width: 5.5, height: 5.5 }));
  const root = document(iframe);

  const result = extract.walkComposed(root);
  assert.ok(Number.isInteger(result.cross_origin));
  assert.ok(Number.isInteger(result.unknown));
  assert.equal(typeof result.truncated, "boolean");

  for (const elem of result.elements) {
    assert.equal(typeof elem.tag, "string");
    assert.ok(Array.isArray(elem.box));
    for (const val of elem.box) {
      assert.ok(Number.isInteger(val), `box value ${val} is not integer`);
    }
  }

  for (const frame of result.frames) {
    assert.equal(typeof frame.tag, "string");
    assert.ok(Number.isInteger(frame.cross_origin));
    for (const val of frame.box) {
      assert.ok(Number.isInteger(val), `frame box value ${val} is not integer`);
    }
  }

  const el = element("button", [], { x: 10.5, y: 10.5, width: 20.5, height: 10.5 });
  const doc = { elementFromPoint: () => el };
  const occ = extract.occlusion(el, doc);
  assert.ok(Number.isInteger(occ), `occlusion result ${occ} is not integer`);
});
