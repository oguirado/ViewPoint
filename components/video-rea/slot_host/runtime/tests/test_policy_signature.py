# SPDX-License-Identifier: AGPL-3.0-only
"""U03 closed signed-format verifier: offline openssl + strict JSON.

Covers AUTHORITY_DESIGN U03 / VERIFY crypto_dependency offline rows:
accept an externally signed positive; reject altered payload, wrong
key, wrong algorithm, oversized inputs, deadline expiry, duplicate
keys, extra fields, malformed envelopes and untrusted key files.
All keys are throwaway Ed25519/RSA/EC keys generated in the test
temp dir; no key outside that dir is ever read.
"""
import base64
import datetime
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from slot_host.runtime.policy_signature import (
    MAX_PAYLOAD_BYTES,
    POLICY_PURPOSE,
    POLICY_SCHEMA,
    PolicySignatureRejected,
    encode_envelope,
    key_id_for_public_key,
    verify_and_parse_envelope,
)

_OPENSSL = "/usr/bin/openssl"
_AUDIENCE = "viewpoint-rea-host"
_NOW = datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
_UTC = datetime.timezone.utc


def _run_openssl(args):
    return subprocess.run(
        [_OPENSSL] + list(args), shell=False, capture_output=True,
        timeout=10, env={"PATH": os.environ.get("PATH", "")},
    )


def _temp_root():
    return Path(tempfile.gettempdir()).resolve()


def _assert_in_temp(path: Path):
    assert str(path.resolve()).startswith(str(_temp_root()))


def _ed_keys(td: str, prefix: str = "ed"):
    priv = Path(td) / f"{prefix}-priv.pem"
    pub = Path(td) / f"{prefix}-pub.pem"
    r1 = _run_openssl(["genpkey", "-algorithm", "ED25519", "-out", str(priv)])
    assert r1.returncode == 0, r1.stderr.decode("utf-8", "replace")[:500]
    r2 = _run_openssl(["pkey", "-in", str(priv), "-pubout", "-out", str(pub)])
    assert r2.returncode == 0, r2.stderr.decode("utf-8", "replace")[:500]
    _assert_in_temp(priv)
    _assert_in_temp(pub)
    return priv, pub


def _rsa_keys(td: str, prefix: str = "rsa"):
    priv = Path(td) / f"{prefix}-priv.pem"
    pub = Path(td) / f"{prefix}-pub.pem"
    r1 = _run_openssl(["genpkey", "-algorithm", "RSA",
                       "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(priv)])
    assert r1.returncode == 0, r1.stderr.decode("utf-8", "replace")[:500]
    r2 = _run_openssl(["pkey", "-in", str(priv), "-pubout", "-out", str(pub)])
    assert r2.returncode == 0, r2.stderr.decode("utf-8", "replace")[:500]
    return priv, pub


def _ec_keys(td: str, prefix: str = "ec"):
    priv = Path(td) / f"{prefix}-priv.pem"
    pub = Path(td) / f"{prefix}-pub.pem"
    r1 = _run_openssl(["genpkey", "-algorithm", "EC",
                       "-pkeyopt", "ec_paramgen_curve:prime256v1", "-out", str(priv)])
    assert r1.returncode == 0, r1.stderr.decode("utf-8", "replace")[:500]
    r2 = _run_openssl(["pkey", "-in", str(priv), "-pubout", "-out", str(pub)])
    assert r2.returncode == 0, r2.stderr.decode("utf-8", "replace")[:500]
    return priv, pub


def _sign(priv: Path, payload_bytes: bytes, td: str, name: str = "policy"):
    ppath = Path(td) / f"{name}.json"
    spath = Path(td) / f"{name}.sig"
    ppath.write_bytes(payload_bytes)
    r = _run_openssl(["pkeyutl", "-sign", "-inkey", str(priv),
                      "-rawin", "-in", str(ppath), "-out", str(spath)])
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:500]
    return ppath, spath


def _derive_key_id(pub: Path) -> str:
    r = _run_openssl(["pkey", "-pubin", "-in", str(pub), "-outform", "DER"])
    assert r.returncode == 0
    return hashlib.sha256(r.stdout).hexdigest()


def _policy(key_id: str) -> dict:
    return {
        "schema": POLICY_SCHEMA,
        "purpose": POLICY_PURPOSE,
        "policy_id": "live-policy-2026-001",
        "issuer": "operator",
        "key_id": key_id,
        "audience": _AUDIENCE,
        "generation": 1,
        "issued_at": "2026-01-01T00:00:00Z",
        "not_before": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
        "baseline": {
            "mode": "live_after_conformance",
            "allowed_profiles": ["external_process"],
            "allowed_transports": ["cli_subcommand", "cli_argv"],
            "required_grants": ["grant.exec.external_process"],
            "perception_ceiling": "none",
            "shared_evidence_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2",
            "shared_evidence_sha256": "b" * 64,
            "conformance_kit_sha256": "c" * 64,
            "registry_generation": 1,
            "enrollment_ref": "enrollment:fixture@1",
        },
        "scope": [{"slot_id": "fixture-slot", "operation_ids": ["record", "replay", "compare"]}],
        "restrictions": [],
        "revocation": {
            "slot_ids": [], "operation_ids": [], "installation_refs": [],
            "implementation_sha256s": [], "revoked_generations": [],
        },
    }


def _payload_bytes(policy: dict) -> bytes:
    return (json.dumps(policy, separators=(",", ":")) + "\n").encode("utf-8")


class PolicySignatureTests(unittest.TestCase):
    def _verify(self, envelope, pub, **kw):
        kw.setdefault("public_key_path", str(pub))
        kw.setdefault("expected_audience", _AUDIENCE)
        kw.setdefault("now", _NOW)
        return verify_and_parse_envelope(envelope, **kw)

    def test_valid_signed_policy_passes_and_key_id_binds(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            policy = _policy(_derive_key_id(pub))
            payload = _payload_bytes(policy)
            _ppath, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            got = self._verify(envelope, pub)
            self.assertEqual(got, policy)
            self.assertEqual(got["key_id"], _derive_key_id(pub))
            self.assertEqual(key_id_for_public_key(str(pub)), _derive_key_id(pub))
            # Dict envelope form is accepted only with exact keys.
            env_dict = json.loads(envelope)
            self.assertEqual(self._verify(env_dict, pub), policy)

    def test_schema_file_matches_verifier_constants(self):
        schema_path = (Path(__file__).resolve().parents[3] / "contracts"
                       / "manifest" / "rea.live_policy_v1.schema.json")
        data = json.loads(schema_path.read_bytes())
        self.assertEqual(data["title"], "rea.live_policy/v1")
        self.assertEqual(data["properties"]["schema"]["const"], POLICY_SCHEMA)
        self.assertEqual(data["properties"]["purpose"]["const"], POLICY_PURPOSE)
        self.assertIs(data["additionalProperties"], False)
        self.assertIn("baseline", data["required"])
        self.assertIn("revocation", data["required"])

    def test_altered_payload_byte_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            policy = _policy(_derive_key_id(pub))
            payload = _payload_bytes(policy)
            _p, spath = _sign(priv, payload, td)
            flipped = bytearray(payload)
            flipped[10] ^= 0x01
            envelope = encode_envelope(bytes(flipped), spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, pub)
            self.assertEqual(ctx.exception.reason, "signature_invalid")

    def test_trailing_newline_is_part_of_signed_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            policy = _policy(_derive_key_id(pub))
            plain = json.dumps(policy, separators=(",", ":")).encode("utf-8")
            _p, spath = _sign(priv, plain, td)
            envelope = encode_envelope(plain + b"\n", spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, pub)
            self.assertEqual(ctx.exception.reason, "signature_invalid")

    def test_wrong_key_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, _pub = _ed_keys(td, "ed-one")
            _priv2, pub2 = _ed_keys(td, "ed-two")
            policy = _policy(_derive_key_id(pub2))
            # Payload claims pub2 key_id but is signed by ed-one.
            payload = _payload_bytes(policy)
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, pub2)
            self.assertEqual(ctx.exception.reason, "signature_invalid")

    def test_wrong_algorithm_rsa_public_key_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, _pub = _ed_keys(td, "ed")
            _rpriv, rpub = _rsa_keys(td, "rsa")
            policy = _policy("a" * 64)
            payload = _payload_bytes(policy)
            _p, spath = _sign(priv, payload, td)
            # 64-byte Ed25519 signature + RSA public key: length passes,
            # so only the explicit DER algorithm check can deny this.
            self.assertEqual(len(spath.read_bytes()), 64)
            envelope = encode_envelope(payload, spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, rpub)
            self.assertEqual(ctx.exception.reason, "public_key_wrong_algorithm")
            # An RSA-self-signed payload must also deny (algorithm, not
            # just signature length).
            rpayload = Path(td) / "rsa-payload.json"
            rsig = Path(td) / "rsa.sig"
            rpayload.write_bytes(payload)
            r = _run_openssl(["pkeyutl", "-sign", "-inkey", str(_rpriv),
                              "-rawin", "-in", str(rpayload), "-out", str(rsig)])
            self.assertEqual(r.returncode, 0)
            self.assertNotEqual(len(rsig.read_bytes()), 64)
            with self.assertRaises(PolicySignatureRejected):
                self._verify(encode_envelope(payload, rsig.read_bytes()), rpub)

    def test_wrong_algorithm_ec_public_key_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, _pub = _ed_keys(td, "ed")
            _ecpriv, ecpub = _ec_keys(td, "ec")
            payload = _payload_bytes(_policy("a" * 64))
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, ecpub)
            self.assertEqual(ctx.exception.reason, "public_key_wrong_algorithm")

    def test_oversize_payload_denies_before_verify(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            policy = _policy(_derive_key_id(pub))
            policy["$comment"] = "x" * (MAX_PAYLOAD_BYTES + 16)
            payload = _payload_bytes(policy)
            self.assertGreater(len(payload), MAX_PAYLOAD_BYTES)
            _p, spath = _sign(priv, payload, td)
            # Envelope helper itself must refuse the oversize payload.
            with self.assertRaises(PolicySignatureRejected):
                encode_envelope(payload, spath.read_bytes())
            raw = json.dumps({
                "payload_b64": base64.b64encode(payload).decode("ascii"),
                "signature_b64": base64.b64encode(spath.read_bytes()).decode("ascii"),
            }, separators=(",", ":")).encode("utf-8")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(raw, pub)
            self.assertEqual(ctx.exception.reason, "envelope_invalid")

    def test_timeout_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            payload = _payload_bytes(_policy(_derive_key_id(pub)))
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            for bad in (0.001, 0, -1, float("inf"), None, True, "5"):
                with self.subTest(timeout=repr(bad)):
                    with self.assertRaises(PolicySignatureRejected) as ctx:
                        self._verify(envelope, pub, timeout_s=bad)
                    self.assertEqual(ctx.exception.reason, "verify_timeout")

    def test_duplicate_key_in_payload_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            policy = _policy(_derive_key_id(pub))
            text = json.dumps(policy, separators=(",", ":"))
            dup_text = text.replace(
                '"schema":"rea.live_policy/v1"',
                '"schema":"rea.live_policy/v1","schema":"rea.live_policy/v1"', 1)
            payload = (dup_text + "\n").encode("utf-8")
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, pub)
            self.assertEqual(ctx.exception.reason, "duplicate_key")

    def test_extra_field_in_payload_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            policy = _policy(_derive_key_id(pub))
            policy["unexpected"] = 1
            payload = _payload_bytes(policy)
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, pub)
            self.assertEqual(ctx.exception.reason, "policy_invalid")

    def test_extra_field_in_envelope_denies_without_selecting_key(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            _priv2, pub2 = _ed_keys(td, "ed-two")
            payload = _payload_bytes(_policy(_derive_key_id(pub)))
            _p, spath = _sign(priv, payload, td)
            good = json.loads(encode_envelope(payload, spath.read_bytes()))
            for extra in ({"algorithm": "ED25519"}, {"key_path": str(pub2)},
                          {"key_id": _derive_key_id(pub2)},
                          {"public_key": str(pub2)}, {"url": "https://example.com/k"}):
                with self.subTest(extra=sorted(extra)):
                    tampered = dict(good)
                    tampered.update(extra)
                    if "key_id" in extra:
                        tampered["key_id"] = extra["key_id"]
                    with self.assertRaises(PolicySignatureRejected) as ctx:
                        self._verify(tampered, pub)
                    self.assertEqual(ctx.exception.reason, "envelope_invalid")

    def test_malformed_envelope_variants_deny(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            payload = _payload_bytes(_policy(_derive_key_id(pub)))
            _p, spath = _sign(priv, payload, td)
            sig = spath.read_bytes()
            good = {"payload_b64": base64.b64encode(payload).decode("ascii"),
                    "signature_b64": base64.b64encode(sig).decode("ascii")}
            cases = [
                {"payload_b64": good["payload_b64"]},
                {"signature_b64": good["signature_b64"]},
                {"payload_b64": "not base64!!", "signature_b64": good["signature_b64"]},
                {"payload_b64": good["payload_b64"] + " ", "signature_b64": good["signature_b64"]},
                {"payload_b64": good["payload_b64"].rstrip("="), "signature_b64": good["signature_b64"]},
                {"payload_b64": good["payload_b64"],
                 "signature_b64": base64.b64encode(sig[:-1]).decode("ascii")},
                {"payload_b64": good["payload_b64"],
                 "signature_b64": base64.b64encode(sig + b"\x00").decode("ascii")},
                {"payload_b64": 123, "signature_b64": good["signature_b64"]},
                {"payload_b64": good["payload_b64"], "signature_b64": None},
                b"not json",
                b'{"payload_b64": "abc"}',
                b"[]",
                b"",
                42,
                None,
            ]
            for i, bad in enumerate(cases):
                with self.subTest(case=i):
                    with self.assertRaises(PolicySignatureRejected):
                        self._verify(bad, pub)

    def test_non_utf8_and_nonfinite_payload_deny(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            # Non-UTF-8 payload (signed): must deny after verify, not crash.
            bad = b"\xff\xfenot-json"
            bad_sig_path = Path(td) / "bad.sig"
            src = Path(td) / "bad.json"
            src.write_bytes(bad)
            r = _run_openssl(["pkeyutl", "-sign", "-inkey", str(priv),
                              "-rawin", "-in", str(src), "-out", str(bad_sig_path)])
            self.assertEqual(r.returncode, 0)
            with self.assertRaises(PolicySignatureRejected):
                self._verify(encode_envelope(bad, bad_sig_path.read_bytes()), pub)
            # NaN generation (signed): strict JSON must reject non-finite.
            text = json.dumps(_policy(key_id), separators=(",", ":")).replace(
                '"generation":1', '"generation":NaN', 1)
            nan_payload = (text + "\n").encode("utf-8")
            _p2, spath2 = _sign(priv, nan_payload, td, "nan")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(encode_envelope(nan_payload, spath2.read_bytes()), pub)
            self.assertIn(ctx.exception.reason, ("json_invalid", "policy_invalid"))

    def test_untrusted_public_key_file_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            payload = _payload_bytes(_policy(_derive_key_id(pub)))
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            # Symlink to the real key must not be followed.
            link = Path(td) / "link-pub.pem"
            link.symlink_to(pub)
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, link)
            self.assertEqual(ctx.exception.reason, "public_key_untrusted")
            # Directory as key.
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, Path(td))
            self.assertEqual(ctx.exception.reason, "public_key_untrusted")
            # Group/world-writable key.
            writable = Path(td) / "writable-pub.pem"
            writable.write_bytes(pub.read_bytes())
            writable.chmod(0o666)
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, writable)
            self.assertEqual(ctx.exception.reason, "public_key_untrusted")
            # Missing, relative and garbage key files.
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, Path(td) / "missing.pem")
            self.assertEqual(ctx.exception.reason, "public_key_invalid")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                verify_and_parse_envelope(
                    envelope, public_key_path="pub.pem",
                    expected_audience=_AUDIENCE, now=_NOW)
            self.assertEqual(ctx.exception.reason, "public_key_invalid")
            garbage = Path(td) / "garbage.pem"
            garbage.write_bytes(b"not a key")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, garbage)
            self.assertIn(ctx.exception.reason,
                          ("public_key_invalid", "public_key_wrong_algorithm"))

    def test_key_id_and_audience_binding_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            # Payload key_id does not match the pinned key.
            payload = _payload_bytes(_policy("a" * 64))
            _p, spath = _sign(priv, payload, td)
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(encode_envelope(payload, spath.read_bytes()), pub)
            self.assertEqual(ctx.exception.reason, "key_id_mismatch")
            # Host expected_key_id does not match the pinned key.
            payload2 = _payload_bytes(_policy(key_id))
            _p2, spath2 = _sign(priv, payload2, td, "pol2")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(encode_envelope(payload2, spath2.read_bytes()), pub,
                             expected_key_id="a" * 64)
            self.assertEqual(ctx.exception.reason, "key_id_mismatch")
            # Host audience does not match the payload.
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(encode_envelope(payload2, spath2.read_bytes()), pub,
                             expected_audience="other-host")
            self.assertEqual(ctx.exception.reason, "audience_mismatch")

    def test_time_interval_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            payload = _payload_bytes(_policy(_derive_key_id(pub)))
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            early = datetime.datetime(2025, 12, 31, 23, 59, 59, tzinfo=_UTC)
            late = datetime.datetime(2027, 1, 1, 0, 0, 0, tzinfo=_UTC)
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, pub, now=early)
            self.assertEqual(ctx.exception.reason, "policy_not_yet_valid")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                self._verify(envelope, pub, now=late)
            self.assertEqual(ctx.exception.reason, "policy_expired")
            # Naive now and non-datetime now fail closed too.
            with self.assertRaises(PolicySignatureRejected):
                self._verify(envelope, pub, now=datetime.datetime(2026, 6, 1))
            with self.assertRaises(PolicySignatureRejected):
                self._verify(envelope, pub, now="2026-06-01")

    def test_schema_purpose_generation_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            variants = []
            bad_schema = _policy(key_id)
            bad_schema["schema"] = "rea.live_policy/v2"
            variants.append(bad_schema)
            bad_purpose = _policy(key_id)
            bad_purpose["purpose"] = "viewpoint.rea.other"
            variants.append(bad_purpose)
            for field, bad in (("generation", 0), ("generation", -1),
                               ("generation", "1"), ("generation", 1.0),
                               ("generation", True), ("generation", None)):
                pol = _policy(key_id)
                pol[field] = bad
                variants.append(pol)
            for i, pol in enumerate(variants):
                with self.subTest(case=i):
                    payload = _payload_bytes(pol)
                    _p, spath = _sign(priv, payload, td, f"var{i}")
                    with self.assertRaises(PolicySignatureRejected) as ctx:
                        self._verify(encode_envelope(payload, spath.read_bytes()), pub)
                    self.assertEqual(ctx.exception.reason, "policy_invalid")

    def test_scope_restriction_revocation_validation_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            variants = []
            p = _policy(key_id)
            p["scope"] = [{"slot_id": "fixture-slot", "namespace": "external-tools"}]
            variants.append(p)
            p = _policy(key_id)
            p["scope"] = [{"operation_ids": ["record"]}]
            variants.append(p)
            p = _policy(key_id)
            p["scope"] = {}
            variants.append(p)
            p = _policy(key_id)
            p["restrictions"] = [{"kind": "disable", "target": "fixture-slot",
                                  "timeout_s": 5}]
            variants.append(p)
            p = _policy(key_id)
            p["restrictions"] = [{"kind": "lower_perception_ceiling",
                                  "target": "fixture-slot"}]
            variants.append(p)
            p = _policy(key_id)
            p["revocation"] = {"slot_ids": [], "operation_ids": [],
                               "installation_refs": ["installation:a"] * 2,
                               "implementation_sha256s": [], "revoked_generations": []}
            variants.append(p)
            p = _policy(key_id)
            p["baseline"]["shared_evidence_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@pending"
            variants.append(p)
            for i, pol in enumerate(variants):
                with self.subTest(case=i):
                    payload = _payload_bytes(pol)
                    _p, spath = _sign(priv, payload, td, f"str{i}")
                    with self.assertRaises(PolicySignatureRejected) as ctx:
                        self._verify(encode_envelope(payload, spath.read_bytes()), pub)
                    self.assertEqual(ctx.exception.reason, "policy_invalid")

            deny_all = _policy(key_id)
            deny_all["scope"] = []
            payload = _payload_bytes(deny_all)
            _, signature = _sign(priv, payload, td, "deny-all")
            self.assertEqual(self._verify(encode_envelope(payload, signature.read_bytes()), pub)["scope"], [])

    def test_verifier_fails_closed_on_wrong_input_types(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            payload = _payload_bytes(_policy(_derive_key_id(pub)))
            _p, spath = _sign(priv, payload, td)
            envelope = encode_envelope(payload, spath.read_bytes())
            # None audience / bad key path are config errors, but the
            # verifier must still deny with PolicySignatureRejected.
            with self.assertRaises(PolicySignatureRejected):
                verify_and_parse_envelope(
                    envelope, public_key_path=str(pub),
                    expected_audience=None, now=_NOW)
            with self.assertRaises(PolicySignatureRejected):
                verify_and_parse_envelope(
                    envelope, public_key_path=None,
                    expected_audience=_AUDIENCE, now=_NOW)
            with self.assertRaises(PolicySignatureRejected):
                verify_and_parse_envelope(
                    envelope, public_key_path=str(pub),
                    expected_audience=_AUDIENCE, now=_NOW,
                    expected_key_id="not-hex")
            with self.assertRaises(PolicySignatureRejected):
                encode_envelope("not bytes", spath.read_bytes())
            with self.assertRaises(PolicySignatureRejected):
                encode_envelope(payload, b"short")


if __name__ == "__main__":
    unittest.main()
