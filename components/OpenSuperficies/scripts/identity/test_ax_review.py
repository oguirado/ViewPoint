#!/usr/bin/env python3
"""Offline review regressions. Exit 0/5; no browser or display."""
import copy
import json
import socket
import sys
import unittest
from unittest.mock import Mock, patch
from test_ax_shim import ax, fixture, ROOT


class ReviewTests(unittest.TestCase):
    def policy(self):
        return ax.Policy(json.loads((ROOT / 'docker/identity/ax-allowlist.json').read_text()))

    def test_url_projection(self):
        w, tree, snap, metrics = fixture()
        w['url'] = 'https://user:pass@[::1]:8443/x?session=abc#access_token=xyz'
        w['title'] = 'Page ' + w['url']
        result = ax.map_snapshot(w, 1, tree, snap, metrics)
        self.assertEqual(result['window']['url'], 'https://[::1]:8443/x')
        self.assertEqual(result['window']['title'], 'Page https://[::1]:8443/x')
        self.assertNotIn('session=', json.dumps(result))
        self.assertNotIn('access_token=', json.dumps(result))
        self.assertNotIn('user:pass', json.dumps(result))

    def test_sensitive_autocomplete_never_read(self):
        class Unreadable(dict):
            def get(self, *args):
                raise AssertionError('sensitive value accessed')
        for token in ('current-password', 'new-password', 'cc-anything', 'one-time-code'):
            with self.subTest(token=token):
                w, tree, snap, metrics = fixture()
                snap['strings'].append('section-login shipping ' + token.upper())
                snap['documents'][0]['nodes']['attributes'][2] = [11, len(snap['strings']) - 1]
                tree[1]['value'] = Unreadable()
                result = ax.map_snapshot(w, 1, tree, snap, metrics)
                self.assertEqual(len(result['elements']), 1)
                with self.assertRaises(ax.Refused) as caught:
                    ax.map_snapshot(w, 1, tree, snap, metrics, ax.node_id(w['id'], 2))
                self.assertEqual(caught.exception.code, 6)

    def test_policy_bindings(self):
        cases = [('Target.attachToTarget', {'targetId': 'page'}, None),
                 ('Target.attachToTarget', {'targetId': 'page', 'flatten': False}, None),
                 ('Target.attachToTarget', {'targetId': 'page', 'flatten': 1}, None),
                 ('Accessibility.getFullAXTree', {}, None),
                 ('Target.getTargets', {}, 'session'),
                 ('target.getTargets', {}, None),
                 ('Accessibility.getfullAXTree', {}, 'session')]
        for method, params, session in cases:
            with self.subTest(method=method, params=params):
                p = self.policy(); p.targets = {'page'}; p.sessions = {'session'}
                with self.assertRaises(ax.Refused):
                    p.check(method, params, session)
                self.assertTrue(p.failed)

    def test_duplicate_json(self):
        with self.assertRaises(ax.Refused) as caught:
            ax.parse(b'{"op":"ax","op":"ax"}')
        self.assertEqual(caught.exception.code, 5)

    def test_main_document_only(self):
        for origin in ('https://fixture.invalid', 'https://other.invalid'):
            w, tree, snap, metrics = fixture()
            sub = copy.deepcopy(snap['documents'][0])
            sub['frameId'] = origin
            sub['nodes']['backendNodeId'] = [101, 102, 103]
            snap['documents'].append(sub)
            tree += [{'backendDOMNodeId': 103, 'role': {'value': 'textbox'},
                      'name': {'value': 'IFRAME SECRET'}, 'value': {'value': 'secret'}}]
            result = ax.map_snapshot(w, 1, tree, snap, metrics)
            self.assertEqual(result['skipped_frames'], 1)
            self.assertEqual(len(result['elements']), 2)
            self.assertNotIn('IFRAME SECRET', json.dumps(result))
            self.assertNotIn('IFRAME SECRET', ax.map_snapshot(
                w, 1, tree, snap, metrics, ax.node_id(w['id'], 2))['text']['text'])

    def test_uncaptured_iframe_count(self):
        w, tree, snap, metrics = fixture()
        snap['strings'].append('IFRAME')
        snap['documents'][0]['nodes']['nodeName'][2] = len(snap['strings']) - 1
        self.assertEqual(ax.map_snapshot(w, 1, tree, snap, metrics)['skipped_frames'], 1)
        snap['documents'].append(copy.deepcopy(snap['documents'][0]))
        snap['documents'][0]['nodes']['contentDocumentIndex'] = {'index': [2], 'value': [1]}
        self.assertEqual(ax.map_snapshot(w, 1, tree, snap, metrics)['skipped_frames'], 1)

    def test_depth_refusal_is_lifecycle(self):
        w, tree, snap, metrics = fixture(260)
        snap['documents'][0]['nodes']['parentIndex'] = [-1] + list(range(261))
        with self.assertRaises(ax.Refused) as caught:
            ax.map_snapshot(w, 1, tree, snap, metrics)
        self.assertEqual(caught.exception.code, 6)

    def test_codepoint_length(self):
        w, tree, snap, metrics = fixture()
        tree[1]['value']['value'] = 'é😀e\u0301'
        result = ax.map_snapshot(w, 1, tree, snap, metrics)
        self.assertEqual(result['elements'][1]['value_len'], 4)

    def test_call_deadline(self):
        a, b = socket.socketpair()
        try:
            pipe = ax.Pipe(a.fileno(), a.fileno(), self.policy())
            with patch.object(ax.select, 'select', side_effect=[([], [a.fileno()], []), ([], [], [])]) as wait:
                with self.assertRaises(ax.Refused):
                    pipe.call('Target.getTargets')
            for call in wait.call_args_list:
                self.assertGreater(call.args[3], 0)
                self.assertLessEqual(call.args[3], 0.5)
        finally:
            a.close(); b.close()

    def test_invalid_replies_poison(self):
        for frames in ([{'id': 2, 'result': {}}],
                       [{'id': 1, 'result': {}}, {'id': 1, 'result': {}}],
                       [{'id': 0, 'sessionId': 'wrong', 'result': {}}]):
            a, b = socket.socketpair()
            try:
                p = self.policy(); pipe = ax.Pipe(a.fileno(), a.fileno(), p)
                pipe.expired = {0: None}
                b.sendall(b''.join(json.dumps(row).encode() + b'\0' for row in frames))
                with self.assertRaises(ax.Refused) as caught:
                    pipe.call('Target.getTargets')
                self.assertEqual(caught.exception.code, 5)
                self.assertTrue(p.failed)
            finally:
                a.close(); b.close()
        a, b = socket.socketpair()
        try:
            # Shut down only the peer's write half: request transmission works,
            # response EOF must poison rather than masquerade as a timeout.
            b.shutdown(socket.SHUT_WR)
            p = self.policy(); pipe = ax.Pipe(a.fileno(), a.fileno(), p)
            with self.assertRaises(ax.Refused) as caught:
                pipe.call('Target.getTargets')
            self.assertEqual(caught.exception.code, 5)
            self.assertTrue(p.failed)
        finally:
            a.close(); b.close()

    def test_pipe_events_and_stale_replies(self):
        a, b = socket.socketpair()
        try:
            p = self.policy(); pipe = ax.Pipe(a.fileno(), a.fileno(), p)
            pipe.seq = 1
            pipe.expired = {1: None}
            frames = [{'method': 'Network.secret', 'params': {'secret': 'hidden'}},
                      {'id': 1, 'result': {'stale': True}},
                      {'id': 2, 'result': {'fresh': True}},
                      {'method': 'Page.event'}]
            b.sendall(b''.join(json.dumps(row).encode() + b'\0' for row in frames))
            self.assertEqual(pipe.call('Target.getTargets'), {'fresh': True})
            self.assertFalse(p.failed)
        finally:
            a.close(); b.close()

    def test_pipe_timeout_recovery(self):
        a, b = socket.socketpair()
        try:
            p = self.policy(); pipe = ax.Pipe(a.fileno(), a.fileno(), p)
            with patch.object(ax.select, 'select', side_effect=[([], [a.fileno()], []), ([], [], [])]):
                with self.assertRaises(ax.Refused) as caught:
                    pipe.call('Target.getTargets')
            self.assertEqual(caught.exception.code, 4)
            self.assertFalse(p.failed)
            b.sendall(b'{"id":1,"result":{}}\0{"id":2,"result":{"ok":true}}\0')
            self.assertEqual(pipe.call('Target.getTargets'), {'ok': True})
        finally:
            a.close(); b.close()

    def test_unexpected_exception_replies_and_poisons(self):
        pipe = Mock(); pipe.policy.failed = False
        with patch.object(ax, 'receive', return_value={}), patch.object(ax, 'snapshot', side_effect=AttributeError):
            result = ax.respond(pipe, Mock())
        self.assertEqual(result, {'error': 'malformed', 'code': 5})
        self.assertTrue(pipe.policy.failed)

    def test_only_snapshot_methods_allowed(self):
        p = self.policy()
        self.assertEqual(ax.SESSION_METHODS, {'Accessibility.enable', 'Accessibility.getFullAXTree',
                                             'DOMSnapshot.captureSnapshot', 'Page.getLayoutMetrics'})
        for method in ('DOM.enable', 'DOM.getDocument', 'DOM.getBoxModel', 'DOM.describeNode', 'Page.getFrameTree'):
            p = self.policy(); p.sessions = {'session'}
            with self.assertRaises(ax.Refused):
                p.check(method, {}, 'session')


if __name__ == '__main__':
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(ReviewTests))
    sys.exit(0 if result.wasSuccessful() else 5)
