"""Fake-bus regressions for the desktop STATE producer; no D-Bus required."""
import unittest

from wender.perceive import atspi_tree, ax
from wender.state.schema import validate_state


class TestAtspiTree(unittest.TestCase):
    def setUp(self):
        # AT-SPI StateType bit positions: BUSY=3, CHECKED=4, EXPANDED=10,
        # PRESSED=19, SELECTED=22, SENSITIVE=23.
        self.tree = {
            '/root': ('window', ' GTK   window ', 1 << 23, ['/bar', '/filler', '/check']),
            '/bar': ('tool bar', 'Tools', 1 << 23, ['/b1', '/b2']),
            '/b1': ('push button', 'Save', 1 << 23, []),
            '/b2': ('push button', 'Save', 1 << 23, []),
            '/filler': ('filler', '', 1 << 23, ['/entry']),
            '/entry': ('entry', 'Search', 1 << 23, []),
            '/check': ('check box', 'Option', 1 << 4, []),
        }
        self.calls = []

    def call(self, address, bus, path, method, *args, timeout=10):
        self.calls.append((address, bus, path, method, args, timeout))
        role, name, bits, children = self.tree[path]
        if method.endswith('.GetChildren'):
            if children is None:
                return None
            return '(' + repr([(':1.5', p) for p in children]) + ',)'
        if method.endswith('.GetRoleName'):
            return '(' + repr(role) + ',)'
        if method.endswith('.GetState'):
            return '([uint32 %d, 0],)' % bits
        self.assertEqual((method, args), ('org.freedesktop.DBus.Properties.Get',
                                         ('org.a11y.atspi.Accessible', 'Name')))
        return '(<' + repr(name) + '>,)'

    def walk(self, **kwargs):
        return atspi_tree.walk('fake-address', ':1.5', '/root', call=self.call, **kwargs)

    def test_walk_order_ids_and_state(self):
        result = self.walk(timeout=7)
        nodes = result['nodes']
        self.assertTrue(all(c[5] == 7 for c in self.calls))
        self.assertEqual([n['role'] for n in nodes],
                         ['application', 'toolbar', 'button', 'button', 'textbox', 'checkbox'])
        self.assertEqual(result['raw_count'], 7)
        self.assertFalse(result['truncated'])
        self.assertEqual(nodes[0]['name'], 'GTK window')
        self.assertEqual([n['ordinal'] for n in nodes[2:4]], [0, 1])
        self.assertEqual([n['cardinality'] for n in nodes[2:4]], [2, 2])
        self.assertEqual(nodes[4]['depth'], 1)
        self.assertEqual(nodes[4]['ancestors'], ('application',))
        self.assertEqual(dict(nodes[-1]['attrs']), {'checked': 'true', 'disabled': 'true'})
        self.assertEqual([n['id'] for n in nodes], [n['id'] for n in self.walk()['nodes']])
        self.assertTrue(all(n['id'] == ax.stable_id(n) for n in nodes))
        state = atspi_tree.atspi_state(result, ' GTK   window ')
        self.assertEqual(validate_state(state), [])
        self.assertEqual(state['source'], {'kind': 'atspi', 'url': '', 'title': 'GTK window'})
        self.assertEqual(state['ax_sha256'], ax.snapshot_sha(nodes))
        self.assertEqual(state['ax_stable_sha256'], ax.snapshot_sha(nodes, with_state=False))
        self.assertIsNone(state['viewport'])
        self.assertTrue(all(n['box'] is None for n in state['nodes']))
        self.assertFalse(state['nodes'][-1]['actionable'])
        self.assertEqual(state['nodes'][4]['parent'], nodes[0]['id'])

    def test_node_and_depth_limits(self):
        result = self.walk(max_nodes=3)
        self.assertEqual(result['raw_count'], 3)
        self.assertTrue(result['truncated'])
        self.assertEqual([n['role'] for n in result['nodes']], ['application', 'toolbar', 'button'])
        result = self.walk(max_depth=1)
        self.assertEqual(result['raw_count'], 4)
        self.assertEqual([n['role'] for n in result['nodes']], ['application', 'toolbar', 'checkbox'])
        self.assertTrue(result['truncated'])
        self.assertEqual(self.walk(max_nodes=0), {'nodes': [], 'raw_count': 0, 'truncated': True})
        self.tree = {'/root': ('window', '', 1 << 23, [])}
        self.assertFalse(self.walk(max_nodes=1, max_depth=0)['truncated'])

    def test_cycles_and_missing_children(self):
        self.tree['/b1'] = ('push button', 'Save', 1 << 23, ['/root', '/b1'])
        self.tree['/b2'] = ('push button', 'Save', 1 << 23, None)
        result = self.walk()
        self.assertEqual(result['raw_count'], 7)
        self.assertFalse(result['truncated'])

    def test_states_and_stable_hash(self):
        before = self.walk()
        self.tree['/b1'] = ('toggle button', 'Save', (1 << 23) | (1 << 19) | (1 << 3), [])
        self.tree['/entry'] = ('entry', "it's \\ fine | now", (1 << 23) | (1 << 10) | (1 << 22), [])
        nodes = self.walk()['nodes']
        self.assertEqual(dict(nodes[2]['attrs']), {'busy': 'true', 'pressed': 'true'})
        self.assertEqual(dict(nodes[4]['attrs']), {'expanded': 'true', 'selected': 'true'})
        self.assertEqual(nodes[4]['name'], ax.normalize_name("it's \\ fine | now"))
        self.tree['/check'] = ('check box', 'Option', 1 << 23, [])
        after = self.walk()
        self.assertEqual(ax.snapshot_sha(nodes, with_state=False),
                         ax.snapshot_sha(after['nodes'], with_state=False))
        self.assertEqual(before['nodes'][-1]['id'], after['nodes'][-1]['id'])
        self.assertNotEqual(ax.snapshot_sha(nodes), ax.snapshot_sha(after['nodes']))

    def test_identity_includes_bus_and_unknown_states_stay_unknown(self):
        def call(address, bus, path, method, *args, timeout=10):
            if method.endswith('.GetChildren'):
                return "([(':1.6', '/root')],)" if bus == ':1.5' else None
            if method.endswith('.GetRoleName'):
                return "('push button',)"
            if method.endswith('.GetState'):
                return None
            return "(<'Same'>,)"
        result = atspi_tree.walk('fake', ':1.5', '/root', call=call)
        self.assertEqual(result['raw_count'], 2)
        self.assertEqual([n['ordinal'] for n in result['nodes']], [0, 1])
        self.assertTrue(all(n['attrs'] == () for n in result['nodes']))
        self.assertFalse(result['truncated'])

    def test_limits_refuse_invalid_values(self):
        for key in ('max_nodes', 'max_depth'):
            for value in (-1, True, 1.5):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.walk(**{key: value})

    def test_missing_replies_and_mapping(self):
        result = atspi_tree.walk('fake', ':1.5', '/root', call=lambda *a, **k: None)
        self.assertEqual(result, {'nodes': [], 'raw_count': 1, 'truncated': False})
        self.assertLessEqual(set(atspi_tree.ATSPI_ROLE_MAP.values()), ax.CHASSIS_ROLES)
        required = {'radio button': 'radio', 'text': 'textbox', 'combo box': 'combobox',
                    'list': 'listbox', 'list item': 'option', 'menu bar': 'menubar',
                    'menu': 'menu', 'menu item': 'menuitem', 'page tab list': 'tablist',
                    'page tab': 'tab', 'slider': 'slider', 'spin button': 'spinbutton',
                    'dialog': 'dialog', 'heading': 'heading', 'link': 'link', 'frame': 'application'}
        for role, expected in required.items():
            self.assertEqual(atspi_tree.ATSPI_ROLE_MAP[role], expected)


if __name__ == '__main__':
    unittest.main()
