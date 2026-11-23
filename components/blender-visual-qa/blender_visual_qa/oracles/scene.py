"""Fold scene.jsonl (bvqa.oracle-sd/v1) into verdicts. Reads JSON only; no bpy."""
import json
import os

from . import harness

SCHEMA = "bvqa.oracle-sd/v1"
# oracle ids whose FAIL only advises (BSPEC 2.2: 1..7 clipped corners is advisory; budgets are project policy)
DEFAULT_ADVISORY = frozenset()


class SceneError(Exception):
    pass


# @anchor:bv.oracles.scene.load
def load_scene(path):
    """Return (rows, summary). Trailer is mandatory (E_SCHEMA otherwise); rows are validated and sorted-checked."""
    rows, summary = [], None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().split("\n") if ln.strip()]
    except OSError as exc:
        raise SceneError("E_IO:" + str(exc))
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            raise SceneError("E_SCHEMA:bad_json")
        if obj.get("schema") != SCHEMA:
            raise SceneError("E_SCHEMA:schema")
        if "summary" in obj:
            if summary is not None:
                raise SceneError("E_SCHEMA:duplicate_trailer")
            summary = obj["summary"]
            continue
        if summary is not None:
            raise SceneError("E_SCHEMA:row_after_trailer")
        try:
            harness.validate_row(obj)
        except harness.OracleError as exc:
            raise SceneError("E_SCHEMA:" + str(exc))
        rows.append(obj)
    if summary is None:
        raise SceneError("E_SCHEMA:trailer_missing")
    keys = [(r["oracle_id"], r.get("object") or "", -1 if r.get("frame") is None else r["frame"]) for r in rows]
    if keys != sorted(keys):
        raise SceneError("E_SCHEMA:unsorted")
    return rows, summary


def count_summary(rows):
    return {"rows": len(rows),
            "fail": sum(1 for r in rows if r["verdict"] == harness.FAIL),
            "cannot_see_count": sum(1 for r in rows if r["verdict"] == harness.CANNOT_SEE),
            "advisory": sum(1 for r in rows if r["verdict"] == harness.ADVISORY)}


# @anchor:bv.oracles.scene.evaluate
def evaluate(rows, summary=None, required=None, advisory=frozenset(), strict_cannot_see=False):
    """Per-oracle verdicts and the folded scene verdict.

    required: oracle ids that MUST appear (missing -> CANNOT_SEE, never PASS). advisory: oracle ids whose FAIL is
    downgraded to ADVISORY. strict_cannot_see: O-B10, any CANNOT_SEE row makes the scene FAIL.
    The trailer is recomputed; a lying trailer is an error.
    """
    recomputed = count_summary(rows)
    if summary is not None and summary != recomputed:
        raise SceneError("E_SCHEMA:trailer_mismatch")
    per = {}
    for r in rows:
        v = r["verdict"]
        if v == harness.FAIL and r["oracle_id"] in advisory:
            v = harness.ADVISORY
        per.setdefault(r["oracle_id"], []).append(v)
    verdicts = {oid: harness.max_precedence(vs) for oid, vs in sorted(per.items())}
    for oid in sorted(required or ()):
        if oid not in verdicts:
            verdicts[oid] = harness.CANNOT_SEE
    folded = harness.max_precedence(list(verdicts.values())) if verdicts else harness.CANNOT_SEE
    if strict_cannot_see and recomputed["cannot_see_count"] > 0:
        folded = harness.FAIL
    return {"verdict": folded, "rc": harness.rc_of(folded), "oracles": verdicts, "summary": recomputed}


def failing_ids(result):
    return sorted(oid for oid, v in result["oracles"].items() if v == harness.FAIL)


def write_result(result, out_path):
    """R4 staging: .tmp then os.replace."""
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(harness.canonical_json(result) + "\n")
    os.replace(tmp, out_path)
