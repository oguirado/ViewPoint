"""R3: the judge of the rewindable Blender capsule (contract rw1, CONTRACT.md sections 2, 4, 6).

Track P (declared Claude work, claude-fable-5-2). Stdlib only, no bpy. Imports the R0 schema and the F1
broker (for FOOTPRINT and diff_regions); edits neither.

    judge(attempt, base, after, run_probe=None) -> rec:judge          (signature frozen by R0)
      attempt = {"attempt": "<event_id>.a<k>", "intent": {...}, "script": str, "verdict": <F1 verdict>, "extra": {...}}
      base    = {"point_id", "digest", "frame_png"}
      after   = {"digest", "frame_png", "job_ms"}
      run_probe(script) -> {"ok": bool, "result": <json>, "digest": {...}}     (one bridge round trip)

Oracles, fixed order (schema.ORACLES); an oracle that does not apply is omitted:
  expect   the intent's read-only probe answers `equals`, and the probe left the digest unchanged
  delta    the F1 verdict is committable and digest delta(base, after) is non-empty and inside
           write_set + touches (schema.delta_outside)
  witness  diff_regions(base_frame, after_frame): something changed inside FOOTPRINT, nothing outside
  cost     job ms, recorded, never deciding; `best()` uses it as the tie-break between accepted attempts

Every oracle fails closed: a missing probe runner, a probe error, an absent digest or a differ error is a
rejection, never an acceptance. `rollback_witness` is the frame check after a restore.
"""
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRODUCT = os.path.dirname(_HERE)
for _p in (os.path.join(_PRODUCT, "ref_rewind"), os.path.join(_PRODUCT, "ref_broker")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import broker as B  # noqa: E402  (F1, imported, never edited)
import schema as RW  # noqa: E402  (R0, imported, never redefined)

FOOTPRINT = tuple(B.FOOTPRINT)          # viewport region (x, y, w, h)
FOOTPRINT_SLACK = B.FOOTPRINT_SLACK     # vision / differ report block-aligned regions
VISIBLE_PREFIXES = ("col:", "mat:")     # digest keys whose change must show in the viewport
VISIBLE_KEYS = ("ui:view3d",)
# detail codes (first token of oracle.detail; RW.PROBE_FAILED / RW.PROBE_NOT_READONLY are R0's)
EXPECT_MISMATCH = "EXPECT_MISMATCH"
VERDICT_NOT_COMMITTABLE = "VERDICT_NOT_COMMITTABLE"
NO_DIGEST = "NO_DIGEST"
NO_EFFECT = "NO_EFFECT"
DELTA_OUTSIDE = "DELTA_OUTSIDE"
WITNESS_ERROR = "WITNESS_ERROR"
WITNESS_NO_CHANGE = "WITNESS_NO_CHANGE"
WITNESS_SPILL = "WITNESS_SPILL"
WITNESS_NOT_RESTORED = "WITNESS_NOT_RESTORED"


def _j(x):
    try:
        return json.dumps(x, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(x)


def _is_digest(d):
    try:
        RW.check_digest(d)
    except RW.SchemaError:
        return False
    return True


def _box(r):
    """[x, y, w, h] from a region given as a list (F1 differ) or as a dict (vision act)."""
    if isinstance(r, dict):
        return [int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])]
    return [int(v) for v in r[:4]]


def _intersects(r, box):
    return r[0] < box[0] + box[2] and box[0] < r[0] + r[2] and r[1] < box[1] + box[3] and box[1] < r[1] + r[3]


def split_regions(regions, footprint=FOOTPRINT, slack=FOOTPRINT_SLACK):
    """(touching, spill): regions that intersect the footprint, and regions not contained in it
    (containment with `slack`, as F1 does). A region across the border is in both lists."""
    boxes = [_box(r) for r in regions or []]
    touching = [r for r in boxes if _intersects(r, footprint)]
    spill = [r for r in boxes if not B._inside(r, footprint, slack)]
    return touching, spill


# ---------------------------------------------------------------- oracle 1: expect probe
def expect_oracle(intent, after_digest, run_probe):
    """None when the intent declares no machine-checkable expectation (F1 text `expect`)."""
    ex = intent.get("expect")
    if not isinstance(ex, dict):
        return None
    if run_probe is None:
        return RW.oracle("expect", False, RW.PROBE_FAILED + " no probe runner")
    try:
        pr = run_probe(ex.get("probe"))
    except Exception as e:  # noqa: BLE001  (a runner that raises is a failed probe, not a crash of the turn)
        return RW.oracle("expect", False, "%s %s: %s" % (RW.PROBE_FAILED, type(e).__name__, e))
    if not isinstance(pr, dict) or pr.get("ok") is not True:
        return RW.oracle("expect", False, RW.PROBE_FAILED)
    # GUARD probe-readonly: the probe must leave the digest exactly as the attempt left it
    if not RW.digest_match(pr.get("digest"), after_digest):
        return RW.oracle("expect", False, RW.PROBE_NOT_READONLY)
    if "equals" not in ex or pr.get("result") != ex["equals"]:
        return RW.oracle("expect", False, "%s got %s want %s" % (EXPECT_MISMATCH, _j(pr.get("result")), _j(ex.get("equals"))))
    return RW.oracle("expect", True, "got %s" % _j(pr.get("result")))


# ---------------------------------------------------------------- oracle 2: digest delta inside write_set
def delta_oracle(intent, verdict, base_digest, after_digest):
    """(oracle, delta). Always applies."""
    if not (_is_digest(base_digest) and _is_digest(after_digest)):
        return RW.oracle("delta", False, NO_DIGEST), []
    delta = RW.digest_delta(base_digest, after_digest)
    out = RW.delta_outside(delta, intent)
    if verdict not in RW.COMMIT_VERDICTS:
        return RW.oracle("delta", False, "%s f1=%s delta=%s" % (VERDICT_NOT_COMMITTABLE, verdict, _j(delta))), delta
    if not delta:
        return RW.oracle("delta", False, NO_EFFECT), delta
    if out:
        return RW.oracle("delta", False, "%s outside=%s delta=%s" % (DELTA_OUTSIDE, _j(out), _j(delta))), delta
    return RW.oracle("delta", True, "delta=%s" % _j(delta)), delta


# ---------------------------------------------------------------- oracle 3: vision witness
def witness_oracle(intent, delta, base_png, after_png, differ=None, footprint=FOOTPRINT,
                   slack=FOOTPRINT_SLACK, strict_outside=None):
    """Script lane: None when a frame is missing (delta and expect still decide). Gui lane: a missing
    frame is a FAILED witness, never an omitted one (xr-core R3-1). `strict_outside`: None = by lane (gui: a change outside the
    footprint is a spill, as in F1; script: outside regions are reported, not deciding, because the
    outliner and the status bar repaint on every scene edit), True/False = forced."""
    if not base_png or not after_png:
        if intent.get("lane") == "gui":   # pixels are the only evidence of a gui turn: no frame, no accept
            return RW.oracle("witness", False, "%s no frame (base=%s after=%s)"
                             % (WITNESS_ERROR, bool(base_png), bool(after_png)))
        return None
    try:
        regions = (differ or B.diff_regions)(base_png, after_png)
        touching, spill = split_regions(regions, footprint, slack)
    except Exception as e:  # noqa: BLE001
        return RW.oracle("witness", False, "%s %s: %s" % (WITNESS_ERROR, type(e).__name__, e))
    strict = (intent.get("lane") == "gui") if strict_outside is None else bool(strict_outside)
    must_show = any(k.startswith(VISIBLE_PREFIXES) or k in VISIBLE_KEYS for k in delta) or intent.get("lane") == "gui"
    detail = "in=%d out=%d first=%s" % (len(touching), len(spill), _j((touching or spill or [None])[0]))
    if strict and spill:
        return RW.oracle("witness", False, "%s %s" % (WITNESS_SPILL, detail))
    if must_show and not touching:
        return RW.oracle("witness", False, "%s %s" % (WITNESS_NO_CHANGE, detail))
    return RW.oracle("witness", True, detail)


def rollback_witness(base_png, restored_png, differ=None, footprint=FOOTPRINT):
    """Oracle dict for the frame after a restore: no changed region may touch the footprint.
    Pixels outside it (status bar, splash) are not part of the claim (rw-build open risk 2)."""
    if not base_png or not restored_png:
        return RW.oracle("witness", False, "%s no frame" % WITNESS_ERROR)
    try:
        regions = (differ or B.diff_regions)(base_png, restored_png)
        touching, spill = split_regions(regions, footprint, 0)
    except Exception as e:  # noqa: BLE001
        return RW.oracle("witness", False, "%s %s: %s" % (WITNESS_ERROR, type(e).__name__, e))
    if touching:
        return RW.oracle("witness", False, "%s in=%d first=%s" % (WITNESS_NOT_RESTORED, len(touching), _j(touching[0])))
    return RW.oracle("witness", True, "restored in=0 out=%d" % len(spill))


# ---------------------------------------------------------------- oracle 4: cost
def cost_oracle(after):
    ms = after.get("job_ms")
    ok = isinstance(ms, (int, float)) and not isinstance(ms, bool) and math.isfinite(ms) and ms >= 0
    return RW.oracle("cost", ok, "job_ms=%s" % (int(ms) if ok else "unknown"))


def cost_ms(judge_rec):
    """job ms recorded in a rec:judge, or None."""
    for o in judge_rec.get("oracles", []):
        if o["name"] == "cost" and o["ok"]:
            try:
                return int(o["detail"].split("=", 1)[1])
            except (IndexError, ValueError):
                return None
    return None


def best(judge_recs):
    """The accepted rec:judge to commit, or None when every attempt was rejected ("all are
    discarded"). Tie-break between accepted attempts: lowest job ms, then attempt order."""
    picked = [(i, r) for i, r in enumerate(judge_recs) if r.get("pick") is True]
    if not picked:
        return None
    return min(picked, key=lambda ir: (cost_ms(ir[1]) is None, cost_ms(ir[1]) or 0, ir[0]))[1]


# ---------------------------------------------------------------- the judge
def judge(attempt, base, after, run_probe=None, differ=None, footprint=FOOTPRINT,
          slack=FOOTPRINT_SLACK, strict_outside=None):
    intent = attempt.get("intent") or {}
    oracles = []
    o = expect_oracle(intent, after.get("digest"), run_probe)
    if o is not None:
        oracles.append(o)
    o, delta = delta_oracle(intent, attempt.get("verdict"), base.get("digest"), after.get("digest"))
    oracles.append(o)
    o = witness_oracle(intent, delta, base.get("frame_png"), after.get("frame_png"), differ=differ,
                       footprint=footprint, slack=slack, strict_outside=strict_outside)
    if o is not None:
        oracles.append(o)
    oracles.append(cost_oracle(after))
    # GUARD pick: every deciding (non-cost) oracle must hold; the schema re-checks it (BAD_PICK)
    pick = all(x["ok"] for x in oracles if x["name"] != "cost")
    return RW.judge_rec(attempt["attempt"], pick, oracles)


def make_judge(differ=None, footprint=FOOTPRINT, slack=FOOTPRINT_SLACK, strict_outside=None):
    """A judge with the R0 call signature and a fixed differ / footprint (what R2's constructor takes)."""
    def _judge(attempt, base, after, run_probe=None):
        return judge(attempt, base, after, run_probe=run_probe, differ=differ, footprint=footprint,
                     slack=slack, strict_outside=strict_outside)
    return _judge


def reject_reason(judge_rec):
    """Short reason for txn rollback.reason: detail code of the first failed deciding oracle."""
    for o in judge_rec.get("oracles", []):
        if o["name"] != "cost" and not o["ok"]:
            return "%s:%s" % (o["name"], (o["detail"].split(" ", 1)[0] or "failed"))
    return None
