"""Local C6 lifecycle. U3c is the authoritative transition log."""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import time

from jsonschema import ValidationError

from bus.journal import Journal, JournalError
from control.core.control import digest
from schemas.validation import _store, canonical_bytes, validate_contract

from .fixture import HASH, ForgeError, append_row, file_sha256, pinned_request, read_spool, verify_attempts


TOOL = "vp.forge_tool/v1"
RECEIPT = "vp.forge_receipt/v1"
JOURNAL_AGENT = "vp-forge"
RISKS = tuple(_store()["urn:vp.capability:v1"]["properties"]["risk_class"]["enum"])
GATES = {"schema": "pass", "oracle": "not_run", "negative_control": "not_run"}
VERBS = frozenset("vp_forge_" + name for name in ("propose", "validate", "status", "list", "admit"))
BOUND_FIELDS = {"session_id", "capsule_id", "target", "observation_id", "profile_id"}
WORKFLOW_REPLAY = "vp_replay_workflow"


def _predicate(predicate, reply):
    """Return None when the necessary observation is unavailable."""
    result = reply["result"]
    op = predicate.get("op")
    if op is None:
        if any(key not in result or type(result[key]) is not type(value) for key, value in predicate.items()):
            return None
        return all(result[key] == value for key, value in predicate.items())
    if op == "code_eq":
        return reply["code"] == predicate["code"]
    if op == "field_eq":
        field, value = predicate["field"], predicate["value"]
        if field not in result:
            return None
        actual = result[field]
        if isinstance(actual, bool) or isinstance(value, bool):
            return type(actual) is type(value) and actual == value
        return actual == value
    field = {"digest_eq": predicate.get("field"), "ax_node_exists": "ax_stable_sha256",
             "ax_text_sha256_eq": "ax_text_sha256", "frame_differs": "frame_sha256"}[op]
    actual = result.get(field)
    if not isinstance(actual, str) or not HASH.fullmatch(actual):
        return None
    if op == "frame_differs":
        return actual != predicate["frame_sha256"]
    return actual == (predicate["ax_stable_sha256"] if op == "ax_node_exists" else predicate["sha256"])


def _seal(event, run_id):
    receipt = dict(event["payload"], lineage={
        "chain": "lineage.event/v1", "run_id": run_id, "lamport": event["lamport"],
        "prev_hash": event["prev_sha256"], "event_hash": digest(event),
    })
    validate_contract(RECEIPT, receipt)
    return receipt


def _verify_validation_requests(directory, names, tool, proof, fixture, payload):
    parent_sha = proof["request"]
    if payload["transition"]["from"] == "quarantined" and parent_sha != payload["effective_request_sha256"]:
        raise ValueError("validation proof refers to a different request")
    for name in names:
        if not name.startswith("request-"):
            continue
        binding = json.loads((directory / name).read_bytes())
        request = binding["request"]
        if digest(request) == parent_sha:
            validate_contract("vp.control.req/v1", request)
            if (name == "request-" + digest(request["request_id"]) + ".json"
                    and request["op"] == "vp_forge_validate" and binding["action"] == "validate"
                    and binding["inputs"] == {"tool": payload["tool"], "tool_sha256": payload["tool_sha256"]}):
                break
    else:
        raise ValueError("validation request is absent from the linked evidence")

    steps = tool["steps"]
    attempts = proof["attempts"]
    if (tool["args_schema"].get("properties") or type(attempts) is not int or not 1 <= attempts <= len(steps)
            or (payload["transition"]["to"] != "rejected" and attempts != len(steps))):
        raise ValueError("validation does not cover the candidate's literal arguments")
    return [pinned_request(step, parent_sha, index, fixture["context"], fixture["allowed"])
            for index, step in enumerate(steps[:attempts])]


def _verify_material(directory, payload, evidence):
    pins = {pin["path"]: pin["sha256"] for pin in evidence}
    for pin in evidence:
        path = directory / pin["path"]
        try:
            if path.parent != directory or path.is_symlink() or file_sha256(path) != pin["sha256"]:
                raise ValueError(pin["path"])
        except (OSError, ValueError) as error:
            raise ForgeError("BLOCKED_SOURCE", "lineage evidence is missing or changed") from error

    try:
        tool_sha = payload["tool_sha256"]
        tool_name = f"tool-{tool_sha}.json"
        request_name = "request-" + digest(payload["request_id"]) + ".json"
        if request_name not in pins or pins.get(tool_name) != tool_sha:
            raise ValueError("missing tool or effective request link")
        tool = json.loads((directory / tool_name).read_bytes())
        validate_contract(TOOL, tool)
        if (digest(tool) != tool_sha or tool["tool"] != payload["tool"]
                or [digest(step) for step in tool["steps"]] != payload["steps_sha256"]
                or list(dict.fromkeys([tool_sha, *tool["parents"]])) != payload["parents"]):
            raise ValueError("receipt claims differ from the canonical candidate")
        if payload["workflow_pin"] is not None or any(step["op"] == WORKFLOW_REPLAY for step in tool["steps"]):
            raise ValueError("workflow publication receipt has no verified replay binding")
        request_file = json.loads((directory / request_name).read_bytes())
        request = request_file["request"]
        validate_contract("vp.control.req/v1", request)
        if (digest(request) != payload["effective_request_sha256"]
                or payload["receipt_id"] != "c6-" + payload["effective_request_sha256"]
                or request["request_id"] != payload["request_id"] or request["context"] != payload["actor"]
                or request_file["inputs"]["tool_sha256"] != tool_sha):
            raise ValueError("effective request differs")
        fixture_name, = (name for name in pins if name.startswith("fixture-"))
        fixture = json.loads((directory / fixture_name).read_bytes())
        if digest(fixture["site"]) != payload["fixture"]["fixture_sha256"]:
            raise ValueError("fixture digest differs")
        if payload["transition"]["from"] == "proposed":
            route_sha = request_file["inputs"]["router_sha256"]
            if pins.get(f"router-{route_sha}.json") != route_sha:
                raise ValueError("router evidence is missing")
        else:
            proof_name, = (name for name in pins if name.startswith("validation-"))
            proof = json.loads((directory / proof_name).read_bytes())
            spool = proof["spool"]
            if (proof["source"] != tool_sha or proof["executor"] != digest(fixture)
                    or proof["gates"] != payload["gates"] or pins.get(spool["path"]) != spool["sha256"]
                    or spool["path"] != "attempts-" + proof["request"] + ".jsonl"
                    or proof_name != "validation-" + proof["request"] + ".json"):
                raise ValueError("validation proof lacks its source, executor, request or spool")
            expected = _verify_validation_requests(directory, pins, tool, proof, fixture, payload)
            if [digest(child) for child in expected] != proof["ordered_requests"]:
                raise ValueError("validation proof does not pin the candidate's child arguments")
            rows = read_spool(directory / spool["path"])
            if (len(rows) != 2 * proof["attempts"] or len(proof["step_checks"]) != proof["attempts"]
                    or [row["phase"] for row in rows] != ["started", "verified"] * proof["attempts"]
                    or [row["reply_sha256"] for row in rows[1::2]] != proof["ordered_observations"]
                    or any(row["request_id"] != child["request_id"] or row["index"] != index
                           or row["request_sha256"] != proof["ordered_requests"][index]
                           for index, child in enumerate(expected) for row in rows[2 * index:2 * index + 2])):
                raise ValueError("attempt count or observation order differs")
        cap_sha = payload["capability_sha256"]
        if cap_sha is not None and pins.get(f"capability-{cap_sha}.json") != cap_sha:
            raise ValueError("publication capability link is absent")
    except (OSError, ValueError, TypeError, KeyError, ValidationError):
        raise ForgeError("BLOCKED_SOURCE", "receipt material does not bind its claims") from None


def _verify_event_material(directory, event):
    payload = event["payload"]
    if (event["agent"] != JOURNAL_AGENT
            or event["op_id"] != "c6-" + payload["effective_request_sha256"][:60]):
        raise ForgeError("BLOCKED_SOURCE", "journal event is not bound to the forge request")
    _verify_material(directory, payload, event["evidence"])


def verify_receipt(directory, receipt):
    """Read back a sealed receipt and its linked material without entering a backend."""
    directory = Path(directory)
    try:
        validate_contract(RECEIPT, receipt)
        run = json.loads((directory / "run.json").read_bytes())
        events = Journal(directory).verify()
        event = events[receipt["lineage"]["lamport"] - 1]
        if event["kind"] != RECEIPT or _seal(event, run["run_id"]) != receipt:
            raise ValueError("receipt does not match its durable event")
    except (OSError, ValueError, ValidationError, TypeError, KeyError, IndexError):
        raise ForgeError("BLOCKED_SOURCE", "sealed receipt does not verify") from None
    _verify_event_material(directory, event)


class Forge:
    """Operate on provisioned actors and an isolated F1 executor in a trusted state directory."""

    def __init__(self, directory, *, run_id, fixture, current_manifest_generation=None):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id is required")
        self.root = Path(directory)
        self.run_id = run_id
        self.fixture = fixture
        self.current_manifest_generation = current_manifest_generation
        self.registry = fixture.dispatcher.registry
        self.journal = Journal(self.root)

    @contextmanager
    def _locked(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / "forge.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            identity = self.root / "run.json"
            self._persist(identity, {"run_id": self.run_id})
            yield

    def _persist(self, path, content):
        expected = canonical_bytes(content)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != expected:
                raise ForgeError("BLOCKED_SOURCE", "existing forge material differs")
        else:
            with os.fdopen(fd, "wb") as stream:
                append_row(stream, content)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return self._evidence(path)

    def _evidence(self, path):
        return {"path": path.name, "sha256": file_sha256(path)}

    def _events(self):
        try:
            return [event for event in self.journal.verify() if event["kind"] == RECEIPT]
        except (JournalError, ValidationError, OSError) as error:
            raise ForgeError("BLOCKED_SOURCE", "forge lineage did not verify") from error

    def _policies(self, tool):
        policies = []
        for step in tool["steps"]:
            op = step["op"]
            entry = self.registry.get(op)
            if (entry is None or not entry["supported"] or op in VERBS
                    or entry["origin"] == "forged" or op == tool["tool"]):
                raise ForgeError("FORGE_DEPENDENCY_UNADMITTED", op)
            if op == WORKFLOW_REPLAY:
                raise ForgeError(
                    "SCHEMA_GAP",
                    "vp.forge_tool/v1 steps[].args and vp.control.req/v1 args for vp_replay_workflow "
                    "do not admit publication_receipt_sha256",
                )
            if "op_table" in entry:
                selector = step["args"].get("op")
                policy = next((row for row in entry["op_table"] if row["op"] == selector), None)
                if policy is None or not policy["supported"] or policy["binding"]["kind"] == "none":
                    raise ForgeError("FORGE_DEPENDENCY_UNADMITTED", op)
                # Keep tool-level operator restrictions even if a child row is less restrictive.
                entry = dict(policy, operator_only=policy["operator_only"] or entry["operator_only"])
            policies.append((op, entry))
        return policies

    @staticmethod
    def _risk(tool, policies):
        child_max = max((policy["risk_class"] for _, policy in policies), key=RISKS.index)
        obligations = set(tool["obligations"])
        obligations.update(f"operator_only:{op}" for op, policy in policies if policy["operator_only"])
        obligations.update(f"pace_and_risk:{op}" for op, _ in policies)
        levels = [tool["risk_class"], child_max]
        raised = []
        if len(policies) > 1:
            raised.append("sequencing")
            levels.append("enviar" if RISKS.index(child_max) >= RISKS.index("preparar") else "preparar")
        if any(BOUND_FIELDS.intersection(step["args"]) for step in tool["steps"]):
            raised.append("binding")
            levels.append("preparar")
        composed = max(levels, key=RISKS.index)
        return {"risk_class": composed, "child_max": child_max, "raised_by": raised,
                "obligations": sorted(obligations)}

    def _request(self, verb, request_id, actor):
        envelope = {
            "schema": "vp.control.req/v1", "op": verb, "request_id": request_id,
            "context": {"caller": actor.caller, "agent_instance": actor.agent_instance},
            "args": {}, "dry_run": False,
        }
        try:
            validate_contract("vp.control.req/v1", envelope)
        except ValidationError:
            raise ForgeError("FORGE_SCHEMA_INVALID", "invalid forge request or lane identity") from None
        return envelope

    def _operation(self, request, action, inputs):
        request_sha = digest(request)
        name = self.root / ("request-" + digest(request["request_id"]) + ".json")
        binding = {"request": request, "action": action, "inputs": inputs}
        if name.is_symlink():
            raise ForgeError("BLOCKED_SOURCE", "request evidence is a symbolic link")
        if name.exists() and name.read_bytes() != canonical_bytes(binding):
            raise ForgeError("REQUEST_ID_CONFLICT")
        evidence = self._persist(name, binding)
        events = self._events()
        prior = next((event for event in events if event["op_id"] == "c6-" + request_sha[:60]), None)
        if prior is not None:
            if evidence not in prior["evidence"]:
                raise ForgeError("BLOCKED_SOURCE", "prior request lacks its source link")
            _verify_event_material(self.root, prior)
        return request_sha, evidence, events, prior

    def _candidate(self, events, tool_name, expected_sha):
        last = next((event for event in reversed(events) if event["payload"]["tool"] == tool_name), None)
        if last is None:
            raise ForgeError("FORGE_DEPENDENCY_UNADMITTED", tool_name)
        if last["payload"]["tool_sha256"] != expected_sha:
            raise ForgeError("REQUEST_ID_CONFLICT", "candidate digest changed")
        _verify_event_material(self.root, last)
        path = self.root / ("tool-" + expected_sha + ".json")
        try:
            if path.is_symlink() or file_sha256(path) != expected_sha:
                raise ValueError("candidate digest mismatch")
            tool = json.loads(path.read_bytes())
            validate_contract(TOOL, tool)
        except (OSError, ValueError, ValidationError):
            raise ForgeError("BLOCKED_SOURCE", "quarantine source did not verify") from None
        return tool, last

    def _issue(self, request, request_sha, tool, *, previous, target, by, evidence,
               gates, code="OK", fingerprint, capability=None, generation=None, unverified=()):
        risk = self._risk(tool, self._policies(tool))
        tool_sha = digest(tool)
        payload = {
            "schema": RECEIPT, "receipt_id": "c6-" + request_sha,
            "tool": tool["tool"], "tool_sha256": tool_sha,
            "transition": {"from": previous, "to": target}, "by": by,
            "actor": request["context"], "request_id": request["request_id"],
            "effective_request_sha256": request_sha, "gates": gates, "code": code,
            "fixture": {"site": "f1", "fixture_sha256": digest(fingerprint["site"])},
            "risk": risk, "steps_sha256": [digest(step) for step in tool["steps"]],
            "capability_sha256": digest(capability) if capability is not None else None,
            "manifest_generation": generation, "workflow_pin": None,
            "parents": list(dict.fromkeys([tool_sha, *tool["parents"]])),
            "issued_mono_ns": time.monotonic_ns(), "not_verified": list(unverified),
        }
        _verify_material(self.root, payload, evidence)
        try:
            ref = self.journal.append(agent=JOURNAL_AGENT, op_id="c6-" + request_sha[:60],
                                      kind=RECEIPT, payload=payload, evidence=evidence)
            receipt = dict(payload, lineage={"chain": "lineage.event/v1", "run_id": self.run_id, **ref})
            verify_receipt(self.root, receipt)
        except (JournalError, ValidationError, OSError, KeyError) as error:
            raise ForgeError("BLOCKED_SOURCE", "forge receipt could not be sealed") from error
        return receipt

    def propose(self, tool, *, request_id, actor, route_reply):
        """Quarantine a declarative proposal after a true absence recommendation."""
        try:
            tool = deepcopy(tool)
            validate_contract(TOOL, tool)
            if tool["state"] != "proposed" or tool["proposed_by"] != actor.agent_instance:
                raise ValueError("proposal actor or state")
            validate_contract("vp.tool_route.reply/v1", route_reply)
            digest(tool)
        except ValidationError as error:
            if (len(error.absolute_path) == 3 and error.absolute_path[0] == "steps"
                    and error.absolute_path[-1] == "op" and isinstance(error.instance, str)):
                raise ForgeError("FORGE_DEPENDENCY_UNADMITTED", error.instance) from None
            raise ForgeError("FORGE_SCHEMA_INVALID") from None
        except (TypeError, ValueError, RecursionError):
            raise ForgeError("FORGE_SCHEMA_INVALID") from None
        if (route_reply["verdict"], route_reply.get("reason")) != ("ABSTAIN", "absent_capability"):
            raise ForgeError("FORGE_DEPENDENCY_UNADMITTED", "router did not report an absent capability")
        if self.registry.get(tool["tool"]) is not None:
            raise ForgeError("FORGE_DEPENDENCY_UNADMITTED", "proposal shadows a registered capability")
        policies = self._policies(tool)
        fingerprint = self.fixture.fingerprint(policies)
        request = self._request("vp_forge_propose", request_id, actor)
        with self._locked():
            tool_sha = digest(tool)
            req_sha, req_pin, events, prior = self._operation(request, "propose", {
                "tool_sha256": tool_sha, "router_sha256": digest(route_reply),
            })
            if prior is not None:
                return _seal(prior, self.run_id)
            if any(event["payload"]["tool"] == tool["tool"] for event in events):
                raise ForgeError("REQUEST_ID_CONFLICT", "tool name already has a lifecycle")
            tool_pin = self._persist(self.root / ("tool-" + tool_sha + ".json"), tool)
            fixture_pin = self._persist(self.root / ("fixture-" + digest(fingerprint) + ".json"), fingerprint)
            router_pin = self._persist(self.root / ("router-" + digest(route_reply) + ".json"), route_reply)
            return self._issue(request, req_sha, tool, previous="proposed", target="quarantined",
                               by=actor.caller, evidence=[req_pin, tool_pin, fixture_pin, router_pin],
                               gates=GATES, fingerprint=fingerprint)

    def validate(self, tool_name, tool_sha256, *, request_id, actor):
        request = self._request("vp_forge_validate", request_id, actor)
        with self._locked():
            req_sha, req_pin, events, prior = self._operation(request, "validate", {
                "tool": tool_name, "tool_sha256": tool_sha256,
            })
            if prior is not None:
                return _seal(prior, self.run_id)
            tool, last = self._candidate(events, tool_name, tool_sha256)
            if last["payload"]["transition"]["to"] != "quarantined":
                raise ForgeError("FORGE_GATE_FAILED", "expected a quarantined candidate")
            if tool["args_schema"].get("properties"):
                raise ForgeError(
                    "SCHEMA_GAP",
                    "vp.forge_tool/v1 steps[].args has no parameter-reference binding to args_schema "
                    "and vp.control.req/v1 args for vp_forge_validate has no runtime argument values",
                )
            policies = self._policies(tool)
            before = self.fixture.fingerprint(policies)
            expected_pin = {"path": "fixture-" + digest(before) + ".json", "sha256": digest(before)}
            if expected_pin not in last["evidence"]:
                raise ForgeError("BLOCKED_SOURCE", "fixture or executor changed since quarantine")
            requests = [pinned_request(step, req_sha, index, before["context"], before["allowed"])
                        for index, step in enumerate(tool["steps"])]
            spool_path = self.root / ("attempts-" + req_sha + ".jsonl")
            replies = []
            try:
                fd = os.open(spool_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError as error:
                raise ForgeError("BLOCKED_SOURCE", "validation attempt already exists") from error
            with os.fdopen(fd, "wb") as spool:
                for index, child in enumerate(requests):
                    reply = self.fixture.dispatch(child, index, spool)
                    replies.append(reply)
                    if reply is None or _predicate(tool["steps"][index]["assert"], reply) is not True:
                        break
            rows = read_spool(spool_path)
            verify_attempts(rows, requests[:len(replies)], replies)
            if before != self.fixture.fingerprint(self._policies(tool)):
                raise ForgeError("BLOCKED_SOURCE", "source or executor changed during validation")
            checks = [_predicate(step["assert"], reply) for step, reply in zip(tool["steps"], replies)]
            if None in checks:
                raise ForgeError("BLOCKED_SOURCE", "step postcondition data is unavailable")
            gates = dict(GATES, oracle="fail")
            if len(replies) == len(requests) and all(checks):
                oracle = _predicate(tool["oracle"], replies[-1])
                negative = _predicate(tool["negative_control"], replies[-1])
                if None in (oracle, negative):
                    raise ForgeError("BLOCKED_SOURCE", "postcondition data is unavailable")
                gates.update(oracle="pass" if oracle else "fail",
                             negative_control="passed_unexpectedly" if negative else "failed_as_required")
            passes = gates["oracle"] == "pass" and gates["negative_control"] == "failed_as_required"
            proof = {"source": tool_sha256, "executor": digest(before), "request": req_sha,
                     "ordered_requests": [digest(child) for child in requests[:len(replies)]],
                     "ordered_observations": [digest(reply) for reply in replies], "attempts": len(replies),
                     "step_checks": checks, "gates": gates, "spool": self._evidence(spool_path)}
            proof_pin = self._persist(self.root / ("validation-" + req_sha + ".json"), proof)
            return self._issue(
                request, req_sha, tool, previous="quarantined", target="validated" if passes else "rejected",
                by="verifier", evidence=[req_pin, *last["evidence"], proof_pin, proof["spool"]],
                gates=gates, code="OK" if passes else "FORGE_GATE_FAILED", fingerprint=before,
            )

    def approve(self, tool_name, tool_sha256, *, request_id, actor):
        return self._operator_transition(tool_name, tool_sha256, request_id, actor)

    def admit(self, tool_name, tool_sha256, *, request_id, actor, capability, manifest_generation):
        return self._operator_transition(tool_name, tool_sha256, request_id, actor,
                                         capability=capability, generation=manifest_generation, admit=True)

    def _operator_transition(self, tool_name, tool_sha, request_id, actor, *,
                             capability=None, generation=None, admit=False):
        if actor.caller != "operator":
            raise ForgeError("FORGE_APPROVAL_REQUIRED")
        destination, previous = ("admitted", "approved") if admit else ("approved", "validated")
        request = self._request("vp_forge_admit", request_id, actor)
        if admit:
            try:
                validate_contract("vp.capability/v1", capability)
            except ValidationError as error:
                if self.registry.get(tool_name) is None and tuple(error.absolute_path) in (
                        ("tool",), ("json_op",)) and error.instance == tool_name:
                    raise ForgeError("SCHEMA_GAP", "new forged tools are absent from the frozen tool enum") from error
                raise ForgeError("FORGE_SCHEMA_INVALID", "invalid capability") from error
        with self._locked():
            req_sha, req_pin, events, prior = self._operation(request, destination, {
                "tool": tool_name, "tool_sha256": tool_sha, "manifest_generation": generation,
                "capability_sha256": digest(capability) if admit else None,
            })
            if prior is not None:
                return _seal(prior, self.run_id)
            tool, last = self._candidate(events, tool_name, tool_sha)
            if last["payload"]["transition"]["to"] != previous:
                raise ForgeError("FORGE_APPROVAL_REQUIRED", f"expected {previous}")
            policies = self._policies(tool)
            before = self.fixture.fingerprint(policies)
            if {"path": "fixture-" + digest(before) + ".json", "sha256": digest(before)} not in last["evidence"]:
                raise ForgeError("BLOCKED_SOURCE", "validation is stale")
            evidence = [req_pin, *last["evidence"]]
            if admit:
                self._check_capability(tool, policies, capability, generation)
                self.fixture.verify_citations(capability["source"])
                evidence.append(self._persist(self.root / ("capability-" + digest(capability) + ".json"),
                                              capability))
            return self._issue(
                request, req_sha, tool, previous=previous, target=destination, by="operator",
                evidence=evidence, gates=last["payload"]["gates"], fingerprint=before,
                capability=capability, generation=generation,
            )

    def _check_capability(self, tool, policies, capability, generation):
        risk = self._risk(tool, policies)
        if type(self.current_manifest_generation) is not int or self.current_manifest_generation < 0:
            raise ForgeError("BLOCKED_SOURCE", "current registry generation is unavailable")
        if type(generation) is not int or generation != self.current_manifest_generation + 1:
            raise ForgeError("FORGE_APPROVAL_REQUIRED", "admission requires the next registry generation")
        if (capability["tool"] != tool["tool"] or capability["json_op"] != tool["tool"]
                or capability["origin"] != "forged" or not capability["supported"]
                or self.registry.get(tool["tool"]) is not None
                or capability["ops"] or "op_table" in capability or "provider" in capability
                or capability["args_schema_sha256"] != digest(tool["args_schema"])
                or RISKS.index(capability["risk_class"]) < RISKS.index(risk["risk_class"])
                or any(policy["operator_only"] for _, policy in policies) and not capability["operator_only"]):
            raise ForgeError("FORGE_SCHEMA_INVALID", "capability does not preserve the approved composition")

    def status(self, tool_name):
        return next((receipt for receipt in self.list() if receipt["tool"] == tool_name), None)

    def list(self):
        with self._locked():
            latest = {event["payload"]["tool"]: event for event in self._events()}
            for event in latest.values():
                _verify_event_material(self.root, event)
            return [_seal(latest[name], self.run_id) for name in sorted(latest)]

    def publication(self, tool_name, *, manifest_generation):
        """Return a sealed admission only to an eligible registry generation."""
        with self._locked():
            events = self._events()
            last = next((event for event in reversed(events) if event["payload"]["tool"] == tool_name), None)
            if last is None or last["payload"]["transition"]["to"] != "admitted":
                return None
            payload = last["payload"]
            if type(manifest_generation) is not int or manifest_generation < payload["manifest_generation"]:
                return None
            existing = self.registry.get(tool_name)
            if existing is not None and digest(existing) != payload["capability_sha256"]:
                raise ForgeError("BLOCKED_SOURCE", "published name conflicts with the live registry")
            tool, _ = self._candidate(events, tool_name, payload["tool_sha256"])
            fingerprint_sha = digest(self.fixture.fingerprint(self._policies(tool)))
            if {"path": f"fixture-{fingerprint_sha}.json", "sha256": fingerprint_sha} not in last["evidence"]:
                raise ForgeError("BLOCKED_SOURCE", "publication validation is stale")
            capability = json.loads((self.root / ("capability-" + payload["capability_sha256"] + ".json")).read_bytes())
            self.fixture.verify_citations(capability["source"])
            return _seal(last, self.run_id)
