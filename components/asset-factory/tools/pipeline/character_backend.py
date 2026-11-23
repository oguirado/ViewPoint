"""Adapt registered character fixture stages to the immutable pipeline cache."""
from pathlib import Path

from asset_factory import core

from .character_stages import CALIBRATION, CHARACTER_STAGES, DOMAINS, REGISTRATION, SCOPE, CharacterStages
from .contracts import REF
from .engine import STAGES, authenticate, file_ref, load, require, validate_previous


def _check_reference(ref):
    core.validate(ref, REF)
    # Match CharacterStages' additional basename refusals before engine
    # authentication, which reads files to compute their digests.
    require(Path(ref["path"]).name not in {".netrc", ".npmrc"}, 2, "sensitive_path_refused")


def _registered_refs(value):
    if isinstance(value, dict):
        if set(value) == {"path", "sha256"}:
            _check_reference(value)
            yield value
        else:
            for child in value.values():
                yield from _registered_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _registered_refs(child)


class _CharacterStep(CharacterStages):
    def resume(self, prefix):
        # The engine has authenticated the committed prefix, including cache
        # hits. CharacterStages rechecks these same records before and after
        # the native evaluator. Its new stage is written under engine scratch.
        self._rows = [{"stage": name, "rc": 0, "receipt": ref}
                      for name, ref in zip(CHARACTER_STAGES, prefix)]
        return self.step(prefix[-1] if prefix else self.export)


class CharacterBackend:
    """Compose NativeStages with the fixture-only character steps.

    The caller pins the registration and scoped FACE/runtime toolchain and
    selects the character plan on Pipeline explicitly.
    """

    scope = SCOPE

    def __init__(self, native, *, registration, toolchain_sha256, face_runner):
        _check_reference(registration)
        self.native = native
        self.registration = dict(registration)
        require(type(toolchain_sha256) is str and len(toolchain_sha256) == 64
                and all(char in "0123456789abcdef" for char in toolchain_sha256),
                2, "character_toolchain_digest_invalid")
        self.toolchain = toolchain_sha256
        self.face_runner = face_runner
        self._prefix = ()

    def bind_output(self, root):
        self.root = root
        if hasattr(self.native, "bind_output"):
            self.native.bind_output(root)

    def bind_raw(self, subject):
        if hasattr(self.native, "bind_raw"):
            self.native.bind_raw(subject)

    def verify_sources(self):
        self.native.verify_sources()
        authenticate(self.registration)

    def bind_prefix(self, name, refs):
        if name in CHARACTER_STAGES:
            self._prefix = refs

    def verify_cached(self, name, directory, record):
        if name not in CHARACTER_STAGES and hasattr(self.native, "verify_cached"):
            self.native.verify_cached(name, directory, record)

    def _export(self, name, subject):
        index = STAGES.index("export")
        step = CHARACTER_STAGES.index(name)
        require(len(self._prefix) == index + 1 + step, 3, "character_prefix_missing")
        export_ref = self._prefix[index]
        export = validate_previous(export_ref, self.root)
        candidate = next((ref for ref in export["outputs"] if ref["path"] == export["subject"]), None)
        require(export["stage"] == "export" and candidate is not None, 4, "export_candidate_stale")
        if name == "face":
            require(subject is not None and candidate == file_ref(subject), 4, "export_candidate_stale")
        return export_ref, candidate, self._prefix[index + 1:]

    def identity(self, name, subject):
        if name not in CHARACTER_STAGES:
            return self.native.identity(name, subject)
        export, candidate, preceding = self._export(name, subject)
        authenticate(self.registration)
        plan = load(self.registration["path"], expected_sha256=self.registration["sha256"])
        require(type(plan) is dict and plan.get("schema") == REGISTRATION,
                2, "character_registration_schema")
        require(plan.get("candidate_sha256") == candidate["sha256"], 4, "registered_candidate_stale")
        refs = [candidate, self.registration, export]
        refs.extend(_registered_refs(plan.get(name)))
        if name == "act":
            refs.extend(_registered_refs(plan.get("skin")))
            refs.extend(validate_previous(preceding[-1], self.root)["outputs"])
        params = {"scope": SCOPE, "native_subject_scope": DOMAINS[name],
                  "calibration_state": CALIBRATION, "production_verdict": "CANNOT_SEE"}
        return list({(ref["path"], ref["sha256"]): ref for ref in refs}.values()), params, self.toolchain

    def execute(self, name, directory, subject):
        if name not in CHARACTER_STAGES:
            return self.native.execute(name, directory, subject)
        inputs, params, pin = self.identity(name, subject)
        export, candidate, prefix = self._export(name, subject)
        runner = _CharacterStep(directory / "character", candidate=candidate, export_receipt=export,
                                registration=self.registration, toolchain_sha256=pin,
                                face_runner=self.face_runner)
        row = runner.resume(prefix)
        ref = row["receipt"]
        record = load(ref["path"], expected_sha256=ref["sha256"])
        if record["rc"]:
            return record
        require(record["stage"] == name and record["parameters"] == params
                and record["toolchain_sha256"] == pin and all(ref in inputs for ref in record["inputs"]),
                4, "character_stage_identity_stale")

        # The native wrapper refers to scratch paths. Seal its authenticated
        # outputs and evidence in the engine receipt using their final paths.
        def relative(item):
            authenticate(item, inside=directory)
            return str(Path(item["path"]).relative_to(directory))

        outputs = [relative(item) for item in record["outputs"]]
        evidence = [relative(item) for item in record["evidence"]]
        Path(ref["path"]).unlink()
        return {"rc": 0, "outputs": outputs, "evidence": evidence,
                "subject": str(Path(record["subject"]).relative_to(directory)) if record["subject"] else None,
                "checks_evaluated": record["checks_evaluated"]}
