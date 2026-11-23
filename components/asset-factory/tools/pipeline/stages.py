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
from tools.mesh import api as mesh
from gates import input_gate, vqa3d_bridge
from gates.appearance_gate import evaluate as appearance
from providers.trellis2 import adapter

ROOT = Path(__file__).resolve().parents[2]
SIBLING = ROOT.parent / "blender-visual-qa"


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
        self.policies = self._policy_files()
        self.policy_refs = {name: file_ref(path) for name, path in self.policies.items()}

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
                    *([self.raw_ref] if self.raw_ref else [])]:
            authenticate(ref)

    def _pin(self, name):
        # Keys pin algorithms as well as data. Imported sibling implementations
        # are hashed in place, never copied or modified.
        paths = {Path(__file__), Path(core.__file__), Path(contracts.__file__),
                 Path(__file__).with_name("engine.py"), Path(__file__).with_name("contracts.py")}
        paths.update(Path(__file__).parent.glob("*.py"))
        paths.update((ROOT / "slots").glob("*.schema.json"))
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
        refs = {str(p.relative_to(ROOT.parent)): core.sha256_file(p) for p in sorted(paths)}
        if name in ("topology", "uv_bake_orm"):
            executable = shutil.which("blender")
            refs["runtime/blender"] = core.sha256_file(Path(executable)) if executable else None
        return core.sha256_json(refs)

    def identity(self, name, subject):
        keys = {"input": ("input_policy",) if self.provider == "trellis2" else (),
                "provider": ("factory_config", "territory_policy", "provider_manifest") if self.provider == "trellis2" else (),
                "mesh": ("mesh_policy",), "topology": ("mesh_policy",),
                "uv_bake_orm": (), "export": ("mesh_policy",),
                "appearance": ("trace", "appearance_request", "appearance_review", "factory_config",
                               "territory_policy", "provider_manifest"), "vqa3d": ("trace",)}[name]
        refs = [self.policy_refs[key] for key in keys if key in self.policy_refs]
        if name in ("mesh", "topology", "uv_bake_orm", "export", "appearance", "vqa3d"):
            require(self.raw_ref is not None, 3, "retained_raw_absent")
            authenticate(self.raw_ref)
            refs.append(self.raw_ref)
        if name == "provider" or (name == "input" and self.provider == "trellis2"):
            refs.append(self.source_ref)
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
        return refs, parameters, self._pin(name)

    def execute(self, name, directory, subject):
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
            result["checks_evaluated"] += 1
        return result

    def _mesh_policy(self):
        require("mesh_policy" in self.policies, 3, "mesh_policy_absent")
        return load(self.policies["mesh_policy"])

    def stage_input(self, directory, subject):
        if self.provider == "byo":
            save(directory / "intake.json", {"schema": "af.byo-entry/v1", "rc": 0,
                                          "scope": "image_gate_not_applicable_to_glb"})
            return success(None, ["intake.json"], ["intake.json"])
        require("input_policy" in self.policies, 3, "input_policy_absent")
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
        if self.service is None:
            raise Refusal(3, "trellis2_service_not_built_or_transport_absent")
        for key in ("factory_config", "territory_policy", "provider_manifest"):
            require(key in self.policies, 3, key + "_absent")
        from licence.check import check_provider
        factory, policy, provider = (load(self.policies[key]) for key in
                                    ("factory_config", "territory_policy", "provider_manifest"))
        contracts.validate_provider(provider)
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

    def stage_uv_bake_orm(self, directory, subject):
        receipt = bake.bake_bundle(subject, subject, directory / "bake",
            res=self.options["texture_resolution"], max_stretch=self.options["max_stretch"],
            timeout_s=self.options["timeout_s"])
        checked(receipt)
        verification = checked(bundle.verify_bundle(directory / "bake",
            expected_receipt_sha256=receipt["receipt_sha256"],
            expected_high_sha256=file_ref(subject)["sha256"], expected_low_sha256=file_ref(subject)["sha256"],
            expected_policy={"res": self.options["texture_resolution"], "margin_px": 8,
                "cage_extrusion_um": 5000, "max_stretch_permille": self.options["max_stretch"],
                "timeout_s": self.options["timeout_s"]}, timeout_s=self.options["timeout_s"]))
        save(directory / "production-verification.json", verification)
        bundle_record = load(directory / "bake/bundle_receipt.json")
        require(bundle_record.get("runner", {}).get("kind") == "scoped-cpu"
                and receipt.get("runtime_measured") is True, 3, "injected_bake_runner")
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
        # Bundle verification binds every UV/map/ORM/PBR receipt and byte.
        save(directory / "bake.json", receipt)
        return success("named_low.glb", ["bake.json", "production-verification.json", "geometry-regression.json", "node-binding.json"],
                       ["named_low.glb"] + [p.relative_to(directory).as_posix() for p in sorted((directory / "bake").iterdir()) if p.is_file()],
                       checks=receipt["checks_evaluated"])

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
