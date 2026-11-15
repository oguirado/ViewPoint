import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {
  CdpClient,
  parseAXTree,
  findAXFocusedNode,
  parseDOMSnapshot,
  isSensitive,
  plan_click,
} from './client.mjs';

const AX_FIXTURE_PATH = 'bench/oracle/tests/fixtures/oracle_ax_focus.json';
const SNAPSHOT_FIXTURE_PATH = 'bench/oracle/tests/fixtures/oracle_snapshot_basic.json';

const axFixture = JSON.parse(fs.readFileSync(AX_FIXTURE_PATH, 'utf8'));
const snapshotFixture = JSON.parse(fs.readFileSync(SNAPSHOT_FIXTURE_PATH, 'utf8'));

test('handshake over real fake pipe binds S1 and FAKE_LOG has only allowed methods', async () => {
  const tmpLog = path.join(os.tmpdir(), `fake_log_${Date.now()}_${Math.random().toString(36).slice(2)}.log`);
  const client = new CdpClient({ fakeLog: tmpLog });

  try {
    const sid = await client.handshake();
    assert.equal(sid, 'S1');

    await client.sendRequest('Accessibility.enable');
    const axRes = await client.sendRequest('Accessibility.getFullAXTree');
    assert.ok(axRes);

    client.close();

    const logContent = fs.readFileSync(tmpLog, 'utf8');
    assert.ok(logContent.length > 0);
    const logLines = logContent.split('\n').filter(Boolean);

    for (const line of logLines) {
      const msg = JSON.parse(line);
      assert.doesNotMatch(msg.method || '', /^Input\./);
      assert.doesNotMatch(msg.method || '', /^Runtime\./);
      assert.notEqual(msg.method, 'Page.navigate');
    }
  } finally {
    client.close();
    if (fs.existsSync(tmpLog)) fs.unlinkSync(tmpLog);
  }
});

test('local refusal of Input.dispatchMouseEvent and Page.navigate writes nothing to FAKE_LOG', async () => {
  const tmpLog = path.join(os.tmpdir(), `fake_log_refusal_${Date.now()}_${Math.random().toString(36).slice(2)}.log`);
  const client = new CdpClient({ fakeLog: tmpLog });

  try {
    await client.handshake();

    await assert.rejects(async () => {
      await client.sendRequest('Input.dispatchMouseEvent', { type: 'mousePressed' });
    }, (err) => err.code === 5);

    await assert.rejects(async () => {
      await client.sendRequest('Page.navigate', { url: 'file:///etc/passwd' });
    }, (err) => err.code === 5);

    client.close();

    const logContent = fs.existsSync(tmpLog) ? fs.readFileSync(tmpLog, 'utf8') : '';
    assert.doesNotMatch(logContent, /Input\.dispatchMouseEvent/);
    assert.doesNotMatch(logContent, /Page\.navigate/);
  } finally {
    client.close();
    if (fs.existsSync(tmpLog)) fs.unlinkSync(tmpLog);
  }
});

test('oversize frame from FAKE_MODE=oversize with FAKE_BYTES above limit is refused', async () => {
  const client = new CdpClient({
    fakeMode: 'oversize',
    fakeBytes: 1000,
    maxFrameBytes: 256,
  });

  try {
    await assert.rejects(async () => {
      await client.handshake(2000);
    }, (err) => err.code === 5);
  } finally {
    client.close();
  }
});

test('snapshot fixture gives id box centre 420,440 in doc and 420,376 in viewport', () => {
  const parsed = parseDOMSnapshot(snapshotFixture);
  assert.ok(parsed);
  const boxNode = parsed.idMap.get('box');
  assert.ok(boxNode);
  assert.equal(boxNode.layout.cx, 420);
  assert.equal(boxNode.layout.cy, 440);
  assert.equal(boxNode.layout.viewportCx, 420);
  assert.equal(boxNode.layout.viewportCy, 376);
});

test('id cv is not visible and exhausts retries with abort not_visible', async () => {
  const provider = async () => ({
    axTree: axFixture,
    snapshot: snapshotFixture,
  });

  const plan = await plan_click({ id: 'cv' }, provider, 3);
  assert.deepEqual(plan, { kind: 'abort', reason: 'not_visible' });
});

test('provider returning empty snapshot first and fixture second succeeds with attempts 2', async () => {
  let count = 0;
  const provider = async () => {
    count++;
    if (count === 1) {
      return { axTree: { nodes: [] }, snapshot: { documents: [] } };
    }
    return { axTree: axFixture, snapshot: snapshotFixture };
  };

  const plan = await plan_click({ id: 'box' }, provider, 3);
  assert.deepEqual(plan, {
    kind: 'click',
    xy: [420, 440],
    viewport_xy: [420, 376],
    attempts: 2,
  });
});

test('AX fixture focused node is nodeId 7 role textbox', () => {
  const nodes = parseAXTree(axFixture);
  const focused = findAXFocusedNode(nodes);
  assert.ok(focused);
  assert.equal(focused.nodeId, '7');
  assert.equal(focused.role, 'textbox');
});

test('refusal cases for type password, card_number id, and input type file', async () => {
  assert.equal(isSensitive('password', 'f1', 'f1', null), true);
  assert.equal(isSensitive('file', 'f2', 'f2', null), true);
  assert.equal(isSensitive('text', 'card_number', 'card_number', null), true);
  assert.equal(isSensitive('text', 'regular_field', 'regular_field', null), false);

  const pwdSnapshot = {
    documents: [{
      scrollOffsetX: 0,
      scrollOffsetY: 0,
      nodes: {
        attributes: [[0, 1, 2, 3]],
        backendNodeId: [10],
      },
      layout: {
        nodeIndex: [0],
        bounds: [[0, 0, 100, 50]],
        styles: [[4, 5, 6]],
      },
    }],
    strings: ['id', 'pwd', 'type', 'password', 'visible', '1', 'block'],
  };

  const planPwd = await plan_click({ id: 'pwd' }, async () => ({
    axTree: { nodes: [] },
    snapshot: pwdSnapshot,
  }));
  assert.deepEqual(planPwd, { kind: 'abort', reason: 'refused' });

  const cardSnapshot = {
    documents: [{
      scrollOffsetX: 0,
      scrollOffsetY: 0,
      nodes: {
        attributes: [[0, 1]],
        backendNodeId: [11],
      },
      layout: {
        nodeIndex: [0],
        bounds: [[0, 0, 100, 50]],
        styles: [[2, 3, 4]],
      },
    }],
    strings: ['id', 'card_number', 'visible', '1', 'block'],
  };

  const planCard = await plan_click({ id: 'card_number' }, async () => ({
    axTree: { nodes: [] },
    snapshot: cardSnapshot,
  }));
  assert.deepEqual(planCard, { kind: 'abort', reason: 'refused' });

  const fileSnapshot = {
    documents: [{
      scrollOffsetX: 0,
      scrollOffsetY: 0,
      nodes: {
        attributes: [[0, 1, 2, 3]],
        backendNodeId: [12],
      },
      layout: {
        nodeIndex: [0],
        bounds: [[0, 0, 100, 50]],
        styles: [[4, 5, 6]],
      },
    }],
    strings: ['id', 'upl', 'type', 'file', 'visible', '1', 'block'],
  };

  const planFile = await plan_click({ id: 'upl' }, async () => ({
    axTree: { nodes: [] },
    snapshot: fileSnapshot,
  }));
  assert.deepEqual(planFile, { kind: 'abort', reason: 'refused' });
});

test('over fake pipe empty results of getFullAXTree and captureSnapshot exhaust retries with not_found', async () => {
  const tmpLog = path.join(os.tmpdir(), `fake_log_empty_${Date.now()}_${Math.random().toString(36).slice(2)}.log`);
  const client = new CdpClient({ fakeLog: tmpLog });

  try {
    await client.handshake();
    await client.sendRequest('Accessibility.enable');
    await client.sendRequest('DOMSnapshot.enable');

    const provider = async () => {
      const axTree = await client.sendRequest('Accessibility.getFullAXTree');
      const snapshot = await client.sendRequest('DOMSnapshot.captureSnapshot', {
        computedStyles: ['visibility', 'opacity', 'display'],
      });
      return { axTree, snapshot };
    };

    const plan = await plan_click({ id: 'nonexistent' }, provider, 2);
    assert.deepEqual(plan, { kind: 'abort', reason: 'not_found' });
  } finally {
    client.close();
    if (fs.existsSync(tmpLog)) fs.unlinkSync(tmpLog);
  }
});

test('two identical runs give byte-identical plans', async () => {
  const provider = async () => ({
    axTree: axFixture,
    snapshot: snapshotFixture,
  });

  const plan1 = await plan_click({ id: 'box' }, provider, 3);
  const plan2 = await plan_click({ id: 'box' }, provider, 3);

  const str1 = JSON.stringify(plan1);
  const str2 = JSON.stringify(plan2);

  assert.equal(str1, str2);
  assert.equal(Buffer.from(str1).equals(Buffer.from(str2)), true);
});
