// @module {"id":"wa.js.inpage.viewport","layer":0,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
// In-page extraction script. Evaluated by wender.perceive.browser via CDP Runtime.evaluate
// (the Python side appends `JSON.stringify(wenderViewport(window))`). Plain JS, no deps;
// the module.exports guard lets `node --test js/tests` exercise it with a fake window.
// Output is integers only: CSS px, and device pixel ratio as parts-per-million.

// @anchor:wa.js.inpage.viewport.toInt
function toInt(x) {
  const n = Number(x);
  return Number.isFinite(n) ? Math.round(n) : 0;
}

// @anchor:wa.js.inpage.viewport.rectToBox
// DOMRect-like {x,y,width,height} -> [x, y, w, h] integers; negative sizes clamp to 0.
function rectToBox(r) {
  if (!r) return null;
  return [toInt(r.x), toInt(r.y), Math.max(0, toInt(r.width)), Math.max(0, toInt(r.height))];
}

// @anchor:wa.js.inpage.viewport.wenderViewport
function wenderViewport(win) {
  const d = win.document && win.document.documentElement;
  return {
    w: toInt(win.innerWidth),
    h: toInt(win.innerHeight),
    scroll_x: toInt(win.scrollX),
    scroll_y: toInt(win.scrollY),
    doc_w: d ? toInt(d.scrollWidth) : 0,
    doc_h: d ? toInt(d.scrollHeight) : 0,
    dpr_ppm: toInt((win.devicePixelRatio || 1) * 1000000),
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { toInt, rectToBox, wenderViewport };
}
