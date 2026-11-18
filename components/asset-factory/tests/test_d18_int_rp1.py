"""Regression controls for legacy registry publication and binary inventory."""
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from tools.docs_ratio import measure_docs_ratio
from tools.registry import Registry, store
import test_d18_reg_store as registry_fixture


class BinaryInventory(unittest.TestCase):
    def test_glb_framing_and_line_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "script.py").write_text("pass\n", encoding="ascii")
            path = root / "asset.glb"
            raw = Path(__file__).with_name("test_d18_uv_sphere_flat.glb").read_bytes()
            path.write_bytes(raw)
            result = measure_docs_ratio(root)
            self.assertEqual(result["rc"], 0)
            asset, script = result["files"]
            self.assertEqual((asset["path"], asset["lines"], asset["class"]), (path.name, 0, "other"))
            self.assertEqual(result["code_schema_lines"], script["lines"])
            last = 20 + struct.unpack_from("<I", raw, 12)[0]
            for name, invalid in {
                "text": b"glTF text", "magic": b"fake" + raw[4:],
                "version": raw[:4] + struct.pack("<I", 1) + raw[8:],
                "length": raw[:-1], "first_chunk": raw[:16] + b"junk" + raw[20:],
                "truncated_header": raw[:8] + struct.pack("<I", last + 4) + raw[12:last+4],
                "chunk_overrun": raw[:last] + struct.pack("<I", len(raw)) + raw[last+4:],
            }.items():
                with self.subTest(name=name):
                    path.write_bytes(invalid)
                    self.assertEqual(measure_docs_ratio(root)["rc"], 2)


class RegistryUpgrade(unittest.TestCase):
    def setUp(self):
        fixture = registry_fixture.RegistryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        current = root / "CURRENT"
        watermark = root / "HIGH_WATER"
        legacy = {"schema": "af.registry-snapshot/v1", "previous": None, "namespace": "studio", "names": {}}
        encoded = registry_fixture.encode(legacy)
        old = registry_fixture.pin(encoded)
        node = root / "snapshots" / old[7:]
        node.mkdir()
        (node / "catalog.json").write_bytes(encoded)
        current.chmod(0o600)
        current.write_text(old + "\n", encoding="ascii")
        watermark.unlink()

        self.fixture, self.root, self.old = fixture, root, old
        self.legacy_file, self.legacy_bytes = node / "catalog.json", encoded

    def test_reader_overlapping_first_upgrade(self):
        reader = Registry(self.root, namespace="studio")
        first_read = reader._high_water
        selected = []
        def pause_after_watermark(root_fd):
            mark = first_read(root_fd)
            if not selected:
                self.assertEqual(mark, (None, 0))
                selected.append(self.fixture.put(expected_snapshot=self.old))
            return mark

        with patch.object(reader, "_high_water", side_effect=pause_after_watermark):
            observed = reader.current_snapshot()
        self.assertEqual(observed, selected[0])
        self.assertNotEqual(observed, self.old)

    def test_crash_between_upgrade_pointer_writes(self):
        fixture, root, old = self.fixture, self.root, self.old
        current, watermark = root / "CURRENT", root / "HIGH_WATER"
        select_current = store._disk.select_current
        class SimulatedCrash(Exception):
            pass

        def before_current(root_fd, staging, successor):
            self.assertEqual(current.read_text(), old + "\n")
            self.assertEqual(watermark.read_text(), old + " 0\n")
            raise SimulatedCrash

        with patch.object(store._disk, "select_current", side_effect=before_current):
            with self.assertRaises(SimulatedCrash):
                fixture.put(expected_snapshot=old)
        self.assertEqual(Registry(root, namespace="studio").current_snapshot(), old)

        def after_current(root_fd, staging, successor):
            self.assertEqual(watermark.read_text(), old + " 0\n")
            select_current(root_fd, staging, successor)
            raise SimulatedCrash
        with patch.object(store._disk, "select_current", side_effect=after_current):
            with self.assertRaises(SimulatedCrash):
                fixture.put(expected_snapshot=old)
        selected = current.read_text().strip()
        self.assertNotEqual(selected, old)
        catalog = json.loads((root / "snapshots" / selected[7:] / "catalog.json").read_bytes())
        self.assertEqual((catalog["schema"], catalog["previous"]), ("af.registry-snapshot/v2", old))
        reopened = Registry(root, namespace="studio")
        self.assertEqual(reopened.current_snapshot(), selected)
        self.assertEqual(reopened.initialize(), selected)
        self.assertEqual(watermark.read_text(), selected + " 1\n")
        self.assertEqual(reopened.put("hero", fixture.asset, blobs={}, expected_snapshot=selected), selected)
        self.assertEqual(self.legacy_file.read_bytes(), self.legacy_bytes)
        watermark.unlink()
        with self.assertRaisesRegex(store.RegistryError, "missing_high_water"):
            reopened.current_snapshot()
