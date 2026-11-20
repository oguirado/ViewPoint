# SPDX-License-Identifier: AGPL-3.0-only
"""Load digest-selected offline fixture contracts and verify record closure."""

from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import stat

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012


BASE_PIN = "669125919ed4e5205e89429644405d53c8bb1eaed235def4db8e1bf5e1b4b247"
SCENE_PIN = "afabe455e8a4b43e53bb64606a1134f4389abb3ec73814c73cba8179fb131576"
SCENE_SCHEMA = "174484b5b329566874bf6a5baf968efc401ddc364543292c01976d8a64260738"
SCENE_FIXTURES = "c47432ba8281e3326b78b7c883d059f64cc79ad125039d240e91010362b9e281"
TABLE_RAW = "b839fda7a468f0f6f7836aa79328619c97aae36d31ce904fa8c1243aabdbcd5a"
ARTIFACT_REF = "3a974ee9a2dfe5c5a23710c78ac6ede36cc15758df593e50f480d71f180bac2c"
TABLE = "vp_rea_call.op_table.json"
STAGE_PARENT = {
    "rea.software_model/v2": "game_evidence",
    "rea.spec/v2": "model",
    "rea.reimpl_plan/v2": "spec",
    "rea.scaffold_receipt/v2": "plan",
    "rea.verification/v2": "scaffold",
    "rea.report/v2": "verification",
}
PRIVATE = {
    "rea.custody.wide_truth/v1", "rea.custody.binary_truth/v1",
    "rea.custody.phase_policy/v1", "rea.custody.phase_receipt/v1",
}
PHASE_BINDS = ("phase", "parent_p_sha256", "extension_sha256", "scorer_sha256",
               "fixture_manifest_sha256", "dependency_receipt_sha256s")
ROUTE_INPUT = {"model": ("screen", "game_evidence"), "spec": ("model",),
               "plan": ("spec",), "scaffold": ("plan",), "verify": ("scaffold",),
               "report": ("verification",)}


class ContractError(ValueError):
    """A refusal at the named contract boundary."""

    def __init__(self, code, reason):
        self.code = code
        super().__init__(f"{code}: {reason}")


def require(condition, code, reason):
    if not condition:
        raise ContractError(code, reason)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return sha256((encoded + "\n").encode("utf-8"))


def decode(raw):
    def unique_keys(pairs):
        values = {}
        for key, value in pairs:
            require(key not in values, "JSON", "duplicate member name")
            values[key] = value
        return values

    def bad_constant(_):
        raise ContractError("JSON", "non-finite number")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys,
                           parse_constant=bad_constant)
        canonical_digest(value)
        return value
    except (UnicodeError, ValueError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError("JSON", "invalid UTF-8 JSON or non-finite number") from error


def read_file(path, code="PINS", expected=None):
    try:
        require(path == path.resolve(strict=True) and stat.S_ISREG(path.lstat().st_mode),
                code, f"unsafe file: {path.name}")
        raw = path.read_bytes()
    except OSError as error:
        raise ContractError(code, f"cannot read {path.name}") from error
    require(expected is None or sha256(raw) == expected, code, f"digest mismatch: {path.name}")
    return raw


def urn(name):
    base, version = name.split("/")
    return f"urn:{base}:{version}"


def check_references(value, registry, uri=""):
    if isinstance(value, dict):
        uri = value.get("$id", uri)
        if "$ref" in value:
            registry.resolver(uri).lookup(value["$ref"])
        for child in value.values():
            check_references(child, registry, uri)
    elif isinstance(value, list):
        for child in value:
            check_references(child, registry, uri)


def closed_inventory(directory, names, code):
    try:
        require(directory == directory.resolve(strict=True), code, "unsafe directory")
        found = {entry.name for entry in directory.iterdir()}
    except OSError as error:
        raise ContractError(code, "missing directory") from error
    require(found == names, code, "inventory mismatch")


def record_refs(value):
    if isinstance(value, dict):
        if value.keys() == {"schema", "sha256"}:
            yield value
        else:
            for child in value.values():
                yield from record_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from record_refs(child)


def walked_witnesses(value):
    if isinstance(value, dict):
        if value.get("origin") in ("frame", "binary"):
            yield value
        else:
            for child in value.values():
                yield from walked_witnesses(child)
    elif isinstance(value, list):
        for child in value:
            yield from walked_witnesses(child)


def compatible_range(inner, outer):
    return (outer["offset"] <= inner["offset"]
            and inner["offset"] + inner["length"] <= outer["offset"] + outer["length"]
            and (inner["offset"] != outer["offset"] or inner["length"] != outer["length"]
                 or inner["sha256"] == outer["sha256"]))


def unique_index(rows, field, code):
    index = {row[field]: row for row in rows}
    require(len(index) == len(rows), code, f"duplicate {field}")
    return index


class Catalog:
    """Check the independently expected pointer and both parents before admitting inputs."""

    def __init__(self, component, expected_extension_sha256):
        require(isinstance(expected_extension_sha256, str)
                and re.fullmatch(r"[a-f0-9]{64}", expected_extension_sha256),
                "EXTENSION_PIN", "independently expected extension digest required")
        component = Path(component).absolute()
        ext = component / "contracts/ext"
        selected = read_file(ext / "PINS.json", "EXTENSION_PIN", expected_extension_sha256)
        generation = ext / "generations" / expected_extension_sha256
        self._configure(component, generation, selected)

    @classmethod
    def _staged(cls, component, generation, expected):
        instance = cls.__new__(cls)
        pinned = read_file(generation / "PINS.json", "EXTENSION_PIN", expected)
        instance._configure(component, generation, pinned)
        return instance

    def _configure(self, component, generation, selected):
        self.extension_sha256 = sha256(selected)
        root = component / "contracts"
        scene = component / "game/scene"
        self.generation = generation
        manifest = decode(selected)
        parents = {"base_manifest_sha256": BASE_PIN, "scene_manifest_sha256": SCENE_PIN,
                   "scene_schema_sha256": SCENE_SCHEMA, "scene_fixtures_sha256": SCENE_FIXTURES}
        require(isinstance(manifest, dict)
                and manifest.keys() == {"schema", "parents", "files", "verifier_sha256"}
                and manifest.get("schema") == "rea.contract_extension_pins/v1"
                and manifest.get("parents") == parents,
                "EXTENSION_PIN", "extension manifest identity or parents")
        read_file(root / "ext/verify.py", "EXTENSION_PIN", manifest["verifier_sha256"])
        require(read_file(generation / "PINS.json", "EXTENSION_PIN", self.extension_sha256) == selected,
                "EXTENSION_PIN", "generation manifest mismatch")
        files = manifest["files"]
        require(isinstance(files, dict) and {"baseline.json", TABLE, "provider_slot_manifest.json"} <= files.keys()
                and all(re.fullmatch(r"[a-zA-Z0-9_.-]+\.json", name) and name != "PINS.json"
                        and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
                        for name, digest in files.items()), "EXTENSION_PIN", "invalid generation inventory")
        closed_inventory(generation, {"PINS.json", *files}, "EXTENSION_PIN")

        base_pins = decode(read_file(root / "PINS.json", "BASE_PIN", BASE_PIN))["files"]
        require(set(base_pins) == {p.name for p in root.glob("*.schema.json")} | {TABLE},
                "BASE_PIN", "base schema inventory")
        base_docs = {name: decode(read_file(root / name, "BASE_PIN")) for name in base_pins}
        require(all(canonical_digest(doc) == base_pins[name] for name, doc in base_docs.items()),
                "BASE_PIN", "base canonical file digest")
        read_file(root / TABLE, "BASE_PIN", TABLE_RAW)
        scene_pins = decode(read_file(scene / "PINS.json", "SCENE_PIN", SCENE_PIN))["files"]
        scene_docs = {name: decode(read_file(scene / name, "SCENE_PIN")) for name in scene_pins}
        require(scene_pins == {"scene.schema.json": SCENE_SCHEMA, "scene.fixtures.json": SCENE_FIXTURES}
                and all(canonical_digest(doc) == scene_pins[name] for name, doc in scene_docs.items()),
                "SCENE_PIN", "scene canonical file digest")
        artifact_ref = component.parent / "vp-direct/schemas/vp.artifact_ref_v1.schema.json"
        artifact_doc = decode(read_file(artifact_ref, "BASE_PIN", ARTIFACT_REF))

        ext_docs = {name: decode(read_file(generation / name, "EXTENSION_PIN", digest))
                    for name, digest in files.items()}
        legacy = [doc for name, doc in base_docs.items() if name.endswith(".schema.json")]
        legacy.extend((scene_docs["scene.schema.json"], artifact_doc))
        extensions = [doc for name, doc in ext_docs.items() if name.endswith(".schema.json")]
        all_ids = [doc["$id"] for doc in legacy + extensions]
        require(len(all_ids) == len(set(all_ids)), "SCHEMA_ALIAS", "duplicate resource ID")
        self.legacy_registry = self._registry(legacy)
        self.registry = self._registry(legacy + extensions)
        self.central_table = base_docs[TABLE]
        self.local_table = ext_docs[TABLE]
        self.slot_manifest = ext_docs["provider_slot_manifest.json"]
        self.validate("rea.provider_slot_manifest/v1", self.slot_manifest)
        self._op_resources()

    @staticmethod
    def _registry(docs):
        for doc in docs:
            try:
                Draft202012Validator.check_schema(doc)
            except SchemaError as error:
                raise ContractError("SCHEMA", f"invalid resource {doc['$id']}") from error
        registry = Registry().with_resources(
            (doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
            for doc in docs
        ).crawl()
        uris = set()
        for doc in docs:
            try:
                check_references(doc, registry)
            except Unresolvable as error:
                raise ContractError("SCHEMA_REF", f"unresolved reference in {doc['$id']}") from error
            for resource in (doc, *doc.get("$defs", {}).values()):
                if "$id" in resource:
                    uri = resource["$id"]
                    require(uri not in uris, "SCHEMA_ALIAS", uri)
                    uris.add(uri)
        return registry

    def _op_resources(self):
        local = self.local_table["op_table"]
        self.validate("vp_rea_call.args/v2", {
            "op": "rea.binary.ingest", "goal_id": "wire-schema-witness",
            "profile": "offline_fixture", "inputs": [
                {"role": "authored_build", "path": "fixture", "media_type": "application/octet-stream",
                 "record_schema": None, "sha256": "0" * 64}],
        })
        envelope = self.registry.contents(urn("vp_rea_call.args/v2"))
        require(set(envelope["properties"]["op"]["enum"]) == set(local)
                and set(envelope["$defs"]) == {f"{op}.{kind}" for op in local for kind in ("args", "result")},
                "SELECTOR", "local selector inventory mismatch")
        slots = unique_index(self.slot_manifest["slots"], "slot", "PROVIDER")
        for row in slots.values():
            for role in row["inputs"] + row["outputs"]:
                require(len(role["schemas"]) == len(set(role["schemas"])), "PROVIDER", "duplicate role schema")
                for name in role["schemas"]:
                    require(name not in PRIVATE, "PRIVACY", "private slot role")
                    self.registry.contents(urn(name))
        for row in local.values():
            require(row["slot"] in slots
                    and not ({"supported", "binding", "operator_only"} & row.keys()),
                    "SELECTOR", "dispatch authority or unknown slot in local table")
        for table, version, registry in ((self.central_table, "v1", self.legacy_registry),
                                         (self.local_table, "v2", self.registry)):
            for op, row in table["op_table"].items():
                for field, role in (("args", "args"), ("reply", "result")):
                    name = f"vp_rea_call.{op}.{role}/{version}"
                    require(row[f"{field}_schema"] == name
                            and canonical_digest(registry.contents(urn(name))) == row[f"{field}_schema_sha256"],
                            "SELECTOR", f"resource digest mismatch for {op}: {role}")

    def validate(self, name, payload, profile="local_fixture"):
        require(profile in ("legacy", "local_fixture"), "PROFILE", "unknown validation profile")
        registry = self.legacy_registry if profile == "legacy" else self.registry
        try:
            canonical_digest(payload)
            schema = registry.contents(urn(name))
        except (KeyError, TypeError, ValueError) as error:
            raise ContractError("SCHEMA", f"unknown schema or invalid JSON value for {name}") from error
        try:
            Draft202012Validator(schema, registry=registry).validate(payload)
        except ValidationError as error:
            raise ContractError("SCHEMA", f"{name} {error.json_path}: {error.validator}") from error

    @staticmethod
    def _route(slot, inputs, outputs):
        if slot in ROUTE_INPUT:
            role = next(item for item in inputs if item["role"] in ROUTE_INPUT[slot])
            name = role["record_schema"]
            game = name == "rea.game.evidence/v1" or name.endswith("/v2")
            require(outputs[0]["record_schema"].endswith("/v2") == game,
                    "BRANCH", "operation changed the model route")

    def validate_operation(self, args, reply=None, *, approved_inputs=None, records=None):
        """Validate transport values without opening any artifact path."""
        self.validate("vp_rea_call.args/v2", args)
        op = args["op"]
        entry = self.local_table["op_table"][op]
        self.validate(entry["args_schema"], args)
        if approved_inputs is not None:
            approved = {canonical_digest(entry) for entry in approved_inputs}
            require(all(canonical_digest(entry) in approved for entry in args["inputs"]),
                    "INPUT_ADMISSION", "input is outside the independently admitted observations and actions")
            require("held_out_split" not in args or canonical_digest(args["held_out_split"]) in approved,
                    "INPUT_ADMISSION", "holdout split is unadmitted")
        if reply is None:
            return
        self.validate(entry["reply_schema"], reply)
        require(reply["receipt"]["schema"] not in PRIVATE, "PRIVACY", "private receipt in public reply")
        self._route(entry["slot"], args["inputs"], reply["artifact_refs"])
        if records is not None:
            for output in reply["artifact_refs"]:
                kind = output["record_schema"]
                if kind is None:
                    continue
                digest = output["sha256"]
                require(digest in records and canonical_digest(records[digest]) == digest,
                        "RECORD_REF", f"missing or changed operation result {kind}")
                value = records[digest]
                self.validate_closure(kind, value, records)
                require(op != "rea.binary.model" or value["mode"] == "binary-only",
                        "BRANCH", "binary model result must be extracted-only")

    def validate_exchange(self, provider, request, result, *, expected_provider_sha256):
        self.validate("rea.provider/v1", provider)
        self.validate("rea.slot_request/v1", request)
        self.validate("rea.slot_result/v1", result)
        require(canonical_digest(provider) == expected_provider_sha256 and provider["runnable"],
                "PROVIDER", "provider is blocked or its digest is unadmitted")
        revision = provider["revision"] if provider["revision"] is not None else provider["implementation_sha256"]
        match = {"slot": provider["slot"], "provider_id": provider["id"], "provider_revision": revision,
                 "implementation_sha256": provider["implementation_sha256"],
                 "execution_profile": provider["execution_profile"], "request_id": request["request_id"]}
        require(all(request[k] == result[k] == value for k, value in match.items()),
                "PROVIDER", "provider identity or revision does not match the exchange")
        require(request["context"]["timeout_s"] <= provider["runtime"]["timeout_s"],
                "PROVIDER", "provider timeout exceeded")
        require(all(receipt["schema"] not in PRIVATE for receipt in result["receipts"]),
                "PRIVACY", "private receipt in public result")
        if result["status"] == "OK":
            self._route(request["slot"], request["inputs"], result["outputs"])

    def validate_closure(self, name, payload, records, *, custody=False):
        """Resolve typed dependencies from a caller-provided digest-to-record map."""
        seen = set()
        active = set()

        def resolve(link):
            kind, digest = link["schema"], link["sha256"]
            require(custody or kind not in PRIVATE, "PRIVACY", "private dependency in public closure")
            require(digest in records and canonical_digest(records[digest]) == digest,
                    "RECORD_REF", f"missing or changed record {kind}")
            point = (kind, digest)
            require(point not in active, "RECORD_REF", "cyclic record closure")
            if point not in seen:
                active.add(point)
                visit(kind, records[digest])
                active.remove(point)
                seen.add(point)
            return records[digest]

        def visit(kind, value):
            require(custody or kind not in PRIVATE, "PRIVACY", "private record in public closure")
            self.validate(kind, value)
            for link in record_refs(value):
                resolve(link)
            for witness in walked_witnesses(value):
                self._witness(witness, resolve)
            self._record_semantics(kind, value, resolve)

        visit(name, payload)

    @staticmethod
    def _witness(witness, resolve):
        if witness["origin"] == "frame":
            recording = resolve(witness["recording"])
            frame = resolve(witness["frame"])
            source = recording["provenance"]["video_sha256"]
            require(witness["clock"]["time_base"] == recording["time_base"]
                    and witness["occurrence"] == frame["provenance"]
                    and witness["occurrence"]["video_sha256"] == source
                    and witness["occurrence"]["label"] == "observed"
                    and witness["lineage_sha256"] == source,
                    "WITNESS", "foreign or invented frame provenance")
            base = recording["time_base"]
            start = recording["start_pts"]
            if isinstance(start, int):
                witness_ms = Fraction(1000 * (witness["clock"]["pts_ticks"] - min(0, start))
                                      * base["numerator"], base["denominator"])
                require(abs(float(witness_ms) - witness["occurrence"]["pts_ms"]) <= 1e-9,
                        "WITNESS", "frame and rational clock differ")
        else:
            artifact = resolve(witness["artifact"])
            lift = resolve(witness["lift"])
            members = unique_index(artifact["members"], "id", "BINARY_RANGE")
            member = members.get(witness["member_id"])
            require(member is not None and compatible_range(witness["range"], member["range"])
                    and lift["artifact"] == witness["artifact"] and lift["decoder"] == witness["decoder"]
                    and witness["lineage_sha256"] == artifact["source"]["sha256"],
                    "WITNESS", "foreign binary range, decoder or lineage")
            require(any(row["member_id"] == member["id"] and row["range"] == witness["range"]
                        for row in lift["decoded"] + lift["opaque"]),
                    "WITNESS", "range is absent from the lift")

    def _record_semantics(self, kind, value, resolve):
        if kind == "rea.binary.artifact/v1":
            by_id = unique_index(value["members"], "id", "BINARY_RANGE")
            unique_index(value["members"], "path", "BINARY_RANGE")
            end = 0
            for member in sorted(by_id.values(), key=lambda x: x["range"]["offset"]):
                span = member["range"]
                require(end <= span["offset"] <= value["byte_size"] - span["length"],
                        "BINARY_RANGE", "overlapping or out-of-bounds member")
                end = span["offset"] + span["length"]
                require((member["reason"] is not None) == (member["state"] == "opaque"),
                        "BINARY_RANGE", "opaque member reason mismatch")
        elif kind == "rea.binary.lift/v1":
            artifact = resolve(value["artifact"])
            members = unique_index(artifact["members"], "id", "BINARY_RANGE")
            for observed in value["decoded"] + value["opaque"]:
                member = members.get(observed["member_id"])
                require(member is not None and compatible_range(observed["range"], member["range"]),
                        "BINARY_RANGE", "decoder cited an unadmitted member span")
            require(all(members[row["member_id"]]["state"] == "selected" for row in value["decoded"]),
                    "BINARY_RANGE", "decoder promoted an opaque member")
            require(all(any(row["member_id"] == member["id"] and row["range"] == member["range"]
                            for row in value["opaque"])
                        for member in members.values() if member["state"] == "opaque"),
                    "BINARY_RANGE", "opaque member was discarded")
        elif kind == "rea.frame_set/v1":
            source = resolve(value["source"])
            previous_index, previous_pts = -1, None
            for item in value["occurrences"]:
                provenance = resolve(item["frame"])["provenance"]
                index = provenance["frame_index"]
                pts = item["clock"]["pts_ticks"]
                require(previous_index < index and (previous_pts is None or previous_pts <= pts)
                        and source["time_base"] == item["clock"]["time_base"]
                        and source["provenance"]["video_sha256"] == provenance["video_sha256"]
                        and item["artifact"]["sha256"] == provenance["frame_sha256"],
                        "WITNESS", "foreign or unordered frame set occurrence")
                previous_index, previous_pts = index, pts
        elif kind == "rea.game.evidence/v1":
            self._evidence(value, resolve)
        elif kind in STAGE_PARENT:
            self._stage(kind, value, resolve)
        elif kind == "rea.game.physics_fit/v1":
            design = value["design"]
            require(design["rank"] <= design["columns"]
                    and not set(design["fit_episodes"]) & set(design["held_out_episodes"])
                    and all(row["lower"] <= row["upper"] for row in value["uncertainty"]["bounds"]),
                    "FIT", "invalid rank, uncertainty interval or overlapping split")
        elif kind == "rea.custody.phase_receipt/v1":
            self._phase_receipt(value, resolve(value["policy"]))

    @staticmethod
    def _evidence(value, resolve):
        facts = unique_index(value["facts"], "id", "FACT")
        join = value.get("join_key")
        lineage = {"frame": set(), "binary": set()}
        scenes = [resolve(scene) for scene in value["scenes"]]
        if value["game_model"]:
            model = resolve(value["game_model"])
            require({row["video_sha256"] for row in model["evidence"]}
                    <= {scene["video_sha256"] for scene in scenes},
                    "JOIN", "game model has foreign scene evidence")
        for fact in facts.values():
            witness = fact["witness"]
            origin = witness["origin"]
            lineage[origin].add(witness["lineage_sha256"])
            if origin == "binary":
                require(witness["artifact"] in value["artifacts"] and witness["lift"] in value["lifts"]
                        and witness["artifact"] in fact["dependencies"] and witness["lift"] in fact["dependencies"]
                        and resolve(witness["artifact"])["target_instance"] == value["target_instance"],
                        "JOIN", "binary fact or dependency outside this evidence")
                entries = resolve(witness["lift"])
                decoded = any(row["member_id"] == witness["member_id"] and row["range"] == witness["range"]
                              and row["kind"] == fact["kind"] and row["value"] == fact["value"]
                              for row in entries["decoded"])
                limitation = fact["kind"] == "limitation" and fact["value"] is None
                require(decoded or limitation, "FACT", "unqualified binary value")
            else:
                require(witness["recording"] in fact["dependencies"]
                        and any(Catalog._scene_occurrence(scene, witness) for scene in scenes),
                        "JOIN", "frame fact or dependency outside this evidence")
            if join:
                require(join == fact.get("join_key") and join["target_instance"] == value["target_instance"],
                        "JOIN", "proposition join differs from a source witness")
                bind = witness["lineage_sha256"] if origin == "binary" else witness["clock"]["transform_sha256"]
                key = "build_sha256" if origin == "binary" else "transform_sha256"
                require(join[key] == bind, "JOIN", "build or clock transform mismatch")
            else:
                require("join_key" not in fact, "JOIN", "unmatched evidence claims a join")
        if join:
            require(lineage["frame"] and lineage["binary"] and not lineage["frame"] & lineage["binary"],
                    "JOIN", "matched lanes require independent source lineages")
        unique_index(value["conflicts"], "id", "FACT")
        require(all(set(conflict["fact_ids"]) <= facts.keys() for conflict in value["conflicts"]),
                "FACT", "conflict references an unknown fact")

    @staticmethod
    def _scene_occurrence(scene, witness):
        occurrence = witness["occurrence"]
        clock = witness["clock"]
        return scene["video_sha256"] == occurrence["video_sha256"] and any(
            shot["geometry_epoch"] == clock["geometry_epoch"]
            and occurrence in shot["level_map"]["evidence"]
            and any(row["frame_index"] == occurrence["frame_index"] and row["pts_ticks"] == clock["pts_ticks"]
                    for row in shot["observations"])
            for shot in scene["shots"])

    @staticmethod
    def _stage(kind, value, resolve):
        parent_ref = value[STAGE_PARENT[kind]]
        parent = resolve(parent_ref)
        require(value["mode"] == parent["mode"], "BRANCH", "stage changed source mode")
        model_kind = "rea.software_model/v2"
        evidence_kind = "rea.game.evidence/v1"
        chain = parent
        chain_kind = parent_ref["schema"]
        model_ref = None
        while chain_kind != evidence_kind:
            if chain_kind == "rea.spec/v2":
                model_ref = chain["model"]
            next_ref = chain[STAGE_PARENT[chain_kind]]
            chain, chain_kind = resolve(next_ref), next_ref["schema"]
        facts = {fact["id"]: fact for fact in chain["facts"]}
        witnesses = {canonical_digest(fact["witness"]) for fact in facts.values()}
        require(all(canonical_digest(w) in witnesses for w in walked_witnesses(value)),
                "WITNESS", "stage invented source provenance")
        claims_key = {"rea.software_model/v2": "obligations", "rea.spec/v2": "requirements",
                      "rea.reimpl_plan/v2": "steps", "rea.report/v2": "claims"}.get(kind)
        if claims_key:
            claims = unique_index(value[claims_key], "id", "FACT")
            require(all(set(claim["support"]) <= facts.keys() for claim in claims.values()),
                    "FACT", "claim references unknown source facts")
        if kind == model_kind:
            require(value["game_model"] == chain["game_model"] and value["scenes"] == chain["scenes"]
                    and value["conflicts"] == chain["conflicts"],
                    "BRANCH", "model changed source bindings or erased conflicts")
            extracted = {key for key, fact in facts.items() if fact["witness"]["origin"] == "binary"}
            require(set(value["extracted_facts"]) == extracted, "FACT", "model changed extracted fact set")
            if value["game_model"]:
                model = resolve(value["game_model"])
                require({link["sha256"] for link in value["entities"]} == set(model["entities"])
                        and {link["sha256"] for link in value["events"]} == set(model["events"]),
                        "RECORD_REF", "model entity or event inventory differs")
            if value["trace"]:
                trace = resolve(value["trace"])
                require(resolve(trace["rules"])["model"] == value["game_model"]
                        and trace["reset_sha256"] == value["reset_sha256"]
                        and trace["actions_sha256"] == value["actions_sha256"],
                        "RECORD_REF", "foreign replay trace")
        elif kind == "rea.verification/v2":
            checks = unique_index(value["checks"], "id", "SCORE")
            require(value["status"] != "passed" or all(row["passed"] for row in checks.values()),
                    "SCORE", "passing verification has a failed check")
        elif kind == "rea.report/v2":
            require(model_ref is not None and value["model"] == model_ref,
                    "RECORD_REF", "report names a foreign model")

    def _phase_receipt(self, value, policy):
        require(all(value[k] == policy[k] for k in PHASE_BINDS)
                and value["extension_sha256"] == self.extension_sha256,
                "PHASE", "phase, parent or policy digest binding differs")
        score = value["score"]
        n = score["S"] + score["F"] + score["U"]
        require(n == score["N"] == score["eligible"] <= score["attempted"]
                and score["interval"] == ([score["S"] / n, (score["S"] + score["U"]) / n] if n else None),
                "SCORE", "score denominator or interval differs")
        mandatory = unique_index(value["mandatory_cases"], "id", "SCORE")
        mutants = unique_index(value["critical_mutants"], "id", "SCORE")
        require(set(mandatory) == set(policy["mandatory_cases"])
                and set(mutants) == set(policy["critical_mutants"])
                and value["M"] == len(mutants) and value["K"] == sum(row["passed"] for row in mutants.values()),
                "SCORE", "mandatory case or critical mutation inventory differs")
        if value["status"] == "PASS":
            require(n > 0 and value["K"] == value["M"] and all(row["passed"] for row in mandatory.values()),
                    "SCORE", "phase PASS is missing mandatory observations or critical kills")
