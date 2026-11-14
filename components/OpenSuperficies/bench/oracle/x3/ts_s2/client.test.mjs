import test from 'node:test';
import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import {
  ClientError, COMPUTED_STYLES, FAKE_URL, FakeCdpClient, NulFramer,
  find_node, focused_node, parse_ax, parse_snapshot, plan_click,
} from './client.mjs';

const ROOT = new URL('../../../../', import.meta.url);
const AX_PATH = 'bench/oracle/tests/fixtures/oracle_ax_focus.json';
const SNAPSHOT_PATH = 'bench/oracle/tests/fixtures/oracle_snapshot_basic.json';
const SEED_PATH = 'bench/oracle/x3/ts_s2/';

function python(code, ...args) {
  return execFileSync('python3', ['-B', '-c',
    'import sys\ntry:\n' + code.split('\n').map((s) => ' ' + s).join('\n')
      + '\nexcept Exception as e:\n print(type(e).__name__,file=sys.stderr);sys.exit(5)\n', ...args],
  { encoding: 'utf8', timeout: 2000, killSignal: 'SIGKILL', maxBuffer: 2 * 1024 * 1024 });
}
function readText(path) {
  return python('from pathlib import Path\nsys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())',
    decodeURIComponent(new URL(path, ROOT).pathname));
}
const ax = JSON.parse(readText(AX_PATH));
const snapshot = JSON.parse(readText(SNAPSHOT_PATH));
const allowlist = JSON.parse(readText('docker/cu/oracle/allowlist.json'));
const fixture = () => ({ ax, snapshot });
const empty = () => ({ ax: {}, snapshot: {} });

function withAttributes(attributes, nodeIndex = 3) {
  const copy = structuredClone(snapshot);
  const pairs = [];
  for (const [key, value] of Object.entries(attributes)) {
    pairs.push(copy.strings.push(key) - 1, copy.strings.push(value) - 1);
  }
  copy.documents[0].nodes.attributes[nodeIndex] = pairs;
  return copy;
}
function axFor(backendNodeId, options = {}) {
  return { nodes: [{
    nodeId: '7', backendDOMNodeId: backendNodeId,
    role: { value: options.role ?? 'textbox' }, name: { value: options.name ?? '' },
    properties: [{ name: 'focused', value: { value: options.focused ?? true } }],
  }] };
}
function logRecords(client) {
  return client.read_log().split('\n').filter(Boolean).map((s) => JSON.parse(s));
}
async function useClient(options, fn) {
  const client = new FakeCdpClient(options);
  try { return await fn(client); }
  finally {
    await client.close();
    assert.strictEqual(client.isReaped, true);
    assert.strictEqual(python('import os\nprint(os.path.exists(sys.argv[1]))', client.logPath).trim(), 'False');
    if (client.pid !== undefined) assert.throws(() => process.kill(client.pid, 0), { code: 'ESRCH' });
  }
}

test('client.mjs first line holds the SHA-256 of the exact TypeScript source', async () => {
  // node:crypto is the explicit hash-test exception to the three test imports.
  const { createHash } = await import('node:crypto');
  const expected = createHash('sha256').update(readText(SEED_PATH + 'client.ts'), 'utf8').digest('hex');
  assert.strictEqual(readText(SEED_PATH + 'client.mjs').split('\n')[0], '// sha256(client.ts): ' + expected);
});

test('box centre is [420,440] in document coordinates and [420,376] in the viewport', async () => {
  const before = JSON.stringify(fixture());
  const nodes = parse_snapshot(snapshot, ax);
  const box = find_node(nodes, { by: 'id', value: 'box' });
  assert.deepStrictEqual(box.rect, [100, 200, 640, 480]);
  assert.strictEqual(box.backendNodeId, 4);
  assert.strictEqual(box.visible, true);
  assert.deepStrictEqual(await plan_click({ by: 'id', value: 'box' }, fixture),
    { kind: 'click', xy: [420, 440], viewport_xy: [420, 376], attempts: 1 });
  assert.strictEqual(JSON.stringify(fixture()), before, 'pure parsers/planners do not mutate the provider data');
});

test('AX fixture focus is nodeId 7, role textbox; role/focus remain queryable without a DOM join', () => {
  assert.strictEqual(parse_ax(ax).length, 2);
  const focused = focused_node(ax);
  assert.strictEqual(focused.nodeId, '7');
  assert.strictEqual(focused.role, 'textbox');
  assert.strictEqual(focused.backendNodeId, null);
  const nodes = parse_snapshot(snapshot, ax);
  assert.strictEqual(find_node(nodes, { by: 'focused' }).nodeId, '7');
  assert.strictEqual(find_node(nodes, { by: 'role', value: 'textbox' }).nodeId, '7');
  assert.strictEqual(find_node(nodes, { by: 'focused' }).visible, false);
});

test('backendDOMNodeId joins AX role and focus to the snapshot rectangle', async () => {
  const provider = () => ({ ax: axFor(4, { role: 'button' }), snapshot });
  for (const query of [{ by: 'role', value: 'button' }, { by: 'focused' }]) {
    assert.deepStrictEqual(await plan_click(query, provider),
      { kind: 'click', xy: [420, 440], viewport_xy: [420, 376], attempts: 1 });
  }
});

test('cv is invisible and exhausts exactly the default three attempts', async () => {
  let calls = 0;
  assert.strictEqual(find_node(parse_snapshot(snapshot), { by: 'id', value: 'cv' }).visible, false);
  const plan = await plan_click({ by: 'id', value: 'cv' }, () => { calls++; return fixture(); });
  assert.deepStrictEqual(plan, { kind: 'abort', reason: 'not_visible', attempts: 3 });
  assert.strictEqual(calls, 3);
});

test('empty first snapshot, fixture second snapshot succeeds on attempt 2', async () => {
  let calls = 0;
  const plan = await plan_click({ by: 'id', value: 'box' }, () => ++calls === 1 ? empty() : fixture());
  assert.deepStrictEqual(plan, { kind: 'click', xy: [420, 440], viewport_xy: [420, 376], attempts: 2 });
  assert.strictEqual(calls, 2);
});

test('missing targets exhaust the supplied attempt budget; the last snapshot determines the reason', async () => {
  assert.deepStrictEqual(await plan_click({ by: 'id', value: 'missing' }, empty, 2),
    { kind: 'abort', reason: 'not_found', attempts: 2 });
  let calls = 0;
  assert.deepStrictEqual(await plan_click({ by: 'id', value: 'cv' }, () => ++calls === 1 ? fixture() : empty(), 2),
    { kind: 'abort', reason: 'not_found', attempts: 2 });
  for (const retries of [0, -1, 1.5, Infinity]) {
    await assert.rejects(plan_click({ by: 'id', value: 'box' }, fixture, retries), { code: 2 });
  }
});

test('password type, card_number id and file type refuse immediately', async () => {
  for (const attributes of [
    { id: 'box', type: 'password' }, { id: 'card_number', type: 'text' }, { id: 'box', type: 'file' },
    { id: 'box', type: 'PaSsWoRd' }, { id: 'box', type: 'FILE' },
  ]) {
    let calls = 0;
    const sensitive = withAttributes(attributes);
    const plan = await plan_click({ by: 'id', value: attributes.id }, () => { calls++; return { ax, snapshot: sensitive }; });
    assert.deepStrictEqual(plan, { kind: 'abort', reason: 'refused', attempts: 1 }, JSON.stringify(attributes));
    assert.strictEqual(calls, 1);
  }
});

test('sensitive id, name and AX name matching is case-insensitive', async () => {
  for (const label of ['PASSWORD', 'PassWd', 'mySecret', 'TOKEN', 'cc-number', 'cardnum', 'card_num', 'card-number', 'CVV', 'CVC']) {
    for (const field of ['id', 'name', 'axName']) {
      const attributes = { id: field === 'id' ? label : 'box' };
      if (field === 'name') attributes.name = label;
      const plan = await plan_click({ by: 'id', value: attributes.id }, () => ({
        ax: axFor(4, { focused: false, name: field === 'axName' ? label : '' }),
        snapshot: withAttributes(attributes),
      }));
      assert.deepStrictEqual(plan, { kind: 'abort', reason: 'refused', attempts: 1 }, field + ': ' + label);
    }
  }
});

test('a sensitive focused node refuses a different benign target, even if target is missing', async () => {
  const provider = () => ({ ax: axFor(5), snapshot: withAttributes({ id: 'c', type: 'file' }, 4) });
  for (const value of ['box', 'missing']) {
    assert.deepStrictEqual(await plan_click({ by: 'id', value }, provider),
      { kind: 'abort', reason: 'refused', attempts: 1 });
  }
  const unjoinedFocus = { nodes: [{ nodeId: '9', role: { value: 'textbox' }, name: { value: 'Secret' },
    properties: [{ name: 'focused', value: { value: true } }] }] };
  assert.deepStrictEqual(await plan_click({ by: 'id', value: 'box' }, () => ({ ax: unjoinedFocus, snapshot })),
    { kind: 'abort', reason: 'refused', attempts: 1 });
});

test('visibility requires each computed style and a positive integer rectangle', () => {
  for (const [slot, value] of [[0, 'hidden'], [1, '0'], [1, '0.0'], [1, ''], [1, 'nonsense'], [2, 'none'], [2, '']]) {
    const copy = structuredClone(snapshot);
    copy.documents[0].layout.styles[2][slot] = copy.strings.push(value) - 1;
    assert.strictEqual(find_node(parse_snapshot(copy), { by: 'id', value: 'box' }).visible, false, value);
  }
  for (const rect of [[100, 200, 0, 480], [100, 200, 640, -1], [100, 200, 640], [100, 200, NaN, 4]]) {
    const copy = structuredClone(snapshot);
    copy.documents[0].layout.bounds[2] = rect;
    assert.strictEqual(find_node(parse_snapshot(copy), { by: 'id', value: 'box' }).visible, false);
  }
  assert.deepStrictEqual(parse_snapshot({}), []);
  assert.deepStrictEqual(parse_ax({}), []);
});

test('coordinates truncate toward zero, centre uses floor of half-size, and both scroll axes are subtracted', async () => {
  const copy = structuredClone(snapshot);
  copy.documents[0].layout.bounds[2] = [-11.9, 21.8, 5.9, 7.2];
  copy.documents[0].scrollOffsetX = -3.8;
  copy.documents[0].scrollOffsetY = 6.9;
  assert.deepStrictEqual(await plan_click({ by: 'id', value: 'box' }, () => ({ ax: {}, snapshot: copy })),
    { kind: 'click', xy: [-9, 24], viewport_xy: [-6, 18], attempts: 1 });
});

test('NUL framing is independent of chunk splits and accepts exactly the byte limit', () => {
  const wire = Buffer.from('{"id":1}\0{"id":2}\0');
  const expected = ['{"id":1}', '{"id":2}'];
  for (let i = 0; i <= wire.length; i++) {
    const framer = new NulFramer(8);
    assert.deepStrictEqual([...framer.feed(wire.subarray(0, i)), ...framer.feed(wire.subarray(i))].map(String), expected);
    framer.finish();
  }
  const framer = new NulFramer(8), frames = [];
  for (const byte of wire) frames.push(...framer.feed(Uint8Array.of(byte)));
  assert.deepStrictEqual(frames.map(String), expected);
  assert.deepStrictEqual(new NulFramer(2).feed(Buffer.from('é\0')).map(String), ['é']);
  assert.throws(() => new NulFramer(1).feed(Buffer.from('é\0')), { code: 5 });
  assert.throws(() => new NulFramer(8).feed(Buffer.from('123456789')), { code: 5 });
  assert.throws(() => new NulFramer(8).feed(Buffer.from('123456789\0')), { code: 5 });
  const partial = new NulFramer(8); partial.feed(Buffer.from('abc'));
  assert.throws(() => partial.finish(), { code: 5 });
});

test('real fake pipe binds S1, matches responses by increasing ids and logs only allowed methods', async () => {
  await useClient({}, async (client) => {
    assert.strictEqual(await client.handshake(), 'S1');
    assert.strictEqual(await client.handshake(), 'S1');
    assert.strictEqual(client.sessionId, 'S1');
    assert.strictEqual(client.maxFrameBytes, allowlist.max_frame_bytes);
    assert.strictEqual(python('import os,stat\nprint(stat.S_ISFIFO(os.stat(sys.argv[1]).st_mode))', client.logPath).trim(), 'True');
    assert.deepStrictEqual(await Promise.all([
      client.request('Target.getTargets'), client.request('Accessibility.getFullAXTree'),
      client.request('DOMSnapshot.captureSnapshot', { computedStyles: [...COMPUTED_STYLES] }),
    ]), [{}, {}, {}]);
    const records = logRecords(client);
    assert.deepStrictEqual(records.map((r) => r.id), [1, 2, 3, 4]);
    assert.deepStrictEqual(records[0], { id: 1, method: 'Target.setAutoAttach',
      params: { autoAttach: true, waitForDebuggerOnStart: false, flatten: true } });
    assert.deepStrictEqual(records[3].params, { computedStyles: ['visibility', 'opacity', 'display'] });
    assert.strictEqual(Object.hasOwn(records[1], 'sessionId'), false);
    for (const record of records) {
      const scope = record.sessionId ? allowlist.session_methods : allowlist.browser_methods;
      assert.ok(Object.hasOwn(scope, record.method));
      assert.ok(!/^(Input|Runtime|Emulation|DOM)\./.test(record.method));
      if (record.sessionId) assert.strictEqual(record.sessionId, 'S1');
    }
    assert.strictEqual(FAKE_URL, 'file:///opt/cu/scenes/v3/ff_form_01.html');
  });
});

test('Input and Page.navigate refusals write nothing before or after handshake; bad parameters are also local', async () => {
  await useClient({}, async (client) => {
    for (const bound of [false, true]) {
      if (bound) await client.handshake();
      const before = client.read_log();
      for (const method of ['Input.dispatchMouseEvent', 'Page.navigate', 'Runtime.evaluate', 'Emulation.setDeviceMetricsOverride', 'DOM.getDocument', 'toString']) {
        await assert.rejects(client.request(method, method === 'Page.navigate' ? { url: FAKE_URL } : {}),
          (error) => error instanceof ClientError && error.code === 5);
        assert.strictEqual(client.read_log(), before);
      }
      await assert.rejects(client.request('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true }), { code: 5 });
      assert.strictEqual(client.read_log(), before);
    }
    await client.request('Target.getTargets');
    assert.deepStrictEqual(logRecords(client).map((r) => r.id), [1, 2], 'refusals consume no request ids');
  });
});

test('oversize fake frame without NUL is refused and the child is reaped', async () => {
  await useClient({ mode: 'oversize', maxFrameBytes: 256, fakeBytes: 4096 }, async (client) => {
    await assert.rejects(client.handshake(), (error) => error.code === 5 && /oversize incoming frame/.test(error.message));
  });
});

test('invalid JSON from the fake is refused and the child is reaped', async () => {
  await useClient({ mode: 'badjson' }, async (client) => {
    await assert.rejects(client.handshake(), (error) => error.code === 5 && /invalid incoming frame/.test(error.message));
  });
});

test('about:blank attachments and targetInfoChanged never bind, even if a later URL matches', async () => {
  for (const mode of ['attach_blank', 'attach_other']) {
    await useClient({ mode, timeoutMs: 300 }, async (client) => {
      await assert.rejects(client.handshake(), (error) => error.code === 5 && /attachment timeout/.test(error.message));
      assert.strictEqual(client.sessionId, null);
      assert.deepStrictEqual(logRecords(client).map((r) => r.method), ['Target.setAutoAttach']);
    });
  }
});

test('empty AX and snapshot results over the fake pipe exhaust retries without actuating', async () => {
  await useClient({}, async (client) => {
    await client.handshake();
    const plan = await plan_click({ by: 'id', value: 'box' }, () => client.snapshot());
    assert.deepStrictEqual(plan, { kind: 'abort', reason: 'not_found', attempts: 3 });
    const methods = logRecords(client).map((r) => r.method);
    assert.deepStrictEqual(methods, ['Target.setAutoAttach',
      'Accessibility.getFullAXTree', 'DOMSnapshot.captureSnapshot',
      'Accessibility.getFullAXTree', 'DOMSnapshot.captureSnapshot',
      'Accessibility.getFullAXTree', 'DOMSnapshot.captureSnapshot']);
  });
});

test('two identical fixture runs and two separate fake runs give byte-identical plans', async () => {
  const plans = [];
  for (let run = 0; run < 2; run++) {
    let calls = 0;
    const click = await plan_click({ by: 'id', value: 'box' }, () => ++calls === 1 ? empty() : fixture());
    const abort = await useClient({}, async (client) => {
      await client.handshake();
      return plan_click({ by: 'id', value: 'box' }, () => client.snapshot());
    });
    plans.push(JSON.stringify({ click, abort }));
  }
  assert.strictEqual(plans[0], plans[1]);
});

test('closing during handshake rejects pending waits, is idempotent and reaps the child', async () => {
  await useClient({ mode: 'attach_blank' }, async (client) => {
    const rejected = assert.rejects(client.handshake(), { code: 5 });
    await client.close();
    await rejected;
    await client.close();
  });
});

test('invalid transport budgets fail before a child can be spawned', () => {
  for (const timeoutMs of [0, 10001, 2.5]) assert.throws(() => new FakeCdpClient({ timeoutMs }), { code: 2 });
  assert.throws(() => new FakeCdpClient({ maxFrameBytes: allowlist.max_frame_bytes + 1 }), { code: 2 });
});
