const test = require('node:test');
const assert = require('node:assert/strict');
const {walkComposed, isVisible, occlusion} = require('../inpage/extract.js');

function element(tag, children = [], rect = {}) {
  return {nodeType: 1, tagName: tag.toUpperCase(), children,
    getBoundingClientRect: () => ({x: 0, y: 0, width: 20, height: 10, ...rect}),
    contains(hit) { return children.includes(hit); }};
}
const document = (...children) => ({nodeType: 9, children});
const tags = result => result.elements.map(item => item.tag);

test('nested open roots walk depth first, followed by light siblings', () => {
  const inner = element('inner');
  inner.shadowRoot = {children: [element('button')]};
  const host = element('host', [element('light')]);
  host.shadowRoot = {children: [inner]};
  const result = walkComposed(document(host, element('footer')));
  assert.deepEqual(tags(result), ['host', 'inner', 'button', 'light', 'footer']);
  assert.equal(result.truncated, false);
  assert.equal(result.unknown, 0);
  assert.doesNotThrow(() => JSON.stringify(result));
});

test('frame offsets accumulate and boxes follow viewport rounding/clamping', () => {
  const nested = element('iframe', [], {x: 10, y: 20});
  nested.contentDocument = document(element('input', [], {x: 1.4, y: 2.6, width: -2}));
  const frame = element('iframe', [], {x: 100, y: 50});
  frame.contentDocument = document(element('button', [], {x: 3, y: 4}), nested);
  const result = walkComposed(document(frame, element('aside')));
  assert.deepEqual(tags(result), ['iframe', 'button', 'iframe', 'input', 'aside']);
  assert.deepEqual(result.elements.map(x => x.box),
    [[100, 50, 20, 10], [103, 54, 20, 10], [110, 70, 20, 10], [111, 73, 0, 10], [0, 0, 20, 10]]);
  assert.equal(result.frames.length, 2);
  assert.equal(result.cross_origin, 0);
  assert.equal(result.frames[0].cross_origin, 0);
});

test('null and throwing documents are cross origin; closed roots require evidence', () => {
  const throwing = element('iframe');
  Object.defineProperty(throwing, 'contentDocument', {get() {throw new Error('denied');}});
  const empty = element('iframe');
  empty.contentDocument = null;
  const closed = element('closed');
  const root = document(throwing, empty, closed);
  assert.equal(walkComposed(root).unknown, 0);
  const result = walkComposed(root, {hostHasClosedRoot: el => el === closed});
  assert.equal(result.cross_origin, 2);
  assert.equal(result.unknown, 1);
  assert.deepEqual(result.frames.map(x => x.cross_origin), [1, 1]);
});

test('budget stops traversal, exact completion is not truncated and cycles terminate', () => {
  const root = element('root', [element('a'), element('b')]);
  assert.deepEqual(tags(walkComposed(root, {maxNodes: 2})), ['root', 'a']);
  assert.equal(walkComposed(root, {maxNodes: 2}).truncated, true);
  assert.equal(walkComposed(root, {maxNodes: 3}).truncated, false);
  assert.equal(walkComposed(root, {maxNodes: 0}).truncated, true);
  assert.equal(walkComposed(document(), {maxNodes: 0}).truncated, false);
  root.children.push(root);
  assert.deepEqual(tags(walkComposed(root)), ['root', 'a', 'b']);
  for (const maxNodes of [-1, 1.5, NaN, Infinity]) {
    assert.throws(() => walkComposed(root, {maxNodes}), RangeError);
  }
  const many = document(...Array.from({length: 5001}, () => element('i')));
  assert.equal(walkComposed(many).elements.length, 5000);
  assert.equal(walkComposed(many).truncated, true);
});

test('visibility rejects each CSS and geometry exclusion', () => {
  const visible = (style = {}, rect = {}) => isVisible(element('button', [], rect), {
    innerWidth: 100, innerHeight: 100,
    getComputedStyle: () => ({display: 'block', visibility: 'visible', opacity: '1', ...style})});
  assert.equal(visible(), true);
  for (const style of [{display: 'none'}, {visibility: 'hidden'},
    {visibility: 'collapse'}, {opacity: '0'}, {opacity: '0.0'}]) assert.equal(visible(style), false);
  for (const rect of [{width: 0}, {height: 0}, {width: -1}, {x: -20},
    {y: -10}, {x: 100}, {y: 100}]) assert.equal(visible({}, rect), false);
  assert.equal(visible({}, {x: -19}), true);
});

test('occlusion samples centre and inset corners, accepting descendants only', () => {
  const child = element('span');
  const el = element('button', [child], {x: 10, y: 20, width: 20, height: 10});
  const points = [];
  const result = occlusion(el, {elementFromPoint(x, y) {
    points.push([x, y]);
    return points.length === 1 ? el : points.length === 2 ? child : null;
  }});
  assert.equal(result, 3);
  assert.deepEqual(points, [[20, 25], [12, 22], [28, 22], [12, 28], [28, 28]]);
  assert.equal(occlusion(el, {elementFromPoint: () => el}), 0);
  assert.equal(occlusion(el, {elementFromPoint: () => child}), 0);
  assert.equal(occlusion(el, {elementFromPoint: () => element('overlay')}), 5);
  const tiny = element('i', [], {width: 1, height: 1});
  occlusion(tiny, {elementFromPoint(x, y) {
    assert.ok(x >= 0 && x <= 1 && y >= 0 && y <= 1);
    return tiny;
  }});
});
