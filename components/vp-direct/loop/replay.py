"""Adapter for U2b's executable MemoryWeb drafts and the pinned ReplayOwner."""
from copy import deepcopy
from hashlib import sha256
from time import monotonic_ns

from recorder.compiler import _validate_draft, _validate_memoryweb
from recorder.store.memoryweb_store import canonical_bytes
from rewind.machine.transitions import require


def pin(document):
    return sha256(canonical_bytes(document)).hexdigest()


def validate(workflow, owner):
    _validate_draft(workflow, owner)
    require(workflow["script_ref"] is None and not workflow["text_slots"] and
            all(step["seq"] == i and step["protected"] and step["verify"]["mode"] == "state_signal"
                and not step["verify"]["fallback_verifiers"]
                for i, step in enumerate(workflow["steps"], 1)), "OUT_OF_SUPPORT")


def projection(workflow, index):
    """Project one protected step, retaining its guards, pace and verification.

    A ReplayOwner always starts at seq 1. The durable flight binds that local
    sequence to the original sequence and full revision before any action.
    """
    doc = deepcopy(workflow)
    step = doc["steps"][index]
    step["seq"] = 1
    doc["steps"] = [step]
    if index + 1 < len(workflow["steps"]):
        doc["oracle"]["postconditions"] = [step["verify"]["predicate"]]
    return doc



def selector_from_plan(step, analysis):
    """Translate R2's admitted Wender click to a name re-grounded by ReplayOwner."""
    action, _, state, node = analysis["plan"]
    name = node.get("name")
    button = {1: "left", 2: "middle", 3: "right"}[step["action"]["button"]]
    if (action.get("type") != "click" or action.get("button") != button or
            not isinstance(name, str) or not name.strip() or
            node.get("actionable") is not True or node["id"] not in state["actionable"]):
        return None
    matches = [row for row in state["nodes"] if row["name"] == name and row["actionable"]]
    if matches != [node]:
        return None
    target = deepcopy(step["target"])
    anchor = "ax:" + name
    target.update(anchor_ref=anchor, required_anchor_refs=[anchor])
    target["frame_predicate"]["bindings"][0]["value"] = name
    return target


def check_owner(owner, doc):
    require(owner.corpus.workflow == doc and owner.corpus.intact() and
            owner.corpus.pins[owner.corpus.path] == pin(doc), "SEAL_MISMATCH")


def run_one(owner, doc, surface):
    """Return a disposition only from the selected owner receipt / intent journal."""
    check_owner(owner, doc)
    result = owner.run()
    if result.verdict_code == "INPUT_UNCERTAIN":
        return "UNCERTAIN_OUTCOME"
    if result.handoff_path or result.verdict_code == "HUMAN_HANDOFF":
        return "HUMAN_NEEDED"
    run = result.summary
    if run is not None:
        _validate_memoryweb("run", run)
        require(owner.output.read_bytes() == canonical_bytes(run) and
                run["workflow_sha256"] == pin(doc) and run["workflow_id"] == doc["id"] and
                run["site_id"] == doc["site_id"] and run["steps_total"] == 1 and
                run["rc"] == result.rc and run["verdict_code"] == result.verdict_code,
                "SEAL_MISMATCH")
        if (result.rc == 0 and result.verdict_code == "VERIFIED" and owner.final_pass and
                run["failed_step_seq"] is None and
                run["steps_verified"] + run["steps_skipped_satisfied"] == 1):
            return "VERIFIED"
    if any(owner.gate.strict_load(raw)["kind"] == "intent" for raw in owner.events):
        return "UNCERTAIN_OUTCOME"
    if run and run["failed_step_seq"] == 1 and result.rc:
        if result.verdict_code in ("LOW_CONFIDENCE", "DEADLINE"):
            return "FAILED_STEP"
        # Wender maps ReplayOwner's MW_TARGET_FRAME exception to OWNER_FAILURE.
        # Narrow that generic failure using a new scoped AX read before repair.
        if result.verdict_code == "OUT_OF_SUPPORT" and result.diagnostic == "OWNER_FAILURE":
            owner.check("step", doc, owner.scope, owner.authority)
            start = monotonic_ns()
            predicate = doc["steps"][0]["target"]["frame_predicate"]
            try:
                semantics(owner, doc, [predicate], start, start + 5_000_000_000,
                          surface, expected=False)
            except Exception:
                return "HUMAN_NEEDED"
            return "FAILED_STEP"
    return "HUMAN_NEEDED"


def semantics(owner, doc, predicates, start, deadline, surface, *, expected=True):
    """Positive, scoped AX publications obtained inside this observation-only pass."""
    check_owner(owner, doc)
    require(predicates and monotonic_ns() <= deadline, "UNCERTAIN_OUTCOME")
    publications = []
    for predicate in predicates:
        require(owner.predicate_supported(predicate), "OUT_OF_SUPPORT")
        value = owner.acquire(predicate, deadline)
        require(owner.validate(value).result is owner.Truth.TRUE and value.value is expected and
                value.complete is True and value.missing_reason is None and
                value.output_kind == "boolean" and value.unit == "truth" and
                value.capability_id == predicate["capability_id"] and
                start <= value.capture_start_ns <= value.capture_end_ns <= monotonic_ns() <= deadline and
                owner.last_publication_frame["surface_id"] == surface, "UNCERTAIN_OUTCOME")
        publications.append((value.publication, value.capture_start_ns, pin(predicate)))
    return pin(publications)


class ReadOnlyBackend:
    def __init__(self, backend):
        self.backend = backend

    def call(self, stage, **args):
        require(stage in ("ownership", "network_context", "offline_proof", "observe",
                          "perceive", "state", "ax"), "NO_LEASE")
        return self.backend.call(stage, **args)


class DispatchFence:
    """Recheck custody on the actual submit, including a single-use send gate."""
    def __init__(self, backend, goal, claim, lifecycle, observation, *, session_check):
        self.backend, self.goal, self.claim = backend, goal, claim
        self.lifecycle, self.observation = lifecycle, observation
        self.session_check = session_check
        self.sent = False

    def call(self, stage, **args):
        if stage != "propose":
            return self.backend.call(stage, **args)
        require(not self.sent and self.goal.status() == self.claim and self.session_check(), "NO_LEASE")
        require(args["sensed"]["frame"]["surface_id"] == self.claim["surface_id"], "STALE_FRAME")
        self.sent = True
        return self.lifecycle.run_agent("act", self.observation,
            lambda: self.backend.call("propose", **args))
