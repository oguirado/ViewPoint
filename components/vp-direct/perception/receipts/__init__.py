"""Verify P4 transition receipts against evidence from the trusted owners."""

from copy import deepcopy
from hashlib import sha256
import json
import re

from jsonschema import ValidationError

from bus.journal import JournalError
from perception.lifecycle import RuntimeEvidence, TransitionEvidence
from perception.stream import Observation, StreamToken
from schemas.validation import SCHEMA_DIR, canonical_bytes, validate_contract


CONTRACT = "vp.transition_receipt/v1"


class ReceiptError(ValueError):
    """A refused transition. P7 cannot promote the W8U lane."""

    runtime_state = "HOLD"
    worker_ui = False

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _require(condition, detail, code="HOLD"):
    if not condition:
        raise ReceiptError(code, detail)


def _validate(contract, value):
    try:
        validate_contract(contract, value)
    except KeyError:
        raise ReceiptError("SCHEMA_GAP", contract) from None
    except ValidationError:
        raise ReceiptError("HOLD", f"invalid {contract}") from None


def _broker_states(proof, receipt, waking):
    binding = proof.binding
    _validate("vp.surface_binding/v1", binding)
    _require(type(proof.broker_records) is tuple and proof.broker_records, "broker records absent")
    sequence = 0
    states = [proof.prior_state]
    for row in proof.broker_records:
        _require(type(row) is dict, "missing broker perception record")
        seq = row.get("seq")
        _require(row.get("by") == "broker" and row.get("rec") == "control"
                 and row.get("op") == "perception" and type(seq) is int and seq > sequence,
                 "unordered or non-broker perception record")
        sequence = seq
        state = row.get("perception")
        states.append(state)
    previous = None
    for state in states:
        _validate("vp.perception_state/v1", state)
        _require(state["session_id"] == proof.session_id
                 and all(state[key] == binding[key] for key in
                         ("surface_id", "incarnation", "authority_epoch")),
                 "broker identity differs from binding", "identity_mismatch")
        if previous is not None:
            _require(state["since_mono_ns"] >= previous["since_mono_ns"],
                     "broker time regressed", "STALE_FRAME")
        previous = state
    names = [state["state"] for state in states]
    if waking:
        _require(len(names) == 3 and names[0] in ("OFF", "ASLEEP", "QUARANTINED")
                 and names[1:] == [receipt["from"], "ACTIVE"],
                 "missing broker wake boundary")
        _require(states[1]["stream_generation"] == states[0]["stream_generation"],
                 "generation advanced before fresh wake", "STALE_FRAME")
    else:
        _require(names == [receipt["from"], receipt["to"]]
                 and (names[0], names[1]) in
                 (("ACTIVE", "ASLEEP"), ("ACTIVE", "OFF"), ("ASLEEP", "OFF")),
                 "missing broker rest boundary")
    return states[0], states[-1]


def _verify_demand(proof, receipt, final, waking):
    agent = proof.agent_demand
    _validate("vp.demand/v1", agent)
    snapshot = proof.demand_snapshot
    binding = proof.binding
    _require(type(snapshot) is dict, "demand snapshot absent")
    _require(snapshot.get("surface_id") == agent["surface_id"] == binding["surface_id"]
             and agent["consumer"] == binding["owner"] and agent["interest"] == "VERIFIED",
             "demand identity differs from binding", "identity_mismatch")
    _require(snapshot.get("state") in ("idle", "in_use") and type(snapshot.get("demands")) is list,
             "demand snapshot unsettled")
    _require(sha256(canonical_bytes(snapshot)).hexdigest() == receipt["demand_snapshot_sha256"],
             "demand snapshot digest differs")
    ids = []
    for row in snapshot["demands"]:
        _require(type(row) is dict and type(row.get("active")) is bool
                 and type(row.get("outstanding")) is int and row["outstanding"] >= 0,
                 "demand row lacks obligation evidence")
        demand = {key: value for key, value in row.items() if key not in ("active", "outstanding")}
        _validate("vp.demand/v1", demand)
        _require(demand["surface_id"] == binding["surface_id"], "foreign retained demand")
        ids.append(demand["demand_id"])
        if ids[-1] == agent["demand_id"]:
            _require(waking and demand == agent and row.get("active") is True,
                     "agent demand is stale or still active at rest")
    _require(receipt["retained"] == ids and snapshot["state"] == ("in_use" if ids else "idle")
             and set(final["obligations"]) == set(binding["obligations"]) | set(ids),
             "retained demand differs from broker obligations")
    drained = [] if waking else [agent["demand_id"]]
    _require(receipt["drained"] == drained and (agent["demand_id"] in ids) == waking
             and (not waking or snapshot["state"] == "in_use"),
             "agent demand lacks required transition")


def _verify_observation(observation, stream, binding):
    _require(stream["ring_id"] and isinstance(observation, Observation)
             and isinstance(observation.token, StreamToken),
             "running producer or observation absent", "E_PRODUCER_ABSENT")
    token = observation.token
    _require(all(getattr(token, key) == binding[key] for key in
                 ("endpoint", "surface_id", "incarnation", "authority_epoch", "owner")),
             "observation identity differs from binding", "identity_mismatch")
    _require(stream["state"] == "ACTIVE" and observation.observation_id
             and stream["fresh_observation_id"] == observation.observation_id
             and type(token.generation) is int and token.generation == stream["stream_generation"]
             and type(token.nonce) is str and token.nonce
             and type(observation.render_index) is int and observation.render_index > 0
             and type(observation.frame_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", observation.frame_sha256),
             "observation lacks fresh producer evidence", "STALE_FRAME")


def _verify_stream(proof, receipt, prior, final, waking):
    before, after = proof.stream_before, proof.stream_after
    fields = {"state", "stream_generation", "reserved_bytes", "ring_id", "last_scrubbed_ring",
              "fresh_observation_id"}
    for state, stream in ((prior, before), (final, after)):
        _require(type(stream) is dict and fields <= stream.keys(), "stream snapshot absent")
        _require(type(stream["stream_generation"]) is int
                 and type(stream["reserved_bytes"]) is int and stream["reserved_bytes"] >= 0
                 and (stream["ring_id"] is None or type(stream["ring_id"]) is str and stream["ring_id"]),
                 "invalid stream generation or reservation")
        generation = state["stream_generation"]
        # A failed wake can retire a P3 generation before P4 commits it.
        _require(stream["stream_generation"] == generation
                 or (stream["state"] == "OFF" and stream["stream_generation"] > generation),
                 "stream generation differs from broker", "STALE_FRAME")
    ring = before["ring_id"]
    expected_before = {"ACTIVE": ("ACTIVE",), "ASLEEP": ("ASLEEP", "OFF"),
                       "OFF": ("OFF",), "QUARANTINED": ("OFF",)}
    _require(before["state"] in expected_before[prior["state"]] and after["state"] == final["state"],
             "stream state differs from broker transition")
    if before["state"] == "OFF":
        _require(ring is None and before["reserved_bytes"] == 0, "resources remain on the prior OFF stream")
    if before["state"] == "ASLEEP":
        _require(ring and before["reserved_bytes"] > 0, "prior retained ring has no reservation")
    runtime = proof.runtime
    _require(isinstance(runtime, RuntimeEvidence) and runtime.app_incarnation == proof.binding["incarnation"]
             and type(runtime.resident_bytes) is int and runtime.resident_bytes >= 0
             and type(runtime.exclusive_pids) is tuple
             and all(type(pid) is int and pid > 0 for pid in runtime.exclusive_pids),
             "runtime charge or app identity absent")
    _require(receipt["charges"] == dict(resident_bytes=runtime.resident_bytes,
                                       reserved_bytes=after["reserved_bytes"]),
             "receipt charges differ from runtime and ring")
    if prior["state"] == "ACTIVE":
        _verify_observation(proof.previous_observation, before, proof.binding)
        _require(before["reserved_bytes"] > 0, "active ring has no reservation")
        retired = [proof.previous_observation.token.nonce]
    else:
        _require(proof.previous_observation is None and before["fresh_observation_id"] is None,
                 "retired observation is still present", "STALE_FRAME")
        retired = []
    _require(receipt["tokens_retired"] == retired and receipt["producers_stopped"] == ([ring] if retired else []),
             "token retirement or producer stop differs from stream evidence")

    if waking:
        _verify_observation(proof.observation, after, proof.binding)
        _require(proof.qualified is True and after["reserved_bytes"] > 0
                 and after["stream_generation"] > before["stream_generation"]
                 and after["ring_id"] != ring
                 and receipt["fresh_observation_id"] == proof.observation.observation_id,
                 "wake lacks qualified fresh observation", "STALE_FRAME")
    else:
        _require(proof.agent_drained is True and proof.observation is None
                 and receipt["fresh_observation_id"] is None and after["fresh_observation_id"] is None
                 and after["stream_generation"] == before["stream_generation"]
                 and final["stream_generation"] == prior["stream_generation"],
                 "agent drain or stream retirement unverified")
        if final["state"] == "ASLEEP":
            _require(after["ring_id"] == ring and ring
                     and after["reserved_bytes"] == before["reserved_bytes"] > 0,
                     "retained ring lost its reservation")
        else:
            _require(not runtime.exclusive_pids and after["ring_id"] is None
                     and after["reserved_bytes"] == 0, "exclusive resources still held")

    scrubbed = ring if final["state"] != "ASLEEP" else None
    if scrubbed:
        _require(proof.reap == {"reaped": [scrubbed], "retained": []}
                 and after["last_scrubbed_ring"] == scrubbed,
                 "Reap or scrub readback unverified")
    else:
        _require(proof.reap is None, "unexpected Reap result")
    _require(receipt["scrub"] == dict(buffers=[scrubbed] if scrubbed else [],
                                     canary_ok=True if scrubbed else None),
             "scrub claim differs from ring readback")


def verify_transition(receipt, proof, *, upstream_code="OK"):
    """Return a verified copy of a committed transition, or give a typed refusal."""
    _require(upstream_code == "OK", "upstream perception refusal", upstream_code)
    receipt = deepcopy(receipt)
    _validate(CONTRACT, receipt)
    _require(receipt["complete"] is True and not receipt["not_verified"], "incomplete transition")
    _require(isinstance(proof, TransitionEvidence), "transition evidence absent")
    waking = receipt["from"] == "WAKING" and receipt["to"] == "ACTIVE"
    required = {"identity", "capability", "stream"} if waking else {"agent-drain", "demand", "stream", "app"}
    _require(set(receipt["acks"]) == required, "transition acknowledgements differ from required participants")
    prior, final = _broker_states(proof, receipt, waking)
    _verify_demand(proof, receipt, final, waking)
    _verify_stream(proof, receipt, prior, final, waking)
    return receipt


def append_transition(journal, receipt, proof, *, agent, op_id=None, upstream_code="OK"):
    """Publish a verified transition and return its durable lineage reference.

    The hook derives its replay key from the committed broker record, which
    includes the session, incarnation and authority epoch.
    """
    payload = verify_transition(receipt, proof, upstream_code=upstream_code)
    path = SCHEMA_DIR / "lineage.event_v1.schema.json"
    try:
        kinds = json.loads(path.read_bytes())["properties"]["kind"]["enum"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        raise ReceiptError("SCHEMA_GAP", "lineage.event/v1 is unavailable") from None
    _require(CONTRACT in kinds, "lineage.event/v1 has no transition receipt kind", "SCHEMA_GAP")
    if op_id is None:
        op_id = sha256(canonical_bytes(proof.broker_records[-1])).hexdigest()
    try:
        return journal.append(agent=agent, op_id=op_id, kind=CONTRACT, payload=payload)
    except JournalError as exc:
        if str(exc) == "unregistered event kind":
            raise ReceiptError("SCHEMA_GAP", "U3c has no transition receipt writer") from None
        raise
