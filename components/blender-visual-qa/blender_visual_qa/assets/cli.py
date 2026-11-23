"""`asset` command: dispatch inspect|normalize|mesh-gate|turntable|render-sheet|lod-ladder|hull-gen|ortho4|orm-pack|heal|self-isect|bake-pbr|uv-gate|pbr-validate|delight and bind each stage receipt into a sha256 chain."""
# @module {"id":"bv.assets.cli","layer":4,"owner":"BINT"}
import contextlib
import hashlib
import importlib
import io
import json
import sys

from blender_visual_qa.assets import limits

SCHEMA = "bvqa.asset-receipt/v1"
STAGES = {"inspect": "inspect_gate", "normalize": "normalize", "mesh-gate": "mesh_gate", "turntable": "turntable",
          "render-sheet": "render_sheet", "lod-ladder": "lod_ladder", "hull-gen": "hull_gen", "ortho4": "ortho4", "orm-pack": "orm_pack",
          "heal": "heal", "self-isect": "self_isect", "bake-pbr": "bake_pbr", "uv-gate": "uv_gate", "pbr-validate": "pbr_validate", "delight": "delight"}
INPUT_FLAGS = ("--in", "--glb", "--mesh")
RC_SET = (limits.RC_OK, limits.RC_USAGE, limits.RC_INCOMPLETE, limits.RC_STALE, limits.RC_FAIL, limits.RC_BUDGET)


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _flag(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return None


def _strip(argv, name):
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == name:
            skip = True
        elif not a.startswith(name + "="):
            out.append(a)
    return out


def _file_sha(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _prev_sha(chain):
    try:
        with open(chain, encoding="utf-8") as fh:
            lines = [s for s in fh.read().splitlines() if s.strip()]
    except FileNotFoundError:
        return None
    return json.loads(lines[-1])["receipt_sha256"] if lines else None


def _run_stage(module, argv):
    buf = io.StringIO()
    try:
        target = importlib.import_module("blender_visual_qa.assets." + module)
    except ModuleNotFoundError as exc:
        if exc.name == "blender_visual_qa.assets." + module:
            return limits.RC_INCOMPLETE, {"status": "E_INCOMPLETE", "reason": "stage_not_built", "stage": module}
        raise
    with contextlib.redirect_stdout(buf):
        try:
            rc = target.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else limits.RC_USAGE
    rec = {}
    lines = [s for s in buf.getvalue().splitlines() if s.strip()]
    if lines:
        try:
            rec = json.loads(lines[-1])
        except ValueError:
            rec = {"status": "E_OUTPUT", "raw": lines[-1][:200]}
    if not isinstance(rec, dict):
        rec = {"value": rec}
    # Domain rc 1 is a gate failure; any rc outside the stage set is also a failure.
    rc = limits.RC_FAIL if rc not in RC_SET else rc
    return rc, rec


# @anchor:bv.assets.cli.main
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in STAGES:
        print(canon({"code": "E_USAGE", "rc": limits.RC_USAGE, "reason": "asset stage must be one of " + ",".join(sorted(STAGES))}))
        return limits.RC_USAGE
    stage, rest = args[0], args[1:]
    chain = _flag(rest, "--chain")
    rest = _strip(rest, "--chain")
    prev = _prev_sha(chain) if chain else None
    rc, rec = _run_stage(STAGES[stage], rest)
    inp = rec.get("input_sha256")
    if inp is None:
        for name in INPUT_FLAGS:
            path = _flag(rest, name)
            if path:
                inp = _file_sha(path)
                break
    env = {"schema": SCHEMA, "stage": stage, "rc": rc, "input_sha256": inp, "prev_receipt_sha256": prev, "stage_receipt": rec}
    env["receipt_sha256"] = hashlib.sha256(canon(env).encode("utf-8")).hexdigest()
    line = canon(env)
    print(line)
    if chain:
        with open(chain, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
