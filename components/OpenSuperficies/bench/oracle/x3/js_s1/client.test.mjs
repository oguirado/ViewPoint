import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, unlinkSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import {
  CDPClient,
  parseSnapshot,
  findNodeById,
  findNodeByRole,
  findFocusedNode,
  plan_click
} from './client.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const REPO_ROOT = join(__dirname, '../../../..');
const AX_FIXTURE_PATH = join(REPO_ROOT, 'bench/oracle/tests/fixtures/oracle_ax_focus.json');
const SNAPSHOT_FIXTURE_PATH = join(REPO_ROOT, 'bench/oracle/tests/fixtures/oracle_snapshot_basic.json');
const ALLOWLIST_PATH = join(REPO_ROOT, 'docker/cu/oracle/allowlist.json');
const FAKE_CHROMIUM_PATH = join(REPO_ROOT, 'docker/cu/tests/fixtures/cdp/fake_chromium.py');

const axFixture = JSON.parse(readFileSync(AX_FIXTURE_PATH, 'utf8'));
const snapshotFixture = JSON.parse(readFileSync(SNAPSHOT_FIXTURE_PATH, 'utf8'));

// Test 1: Handshake over real fake pipe binds S1 & FAKE_LOG contains only allow-listed methods and no Input or Runtime method
test('handshake binds S1 and FAKE_LOG contains only allow-listed methods', async () => {
  const logFile = join(tmpdir(), `fake_cdp_test1_${Date.now()}.log`);
  const client = new CDPClient({
    allowlistPath: ALLOWLIST_PATH,
    fakeChromiumPath: FAKE_CHROMIUM_PATH,
    fakeLog: logFile
  });

  try {
    await client.connect();
    assert.equal(client.sessionId, 'S1');

    await client.call('Accessibility.enable');
    await client.call('DOMSnapshot.enable');

    client.close();

    assert.ok(existsSync(logFile));
    const logContent = readFileSync(logFile, 'utf8');
    const logLines = logContent.split('\n').filter(Boolean);

    for (const line of logLines) {
      const parsed = JSON.parse(line);
      assert.ok(!parsed.method.startsWith('Input.'), `Unexpected method in log: ${parsed.method}`);
      assert.ok(!parsed.method.startsWith('Runtime.'), `Unexpected method in log: ${parsed.method}`);
      assert.ok(
        parsed.method === 'Target.setAutoAttach' ||
        parsed.method === 'Accessibility.enable' ||
        parsed.method === 'DOMSnapshot.enable',
        `Method ${parsed.method} not expected`
      );
    }
  } finally {
    client.close();
    if (existsSync(logFile)) unlinkSync(logFile);
  }
});

// Test 2: Local refusal of Input.dispatchMouseEvent and Page.navigate writes nothing to FAKE_LOG
test('local refusal of Input.dispatchMouseEvent and Page.navigate writes nothing to FAKE_LOG', async () => {
  const logFile = join(tmpdir(), `fake_cdp_test2_${Date.now()}.log`);
  const client = new CDPClient({
    allowlistPath: ALLOWLIST_PATH,
    fakeChromiumPath: FAKE_CHROMIUM_PATH,
    fakeLog: logFile
  });

  try {
    await client.connect();

    assert.throws(
      () => client.call('Input.dispatchMouseEvent', { type: 'mousePressed' }),
      (err) => err.code === 5
    );

    assert.throws(
      () => client.call('Page.navigate', { url: 'file:///opt/cu/scenes/v3/ff_form_01.html' }),
      (err) => err.code === 5
    );

    client.close();

    const logContent = existsSync(logFile) ? readFileSync(logFile, 'utf8') : '';
    assert.ok(!logContent.includes('Input.dispatchMouseEvent'));
    assert.ok(!logContent.includes('Page.navigate'));
  } finally {
    client.close();
    if (existsSync(logFile)) unlinkSync(logFile);
  }
});

// Test 3: Oversize frame from FAKE_MODE=oversize with FAKE_BYTES above small limit is refused
test('oversize frame under FAKE_MODE=oversize is refused', async () => {
  const client = new CDPClient({
    allowlistPath: ALLOWLIST_PATH,
    fakeChromiumPath: FAKE_CHROMIUM_PATH,
    fakeMode: 'oversize',
    fakeBytes: 5000,
    maxFrameBytes: 1000
  });

  await assert.rejects(
    async () => {
      await client.connect();
    },
    (err) => err.code === 5
  );

  client.close();
});

// Test 4: Snapshot fixture gives id box centre 420,440 in document coordinates and 420,376 in viewport
test('snapshot fixture gives id box centre 420,440 doc and 420,376 viewport', async () => {
  const provider = async () => ({ axTree: axFixture, snapshot: snapshotFixture });
  const plan = await plan_click(provider, { id: 'box' }, 1);

  assert.equal(plan.kind, 'click');
  assert.deepEqual(plan.xy, [420, 440]);
  assert.deepEqual(plan.viewport, [420, 376]);
  assert.equal(plan.attempts, 1);
});

// Test 5: Id cv is not visible and exhausts retries with abort not_visible
test('id cv is not visible and exhausts retries with abort not_visible', async () => {
  const provider = async () => ({ axTree: axFixture, snapshot: snapshotFixture });
  const plan = await plan_click(provider, { id: 'cv' }, 3);

  assert.equal(plan.kind, 'abort');
  assert.equal(plan.reason, 'not_visible');
});

// Test 6: Provider returning empty snapshot first and fixture second succeeds with attempts 2
test('provider returning empty snapshot first and fixture second succeeds with attempts 2', async () => {
  let callCount = 0;
  const provider = async () => {
    callCount++;
    if (callCount === 1) {
      return { axTree: { nodes: [] }, snapshot: { documents: [], strings: [] } };
    }
    return { axTree: axFixture, snapshot: snapshotFixture };
  };

  const plan = await plan_click(provider, { id: 'box' }, 3);

  assert.equal(plan.kind, 'click');
  assert.deepEqual(plan.xy, [420, 440]);
  assert.equal(plan.attempts, 2);
});

// Test 7: AX fixture focused node is nodeId 7 role textbox
test('AX fixture focused node is nodeId 7 role textbox', () => {
  const focusedResult = findFocusedNode(axFixture, snapshotFixture);
  assert.ok(focusedResult);
  assert.ok(focusedResult.axNode);
  assert.equal(focusedResult.axNode.nodeId, '7');
  const roleVal = typeof focusedResult.axNode.role === 'object' ? focusedResult.axNode.role.value : focusedResult.axNode.role;
  assert.equal(roleVal, 'textbox');
});

// Test 8: Refusal cases for type password, card_number id, and input type file
test('refusal cases for password type, card_number id, and file type', async () => {
  // Case A: password type
  const passwordSnapshot = JSON.parse(JSON.stringify(snapshotFixture));
  // Change attributes of node index 3 (backendNodeId 4, id="box") to include type="password"
  passwordSnapshot.strings.push('type', 'password');
  const typeIdx = passwordSnapshot.strings.length - 2;
  const pwdIdx = passwordSnapshot.strings.length - 1;
  passwordSnapshot.documents[0].nodes.attributes[3].push(typeIdx, pwdIdx);

  const planPwd = await plan_click(async () => ({ axTree: axFixture, snapshot: passwordSnapshot }), { id: 'box' }, 1);
  assert.equal(planPwd.kind, 'abort');
  assert.equal(planPwd.reason, 'refused');

  // Case B: card_number id
  const cardSnapshot = JSON.parse(JSON.stringify(snapshotFixture));
  cardSnapshot.strings.push('card_number');
  const cardStrIdx = cardSnapshot.strings.length - 1;
  // Replace string index for 'box' with 'card_number'
  cardSnapshot.documents[0].nodes.attributes[3][1] = cardStrIdx;

  const planCard = await plan_click(async () => ({ axTree: axFixture, snapshot: cardSnapshot }), { id: 'card_number' }, 1);
  assert.equal(planCard.kind, 'abort');
  assert.equal(planCard.reason, 'refused');

  // Case C: file type
  const fileSnapshot = JSON.parse(JSON.stringify(snapshotFixture));
  fileSnapshot.strings.push('type', 'file');
  const fileTypeIdx = fileSnapshot.strings.length - 2;
  const fileValIdx = fileSnapshot.strings.length - 1;
  fileSnapshot.documents[0].nodes.attributes[3].push(fileTypeIdx, fileValIdx);

  const planFile = await plan_click(async () => ({ axTree: axFixture, snapshot: fileSnapshot }), { id: 'box' }, 1);
  assert.equal(planFile.kind, 'abort');
  assert.equal(planFile.reason, 'refused');
});

// Test 9: Over fake pipe, empty results of getFullAXTree and captureSnapshot are handled as no nodes and exhaust retries
test('empty results over fake pipe exhaust retries', async () => {
  const client = new CDPClient({
    allowlistPath: ALLOWLIST_PATH,
    fakeChromiumPath: FAKE_CHROMIUM_PATH
  });

  try {
    await client.connect();

    const provider = async () => {
      const axTree = await client.call('Accessibility.getFullAXTree');
      const snapshot = await client.call('DOMSnapshot.captureSnapshot', { computedStyles: ['visibility', 'opacity', 'display'] });
      return { axTree, snapshot };
    };

    const plan = await plan_click(provider, { id: 'nonexistent' }, 2);
    assert.equal(plan.kind, 'abort');
    assert.equal(plan.reason, 'not_found');
  } finally {
    client.close();
  }
});

// Test 10: Two identical runs give byte-identical plans
test('two identical runs give byte-identical plans', async () => {
  const provider = async () => ({ axTree: axFixture, snapshot: snapshotFixture });

  const plan1 = await plan_click(provider, { id: 'box' }, 3);
  const plan2 = await plan_click(provider, { id: 'box' }, 3);

  const json1 = JSON.stringify(plan1);
  const json2 = JSON.stringify(plan2);

  assert.equal(json1, json2);
});
