# @module {"id":"wa.remember.sculpt","layer":1,"owner":"REMEMBER","deps":["wa.remember.skills","wa.remember.program_map"],"book":"HB-WA-s3-remember"}
"""Mesh-specific historical support, separate from present execution authority.

VE-0029 C1-C3: hashes bind claims; they cannot authenticate a dishonest producer.
The LEARN replayer produces observations; this module only validates records.
Held-out means distinct input byte identities outside the declared training set,
not statistical independence or a proof that training history was disclosed.
Two supported meshes [TARGET: S3-U3] do not establish generalisation.
No pixels, mesh bytes or executable code are persisted here.
"""
import hashlib
import re

from .program_map import canonical_json
from .skills import SkillRejected

_SCHEMA = "wender.sculpt-reproduction/v1"
_POLICY = {"schema", "skill_sha256", "toolchain_sha256", "source_mesh_sha256",
           "training_mesh_sha256s", "required_guards", "metric", "min_gain", "trials"}
_TRIAL = {"mesh_sha256", "output_sha256", "replay_sha256", "rc", "reason_codes",
          "score_before", "score_after", "guards", "wall_ns", "receipt_sha256",
          "skill_sha256", "toolchain_sha256", "policy_sha256"}
_RC = (0, 2, 3, 4, 5, 6)


def _need(condition, reason):
    if not condition:
        raise SkillRejected(reason)


def _digest(value):
    _need(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
          "sculpt digest must be lowercase sha256")


def _texts(values):
    _need(type(values) is list and bool(values) and all(
        type(x) is str and x.strip() for x in values), "nonempty sculpt names required")
    _need(len(values) == len(set(values)), "duplicate sculpt names")


# @anchor:wa.remember.sculpt.record_sha256
def record_sha256(value):
    """Hash canonical integer metadata without a trailing newline."""
    try:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    except (ValueError, TypeError, RecursionError) as exc:
        raise SkillRejected("invalid sculpt metadata") from exc


# @anchor:wa.remember.sculpt.skill_revision
def skill_revision(skill):
    """Bind the entire recipe, including preconditions, excluding historical claims."""
    return record_sha256({key: value for key, value in skill.items()
                          if key not in ("evidence", "sculpt")})


# @anchor:wa.remember.sculpt.validate_policy
def validate_policy(policy, revision):
    """Validate a frozen reproduction policy before replay or admission."""
    _need(type(policy) is dict and set(policy) == _POLICY, "sculpt policy fields")
    _need(policy["schema"] == _SCHEMA, "sculpt reproduction schema")
    for key in ("skill_sha256", "toolchain_sha256", "source_mesh_sha256"):
        _digest(policy[key])
    _need(policy["skill_sha256"] == revision, "sculpt skill revision stale")
    training = policy["training_mesh_sha256s"]
    _need(type(training) is list and 1 <= len(training) <= 64, "training set required")
    for digest in training:
        _digest(digest)
    _need(training == sorted(set(training)), "training identities must be sorted and unique")
    _need(policy["source_mesh_sha256"] in training, "source missing from training set")
    _texts(policy["required_guards"])
    _need(type(policy["metric"]) is str and bool(policy["metric"].strip()), "metric required")
    _need(type(policy["min_gain"]) is int and policy["min_gain"] > 0, "positive gain required")
    _need(type(policy["trials"]) is list and len(policy["trials"]) <= 32, "trial budget")


# @anchor:wa.remember.sculpt.supported_meshes
def supported_meshes(policy):
    """Recompute support; never trust caller-supplied counters or truthy flags."""
    seen, supported = set(), set()
    required = set(policy["required_guards"])
    training = set(policy["training_mesh_sha256s"])
    policy_hash = record_sha256({key: value for key, value in policy.items() if key != "trials"})
    for trial in policy["trials"]:
        _need(type(trial) is dict and set(trial) == _TRIAL, "sculpt trial fields")
        mesh = trial["mesh_sha256"]
        _digest(mesh)
        _need(mesh not in seen, "duplicate reproduction mesh")
        seen.add(mesh)
        _digest(trial["receipt_sha256"])
        unsigned = {key: value for key, value in trial.items() if key != "receipt_sha256"}
        _need(record_sha256(unsigned) == trial["receipt_sha256"], "sculpt receipt drift")
        for key, expected in (("skill_sha256", policy["skill_sha256"]),
                              ("toolchain_sha256", policy["toolchain_sha256"]),
                              ("policy_sha256", policy_hash)):
            _need(trial[key] == expected, "sculpt trial identity stale: " + key)
        _need(type(trial["rc"]) is int and trial["rc"] in _RC, "invalid reproduction rc")
        _need(type(trial["wall_ns"]) is int and trial["wall_ns"] >= 0, "invalid replay timing")
        reasons = trial["reason_codes"]
        _need(type(reasons) is list and all(type(x) is str and x for x in reasons),
              "invalid reproduction reasons")
        guards = trial["guards"]
        _need(type(guards) is dict and set(guards) == required and all(
            value is None or type(value) is bool for value in guards.values()),
            "missing or invalid reproduction guard")
        for key in ("score_before", "score_after"):
            _need(trial[key] is None or type(trial[key]) is int, "noninteger reproduction score")
        for key in ("output_sha256", "replay_sha256"):
            if trial[key] is not None:
                _digest(trial[key])
        if trial["rc"] != 0:
            _need(bool(reasons), "refusal needs a reason")
            continue
        _need(not reasons, "successful reproduction has refusal reasons")
        _need(mesh not in training, "source or training mesh is not held out")
        _need(trial["output_sha256"] is not None and
              trial["output_sha256"] == trial["replay_sha256"], "reproduction not repeatable")
        _need(trial["output_sha256"] != mesh, "no-op cannot support a sculpt skill")
        _need(all(value is True for value in guards.values()), "sculpt guard not proven")
        _need(type(trial["score_before"]) is int and type(trial["score_after"]) is int and
              trial["score_after"] - trial["score_before"] >= policy["min_gain"],
              "sculpt gain not proven")
        supported.add(mesh)
    return supported


# @anchor:wa.remember.sculpt.validate_sculpt_skill
def validate_sculpt_skill(skill):
    """Require historical support on distinct held-out meshes, never source alone."""
    _need(skill.get("domain") == "sculpt", "sculpt domain required")
    policy = skill.get("sculpt")
    validate_policy(policy, skill_revision(skill))
    _need(len(supported_meshes(policy)) >= 2, "two distinct held-out meshes required")
    evidence = skill["evidence"]
    _need(type(evidence.get("verified_by")) is str and bool(evidence["verified_by"].strip()),
          "sculpt verifier required")
    _need(type(evidence.get("version")) is int and evidence["version"] > 0,
          "sculpt evidence version required")
