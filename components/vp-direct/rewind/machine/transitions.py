"""Internal durable goal transitions. Only the coordinator calls these handlers."""

from fractions import Fraction
import hashlib

from jsonschema import ValidationError
from schemas.validation import canonical_bytes, validate_contract
from vendor.rewind import schema as rw


STOP = "HARD_STOP_ESCALATED"


class Refused(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise Refused(code)


def natural(value):
    return type(value) is int and value >= 0


def identifier(value):
    return isinstance(value, str) and bool(value.strip())


def digest(value):
    return isinstance(value, str) and rw.HEX64_RE.fullmatch(value) is not None


def potential(s):
    return (s["T"] - s["t"], s["B"] - s["r"], s["D"] - s["d"],
            s["n"] - s["i"], s["u"], s["a"], s["q"])


def action_hash(action, touches, quantum):
    """Encode JSON types distinctly. Round action numbers to nearest even bin."""
    step = Fraction(quantum)

    def encode(value):
        if isinstance(value, dict):
            require(all(isinstance(k, str) for k in value), "BAD_INTENT")
            return ["object", [[k, encode(value[k])] for k in sorted(value)]]
        if isinstance(value, list):
            return ["array", [encode(v) for v in value]]
        if type(value) in (int, float):
            return ["number", round(Fraction(str(value)) / step)]
        require(value is None or isinstance(value, (str, bool)), "BAD_INTENT")
        return ["literal", value]

    require(isinstance(action, dict) and action, "BAD_INTENT")
    return hashlib.sha256(canonical_bytes([encode(action), touches])).hexdigest()


def create(*, goal_id, session_id, binding, checkpoint, score, steps, duplicates,
           epsilon, action_quantum, observation_id, generation, evidence=None):
    validate_contract("vp.surface_binding/v1", binding)
    validate_contract("vp.rewind_receipt/v1", checkpoint)
    require(all(identifier(x) for x in (goal_id, session_id, observation_id)), "BAD_INTENT")
    require(all(natural(x) for x in (steps, duplicates, generation, score)), "BAD_INTENT")
    # Pin the numerical policy once at goal admission, including across restarts.
    require(type(epsilon) is int and epsilon > 0 and Fraction(action_quantum) > 0, "BAD_INTENT")
    b = checkpoint["budget"]
    require(checkpoint["op"] == "checkpoint" and checkpoint["ok"] and
            checkpoint["goal_id"] == goal_id and checkpoint["last_good"] == "p0" and
            checkpoint["authority_epoch"] == binding["authority_epoch"] and
            b["attempts_used"] == b["rewinds_used"] == 0, "BASE_NOT_LAST_GOOD")
    if evidence is not None:
        require(evidence["point_id"] == "p0" and evidence["quality"]["score"] == score, "RESTORE_MISMATCH")
    return dict(**({"checkpoint_evidence": evidence} if evidence is not None else {}),
                goal_id=goal_id, session_id=session_id, owner=binding["owner"],
                incarnation=binding["incarnation"], surface_id=binding["surface_id"],
                endpoint=binding["endpoint"], authority_epoch=binding["authority_epoch"],
                scene_epoch=checkpoint["scene_epoch"], checkpoint=checkpoint, points=["p0"],
                best=score, epsilon=epsilon, quantum=action_quantum, T=b["attempts_limit"],
                B=b["rewinds_limit"], D=duplicates, n=steps, t=0, r=0, d=0, i=0, u=0, a=0, q=0,
                phase="READY", human=False, perception="ACTIVE", wake_source=None,
                generation=generation, observation=observation_id, observations=[observation_id],
                wake_failures=0, flight=0, work={}, last_attempt=None, attempts=[], pending=None,
                supersedes=None, alternative=None, failed_branches=[], restore_claim=None, reason=None)


def cleanup(s, reason):
    s.update(phase="CLEANUP", reason=reason, restore_claim=None, observation=None)


def duplicate(s, code):
    if s["d"] < s["D"]:
        s["d"] += 1
    if s["d"] == s["D"]:
        cleanup(s, code)
    return code


def agent(s):
    require(not s["human"] and not s.get("pending_rest") and s["perception"] == "ACTIVE" and not s["q"], "NO_LEASE")


def proposal(s, *, attempt, branch, touches, observation, supersedes, retry):
    agent(s)
    require(s["phase"] == ("REJECTED" if retry else "READY"), "NO_LEASE")
    require(s["observation"] and observation == s["observation"], "STALE_FRAME")
    require(not s["u"], "UNCERTAIN_OUTCOME")
    require(isinstance(attempt, str) and rw.ATTEMPT_RE.fullmatch(attempt) and
            int(rw.split_attempt(attempt)[1]) <= rw.K_MAX and not rw.is_control_id(attempt),
            "BAD_ATTEMPT")
    if attempt in s["attempts"]:
        return duplicate(s, "ATTEMPT_REUSED")
    base = s["checkpoint"]["last_good"]
    if any(row["base_point"] == base and row["canonical_action_sha256"] == branch
           for row in s["failed_branches"]):
        return duplicate(s, "BRANCH_ALREADY_FAILED")
    require(supersedes == s["supersedes"], "ATTEMPT_REUSED")
    if s["t"] >= s["T"] or (retry and s["r"] >= s["B"]):
        cleanup(s, "ALL_REJECTED")
        return "CLEANUP"
    if retry:
        s.update(phase="REWINDING", r=s["r"] + 1, restore_claim=None,
                 alternative=[attempt, branch])
        return "REWINDING"
    require(s["alternative"] in (None, [attempt, branch]), "BAD_INTENT")
    s["alternative"] = None
    s["pending"] = dict(attempt=attempt, base_point=base, canonical_action_sha256=branch,
                        touches=touches)
    s["attempts"].append(attempt)
    s.update(phase="EXECUTING", last_attempt=attempt, t=s["t"] + 1, a=1)
    return "EXECUTING"


def improves(score, best, epsilon):
    return score > best + epsilon


def judge(s, *, attempt, post_state, post_digest, score, predicates):
    require(not s["human"] and s["phase"] == "EXECUTING" and
            s["last_attempt"] == attempt and not s["u"] and not s["pending"].get("fenced"), "NO_LEASE")
    require(digest(post_state) and digest(post_digest) and natural(score) and
            isinstance(predicates, list) and all(type(p) is bool for p in predicates), "BAD_INTENT")
    seen = any(row["post_state_sha256"] == post_state or row["post_digest"] == post_digest
               for row in s["failed_branches"])
    s["pending"].update(post_state_sha256=post_state, post_digest=post_digest,
                        score_before=s["best"], score_after=score)
    if seen:
        reason = "STATE_ALREADY_FAILED"
    elif not predicates or not all(predicates) or not improves(score, s["best"], s["epsilon"]):
        reason = "JUDGE_REJECT"
    elif len(s["points"]) >= rw.RING_SLOTS:
        reason = "CKPT_BUDGET"
    else:
        s["phase"] = "JUDGING"
        return "JUDGING"
    rejected = dict(s["pending"], reason=reason)
    del rejected["attempt"]
    rejected.pop("uncertain", None)
    rejected.pop("fenced", None)
    s["failed_branches"].append(rejected)
    s.update(phase="REJECTED", pending=None, supersedes=attempt, observation=None)
    if s["t"] >= s["T"] or s["r"] >= s["B"] or reason == "CKPT_BUDGET":
        cleanup(s, reason)
    return reason


def matching_receipt(s, receipt, op):
    validate_contract("vp.rewind_receipt/v1", receipt)
    prior = s["checkpoint"]
    require(receipt["budget"] == dict(attempts_used=s["t"], attempts_limit=s["T"],
                                      rewinds_used=s["r"], rewinds_limit=s["B"]), "RESTORE_MISMATCH")
    require(receipt["op"] == op and
            all(receipt[k] == prior[k] for k in
                ("goal_id", "capsule_id", "backend", "compatibility_sha256")), "RESTORE_MISMATCH")
    require(receipt["ok"], receipt["code"])


def commit(s, *, attempt, receipt, revised_steps, evidence=None):
    require(not s["human"] and s["phase"] == "JUDGING" and s["last_attempt"] == attempt, "NO_LEASE")
    matching_receipt(s, receipt, "checkpoint")
    point = receipt["last_good"]
    require(rw.POINT_ID_RE.fullmatch(point) and point not in s["points"] and
            receipt["scene_epoch"] == s["scene_epoch"] and
            receipt["authority_epoch"] == s["authority_epoch"] and
            receipt["confirmed_tick"] > s["checkpoint"]["confirmed_tick"] and
            receipt["state_sha256"] == s["pending"]["post_state_sha256"], "CKPT_CORRUPT")
    if revised_steps is not None:
        require(natural(revised_steps) and revised_steps >= s["i"], "BAD_INTENT")
        s["n"] = revised_steps
    if evidence is not None or "checkpoint_evidence" in s:
        require(evidence is not None and evidence["point_id"] == point
                and evidence["quality"]["accepted"] is True
                and evidence["quality"]["score"] == s["pending"]["score_after"]
                and evidence["quality"]["scene_digest"] == s["pending"]["post_digest"], "RESTORE_MISMATCH")
        s["checkpoint_evidence"] = evidence
    s["points"].append(point)
    s.update(best=s["pending"]["score_after"], pending=None, checkpoint=receipt,
             phase="READY", supersedes=None, observation=None)
    return "COMMITTED"


def abandon(s):
    require(not s["human"] and s["phase"] in ("REJECTED", "JUDGING"), "NO_LEASE")
    require(not s["u"], "UNCERTAIN_OUTCOME")
    cleanup(s, "ALL_REJECTED")
    return "CLEANUP"


def claim_restore(s):
    require(not s["human"] and s["phase"] in ("CLEANUP", "REWINDING"), "NO_LEASE")
    require(s["restore_claim"] is None, "UNCERTAIN_OUTCOME")
    point = s["checkpoint"]["last_good"]
    job = rw.control_job_id("restore", point, "e%d" % (s["scene_epoch"] + 1))
    s["restore_claim"] = job
    return job


def restored(s, *, claim, receipt, error, evidence=None):
    require(s["phase"] in ("REWINDING", "CLEANUP") and
            claim is not None and s["restore_claim"] == claim, "NO_LEASE")
    prior = s["checkpoint"]
    cleaning = s["phase"] == "CLEANUP"
    try:
        if error is not None:
            raise Refused(error)
        matching_receipt(s, receipt, "rewind")
        if evidence is not None or "checkpoint_evidence" in s:
            require(evidence is not None and evidence == s.get("checkpoint_evidence"), "RESTORE_MISMATCH")
        require(receipt["digest_match"] and receipt["scene_epoch"] == s["scene_epoch"] + 1
                and receipt["authority_epoch"] >= s["authority_epoch"]
                and (prior["backend"] != "microvm_v0_2" or
                     receipt["authority_epoch"] > s["authority_epoch"])
                and all(receipt[k] == prior[k] for k in
                        ("state_sha256", "last_good", "confirmed_tick")), "RESTORE_MISMATCH")
    except (ValidationError, Refused) as exc:
        s.update(phase=STOP, reason=getattr(exc, "code", "RESTORE_MISMATCH"), observation=None)
        return STOP
    s.update(scene_epoch=receipt["scene_epoch"], authority_epoch=receipt["authority_epoch"],
             phase=STOP if cleaning else "READY", restore_claim=None, pending=None, observation=None)
    return s["phase"]


def fresh(s, observation, generation):
    require(identifier(observation) and observation not in s["observations"] and
            natural(generation) and generation >= s["generation"], "STALE_FRAME")
    s["observations"].append(observation)
    s.update(observation=observation, generation=generation)


def observe(s, *, observation, generation):
    agent(s)
    require(s["phase"] in ("REJECTED", "READY"), "NO_LEASE")
    fresh(s, observation, generation)
    return s["phase"]


def verified(s, *, step, observation):
    agent(s)
    require(s["phase"] == "READY" and not s["u"], "NO_LEASE")
    require(identifier(observation) and observation == s["observation"] and
            natural(step) and step == s["i"] < s["n"], "STALE_FRAME")
    s.update(i=step + 1, a=1, observation=None)
    return "VERIFIED"


def uncertain(s, *, attempt):
    require(not s["human"] and s["phase"] == "EXECUTING" and s["last_attempt"] == attempt
            and not s["u"] and (not s["pending"].get("uncertain") or s["pending"].get("fenced")),
            "NO_LEASE")
    s["pending"]["uncertain"] = True
    s["u"] = 1
    return "UNCERTAIN_OUTCOME"


def reconcile(s, *, attempt, effect, observation, generation):
    require(not s["human"] and s["u"] and s["last_attempt"] == attempt, "NO_LEASE")
    fresh(s, observation, generation)
    s["u"] = 0
    s["pending"].pop("fenced", None)
    if effect == "verified":
        return "VERIFIED"
    if effect == "unknown":
        # The single observe-only pass is spent. Only a broker handback may
        # resume this goal; a later agent observation cannot reopen the pass.
        s.update(t=min(s["T"], s["t"] + 1), phase="HUMAN_NEEDED", observation=None)
        return "HUMAN_NEEDED"
    require(effect == "no_effect", "BAD_INTENT")
    s.update(phase="READY", pending=None, supersedes=attempt)
    return "READY"


def human(s, *, lease, observation, generation):
    validate_contract("vp.human_lease/v1", lease)
    take = lease["state"] == "HUMAN"
    require(take != s["human"] and lease["session_id"] == s["session_id"] and
            lease["epoch_before"] == s["authority_epoch"] and
            lease["epoch_after"] == s["authority_epoch"] + 1, "NO_LEASE")
    if not take:
        require(not lease["outstanding"], "UNCERTAIN_OUTCOME")
        fresh(s, observation, generation)
        if s["phase"] == "HUMAN_NEEDED":
            s.update(phase="READY", pending=None, supersedes=s["last_attempt"])
            if s["t"] == s["T"]:
                cleanup(s, "ALL_REJECTED")
    else:
        s["observation"] = None
        if s["phase"] == "EXECUTING":
            # An unfinished dispatch straddles an authority change. Old
            # evidence cannot settle it when the operator gives control back.
            s["pending"]["fenced"] = True
    s.update(human=take, authority_epoch=lease["epoch_after"])
    return lease["state"]


def begin_rest(s, *, intent):
    require(not s["human"] and s["phase"] == "READY" and not s["u"]
            and not (s["perception"] == "ACTIVE" and s["q"])
            and intent["target"] in ("ASLEEP", "OFF")
            and intent["record"]["state"] == s["perception"]
            and intent["record"]["stream_generation"] == s["generation"]
            and intent["record"]["authority_epoch"] == s["authority_epoch"]
            and intent["flight"] == s["flight"], "NO_LEASE")
    require(s.get("pending_rest", intent) == intent, "HOLD")
    s["pending_rest"] = intent
    return intent


def finish_rest_intent(s, target, boundary):
    intent = s.get("pending_rest")
    if intent is not None:
        require(boundary and intent["source"] == boundary["source"]
                and intent["flight"] == boundary["flight"] and intent["record"] == boundary["prior"]
                and intent["target"] == target, "HOLD")
        del s["pending_rest"]


def sleep(s, *, external, target, generation, authority_epoch, boundary=None):
    finish_rest_intent(s, target, boundary)
    agent(s)
    require(generation == s["generation"] and authority_epoch == s["authority_epoch"], "STALE_FRAME")
    require(s["phase"] == "READY" and not s["u"], "NO_LEASE")
    if not external:
        if not s["a"]:
            if s["t"] == s["T"]:
                cleanup(s, "ALL_REJECTED")
                return "CLEANUP"
            s["t"] += 1
        s.update(a=0, q=1)
    if boundary is not None:
        s["rest_boundary"] = boundary
    s.update(perception=target, observation=None)
    return target


def rest_off(s, *, generation, authority_epoch, boundary=None):
    finish_rest_intent(s, "OFF", boundary)
    require(not s["human"] and s["phase"] == "READY" and s["perception"] == "ASLEEP"
            and s["generation"] == generation and s["authority_epoch"] == authority_epoch, "NO_LEASE")
    if boundary is not None:
        s["rest_boundary"] = boundary
    s["perception"] = "OFF"
    return "OFF"


def admit_work(s, *, token):
    require(identifier(token), "BAD_INTENT")
    if token in s["work"]:
        return "ALREADY_ADMITTED"
    s["work"][token] = "admitted"
    return "ADMITTED"


def delivered(s, *, token):
    require(token in s.get("work_flights", {}) and
            s["work"].get(token) in ("admitted", "consumed", "delivered"), "NO_LEASE")
    s["work"][token] = "delivered"
    return "DELIVERED"


def settle_work(s, *, token):
    require(s["work"].get(token) in ("admitted", "consumed", "delivered"), "NO_LEASE")
    s["work"][token] = "settled"
    return "SETTLED"


def wake(s, *, token):
    require(not s.get("pending_rest") and not s["human"] and s["phase"] == "READY" and
            s["perception"] != "QUARANTINED", "NO_LEASE")
    if token is None:
        if not s["q"]:
            return duplicate(s, "DUPLICATE_WAKE")
        s["q"] = 0
        # External demand wakes the producer independently. Redeeming the
        # goal's bit either joins that flight or uses its fresh acquisition.
        if s["perception"] == "ACTIVE":
            return "ACTIVE"
        if s["perception"] == "WAKING":
            s["wake_source"] = "joined"
            return "WAKE_PENDING"
    else:
        require(s["work"].get(token) == "admitted", "NO_LEASE")
        if s["perception"] in ("ACTIVE", "WAKING"):
            return "ACTIVE" if s["perception"] == "ACTIVE" else "WAKE_PENDING"
    require(s["perception"] in ("ASLEEP", "OFF"), "NO_LEASE")
    if s["t"] == s["T"]:
        cleanup(s, "ALL_REJECTED")
        return "CLEANUP"
    if token is not None:
        s["work"][token] = "consumed"
    s.update(perception="WAKING", flight=s["flight"] + 1,
             wake_source="external" if token is not None else "agent")
    return s["flight"]


def claim_wake(s, *, token):
    require(admit_work(s, token=token) == "ADMITTED", "NO_LEASE")
    result = wake(s, token=token)
    s.setdefault("work_flights", {})[token] = s["flight"]
    return result


def awake(s, *, observation, generation, reopen, authority_epoch, flight, boundary=None):
    require(natural(flight) and flight == s["flight"], "STALE_FRAME")
    require(authority_epoch == s["authority_epoch"], "NO_LEASE")
    require(not s["human"] and s["phase"] == "READY", "NO_LEASE")
    require(s["perception"] == ("QUARANTINED" if reopen else "WAKING"), "NO_LEASE")
    if reopen:
        if s["r"] >= s["B"]:
            cleanup(s, "ALL_REJECTED")
            return "CLEANUP"
        s["r"] += 1
    require(natural(generation) and generation > s["generation"], "STALE_FRAME")
    fresh(s, observation, generation)
    if boundary is not None:
        s["active_boundary"] = boundary
    s.update(perception="ACTIVE", wake_source=None)
    if reopen:
        s.update(q=0, wake_failures=0)
    return "ACTIVE"


def failed_wake(s, *, flight, boundary=None):
    require(natural(flight) and flight == s["flight"], "STALE_FRAME")
    require(not s["human"] and s["phase"] == "READY" and s["perception"] == "WAKING", "NO_LEASE")
    if s["t"] >= s["T"]:
        cleanup(s, "ALL_REJECTED")
        return "CLEANUP"
    s["t"] += 1
    s["wake_failures"] += 1
    quarantine = s["wake_failures"] >= 2
    s.update(perception="QUARANTINED" if quarantine else "ASLEEP",
             q=int(not quarantine and (s["q"] or s["wake_source"] in ("agent", "joined"))), wake_source=None)
    if boundary is not None:
        require(boundary["record"]["state"] == s["perception"] and boundary["flight"] == flight, "HOLD")
        s["rest_boundary"] = boundary
    return s["perception"]


HANDLERS = {fn.__name__: fn for fn in (
    proposal, judge, commit, abandon, claim_restore, restored, observe, verified,
    uncertain, reconcile, human, begin_rest, sleep, rest_off, admit_work, settle_work, delivered, wake, claim_wake, awake, failed_wake)}


def apply(s, op, data):
    """Check the progress proof at admission and on replay. Broker completions
    settle an existing transition. Terminal cleanup admits no further edits.
    """
    before = potential(s)
    result = HANDLERS[op](s, **data)
    after = potential(s)
    bounded = (op in ("proposal", "verified", "failed_wake", "reconcile")
               or (op == "sleep" and not data["external"])
               or (op == "wake" and data["token"] is None)
               or (op == "awake" and data["reopen"]))
    require(all(natural(x) for x in after), "BUDGET_EXCEEDED")
    if bounded and s["phase"] not in ("CLEANUP", STOP):
        require(after < before, "NON_DESCENDING")
    return result
