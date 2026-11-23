"""Adapters to the landed F0/F1/F2 APIs; missing acceptance data refuses.

An image service, independent appearance review and vqa3d trace are explicit
inputs. This module never manufactures a reviewer, a profile trace or RAM proof.
The geometry policy is explicit; no caller component inventory is inferred.
"""
import asyncio
import hashlib
from pathlib import Path
import shutil

from asset_factory import core, contracts
from .engine import Refusal, authenticate, file_ref, load, require, owned_directory
from . import topology_bridge, bundle
from tools.blender import bake, bake_support, glb_io, topology, uv
from tools.blender.bake_support import read_bytes
from tools.mesh import api as mesh, certificate
from tools.input.evidence import is_digest
from tools.registry import _disk
from gates import input_gate, vqa3d_bridge
from gates.appearance_gate import evaluate as appearance
from providers.trellis2 import adapter

ROOT = Path(__file__).resolve().parents[2]
SIBLING = ROOT.parent / "blender-visual-qa"
GEOMETRY_STAGES = ("mesh", "topology", "uv_bake_orm", "export")
GEOMETRY_RECEIPT = "geometry-regression-v2.json"


def save(path, value):
    core.atomic_write_json(path, value)


def success(subject, evidence, outputs=None, checks=1):
    return {"rc": 0, "subject": subject, "outputs": outputs or [subject],
            "evidence": evidence, "checks_evaluated": checks}


def checked(record, *, declared_rc=None):
    require(type(record) is dict and type(record.get("rc")) is int
            and record["rc"] in core.RC, 2, "stage_contract_invalid")
    rc = record["rc"]
    if declared_rc is not None:
        require(type(declared_rc) is int and declared_rc == rc, 2, "stage_rc_disagrees")
    if "status" in record:
        require(record["status"] == core.STATUS[rc], 2, "stage_status_invalid")
    if "verdict" in record:
        require(record["verdict"] == core.VERDICT[rc], 2, "stage_verdict_invalid")
    reasons = record.get("reason_codes") or record.get("failed")
    if not reasons and isinstance(record.get("unavailable"), dict):
        reasons = list(record["unavailable"].values())
    if rc:
        refusal = Refusal(rc, str((reasons or ["stage_refused"])[0]))
        refusal.evidence_record = record
        raise refusal
    return record


class NativeStages:
    scope = "asset-pipeline"

    def __init__(self, *, source, reference, provider, options, service=None, scheduler_backend=None):
        require(provider in ("byo", "trellis2"), 2, "provider_invalid")
        self.source, self.reference = Path(source).absolute(), Path(reference).absolute()
        self.provider, self.options, self.service = provider, options, service
        self.scheduler_backend = scheduler_backend
        self.source_ref, self.reference_ref = file_ref(self.source), file_ref(self.reference)
        self.raw_ref = self.source_ref if provider == "byo" else None
        require(read_bytes(self.reference).startswith(b"\x89PNG\r\n\x1a\n"), 2, "reference_png_required")
        self.geometry_inputs = {}
        self.bake_inputs = {}
        self.stage_pins = {}
        self.policies = self._policy_files()
        self.policy_refs = {name: file_ref(path) for name, path in self.policies.items()}
        self.qualification_refs = ()

    def _policy_files(self):
        keys = ("mesh_policy", "input_policy", "factory_config", "territory_policy",
                "provider_manifest", "trace", "appearance_request", "appearance_review")
        return {key: Path(self.options[key]).absolute() for key in keys if self.options.get(key)}

    def bind_output(self, root):
        self.output_root = root

    def bind_raw(self, subject):
        """Called after authenticating the provider receipt, including cache hits."""
        raw = file_ref(subject)
        if self.provider == "byo":
            require(raw["sha256"] == self.source_ref["sha256"], 4, "raw_source_stale")
        self.raw_ref = raw

    def verify_sources(self):
        for ref in [self.source_ref, self.reference_ref, *self.policy_refs.values(),
                    *self.qualification_refs,
                    *self.bake_inputs.values(),
                    *([self.raw_ref] if self.raw_ref else [])]:
            authenticate(ref)
        for ref in self.geometry_inputs.values():
            authenticate(ref)
        for name, pin in self.stage_pins.items():
            require(self._pin(name) == pin, 4, "upstream_toolchain_stale")

    def _pin(self, name):
        # Keys pin algorithms as well as data. Imported sibling implementations
        # are hashed in place, never copied or modified.
        paths = {Path(__file__), Path(core.__file__), Path(contracts.__file__),
                 Path(__file__).with_name("engine.py"), Path(__file__).with_name("contracts.py")}
        paths.update(Path(__file__).parent.glob("*.py"))
        paths.update((ROOT / "slots").glob("*.schema.json"))
        if name == "input":
            paths.update((ROOT / "tools/input").glob("*.py"))
            paths.add(ROOT / "asset_factory/domains.py")
        modules = {"input": [input_gate],
                   "provider": [glb_io] if self.provider == "byo" else [adapter], "mesh": [mesh],
                   "topology": [topology, mesh], "uv_bake_orm": [bake, uv],
                   "export": [glb_io, uv, mesh], "appearance": [appearance, vqa3d_bridge],
                   "vqa3d": [vqa3d_bridge]}[name]
        for module in modules:
            paths.update(Path(module.__file__).parent.glob("*.py"))
            paths.update(Path(module.__file__).parent.glob("*.schema.json"))
        if name in ("provider", "appearance"):
            paths.update((ROOT / "licence").glob("*.py"))
            paths.update((ROOT / "licence").glob("*.schema.json"))
            paths.update((ROOT / "config").glob("*.schema.json"))
        if name == "provider":
            paths.update((ROOT / "tools/scheduler").glob("*.py"))
            paths.update((ROOT / "tools/scheduler/schemas").glob("*.schema.json"))
        if name in ("mesh", "topology", "uv_bake_orm", "export"):
            paths.update((SIBLING / "blender_visual_qa" / "assets").glob("*.py"))
        if name in GEOMETRY_STAGES:
            for module in (certificate, certificate.domains, certificate.math_acceptance,
                           certificate.raw_admission, _disk):
                paths.add(Path(module.__file__))
            paths.update((ROOT / "tools/mesh").glob("*.py"))
            paths.update((ROOT / "tools/input").glob("*.py"))
            paths.update((ROOT / "schemas").glob("*.schema.json"))
        refs = {str(p.relative_to(ROOT.parent)): core.sha256_file(p) for p in sorted(paths)}
        if name in ("topology", "uv_bake_orm"):
            executable = shutil.which("blender")
            refs["runtime/blender"] = core.sha256_file(Path(executable)) if executable else None
        return core.sha256_json(refs)

    def identity(self, name, subject):
        keys = {"input": ("input_policy",) if self.provider == "trellis2" else (),
                "provider": ("factory_config", "territory_policy", "provider_manifest") if self.provider == "trellis2" else (),
                "mesh": ("mesh_policy",), "topology": ("mesh_policy",),
                "uv_bake_orm": ("mesh_policy",), "export": ("mesh_policy",),
                "appearance": ("trace", "appearance_request", "appearance_review", "factory_config",
                               "territory_policy", "provider_manifest"), "vqa3d": ("trace",)}[name]
        refs = [self.policy_refs[key] for key in keys if key in self.policy_refs]
        if name == "provider" and self.provider == "trellis2" and self.options.get("qualification") is not None:
            self._qualification_record()
            refs.extend(self.qualification_refs)
        if name in ("mesh", "topology", "uv_bake_orm", "export", "appearance", "vqa3d"):
            require(self.raw_ref is not None, 3, "retained_raw_absent")
            authenticate(self.raw_ref)
            refs.append(self.raw_ref)
        if name == "provider" or (name == "input" and self.provider == "trellis2"):
            refs.append(self.source_ref)
        if self._requires_quad_cage(name):
            refs.append(self._topology_cage_input())
        if subject is not None:
            refs.append(file_ref(subject))
        if name == "appearance":
            refs.append(self.reference_ref)
            if "appearance_request" in self.policies:
                request = load(self.policies["appearance_request"])
                self._appearance_admission(request)
                root = Path(request["evidence_root"]).absolute()
                def bound(item):
                    path = root / item["path"]
                    require(not Path(item["path"]).is_absolute() and ".." not in Path(item["path"]).parts,
                            2, "appearance_evidence_escape")
                    ref = file_ref(path)
                    require(ref["sha256"] == item["sha256"], 4, "appearance_evidence_stale")
                    refs.append(ref)
                    return path
                for key in ("asset", "reference"):
                    bound(request[key])
                manifest = load(bound(request["render_manifest"]))
                for frame in manifest["frames"].values():
                    bound(frame)
        parameters = {"provider": self.provider}
        for key in {"provider": ("timeout_s", "seed", "pipeline_type"),
                    "mesh": ("timeout_s",), "topology": ("timeout_s", "method", "target_faces", "seed"),
                    "uv_bake_orm": ("timeout_s", "texture_resolution", "max_stretch"),
                    "export": ("timeout_s",), "appearance": ("profile", "profile_sha256", "required_checks"),
                    "vqa3d": ("profile", "profile_sha256", "required_checks")}.get(name, ()):
            parameters[key] = self.options.get(key)
        if name == "uv_bake_orm":
            high, low = self._bake_sources(subject)
            look_scope, look_refs = self._bake_look_scope()
            refs.extend(look_refs)
            parameters["bake_sources"] = {"high_sha256": high["sha256"], "low_sha256": low["sha256"]}
            if look_scope is not None:
                parameters["look_witness_digest"] = look_scope["witness_digest"]
                parameters["production_verdict"] = "CANNOT_SEE"
                parameters["calibration_state"] = "factory_manifest_schema_unassigned"
        pin = self._pin(name)
        require(self.stage_pins.setdefault(name, pin) == pin, 4, "upstream_toolchain_stale")
        return refs, parameters, pin

    def execute(self, name, directory, subject):
        if name in GEOMETRY_STAGES:
            self._geometry_scope(name)
        result = getattr(self, "stage_" + name)(directory, subject)
        if name in ("mesh", "topology", "uv_bake_orm", "export") and result.get("rc") == 0:
            require(self.raw_ref is not None, 3, "retained_raw_absent")
            authenticate(self.raw_ref)
            raw = Path(self.raw_ref["path"])
            candidate_name = result.get("subject")
            require(type(candidate_name) is str and not Path(candidate_name).is_absolute()
                    and ".." not in Path(candidate_name).parts, 2, "stage_path_escape")
            candidate, policy = directory / candidate_name, self._mesh_policy()
            rc, receipt = mesh.compare_meshes(raw, candidate, policy=policy)
            topology_bridge.verify_regression(rc, receipt, raw, candidate, policy)
            authenticate(self.raw_ref)
            save(directory / "raw-regression.json", receipt)
            result["evidence"] = [*result["evidence"], "raw-regression.json"]
            proof = self._geometry_proof(name, candidate, topology_input=subject)
            save(directory / GEOMETRY_RECEIPT, proof)
            result["evidence"].append(GEOMETRY_RECEIPT)
            result["checks_evaluated"] += 2
            if self._requires_quad_cage(name):
                result["evidence"].append(candidate.with_suffix(".cage.json").relative_to(directory).as_posix())
                result["checks_evaluated"] += 1
        return result

    def _mesh_policy(self):
        require("mesh_policy" in self.policies, 3, "mesh_policy_absent")
        policy = load(self.policies["mesh_policy"], expected_sha256=self.policy_refs["mesh_policy"]["sha256"])
        policy.pop("geometry_scope", None)
        return policy

    def _geometry_scope(self, name):
        require("mesh_policy" in self.policy_refs, 3, "mesh_policy_absent")
        policy = load(self.policies["mesh_policy"], expected_sha256=self.policy_refs["mesh_policy"]["sha256"])
        scopes = policy.get("geometry_scope")
        require(isinstance(scopes, dict) and name in scopes, 3, "geometry_scope_missing")
        scope = scopes[name]
        required = {"domain_digest", "registration_digests", "profile_digest", "stage_digest"}
        require(type(scope) is dict and scope.keys() == required, 2, "geometry_scope_invalid")
        registrations = scope["registration_digests"]
        require(isinstance(registrations, list) and len(registrations) == 2 and
                all(is_digest(pin) for pin in [*registrations, scope["domain_digest"],
                                               scope["profile_digest"], scope["stage_digest"]]),
                2, "geometry_scope_pin_invalid")
        require(isinstance(scopes.get("authority"), str), 3, "geometry_authority_missing")
        root = Path(scopes["authority"])
        require(root.is_absolute() and root.resolve() == root and root.is_dir()
                and not root.is_relative_to(self.output_root), 2, "geometry_authority_invalid")
        return root, scope

    def _geometry_reader(self, root):
        def read_checked(role, pin):
            require(role.replace("-", "").isalpha() and is_digest(pin), 2, "geometry_record_invalid")
            with _disk.root_directory(root / role) as directory:
                with _disk.regular(directory, pin[7:]) as stream:
                    data = stream.read(certificate.record_limit(role) + 1)
            require(len(data) <= certificate.record_limit(role), 6, "geometry_record_budget")
            ref = {"path": str(root / role / pin[7:]), "sha256": pin[7:]}
            self.geometry_inputs[ref["path"]] = ref
            return data

        return read_checked

    def _requires_quad_cage(self, name):
        return name == "topology" and self.options.get("method") == "quadriflow"

    def _topology_cage_input(self):
        # The exported cage does not exist when the cache key is computed.
        # Bind the authority's expected bytes, which GEO later checks against the export.
        root, scope = self._geometry_scope("topology")
        read_checked = self._geometry_reader(root)
        registration = certificate.checked_record(read_checked, "geo-registration",
                                                   scope["registration_digests"][-1])
        pin = registration.get("quad_cage")
        require(pin is not None, 3, "required_quad_cage_missing")
        certificate.checked_record(read_checked, "geo-quad-cage", pin)
        return self.geometry_inputs[str(root / "geo-quad-cage" / pin[7:])]

    def _geometry_proof(self, name, candidate, *, topology_input=None, topology_receipt_sha=None):
        root, scope = self._geometry_scope(name)
        proof = topology.certify_chain(self.raw_ref["path"], [candidate],
                                       read_checked=self._geometry_reader(root), **scope)
        checked(proof)
        expected = ["sha256:" + self.raw_ref["sha256"], "sha256:" + file_ref(candidate)["sha256"]]
        require(proof.get("schema") == "af.mesh-regression/v2" and proof.get("subjects") == expected
                and proof.get("measurement_rc") == 0, 4, "geometry_subject_stale")
        gate = proof["base"]["gate"]
        require(gate["rc"] == 0 and gate["verdict"] == "PASS" and gate["subject_digest"] == expected[-1]
                and gate["receipt_digest"] == scope["stage_digest"]
                and gate["profile_digest"] == scope["profile_digest"], 4, "geometry_qualification_stale")
        if self._requires_quad_cage(name):
            receipt = load(candidate.parent.parent / "topology.json", expected_sha256=topology_receipt_sha)
            topology_bridge.verify_quad_cage(topology_input, candidate, receipt,
                                             target_faces=self.options["target_faces"], geometry_proof=proof)
        return proof

    def verify_cached(self, name, directory, record):
        if name == "uv_bake_orm":
            sources = record["parameters"]["bake_sources"]
            self._verify_bake_sources(directory / "bake", sources["high_sha256"], sources["low_sha256"])
            look_scope, _ = self._bake_look_scope()
            if look_scope is not None:
                receipt = load(directory / "bake.json")
                replay = self._verify_bake_look(directory / "bake", receipt, look_scope)
                require(file_ref(directory / "look-verification.json")["sha256"] == core.sha256_json(replay),
                        4, "look_verification_stale")
        if name in GEOMETRY_STAGES:
            path = directory / GEOMETRY_RECEIPT
            refs = [ref for ref in record["evidence"] if ref["path"] == str(path)]
            require(len(refs) == 1, 3, "geometry_certificate_missing")
            candidate = Path(record["subject"])
            kwargs = {}
            if self._requires_quad_cage(name):
                evidence = {ref["path"]: ref["sha256"] for ref in record["evidence"]}
                require(str(candidate.with_suffix(".cage.json")) in evidence, 3, "required_quad_cage_missing")
                receipt_sha = evidence.get(str(directory / "topology.json"))
                require(receipt_sha is not None, 3, "topology_receipt_missing")
                # identity() keeps the immediate topology input last.
                kwargs = {"topology_input": record["inputs"][-1]["path"], "topology_receipt_sha": receipt_sha}
            proof = self._geometry_proof(name, candidate, **kwargs)
            require(refs[0]["sha256"] == core.sha256_json(proof), 4, "geometry_certificate_stale")

    def stage_input(self, directory, subject):
        if self.provider == "byo":
            save(directory / "intake.json", {"schema": "af.byo-entry/v1", "rc": 0,
                                          "scope": "image_gate_not_applicable_to_glb"})
            return success(None, ["intake.json"], ["intake.json"])
        require("input_policy" in self.policies, 3, "input_policy_absent")
        policy_path = self.policies["input_policy"]
        policy_sha = self.policy_refs["input_policy"]["sha256"]
        try:
            policy = load(policy_path, expected_sha256=policy_sha)
        except Refusal as exc:
            if exc.rc == 4:
                raise
            policy = None
        except (ValueError, core.ContractError):
            # Keep the legacy gate's own policy diagnostics.
            policy = None
        if type(policy) is dict and policy.get("schema") == "af.input-evidence/v1":
            from gates.input_d18 import inspect_matte
            from tools.input.evidence import bytes_digest, checked_record, verify_input_receipt

            require(policy.get("gate_id") == "input.matte", 2, "matte_receipt_invalid")
            checked({"rc": policy.get("source_rc"), "reason_codes": policy.get("reason_codes")})
            derivative_pin = "sha256:" + self.source_ref["sha256"]
            require(policy.get("subject_digest") == derivative_pin, 4, "matte_derivative_stale")
            intent_pin = policy.get("domain_digest")
            require(intent_pin is not None, 3, "matte_intent_digest_missing")

            witnesses = {}

            def read_checked(role, pin):
                require(role.replace("-", "").isalpha() and is_digest(pin),
                        2, "matte_witness_pin_invalid")
                name = Path("matte-witnesses") / role / pin[7:]
                if name not in witnesses:
                    try:
                        raw = read_bytes(policy_path.parent / role / pin[7:])
                    except bake_support.Refusal as exc:
                        if exc.rc == 3:
                            return None
                        raise
                    require(bytes_digest(raw) == pin, 4, "matte_witness_stale")
                    witnesses[name] = raw
                return witnesses[name]

            intent = checked_record(read_checked, "matte-intent", intent_pin)
            refs = policy.get("input_refs")
            require(type(refs) is list and len(refs) == 3, 3, "matte_input_refs_missing")
            require(refs[2] == derivative_pin, 4, "matte_derivative_stale")
            original = read_checked("original-rgb", intent.get("original_digest"))
            master = read_checked("straight-alpha", refs[1])
            mask = read_checked("approved-mask", intent.get("mask_digest"))
            require(all(raw is not None for raw in (original, master, mask)), 3, "matte_raster_witness_missing")
            submitted = read_bytes(self.source)
            require(bytes_digest(submitted) == derivative_pin, 4, "matte_derivative_stale")
            recomputed = inspect_matte(original, master, submitted, mask,
                                       intent_digest=intent_pin, read_checked=read_checked)
            checked({**recomputed, "rc": recomputed["source_rc"]})
            receipt_bytes = read_bytes(policy_path)
            require(bytes_digest(receipt_bytes) == "sha256:" + policy_sha, 4, "matte_receipt_stale")
            verify_input_receipt(receipt_bytes, recomputed)

            for name, raw in witnesses.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            save(directory / "intake.json", {
                "schema": "af.d18-matte-intake/v1", "rc": 0,
                "matte_receipt_sha256": policy_sha, "derivative_sha256": self.source_ref["sha256"],
                "intent_digest": intent_pin, "required_preprocess_image": False,
            })
            return success(None, ["intake.json", *(name.as_posix() for name in sorted(witnesses))],
                           ["intake.json"])

        rc = input_gate.run(self.source, directory / "input", policy_path=self.policies["input_policy"],
                            timeout_seconds=self.options["timeout_s"])
        require(rc == 0, rc, "input_gate_refused")
        # The input gate publishes an immutable, internally authenticated bundle.
        result = checked(input_gate.verify_receipt(directory / "input", expected_source_sha256=self.source_ref["sha256"],
            expected_policy_sha256=core.sha256_json(load(self.policies["input_policy"])),
            expected_toolchain_sha256=input_gate.toolchain_identity()))
        save(directory / "verification.json", result)
        return success(None, ["verification.json"], [p.relative_to(directory).as_posix()
                       for p in sorted((directory / "input").rglob("*")) if p.is_file()])

    def _qualification_record(self):
        from tools.qualification.manifest import SHA256

        path, digest = self.options["qualification"]
        path = Path(path).absolute()
        admission_ref = {"path": str(path), "sha256": digest}
        authenticate(admission_ref)
        admission = load(path, expected_sha256=digest)
        require(type(admission) is dict and admission.keys() == {
            "schema", "scope", "receipt", "candidate_sha256", "input_derivative_sha256",
        } and admission["schema"] == "af.pipeline-qualification/v1"
                and admission["scope"] in ("fixture", "production"), 2, "qualification_admission_invalid")
        receipt_ref = admission["receipt"]
        require(type(receipt_ref) is dict and receipt_ref.keys() == {"path", "sha256"}
                and type(receipt_ref["path"]) is str, 2, "qualification_receipt_ref_invalid")
        for pin in (receipt_ref["sha256"], admission["candidate_sha256"], admission["input_derivative_sha256"]):
            require(type(pin) is str and SHA256.fullmatch(pin) is not None, 2, "qualification_pin_invalid")
        refs = (admission_ref, receipt_ref)
        require(not self.qualification_refs or self.qualification_refs == refs, 4, "qualification_inputs_stale")
        self.qualification_refs = refs
        return admission

    def _provider_admission(self):
        from .engine import validate_previous
        from tools.qualification import verify_receipt
        from tools.qualification.harness import RECEIPT_SCHEMA

        if self.options.get("qualification") is None:
            # Preserve the pre-existing diagnostic for an unwired CLI entry point.
            reason = ("trellis2_service_not_built_or_transport_absent" if self.service is None
                      else "qualification_absent")
            raise Refusal(3, reason)
        admission = self._qualification_record()
        receipt_ref = admission["receipt"]
        authenticate(receipt_ref)
        receipt_path = Path(receipt_ref["path"])
        receipt = load(receipt_path, expected_sha256=receipt_ref["sha256"])
        require(type(receipt) is dict and receipt.get("schema") == RECEIPT_SCHEMA,
                2, "qualification_receipt_invalid")
        require(receipt.get("candidate_sha256") == admission["candidate_sha256"]
                and receipt.get("provider") == self.provider, 4, "qualification_candidate_stale")

        # The input stage has no mesh subject. Resolve its exact cache key so a
        # caller cannot substitute a loose intake record for the previous stage.
        refs, parameters, pin = self.identity("input", None)
        identity = {"stage": "input", "inputs": refs, "parameters": parameters,
                    "previous": None, "toolchain_sha256": pin}
        key = core.sha256_json(identity)
        intake_dir = self.output_root / "cache" / "input" / key
        entry = load(intake_dir / "entry.json")
        require(type(entry) is dict and entry.get("schema") == "af.pipeline-cache/v1"
                and entry.get("key") == key and type(entry.get("receipt")) is dict,
                4, "qualification_intake_stale")
        stage_ref = entry["receipt"]
        require(stage_ref.get("path") == str(intake_dir / "receipt.json"), 4, "qualification_intake_stale")
        stage = validate_previous(stage_ref, self.output_root)
        require(stage["key"] == key and all(stage[field] == value for field, value in identity.items()),
                4, "qualification_intake_stale")
        intake_path = str(intake_dir / "intake.json")
        intake_ref = next((ref for ref in stage["evidence"] if ref["path"] == intake_path), None)
        require(intake_ref is not None, 3, "qualification_d18_intake_missing")
        intake = load(intake_path, expected_sha256=intake_ref["sha256"])
        require(intake.get("schema") == "af.d18-matte-intake/v1" and intake.get("rc") == 0
                and intake.get("required_preprocess_image") is False,
                3, "qualification_d18_intake_missing")
        derivative = intake.get("derivative_sha256")
        require(derivative == self.source_ref["sha256"] == admission["input_derivative_sha256"]
                and intake.get("matte_receipt_sha256") == self.policy_refs["input_policy"]["sha256"],
                4, "qualification_derivative_stale")

        checked(receipt)
        fixture = admission["scope"] == "fixture"
        require((receipt.get("scope") == "fixture") == fixture, 5, "qualification_fixture_scope")
        if not fixture:
            require(receipt_path.name == "qualification.json", 2, "qualification_receipt_name_invalid")
            verified = verify_receipt(receipt_path.parent, expected_sha256=receipt_ref["sha256"])
            require("gpu_policy" in verified, 3, "qualification_gpu_evidence_missing")
            result_ref = verified["runs"][-1]["outputs"]["result.json"]
            measurement = load(receipt_path.parent / "output" / "result.json", expected_sha256=result_ref["sha256"])
            require(measurement.get("input_sha256") == derivative
                    and measurement.get("seed") == self.options.get("seed")
                    and measurement.get("pipeline_type") == self.options.get("pipeline_type")
                    and measurement.get("preprocess_image") is False, 4, "qualification_run_stale")
        require(not fixture or self.service is None, 5, "qualification_fixture_scope")
        if self.service is not None:
            return load(receipt_path.parent / "candidate.json", expected_sha256=admission["candidate_sha256"])

    def _bind_qualified_provider(self, candidate, provider):
        # QUAL's code revision is the upstream source revision. Its pinned
        # provider manifest carries the dispatched revision and weight repositories.
        manifest_sha = self.policy_refs["provider_manifest"]["sha256"]
        require(candidate["provider"] == provider["id"] == self.provider
                and any(ref["sha256"] == manifest_sha for ref in candidate["code"]["files"]),
                4, "qualification_provider_stale")
        manifest_files = [(Path(ref["path"]).parts, ref["sha256"])
                          for weight in provider["weights"] for ref in weight["files"]]
        remaining = set(manifest_files)
        require(bool(remaining) and len(remaining) == len(manifest_files), 4, "qualification_weights_stale")
        candidate_files = candidate["weights"] + [
            ref for component in candidate["conditioners"] for ref in component["files"]
        ]
        for ref in candidate_files:
            parts = Path(ref["path"]).parts
            matches = [pin for pin in manifest_files if pin[1] == ref["sha256"]
                       and parts[-len(pin[0]):] == pin[0]]
            require(len(matches) == 1 and matches[0] in remaining, 4, "qualification_weights_stale")
            remaining.remove(matches[0])
        require(not remaining, 4, "qualification_weights_stale")

    def stage_provider(self, directory, subject):
        if self.provider == "byo":
            data = read_bytes(self.source)
            # BYO uses the static asset boundary, including embedded materials.
            # TRELLIS's generated geometry subset remains provider-specific.
            glb_io.decode_glb(data)
            (directory / "raw.glb").write_bytes(data)
            request = {"schema": "af.topology/v1", "kind": "request", "slot": "topology",
                       "request_id": self.source_ref["sha256"], "provider_id": "byo",
                       "provider_revision": self._pin("provider"),
                       "inputs": [{"role": "raw_glb", "path": str(self.source),
                                   "media_type": "model/gltf-binary", "sha256": self.source_ref["sha256"]}],
                       "params": {}, "context": {"jurisdiction": "not_applicable",
                          "commercial_use": False, "policy_sha256": core.sha256_json({}),
                          "toolchain_sha256": self._pin("provider"), "timeout_s": self.options["timeout_s"]}}
            # Structural validation does not assert rights over caller BYO bytes.
            contracts.validate_slot(request, "topology", "request")
            save(directory / "custody.json", {"schema": "af.byo-custody/v1", "rc": 0,
                                             "input_sha256": self.source_ref["sha256"],
                                             "request": request, "licence": "caller_owned_not_evaluated"})
            return success("raw.glb", ["custody.json"])
        candidate = self._provider_admission()
        if self.service is None:
            raise Refusal(3, "trellis2_service_not_built_or_transport_absent")
        for key in ("factory_config", "territory_policy", "provider_manifest"):
            require(key in self.policies, 3, key + "_absent")
        from licence.check import check_provider
        factory, policy = (load(self.policies[key]) for key in ("factory_config", "territory_policy"))
        provider = load(self.policies["provider_manifest"],
                        expected_sha256=self.policy_refs["provider_manifest"]["sha256"])
        contracts.validate_provider(provider)
        self._bind_qualified_provider(candidate, provider)
        decision = check_provider(factory, provider, operation="generate", policy=policy)
        checked(decision)
        request = {"schema": "af.generate/v1", "kind": "request", "slot": "generate",
                   "request_id": self.source_ref["sha256"], "provider_id": provider["id"],
                   "provider_revision": provider["revision"],
                   "inputs": [{"role": "image", "path": str(self.source), "media_type": "image/png",
                               "sha256": self.source_ref["sha256"]}],
                   "params": {"seed": self.options["seed"], "pipeline_type": self.options["pipeline_type"]},
                   "context": {"jurisdiction": factory["jurisdiction"], "commercial_use": factory["commercial_use"],
                       "policy_sha256": core.sha256_json(policy), "toolchain_sha256": self._pin("provider"),
                       "timeout_s": self.options["timeout_s"]}}
        contracts.validate_slot(request, "generate", "request")
        # The service boundary requires its own scheduler admission proof. Never
        # bypass the F0 backend when only a transport has been supplied.
        require(self.scheduler_backend is not None, 3, "scheduler_backend_absent")
        from tools.scheduler import scheduler
        # A lease whose release cannot be proven must outlive disposable stage
        # scratch and prevent all subsequent request keys from starting a model.
        if hasattr(self, "output_root"):
            state = self.output_root / "state"
            owned_directory(state, self.output_root)
            lease = state / "trellis2.lease"
        else:
            lease = directory / "model.lease"
        sched_request = scheduler.build_request(request, provider, factory, decision, lease_path=lease)
        owner = self
        lifecycle = self.scheduler_backend
        class ScheduledAdapter:
            async def observe(self, handle, deadline_ns):
                return await lifecycle.observe(handle, deadline_ns)
            async def start(self, req, deadline_ns):
                return await lifecycle.start(req, deadline_ns)
            async def stop(self, handle, deadline_ns):
                return await lifecycle.stop(handle, deadline_ns)
            async def run(self, handle, req, deadline_ns):
                result = await adapter.generate(read_bytes(owner.source), seed=owner.options["seed"],
                    pipeline_type=owner.options["pipeline_type"], output_dir=directory / "generation",
                    request=request, provider=provider, decision=decision, factory=factory, policy=policy,
                    service=owner.service, timeout_s=owner.options["timeout_s"])
                contracts.validate_slot(result, "generate", "result")
                return {"rc": result["rc"], "status": result["status"],
                        "reason_codes": [result["absence_reasons"].get("outputs", "provider_refused")]
                                        if result["rc"] else [],
                        "peak_rss_bytes": result["metrics"]["peak_rss_bytes"]}
        receipt = asyncio.run(scheduler.execute(sched_request, ScheduledAdapter()))
        checked(receipt)
        source = directory / "generation"
        result = checked(load(source / "result.json"))
        contracts.validate_slot(result, "generate", "result")
        output = next((r for r in result["outputs"] if r["role"] == "mesh_glb"), None)
        require(output is not None, 3, "provider_glb_absent")
        path = source / output["path"]
        require(path.resolve().is_relative_to(source.resolve()), 2, "provider_path_escape")
        require(file_ref(path)["sha256"] == output["sha256"], 4, "provider_output_stale")
        adapter.validate_glb(read_bytes(path))
        (directory / "raw.glb").write_bytes(read_bytes(path))
        save(directory / "scheduler.json", receipt)
        save(directory / "provider.json", result)
        evidence = ["scheduler.json", "provider.json"]
        for ref in result["receipts"]:
            path = source / ref["path"]
            require(path.resolve().is_relative_to(source.resolve()), 2, "provider_receipt_escape")
            require(file_ref(path)["sha256"] == ref["sha256"], 4, "provider_receipt_stale")
            evidence.append(path.relative_to(directory).as_posix())
        return success("raw.glb", evidence)

    def stage_mesh(self, directory, subject):
        policy = self._mesh_policy()
        rc, receipt, data = mesh.repair_mesh(subject, policy=policy)
        # Validate refusals too; a successful tuple code alone is no evidence.
        if rc:
            topology_bridge.verify_regression(rc, receipt, subject, None, policy)
        require(data is not None, 3, "repair_artifact_absent")
        (directory / "repaired.glb").write_bytes(data)
        topology_bridge.verify_regression(rc, receipt, subject, directory / "repaired.glb", policy)
        rc, metrics = mesh.compare_meshes(subject, directory / "repaired.glb", policy=policy)
        topology_bridge.verify_regression(rc, metrics, subject, directory / "repaired.glb", policy)
        save(directory / "repair.json", receipt)
        save(directory / "metrics.json", metrics)
        return success("repaired.glb", ["repair.json", "metrics.json"], checks=2)

    def stage_topology(self, directory, subject):
        policy = self._mesh_policy()
        from tools.blender.topology_glb import inspect_glb
        info = inspect_glb(subject)
        method = self.options["method"]
        target = self.options.get("target_faces")
        require(target is not None or method == "identity", 2, "target_faces_required")
        target = info["faces"] if method == "identity" else target
        receipt = topology_bridge.run_candidate(subject, directory / "topology", method=method,
            target_faces=target, seed=self.options["seed"], policy=policy,
            timeout_s=self.options["timeout_s"])
        checked(receipt)
        save(directory / "topology.json", receipt)
        return success("topology/master.glb", ["topology.json"],
                       [p.relative_to(directory).as_posix() for p in sorted((directory / "topology").rglob("*")) if p.is_file()],
                       checks=len(receipt["checks"]))

    def _remember_bake_input(self, ref):
        require(self.bake_inputs.setdefault(ref["path"], ref) == ref, 4, "bake_input_stale")
        return ref

    def _bake_sources(self, subject):
        require(self.raw_ref is not None, 3, "retained_raw_absent")
        authenticate(self.raw_ref)
        require(subject is not None, 3, "bake_target_missing")
        return self._remember_bake_input(self.raw_ref), self._remember_bake_input(file_ref(subject))

    def _bake_look_scope(self):
        from tools.input.evidence import checked_record

        policy = (load(self.policies["mesh_policy"], expected_sha256=self.policy_refs["mesh_policy"]["sha256"])
                  if "mesh_policy" in self.policies else {})
        scopes = policy.get("geometry_scope", {})
        require(type(scopes) is dict, 2, "geometry_scope_invalid")
        if "look_scope" not in scopes:
            require(self.provider == "byo", 3, "look_scope_missing")
            return None, ()
        scope = scopes["look_scope"]
        require(type(scope) is dict and scope.keys() == {"witness_digest"}
                and is_digest(scope["witness_digest"]), 2, "look_scope_invalid")
        require(type(scopes.get("authority")) is str, 3, "look_authority_missing")
        root = Path(scopes["authority"])
        output = getattr(self, "output_root", None)
        require(root.is_absolute() and root.resolve() == root and root.is_dir()
                and (output is None or not root.is_relative_to(output)), 2, "look_authority_invalid")

        refs = {}

        def read_checked(role, pin):
            name = role.replace("-", "")
            require(name.isascii() and name.isalpha() and is_digest(pin), 2, "look_evidence_pin_invalid")
            path = root / role / pin[7:]
            data = read_bytes(path)
            require(hashlib.sha256(data).hexdigest() == pin[7:], 4, "look_evidence_stale")
            refs[role, pin] = self._remember_bake_input({"path": str(path), "sha256": pin[7:]})
            return data

        pin = scope["witness_digest"]
        witness = checked_record(read_checked, "look-bake-witness", pin)
        require(witness.get("schema") == "af.look-bake-witness/v1", 2, "look_witness_invalid")
        read_checked("uv-layout", witness.get("atlas_digest"))
        read_checked("uv-projection-layout", witness.get("projection_digest"))
        return {"witness_digest": pin, "read_checked": read_checked}, tuple(refs.values())

    def _verify_bake_sources(self, directory, high_sha, low_sha):
        require(file_ref(directory / "source_high.glb")["sha256"] == high_sha
                and file_ref(directory / "source_low.glb")["sha256"] == low_sha,
                4, "bake_inputs_stale")

    def _verify_bake_look(self, directory, receipt, look_scope):
        witness = look_scope["witness_digest"]
        record = load(directory / "bundle_receipt.json", expected_sha256=receipt["receipt_sha256"])
        require(record.get("scope") == receipt.get("scope") == "protected-look-fixture"
                and record.get("look_witness_digest") == witness, 4, "look_bundle_scope_stale")
        verification = checked(bake.verify_bundle(directory, expected_receipt_sha256=receipt["receipt_sha256"],
                                                 read_checked=look_scope["read_checked"]))
        require(verification.get("scope") == "protected-look-replay"
                and verification.get("production_evidence") is False, 4, "look_bundle_scope_stale")
        return {**verification, "look_witness_digest": witness}

    def stage_uv_bake_orm(self, directory, subject):
        high, low = self._bake_sources(subject)
        look_scope, _ = self._bake_look_scope()
        output = directory / "bake"
        options = {"res": self.options["texture_resolution"], "max_stretch": self.options["max_stretch"],
                   "timeout_s": self.options["timeout_s"]}
        if look_scope is not None:
            options.update(look_scope=look_scope, margin_px=2, blender_threads=1)
        receipt = bake.bake_bundle(high["path"], low["path"], output, **options)
        checked(receipt)
        self._verify_bake_sources(output, high["sha256"], low["sha256"])
        bundle_record = load(output / "bundle_receipt.json", expected_sha256=receipt["receipt_sha256"])
        require(bundle_record.get("runner", {}).get("kind") == "scoped-cpu"
                and receipt.get("runtime_measured") is True, 3, "injected_bake_runner")
        outputs = ["named_low.glb", *(p.relative_to(directory).as_posix()
                    for p in sorted(output.iterdir()) if p.is_file())]
        save(directory / "bake.json", receipt)
        if look_scope is not None:
            verified = self._verify_bake_look(output, receipt, look_scope)
            save(directory / "look-verification.json", verified)
            # The authenticated target retains its original atlas and node binding.
            # The protected textured GLB is retained in the bake bundle.
            (directory / "named_low.glb").write_bytes(read_bytes(output / "source_low.glb"))
            return success("named_low.glb", ["bake.json", "look-verification.json"], outputs,
                           checks=receipt["checks_evaluated"])
        verification = checked(bundle.verify_bundle(output,
            expected_receipt_sha256=receipt["receipt_sha256"],
            expected_high_sha256=high["sha256"], expected_low_sha256=low["sha256"],
            expected_policy={"res": self.options["texture_resolution"], "margin_px": 8,
                "cage_extrusion_um": 5000, "max_stretch_permille": self.options["max_stretch"],
                "timeout_s": self.options["timeout_s"]}, timeout_s=self.options["timeout_s"]))
        save(directory / "production-verification.json", verification)
        # The upstream bake worker renames its single object. Restore a proven
        # single-node correspondence so explicit component policies remain valid;
        # never infer or register a replacement component allowlist.
        original, _ = glb_io.decode_glb(read_bytes(subject))
        derived, binary = glb_io.decode_glb(read_bytes(directory / "bake/low_uv.glb"))
        source_nodes = [n for n in original["nodes"] if "mesh" in n]
        target_nodes = [n for n in derived["nodes"] if "mesh" in n]
        require(len(source_nodes) == len(target_nodes) == 1
                and bool(source_nodes[0].get("name")), 3, "bake_name_correspondence_unproven")
        previous_name = target_nodes[0].get("name")
        target_nodes[0]["name"] = source_nodes[0]["name"]
        named = directory / "named_low.glb"
        named.write_bytes(glb_io.encode_glb(derived, binary))
        rc, regression = mesh.compare_meshes(subject, named, policy=self._mesh_policy())
        topology_bridge.verify_regression(rc, regression, subject, named, self._mesh_policy())
        save(directory / "geometry-regression.json", regression)
        save(directory / "node-binding.json", {"schema": "af.bake-node-binding/v1", "rc": 0,
            "source_sha256": file_ref(subject)["sha256"], "derived_sha256": file_ref(named)["sha256"],
            "previous_name": previous_name, "source_name": source_nodes[0]["name"],
            "correspondence": "single_mesh_node_and_measured_raw_regression"})
        return success("named_low.glb", ["bake.json", "production-verification.json", "geometry-regression.json", "node-binding.json"],
                       outputs, checks=receipt["checks_evaluated"])

    def stage_export(self, directory, subject):
        # Assemble maps using the existing GLB encoder, then independently check
        # map identities, sampler semantics and geometry with existing gates.
        bundle = subject.parent / "bake"
        maps = {name: read_bytes(bundle / (name + ".png")) for name in ("albedo", "normal", "orm")}
        doc, binary = glb_io.decode_glb(read_bytes(subject))
        data = bytearray(binary)
        doc["images"], doc["textures"] = [], []
        sampler = {"magFilter": 9729, "minFilter": 9729, "wrapS": 10497, "wrapT": 10497}
        doc["samplers"] = [sampler]
        for name, raw in maps.items():
            data.extend(b"\0" * (-len(data) % 4))
            index = len(doc.setdefault("bufferViews", []))
            doc["bufferViews"].append({"buffer": 0, "byteOffset": len(data), "byteLength": len(raw)})
            data.extend(raw)
            doc["images"].append({"bufferView": index, "mimeType": "image/png", "name": name})
            doc["textures"].append({"source": len(doc["images"]) - 1, "sampler": 0})
        doc["materials"] = [{"name": "baked", "pbrMetallicRoughness": {
            "baseColorTexture": {"index": 0}, "metallicRoughnessTexture": {"index": 2}},
            "normalTexture": {"index": 1}, "occlusionTexture": {"index": 2}}]
        for mesh_doc in doc["meshes"]:
            for primitive in mesh_doc["primitives"]:
                primitive["material"] = 0
        data.extend(b"\0" * (-len(data) % 4))
        doc["buffers"] = [{"byteLength": len(data)}]
        candidate = directory / "asset.glb"
        candidate.write_bytes(glb_io.encode_glb(doc, bytes(data)))
        receipt = checked(uv.check_textured_glb(candidate,
            expected_maps={name: hashlib.sha256(raw).hexdigest() for name, raw in maps.items()},
            sampler_policy=sampler, expected_geometry=subject,
            expected_geometry_sha256=file_ref(subject)["sha256"]))
        rc, regression = mesh.compare_meshes(subject, candidate, policy=self._mesh_policy())
        topology_bridge.verify_regression(rc, regression, subject, candidate, self._mesh_policy())
        save(directory / "binding.json", receipt)
        save(directory / "regression.json", regression)
        return success("asset.glb", ["binding.json", "regression.json"], checks=2)

    def _bridge(self, subject):
        require("trace" in self.policies, 3, "vqa3d_profile_trace_absent")
        require(bool(self.options.get("profile")) and bool(self.options.get("profile_sha256"))
                and bool(self.options.get("required_checks")), 3, "vqa3d_expectations_absent")
        return vqa3d_bridge.read_receipt(self.policies["trace"], expected_source_sha256=file_ref(subject)["sha256"],
            expected_profile_id=self.options["profile"], expected_profile_sha256=self.options["profile_sha256"],
            required_check_ids=tuple(self.options["required_checks"]))

    def _appearance_admission(self, request):
        """Recompute authority from current files before evaluation or cache use."""
        from licence.check import check_provider
        for key in ("factory_config", "territory_policy", "provider_manifest"):
            require(key in self.policies, 3, key + "_absent")
            authenticate(self.policy_refs[key])
        require(type(request) is dict, 2, "appearance_request_invalid")
        context = request.get("licence_context")
        require(type(context) is dict, 3, "licence_context_missing")
        factory, policy, provider = (load(self.policies[key]) for key in
                                    ("factory_config", "territory_policy", "provider_manifest"))
        for key, value in (("configuration", factory), ("territory_policy", policy),
                           ("provider_manifest", provider)):
            require(core.sha256_json(context.get(key)) == core.sha256_json(value),
                    4, "licence_context_stale")
        require(type(provider) is dict and context.get("operation") == provider.get("slot"),
                4, "licence_operation_stale")
        decision = checked(check_provider(factory, provider, operation=provider.get("slot"), policy=policy))
        require(factory["slots"][decision["operation"]]["provider"] == provider["id"]
                and request.get("provider") == {"id": provider["id"], "revision": provider["revision"]},
                4, "licence_provider_stale")
        require(core.sha256_json(request.get("licence_decision")) == core.sha256_json(decision),
                4, "licence_decision_stale")
        frozen, proposed = factory["gates"]["appearance"], request.get("policy")
        require(type(proposed) is dict, 2, "appearance_policy_invalid")
        # The same publication minimum applies to craft and reference likeness.
        # A request may strengthen it, never lower the current factory minimum.
        for key, floor in (("floor", frozen["floor"]), ("target", frozen["target"]),
                           ("likeness_floor", frozen["floor"]), ("likeness_target", frozen["target"])):
            require(type(proposed.get(key)) is int and proposed[key] >= floor,
                    2, "appearance_policy_weakened")
        return decision

    def stage_appearance(self, directory, subject):
        # The existing appearance gate consumes bridge evidence, so authenticate
        # it here as a prerequisite and run the bridge stage again at the end.
        for key in ("appearance_request", "appearance_review"):
            require(key in self.policies, 3, key + "_absent")
        request, review = (load(self.policies[key]) for key in ("appearance_request", "appearance_review"))
        decision = self._appearance_admission(request)
        require(request.get("asset", {}).get("sha256") == file_ref(subject)["sha256"]
                and request.get("reference", {}).get("sha256") == self.reference_ref["sha256"],
                4, "appearance_subject_or_reference_stale")
        bridge = checked(self._bridge(subject))
        receipt = checked(appearance.evaluate(request, bridge, review))
        shutil.copyfile(subject, directory / "asset.glb")
        save(directory / "appearance.json", receipt)
        save(directory / "licence.json", decision)
        save(directory / "bridge-prerequisite.json", bridge)
        return success("asset.glb", ["appearance.json", "licence.json", "bridge-prerequisite.json"])

    def stage_vqa3d(self, directory, subject):
        receipt = checked(self._bridge(subject))
        shutil.copyfile(subject, directory / "asset.glb")
        save(directory / "bridge.json", receipt)
        return success("asset.glb", ["bridge.json"], checks=len(self.options["required_checks"]))
