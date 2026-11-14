import test from 'node:test';
import { strict as assert } from 'node:assert';
import { execFile } from 'node:child_process';
import {
  ClientError, COMPUTED_STYLES, FAKE_URL, FakeCDPClient, NulFramer,
  findNode, isRefused, parseAXTree, parseSnapshot, parseTree, plan_click,
} from './client.mjs';

const ROOT = decodeURIComponent(new URL('../../../../', import.meta.url).pathname);
const OPTIONS = { timeout: 10_000 };
// Use the permitted child_process module for fixture reads; no extra test imports.
function python(code, path) {
  return new Promise((resolve, reject) => {
    execFile('python3', ['-B', '-c', code, path],
      { cwd: ROOT, encoding: 'utf8', timeout: 5000, killSignal: 'SIGKILL' },
      (error, stdout) => error ? reject(error) : resolve(stdout));
  });
}
const [AX, SNAPSHOT, ALLOWLIST] = await Promise.all([
  'bench/oracle/tests/fixtures/oracle_ax_focus.json',
  'bench/oracle/tests/fixtures/oracle_snapshot_basic.json',
  'docker/cu/oracle/allowlist.json',
].map(async path => JSON.parse(await python(
  'import pathlib, sys\nsys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())', path))));
const clone = value => JSON.parse(JSON.stringify(value));
const rows = client => client.auditLog().split('\n').filter(Boolean).map(line => JSON.parse(line));

function attribute(snapshot, nodeIndex, key, value) {
  const at = snapshot.strings.length;
  snapshot.strings.push(key, value);
  snapshot.documents[0].nodes.attributes[nodeIndex].push(at, at + 1);
}

async function assertReaped(client) {
  assert.notEqual(client.exitInfo, null, 'close event must be observed');
  assert.throws(() => process.kill(client.pid, 0), error => error.code === 'ESRCH');
  const exists = await python('import pathlib, sys\nprint(pathlib.Path(sys.argv[1]).exists())', client.logPath);
  assert.equal(exists.trim(), 'False', 'temporary audit log must be removed');
}

test('NUL framing accepts the exact byte limit and all chunk boundaries', OPTIONS, () => {
  const stream = Buffer.from('{"id":1}\0{"id":2}\0');
  const expected = ['{"id":1}', '{"id":2}'];
  for (let cut = 0; cut <= stream.length; cut++) {
    const framer = new NulFramer(8);
    assert.deepEqual([...framer.feed(stream.subarray(0, cut)), ...framer.feed(stream.subarray(cut))]
      .map(frame => frame.toString()), expected);
  }
  const framer = new NulFramer(8);
  const frames = [];
  for (const byte of stream) frames.push(...framer.feed(Buffer.from([byte])));
  assert.deepEqual(frames.map(frame => frame.toString()), expected);
  const exact = new NulFramer(8);
  assert.deepEqual(exact.feed(Buffer.from('12345678')), []);
  assert.deepEqual(exact.feed(Buffer.from([0])).map(frame => frame.toString()), ['12345678']);
  assert.throws(() => new NulFramer(8).feed(Buffer.from('123456789')), { code: 5 });
  assert.throws(() => new NulFramer(8).feed(Buffer.from('123456789\0')), { code: 5 });
  assert.throws(() => new NulFramer(1).feed(Buffer.from('é\0')), { code: 5 });
  assert.equal(new NulFramer(2).feed(Buffer.from('é\0'))[0].toString(), 'é');
});

test('real fake pipe binds S1 and logs only scoped allowlisted requests', OPTIONS, async () => {
  const client = new FakeCDPClient({ timeoutMs: 1500 });
  try {
    assert.equal(client.maxFrameBytes, ALLOWLIST.max_frame_bytes);
    assert.equal(await client.start(), 'S1');
    assert.equal(await client.start(), 'S1');
    assert.deepEqual(await client.snapshot(), { ax: {}, snapshot: {} });
    assert.deepEqual(await Promise.all([
      client.request('Page.getFrameTree'), client.request('Accessibility.enable'),
    ]), [{}, {}]);
    const sent = rows(client);
    assert.deepEqual(sent[0], { id: 1, method: 'Target.setAutoAttach', params: {
      autoAttach: true, waitForDebuggerOnStart: false, flatten: true,
    } });
    assert.deepEqual(sent.map(row => row.id), [1, 2, 3, 4, 5]);
    assert.deepEqual(sent[2].params, { computedStyles: [...COMPUTED_STYLES] });
    for (const row of sent) {
      assert.ok(Object.prototype.hasOwnProperty.call(ALLOWLIST.browser_methods, row.method)
        || Object.prototype.hasOwnProperty.call(ALLOWLIST.session_methods, row.method));
      assert.doesNotMatch(row.method, /^(Input|Runtime|Emulation|DOM)\./);
      assert.notEqual(row.method, 'Page.navigate');
      if (row.method !== 'Target.setAutoAttach') assert.equal(row.sessionId, 'S1');
    }
    assert.doesNotMatch(client.auditLog(), /Runtime\.consoleAPICalled/);
  } finally {
    await client.close();
  }
  await assertReaped(client);
});

test('Input and Page.navigate refusals write nothing before or after handshake', OPTIONS, async () => {
  const client = new FakeCDPClient({ timeoutMs: 1500 });
  try {
    await assert.rejects(client.request('Input.dispatchMouseEvent', { type: 'mousePressed' }), { code: 5 });
    await assert.rejects(client.request('Page.navigate', { url: FAKE_URL }), { code: 5 });
    await assert.rejects(client.request('Accessibility.getFullAXTree'), { code: 5 });
    assert.equal(client.auditLog(), '');
    await client.start();
    const before = client.auditLog();
    for (const [method, params] of [
      ['Input.dispatchMouseEvent', {}], ['Page.navigate', { url: FAKE_URL }],
      ['Runtime.evaluate', {}], ['Emulation.setDeviceMetricsOverride', {}], ['DOM.getDocument', {}],
      ['Accessibility.getFullAXTreeX', {}], ['constructor', {}],
      ['Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }],
      ['DOMSnapshot.captureSnapshot', { computedStyles: ['background-image'] }],
    ]) await assert.rejects(client.request(method, params), { code: 5 });
    assert.equal(client.auditLog(), before);
    await client.request('Page.getFrameTree'); // Response is a barrier after every refusal.
    assert.deepEqual(rows(client).map(row => [row.id, row.method]), [
      [1, 'Target.setAutoAttach'], [2, 'Page.getFrameTree'],
    ]);
  } finally {
    await client.close();
  }
  await assertReaped(client);
});

test('oversize real fake output is refused before a NUL arrives and is reaped', OPTIONS, async () => {
  const client = new FakeCDPClient({ mode: 'oversize', fakeBytes: 4096, maxFrameBytes: 256, timeoutMs: 1500 });
  try {
    await assert.rejects(client.start(), error => error.code === 5 && /oversize/.test(error.message));
    assert.equal(client.sessionId, null);
    assert.equal(client.auditLog(), '');
  } finally {
    await client.close();
  }
  await assertReaped(client);
});

test('malformed fake JSON is refused and the child is reaped', OPTIONS, async () => {
  const client = new FakeCDPClient({ mode: 'badjson', timeoutMs: 1500 });
  try {
    await assert.rejects(client.start(), error => error.code === 5 && /JSON/.test(error.message));
  } finally {
    await client.close();
  }
  await assertReaped(client);
});

for (const mode of ['attach_blank', 'attach_other']) {
  test(`${mode}: info changes never substitute for a matching attachment`, OPTIONS, async () => {
    const client = new FakeCDPClient({ mode, timeoutMs: 250 });
    try {
      await assert.rejects(client.start(), error => error.code === 7 && /attachment/.test(error.message));
      assert.equal(client.sessionId, null);
      assert.deepEqual(rows(client).map(row => row.method), ['Target.setAutoAttach']);
    } finally {
      await client.close();
    }
    await assertReaped(client);
  });
}

test('an unanswered request times out and kills/reaps a stopped fake', OPTIONS, async () => {
  const client = new FakeCDPClient({ timeoutMs: 250 });
  try {
    await client.start();
    process.kill(client.pid, 'SIGSTOP');
    await assert.rejects(client.request('Page.getFrameTree'), { code: 7 });
  } finally {
    await client.close();
  }
  await assertReaped(client);
});

test('close rejects outstanding work and is idempotent', OPTIONS, async () => {
  const client = new FakeCDPClient({ timeoutMs: 1500 });
  try {
    await client.start();
    process.kill(client.pid, 'SIGSTOP');
    const rejected = assert.rejects(client.request('Page.getFrameTree'), { code: 5 });
    const close = client.close();
    assert.equal(client.close(), close);
    await close;
    await rejected;
    await assert.rejects(client.request('Page.getFrameTree'), { code: 5 });
  } finally {
    await client.close();
  }
  await assertReaped(client);
});

test('snapshot fixture decodes box and yields document/viewport centres', OPTIONS, async () => {
  const original = JSON.stringify(SNAPSHOT);
  const box = findNode(parseTree(SNAPSHOT), { id: 'box' });
  assert.equal(box.backendNodeId, 4);
  assert.deepEqual(box.rect, [100, 200, 640, 480]);
  assert.equal(box.visible, true);
  assert.deepEqual(await plan_click({ id: 'box' }, () => SNAPSHOT), {
    kind: 'click', xy: [420, 440], viewport_xy: [420, 376], attempts: 1,
  });
  assert.equal(JSON.stringify(SNAPSHOT), original, 'pure decoders/planner must not mutate input');
});

test('cv is not visible and consumes the default three attempts', OPTIONS, async () => {
  let calls = 0;
  assert.equal(findNode(parseTree(SNAPSHOT), { id: 'cv' }).visible, false);
  assert.deepEqual(await plan_click({ id: 'cv' }, () => { calls++; return SNAPSHOT; }), {
    kind: 'abort', reason: 'not_visible', attempts: 3,
  });
  assert.equal(calls, 3);
});

test('empty first snapshot followed by the fixture succeeds on attempt 2', OPTIONS, async () => {
  const attempts = [];
  assert.deepEqual(await plan_click({ id: 'box' }, attempt => {
    attempts.push(attempt);
    return attempt === 1 ? {} : SNAPSHOT;
  }), { kind: 'click', xy: [420, 440], viewport_xy: [420, 376], attempts: 2 });
  assert.deepEqual(attempts, [1, 2]);
});

test('AX fixture exposes focused nodeId 7/textbox without guessing a DOM join', OPTIONS, async () => {
  assert.equal(parseAXTree(AX).length, 2);
  const tree = parseTree({ ax: AX, snapshot: SNAPSHOT });
  const focused = findNode(tree, { focused: true });
  assert.equal(focused.nodeId, '7');
  assert.equal(focused.role, 'textbox');
  assert.equal(findNode(tree, { role: 'textbox' }), focused);
  assert.equal(focused.backendNodeId, null);
  assert.equal(focused.rect, null);
  assert.deepEqual(await plan_click({ focused: true }, () => ({ ax: AX, snapshot: SNAPSHOT }), 1), {
    kind: 'abort', reason: 'not_visible', attempts: 1,
  });
});

test('AX role and focus queries join only by backendDOMNodeId', OPTIONS, async () => {
  const ax = clone(AX);
  ax.nodes[1].backendDOMNodeId = 4;
  for (const query of [{ role: 'textbox' }, { focused: true }]) {
    assert.deepEqual(await plan_click(query, () => ({ ax, snapshot: SNAPSHOT })), {
      kind: 'click', xy: [420, 440], viewport_xy: [420, 376], attempts: 1,
    });
  }
});

test('visibility, opacity, display and positive bounds each independently gate visibility', OPTIONS, () => {
  const changes = [
    s => { s.documents[0].layout.styles[2][0] = 12; },
    s => { s.strings.push('0.0'); s.documents[0].layout.styles[2][1] = s.strings.length - 1; },
    s => { s.documents[0].layout.styles[2][2] = 15; },
    s => { s.documents[0].layout.bounds[2][2] = 0; },
    s => { s.documents[0].layout.bounds[2][3] = 0; },
    s => { s.documents[0].layout.styles[2] = []; },
    s => { s.documents[0].layout.nodeIndex[2] = -1; },
  ];
  for (const change of changes) {
    const snapshot = clone(SNAPSHOT);
    change(snapshot);
    assert.equal(findNode(parseTree(snapshot), { id: 'box' }).visible, false);
  }
  assert.deepEqual(parseSnapshot({}), []);
  assert.deepEqual(parseAXTree({}), []);
  assert.deepEqual(parseTree(null), []);
});

test('centres floor odd dimensions and subtract both scroll offsets', OPTIONS, async () => {
  const snapshot = clone(SNAPSHOT);
  Object.assign(snapshot.documents[0], { scrollOffsetX: -3, scrollOffsetY: 2 });
  snapshot.documents[0].layout.bounds[2] = [-4, -5, 5, 7];
  assert.deepEqual(await plan_click({ id: 'box' }, () => snapshot), {
    kind: 'click', xy: [-2, -2], viewport_xy: [1, -4], attempts: 1,
  });
});

test('password type, card_number id and input type file abort before planning', OPTIONS, async () => {
  for (const [key, value] of [['type', 'PaSsWoRd'], ['id', 'card_number'], ['type', 'file']]) {
    const snapshot = clone(SNAPSHOT);
    snapshot.strings.push('INPUT');
    snapshot.documents[0].nodes.nodeName[3] = snapshot.strings.length - 1;
    attribute(snapshot, 3, key, value);
    let calls = 0;
    const result = await plan_click({ id: key === 'id' ? value : 'box' }, () => { calls++; return snapshot; });
    assert.deepEqual(result, { kind: 'abort', reason: 'refused', attempts: 1 });
    assert.equal(calls, 1);
    assert.equal('xy' in result, false);
  }
});

test('all sensitive patterns are case-insensitive in id, name and AX name', OPTIONS, () => {
  for (const value of ['PASSWORD', 'PassWd', 'SECRET', 'ToKeN', 'CC-', 'CardNum', 'card_number',
    'card-number', 'card num', 'cardXnum', 'card\nnum', 'CVV', 'CVC']) {
    for (const node of [{ attributes: { id: value } }, { attributes: { name: value } }, { name: value }]) {
      assert.equal(isRefused(node), true, value);
    }
  }
  assert.equal(isRefused({ attributes: { id: 'box', type: 'text' }, name: 'Address' }), false);
});

test('a sensitive focused node refuses even a different or missing target', OPTIONS, async () => {
  for (const type of ['password', 'file']) {
    const snapshot = clone(SNAPSHOT);
    const ax = clone(AX);
    attribute(snapshot, 4, 'type', type);
    ax.nodes[1].backendDOMNodeId = 5;
    for (const id of ['box', 'missing']) {
      assert.deepEqual(await plan_click({ id }, () => ({ ax, snapshot })), {
        kind: 'abort', reason: 'refused', attempts: 1,
      });
    }
  }
  const ax = clone(AX);
  ax.nodes[1].name = { type: 'computedString', value: 'Secret field' };
  assert.deepEqual(await plan_click({ id: 'box' }, () => ({ ax, snapshot: SNAPSHOT })), {
    kind: 'abort', reason: 'refused', attempts: 1,
  });
  ax.nodes[1].backendDOMNodeId = 4;
  ax.nodes[1].properties = []; // Sensitive target also refuses without focus.
  assert.deepEqual(await plan_click({ role: 'textbox' }, () => ({ ax, snapshot: SNAPSHOT })), {
    kind: 'abort', reason: 'refused', attempts: 1,
  });
});

test('empty results over the real fake pipe exhaust retries as not_found', OPTIONS, async () => {
  const client = new FakeCDPClient({ timeoutMs: 1500 });
  try {
    assert.deepEqual(await plan_click({ id: 'box' }, () => client.snapshot()), {
      kind: 'abort', reason: 'not_found', attempts: 3,
    });
    assert.deepEqual(rows(client).map(row => row.method), [
      'Target.setAutoAttach',
      'Accessibility.getFullAXTree', 'DOMSnapshot.captureSnapshot',
      'Accessibility.getFullAXTree', 'DOMSnapshot.captureSnapshot',
      'Accessibility.getFullAXTree', 'DOMSnapshot.captureSnapshot',
    ]);
  } finally {
    await client.close();
  }
  await assertReaped(client);
});

test('two independent runs produce byte-identical serialized plans', OPTIONS, async () => {
  async function run() {
    const client = new FakeCDPClient({ timeoutMs: 1500 });
    try {
      return Buffer.from(JSON.stringify([
        await plan_click({ id: 'box' }, () => client.snapshot(), 2),
        await plan_click({ id: 'box' }, () => clone(SNAPSHOT)),
        await plan_click({ id: 'cv' }, () => clone(SNAPSHOT)),
      ]));
    } finally {
      await client.close();
    }
  }
  assert.deepEqual(await run(), await run());
});

test('provider waits and options are bounded; error codes are 2, 5 or 7', OPTIONS, async () => {
  await assert.rejects(plan_click({ id: 'box' }, () => new Promise(() => {}), 3, { timeoutMs: 30 }), { code: 7 });
  await assert.rejects(plan_click({ id: 'box' }, () => SNAPSHOT, 0), { code: 2 });
  await assert.rejects(plan_click({ id: 'box' }, () => SNAPSHOT, 3, { timeoutMs: 10_001 }), { code: 2 });
  await assert.rejects(plan_click({ id: 'box', role: 'textbox' }, () => SNAPSHOT), { code: 2 });
  assert.throws(() => new FakeCDPClient({ timeoutMs: 10_001 }), { code: 2 });
  assert.throws(() => new FakeCDPClient({ maxFrameBytes: ALLOWLIST.max_frame_bytes + 1 }), { code: 2 });
  assert.equal(new ClientError(5, 'refused').code, 5);
});
