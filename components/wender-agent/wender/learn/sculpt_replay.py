# @module {"id":"wa.learn.sculpt_replay","layer":2,"owner":"LEARN","deps":["wa.remember.skills","wa.remember.sculpt"],"book":"HB-WA-s4-learn"}
"""Fresh bounded mesh replay with injected executor and independent evaluator.

No model calls, bpy imports or implicit mutation of caller data. Each callback
receives the immutable input bytes, a recipe copy and a monotonic deadline.
The executor returns mesh bytes; the evaluator measures input versus output.
The host owns process timeout/cleanup: arbitrary Python callbacks cannot be
preempted here. Late returns are refused. Policy guards/metric must be frozen
by the caller before seeing held-out results. Frames, if used, remain RAM-only.
"""
from copy import deepcopy
import hashlib
import time

from ..remember.skills import SkillRejected, _base, admit
from ..remember.sculpt import (record_sha256, skill_revision, supported_meshes,
                               validate_policy)


def _mesh(value):
    if type(value) is not bytes or not value or len(value) > 64 * 2**20:
        raise SkillRejected("nonempty mesh bytes within budget required")
    return hashlib.sha256(value).hexdigest()


def _trial(mesh, recipe, replay, evaluate, policy, deadline):
    started = time.monotonic_ns()
    row = {"mesh_sha256": _mesh(mesh), "output_sha256": None, "replay_sha256": None,
           "skill_sha256": policy["skill_sha256"],
           "toolchain_sha256": policy["toolchain_sha256"],
           "policy_sha256": record_sha256({k: v for k, v in policy.items() if k != "trials"}),
           "rc": 3, "reason_codes": ["reproduction_unavailable"],
           "score_before": None, "score_after": None,
           "guards": {name: None for name in policy["required_guards"]}, "wall_ns": 0}
    try:
        if started >= deadline:
            raise TimeoutError
        first = replay(mesh, deepcopy(recipe), deadline)
        row["output_sha256"] = _mesh(first)
        if time.monotonic_ns() >= deadline:
            raise TimeoutError
        second = replay(mesh, deepcopy(recipe), deadline)
        row["replay_sha256"] = _mesh(second)
        if time.monotonic_ns() >= deadline:
            raise TimeoutError
        measured = evaluate(mesh, first, deepcopy(recipe), deadline)
        if time.monotonic_ns() >= deadline:
            raise TimeoutError
        if (type(measured) is not dict or set(measured) != {
                "score_before", "score_after", "guards"} or
                any(type(measured[k]) is not int for k in ("score_before", "score_after")) or
                type(measured["guards"]) is not dict or
                set(measured["guards"]) != set(policy["required_guards"]) or
                any(type(v) is not bool for v in measured["guards"].values())):
            raise SkillRejected("evaluator contract")
        row.update(deepcopy(measured))
        reasons = []
        if first != second:
            reasons.append("replay_drift")
        if first == mesh:
            reasons.append("no_op")
        if not all(row["guards"].values()):
            reasons.append("guard_failed")
        if row["score_after"] - row["score_before"] < policy["min_gain"]:
            reasons.append("gain_below_floor")
        row.update(rc=5 if reasons else 0, reason_codes=reasons)
    except TimeoutError:
        row.update(rc=6, reason_codes=["deadline_exceeded"])
    except NotImplementedError:
        row.update(rc=3, reason_codes=["reproduction_unavailable"])
    except Exception:
        # Record failure, never exception text that may contain private data.
        row.update(rc=2, reason_codes=["reproduction_contract"])
    row["wall_ns"] = time.monotonic_ns() - started
    row["receipt_sha256"] = record_sha256(row)
    return row


# @anchor:wa.learn.sculpt_replay.qualify_sculpt_skill
def qualify_sculpt_skill(draft, *, source_mesh, held_out_meshes, toolchain_sha256,
                         required_guards, metric, min_gain, replay, evaluate,
                         training_mesh_sha256s=(), verified_by, timeout_s=60):
    """Return a qualification report; only rc zero carries an admitted skill.

    Every attempted held-out case stays in trials, including refused cases.
    Two distinct successes are required [TARGET: S3-U3], not two replay calls
    on the same mesh. Same bytes under different filenames are one identity.
    """
    recipe = deepcopy(draft)
    _base(recipe, allow_unverified=True)
    if recipe.get("domain") != "sculpt" or "sculpt" in recipe:
        raise SkillRejected("a fresh sculpt draft is required")
    if type(timeout_s) is not int or not 1 <= timeout_s <= 300:
        raise SkillRejected("replay timeout budget")
    if not callable(replay) or not callable(evaluate):
        raise SkillRejected("executor and evaluator required")
    if type(verified_by) is not str or not verified_by.strip():
        raise SkillRejected("verifier required")
    if type(required_guards) not in (list, tuple):
        raise SkillRejected("bounded guard names required")
    if type(held_out_meshes) not in (list, tuple):
        raise SkillRejected("bounded held-out mesh list required")
    if type(training_mesh_sha256s) not in (list, tuple) or len(training_mesh_sha256s) > 63:
        raise SkillRejected("bounded training identity list required")
    if any(type(value) is not str for value in training_mesh_sha256s):
        raise SkillRejected("training sha256 text required")
    meshes = list(held_out_meshes)
    if len(meshes) > 32 or sum(len(x) for x in meshes if type(x) is bytes) > 256 * 2**20:
        raise SkillRejected("held-out mesh budget")
    source = _mesh(source_mesh)
    identities = [_mesh(mesh) for mesh in meshes]
    training = sorted(set([source] + list(training_mesh_sha256s)))
    policy = {"schema": "wender.sculpt-reproduction/v1", "skill_sha256": skill_revision(recipe),
              "toolchain_sha256": toolchain_sha256, "source_mesh_sha256": source,
              "training_mesh_sha256s": training, "required_guards": list(required_guards),
              "metric": metric, "min_gain": min_gain, "trials": []}
    validate_policy(policy, skill_revision(recipe))
    # Refuse leakage/aliases before any callback can mutate an external scene.
    reasons = []
    if len(set(identities)) != len(identities):
        reasons.append("duplicate_mesh")
    if set(identities) & set(training):
        reasons.append("training_leakage")
    if len(identities) < 2:
        reasons.append("held_out_support_missing")
    if reasons:
        return {"schema": "wender.sculpt-qualification/v1", "rc": 3,
                "reason_codes": reasons, "supported_meshes": [], "policy": policy, "skill": None}
    deadline = time.monotonic_ns() + timeout_s * 10**9
    for mesh in meshes:
        policy["trials"].append(_trial(mesh, recipe, replay, evaluate, policy, deadline))
    support = sorted(supported_meshes(policy))
    success = len(support) >= 2
    result = None
    if success:
        result = deepcopy(recipe)
        result["sculpt"] = policy
        previous = result["evidence"].get("version", 0)
        if type(previous) is not int or previous < 0:
            raise SkillRejected("invalid prior evidence version")
        result["evidence"] = {"version": previous + 1, "reproduced": True,
                              "verified_by": verified_by}
        admit(result)
    rcs = {row["rc"] for row in policy["trials"]}
    rc = 0 if success else next((code for code in (2, 5, 6, 3) if code in rcs), 3)
    return {"schema": "wender.sculpt-qualification/v1", "rc": rc,
            "reason_codes": [] if success else ["held_out_support_missing"],
            "supported_meshes": support, "policy": deepcopy(policy), "skill": result}
