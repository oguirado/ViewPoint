"""Worst-wins verdicts; verdict command codes and stage codes are separate."""
PASS, FAIL, CANNOT_SEE, ADVISORY, ERROR = "PASS", "FAIL", "CANNOT_SEE", "ADVISORY", "ERROR"
PRECEDENCE = {PASS: 0, ADVISORY: 1, CANNOT_SEE: 2, FAIL: 3, ERROR: 4}
VERDICT_RC = {PASS: 0, ADVISORY: 0, FAIL: 1, ERROR: 2, CANNOT_SEE: 3}
STAGE_RC = {"OK": 0, "ERROR": 2, "INCOMPLETE": 3, "STALE": 4, "GATE_FAILURE": 5, "BUDGET_EXCEEDED": 6}


def max_precedence(verdicts):
    worst = PASS
    for value in verdicts:
        if value not in PRECEDENCE:
            raise ValueError("unknown_verdict:" + str(value))
        if PRECEDENCE[value] > PRECEDENCE[worst]:
            worst = value
    return worst


def rc_of(verdict):
    return VERDICT_RC[verdict]


def final_verdict(sd=(), px=(), judge=()):
    # blender_visual_qa/oracles/harness.py:43-46 documents judge ADVISORY as
    # CANNOT_SEE; its body differs. vqa3d deliberately pins the documented rule.
    judge = [v if type(v) is str and v in PRECEDENCE and v != ADVISORY else CANNOT_SEE for v in judge]
    return max_precedence(list(sd) + list(px) + judge)
