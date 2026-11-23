"""Offline integrity oracles and deliberate broken-payload controls.

Run with python3 -I -B to keep the imported reference trees untouched.
No vendored implementation or live smoke script is executed here.
"""

import copy
import hashlib
import json
import re
import unittest
from pathlib import Path, PurePosixPath


VENDOR = Path(__file__).resolve().parents[1]
REPO = VENDOR.parents[2]
MANIFEST = VENDOR / "PINS_VENDOR.json"
RECEIPT = VENDOR / "H23_RECEIPT.json"
TREES = {"tasklight", "rewind", "broker"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_payloads():
    payloads = {}
    for name in sorted(TREES):
        root = VENDOR / name
        if root.is_symlink() or not root.is_dir():
            raise ValueError("missing tree or symlink")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlink in reference tree")
            if path.is_file():
                payloads[path.relative_to(VENDOR).as_posix()] = path.read_bytes()
    return payloads


def verify_payloads(manifest, payloads):
    """Require complete, unique, source-bound pins, then recompute every digest."""
    sources = {row["name"]: row for row in manifest["sources"]}
    if set(sources) != TREES or len(manifest["sources"]) != len(TREES):
        raise ValueError("incorrect source set")
    files = manifest["files"]
    paths = [row["path"] for row in files]
    if not files or len(paths) != len(set(paths)):
        raise ValueError("empty or duplicate pins")
    if set(paths) != set(payloads):
        raise ValueError("missing or unexpected payload")
    seen_trees = set()
    for row in files:
        path = PurePosixPath(row["path"])
        if path.is_absolute() or ".." in path.parts or path.as_posix() != row["path"]:
            raise ValueError("noncanonical path")
        source = sources.get(path.parts[0])
        if source is None or len(path.parts) < 2:
            raise ValueError("unowned path")
        seen_trees.add(path.parts[0])
        expected_source_path = str(PurePosixPath(source["source_path"]).joinpath(*path.parts[1:]))
        if (row["source_repo"] != source["source_repo"]
                or row["commit"] != source["commit"]
                or row["source_path"] != expected_source_path):
            raise ValueError("source provenance mismatch")
        if not re.fullmatch(r"[0-9a-f]{40}", row["commit"]):
            raise ValueError("unpinned commit")
        if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise ValueError("invalid digest")
        if row["source_mode"] not in ("100644", "100755"):
            raise ValueError("invalid source mode")
        expected_mode = "0555" if row["source_mode"] == "100755" else "0444"
        if row["read_only_mode"] != expected_mode:
            raise ValueError("invalid read-only mode")
        data = payloads[row["path"]]
        if len(data) != row["size_bytes"] or sha256(data) != row["sha256"]:
            raise ValueError("payload integrity mismatch")
    if seen_trees != TREES:
        raise ValueError("empty reference tree")


class VendorPinsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text())
        cls.payloads = read_payloads()

    def test_oracle_every_vendored_file_matches_sha256(self):
        verify_payloads(self.manifest, self.payloads)

    def test_oracle_requested_revisions(self):
        expected = {"tasklight": "729b451", "rewind": "4d33836", "broker": "4d33836"}
        for source in self.manifest["sources"]:
            self.assertEqual(source["requested_commit"], expected[source["name"]])
            self.assertTrue(source["commit"].startswith(expected[source["name"]]))

    def test_oracle_h2_exact_file_set_and_hashes(self):
        receipt = json.loads(RECEIPT.read_text())
        h2 = receipt["h2"]
        self.assertTrue(h2["commit"].startswith("9377116"))
        root = REPO / "memoryweb-builtin/standards/schemas"
        expected = {row["path"]: row["sha256"] for row in h2["files"]}
        actual = {}
        for path in root.rglob("*"):
            self.assertFalse(path.is_symlink())
            if path.is_file():
                actual[path.relative_to(root).as_posix()] = sha256(path.read_bytes())
        self.assertEqual(actual, expected)
        self.assertEqual(h2["before"]["rc"], 1)
        self.assertEqual(h2["after"]["rc"], 0)
        self.assertIn("build_schemas --check: ok", h2["after"]["stdout"])

    def test_oracle_frozen_files_unchanged(self):
        receipt = json.loads(RECEIPT.read_text())
        for name, digest in receipt["frozen_files"].items():
            self.assertEqual(sha256((REPO / name).read_bytes()), digest)

    def test_negative_missing_file_is_rejected(self):
        payloads = dict(self.payloads)
        payloads.pop(next(iter(payloads)))
        with self.assertRaisesRegex(ValueError, "missing or unexpected"):
            verify_payloads(self.manifest, payloads)

    def test_negative_unpinned_file_is_rejected(self):
        payloads = dict(self.payloads)
        payloads["broker/unpinned.py"] = b"untracked content\n"
        with self.assertRaisesRegex(ValueError, "missing or unexpected"):
            verify_payloads(self.manifest, payloads)

    def test_negative_empty_manifest_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["files"] = []
        with self.assertRaisesRegex(ValueError, "empty or duplicate"):
            verify_payloads(manifest, {})

    def test_negative_duplicate_pin_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["files"].append(copy.deepcopy(manifest["files"][0]))
        with self.assertRaisesRegex(ValueError, "empty or duplicate"):
            verify_payloads(manifest, self.payloads)

    def test_mutant_changed_bytes_same_length_are_rejected(self):
        payloads = dict(self.payloads)
        path = next(name for name, data in payloads.items() if data)
        data = payloads[path]
        payloads[path] = bytes([data[0] ^ 1]) + data[1:]
        with self.assertRaisesRegex(ValueError, "payload integrity"):
            verify_payloads(self.manifest, payloads)

    def test_mutant_wrong_digest_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        row = manifest["files"][0]
        row["sha256"] = ("0" if row["sha256"][0] != "0" else "1") + row["sha256"][1:]
        with self.assertRaisesRegex(ValueError, "payload integrity"):
            verify_payloads(manifest, self.payloads)

    def test_mutant_wrong_source_commit_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["files"][0]["commit"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "source provenance"):
            verify_payloads(manifest, self.payloads)

    def test_negative_path_traversal_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        payloads = dict(self.payloads)
        row = manifest["files"][0]
        data = payloads.pop(row["path"])
        row["path"] = "broker/../escape.py"
        payloads[row["path"]] = data
        with self.assertRaisesRegex(ValueError, "noncanonical path"):
            verify_payloads(manifest, payloads)


if __name__ == "__main__":
    unittest.main()
