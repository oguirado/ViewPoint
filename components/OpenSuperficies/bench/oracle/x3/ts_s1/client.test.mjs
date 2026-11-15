import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as crypto from 'node:crypto';
import {
  CDPClient,
  loadAllowlist,
  parseAXTree,
  findAXFocusedNode,
  findAXNodeByRole,
  parseDOMSnapshot,
  isNodeVisible,
  findNodeById,
  isSensitiveNode,
  plan_click,
} from './client.mjs';

const clientTsPath = path.resolve('bench/oracle/x3/ts_s1/client.ts');
const clientMjsPath = path.resolve('bench/oracle/x3/ts_s1/client.mjs');
const axFixturePath = path.resolve('bench/oracle/tests/fixtures/oracle_ax_focus.json');
const snapshotFixturePath = path.resolve('bench/oracle/tests/fixtures/oracle_snapshot_basic.json');

const axFixture = JSON.parse(fs.readFileSync(axFixturePath, 'utf-8'));
const snapshotFixture = JSON.parse(fs.readFileSync(snapshotFixturePath, 'utf-8'));

test('SHA256 header in client.mjs matches client.ts', () => {
  const tsContent = fs.readFileSync(clientTsPath);
  const tsHash = crypto.createHash('sha256').update(tsContent).digest('hex');

  const mjsContent = fs.readFileSync(clientMjsPath, 'utf-8');
  const firstLine = mjsContent.split('\n')[0].trim();
  const expectedComment = `// ${tsHash}`;

  assert.equal(firstLine, expectedComment, `Expected header '${expectedComment}', got '${firstLine}'`);
});

test('CDP Handshake over fake_chromium pipe binds session S1 and logs only allowed methods', async () => {
  const tmpLog = path.resolve(`test_fake_${Date.now()}.log`);

  const client = new CDPClient({
    fakeLog: tmpLog,
    fakeUrl: 'file:///opt/cu/scenes/v3/ff_form_01.html',
    timeoutMs: 5000,
  });

  try {
    const sessionId = await client.start();
    assert.equal(sessionId, 'S1');
    assert.equal(client.getSessionId(), 'S1');

    // Send a session call
    const axTree = await client.send('Accessibility.getFullAXTree', {});
    assert.ok(axTree);
  } finally {
    client.close();
  }

  // Check FAKE_LOG
  assert.ok(fs.existsSync(tmpLog));
  const logLines = fs.readFileSync(tmpLog, 'utf-8').trim().split('\n').filter(Boolean);
  const allowlist = loadAllowlist();

  for (const line of logLines) {
    const req = JSON.parse(line);
    const m = req.method;
    assert.ok(m, 'Logged entry must have method');

    const inBrowser = Object.prototype.hasOwnProperty.call(allowlist.browser_methods, m);
    const inSession = Object.prototype.hasOwnProperty.call(allowlist.session_methods, m);
    assert.ok(inBrowser || inSession, `Method ${m} in log must be allowlisted`);

    assert.ok(!m.startsWith('Input.'), `Method ${m} must not be Input`);
    assert.ok(!m.startsWith('Runtime.'), `Method ${m} must not be Runtime`);
  }

  fs.unlinkSync(tmpLog);
});

test('Local refusal of Input.dispatchMouseEvent and Page.navigate writes nothing to FAKE_LOG', async () => {
  const tmpLog = path.resolve(`test_refusal_${Date.now()}.log`);

  const client = new CDPClient({
    fakeLog: tmpLog,
    fakeUrl: 'file:///opt/cu/scenes/v3/ff_form_01.html',
    timeoutMs: 5000,
  });

  try {
    await client.start();

    // Refusal: Input method
    await assert.rejects(
      () => client.send('Input.dispatchMouseEvent', { type: 'mousePressed' }),
      (err) => err.code === 5
    );

    // Refusal: Page.navigate always
    await assert.rejects(
      () => client.send('Page.navigate', { url: 'file:///opt/cu/scenes/v3/ff_form_01.html' }),
      (err) => err.code === 5
    );
  } finally {
    client.close();
  }

  // Check FAKE_LOG
  const logLines = fs.readFileSync(tmpLog, 'utf-8').trim().split('\n').filter(Boolean);
  for (const line of logLines) {
    const req = JSON.parse(line);
    assert.notEqual(req.method, 'Input.dispatchMouseEvent');
    assert.notEqual(req.method, 'Page.navigate');
  }

  fs.unlinkSync(tmpLog);
});

test('Oversize frame from FAKE_MODE=oversize is refused', async () => {
  const client = new CDPClient({
    fakeMode: 'oversize',
    fakeBytes: 1000,
    maxFrameBytesOverride: 500, // lower limit to trigger refusal
    timeoutMs: 2000,
  });

  try {
    await assert.rejects(
      () => client.start(),
      (err) => err.code === 5
    );
  } finally {
    client.close();
  }
});

test('DOMSnapshot fixture box element centre coordinates computation', () => {
  const nodes = parseDOMSnapshot(snapshotFixture);
  const boxNode = nodes.find((n) => n.attributes.id === 'box');
  assert.ok(boxNode, 'box node should exist');

  const [x, y, w, h] = boxNode.bounds;
  const cx = x + Math.floor(w / 2);
  const cy = y + Math.floor(h / 2);
  const vcx = cx - boxNode.scrollOffsetX;
  const vcy = cy - boxNode.scrollOffsetY;

  assert.equal(cx, 420);
  assert.equal(cy, 440);
  assert.equal(vcx, 420);
  assert.equal(vcy, 376);
});

test('Element cv is not visible and exhausts retries with abort not_visible', async () => {
  const provider = async () => ({
    axData: axFixture,
    snapshotData: snapshotFixture,
  });

  const res = await plan_click({ id: 'cv' }, provider, 3);
  assert.deepEqual(res, {
    kind: 'abort',
    reason: 'not_visible',
  });
});

test('Retry provider returning empty snapshot first and fixture second succeeds with attempts 2', async () => {
  let callCount = 0;
  const provider = async () => {
    callCount++;
    if (callCount === 1) {
      return { axData: { nodes: [] }, snapshotData: { documents: [] } };
    }
    return { axData: axFixture, snapshotData: snapshotFixture };
  };

  const res = await plan_click({ id: 'box' }, provider, 3);
  assert.equal(res.kind, 'click');
  assert.equal(res.attempts, 2);
  assert.deepEqual(res.xy, [420, 440]);
  assert.deepEqual(res.viewport_xy, [420, 376]);
});

test('AX fixture focused node query finds nodeId 7 role textbox', () => {
  const focusedNode = findAXFocusedNode(axFixture);
  assert.ok(focusedNode);
  assert.equal(focusedNode.nodeId, '7');
  const roleVal = typeof focusedNode.role === 'object' ? focusedNode.role.value : focusedNode.role;
  assert.equal(roleVal, 'textbox');
});

test('Sensitivity refusal cases for password, card_number, and input type file', () => {
  const passwordNode = {
    backendNodeId: 10,
    attributes: { type: 'password', id: 'pass' },
    bounds: [0, 0, 100, 30],
    computedStyle: { visibility: 'visible', opacity: '1', display: 'block' },
    scrollOffsetX: 0,
    scrollOffsetY: 0,
  };
  assert.ok(isSensitiveNode(passwordNode));

  const cardNode = {
    backendNodeId: 11,
    attributes: { id: 'card_number' },
    bounds: [0, 0, 100, 30],
    computedStyle: { visibility: 'visible', opacity: '1', display: 'block' },
    scrollOffsetX: 0,
    scrollOffsetY: 0,
  };
  assert.ok(isSensitiveNode(cardNode));

  const fileNode = {
    backendNodeId: 12,
    attributes: { type: 'file' },
    bounds: [0, 0, 100, 30],
    computedStyle: { visibility: 'visible', opacity: '1', display: 'block' },
    scrollOffsetX: 0,
    scrollOffsetY: 0,
  };
  assert.ok(isSensitiveNode(fileNode));
});

test('Over fake pipe, empty results of getFullAXTree and captureSnapshot exhaust retries with not_found', async () => {
  const client = new CDPClient({
    fakeUrl: 'file:///opt/cu/scenes/v3/ff_form_01.html',
    timeoutMs: 5000,
  });

  try {
    await client.start();

    const provider = async () => {
      const axData = await client.send('Accessibility.getFullAXTree', {});
      const snapshotData = await client.send('DOMSnapshot.captureSnapshot', { computedStyles: ['visibility', 'opacity', 'display'] });
      return { axData, snapshotData };
    };

    const res = await plan_click({ id: 'box' }, provider, 3);
    assert.deepEqual(res, {
      kind: 'abort',
      reason: 'not_found',
    });
  } finally {
    client.close();
  }
});

test('Two identical runs give byte-identical plans', async () => {
  const provider = async () => ({
    axData: axFixture,
    snapshotData: snapshotFixture,
  });

  const res1 = await plan_click({ id: 'box' }, provider, 3);
  const res2 = await plan_click({ id: 'box' }, provider, 3);

  const json1 = JSON.stringify(res1);
  const json2 = JSON.stringify(res2);

  assert.equal(json1, json2);
});
