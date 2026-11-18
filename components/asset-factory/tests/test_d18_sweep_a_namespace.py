"""Exercise the Blender witness import with the TOOLS-NS namespace probe."""

import unittest

import test_n18_tools_namespace as namespace


class BlenderImportNamespaceTests(unittest.TestCase):
    def test_blender_witness_import_preserves_factory_namespace(self):
        namespace.ToolsNamespaceTests.check_in_fresh_interpreter(
            self, namespace.PROBE, "test_f2u2_blender")
