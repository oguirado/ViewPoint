"""Offline conformance tests for the frozen and retention wire contracts."""

from base64 import b64decode
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from retention_service.contracts.signer import decode_sign_reply, decode_sign_request
from retention_service.contracts.validation import (
    ContractError, ContractValidator, FROZEN_PINS, INDEX_V1_URI, RECORD_V02_URI,
    RECORD_V03_URI, WIRE_PATH, strict_json,
)
from retention_service.contracts.wire import (
    DENIAL_BODY, DENIAL_RESPONSE, DIGEST_HEADER, MAX_ARTIFACT_BYTES, MAX_FINISH_BYTES,
    MAX_RECORD_BYTES, PAGE_HEADERS, TENANT_HEADER, admission_bytes, decode_base64,
    decode_finish, decode_record, decode_start, page_parameters, record_bytes,
    tenant_header, verify_admission,
)

VECTOR_PATH = Path(__file__).with_name("vectors.json")


def load_vectors():
    return strict_json(VECTOR_PATH.read_bytes())


def vector_assertions(vector, validator):
    data = vector["data"]
    check = unittest.TestCase()
    match vector["kind"]:
        case "schema":
            validator.validate(vector["form"], data)
        case "admission":
            verify_admission(data, validator)
            check.assertEqual(admission_bytes(data, validator), vector["canonical_utf8"].encode())
        case "config":
            effective = validator.load_config(data)
            check.assertIs(effective["local_only"], vector["local_only"])
        case "start":
            raw = b64decode(data)
            record = decode_start(raw, content_length=vector["bytes"], digest=vector["digest"], validator=validator)
            check.assertEqual(record_bytes(record), raw)
        case "terminal":
            raw = b64decode(data)
            record = decode_record(raw, "Terminal", validator)
            check.assertEqual(record_bytes(record), raw)
            check.assertEqual(len(raw), vector["bytes"])
            check.assertEqual(sha256(raw).hexdigest(), vector["digest"])
        case "finish":
            terminal, artifact = decode_finish(b64decode(data), validator)
            check.assertEqual(sha256(terminal).hexdigest(), vector["terminal_sha256"])
            check.assertEqual(sha256(artifact).hexdigest(), vector["artifact_sha256"])
        case "recovery_bytes":
            raw = b64decode(data)
            validator.validate(vector["form"], strict_json(raw))
            check.assertEqual(sha256(raw).hexdigest(), vector["sha256"])
        case "base64":
            raw = decode_base64(data, maximum=vector["maximum"], allow_empty=False)
            check.assertEqual(raw.hex(), vector["hex"])
        case "denial":
            raw = b64decode(data)
            if raw != DENIAL_RESPONSE:
                raise ContractError("denial bytes differ")
            check.assertEqual(raw.split(b"\r\n\r\n")[1], DENIAL_BODY)
            check.assertEqual(len(DENIAL_BODY), 23)
        case "page_headers":
            check.assertEqual(tuple(data), PAGE_HEADERS)
            check.assertEqual(TENANT_HEADER, "Retention-Tenant")
            check.assertEqual(DIGEST_HEADER, "Retention-SHA256")
        case "page":
            result = page_parameters(*data)
            check.assertEqual(result, tuple(vector["result"]))
        case "tenant":
            result = tenant_header(data, validator)
            check.assertEqual(result, data[0])
        case "receipt":
            validator.validate_live_receipt_binding(*data)
        case "sign_request":
            nonce, handle, digest = decode_sign_request(bytes.fromhex(data))
            check.assertEqual((nonce.hex(), handle, digest.hex()), (vector["nonce"], vector["handle"], vector["digest"]))
        case "sign_reply":
            signature = decode_sign_reply(bytes.fromhex(data), bytes.fromhex(vector["nonce"]))
            check.assertEqual(signature.hex() if signature is not None else None, vector["signature"])
        case _:
            raise AssertionError(f"unknown vector kind: {vector['kind']}")


def check_vector(vector, validator):
    try:
        vector_assertions(vector, validator)
    except ContractError:
        if vector["valid"]:
            raise
    else:
        if not vector["valid"]:
            raise AssertionError("invalid vector accepted")


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frozen = Path(os.environ["RETENTION_FROZEN_DIR"])
        cls.validator = ContractValidator(cls.frozen)
        cls.vectors = load_vectors()

    def test_vectors_are_unique(self):
        self.assertEqual(len({v["name"] for v in self.vectors}), len(self.vectors))
        self.assertTrue(any(v["valid"] for v in self.vectors))
        self.assertTrue(any(not v["valid"] for v in self.vectors))

    def test_frozen_v02_examples(self):
        samples = sorted((self.frozen / "examples").glob("attempt_evidence_record.*.json"))
        self.assertTrue(samples)
        for path in samples:
            if path.name.endswith(".schema.json"):
                continue
            with self.subTest(path=path.name):
                value = strict_json(path.read_bytes())
                if ".invalid." in path.name:
                    with self.assertRaises(ContractError):
                        self.validator.validate_pinned(RECORD_V02_URI, value)
                else:
                    self.validator.validate_pinned(RECORD_V02_URI, value)

    def test_frozen_v03_examples(self):
        samples = sorted((self.frozen / "drafts").glob("attempt_evidence_record.valid.*.json"))
        self.assertTrue(samples)
        for path in samples:
            with self.subTest(path=path.name):
                value = strict_json(path.read_bytes())
                self.validator.validate_pinned(RECORD_V03_URI, value)

    def test_frozen_v03_invalid_record_shape(self):
        path = self.frozen / "drafts" / "attempt_evidence_record.invalid.started_events_object_ref.json"
        with self.assertRaises(ContractError):
            self.validator.validate_pinned(RECORD_V03_URI, strict_json(path.read_bytes()))

    def test_frozen_index_examples(self):
        samples = sorted((self.frozen / "drafts").glob("rea_retention_index.*.json"))
        self.assertTrue(samples)
        for path in samples:
            if path.name.endswith(".schema.json"):
                continue
            with self.subTest(path=path.name):
                index = strict_json(path.read_bytes())
                if ".invalid." in path.name:
                    with self.assertRaises(ContractError):
                        self.validator.validate_index(index)
                else:
                    self.validator.validate_index(index)
                    bad = {**index, "profile_sha256": "0" * 64}
                    self.validator.validate_pinned(INDEX_V1_URI, bad)
                    with self.assertRaisesRegex(ContractError, "profile binding"):
                        self.validator.validate_index(bad)

    def test_all_frozen_pin_mutations_fail_at_startup(self):
        names = ["FROZEN_SHA256SUMS", *(filename for _, filename, _ in FROZEN_PINS)]
        with TemporaryDirectory(dir=os.environ.get("RETENTION_TEST_TMP")) as directory:
            for name in names:
                path = Path(directory) / name
                path.write_bytes((self.frozen / name).read_bytes())
            for name in names:
                with self.subTest(file=name):
                    path = Path(directory) / name
                    verified = path.read_bytes()
                    path.write_bytes(verified + b"\n")
                    with self.assertRaisesRegex(ContractError, "digest mismatch"):
                        ContractValidator(directory)
                    path.write_bytes(verified)

    def test_invalid_schema_references_fail_before_validation(self):
        schema = strict_json(WIRE_PATH.read_bytes())
        missing = (
            "https://metaviewer.local/contracts/stale/attempt_evidence_record.schema.json",
            RECORD_V03_URI + "#/$defs/missing",
            "#/$defs/missing",
            "file:///unregistered-schema.json",
        )
        with TemporaryDirectory(dir=os.environ.get("RETENTION_TEST_TMP")) as directory:
            path = Path(directory) / "schema.json"
            for ref in missing:
                with self.subTest(ref=ref):
                    bad = deepcopy(schema)
                    bad["$defs"]["Start"]["allOf"][0]["$ref"] = ref
                    path.write_text(json.dumps(bad))
                    with patch("socket.socket", side_effect=AssertionError("network access")):
                        with self.assertRaisesRegex(ContractError, "unresolved local schema reference"):
                            ContractValidator(self.frozen, wire_path=path)

    def test_unsupported_schema_assertions_fail_at_startup(self):
        schema = strict_json(WIRE_PATH.read_bytes())
        with TemporaryDirectory(dir=os.environ.get("RETENTION_TEST_TMP")) as directory:
            path = Path(directory) / "schema.json"
            for bad in ({"format": "future-format"}, {"minBytes": 1}, {"$dynamicRef": "#future"}):
                with self.subTest(bad=bad):
                    schema["$defs"]["Unsupported"] = {"type": "string", **bad}
                    path.write_text(json.dumps(schema))
                    with self.assertRaisesRegex(ContractError, "unsupported"):
                        ContractValidator(self.frozen, wire_path=path)

    def test_strict_json_rejects_invalid_octets_and_nonfinite_numbers(self):
        for data in (b"\xff", b"\xef\xbb\xbf{}", b"NaN", b"Infinity", b"1e999", b'{"x":{"y":0,"y":1}}'):
            with self.subTest(data=data), self.assertRaises(ContractError):
                strict_json(data)

    def test_unpaired_surrogate_cannot_be_hashed_in_admission(self):
        admission = next(v["data"] for v in self.vectors if v["name"] == "record-admission")
        with self.assertRaisesRegex(ContractError, "non-Unicode"):
            admission_bytes({**admission, "provider_revision": "\ud800"}, self.validator)

    def test_oversize_record_and_envelope_rejected_before_json_parse(self):
        with patch("retention_service.contracts.wire.strict_json", side_effect=AssertionError("parsed")):
            for name in ("Start", "Terminal"):
                with self.subTest(name=name), self.assertRaisesRegex(ContractError, "record exceeds"):
                    decode_record(b" " * (MAX_RECORD_BYTES + 1), name, self.validator)
            with self.assertRaisesRegex(ContractError, "finish envelope exceeds"):
                decode_finish(b" " * (MAX_FINISH_BYTES + 1), self.validator)

    def test_base64_bound_checks_precede_allocation(self):
        for maximum in (MAX_RECORD_BYTES, MAX_ARTIFACT_BYTES):
            with self.subTest(maximum=maximum):
                # Filling the last quartet has the allowed encoded length
                # but decodes to more than the byte limit.
                encoded = "A" * (4 * ((maximum + 2) // 3))
                with patch("retention_service.contracts.wire.b64decode", side_effect=AssertionError("decoded")):
                    with self.assertRaisesRegex(ContractError, "decoded bytes exceed"):
                        decode_base64(encoded, maximum=maximum, allow_empty=False)


def _vector_test(vector):
    def test(self):
        check_vector(vector, self.validator)

    return test


for _vector in load_vectors():
    setattr(ContractTests, "test_vector_" + _vector["name"], _vector_test(_vector))
