import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as crypto from 'node:crypto';
import {
  CDPClient,
  CDPError,
  parseAXTree,
  parseSnapshot,
  findSnapshotNodeById,
  findFocusedAXNode,
  findAXNodeByRole,
  isNodeVisible,
  isRefusedNode,
  plan_click
} from './client.mjs';

const allowlistPath = 'docker/cu/oracle/allowlist.json';
const fakeChromiumPath = 'docker/cu/tests/fixtures/cdp/fake_chromium.py';
const axFocusPath = 'bench/oracle/tests/fixtures/oracle_ax_focus.json';
const snapshotPath = 'bench/oracle/tests/fixtures/oracle_snapshot_basic.json';
const fakeUrl = 'file:///opt/cu/scenes/v3/ff_form_01.html';

test('client.mjs first line holds sha256 hex of client.ts', () => {
  const tsContent = fs.readFileSync('bench/oracle/x3/ts_s3/client.ts');
  const expectedSha256 = crypto.createHash('sha256').update(tsContent).digest('hex');

  const mjsContent = fs.readFileSync('bench/oracle/x3/ts_s3/client.mjs', 'utf-8');
  const firstLine = mjsContent.split('\n')[0].trim();
  const match = firstLine.match(/^\/\/\s*([a-f0-9]{64})$/);

  assert.ok(match, 'First line of client.mjs must be // <sha256_hex>');
  assert.equal(match[1], expectedSha256);
});

test('handshake over real fake pipe binds S1 and FAKE_LOG contains only allowlisted methods', async () => {
  const fakeLog = path.join('/dev/shm', `ts_s3_test_log_${Date.now()}_1.log`);
  if (fs.existsSync(fakeLog)) fs.unlinkSync(fakeLog);

  const client = new CDPClient(allowlistPath);
  try {
    const sessionId = await client.connect(fakeChromiumPath, fakeUrl, fakeLog, 'echo');
    assert.equal(sessionId, 'S1');

    const logData = fs.readFileSync(fakeLog, 'utf-8').trim();
    const logLines = logData ? logData.split('\n') : [];

    assert.ok(logLines.length > 0, 'FAKE_LOG should contain logged frames');
    const allowlist = JSON.parse(fs.readFileSync(allowlistPath, 'utf-8'));
    const allowedMethods = new Set([
      ...Object.keys(allowlist.browser_methods || {}),
      ...Object.keys(allowlist.session_methods || {})
    ]);

    for (const line of logLines) {
      const obj = JSON.parse(line);
      if (obj.method) {
        assert.ok(allowedMethods.has(obj.method), `Method ${obj.method} in FAKE_LOG must be allowlisted`);
        assert.ok(!obj.method.startsWith('Input.'), `Method ${obj.method} must not be an Input method`);
        assert.ok(!obj.method.startsWith('Runtime.'), `Method ${obj.method} must not be a Runtime method`);
      }
    }
  } finally {
    client.close();
    if (fs.existsSync(fakeLog)) fs.unlinkSync(fakeLog);
  }
});

test('local refusal of Input.dispatchMouseEvent and Page.navigate writes nothing to FAKE_LOG', async () => {
  const fakeLog = path.join('/dev/shm', `ts_s3_test_log_${Date.now()}_2.log`);
  if (fs.existsSync(fakeLog)) fs.unlinkSync(fakeLog);

  const client = new CDPClient(allowlistPath);
  try {
    await client.connect(fakeChromiumPath, fakeUrl, fakeLog, 'echo');
    const logBefore = fs.existsSync(fakeLog) ? fs.readFileSync(fakeLog, 'utf-8') : '';

    await assert.rejects(
      async () => {
        await client.send('Input.dispatchMouseEvent', { type: 'mousePressed' });
      },
      (err) => err instanceof CDPError && err.code === 5
    );

    await assert.rejects(
      async () => {
        await client.send('Page.navigate', { url: 'http://example.com' });
      },
      (err) => err instanceof CDPError && err.code === 5
    );

    const logAfter = fs.readFileSync(fakeLog, 'utf-8');
    assert.equal(logAfter, logBefore, 'FAKE_LOG must not be modified by locally refused calls');
  } finally {
    client.close();
    if (fs.existsSync(fakeLog)) fs.unlinkSync(fakeLog);
  }
});

test('oversize frame from FAKE_MODE=oversize is refused', async () => {
  const client = new CDPClient(allowlistPath, 1000);
  try {
    await assert.rejects(
      async () => {
        await client.connect(fakeChromiumPath, fakeUrl, undefined, 'oversize', '5000');
      },
      (err) => err instanceof CDPError && err.code === 5
    );
  } finally {
    client.close();
  }
});

test('snapshot fixture gives id box center 420,440 doc and 420,376 viewport', () => {
  const rawSnapshot = JSON.parse(fs.readFileSync(snapshotPath, 'utf-8'));
  const doc = parseSnapshot(rawSnapshot);
  assert.ok(doc, 'Snapshot doc should parse');

  const boxNode = findSnapshotNodeById(doc, 'box');
  assert.ok(boxNode, 'Node with id box should be found');
  assert.ok(isNodeVisible(boxNode), 'Node box should be visible');

  const [x, y, w, h] = boxNode.bounds;
  const cx = Math.floor(x + Math.floor(w / 2));
  const cy = Math.floor(y + Math.floor(h / 2));
  const vx = cx - doc.scrollOffsetX;
  const vy = cy - doc.scrollOffsetY;

  assert.equal(cx, 420);
  assert.equal(cy, 440);
  assert.equal(vx, 420);
  assert.equal(vy, 376);
});

test('id cv is not visible and exhausts retries with abort not_visible', async () => {
  const rawSnapshot = JSON.parse(fs.readFileSync(snapshotPath, 'utf-8'));
  const plan = await plan_click({ id: 'cv' }, () => rawSnapshot, 3);

  assert.deepEqual(plan, { kind: 'abort', reason: 'not_visible' });
});

test('provider returning empty snapshot first and fixture second succeeds with attempts 2', async () => {
  const rawSnapshot = JSON.parse(fs.readFileSync(snapshotPath, 'utf-8'));
  let callCount = 0;
  const provider = () => {
    callCount++;
    if (callCount === 1) {
      return { documents: [], strings: [] };
    }
    return rawSnapshot;
  };

  const plan = await plan_click({ id: 'box' }, provider, 3);
  assert.deepEqual(plan, {
    kind: 'click',
    xy: [420, 440],
    viewport_xy: [420, 376],
    attempts: 2
  });
});

test('AX fixture focused node is nodeId 7 role textbox', () => {
  const rawAx = JSON.parse(fs.readFileSync(axFocusPath, 'utf-8'));
  const nodes = parseAXTree(rawAx);
  const focusedNode = findFocusedAXNode(nodes);

  assert.ok(focusedNode, 'Focused AX node should be found');
  assert.equal(focusedNode.nodeId, '7');
  const roleVal = typeof focusedNode.role === 'object' ? focusedNode.role?.value : focusedNode.role;
  assert.equal(roleVal, 'textbox');
});

test('refusal cases for type password, card_number id, and input type file', async () => {
  // Case 1: type password
  const pwdDoc = {
    documents: [{
      scrollOffsetX: 0,
      scrollOffsetY: 0,
      layout: { nodeIndex: [0], bounds: [[0, 0, 100, 100]], styles: [[0, 1, 2]] },
      nodes: {
        backendNodeId: [10],
        attributes: [[0, 1, 2, 3]], // id="pwd", type="password"
        nodeName: [4],
        nodeType: [1]
      }
    }],
    strings: ['id', 'pwd', 'type', 'password', 'INPUT', 'visible', '1', 'block']
  };

  const pwdPlan = await plan_click({ id: 'pwd' }, () => pwdDoc, 3);
  assert.deepEqual(pwdPlan, { kind: 'abort', reason: 'refused' });

  // Case 2: card_number id
  const cardPlan = await plan_click({ id: 'card_number' }, () => ({}), 3);
  assert.deepEqual(cardPlan, { kind: 'abort', reason: 'refused' });

  // Case 3: input type file
  const fileDoc = {
    documents: [{
      scrollOffsetX: 0,
      scrollOffsetY: 0,
      layout: { nodeIndex: [0], bounds: [[0, 0, 100, 100]], styles: [[0, 1, 2]] },
      nodes: {
        backendNodeId: [20],
        attributes: [[0, 1, 2, 3]], // id="file_input", type="file"
        nodeName: [4],
        nodeType: [1]
      }
    }],
    strings: ['id', 'file_input', 'type', 'file', 'INPUT', 'visible', '1', 'block']
  };

  const filePlan = await plan_click({ id: 'file_input' }, () => fileDoc, 3);
  assert.deepEqual(filePlan, { kind: 'abort', reason: 'refused' });
});

test('over fake pipe empty results of getFullAXTree and captureSnapshot are handled as no nodes and exhaust retries', async () => {
  const client = new CDPClient(allowlistPath);
  try {
    await client.connect(fakeChromiumPath, fakeUrl, undefined, 'echo');

    const axProvider = async () => client.send('Accessibility.getFullAXTree');
    const snapshotProvider = async () => client.send('DOMSnapshot.captureSnapshot', { computedStyles: ['visibility', 'opacity', 'display'] });

    const plan = await plan_click({ id: 'box' }, snapshotProvider, 3, axProvider);
    assert.deepEqual(plan, { kind: 'abort', reason: 'not_found' });
  } finally {
    client.close();
  }
});

test('two identical runs give byte-identical plans', async () => {
  const rawSnapshot = JSON.parse(fs.readFileSync(snapshotPath, 'utf-8'));
  const plan1 = await plan_click({ id: 'box' }, () => rawSnapshot, 3);
  const plan2 = await plan_click({ id: 'box' }, () => rawSnapshot, 3);

  const json1 = JSON.stringify(plan1);
  const json2 = JSON.stringify(plan2);

  assert.equal(json1, json2);
  assert.equal(Buffer.from(json1).toString('hex'), Buffer.from(json2).toString('hex'));
});
