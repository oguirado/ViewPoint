#!/usr/bin/env python3
"""Offline AX pipe oracle. No real browser, X server or Docker. Exits 0/5."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('ax_shim', ROOT / 'docker/identity/ax-shim.py')
ax = importlib.util.module_from_spec(SPEC)
try:
    SPEC.loader.exec_module(ax)
except OSError:
    print('AX shim absent', file=sys.stderr)
    sys.exit(5)


def fixture(count=1):
    strings = ['#document', 'INPUT', 'type', 'text', 'display', 'visibility', 'opacity',
               'block', 'visible', '1', 'password', 'autocomplete', 'cc-number']
    nodes = {'parentIndex': [-1, 0] + [1] * count,
             'backendNodeId': [1, 2] + list(range(3, 3 + count)),
             'nodeName': [0, 0] + [1] * count,
             'attributes': [[], []] + [[2, 3]] * count,
             'nodeValue': [0] * (count + 2)}
    tree = [{'nodeId': 'a', 'backendDOMNodeId': 2, 'role': {'value': 'main'},
             'name': {'value': 'Main'}, 'childIds': [str(i) for i in range(count)]}]
    for i in range(count):
        tree.append({'nodeId': str(i), 'parentId': 'a', 'backendDOMNodeId': i + 3,
                     'role': {'value': 'textbox'}, 'name': {'value': 'N' * 130},
                     'value': {'value': 'private'},
                     'properties': [{'name': 'focusable', 'value': {'value': True}}]})
    snap = {'strings': strings, 'documents': [{'nodes': nodes, 'scrollOffsetX': 0,
            'scrollOffsetY': 20, 'layout': {'nodeIndex': [1] + list(range(2, count + 2)),
            'bounds': [[0, 20, 1280, 800]] + [[10, 30 + i, 100, 20] for i in range(count)],
            'styles': [[7, 8, 9]] * (count + 1)}}]}
    window = {'id': 'window-1', 'x': 0, 'y': 0, 'w': 1280, 'h': 900,
              'title': 'Fixture - Chromium', 'url': 'https://fixture.invalid/'}
    metrics = {'cssVisualViewport': {'clientWidth': 1280, 'clientHeight': 800,
                                    'offsetX': 0, 'offsetY': 0, 'scale': 1}}
    return window, tree, snap, metrics


class Tests(unittest.TestCase):
    def policy(self):
        return ax.Policy(json.loads((ROOT / 'docker/identity/ax-allowlist.json').read_text()))

    def test_closed_policy(self):
        for method in ['Runtime.evaluate', 'Network.getCookies', 'Browser.getVersion',
                       'Page.navigate', 'DOM.resolveNode', 'Input.dispatchKeyEvent']:
            p = self.policy()
            with self.assertRaises(ax.Refused):
                p.check(method, {}, None)
            with self.assertRaises(ax.Refused):
                p.check('Target.getTargets', {}, None)
        p = self.policy()
        p.check('Target.getTargets', {}, None)
        with self.assertRaises(ax.Refused):
            p.check('Target.attachToTarget', {'targetId': 'other', 'flatten': True}, None)
        p = self.policy()
        p.targets = {'page'}
        p.check('Target.attachToTarget', {'targetId': 'page', 'flatten': True}, None)
        p.sessions.add('session')
        p.check('Accessibility.getFullAXTree', {}, 'session')
        with self.assertRaises(ax.Refused):
            p.check('DOMSnapshot.captureSnapshot', {'computedStyles': ['color']}, 'session')

    def test_pipe_fake_browser(self):
        a, b = socket.socketpair()
        def browser():
            try:
                data = b.recv(4096)
                request = json.loads(data.rstrip(b'\0'))
                response = json.dumps({'id': request['id'], 'result': {'targetInfos': []}}).encode() + b'\0'
                b.sendall(response[:4]); b.sendall(response[4:])
            finally:
                b.close()
        worker = threading.Thread(target=browser)
        worker.start()
        try:
            pipe = ax.Pipe(a.fileno(), a.fileno(), self.policy())
            self.assertEqual(pipe.call('Target.getTargets'), {'targetInfos': []})
        finally:
            a.close(); worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_oversize_sticky(self):
        framer = ax.Framer(8)
        self.assertEqual(framer.feed(b'{}\0'), [b'{}'])
        with self.assertRaises(ax.Refused):
            framer.feed(b'x' * 9)
        with self.assertRaises(ax.Refused):
            framer.feed(b'{}\0')

    def test_mapping(self):
        window, tree, snap, metrics = fixture(3)
        result = ax.map_snapshot(window, 7, tree, snap, metrics)
        fields = result['elements'][1:]
        self.assertEqual(result['frame_seq'], 7)
        self.assertEqual(fields[0]['box'], [10, 110, 100, 20])
        self.assertEqual(fields[0]['region'], 'main')
        self.assertEqual(fields[0]['value_len'], 7)
        self.assertEqual(len(fields[0]['name']), 120)
        self.assertTrue(fields[0]['actionable'])
        self.assertEqual(fields[0]['container']['ordinal'], 0)
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result, ax.map_snapshot(window, 7, tree, snap, metrics))

    def test_sensitive_hidden_and_text(self):
        w, tree, snap, metrics = fixture(3)
        snap['documents'][0]['nodes']['attributes'][2] = [2, 10]
        snap['documents'][0]['nodes']['attributes'][3] = [11, 12]
        result = ax.map_snapshot(w, 7, tree, snap, metrics)
        self.assertEqual(len(result['elements']), 2)
        with self.assertRaises(ax.Refused):
            ax.map_snapshot(w, 7, tree, snap, metrics, ax.node_id(w['id'], 2))
        with self.assertRaises(ax.Refused):
            ax.map_snapshot(w, 7, tree, snap, metrics, 'absent')
        w, tree, snap, metrics = fixture()
        result = ax.map_snapshot(w, 7, tree, snap, metrics, ax.node_id(w['id'], 2))
        self.assertEqual(result['text']['bytes'], len(result['text']['text'].encode()))
        self.assertNotIn('private', result['text']['text'])
        snap['documents'][0]['layout']['styles'][1] = [7, 0, 9]
        result = ax.map_snapshot(w, 7, tree, snap, metrics)
        self.assertEqual(len(result['elements']), 1)

    def test_cap_and_geometry(self):
        w, tree, snap, metrics = fixture(400)
        result = ax.map_snapshot(w, 7, tree, snap, metrics)
        self.assertEqual(len(result['elements']), 300)
        self.assertTrue(result['truncated'])
        self.assertTrue(all(e['actionable'] for e in result['elements']))
        metrics['cssVisualViewport']['clientWidth'] = 1288
        with self.assertRaises(ax.Refused):
            ax.map_snapshot(w, 7, tree, snap, metrics)

    def test_frame_border_offset(self):
        # Measured live (Chromium 152, 1280x900 window): viewport 1272x809, 4 px border on the
        # left, right and bottom; a CSS box at (100, 200) lands at window (104, 287).
        w, tree, snap, metrics = fixture(1)
        metrics['cssVisualViewport']['clientWidth'] = 1272
        metrics['cssVisualViewport']['clientHeight'] = 809
        snap['documents'][0]['layout']['bounds'][1] = [100, 200, 120, 40]
        snap['documents'][0]['scrollOffsetX'] = snap['documents'][0]['scrollOffsetY'] = 0
        boxes = [e['box'] for e in ax.map_snapshot(w, 7, tree, snap, metrics)['elements']]
        self.assertIn([104, 287, 120, 40], boxes)

    def test_text_bound(self):
        w, tree, snap, metrics = fixture(600)
        tree[1]['role']['value'] = 'StaticText'
        tree[1]['name']['value'] = 'Z' * 70000
        result = ax.map_snapshot(w, 7, tree, snap, metrics, ax.node_id(w['id'], 2))
        self.assertEqual(result['text']['bytes'], 65536)

    def test_absent_attribute_value_and_text_with_line_boxes(self):
        # Measured live on perplexity.ai: DOMSnapshot writes -1 for a valueless attribute
        # (e.g. `disabled`), and every StaticText has InlineTextBox children.
        w, tree, snap, metrics = fixture(3)
        snap['documents'][0]['nodes']['attributes'][2] = [2, -1]
        tree[1]['role']['value'] = 'StaticText'
        tree[1]['name']['value'] = 'Canberra'
        tree[1]['childIds'] = ['inline-1']
        result = ax.map_snapshot(w, 7, tree, snap, metrics, ax.node_id(w['id'], 2))
        self.assertEqual(result['text']['text'], 'Canberra')
        snap['documents'][0]['nodes']['attributes'][2] = [2, -2]
        with self.assertRaises(ax.Refused):
            ax.map_snapshot(w, 7, tree, snap, metrics)


    def test_title_mismatch_falls_back_to_window_bounds(self):
        # Measured live: a page title with non-ASCII text can read differently through xdotool and
        # CDP, so no title matches; the page is then chosen by its browser window rectangle.
        w, tree, snap, metrics = fixture(3)
        calls = []
        class FakePipe:
            policy = self.policy()
            def call(self, method, params=None, session=None):
                calls.append(method)
                if method == 'Target.getTargets':
                    return {'targetInfos': [{'targetId': 'a', 'type': 'page', 'title': 'Other', 'url': w['url']},
                                            {'targetId': 'b', 'type': 'page', 'title': 'Caf\u00e9', 'url': w['url']}]}
                if method == 'Browser.getWindowForTarget':
                    assert params['targetId'] in self.policy.targets
                    rect = (w['x'], w['y'], w['w'], w['h']) if params['targetId'] == 'b' else (9, 9, 9, 9)
                    return {'bounds': dict(zip(('left', 'top', 'width', 'height'), rect))}
                return {'Target.attachToTarget': {'sessionId': 's'}, 'Accessibility.enable': {},
                        'Accessibility.getFullAXTree': {'nodes': tree}, 'DOMSnapshot.captureSnapshot': snap,
                        'Page.getLayoutMetrics': metrics}[method]
        pipe = FakePipe()
        with patch.object(ax, 'resolve_window', side_effect=lambda _: dict(w, title='Caf? - Chromium', url='')):
            result = ax.snapshot(pipe, {'op': 'ax', 'window_title_or_index': 'w1', 'frame_seq': 7})
        self.assertEqual(pipe.policy.targets, {'b'})
        self.assertEqual(calls.count('Browser.getWindowForTarget'), 2)
        self.assertTrue(result['elements'])


    def test_complete_fake_snapshot(self):
        w, tree, snap, metrics = fixture(3)
        a, b = socket.socketpair()
        methods = []
        expected = ['Target.getTargets', 'Target.attachToTarget', 'Accessibility.enable',
                    'Accessibility.getFullAXTree', 'DOMSnapshot.captureSnapshot', 'Page.getLayoutMetrics']
        failures = []
        def browser():
            try:
                framer = ax.Framer()
                for method in expected:
                    frames = []
                    while not frames:
                        chunk = b.recv(65536)
                        if not chunk:
                            raise RuntimeError('unexpected eof')
                        frames.extend(framer.feed(chunk))
                    request = json.loads(frames[0])
                    methods.append(request['method'])
                    result = {
                        'Target.getTargets': {'targetInfos': [{'targetId': 'page', 'type': 'page',
                            'title': 'Fixture', 'url': w['url']}, {'targetId': 'worker', 'type': 'worker'}]},
                        'Target.attachToTarget': {'sessionId': 'session'},
                        'Accessibility.enable': {}, 'Accessibility.getFullAXTree': {'nodes': tree},
                        'DOMSnapshot.captureSnapshot': snap, 'Page.getLayoutMetrics': metrics}[method]
                    reply = {'id': request['id'], 'result': result}
                    if 'sessionId' in request:
                        reply['sessionId'] = request['sessionId']
                    b.sendall(json.dumps(reply).encode() + b'\0')
            except Exception as exc:
                failures.append(type(exc).__name__)
            finally:
                b.close()
        b.settimeout(2)
        worker = threading.Thread(target=browser)
        worker.start()
        try:
            pipe = ax.Pipe(a.fileno(), a.fileno(), self.policy())
            with patch.object(ax, 'resolve_window', side_effect=lambda _: dict(w, url='')):
                result = ax.snapshot(pipe, {'op': 'ax', 'window_title_or_index': w['title'], 'frame_seq': 7})
            self.assertEqual(result['elements'][1]['box'], [10, 110, 100, 20])
        finally:
            a.close(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(methods, expected)

    def test_launch_real_fds_fake_process(self):
        source = ("import os,json; "
                  "data=os.read(3,4096); q=json.loads(data.rstrip(bytes([0]))); "
                  "os.write(4,json.dumps({'id':q['id'],'result':{}}).encode()+bytes([0]))")
        pid, r, w = ax.launch([sys.executable, '-I', '-c', source])
        try:
            self.assertEqual(ax.Pipe(r, w, self.policy()).call('Target.getTargets'), {})
        finally:
            os.close(r); os.close(w)
            # Fake child exits immediately after its only reply.
            os.waitpid(pid, 0)
        with self.assertRaises(ax.Refused):
            ax.launch(['chromium', '--remote-debugging-port=9999'])

    def test_node_states_and_utf8_text(self):
        w, tree, snap, metrics = fixture(3)
        tree[1]['properties'] += [{'name': 'checked', 'value': {'value': 'true'}},
                                 {'name': 'focused', 'value': {'value': True}}]
        tree[2]['role']['value'] = 'StaticText'
        tree[2]['name']['value'] = '€' * 30000
        result = ax.map_snapshot(w, 7, tree, snap, metrics, ax.node_id(w['id'], 2))
        self.assertTrue(result['elements'][1]['states']['checked'])
        self.assertTrue(result['elements'][1]['focused'])
        self.assertGreater(result['text']['bytes'], 65000)
        self.assertLessEqual(result['text']['bytes'], 65536)
        self.assertEqual(result['text']['bytes'], len(result['text']['text'].encode()))


    def test_sensitive_values_are_never_accessed(self):
        class Unreadable(dict):
            def get(self, *args):
                raise AssertionError('sensitive value accessed')
        for attrs in [[2, 10], [11, 12]]:
            w, tree, snap, metrics = fixture()
            snap['documents'][0]['nodes']['attributes'][2] = attrs
            tree[1]['value'] = Unreadable()
            self.assertEqual(len(ax.map_snapshot(w, 7, tree, snap, metrics)['elements']), 1)
        w, tree, snap, metrics = fixture()
        snap['strings'] += ['one-time-code']
        snap['documents'][0]['nodes']['attributes'][2] = [11, len(snap['strings']) - 1]
        tree[1]['value'] = Unreadable()
        self.assertEqual(len(ax.map_snapshot(w, 7, tree, snap, metrics)['elements']), 1)

    def test_ambiguous_page_refused(self):
        from unittest.mock import Mock
        w, _, _, _ = fixture()
        pipe = Mock()
        page = {'targetId': 'one', 'type': 'page', 'title': 'Fixture', 'url': w['url']}
        pipe.call.return_value = {'targetInfos': [page, dict(page, targetId='two')]}
        with patch.object(ax, 'resolve_window', return_value=w), self.assertRaises(ax.Refused) as caught:
            ax.snapshot(pipe, {'op': 'ax', 'window_title_or_index': w['title'], 'frame_seq': 7})
        self.assertEqual(caught.exception.code, 6)
        # Only getTargets plus one read-only bounds query per candidate; no attach.
        self.assertEqual([c.args[0] for c in pipe.call.call_args_list],
                         ['Target.getTargets', 'Browser.getWindowForTarget', 'Browser.getWindowForTarget'])

    def test_same_title_disambiguated_by_window_bounds(self):
        from unittest.mock import Mock
        w, _, _, _ = fixture()
        page = {'targetId': 'one', 'type': 'page', 'title': 'Fixture', 'url': w['url']}
        other = dict(page, targetId='two')
        def call(method, params=None, session=None):
            if method == 'Target.getTargets':
                return {'targetInfos': [page, other]}
            if method == 'Browser.getWindowForTarget':
                if params['targetId'] == 'two':
                    return {'windowId': 2, 'bounds': {'left': w['x'], 'top': w['y'], 'width': w['w'], 'height': w['h']}}
                return {'windowId': 1, 'bounds': {'left': w['x'] + 640, 'top': w['y'], 'width': w['w'], 'height': w['h']}}
            raise ax.Refused(5)  # stop right after the target choice
        pipe = Mock(); pipe.call.side_effect = call; pipe.attached = {}
        pipe.policy = ax.Policy(json.loads((ROOT / 'docker/identity/ax-allowlist.json').read_text()))
        with patch.object(ax, 'resolve_window', return_value=w), self.assertRaises(ax.Refused):
            ax.snapshot(pipe, {'op': 'ax', 'window_title_or_index': w['title'], 'frame_seq': 7})
        self.assertEqual(pipe.policy.targets, {'two'})

    def test_window_bounds_query_limited_to_candidates(self):
        policy = ax.Policy(json.loads((ROOT / 'docker/identity/ax-allowlist.json').read_text()))
        policy.targets = {'one'}
        policy.check('Browser.getWindowForTarget', {'targetId': 'one'}, None)
        with self.assertRaises(ax.Refused):
            policy.check('Browser.getWindowForTarget', {'targetId': 'other'}, None)

    def test_allowlist_mutants(self):
        for forbidden in ['Runtime.evaluate', 'Network.getCookies']:
            al = json.loads((ROOT / 'docker/identity/ax-allowlist.json').read_text())
            al['session_methods'].append(forbidden)
            with self.assertRaises(ax.Refused):
                ax.Policy(al)


if __name__ == '__main__':
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    sys.exit(0 if result.wasSuccessful() else 5)
