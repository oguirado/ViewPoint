# SPDX-License-Identifier: AGPL-3.0-only
"""U10 replay lineage: fail-closed validation before launch (B18/C27).

Each test exercises the production handler/dispatch entry point with a
test-owned tape resolver. Refusal tests use a marker fixture: a launch
would create the marker, so absence proves zero spawn.
"""
import hashlib
import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from slot_host.runtime.binding import sha256_of_file
from slot_host.runtime.replay_lineage import ReplayLineageError, resolve_replay_lineage
from slot_host.runtime.tools import _ControlError, main, register_installation
from slot_host.runtime.tests.binding_fixtures import (
    FixtureLaunch as _AllowLiveAuthority,
    execute_fixture,
    fixture_retention,
    dispatch_fixture as dispatch,
    replay_fixture as handle_replay,
)

setUpModule = fixture_retention


_ADAPTER = "d" * 64
_PROVIDER_SHA = "a" * 64
_REQ_DIGEST = "b" * 64
_RES_DIGEST = "c" * 64


def _manifest(impl_sha, *, profile="external_process"):
    def _op(oid, role):
        return {
            "id": oid,
            "role": role,
            "input_ref": f"urn:rea.slot_request:v2#slot={role}",
            "output_ref": f"urn:rea.slot_result:v2#slot={role}",
            "binding": {"kind": "cli_subcommand", "argv": [oid], "typed_args": []},
            "required_effects": ["process.spawn:child"],
            "required_grants": ["grant.exec.external_process"],
            "limits": {"timeout_s": 5, "max_output_bytes": 4096},
            "retry_semantics": {"mode": "at_most_once", "on_interrupt": "UNCERTAIN_OUTCOME"},
        }

    ops = [_op("record", "replay"), _op("replay", "replay"), _op("compare", "verify")]
    return {
        "schema": "rea.slot_manifest/v2",
        "slot_id": "fixture-slot",
        "status": "registered",
        "upstream": {"name": "fixture", "source_url": "https://example.com/fixture", "version": "1.0.0", "licence_id": "MIT", "licence_evidence_refs": ["fixture: licence"], "component_notices": [], "user_installed": True},
        "provider_ref": {"schema": "rea.provider/v2", "provider_id": "fixture-slot", "provider_revision": "1.0.0", "provider_sha256": _PROVIDER_SHA, "implementation_sha256": impl_sha},
        "contract_refs": [
            {"role": "replay", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ_DIGEST, "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES_DIGEST, "execution_profile": profile},
            {"role": "verify", "request_schema_ref": "urn:rea.slot_request:v2", "request_digest": _REQ_DIGEST, "result_schema_ref": "urn:rea.slot_result:v2", "result_digest": _RES_DIGEST, "execution_profile": profile},
        ],
        "transport": {"kind": "cli_argv", "driver_location": "outside_capsule", "executable_binding": "user_selected", "argv_prefix": [], "framing": "none", "cwd_policy": "run_scratch", "env_allowlist": ["PATH"]},
        "operations": ops,
        "capabilities": {"target_kinds": ["fixture_target"], "platforms": ["linux"], "prerequisite_probes": [], "optional_features": [], "adapter_identity": {"adapter_id": "fixture.v1", "adapter_sha256": _ADAPTER, "provenance": "host_authored"}},
        "perception": {"required_level": "none", "optional_levels": [], "observation_sources": [{"id": "tool-evidence", "level": "none", "producer": "tool_stdio", "outside_capsule": True}]},
        "record_replay": {"input_codec": "json", "output_codec": "json", "observable_channels": ["error"], "state_domain": "per_case", "effects": ["process.spawn:child"], "determinism_modes": ["recorded_inputs"], "freshness_inputs": ["fixture"]},
        "comparison": {"comparator_id": "fixture.compare", "version": "1", "policy_ref": "policy:fixture@1", "coverage_requirements": {"channel": "error"}, "tolerance_units": "none", "waiver_policy": {"source": "host_policy", "binds": ["input", "actual", "reason", "scope", "policy_version"], "stale": "fail"}},
        "evidence": {"shared_schema_ref": "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2", "required_rea_fields": ["episode", "observation", "slot"], "artifact_roles": ["evidence_bundle"], "retention": "evidence_store_attempt", "visibility": "redacted_projection"},
        "conformance": {"fixture_refs": ["fixture:1"], "negative_control_refs": ["control:1"], "supported_entry_points": ["cli", "mcp", "json"], "required_checks": ["admit-and-project", "outside-record-replay-compare", "perception-and-pool", "transport-parity-and-retry"]},
        "discovery": {"summary": "fixture", "search_terms": ["fixture"], "operation_descriptions": {"record": "record", "replay": "replay", "compare": "compare"}, "examples": []},
    }


def _write_marker(path: Path, marker: Path):
    script = (
        b"#!/usr/bin/env python3\n"
        b"# SPDX-License-Identifier: AGPL-3.0-only\n"
        b"import sys, json, pathlib\n"
        b"raw = sys.stdin.buffer.read(8192)\n"
        b"with pathlib.Path(" + repr(str(marker)).encode() + b").open('a') as marker:\n"
        b"    echo = {'argv': sys.argv[1:], 'replay': json.loads(raw) if raw else None}\n"
        b"    marker.write(json.dumps(echo) + '\\n')\n"
        b"obj={\"schema\":\"rea.slot_result/v1\",\"status\":\"OK\",\"rc\":0,\"sha256\":\"e\"+\"f\"*63}\n"
        b"sys.stdout.write(json.dumps(obj)+'\\n')\n"
        b"sys.stdout.flush()\n"
    )
    path.write_bytes(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return sha256_of_file(path)


_TAPE = hashlib.sha256(b"tape-u10").hexdigest()
_PARENT = "ep-parent-u10-test"


def _resolver_for(*shas, parent=_PARENT):
    retained = set(shas)

    def _resolve(episode_id, sha=None):
        if sha is None:
            # Exercise the pre-step tape-only lookup in the negative control.
            sha, episode_id = episode_id, parent
        if episode_id == parent and sha in retained:
            return {"parent_episode_id": parent, "input_tape_sha256": sha}
        return None

    return _resolve


class ReplayLineageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.marker = self.dir / "launched.txt"
        self.exe = self.dir / "fixture-replay.py"
        impl = _write_marker(self.exe, self.marker)
        self.ref = "installation:fixture-slot@u10replay"
        self.manifest = _manifest(impl)
        register_installation(self.ref, self.manifest, str(self.exe))

    def _policy(self, grants=None, **extra):
        pol = {"grants": list(grants) if grants is not None else ["grant.exec.external_process"]}
        try:
            from slot_host.runtime.tools import _INSTALLATIONS

            last = list(_INSTALLATIONS.values())[-1]["manifest"]

            def _reg(m):
                c = next(r for r in m["contract_refs"] if r["role"] == "replay")
                pr = m["provider_ref"]
                ad = m["capabilities"]["adapter_identity"]["adapter_sha256"]
                return {"provider_sha256": pr["provider_sha256"], "provider_revision": pr["provider_revision"], "implementation_sha256": pr["implementation_sha256"], "adapter_sha256": ad, "request_digest": c["request_digest"], "result_digest": c["result_digest"], "request_schema_ref": c["request_schema_ref"], "result_schema_ref": c["result_schema_ref"], "execution_profile": c["execution_profile"]}

            pol["registry"] = _reg(last)
            pol["evidence_ref"] = "evidence-store:EVIDENCE_RECEIPT_SCHEMA@v0.2"
            pol["spawn_granted"] = True
            pol["licence_disposition"] = "external_execution"
            from slot_host.projections.schema import digest as _jcs_digest

            pol["manifest_digest"] = _jcs_digest(last)
            pol["slot_id"] = "fixture-slot"
            pol["probes"] = {}
        except Exception:
            pass
        pol.update(extra)
        return pol

    def _payload(self, **over):
        payload = {
            "installation_ref": self.ref,
            "operation_id": "replay",
            "argv": [],
            "host_grants": ["grant.exec.external_process"],
            "parent_episode_id": _PARENT,
            "tape_sha256": _TAPE,
            "replay": {"input_tape_sha256": _TAPE, "determinism_mode": "seeded", "seed": 7},
        }
        payload.update(over)
        return payload

    def test_valid_seeded_replay_reaches_child_with_block(self):
        request = {"request_id": "req-u10-seeded-ok-001", "payload": self._payload()}
        reply = dispatch(
            "behaviour.replay", request,
            host_policy=self._policy(),
            live_authority=_AllowLiveAuthority(),
            tape_resolver=_resolver_for(_TAPE),
        )
        self.assertTrue(reply["ok"])
        expected = {"input_tape_sha256": _TAPE, "determinism_mode": "seeded", "seed": 7}
        echo = {"argv": ["replay"], "replay": {"parent_episode_id": _PARENT, **expected}}
        self.assertEqual(json.loads(self.marker.read_text()), echo)
        receipt = reply["result"]["receipt"]
        self.assertEqual(receipt["episode"]["parent_episode_id"], _PARENT)
        self.assertEqual(receipt["replay"], expected)
        refused = handle_replay(
            self._payload(argv=["replay"]),
            request_id="req-u10-seeded-extra-001",
            host_policy=self._policy(),
            live_authority=_AllowLiveAuthority(),
            tape_resolver=_resolver_for(_TAPE),
        )
        self.assertIsNone(refused["pid"])
        self.assertEqual(json.loads(self.marker.read_text()), echo)

    def test_valid_unseeded_modes_reach_child_with_block(self):
        modes = {
            "distribution": {"sample_count": 3},
            "exact": {"controls": {"clock": "controlled", "rng": "not_applicable",
                                   "external_effects": "uncontrolled"}},
        }
        for mode, options in modes.items():
            with self.subTest(mode=mode):
                self.marker.unlink(missing_ok=True)
                replay = {"input_tape_sha256": _TAPE, "determinism_mode": mode, **options}
                result = handle_replay(self._payload(replay=replay), request_id=f"req-u10-{mode}-stdin",
                                       host_policy=self._policy(), live_authority=_AllowLiveAuthority(),
                                       tape_resolver=_resolver_for(_TAPE))
                self.assertEqual(json.loads(self.marker.read_text()), {
                    "argv": ["replay"], "replay": {"parent_episode_id": _PARENT, **replay},
                })
                self.assertEqual(result["receipt"]["replay"], replay)

    @unittest.skip("owner-pending: T04")
    def test_owner_retained_parent_and_tape_reach_replay_child(self):
        # Supply owner-enrolled authority and the authenticated retention resolver when available.
        payload = self._payload()
        reply = dispatch("behaviour.replay", {"request_id": "req-u10-owner-t04", "payload": payload},
                         host_policy=self._policy())
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["receipt"]["episode"]["parent_episode_id"], _PARENT)
        self.assertEqual(json.loads(self.marker.read_text())["replay"],
                         {"parent_episode_id": _PARENT, **payload["replay"]})

    @unittest.skip("owner-pending: T06")
    def test_owner_unseeded_replay_receipt_resolves_with_approved_projection(self):
        # Supply the owner-approved unseeded projection and receipt readback when available.
        replay = {"input_tape_sha256": _TAPE, "determinism_mode": "distribution", "sample_count": 3}
        reply = dispatch("behaviour.replay", {"request_id": "req-u10-owner-t06",
                                              "payload": self._payload(replay=replay)},
                         host_policy=self._policy())
        self.assertTrue(reply["ok"])
        receipt = reply["result"]["receipt"]
        self.assertEqual(receipt["replay"], replay)
        adapter = receipt["shared_receipt_adapter"]
        self.assertEqual(adapter["binding_state"], "bound")
        self.assertIs(adapter["live_use_permitted"], True)
        self.assertTrue(adapter["shared_receipt_ref"])

    def test_cached_replay_requires_current_retained_tape(self):
        policy = self._policy()
        request = {"request_id": "req-u10-cache-tape-001", "payload": self._payload()}
        options = {"host_policy": policy, "live_authority": _AllowLiveAuthority()}
        retained = _resolver_for(_TAPE)
        self.assertTrue(dispatch("behaviour.replay", request, tape_resolver=retained, **options)["ok"])
        self.assertTrue(self.marker.exists())
        marker = self.marker.read_bytes()
        self.assertTrue(dispatch("behaviour.replay", request, tape_resolver=retained, **options)["ok"])
        for resolver in (_resolver_for(), _resolver_for(_TAPE, parent="ep-different-parent")):
            with self.assertRaises(_ControlError) as denied:
                dispatch("behaviour.replay", request, tape_resolver=resolver, **options)
            self.assertEqual(denied.exception.code, "SCHEMA_INVALID")
        self.assertEqual(self.marker.read_bytes(), marker)

    def test_retention_requires_matching_parent_and_tape_record(self):
        for record in (True, {"input_tape_sha256": _TAPE},
                       {"parent_episode_id": "ep-other", "input_tape_sha256": _TAPE}):
            with self.subTest(record=record):
                with self.assertRaises(ReplayLineageError):
                    handle_replay(self._payload(), request_id="req-u10-unverified-parent",
                                  host_policy=self._policy(), live_authority=_AllowLiveAuthority(),
                                  tape_resolver=lambda *_: record)
                self.assertFalse(self.marker.exists())

    def test_host_tape_pin_cannot_hide_disagreeing_caller_tape(self):
        with self.assertRaises(ReplayLineageError):
            handle_replay(self._payload(tape_sha256="f" * 64), request_id="req-u10-host-tape",
                          host_policy=self._policy(), live_authority=_AllowLiveAuthority(),
                          tape_sha256=_TAPE, tape_resolver=_resolver_for(_TAPE))
        self.assertFalse(self.marker.exists())

    def test_missing_parent_refused_no_spawn(self):
        payload = self._payload()
        del payload["parent_episode_id"]
        with self.assertRaises(ReplayLineageError):
            handle_replay(payload, request_id="req-u10-noparent-001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=_resolver_for(_TAPE))
        self.assertFalse(self.marker.exists())

    def test_tape_disagreement_refused_no_spawn(self):
        payload = self._payload(tape_sha256="f" * 64)
        with self.assertRaises(ReplayLineageError):
            handle_replay(payload, request_id="req-u10-mismatch-001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=_resolver_for(_TAPE, "f" * 64))
        self.assertFalse(self.marker.exists())

    def test_unsupported_mode_refused_no_spawn(self):
        payload = self._payload(replay={"input_tape_sha256": _TAPE, "determinism_mode": "bogus"})
        with self.assertRaises(ReplayLineageError):
            handle_replay(payload, request_id="req-u10-bogus-000001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=_resolver_for(_TAPE))
        self.assertFalse(self.marker.exists())

    def test_exact_missing_controls_refused_no_spawn(self):
        payload = self._payload(replay={"input_tape_sha256": _TAPE, "determinism_mode": "exact", "controls": {"clock": "controlled"}})
        with self.assertRaises(ReplayLineageError):
            handle_replay(payload, request_id="req-u10-exact-00001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=_resolver_for(_TAPE))
        self.assertFalse(self.marker.exists())

    def test_seeded_missing_seed_refused_no_spawn(self):
        payload = self._payload(replay={"input_tape_sha256": _TAPE, "determinism_mode": "seeded"})
        with self.assertRaises(ReplayLineageError):
            handle_replay(payload, request_id="req-u10-seed-000001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=_resolver_for(_TAPE))
        self.assertFalse(self.marker.exists())

    def test_unretained_tape_refused_no_spawn(self):
        with self.assertRaises(ReplayLineageError):
            handle_replay(self._payload(), request_id="req-u10-unret-000001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=_resolver_for())
        self.assertFalse(self.marker.exists())

    def test_missing_resolver_refused_owner_pending(self):
        with self.assertRaisesRegex((RuntimeError, ReplayLineageError), "owner-pending: T04") as denied:
            handle_replay(self._payload(), request_id="req-u10-nores-000001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=None)
        self.assertIsInstance(denied.exception, RuntimeError)
        self.assertFalse(self.marker.exists())

    def test_execute_requires_replay_retention_before_opening_attempt(self):
        retained = []
        with self.assertRaisesRegex(RuntimeError, "owner-pending: T04"):
            execute_fixture(self.manifest, str(self.exe), [], operation_id="replay",
                            host_grants=["grant.exec.external_process"],
                            parent_episode_id=_PARENT, tape_sha256=_TAPE,
                            live_authority=_AllowLiveAuthority(), attempt_store=retained.append)
        self.assertEqual(retained, [])
        self.assertFalse(self.marker.exists())

    def test_missing_tape_refused_no_spawn(self):
        payload = self._payload()
        del payload["tape_sha256"]
        payload["replay"] = {"determinism_mode": "uncontrolled"}
        # Replay block without a tape digest is itself invalid.
        with self.assertRaises(ReplayLineageError):
            handle_replay(payload, request_id="req-u10-notape-000001", host=None, host_policy=self._policy(), live_authority=_AllowLiveAuthority(), tape_resolver=_resolver_for(_TAPE))
        self.assertFalse(self.marker.exists())

    def test_dry_run_replay_missing_lineage_refused(self):
        payload = self._payload()
        del payload["parent_episode_id"]
        with self.assertRaises(_ControlError) as ctx:
            dispatch(
                "behaviour.replay",
                {"request_id": "req-u10-dryrun-0001", "dry_run": True, "payload": payload},
                host=None,
                host_policy=self._policy(),
                live_authority=_AllowLiveAuthority(),
                tape_resolver=_resolver_for(_TAPE),
            )
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertFalse(self.marker.exists())

    def test_dispatch_replay_missing_lineage_refused_no_spawn(self):
        payload = self._payload()
        del payload["parent_episode_id"]
        with self.assertRaises(_ControlError) as denied:
            dispatch(
                "behaviour.replay",
                {"request_id": "req-u10-dispatch-001", "payload": payload},
                host=None,
                host_policy=self._policy(),
                live_authority=_AllowLiveAuthority(),
                tape_resolver=_resolver_for(_TAPE),
            )
        self.assertEqual(denied.exception.code, "SCHEMA_INVALID")
        with self.assertRaises(_ControlError) as parked:
            dispatch("behaviour.replay", {"request_id": "req-u10-dispatch-002", "payload": self._payload()},
                     host_policy=self._policy(), live_authority=_AllowLiveAuthority())
        self.assertEqual(parked.exception.code, "AVAILABILITY_PARKED")
        self.assertFalse(self.marker.exists())

    def test_preview_and_live_replay_share_control_codes_and_cli_exits(self):
        def outage(*_):
            raise OSError("retention is offline")

        cases = (
            ("no-resolver", self._payload(), None, "AVAILABILITY_PARKED", 16),
            ("outage", self._payload(), outage, "AVAILABILITY_PARKED", 16),
            ("invalid", self._payload(parent_episode_id=""), None, "SCHEMA_INVALID", 14),
            ("bad-control", self._payload(replay={"input_tape_sha256": _TAPE, "determinism_mode": "exact",
                                                  "controls": {"clock": [], "rng": "controlled",
                                                               "external_effects": "controlled"}}),
             None, "SCHEMA_INVALID", 14),
        )
        for dry_run in (True, False):
            for case, payload, resolver, code, cli_exit in cases:
                with self.subTest(dry_run=dry_run, case=case):
                    request = {"request_id": f"req-u10-ctrl-{case}", "dry_run": dry_run, "payload": payload}

                    def dispatch_call(tool_id, arguments):
                        return dispatch(tool_id, arguments, host_policy=self._policy(),
                                        live_authority=_AllowLiveAuthority(), tape_resolver=resolver)

                    with self.assertRaises(_ControlError) as denied:
                        dispatch_call("behaviour.replay", request)
                    self.assertEqual(denied.exception.code, code)
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(main(["mini", "behaviour.replay", "--args", json.dumps(request)],
                                              dispatch_fn=dispatch_call), cli_exit)
                    self.assertFalse(self.marker.exists())

    def test_live_replay_refuses_retention_loss_before_attempt_open(self):
        for error_code in ("AVAILABILITY_PARKED", "SCHEMA_INVALID"):
            with self.subTest(error_code=error_code):
                retained = []
                probes = iter((_resolver_for(_TAPE)(_PARENT, _TAPE), None))

                def resolve(*_):
                    witness = next(probes)
                    if witness is None and error_code == "AVAILABILITY_PARKED":
                        raise OSError("retention is offline")
                    return witness

                with self.assertRaises(_ControlError) as denied:
                    dispatch("behaviour.replay", {"request_id": f"req-u10-loss-{error_code}",
                                                  "payload": self._payload()},
                             host_policy=self._policy(), live_authority=_AllowLiveAuthority(),
                             tape_resolver=resolve, attempt_store=retained.append)
                self.assertEqual(denied.exception.code, error_code)
                self.assertEqual(retained, [])
                self.assertFalse(self.marker.exists())

    def test_resolve_replay_lineage_tape_only_forwards_uncontrolled(self):
        resolved = resolve_replay_lineage(
            {"parent_episode_id": _PARENT, "tape_sha256": _TAPE},
            tape_resolver=_resolver_for(_TAPE),
        )
        self.assertEqual(resolved["parent_episode_id"], _PARENT)
        self.assertEqual(resolved["replay"]["determinism_mode"], "uncontrolled")
        self.assertEqual(resolved["tape_sha256"], _TAPE)


if __name__ == "__main__":
    unittest.main()
