# SPDX-License-Identifier: AGPL-3.0-only
"""U04 monotonic activation: generation, expiry, rollback, corrupt state, monotonic clock.

Covers AUTHORITY_DESIGN U04 / VERIFY gate offline rows:
* Valid signed policy and exact repeat pass.
* Higher generation passes.
* Same-generation conflict, rollback (lower generation), expiry,
  clock rollback, corrupt and missing state, wrong key and altered
  payload deny (fail closed).
* Atomic durable publish (generation + digest + revocation + clock).
All keys are throwaway Ed25519 keys generated in the test temp dir
only; no key outside that dir is ever read.
"""
import base64
import datetime
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from slot_host.runtime.binding import _PUBLISHED_SHA
from slot_host.runtime.conformance_authority import policy_covers
from slot_host.runtime.policy_activation import (
    ACTIVATION_SCHEMA,
    PolicyActivationRejected,
    activate,
    assert_active,
    initialize_activation_state,
    is_active,
    load_activation_state,
)
from slot_host.runtime.policy_signature import (
    POLICY_PURPOSE,
    POLICY_SCHEMA,
    PolicySignatureRejected,
    encode_envelope,
    key_id_for_public_key,
)

_OPENSSL = "/usr/bin/openssl"
_AUDIENCE = "viewpoint-rea-host"
_UTC = datetime.timezone.utc
_NOW = datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=_UTC)
_NOW_LATER = datetime.datetime(2026, 6, 2, 0, 0, 0, tzinfo=_UTC)
_NOW_EARLIER = datetime.datetime(2026, 5, 31, 0, 0, 0, tzinfo=_UTC)

def _run_openssl(args):
    return subprocess.run(
        [_OPENSSL] + list(args), shell=False, capture_output=True,
        timeout=10, env={"PATH": os.environ.get("PATH", "")},
    )

def _temp_root():
    return Path(tempfile.gettempdir()).resolve()

def _ed_keys(td: str, prefix: str = "ed"):
    priv = Path(td) / f"{prefix}-priv.pem"
    pub = Path(td) / f"{prefix}-pub.pem"
    r1 = _run_openssl(["genpkey", "-algorithm", "ED25519", "-out", str(priv)])
    assert r1.returncode == 0, r1.stderr.decode("utf-8", "replace")[:500]
    r2 = _run_openssl(["pkey", "-in", str(priv), "-pubout", "-out", str(pub)])
    assert r2.returncode == 0, r2.stderr.decode("utf-8", "replace")[:500]
    assert str(priv.resolve()).startswith(str(_temp_root()))
    assert str(pub.resolve()).startswith(str(_temp_root()))
    return priv, pub

def _derive_key_id(pub: Path) -> str:
    r = _run_openssl(["pkey", "-pubin", "-in", str(pub), "-outform", "DER"])
    assert r.returncode == 0
    return hashlib.sha256(r.stdout).hexdigest()

def _policy(key_id: str, *, generation=1, not_before="2026-01-01T00:00:00Z", expires_at="2027-01-01T00:00:00Z", **over):
    pol = {
        "schema": POLICY_SCHEMA,
        "purpose": POLICY_PURPOSE,
        "policy_id": "live-policy-2026-001",
        "issuer": "operator",
        "key_id": key_id,
        "audience": _AUDIENCE,
        "generation": generation,
        "issued_at": "2026-01-01T00:00:00Z",
        "not_before": not_before,
        "expires_at": expires_at,
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
    for k, v in over.items():
        pol[k] = v
    return pol

def _payload_bytes(policy: dict) -> bytes:
    return (json.dumps(policy, separators=(",", ":")) + "\n").encode("utf-8")

def _sign(priv: Path, payload_bytes: bytes, td: str, name: str = "policy"):
    ppath = Path(td) / f"{name}.json"
    spath = Path(td) / f"{name}.sig"
    ppath.write_bytes(payload_bytes)
    r = _run_openssl(["pkeyutl", "-sign", "-inkey", str(priv), "-rawin", "-in", str(ppath), "-out", str(spath)])
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[:500]
    return ppath, spath

def _make_envelope(priv: Path, policy: dict, td: str, name: str = "policy"):
    payload = _payload_bytes(policy)
    _p, spath = _sign(priv, payload, td, name)
    return encode_envelope(payload, spath.read_bytes())

class PolicyActivationTests(unittest.TestCase):
    def _setup(self, td):
        priv, pub = _ed_keys(td)
        key_id = _derive_key_id(pub)
        state_path = Path(td) / "activation.json"
        initialize_activation_state(
            str(state_path), audience=_AUDIENCE, key_id=key_id,
            minimum_generation=1, now=_NOW,
        )
        return priv, pub, key_id, state_path

    def test_valid_signed_policy_passes_and_exact_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            env = _make_envelope(priv, _policy(key_id, generation=1), td, "gen1")
            got = activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(got["generation"], 1)
            self.assertEqual(got["audience"], _AUDIENCE)
            self.assertEqual(got["key_id"], key_id)
            self.assertIsNotNone(got["payload_sha256"])
            self.assertEqual(got["max_clock"], "2026-06-01T00:00:00Z")
            # Exact repeat with same envelope and same clock succeeds without generation bump
            again = activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(again["generation"], 1)
            # Also with dict form
            env_dict = json.loads(env)
            third = activate(env_dict, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(third["payload_sha256"], got["payload_sha256"])
            # Later clock for exact repeat should advance max_clock but keep gen
            later = activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW_LATER, state_path=str(state_path))
            self.assertEqual(later["generation"], 1)
            self.assertEqual(later["max_clock"], "2026-06-02T00:00:00Z")
            self.assertTrue(is_active(str(state_path), now=_NOW_LATER))

    def test_higher_generation_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            e1 = _make_envelope(priv, _policy(key_id, generation=1), td, "g1")
            g1_state = activate(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            e2 = _make_envelope(priv, _policy(key_id, generation=2), td, "g2")
            got = activate(e2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW_LATER, state_path=str(state_path))
            self.assertEqual(got["generation"], 2)
            self.assertNotEqual(g1_state["payload_sha256"], got["payload_sha256"])
            self.assertIsNotNone(got["payload_sha256"]) # placeholder, replaced below
            # assert_active for new generation succeeds
            st = assert_active(e2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW_LATER, state_path=str(state_path))
            self.assertEqual(st["generation"], 2)
            # assert_active for old generation must mismatch
            with self.assertRaises(PolicyActivationRejected) as ctx:
                assert_active(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW_LATER, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "active_mismatch")

    def test_same_generation_conflict_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            e1 = _make_envelope(priv, _policy(key_id, generation=1), td, "c1")
            activate(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            # Same generation, different payload (different policy_id)
            pol2 = _policy(key_id, generation=1)
            pol2["policy_id"] = "live-policy-2026-001-alt"
            e_conf = _make_envelope(priv, pol2, td, "conf")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(e_conf, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "generation_conflict")
            # State unchanged
            self.assertEqual(load_activation_state(str(state_path))["generation"], 1)

    def test_generation_rollback_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            e2 = _make_envelope(priv, _policy(key_id, generation=2), td, "rb2")
            activate(e2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            e1 = _make_envelope(priv, _policy(key_id, generation=1), td, "rb1")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "generation_rollback")

    def test_expiry_and_not_yet_valid_deny(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            # Expired
            e_exp = _make_envelope(priv, _policy(key_id, generation=1, expires_at="2026-01-02T00:00:00Z"), td, "exp")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                activate(e_exp, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "policy_expired")
            # Not yet valid
            e_nb = _make_envelope(priv, _policy(key_id, generation=1, not_before="2027-01-01T00:00:00Z", expires_at="2028-01-01T00:00:00Z"), td, "nb")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                activate(e_nb, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "policy_not_yet_valid")
            # is_active respects expiry
            self.assertFalse(is_active(str(state_path), now=_NOW))
            # After committing valid then moving time past expiry, is_active becomes False
            e_ok = _make_envelope(priv, _policy(key_id, generation=1), td, "ok")
            activate(e_ok, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertTrue(is_active(str(state_path), now=_NOW))
            self.assertFalse(is_active(str(state_path), now=datetime.datetime(2027, 1, 1, 0, 0, 0, tzinfo=_UTC)))

    def test_clock_rollback_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            e1 = _make_envelope(priv, _policy(key_id, generation=1), td, "clk1")
            activate(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW_LATER, state_path=str(state_path))
            # Now with earlier clock but higher generation should still deny due to clock rollback
            e2 = _make_envelope(priv, _policy(key_id, generation=2), td, "clk2")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(e2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "clock_rollback")
            # Even exact repeat with earlier clock denies
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "clock_rollback")

    def test_corrupt_state_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            e1 = _make_envelope(priv, _policy(key_id, generation=1), td, "cor1")
            activate(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            # Corrupt the state file: duplicate key
            raw = state_path.read_bytes()
            text = raw.decode("utf-8")
            # Duplicate schema key
            dup = text.replace('"schema":"rea.live_activation/v1"', '"schema":"rea.live_activation/v1","schema":"rea.live_activation/v1"', 1)
            state_path.write_bytes(dup.encode("utf-8"))
            e2 = _make_envelope(priv, _policy(key_id, generation=2), td, "cor2")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(e2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "state_corrupt")
            # Also wrong schema
            bad_schema = '{"schema":"evil","audience":"%s","key_id":"%s","minimum_generation":1,"generation":1,"payload_sha256":null,"policy_id":null,"not_before":null,"expires_at":null,"activated_at":null,"max_clock":null,"revoked_generations":[]}' % (_AUDIENCE, key_id)
            state_path.write_bytes(bad_schema.encode("utf-8") + b"\n")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                _ = load_activation_state(str(state_path))
            self.assertEqual(ctx.exception.reason, "state_corrupt")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(e2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "state_corrupt")
            # Not JSON
            state_path.write_bytes(b"not json")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                _ = load_activation_state(str(state_path))
            self.assertEqual(ctx.exception.reason, "state_corrupt")
            # Extra field
            valid = json.loads(raw.decode("utf-8"))
            valid["extra"] = 1
            state_path.write_bytes((json.dumps(valid) + "\n").encode("utf-8"))
            with self.assertRaises(PolicyActivationRejected) as ctx:
                _ = load_activation_state(str(state_path))
            self.assertEqual(ctx.exception.reason, "state_corrupt")

    def test_missing_state_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            state_path = Path(td) / "activation.json"
            # No initialize -> activate must deny with state_missing
            env = _make_envelope(priv, _policy(key_id, generation=1), td, "miss")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "state_missing")
            # After initialize, re-activate after file deletion must also deny (previously initialized)
            initialize_activation_state(str(state_path), audience=_AUDIENCE, key_id=key_id, minimum_generation=1, now=_NOW)
            env2 = _make_envelope(priv, _policy(key_id, generation=1), td, "miss2")
            activate(env2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            # Now simulate deletion (previous commit exists would be missing)
            state_path.unlink()
            env3 = _make_envelope(priv, _policy(key_id, generation=2), td, "miss3")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(env3, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "state_missing")

    def test_wrong_key_and_altered_bytes_deny(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td, "ed-one")
            priv2, pub2 = _ed_keys(td, "ed-two")
            key_id = _derive_key_id(pub)
            key_id2 = _derive_key_id(pub2)
            state_path = Path(td) / "activation.json"
            initialize_activation_state(str(state_path), audience=_AUDIENCE, key_id=key_id, minimum_generation=1, now=_NOW)
            # Payload claims key_id of pub but signed by priv2 -> signature_invalid
            env = _make_envelope(priv2, _policy(key_id, generation=1), td, "wrongkey")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "signature_invalid")
            # Altered payload byte
            good_priv, _good_pub = priv, pub
            env_good = _make_envelope(good_priv, _policy(key_id, generation=1), td, "good")
            # Flip a byte in payload_b64 decoded payload
            obj = json.loads(env_good)
            payload = base64.b64decode(obj["payload_b64"])
            sig = base64.b64decode(obj["signature_b64"])
            flipped = bytearray(payload)
            flipped[10] ^= 0x01
            tampered = json.dumps({
                "payload_b64": base64.b64encode(bytes(flipped)).decode("ascii"),
                "signature_b64": obj["signature_b64"],
            }, separators=(",", ":")).encode("utf-8") + b"\n"
            with self.assertRaises(PolicySignatureRejected) as ctx:
                activate(tampered, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "signature_invalid")
            # Audience mismatch
            env_aud = _make_envelope(priv, _policy(key_id, generation=1), td, "aud")
            with self.assertRaises(PolicySignatureRejected) as ctx:
                activate(env_aud, public_key_path=str(pub), expected_audience="other-host", now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "audience_mismatch")

    def test_minimum_generation_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            state_path = Path(td) / "activation.json"
            initialize_activation_state(str(state_path), audience=_AUDIENCE, key_id=key_id, minimum_generation=5, now=_NOW)
            env = _make_envelope(priv, _policy(key_id, generation=1), td, "lowgen")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "minimum_generation")

    def test_audience_and_key_binding_from_state_deny(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            state_path = Path(td) / "activation.json"
            initialize_activation_state(str(state_path), audience=_AUDIENCE, key_id=key_id, minimum_generation=1, now=_NOW)
            env = _make_envelope(priv, _policy(key_id, generation=1), td, "bind1")
            # Activate with mismatched expected audience (state says viewpoint-rea-host)
            with self.assertRaises((PolicyActivationRejected, PolicySignatureRejected)) as ctx:
                activate(env, public_key_path=str(pub), expected_audience="other", now=_NOW, state_path=str(state_path))
            self.assertIn(ctx.exception.reason, ("audience_mismatch", "audience_invalid"))

    def test_revoked_self_generation_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            pol = _policy(key_id, generation=1)
            pol["revocation"]["revoked_generations"] = [1]
            env = _make_envelope(priv, pol, td, "rev")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "generation_revoked")

    def test_assert_active_recovery_disagreement_denies(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, state_path = self._setup(td)
            e1 = _make_envelope(priv, _policy(key_id, generation=1), td, "rec1")
            activate(e1, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            # Different payload with same generation already would have triggered conflict,
            # but assert with a fresh higher generation that was never committed should mismatch
            e2 = _make_envelope(priv, _policy(key_id, generation=2), td, "rec2")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                assert_active(e2, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path=str(state_path))
            self.assertEqual(ctx.exception.reason, "active_mismatch")

    def test_state_file_must_be_absolute_and_regular(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub = _ed_keys(td)
            key_id = _derive_key_id(pub)
            # Relative path denies
            env = _make_envelope(priv, _policy(key_id, generation=1), td, "rel")
            with self.assertRaises(PolicyActivationRejected) as ctx:
                activate(env, public_key_path=str(pub), expected_audience=_AUDIENCE, now=_NOW, state_path="relative/path.json")
            self.assertEqual(ctx.exception.reason, "state_path_invalid")
            # Symlink denies when loading
            state_path = Path(td) / "activation.json"
            initialize_activation_state(str(state_path), audience=_AUDIENCE, key_id=key_id, minimum_generation=1, now=_NOW)
            link = Path(td) / "link.json"
            link.symlink_to(state_path)
            with self.assertRaises(PolicyActivationRejected) as ctx:
                _ = load_activation_state(str(link))
            self.assertEqual(ctx.exception.reason, "state_corrupt")



    def test_state_reads_require_trusted_owner_on_opened_descriptor(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, _, path = self._setup(td)
            expected = path.stat()
            real_fstat = os.fstat

            def untrusted_owner(fd):
                info = real_fstat(fd)
                if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                    uid = max(os.geteuid(), 0) + 1
                    return SimpleNamespace(st_uid=uid, st_mode=info.st_mode, st_size=info.st_size)
                return info

            with patch("os.fstat", side_effect=untrusted_owner):
                with self.assertRaises(PolicyActivationRejected) as denied:
                    load_activation_state(path)
                self.assertEqual(denied.exception.reason, "state_corrupt")
                self.assertFalse(is_active(path, now=_NOW))

    def test_state_reads_open_once_with_nofollow_and_check_open_descriptor(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, _, path = self._setup(td)
            with patch("os.open", wraps=os.open) as opened:
                self.assertEqual(load_activation_state(path)["generation"], 0)
            state_opens = [call for call in opened.call_args_list if Path(call.args[0]).name == path.name]
            self.assertEqual(len(state_opens), 1)
            flags = state_opens[0].args[1]
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
            self.assertEqual(flags & (os.O_NOFOLLOW | os.O_CLOEXEC), os.O_NOFOLLOW | os.O_CLOEXEC)
            link = path.with_name("state-link")
            link.symlink_to(path)
            with self.assertRaises(PolicyActivationRejected) as denied:
                load_activation_state(link)
            self.assertEqual(denied.exception.reason, "state_corrupt")

    def test_activation_and_initialization_contend_on_same_lock_file(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, path = self._setup(td)
            env = _make_envelope(priv, _policy(key_id), td, "locked")
            options = dict(public_key_path=pub, expected_audience=_AUDIENCE, now=_NOW, state_path=path)
            lock = path.with_name(f".{path.name}.lock")
            fd = os.open(lock, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    activate(env, **options)
                with self.assertRaises(BlockingIOError):
                    initialize_activation_state(path, audience=_AUDIENCE, key_id=key_id, now=_NOW)
                self.assertEqual(load_activation_state(path)["generation"], 0)
            finally:
                os.close(fd)
            self.assertEqual(activate(env, **options)["generation"], 1)

    def test_higher_generation_deny_all_supersedes_previous_policy(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, path = self._setup(td)
            permit = _policy(key_id)
            permit["baseline"]["shared_evidence_sha256"] = _PUBLISHED_SHA
            allow = _make_envelope(priv, permit, td, "allow")
            binding = SimpleNamespace(
                execution_profile="external_process", transport_kind="cli_argv",
                operation_binding={"kind": "cli_subcommand"}, grants=("grant.exec.external_process",),
                perception_required="none", slot_id="fixture-slot", installation_ref="installation:fixture-slot@1",
                implementation_sha256="d" * 64, manifest_digest="e" * 64,
                shared_schema_ref=permit["baseline"]["shared_evidence_ref"], shared_schema_sha256=_PUBLISHED_SHA,
            )
            options = dict(public_key_path=pub, expected_audience=_AUDIENCE, now=_NOW, state_path=path)
            activate(allow, **options)
            self.assertEqual(policy_covers(assert_active(allow, return_policy=True, **options), binding, "record"),
                             (True, "covered"))
            deny = dict(permit, generation=2, scope=[])
            deny["revocation"] = dict(permit["revocation"], revoked_generations=[1])
            deny_all = _make_envelope(priv, deny, td, "deny-all")
            committed = activate(deny_all, **options)
            self.assertEqual(committed["generation"], 2)
            self.assertEqual(committed["revoked_generations"], [1])
            self.assertEqual(activate(deny_all, **options), committed)
            active = assert_active(deny_all, return_policy=True, **options)
            for operation in ("record", "replay", "compare"):
                self.assertFalse(policy_covers(active, binding, operation)[0])
            with self.assertRaises(PolicyActivationRejected) as rollback:
                activate(allow, **options)
            self.assertEqual(rollback.exception.reason, "generation_rollback")
            with self.assertRaises(PolicyActivationRejected) as inactive:
                assert_active(allow, **options)
            self.assertEqual(inactive.exception.reason, "active_mismatch")

    def test_revocations_accumulate_and_previously_revoked_generation_cannot_activate(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, path = self._setup(td)
            options = dict(public_key_path=pub, expected_audience=_AUDIENCE, now=_NOW, state_path=path)
            first = _policy(key_id)
            first["revocation"]["revoked_generations"] = [3, 5]
            activate(_make_envelope(priv, first, td, "first"), **options)
            second = _policy(key_id, generation=2)
            second["revocation"]["revoked_generations"] = [5, 7]
            env2 = _make_envelope(priv, second, td, "second")
            state = activate(env2, **options)
            self.assertEqual(state["revoked_generations"], [3, 5, 7])
            self.assertEqual(assert_active(env2, **options), state)
            revoked = _make_envelope(priv, _policy(key_id, generation=3), td, "revoked-generation")
            with self.assertRaises(PolicyActivationRejected) as denied:
                activate(revoked, **options)
            self.assertEqual(denied.exception.reason, "generation_revoked")
            self.assertEqual(load_activation_state(path), state)
            self.assertTrue(is_active(path, now=_NOW))

    def test_state_only_check_refuses_revoked_committed_generation(self):
        with tempfile.TemporaryDirectory() as td:
            priv, pub, key_id, path = self._setup(td)
            options = dict(public_key_path=pub, expected_audience=_AUDIENCE, now=_NOW, state_path=path)
            env = _make_envelope(priv, _policy(key_id), td, "state-revoked")
            state = activate(env, **options)
            path.write_text(json.dumps(dict(state, revoked_generations=[state["generation"]])))
            self.assertFalse(is_active(path, now=_NOW))
            for check in (activate, assert_active):
                with self.assertRaises(PolicyActivationRejected) as denied:
                    check(env, **options)
                self.assertEqual(denied.exception.reason, "generation_revoked")
