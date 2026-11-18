"""Adapters to the landed F0/F1/F2 APIs; missing acceptance data refuses.

An image service, independent appearance review and vqa3d trace are explicit
inputs. This module never manufactures a reviewer, a profile trace or RAM proof.
The geometry policy is explicit; no caller component inventory is inferred.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import shutil

from asset_factory import core, contracts
from .engine import Refusal, authenticate, file_ref, load, require, owned_directory
from . import topology_bridge, bundle
from . import replay_transport
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
        self.replay = service if isinstance(service, replay_transport.ReplayTransport) else None
        if self.replay is not None:
            require(provider == "trellis2", 2, "replay_requires_trellis2")
            self.scope = replay_transport.SCOPE
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
                    *getattr(self, "_matte_approval_refs", ()),
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
        if name in ("uv_bake_orm", "export"):
            paths.add(Path(__file__).with_name("uv_derivative.py"))
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
        if name == "input" and self.provider == "trellis2" and "input_policy" in self.policies:
            policy = self._matte_input_policy()
            if policy is not None:
                require(policy.get("gate_id") == "input.matte", 2, "matte_receipt_invalid")
                if policy.get("source_rc") == 0:
                    approval_refs, _ = self._approved_matte_intent(policy)
                    refs.extend(approval_refs)
        if name == "provider" and self.provider == "trellis2" and self.options.get("qualification") is not None:
            admission = self._qualification_record()
            refs.extend(self.qualification_refs)
            if self.replay is not None:
                path = Path(admission["receipt"]["path"]).with_name("candidate.json")
                refs.append({"path": str(path), "sha256": admission["candidate_sha256"]})
        if name == "provider" and self.replay is not None:
            refs.extend(self.replay.refs)
        if name in ("mesh", "topology", "uv_bake_orm", "export", "appearance", "vqa3d"):
            require(self.raw_ref is not None, 3, "retained_raw_absent")
            authenticate(self.raw_ref)
            refs.append(self.raw_ref)
        if name == "provider" or (name == "input" and self.provider == "trellis2"):
            refs.append(self.source_ref)
        solver = None
        if name == "topology":
            solver, cage_ref = self._topology_solver()
            if cage_ref is not None:
                refs.append(cage_ref)
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
        if solver is not None:
            parameters["quadriflow_solver"] = solver
        if name == "provider" and self.replay is not None:
            parameters.update(scope=replay_transport.SCOPE, production_verdict="CANNOT_SEE",
                              calibration_state=replay_transport.SCOPE)
        for key in {"provider": ("timeout_s", "seed", "pipeline_type"),
                    "mesh": ("timeout_s",), "topology": ("timeout_s", "method", "target_faces", "seed"),
                    "uv_bake_orm": ("timeout_s", "texture_resolution", "max_stretch"),
                    "export": ("timeout_s",), "appearance": ("profile", "profile_sha256", "required_checks"),
                    "vqa3d": ("profile", "profile_sha256", "required_checks")}.get(name, ()):
            parameters[key] = self.options.get(key)
        if name == "uv_bake_orm":
            high, low, look_scope, look_refs, derivative = self._prepared_bake_inputs(subject)
            refs.extend(look_refs)
            parameters["bake_sources"] = {"high_sha256": high["sha256"], "low_sha256": low["sha256"]}
            if derivative is not None:
                parameters["uv_derivative"] = derivative
            if look_scope is not None:
                parameters["look_witness_digest"] = look_scope["witness_digest"]
                parameters["cage_extrusion_um"] = look_scope["cage_extrusion_um"]
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
        if name == "input" and self.provider == "trellis2":
            policy = self._matte_input_policy()
            if policy is not None:
                intake_ref = next((ref for ref in record["evidence"]
                                   if ref["path"] == str(directory / "intake.json")), None)
                require(intake_ref is not None and policy.get("source_rc") == 0,
                        4, "matte_intake_cache_stale")
                intake = load(intake_ref["path"], expected_sha256=intake_ref["sha256"])
                approval_refs, _ = self._approved_matte_intent(policy)
                require(all(ref in record["inputs"] for ref in approval_refs)
                        and intake.get("matte_approval_store") == approval_refs[1]
                        and intake.get("intent_digest") == policy["domain_digest"]
                        and intake.get("matte_source") == policy["matte_source"],
                        4, "matte_intake_cache_stale")
                if intake["matte_source"] == "birefnet":
                    from tools.qualification import verify_receipt

                    verify_receipt(policy["birefnet_evidence_dir"],
                                   expected_sha256=policy["birefnet_qualification"]["receipt_sha256"])
        if name == "uv_bake_orm":
            look_scope, _, pin = self._bake_look_scope()
            if pin is not None:
                require(self._pin(name) == record["toolchain_sha256"], 4, "upstream_toolchain_stale")
            sources = record["parameters"]["bake_sources"]
            self._verify_bake_sources(directory / "bake", sources["high_sha256"], sources["low_sha256"])
            derivative = record["parameters"].get("uv_derivative")
            if pin is not None:
                require(type(derivative) is dict and derivative.get("registration_digest") == pin,
                        4, "uv_derivative_cache_stale")
                source = derivative.get("cage_bound_low")
                require(source in record["inputs"], 4, "uv_derivative_cache_stale")
                high, low, look_scope, _, expected = self._prepared_bake_inputs(Path(source["path"]))
                require(derivative == expected and sources == {
                    "high_sha256": high["sha256"], "low_sha256": low["sha256"]},
                    4, "uv_derivative_cache_stale")
                require(file_ref(directory / "uv-derivative.json")["sha256"] == core.sha256_json(expected)
                        and file_ref(directory / "named_low.glb")["sha256"] == low["sha256"]
                        and file_ref(directory / "prepared_low.glb")["sha256"] == low["sha256"],
                        4, "uv_derivative_cache_stale")
            elif look_scope is not None and self.options["method"] == "quadriflow":
                raise Refusal(4, "uv_derivative_cache_stale")
            if look_scope is not None:
                receipt = load(directory / "bake.json")
                manifest = load(directory / "bake/bundle_receipt.json", expected_sha256=receipt["receipt_sha256"])
                policy = manifest.get("policy", {})
                require(type(policy) is dict, 2, "look_manifest_policy_invalid")
                require(record["parameters"].get("cage_extrusion_um") ==
                        policy.get("cage_extrusion_um") == look_scope["cage_extrusion_um"],
                        4, "look_fixture_cage_replay_stale")
                replay = self._verify_bake_look(directory / "bake", receipt, look_scope)
                require(file_ref(directory / "look-verification.json")["sha256"] == core.sha256_json(replay),
                        4, "look_verification_stale")
        if name == "export":
            look_scope, _, _ = self._bake_look_scope(require_for_provider=False)
            if look_scope is not None:
                require(self._pin(name) == record["toolchain_sha256"], 4, "upstream_toolchain_stale")
                # The engine has authenticated the previous UV/bake receipt.
                # Bind its named output as an exact input to this export.
                previous = record.get("previous")
                require(type(previous) is dict and previous.keys() == {"path", "sha256"}
                        and type(previous["path"]) is str and "\0" not in previous["path"]
                        and Path(previous["path"]).is_absolute() and Path(previous["path"]).name not in ("", "..")
                        and type(previous["sha256"]) is str and is_digest("sha256:" + previous["sha256"]),
                        2, "look_export_previous_invalid")
                subject = Path(previous["path"]).with_name("named_low.glb")
                require(file_ref(subject) in record["inputs"], 4, "look_export_input_stale")
                proof = self._protected_export_binding(directory / record["subject"], subject, look_scope)
                require(file_ref(directory / "protected-export.json")["sha256"] == core.sha256_json(proof),
                        4, "look_export_verification_stale")
        if name in GEOMETRY_STAGES:
            path = directory / GEOMETRY_RECEIPT
            refs = [ref for ref in record["evidence"] if ref["path"] == str(path)]
            require(len(refs) == 1, 3, "geometry_certificate_missing")
            candidate = directory / record["subject"] if name == "export" else Path(record["subject"])
            kwargs = {}
            if self._requires_quad_cage(name):
                evidence = {ref["path"]: ref["sha256"] for ref in record["evidence"]}
                require(str(candidate.with_suffix(".cage.json")) in evidence, 3, "required_quad_cage_missing")
                receipt_sha = evidence.get(str(directory / "topology.json"))
                require(receipt_sha is not None, 3, "topology_receipt_missing")
                solver, _ = self._topology_solver()
                receipt = load(directory / "topology.json", expected_sha256=receipt_sha)
                settings = core.canonical_json(solver)
                require(core.canonical_json(record["parameters"].get("quadriflow_solver")) == settings
                        and core.canonical_json(receipt.get("quadriflow_solver")) == settings,
                        4, "quadriflow_solver_cache_stale")
                # identity() keeps the immediate topology input last.
                kwargs = {"topology_input": record["inputs"][-1]["path"], "topology_receipt_sha": receipt_sha}
            proof = self._geometry_proof(name, candidate, **kwargs)
            require(refs[0]["sha256"] == core.sha256_json(proof), 4, "geometry_certificate_stale")

    def _matte_input_policy(self):
        ref = self.policy_refs.get("input_policy")
        if ref is None:
            return None
        try:
            policy = load(ref["path"], expected_sha256=ref["sha256"])
        except Refusal as exc:
            if exc.rc == 4:
                raise
            return None
        except (ValueError, core.ContractError):
            # Keep the legacy image gate's own policy diagnostics.
            return None
        if type(policy) is dict and policy.get("schema") == "af.input-evidence/v1":
            return policy
        return None

    def _approved_matte_intent(self, policy):
        from tools.input.evidence import bytes_digest
        from tools.input.matte import read_intent

        config_ref = self.policy_refs.get("factory_config")
        # The trusted launcher supplies the expected digest outside the request.
        config_pin = os.environ.get("AF_FACTORY_CONFIG_SHA256")
        require(config_ref is not None and config_pin, 3, "matte_approval_authority_missing")
        require(is_digest("sha256:" + config_pin), 2, "matte_factory_config_pin_invalid")
        require(config_ref["sha256"] == config_pin, 4, "matte_factory_config_stale")
        config = load(config_ref["path"], expected_sha256=config_pin)
        require(type(config) is dict and config.get("schema") == "af.factory-config/v1",
                2, "matte_factory_config_invalid")
        store_ref = config.get("matte_approval_store")
        require(store_ref is not None, 3, "matte_approval_authority_missing")
        require(type(store_ref) is dict and store_ref.keys() == {"path", "sha256"}
                and type(store_ref["path"]) is str and "\x00" not in store_ref["path"]
                and type(store_ref["sha256"]) is str and is_digest("sha256:" + store_ref["sha256"]),
                2, "matte_approval_store_ref_invalid")
        store_path = Path(store_ref["path"])
        require(store_path.is_absolute(), 2, "relative_reference")
        require(str(store_path) == store_ref["path"] and store_path.resolve() == store_path,
                2, "noncanonical_reference")
        require(not (store_path.name in {"auth.json", "operator.token", "launch.token", ".netrc", ".npmrc"}
                     or store_path.name.startswith(".env") or store_path.suffix == ".key"
                     or ".gemini" in store_path.parts), 2, "sensitive_path_refused")
        store = load(store_path, expected_sha256=store_ref["sha256"])
        require(type(store) is dict and store.keys() == {"schema", "approvals"}
                and store["schema"] == "af.matte-approval-store/v1" and type(store["approvals"]) is list,
                2, "matte_approval_store_invalid")

        input_refs = policy.get("input_refs")
        registration = policy.get("registration")
        require(type(input_refs) is list and len(input_refs) == 3 and type(registration) is dict,
                3, "matte_approval_pair_missing")
        pair = input_refs[0], registration.get("selection_digest")
        require(all(is_digest(pin) for pin in pair), 2, "matte_approval_pair_invalid")
        approvals = {}
        for entry in store["approvals"]:
            require(type(entry) is dict and entry.keys() == {"original_digest", "selection_digest", "intent_digest"}
                    and all(is_digest(pin) for pin in entry.values()), 2, "matte_approval_entry_invalid")
            key = entry["original_digest"], entry["selection_digest"]
            require(key not in approvals, 2, "matte_approval_pair_duplicate")
            approvals[key] = entry["intent_digest"]
        require(pair in approvals, 4, "matte_approval_pair_mismatch")
        intent_pin = policy.get("domain_digest")
        require(intent_pin is not None, 3, "matte_intent_digest_missing")
        require(intent_pin == approvals[pair], 4, "matte_intent_not_approved")

        path = store_path.parent / "matte-intent" / intent_pin[7:]
        try:
            raw = read_bytes(path)
        except bake_support.Refusal as exc:
            if exc.rc == 3:
                raise Refusal(3, "missing_matte-intent") from exc
            raise
        require(bytes_digest(raw) == intent_pin, 4, "matte_intent_stale")
        intent = read_intent(lambda _role, _pin: raw, intent_pin)
        require((intent.get("original_digest"), intent.get("selection_digest")) == pair,
                4, "matte_intent_pair_mismatch")
        require("matte_source" in policy, 3, "matte_source_missing")
        require(policy["matte_source"] == intent["matte_source"], 4, "matte_source_mismatch")
        refs = (config_ref, store_ref, {"path": str(path), "sha256": intent_pin[7:]})
        self._matte_approval_refs = refs
        return refs, raw

    def stage_input(self, directory, subject):
        if self.provider == "byo":
            save(directory / "intake.json", {"schema": "af.byo-entry/v1", "rc": 0,
                                          "scope": "image_gate_not_applicable_to_glb"})
            return success(None, ["intake.json"], ["intake.json"])
        require("input_policy" in self.policies, 3, "input_policy_absent")
        policy_path = self.policies["input_policy"]
        policy_sha = self.policy_refs["input_policy"]["sha256"]
        policy = self._matte_input_policy()
        if policy is not None:
            from gates.input_d18 import inspect_matte
            from tools.input.evidence import bytes_digest, checked_record, verify_input_receipt

            require(policy.get("gate_id") == "input.matte", 2, "matte_receipt_invalid")
            checked({"rc": policy.get("source_rc"), "reason_codes": policy.get("reason_codes")})
            derivative_pin = "sha256:" + self.source_ref["sha256"]
            require(policy.get("subject_digest") == derivative_pin, 4, "matte_derivative_stale")
            approval_refs, intent_bytes = self._approved_matte_intent(policy)
            intent_pin = policy["domain_digest"]
            matte_source = policy["matte_source"]
            proof = policy.get("birefnet_qualification")
            if proof is not None:
                require(type(proof) is dict and proof.keys() == {"receipt_sha256", "candidate_sha256"},
                        2, "invalid_birefnet_qualification")
                proof = {**proof, "evidence_dir": policy.get("birefnet_evidence_dir")}

            witnesses = {}

            def read_checked(role, pin):
                require(role.replace("-", "").isalpha() and is_digest(pin),
                        2, "matte_witness_pin_invalid")
                name = Path("matte-witnesses") / role / pin[7:]
                if name not in witnesses:
                    try:
                        if role == "matte-intent":
                            require(pin == intent_pin, 4, "matte_intent_not_approved")
                            raw = intent_bytes
                        else:
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
                                        intent_digest=intent_pin, read_checked=read_checked,
                                        matte_source=matte_source, birefnet_qualification=proof)
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
                "matte_source": matte_source, "matte_approval_store": approval_refs[1],
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
        require(self.service is None or fixture == (self.replay is not None),
                5, "qualification_fixture_scope")
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
        declarations = {key: factory[key] for key in ("jurisdiction", "commercial_use") if key in factory}
        request = {"schema": "af.generate/v1", "kind": "request", "slot": "generate",
                   "request_id": self.source_ref["sha256"], "provider_id": provider["id"],
                   "provider_revision": provider["revision"],
                   "inputs": [{"role": "image", "path": str(self.source), "media_type": "image/png",
                               "sha256": self.source_ref["sha256"]}],
                   "params": {"seed": self.options["seed"], "pipeline_type": self.options["pipeline_type"]},
                   "context": {**declarations, "policy_sha256": core.sha256_json(policy),
                       "toolchain_sha256": self._pin("provider"), "timeout_s": self.options["timeout_s"]}}
        contracts.validate_slot(request, "generate", "request")

        async def generate():
            result = await adapter.generate(read_bytes(self.source), seed=self.options["seed"],
                pipeline_type=self.options["pipeline_type"], output_dir=directory / "generation",
                request=request, provider=provider, decision=decision, factory=factory, policy=policy,
                service=self.service, timeout_s=self.options["timeout_s"])
            contracts.validate_slot(result, "generate", "result")
            return result

        if self.replay is not None:
            self.replay.bind_admission(core.sha256_json(candidate), provider)
            replay_result = asyncio.run(generate())
            if replay_result["rc"]:
                refusal = Refusal(replay_result["rc"], replay_result["absence_reasons"]["outputs"])
                refusal.evidence_record = replay_result
                raise refusal
            save(directory / "replay-transport.json", self.replay.record)
        else:
            self._schedule_provider(directory, provider, factory, decision, request, generate)

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
        save(directory / "provider.json", result)
        evidence = ["replay-transport.json" if self.replay is not None else "scheduler.json", "provider.json"]
        for ref in result["receipts"]:
            path = source / ref["path"]
            require(path.resolve().is_relative_to(source.resolve()), 2, "provider_receipt_escape")
            require(file_ref(path)["sha256"] == ref["sha256"], 4, "provider_receipt_stale")
            evidence.append(path.relative_to(directory).as_posix())
        return success("raw.glb", evidence)

    def _schedule_provider(self, directory, provider, factory, decision, request, generate):
        # Live services require the F0 scheduler's admission and release proofs.
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
        lifecycle = self.scheduler_backend
        class ScheduledAdapter:
            async def observe(self, handle, deadline_ns):
                return await lifecycle.observe(handle, deadline_ns)
            async def start(self, req, deadline_ns):
                return await lifecycle.start(req, deadline_ns)
            async def stop(self, handle, deadline_ns):
                return await lifecycle.stop(handle, deadline_ns)
            async def run(self, handle, req, deadline_ns):
                result = await generate()
                return {"rc": result["rc"], "status": result["status"],
                        "reason_codes": [result["absence_reasons"].get("outputs", "provider_refused")]
                                        if result["rc"] else [],
                        "peak_rss_bytes": result["metrics"]["peak_rss_bytes"]}
        receipt = asyncio.run(scheduler.execute(sched_request, ScheduledAdapter()))
        checked(receipt)
        save(directory / "scheduler.json", receipt)

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

    def _topology_solver(self):
        require("quadriflow_target_quads" not in self.options, 2, "quadriflow_solver_option_refused")
        if not self._requires_quad_cage("topology"):
            return None, None
        ref = self.policy_refs.get("mesh_policy")
        require(ref is not None, 3, "mesh_policy_absent")
        policy = load(ref["path"], expected_sha256=ref["sha256"])
        scopes = policy.get("geometry_scope", {})
        require(type(scopes) is dict, 2, "geometry_scope_invalid")
        require("quadriflow_solver" in scopes, 3, "quadriflow_solver_policy_missing")
        solver = scopes["quadriflow_solver"]
        require(type(solver) is dict and solver.keys() == {
                    "quadriflow_target_quads", "quad_cage_digest", "production_evidence"}
                and solver["production_evidence"] is False
                and type(solver["quadriflow_target_quads"]) is int
                and solver["quadriflow_target_quads"] > 0
                and is_digest(solver["quad_cage_digest"]), 2, "quadriflow_solver_policy_invalid")
        cage_ref = self._topology_cage_input()
        require(solver["quad_cage_digest"] == "sha256:" + cage_ref["sha256"],
                4, "quadriflow_solver_cage_stale")
        return solver, cage_ref

    def stage_topology(self, directory, subject):
        policy = self._mesh_policy()
        from tools.blender.topology_glb import inspect_glb
        info = inspect_glb(subject)
        method = self.options["method"]
        target = self.options.get("target_faces")
        require(target is not None or method == "identity", 2, "target_faces_required")
        target = info["faces"] if method == "identity" else target
        solver, _ = self._topology_solver()
        receipt = topology_bridge.run_candidate(subject, directory / "topology", method=method,
            target_faces=target, seed=self.options["seed"], policy=policy,
            timeout_s=self.options["timeout_s"],
            quadriflow_target_quads=solver["quadriflow_target_quads"] if solver is not None else None)
        checked(receipt)
        if solver is not None:
            receipt["quadriflow_solver"] = solver
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

    def _bake_look_scope(self, *, require_for_provider=True):
        from tools.input.evidence import checked_record

        policy = (load(self.policies["mesh_policy"], expected_sha256=self.policy_refs["mesh_policy"]["sha256"])
                  if "mesh_policy" in self.policies else {})
        scopes = policy.get("geometry_scope", {})
        require(type(scopes) is dict, 2, "geometry_scope_invalid")
        if "look_scope" not in scopes:
            require(not require_for_provider or self.provider == "byo", 3, "look_scope_missing")
            return None, (), None
        scope = scopes["look_scope"]
        require(type(scope) is dict and is_digest(scope.get("witness_digest")), 2, "look_scope_invalid")
        require(not {"cage_extrusion_um", "diagnostic_cage_extrusion_um"} & scope.keys(),
                2, "look_fixture_cage_refused")
        require(scope.keys() <= {"witness_digest", "uv_derivative_digest", "fixture_cage_policy"}
                and ("uv_derivative_digest" not in scope or is_digest(scope["uv_derivative_digest"])),
                2, "look_scope_invalid")
        cage_um = 5000
        if "fixture_cage_policy" in scope:
            cage = scope["fixture_cage_policy"]
            require(type(cage) is dict and cage.keys() == {
                        "cage_extrusion_um", "witness_digest", "production_evidence"}
                    and cage["production_evidence"] is False
                    and cage["witness_digest"] == scope["witness_digest"]
                    and type(cage["cage_extrusion_um"]) is int
                    and 5000 <= cage["cage_extrusion_um"] <= 80000,
                    2, "look_fixture_cage_refused")
            # The authenticated mesh policy binds this fixture-only range to
            # the exact high/low pair already sealed in the LOOK witness.
            cage_um = cage["cage_extrusion_um"]
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
        return {"witness_digest": pin, "read_checked": read_checked,
                "cage_extrusion_um": cage_um}, refs, scope.get("uv_derivative_digest")

    def _prepared_bake_inputs(self, subject):
        from . import uv_derivative
        from tools.input.evidence import bytes_digest, checked_record

        high, low = self._bake_sources(subject)
        look, refs, pin = self._bake_look_scope()
        if look is None:
            return high, low, look, (), None
        read = look["read_checked"]
        witness = checked_record(read, "look-bake-witness", look["witness_digest"])
        registration = None
        if pin is not None:
            registration = checked_record(read, "uv-derivative-registration", pin)
            require(registration.keys() == {"schema", "source_digest", "cage_digest", "mesh_digest",
                                           "witness_digest", "resolution"}
                    and registration["schema"] == "af.uv-derivative-registration/v1"
                    and all(is_digest(registration[field]) for field in
                            ("source_digest", "cage_digest", "mesh_digest", "witness_digest"))
                    and type(registration["resolution"]) is int, 2, "uv_derivative_registration_invalid")
        target = registration["mesh_digest"] if registration is not None else "sha256:" + low["sha256"]
        require([witness.get("high_digest"), witness.get("mesh_digest")] ==
                ["sha256:" + high["sha256"], target], 4, "look_subject_drift")
        if registration is None:
            require(self.options["method"] != "quadriflow", 3, "uv_derivative_registration_missing")
            return high, low, look, tuple(refs.values()), None
        cage = self._remember_bake_input(file_ref(Path(subject).with_suffix(".cage.json")))
        require(registration["source_digest"] == "sha256:" + low["sha256"]
                and registration["cage_digest"] == "sha256:" + cage["sha256"]
                and registration["witness_digest"] == look["witness_digest"]
                and registration["resolution"] == self.options["texture_resolution"],
                4, "uv_derivative_registration_stale")
        atlas = checked_record(read, "uv-layout", witness["atlas_digest"])
        projection = checked_record(read, "uv-projection-layout", witness["projection_digest"])
        require(atlas.get("mesh_digest") == projection.get("mesh_digest") == target,
                4, "uv_derivative_atlas_stale")
        read("look-protected-regions", witness.get("protected_regions_digest"))
        raw = read("uv-derivative-glb", target)
        original = read_bytes(subject)
        require(bytes_digest(original) == registration["source_digest"], 4, "uv_derivative_registration_stale")
        max_faces = self.options["target_faces"]
        require(type(max_faces) is int and max_faces > 0, 2, "target_faces_required")
        faces = uv_derivative.same_geometry(original, raw, max_faces=max_faces)
        proof = {"schema": "af.uv-derivative-proof/v1", "registration_digest": pin,
                 "cage_bound_low": low, "cage": cage, "derivative_digest": target, "faces": faces,
                 "geometry_rule": "exact_oriented_triangles_with_coincident_uv_splits"}
        refs["cage", registration["cage_digest"]] = cage
        refs["worker", "unwrap"] = self._remember_bake_input(file_ref(bake_support.upstream("bake_pbr").WORKER))
        return high, refs["uv-derivative-glb", target], look, tuple(refs.values()), proof

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
        from . import uv_derivative

        high, low, look_scope, _, derivative = self._prepared_bake_inputs(subject)
        output = directory / "bake"
        options = {"res": self.options["texture_resolution"], "max_stretch": self.options["max_stretch"],
                   "timeout_s": self.options["timeout_s"]}
        if look_scope is not None:
            options.update(look_scope={key: look_scope[key] for key in ("witness_digest", "read_checked")},
                           cage_extrusion_um=look_scope["cage_extrusion_um"], margin_px=2, blender_threads=1)
        low_path = low["path"]
        if derivative is not None:
            low_path = directory / "prepared_low.glb"
            raw = uv_derivative.prepare(subject, low_path, resolution=options["res"], timeout_s=options["timeout_s"])
            uv_derivative.same_geometry(read_bytes(subject), raw, max_faces=self.options["target_faces"])
            require(hashlib.sha256(raw).hexdigest() == low["sha256"], 4, "uv_derivative_reproduction_drift")
            save(directory / "uv-derivative.json", derivative)
        receipt = bake.bake_bundle(high["path"], low_path, output, **options)
        checked(receipt)
        self._verify_bake_sources(output, high["sha256"], low["sha256"])
        bundle_record = load(output / "bundle_receipt.json", expected_sha256=receipt["receipt_sha256"])
        require(bundle_record.get("runner", {}).get("kind") == "scoped-cpu"
                and receipt.get("runtime_measured") is True, 3, "injected_bake_runner")
        outputs = ["named_low.glb", *(p.relative_to(directory).as_posix()
                    for p in sorted(output.iterdir()) if p.is_file())]
        save(directory / "bake.json", receipt)
        if look_scope is not None:
            policy = bundle_record.get("policy", {})
            require(type(policy) is dict, 2, "look_manifest_policy_invalid")
            require(policy.get("cage_extrusion_um") == look_scope["cage_extrusion_um"],
                    4, "look_fixture_cage_replay_stale")
            verified = self._verify_bake_look(output, receipt, look_scope)
            save(directory / "look-verification.json", verified)
            # The authenticated target retains its original atlas and node binding.
            # The protected textured GLB is retained in the bake bundle.
            (directory / "named_low.glb").write_bytes(read_bytes(output / "source_low.glb"))
            evidence = ["bake.json", "look-verification.json"]
            if derivative is not None:
                evidence.append("uv-derivative.json")
                outputs.append("prepared_low.glb")
            return success("named_low.glb", evidence, outputs,
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

    def _protected_export_binding(self, candidate, subject, look_scope):
        from . import uv_derivative

        root = subject.parent
        bundle = root / "bake"
        receipt = load(root / "bake.json")
        replay = self._verify_bake_look(bundle, receipt, look_scope)
        require(file_ref(root / "look-verification.json")["sha256"] == core.sha256_json(replay),
                4, "look_verification_stale")
        self._verify_bake_sources(bundle, self.raw_ref["sha256"], file_ref(subject)["sha256"])
        manifest = load(bundle / "bundle_receipt.json", expected_sha256=replay["receipt_sha256"])
        policy = manifest.get("policy", {})
        require(type(policy) is dict, 2, "look_manifest_policy_invalid")
        require(policy.get("cage_extrusion_um") == look_scope["cage_extrusion_um"],
                4, "look_fixture_cage_replay_stale")
        files = {item["path"]: item["sha256"] for item in manifest["files"]}
        raw = read_bytes(bundle / "look_receipt.json")
        require(hashlib.sha256(raw).hexdigest() == files["look_receipt.json"], 4, "bytes_stale")
        look = bake_support.load_record(raw)
        core._check_json(look)
        binding = uv_derivative.verify_export(candidate, bundle, look, files["textured.glb"])
        return {**replay, **binding}

    def stage_export(self, directory, subject):
        look_scope, _, _ = self._bake_look_scope(require_for_provider=False)
        if look_scope is not None:
            candidate = directory / "asset.glb"
            candidate.write_bytes(read_bytes(subject.parent / "bake/textured.glb"))
            proof = self._protected_export_binding(candidate, subject, look_scope)
            save(directory / "protected-export.json", proof)
            return success("asset.glb", ["protected-export.json"], checks=proof["checks_evaluated"])
        require(not (subject.parent / "look-verification.json").exists(), 3, "look_scope_missing")
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
        recorded = request.get("licence_decision")
        require(type(recorded) is dict and recorded.get("schema") == "af.licence-decision/v1",
                2, "licence_decision_invalid")
        require(core.sha256_json(recorded) == request.get("licence_decision_sha256"),
                4, "licence_decision_stale")
        # Historic decisions may carry a negative licence advisory. Their
        # evidence still has to bind the current operation and inputs.
        for key, expected in (("configuration_sha256", core.sha256_json(factory)),
                              ("provider_sha256", core.sha256_json(provider)),
                              ("policy_sha256", core.sha256_json(policy)),
                              ("provider_id", provider["id"]), ("operation", provider["slot"])):
            require(recorded.get(key) == expected, 4, "licence_decision_stale")
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
