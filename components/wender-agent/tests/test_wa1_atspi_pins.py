"""Adversarial pin tests for wender.perceive.atspi_tree."""
import os
import unittest

try:
    from wender.perceive import atspi_tree, ax
    from wender.state import schema
except ImportError as err:
    if os.environ.get("WA1_PIN_ALLOW_SKIP") == "1":
        atspi_tree = None
        ax = None
        schema = None
        skip_reason = f"Module import failed for wa1-spark-atspi-state: {err}"
    else:
        raise
else:
    skip_reason = None

_SENSITIVE = 1 << 24


@unittest.skipIf(atspi_tree is None, skip_reason)
class TestWa1AtspiPins(unittest.TestCase):
    def test_malformed_gdbus_output(self):
        """Kills mutant: silent crash, unhandled exception, or corrupt node attrs when gdbus produces malformed state or text strings."""
        responses = {
            '/root/GetRoleName': 'invalid-unquoted-string',
            '/root/Name': '(<"Normal Name">,)',
            '/root/GetState': '([uint32 invalid_bit],)',
            '/root/GetChildren': "([(':1.1', '/child1')],)",
            '/child1/GetRoleName': "('push button',)",
            '/child1/Name': "(<'Child 1'>,)",
            '/child1/GetState': '([uint32 0x100000000],)',  # exceeds uint32 max, bitmask parser must handle safely
            '/child1/GetChildren': '()',
        }

        def mock_call(address, bus, path, method, *args, timeout=10):
            if 'GetRoleName' in method:
                m = 'GetRoleName'
            elif 'GetChildren' in method:
                m = 'GetChildren'
            elif 'GetState' in method:
                m = 'GetState'
            else:
                m = 'Name'
            key = path + '/' + m
            return responses.get(key)

        res = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call)
        self.assertEqual(len(res['nodes']), 1)
        node = res['nodes'][0]
        self.assertEqual(node['role'], 'button')
        self.assertEqual(node['name'], 'Child 1')
        self.assertEqual(node['attrs'], ())

    def test_child_listing_itself_and_parent_cycles(self):
        """Kills mutant: infinite traversal loop or unhandled cycle when a node lists itself or its parent as a child."""
        tree = {
            '/root': ('window', 'Root Window', _SENSITIVE, [(':1.1', '/root'), (':1.1', '/child')]),
            '/child': ('push button', 'Click Me', _SENSITIVE, [(':1.1', '/root'), (':1.1', '/child')]),
        }

        def mock_call(address, bus, path, method, *args, timeout=10):
            role, name, bits, children = tree[path]
            if 'GetRoleName' in method:
                return f"('{role}',)"
            if 'GetState' in method:
                return f"([uint32 {bits}],)"
            if 'GetChildren' in method:
                return "(" + repr(children) + ",)"
            return f"(<'{name}'>,)"

        res = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call)
        self.assertEqual(res['raw_count'], 2)
        self.assertFalse(res['truncated'])
        self.assertEqual([n['role'] for n in res['nodes']], ['application', 'button'])

    def test_depth_max_depth_plus_one_truncation(self):
        """Kills mutant: off-by-one in depth limit checking that allows nodes at max_depth + 1 or misreports truncation status."""
        max_d = 5
        # Build chain of max_d + 2 nodes (depths 0 through max_d + 1)
        nodes_map = {}
        for i in range(max_d + 2):
            path = f'/node_{i}'
            next_path = f'/node_{i+1}' if i < max_d + 1 else None
            children = [(':1.1', next_path)] if next_path else []
            nodes_map[path] = ('push button', f'Btn {i}', _SENSITIVE, children)

        def mock_call(address, bus, path, method, *args, timeout=10):
            role, name, bits, children = nodes_map[path]
            if 'GetRoleName' in method:
                return f"('{role}',)"
            if 'GetState' in method:
                return f"([uint32 {bits}],)"
            if 'GetChildren' in method:
                return "(" + repr(children) + ",)"
            return f"(<'{name}'>,)"

        res = atspi_tree.walk('fake_addr', ':1.1', '/node_0', call=mock_call, max_depth=max_d)
        self.assertTrue(res['truncated'])
        self.assertEqual(len(res['nodes']), max_d + 1)
        self.assertEqual([n['depth'] for n in res['nodes']], list(range(max_d + 1)))

    def test_name_normalization_pipe_backslash_200bytes(self):
        """Kills mutant: failure to escape pipes and backslashes or failure to cut names to 64 UTF-8 bytes on character boundaries."""
        long_text = "a\\" + "b|" * 20 + "c" * 200 + " \t\n text"

        def mock_call(address, bus, path, method, *args, timeout=10):
            if 'GetRoleName' in method:
                return "('push button',)"
            if 'GetState' in method:
                return f"([uint32 {_SENSITIVE}],)"
            if 'GetChildren' in method:
                return "()"
            return f"(<{repr(long_text)}>,)"

        res = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call)
        node_name = res['nodes'][0]['name']
        self.assertEqual(node_name, ax.normalize_name(long_text))
        self.assertIn('\\\\', node_name)
        self.assertIn('\\|', node_name)

    def test_unmapped_role_between_mapped_roles_depth_counting(self):
        """Kills mutant: depth counting all traversed nodes including unmapped roles instead of counting emitted ancestors only."""
        tree = {
            '/root': ('window', 'Win', _SENSITIVE, [(':1.1', '/filler')]),
            '/filler': ('filler', 'Unmapped Filler', _SENSITIVE, [(':1.1', '/btn')]),
            '/btn': ('push button', 'Action', _SENSITIVE, []),
        }

        def mock_call(address, bus, path, method, *args, timeout=10):
            role, name, bits, children = tree[path]
            if 'GetRoleName' in method:
                return f"('{role}',)"
            if 'GetState' in method:
                return f"([uint32 {bits}],)"
            if 'GetChildren' in method:
                return "(" + repr(children) + ",)"
            return f"(<'{name}'>,)"

        res = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call)
        emitted = res['nodes']
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[0]['role'], 'application')
        self.assertEqual(emitted[0]['depth'], 0)
        self.assertEqual(emitted[0]['ancestors'], ())

        self.assertEqual(emitted[1]['role'], 'button')
        # Emitted depth must be 1 (ancestors only includes 'application', skipping 'filler')
        self.assertEqual(emitted[1]['depth'], 1)
        self.assertEqual(emitted[1]['ancestors'], ('application',))

    def test_insensitive_widget_becomes_disabled_and_not_actionable(self):
        """An insensitive widget is disabled and excluded from actionable STATE nodes."""
        def mock_call(address, bus, path, method, *args, timeout=10):
            if 'GetRoleName' in method:
                return "('push button',)"
            if 'GetState' in method:
                return "([uint32 0],)"
            if 'GetChildren' in method:
                return "()"
            return "(<'Disabled Button'>,)"

        res = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call)
        self.assertEqual(dict(res['nodes'][0]['attrs']), {'disabled': 'true'})

        state = atspi_tree.atspi_state(res, 'Title')
        self.assertFalse(state['nodes'][0]['actionable'])
        self.assertEqual(state['actionable'], [])

    def test_ids_independent_of_bus_names(self):
        """Kills mutant: stable node id depending on D-Bus connection address or bus string instead of fingerprint and ordinal."""
        tree = {
            '/root': ('push button', 'Submit', _SENSITIVE, []),
        }

        def mock_call(address, bus, path, method, *args, timeout=10):
            role, name, bits, children = tree[path]
            if 'GetRoleName' in method:
                return f"('{role}',)"
            if 'GetState' in method:
                return f"([uint32 {bits}],)"
            if 'GetChildren' in method:
                return "(" + repr(children) + ",)"
            return f"(<'{name}'>,)"

        res1 = atspi_tree.walk('fake_addr1', ':1.100', '/root', call=mock_call)
        res2 = atspi_tree.walk('fake_addr2', ':1.200', '/root', call=mock_call)

        self.assertEqual(res1['nodes'][0]['id'], res2['nodes'][0]['id'])

    def test_state_hash_differs_stable_hash_equal_when_checked_flips(self):
        """Kills mutant: ax_stable_sha256 changing when mutable state attrs flip or ax_sha256 failing to change."""
        def mock_call_checked(address, bus, path, method, *args, timeout=10):
            if 'GetRoleName' in method:
                return "('check box',)"
            if 'GetState' in method:
                return f"([uint32 {_SENSITIVE | (1 << 4)}],)"  # sensitive + checked
            if 'GetChildren' in method:
                return "()"
            return "(<'Check'>,)"

        def mock_call_unchecked(address, bus, path, method, *args, timeout=10):
            if 'GetRoleName' in method:
                return "('check box',)"
            if 'GetState' in method:
                return f"([uint32 {_SENSITIVE}],)"
            if 'GetChildren' in method:
                return "()"
            return "(<'Check'>,)"

        res1 = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call_checked)
        res2 = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call_unchecked)

        st1 = atspi_tree.atspi_state(res1, 'Title')
        st2 = atspi_tree.atspi_state(res2, 'Title')

        self.assertNotEqual(st1['ax_sha256'], st2['ax_sha256'])
        self.assertEqual(st1['ax_stable_sha256'], st2['ax_stable_sha256'])

    def test_atspi_state_schema_canonical_json_no_floats(self):
        """Kills mutant: producing invalid JSON STATE schema structure or introducing floats into the output."""
        def mock_call(address, bus, path, method, *args, timeout=10):
            if 'GetRoleName' in method:
                return "('push button',)"
            if 'GetState' in method:
                return f"([uint32 {_SENSITIVE}],)"
            if 'GetChildren' in method:
                return "()"
            return "(<'OK'>,)"

        res = atspi_tree.walk('fake_addr', ':1.1', '/root', call=mock_call)
        st = atspi_tree.atspi_state(res, 'Window Title')

        violations = schema.validate_state(st)
        self.assertEqual(violations, [])

        # Ensure schema.dumps succeeds without throwing StateError due to float values
        dumped = schema.dumps(st)
        self.assertIsInstance(dumped, str)


if __name__ == '__main__':
    unittest.main()
