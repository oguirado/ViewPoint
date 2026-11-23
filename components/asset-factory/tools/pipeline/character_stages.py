"""Receipt-chained fixture dispatch after an authenticated pipeline export."""
import hashlib
import json
from pathlib import Path
import time

from asset_factory import core
from providers.unirig import gate as skin_gate
from tools.blender.bake_support import read_bytes
from tools.lipsync import build_track
from tools.lipsync.witness import check_timing
from tools.motion import replay as motion
from tools.physgate import replay as physics

from . import animated_check
from .contracts import REF, RUN, validate_stage
from .engine import MAX_JSON, fault, file_ref, require

CHARACTER_STAGES = ("face", "skin", "act", "voice", "phys")
SCOPE = "character-stage-fixture-replay"
CALIBRATION = "TARGET_UNCALIBRATED"
REGISTRATION = "af.character-fixture-registration/v1"
DOMAINS = {
    "face": "independent_procedural_head",
    "skin": "exported_skin",
    "act": "exported_skin_and_registered_action",
    "voice": "independent_audio_timing",
    "phys": "independent_box_replay",
}


def _is_sha(pin):
    return type(pin) is str and len(pin) == 64 and all(char in "0123456789abcdef" for char in pin)


def _read(ref, reason):
    core.validate(ref, REF)
    require(_is_sha(ref["sha256"]), 2, "input_digest_invalid")
    path = Path(ref["path"])
    require(path.is_absolute() and path.resolve() == path, 2, "noncanonical_reference")
    require(not (path.name in {"auth.json", "operator.token", "launch.token", ".netrc", ".npmrc"}
                 or path.name.startswith(".env") or path.suffix == ".key"
                 or ".gemini" in path.parts), 2, "sensitive_path_refused")
    raw = read_bytes(path)
    require(hashlib.sha256(raw).hexdigest() == ref["sha256"], 4, reason)
    return raw


def _json(raw, *, canonical=True):
    require(len(raw) <= MAX_JSON, 6, "json_byte_budget")
    value = json.loads(raw, object_pairs_hook=core._unique_object,
                       parse_constant=core._reject_number, parse_float=core._reject_number)
    require(type(value) is dict and (not canonical or core.canonical_json(value).encode("utf-8") == raw),
            2, "character_record_noncanonical")
    return value


def _stage_record(ref, reason):
    record = _json(_read(ref, reason))
    validate_stage(record)
    return record


class _NativeRefusal(Exception):
    def __init__(self, rc, reasons):
        self.rc = rc
        self.reasons = reasons


class _Attempt:
    def __init__(self, directory, inputs):
        self.directory = directory
        self.inputs = inputs
        self.evidence = []
        self.outputs = []
        self.subject = None
        self.checks = 0

    def read(self, ref, reason):
        data = _read(ref, reason)
        if ref not in self.inputs:
            self.inputs.append(ref)
        return data

    def write(self, name, value):
        path = self.directory / name
        raw = value if type(value) is bytes else core.canonical_json(value).encode("utf-8")
        # The parent is an exclusive attempt directory. Existing files are evidence of conflict.
        with path.open("xb") as stream:
            stream.write(raw)
        return file_ref(path)

    def native(self, receipt, *, schema, name="native.json", reasons="reason_codes"):
        require(type(receipt) is dict and receipt.get("schema") == schema, 2, "native_receipt_schema")
        self.evidence.append(self.write(name, receipt))
        rc = receipt.get("rc")
        require(type(rc) is int and rc in core.RC, 2, "native_receipt_rc")
        self.checks += 1
        if rc:
            codes = receipt.get(reasons, [])
            raise _NativeRefusal(rc, codes if codes else ["native_fixture_refused"])


class CharacterStages:
    """Run a fresh diagnostic chain with caller-pinned registration and export refs.

    The face runner owns scoped Blender execution. It receives the pinned FACE
    arguments and an exclusive output directory, reopens head.blend through the
    accepted FACE evaluator and returns that evaluator's receipt.
    """

    def __init__(self, directory, *, candidate, export_receipt, registration, toolchain_sha256,
                 face_runner):
        require(_is_sha(toolchain_sha256), 2, "character_toolchain_digest_invalid")
        self.directory = Path(directory)
        require(self.directory.is_absolute() and self.directory.resolve() == self.directory,
                2, "noncanonical_attempt_directory")
        for ref in (candidate, export_receipt, registration):
            core.validate(ref, REF)
            require(_is_sha(ref["sha256"]) and bool(ref["path"]), 2, "input_reference_invalid")
        self.directory.mkdir(exist_ok=False)
        self.candidate = dict(candidate)
        self.export = dict(export_receipt)
        self.registration = dict(registration)
        self.toolchain = toolchain_sha256
        self.face_runner = face_runner
        self._rows = []
        self._checks_evaluated = 0
        self._last_reasons = []
        self._started = time.monotonic_ns()

    def _prefix(self, previous):
        expected = self._rows[-1]["receipt"] if self._rows else self.export
        require(previous == expected, 4, "previous_receipt_stale")
        prefix = _stage_record(self.export, "receipt_bytes_stale")
        require(prefix["stage"] == "export" and prefix["subject"] == self.candidate["path"]
                and self.candidate in prefix["outputs"], 4, "export_candidate_stale")
        require(prefix["rc"] == 0, prefix["rc"] or 2, "export_not_admitted")
        prior = self.export
        for row in self._rows:
            prefix = _stage_record(row["receipt"], "receipt_bytes_stale")
            require(prefix["rc"] == 0 and prefix["stage"] == row["stage"] and prefix["previous"] == prior
                    and self.candidate in prefix["inputs"] and self.registration in prefix["inputs"],
                    4, "receipt_chain_stale")
            for ref in prefix["inputs"] + prefix["outputs"] + prefix["evidence"]:
                _read(ref, "prefix_evidence_stale")
            prior = row["receipt"]
        return prefix

    def step(self, previous):
        """Consume the current predecessor's exact reference and seal one stage."""
        index = len(self._rows)
        require(index < len(CHARACTER_STAGES) and (not self._rows or self._rows[-1]["rc"] == 0),
                3, "character_chain_stopped")
        stage = CHARACTER_STAGES[index]
        # Seal the refusal against the authorized predecessor even when an alien reference was supplied.
        expected = self._rows[-1]["receipt"] if self._rows else self.export
        directory = self.directory / stage
        directory.mkdir(exist_ok=False)
        attempt = _Attempt(directory, [self.candidate, self.registration])
        start = time.monotonic_ns()
        rc, reasons = 0, []
        try:
            prior = self._prefix(previous)
            candidate = attempt.read(self.candidate, "exported_glb_stale")
            plan = _json(attempt.read(self.registration, "character_registration_stale"))
            require(plan.get("schema") == REGISTRATION, 2, "character_registration_schema")
            require(plan.get("candidate_sha256") == self.candidate["sha256"], 4, "registered_candidate_stale")
            require(type(plan.get(stage)) is dict, 3, "character_fixture_missing")
            if stage == "act":
                self._act(attempt, plan, candidate, prior)
            elif stage == "skin":
                self._skin(attempt, plan["skin"], candidate)
            else:
                evaluator = {"face": self._face, "voice": self._voice, "phys": self._phys}[stage]
                evaluator(attempt, plan[stage])
            self._prefix(previous)
            for ref in attempt.inputs:
                _read(ref, "stage_input_stale")
        except _NativeRefusal as exc:
            rc, reasons = exc.rc, exc.reasons
        except MemoryError:
            rc, reasons = 6, ["character_memory_budget"]
        except Exception as exc:
            rc, reason = fault(exc)
            reasons = [reason]

        parameters = {"scope": SCOPE, "native_subject_scope": DOMAINS[stage],
                      "calibration_state": CALIBRATION, "production_verdict": "CANNOT_SEE"}
        payload = {
            "schema": "af.pipeline-stage/v1", "stage": stage,
            "key": core.sha256_json([stage, expected, attempt.inputs, parameters, self.toolchain]),
            "rc": rc, "status": core.STATUS[rc], "verdict": core.VERDICT[rc],
            "inputs": attempt.inputs, "previous": expected, "parameters": parameters,
            "toolchain_sha256": self.toolchain, "outputs": attempt.outputs if rc == 0 else [],
            "evidence": attempt.evidence, "subject": attempt.subject if rc == 0 else None,
            "checks_evaluated": attempt.checks, "wall_ms": (time.monotonic_ns() - start) // 1_000_000,
            "peak_rss_bytes": None, "absence_reasons": {"peak_rss_bytes": "stage_peak_unmeasured"},
            "reason_codes": reasons,
        }
        validate_stage(payload)
        row = {"stage": stage, "rc": rc, "verdict": core.VERDICT[rc], "cache_hit": False,
               "receipt": attempt.write("stage.json", payload)}
        self._rows.append(row)
        self._checks_evaluated += attempt.checks
        self._last_reasons = reasons
        return {**row, "receipt": dict(row["receipt"])}

    def run(self):
        while len(self._rows) < len(CHARACTER_STAGES) and (not self._rows or self._rows[-1]["rc"] == 0):
            previous = self._rows[-1]["receipt"] if self._rows else self.export
            self.step(previous)
        rows = [{**row, "receipt": dict(row["receipt"])} for row in self._rows]
        rc = rows[-1]["rc"]
        reasons = list(self._last_reasons)
        if rc == 0:
            try:
                self._prefix(rows[-1]["receipt"])
                rc, reasons = 3, [CALIBRATION]
            except Exception as exc:
                rc, reason = fault(exc)
                reasons = [reason]
        result = {
            "schema": "af.pipeline-run/v1", "scope": SCOPE, "rc": rc, "status": core.STATUS[rc],
            "verdict": core.VERDICT[rc], "run_key": core.sha256_json([row["receipt"] for row in rows]),
            "stages": rows, "checks_evaluated": self._checks_evaluated,
            "wall_ms": (time.monotonic_ns() - self._started) // 1_000_000,
            "artifact": None, "reason_codes": reasons,
        }
        core.validate(result, RUN)
        run_path = self.directory / "run.json"
        with run_path.open("xb") as stream:
            stream.write(core.canonical_json(result).encode("utf-8"))
        return result

    def _face(self, attempt, plan):
        args = _json(attempt.read(plan["args"], "face_args_stale"))
        require(callable(self.face_runner), 3, "face_runtime_unavailable")
        receipt = self.face_runner(args, attempt.directory)
        attempt.native(receipt, schema="af.face-fit/v1", reasons="reasons")
        scene = file_ref(attempt.directory / "head.blend")
        require(receipt.get("fixture_verdict") == "PASS" and receipt.get("character_verdict") == "CANNOT_SEE"
                and receipt.get("scope") == "procedural_fixture", 3, "face_fixture_scope_unavailable")
        require(receipt.get("args") == args and receipt.get("scene_sha256") == scene["sha256"],
                4, "face_receipt_subject_stale")
        attempt.outputs.append(scene)

    def _skin(self, attempt, plan, candidate):
        anatomy = attempt.read(plan["anatomy"], "skin_anatomy_stale")
        policy = _json(attempt.read(plan["policy"], "skin_policy_stale"), canonical=False)
        registered = _json(attempt.read(plan["registered_receipt"], "skin_registered_receipt_stale"))
        require(registered.get("rigged_sha256") == self.candidate["sha256"], 4, "skin_receipt_rig_stale")
        pin = "sha256:" + plan["anatomy"]["sha256"]
        receipt = skin_gate.inspect_skin_bytes(candidate, policy=policy, fixture_digest=pin,
            read_checked=lambda role, expected: anatomy if (role, expected) == ("skin-fixture", pin) else None)
        attempt.native(receipt, schema="af.skin-gate/v1")
        self._match_skin(receipt, registered)
        attempt.outputs.append(attempt.evidence[-1])

    @staticmethod
    def _match_skin(fresh, registered):
        require({key: value for key, value in fresh.items() if key != "wall_ns"}
                == {key: value for key, value in registered.items() if key != "wall_ns"},
                4, "skin_replay_receipt_stale")

    def _act(self, attempt, plan, candidate, previous):
        skin, act = plan["skin"], plan["act"]
        anatomy = attempt.read(skin["anatomy"], "skin_anatomy_stale")
        registered = attempt.read(skin["registered_receipt"], "skin_registered_receipt_stale")
        fresh = _json(attempt.read(previous["outputs"][0], "skin_replay_receipt_stale"))
        self._match_skin(fresh, _json(registered))
        registration = _json(attempt.read(act["registration"], "action_registration_stale"))
        support = attempt.read(act["support"], "action_support_stale")
        clip = attempt.read(act["clip"], "action_glb_stale")
        receipt, trace = motion.evaluate(
            clip, candidate, anatomy, registered, registration, act["registration"]["sha256"],
            act["fps"], support_data=support,
        )
        attempt.native(receipt, schema="af.motion-receipt/v1")
        require(receipt.get("production_verdict") == "CANNOT_SEE"
                and receipt.get("calibration_state") == CALIBRATION, 3, "action_fixture_scope_unavailable")
        require(trace is not None and core.sha256_json(trace) == receipt.get("trace_sha256")
                and receipt.get("action_sha256") == act["clip"]["sha256"], 4, "action_receipt_stale")
        animated = attempt.write("animated.glb", clip)
        checked = animated_check.check_animated(
            animated["path"], expected_sha256=act["clip"]["sha256"], skin_path=self.candidate["path"],
            skin_sha256=self.candidate["sha256"], animation_name=registration["name"],
            required_channels=registration["required_channels"],
        )
        attempt.native(checked, schema="af.animated-glb-check/v1", name="animated-check.json")
        attempt.outputs.extend((animated, attempt.write("trace.json", trace)))
        attempt.subject = animated["path"]

    def _voice(self, attempt, plan):
        audio = attempt.read(plan["audio"], "voice_audio_stale")
        alignment = attempt.read(plan["alignment"], "voice_alignment_stale")
        witness = _json(attempt.read(plan["witness"], "voice_witness_stale"))
        track = build_track(audio, alignment, fps=plan["fps"], silence_peak_pcm=plan["silence_peak_pcm"],
                            max_frames=plan["max_frames"])
        receipt = check_timing(track, audio, witness, onset_limit_frames=plan["onset_limit_frames"],
                               bridge_samples=plan["bridge_samples"], silence_peak_pcm=plan["silence_peak_pcm"])
        attempt.native(receipt, schema="af.voice-witness-receipt/v1")
        require(receipt.get("production_verdict") == "CANNOT_SEE", 3, "voice_fixture_scope_unavailable")
        attempt.outputs.append(attempt.write("track.json", track))

    def _phys(self, attempt, plan):
        proxy = attempt.read(plan["proxy"], "physics_proxy_stale")
        registration = _json(attempt.read(plan["registration"], "physics_registration_stale"))
        support = attempt.read(plan["support"], "physics_support_stale")
        collider = _json(attempt.read(plan["collider"], "physics_collider_stale"))
        receipt, evidence = physics.run_fixture(
            render_glb=proxy, registration=registration, registration_sha256=plan["registration"]["sha256"],
            support_data=support, collider=collider, support_height_um=plan["support_height_um"],
            gravity_um_s2=plan["gravity_um_s2"],
        )
        attempt.native(receipt, schema="af.physics-receipt/v1")
        require(receipt.get("production_verdict") == "CANNOT_SEE"
                and receipt.get("threshold_status") == CALIBRATION, 3, "physics_fixture_scope_unavailable")
        traces = evidence["traces"]
        require(traces and [core.sha256_json(trace) for trace in traces] == receipt.get("trace_sha256s")
                and receipt.get("render_sha256") == plan["proxy"]["sha256"], 4, "physics_receipt_stale")
        attempt.outputs.extend(attempt.write(f"trace-{index}.json", trace) for index, trace in enumerate(traces))
        for index, model in enumerate(evidence["models"]):
            attempt.evidence.append(attempt.write(f"model-{index}.xml", model.encode("utf-8")))
        for name in ("scene", "protocol", "kit_receipt"):
            attempt.evidence.append(attempt.write(name + ".json", evidence[name]))
