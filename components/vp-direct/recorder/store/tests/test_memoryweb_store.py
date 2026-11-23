from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(HERE.parent))
import memoryweb_store as store


MW_TOOLS = Path(os.environ.get("MEMORYWEB_TOOLS", REPO.parent.parent / "memoryweb/tools"))
MW = runpy.run_path(str(MW_TOOLS / "mwlib.py"))
SCHEMAS = REPO / "memoryweb-builtin/standards/schemas"
WORKFLOW = REPO / "memoryweb-builtin/sites/sample-chat/workflows/ask-fixture.json"
SITE = "sample-chat"
WORKFLOW_ID = "ask-fixture"
RECEIPT_DOMAIN = b"mw.publication_receipt.v1\x00"


def digest(raw):
    return sha256(raw).hexdigest()


def canon(value):
    return MW["canon_bytes"](value)


def receipt(workflow_sha256, seq=1, previous=None):
    return {
        "schema": "mw.publication_receipt.v1",
        "seq": seq,
        "site_id": SITE,
        "workflow_id": WORKFLOW_ID,
        "workflow_sha256": workflow_sha256,
        "rc": 0,
        "verdict_code": "VERIFIED",
        "verification_level": "T1",
        "reviewed_by_operator": True,
        "public_evidence_refs": [],
        "prev_receipt_sha256": previous,
        "mode": "replay",
        "evaluation_profile_sha256": None,
        "cohort_task_id": None,
    }


class MemoryWebStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = store.MemoryWebStore(self.root)
        self.site = self.root / "sites" / SITE
        self.journal = self.site / "runs" / "receipts.jsonl"
        self.workflow = json.loads(WORKFLOW.read_bytes())
        self.first = canon(self.workflow)
        self.second = canon(dict(self.workflow, status="retired"))

    def workflow_path(self, raw):
        return self.site / "revisions" / digest(raw) / (WORKFLOW_ID + ".json")

    def assert_refused(self, code, operation, *args, **kwargs):
        with self.assertRaises(store.StoreError) as raised:
            operation(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def assert_upstream_canonical(self):
        unicode_value = {"z": ["é", "雪", "\n"], "a": {"item": True}}
        self.assertEqual(store.canonical_bytes(unicode_value), canon(unicode_value))
        self.assertTrue(store.canonical_bytes(unicode_value).endswith(b"\n"))

    def test_canonical_bytes_match_mwlib_and_round_trip(self):
        self.assert_upstream_canonical()
        answer = self.db.save_workflow(self.first)
        self.assertEqual(answer, {"workflow_sha256": digest(self.first), "trust": "signature_unverified"})
        self.assertEqual(self.workflow_path(self.first).read_bytes(), self.first)
        loaded = self.db.load_workflow(SITE, WORKFLOW_ID, answer["workflow_sha256"])
        self.assertEqual(loaded["workflow"], self.workflow)
        self.assertEqual(loaded["trust"], "signature_unverified")

    def test_missing_lf_is_noncanonical_on_save_and_load(self):
        self.assert_refused("JSON_NONCANONICAL", self.db.save_workflow, self.first[:-1])
        self.assertFalse(self.site.exists())
        self.db.save_workflow(self.first)
        self.workflow_path(self.first).write_bytes(self.first[:-1])
        self.assert_refused("JSON_NONCANONICAL", self.db.load_workflow, SITE, WORKFLOW_ID, digest(self.first))

    def assert_tampered_revision_refused(self):
        self.assert_refused("DIGEST_MISMATCH", self.db.load_workflow, SITE, WORKFLOW_ID, digest(self.first))

    def test_digest_checked_after_every_successful_load(self):
        self.db.save_workflow(self.first)
        pin = self.db.append_publication_receipt(SITE, canon(receipt(digest(self.first))))
        self.db.load_workflow(SITE, WORKFLOW_ID, digest(self.first))
        self.db.load_pinned_workflow(SITE, pin["publication_receipt_sha256"])
        self.workflow_path(self.first).write_bytes(self.second)
        self.assert_tampered_revision_refused()
        self.assert_refused("DIGEST_MISMATCH", self.db.load_pinned_workflow, SITE, pin["publication_receipt_sha256"])

    def publish_two(self):
        self.db.save_workflow(self.first)
        first = canon(receipt(digest(self.first)))
        self.db.append_publication_receipt(SITE, first)
        self.db.save_workflow(self.second)
        second = canon(receipt(digest(self.second), 2, digest(RECEIPT_DOMAIN + first)))
        self.db.append_publication_receipt(SITE, second)
        return first, second

    def test_receipt_journal_matches_upstream_and_exact_retry(self):
        first, second = self.publish_two()
        self.assertEqual(self.journal.read_bytes(), first + second)
        first_pin = self.db.append_publication_receipt(SITE, first)
        self.assertEqual(first_pin, {"publication_receipt_sha256": digest(RECEIPT_DOMAIN + first),
                                     "trust": "signature_unverified"})
        self.assertEqual(self.journal.read_bytes(), first + second)
        check = subprocess.run(
            [sys.executable, "-I", "-B", str(MW_TOOLS / "journal_check.py"), str(self.journal),
             "--site", SITE, "--head", digest(RECEIPT_DOMAIN + second)], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def assert_pin_comes_from_receipt(self, first_pin):
        answer = self.db.load_pinned_workflow(SITE, first_pin)
        self.assertEqual(answer["workflow_sha256"], digest(self.first))
        self.assertEqual(answer["publication_receipt"], receipt(digest(self.first)))
        self.assertEqual(answer["trust"], "signature_unverified")
        self.assert_refused("RECEIPT_NOT_FOUND", self.db.load_pinned_workflow, SITE, digest(self.second))

    def test_pin_resolves_only_through_publication_receipt(self):
        first, _ = self.publish_two()
        (self.root / "PINS.json").write_bytes(canon({"workflow_sha256": digest(self.second)}))
        self.assert_pin_comes_from_receipt(digest(RECEIPT_DOMAIN + first))

    def assert_prior_revision_preserved(self):
        self.assertEqual(self.workflow_path(self.first).read_bytes(), self.first)
        self.assertEqual(self.workflow_path(self.second).read_bytes(), self.second)
        old = self.db.load_workflow(SITE, WORKFLOW_ID, digest(self.first))
        self.assertEqual(canon(old["workflow"]), self.first)

    def test_new_revision_preserves_old_bytes_and_receipt(self):
        first, second = self.publish_two()
        self.assert_prior_revision_preserved()
        for entry, expected in ((first, self.first), (second, self.second)):
            answer = self.db.load_pinned_workflow(SITE, digest(RECEIPT_DOMAIN + entry))
            self.assertEqual(canon(answer["workflow"]), expected)

    def test_torn_journal_refuses_reads_and_appends(self):
        first, second = self.publish_two()
        torn = (first + second)[:-1]
        self.journal.write_bytes(torn)
        self.assert_refused("JOURNAL_TORN", self.db.load_pinned_workflow, SITE, digest(RECEIPT_DOMAIN + first))
        self.assert_refused("JOURNAL_TORN", self.db.append_publication_receipt, SITE, second)
        self.assertEqual(self.journal.read_bytes(), torn)

    def test_receipt_chain_rejects_tampering_and_unlinked_records(self):
        first, second = self.publish_two()
        self.journal.write_bytes(first)
        wrong = canon(receipt(digest(self.second), 2, digest(first)))
        self.assert_refused("JOURNAL_CHAIN", self.db.append_publication_receipt, SITE, wrong)
        self.assertEqual(self.journal.read_bytes(), first)
        new_first = canon(dict(json.loads(first), mode="repair"))
        self.journal.write_bytes(new_first + second)
        self.assert_refused("JOURNAL_CHAIN", self.db.load_pinned_workflow, SITE,
                            digest(RECEIPT_DOMAIN + new_first))

    def test_concurrent_conflicting_appends_keep_one_committed_record(self):
        self.db.save_workflow(self.first)
        self.db.save_workflow(self.second)
        candidates = [canon(receipt(digest(raw))) for raw in (self.first, self.second)]

        def append(raw):
            try:
                return self.db.append_publication_receipt(SITE, raw)["publication_receipt_sha256"]
            except store.StoreError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(append, candidates))
        self.assertEqual(outcomes.count("JOURNAL_SEQ"), 1)
        committed = self.journal.read_bytes()
        self.assertIn(committed, candidates)
        self.assertIn(digest(RECEIPT_DOMAIN + committed), outcomes)

    def script_workflow(self, script):
        return canon(dict(self.workflow, script_ref="scripts/fixture.py", script_sha256=digest(script)))

    def test_script_without_mw_caps_is_refused(self):
        script = b"#!/usr/bin/env python3\nraise RuntimeError()\n"
        self.assert_refused("SCRIPT_CAPS_MISSING", self.db.save_workflow, self.script_workflow(script), script=script)
        self.assertFalse(self.site.exists())

    def test_script_is_retained_as_passive_bytes_and_reverified(self):
        caps = {"schema": "mw.caps.v1", "engines": [self.workflow["engine_compat"][0]["engine"]],
                "surface_access": "read_only", "network": "none", "filesystem": "none",
                "subprocess": "none"}
        script = b"# MW-CAPS: " + canon(caps) + b"raise RuntimeError()\n"
        raw = self.script_workflow(script)
        self.db.save_workflow(raw, script=script)
        self.assertEqual(self.db.load_workflow(SITE, WORKFLOW_ID, digest(raw))["script"], script)
        (self.workflow_path(raw).parent / "scripts/fixture.py").write_bytes(b"# changed\n")
        self.assert_refused("DIGEST_MISMATCH", self.db.load_workflow, SITE, WORKFLOW_ID, digest(raw))

    def test_refuses_symlinked_site(self):
        target = self.root / "other"
        target.mkdir()
        self.site.parent.mkdir()
        self.site.symlink_to(target, target_is_directory=True)
        self.assert_refused("CONTAINMENT", self.db.save_workflow, self.first)
        self.assertEqual(list(target.iterdir()), [])

    def test_frozen_build_schemas_check_refuses_drift(self):
        clone = self.root / "schemas"
        shutil.copytree(SCHEMAS, clone)
        command = [sys.executable, "-I", "-B", str(MW_TOOLS / "build_schemas.py"), "--check", "--dir", str(clone)]
        nominal = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(nominal.returncode, 0, nominal.stdout + nominal.stderr)
        view = clone / "workflow.v1.json"
        view.write_bytes(view.read_bytes().rstrip(b"\n"))
        drifted = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(drifted.returncode, 1, drifted.stdout + drifted.stderr)
        self.assertIn("workflow.v1.json differs from the bundle", drifted.stdout)

    def test_mutant_strip_lf_fails_canonical_oracle(self):
        with patch.object(store, "canonical_bytes", side_effect=lambda value: canon(value)[:-1]):
            with self.assertRaises(AssertionError):
                self.assert_upstream_canonical()

    def test_mutant_skip_digest_fails_load_oracle(self):
        self.db.save_workflow(self.first)
        self.workflow_path(self.first).write_bytes(self.second)
        self.assert_tampered_revision_refused()
        with patch.object(store, "_require_digest", return_value=None):
            with self.assertRaises(AssertionError):
                self.assert_tampered_revision_refused()

    def test_mutant_pins_json_fails_receipt_pin_oracle(self):
        first, _ = self.publish_two()
        pins = self.root / "PINS.json"
        pins.write_bytes(canon({"workflow_sha256": digest(self.second)}))

        def load_legacy_pin(site, _pin):
            revision = json.loads(pins.read_bytes())["workflow_sha256"]
            return self.db.load_workflow(site, WORKFLOW_ID, revision)

        with patch.object(self.db, "load_pinned_workflow", side_effect=load_legacy_pin):
            with self.assertRaises(AssertionError):
                self.assert_pin_comes_from_receipt(digest(RECEIPT_DOMAIN + first))

    def test_mutant_overwrite_fails_revision_retention_oracle(self):
        save = self.db.save_workflow

        def overwrite_previous(raw):
            saved = save(raw)
            if raw == self.second:
                self.workflow_path(self.first).write_bytes(raw)
            return saved

        with patch.object(self.db, "save_workflow", side_effect=overwrite_previous):
            self.publish_two()
        with self.assertRaises(AssertionError):
            self.assert_prior_revision_preserved()
