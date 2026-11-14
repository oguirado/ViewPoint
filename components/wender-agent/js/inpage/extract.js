// @module {"id":"wa.js.inpage.extract","layer":0,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
// Plain in-page helpers; no DOM references escape the serialized walk result.
// Box conversion mirrors js/inpage/viewport.js for standalone CDP evaluation.
// Element roots are included; Document/ShadowRoot containers are not emitted.
// Open shadow children precede light children. Slots are not expanded twice.

function _int(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.round(number) : 0;
}

function _box(rect, offsetX, offsetY) {
  if (!rect) return null;
  return [_int(rect.x + offsetX), _int(rect.y + offsetY),
    Math.max(0, _int(rect.width)), Math.max(0, _int(rect.height))];
}

// @anchor:wa.js.inpage.extract.walkComposed
// elements: {tag, box}[]; frames: {tag, box, cross_origin: 0|1}[];
// cross_origin and unknown are counts. Only truncated is a boolean.
function walkComposed(root, opts = {}) {
  const maxNodes = opts.maxNodes === undefined ? 5000 : opts.maxNodes;
  if (!Number.isSafeInteger(maxNodes) || maxNodes < 0) {
    throw new RangeError('maxNodes must be a nonnegative safe integer');
  }
  const result = {elements: [], frames: [], cross_origin: 0, unknown: 0, truncated: false};
  const seen = new Set();
  function* children(container, x, y) {
    for (const child of container.children || []) yield {node: child, x, y};
  }
  function* descend(node, x, y, frameDoc, rect) {
    if (node.shadowRoot) yield* children(node.shadowRoot, x, y);
    if (frameDoc) {
      yield* children(frameDoc, x + rect.x, y + rect.y);
    }
    yield* children(node, x, y);
  }
  // Iterator frames keep both traversal and pending work bounded by depth.
  const stack = [[{node: root, x: 0, y: 0}][Symbol.iterator]()];
  while (stack.length) {
    const next = stack[stack.length - 1].next();
    if (next.done) { stack.pop(); continue; }
    const {node, x, y} = next.value;
    if (!node || seen.has(node)) continue;
    seen.add(node);
    const isElement = node.nodeType === 1 || typeof node.tagName === 'string';
    if (!isElement) {
      stack.push(children(node, x, y));
      continue;
    }
    if (result.elements.length === maxNodes) {
      result.truncated = true;
      break;
    }
    const tag = node.tagName.toLowerCase();
    const rect = node.getBoundingClientRect();
    const record = {tag, box: _box(rect, x, y)};
    result.elements.push(record);
    let frameDoc = null;
    if (tag === 'iframe') {
      try { frameDoc = node.contentDocument; } catch (_) { /* inaccessible frame */ }
      const cross_origin = frameDoc ? 0 : 1;
      result.frames.push({...record, cross_origin});
      result.cross_origin += cross_origin;
    }
    if (!node.shadowRoot && opts.hostHasClosedRoot && opts.hostHasClosedRoot(node)) {
      result.unknown += 1;
    }
    stack.push(descend(node, x, y, frameDoc, rect));
  }
  return result;
}

// @anchor:wa.js.inpage.extract.isVisible
function isVisible(el, win) {
  const style = win.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' ||
      style.visibility === 'collapse' || Number(style.opacity) === 0) return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && rect.x < win.innerWidth &&
    rect.y < win.innerHeight && rect.x + rect.width > 0 && rect.y + rect.height > 0;
}

// @anchor:wa.js.inpage.extract.occlusion
function occlusion(el, doc) {
  const rect = el.getBoundingClientRect();
  const insetX = Math.min(2, Math.max(0, rect.width) / 2);
  const insetY = Math.min(2, Math.max(0, rect.height) / 2);
  const left = rect.x + insetX, right = rect.x + rect.width - insetX;
  const top = rect.y + insetY, bottom = rect.y + rect.height - insetY;
  const points = [[rect.x + rect.width / 2, rect.y + rect.height / 2],
    [left, top], [right, top], [left, bottom], [right, bottom]];
  let covered = 0;
  for (const [x, y] of points) {
    const hit = doc.elementFromPoint(x, y);
    if (hit !== el && (!hit || !el.contains(hit))) covered += 1;
  }
  return covered;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {walkComposed, isVisible, occlusion};
}
