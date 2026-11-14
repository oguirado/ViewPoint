# @module {"id":"wa.learn.replay","layer":2,"owner":"LEARN","deps":["wa.state.schema","wa.state.diff","wa.remember.skills","wa.remember.program_map","wa.learn.explore"],"book":"HB-WA-s3-remember"}
"""@module wa.learn.replay - skills admission with verified replay.

Replays draft skill steps through Driver, evaluates checkpoints against post STATEs,
verifies diff summary stability across runs, attaches evidence, and admits via skills.admit.
"""
import copy
import hashlib
import os
import unittest

from ..remember import skills
from ..remember.program_map import canonical_json
from ..state import diff as state_diff
from ..state.schema import dumps

try:
    from .explore import Driver
except ImportError as err:
    if os.environ.get("WA2_ALLOW_SKIP") == "1":
        raise unittest.SkipTest("wa2-jules-explore-loop module wender.learn.explore not available: %s" % err)
    raise


def _match_node(node, predicate):
    if not isinstance(predicate, dict):
        return False
    role = predicate.get("role")
    name = predicate.get("name")
    if role is not None and node.get("role") != role:
        return False
    if name is not None and node.get("name") != name:
        return False
    return True


def _eval_expect(expect, post_state):
    nodes = post_state.get("nodes", [])

    if "node_present" in expect:
        target = expect["node_present"]
        preds = target if isinstance(target, list) else [target]
        for pred in preds:
            if not any(_match_node(n, pred) for n in nodes):
                return False

    if "node_absent" in expect:
        target = expect["node_absent"]
        preds = target if isinstance(target, list) else [target]
        for pred in preds:
            if any(_match_node(n, pred) for n in nodes):
                return False

    if "text_contains" in expect:
        target = expect["text_contains"]
        items = target if isinstance(target, list) else [target]
        for item in items:
            if isinstance(item, str):
                if not any(item in (n.get("name") or "") for n in nodes):
                    return False
            elif isinstance(item, dict):
                text = item.get("text") or item.get("contains") or item.get("name")
                matching_nodes = [n for n in nodes if _match_node(n, item)]
                if text is not None:
                    if not any(text in (n.get("name") or "") for n in matching_nodes):
                        return False
                elif not matching_nodes:
                    return False

    return True


def _checkpoint_index(checkpoint, count):
    if not isinstance(checkpoint, dict):
        raise skills.SkillRejected("checkpoint must be a dict")
    indices = [checkpoint[key] for key in ("after_step", "after") if key in checkpoint]
    if (not indices or any(type(index) is not int or not 0 <= index < count for index in indices)
            or any(index != indices[0] for index in indices[1:])):
        raise skills.SkillRejected("checkpoint index must identify one existing step")
    return indices[0]


def _validate_expect(expect):
    """Refuse unchecked or vacuous checkpoint claims before executing actions.

    The legacy evaluator's public predicate behavior is retained. Replay itself
    accepts only predicates it can actually evaluate; arbitrary C-11 metadata is
    declarative and cannot silently become successful execution evidence.
    """
    allowed = {"node_present", "node_absent", "text_contains"}
    if not isinstance(expect, dict) or not expect or set(expect) - allowed:
        raise skills.SkillRejected("checkpoint requires supported expectation predicates")
    for name, target in expect.items():
        items = target if isinstance(target, list) else [target]
        if not items:
            raise skills.SkillRejected("checkpoint predicate must not be empty")
        for item in items:
            if name == "text_contains" and isinstance(item, str) and item:
                continue
            fields = {"role", "name", "text", "contains"} if name == "text_contains" else {"role", "name"}
            if (not isinstance(item, dict) or not item or set(item) - fields
                    or any(not isinstance(value, str) or not value for value in item.values())):
                raise skills.SkillRejected("malformed checkpoint predicate")


def _observe_state(driver):
    observation = driver.observe()
    state = observation[0] if isinstance(observation, tuple) and observation else observation
    if not isinstance(state, dict) or not isinstance(state.get("nodes"), list):
        raise skills.SkillRejected("driver must observe a STATE dict")
    # A driver may reuse and mutate one dictionary on its next act/observe call.
    return copy.deepcopy(state)


# @anchor:wa.learn.replay.replay_and_admit
def replay_and_admit(draft, driver, verified_by, runs=2):
    """Replay at least twice, then allocate a verified revision without mutating draft.

    Absent prior evidence.version starts at one. A supplied prior version must be
    an exact positive integer and the new revision is its successor. This is
    local revision allocation, not a Store migration or a concurrent allocator.
    """
    if not isinstance(draft, dict):
        raise skills.SkillRejected("draft must be a dict")

    checkpoints = draft.get("checkpoints")
    if not checkpoints:
        raise skills.SkillRejected("no verification checkpoints")

    steps = draft.get("steps")
    if not steps:
        raise skills.SkillRejected("no steps")

    if not isinstance(steps, list) or not isinstance(checkpoints, list):
        raise skills.SkillRejected("steps and checkpoints must be lists")

    allow_irreversible = draft.get("allow_irreversible") is True
    for step_idx, step in enumerate(steps):
        if isinstance(step, dict) and step.get("irreversible") is True and not allow_irreversible:
            raise skills.SkillRejected("step %d is irreversible without allow_irreversible" % step_idx)

    if type(runs) is not int or runs < 2:
        raise skills.SkillRejected("runs must be an exact integer >= 2")
    if not isinstance(verified_by, str) or not verified_by.strip():
        raise skills.SkillRejected("verified_by must name the replay verifier")

    prior_evidence = draft.get("evidence", {})
    if not isinstance(prior_evidence, dict):
        raise skills.SkillRejected("prior evidence must be a dict")
    version = 1
    if "version" in prior_evidence:
        prior_version = prior_evidence["version"]
        if type(prior_version) is not int or prior_version < 1:
            raise skills.SkillRejected("prior evidence.version must be an exact positive integer")
        version = prior_version + 1

    indexed_checkpoints = []
    for checkpoint in checkpoints:
        index = _checkpoint_index(checkpoint, len(steps))
        if "expect" in checkpoint and "expected_post" in checkpoint:
            raise skills.SkillRejected("checkpoint must use one expectation field")
        expect = checkpoint.get("expect", checkpoint.get("expected_post"))
        _validate_expect(expect)
        indexed_checkpoints.append((index, copy.deepcopy(expect)))

    # Validate the eventual record's shape before side effects. These tentative
    # claims remain private and are returned only after every replay succeeds.
    try:
        canonical_json(draft)
        candidate = copy.deepcopy(draft)
        candidate["evidence"] = copy.deepcopy(prior_evidence)
        candidate["evidence"].update({"reproduced": True, "verified_by": verified_by,
                                      "runs": runs, "version": version})
        canonical_json(candidate)
        skills.admit(candidate)
    except (ValueError, TypeError, RecursionError) as exc:
        raise skills.SkillRejected(str(exc)) from exc
    steps = candidate["steps"]

    all_run_summaries = []

    for run_idx in range(1, runs + 1):
        if hasattr(driver, "reset") and callable(driver.reset):
            driver.reset()

        pre_state = _observe_state(driver)

        run_summaries = []

        for step_idx, step in enumerate(steps):
            driver.act(copy.deepcopy(step))

            post_state = _observe_state(driver)

            d = state_diff.diff_states(pre_state, post_state)
            summary = state_diff.summarize(d)
            run_summaries.append(summary)

            for index, expect in indexed_checkpoints:
                if index != step_idx:
                    continue
                if not _eval_expect(expect, post_state):
                    raise skills.SkillRejected(
                        "failing checkpoint in run %d at step %d: %s" % (run_idx, step_idx, expect)
                    )

            pre_state = post_state

        all_run_summaries.append(run_summaries)

    first_summary = all_run_summaries[0]
    for r_idx, r_sum in enumerate(all_run_summaries[1:], start=2):
        if r_sum != first_summary:
            raise skills.SkillRejected("diff summaries mismatch in run %d" % r_idx)

    summary_bytes = dumps(first_summary).encode("utf-8")
    summary_sha = hashlib.sha256(summary_bytes).hexdigest()

    candidate["evidence"].update({
        "reproduced": True,
        "verified_by": verified_by,
        "runs": runs,
        "sha256": summary_sha,
        "version": version,
    })

    return skills.admit(candidate)
