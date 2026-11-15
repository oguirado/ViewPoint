"""Blender command dispatch. Unavailable pipeline stages fail closed."""
# @module {"id":"bv.cli","layer":4,"owner":"BINT"}
from __future__ import annotations
import argparse
import importlib
import json
from pathlib import Path
import sys

from blender_visual_qa import contracts, report


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def emit_stage(code, rc, **detail):
    print(contracts.canonical_json(dict(code=code, rc=rc, **detail)), end="")
    return rc


def emit_error(args, code, rc, reason):
    if args and args[0] in ("run", "report", "judge"):
        result = report.reconcile(command=args[0], scope="render", errors=[{"code": code.lower(), "stage": "cli", "message": reason}])
        return report.emit(result)
    return emit_stage(code, rc, reason=reason)


def stage_to_verdict(rc, code=None, reason=None):
    """R-B3 mapping: a precondition failure is an ERROR."""
    if rc == 0:
        return 0
    if rc in (2, 4, 6):
        return 2
    if rc == 3:
        return 3
    if rc == 5:
        return 2 if reason in ("src_mutated", "mutant_noop", "argv_forbidden") else 1
    raise ValueError("invalid stage rc")


def _dispatch(module, symbol, argv):
    try:
        target = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if module == exc.name or module.startswith(exc.name + "."):
            return emit_stage("E_INCOMPLETE", 3, reason="stage_not_built", stage=module)
        raise
    return getattr(target, symbol)(argv)


# @anchor:bv.cli.main
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = Parser(prog="blender-visual-qa", description=__doc__)
        parser.add_argument("command", choices=("drive", "drive-check", "validate", "run", "judge", "report", "selftest", "calibrate", "mutate", "asset"))
        ns, rest = parser.parse_known_args(args)
        if ns.command in ("drive", "drive-check"):
            return _dispatch("blender_visual_qa.drive.launch", "check_main" if ns.command == "drive-check" else "main", rest)
        if ns.command == "asset":
            return _dispatch("blender_visual_qa.assets.cli", "main", rest)
        if ns.command == "calibrate":
            return _dispatch("blender_visual_qa.truth.calibrate", "main", rest)
        if ns.command == "mutate":
            return emit_stage("E_INCOMPLETE", 3, reason="oracles_stage_not_built")
        if ns.command == "validate":
            p = Parser(prog="blender-visual-qa validate")
            for name in ("adapter", "checklist", "plan"):
                p.add_argument("--" + name, required=True)
            o = p.parse_args(rest)
            cfg = contracts.load(o.adapter, o.checklist, o.plan)
            return emit_stage("OK", 0, run_id=cfg.run_id, configuration_sha256=cfg.configuration_sha256)
        if ns.command == "report":
            p = Parser(prog="blender-visual-qa report")
            p.add_argument("--from-run", required=True)
            o = p.parse_args(rest)
            bundle = contracts.verify_run(o.from_run)
            return report.emit(bundle.report)
        if ns.command == "judge":
            p = Parser(prog="blender-visual-qa judge")
            p.add_argument("--from-run", required=True)
            p.add_argument("--arm", required=True, choices=("A", "B", "C"))
            o = p.parse_args(rest)
            # B5 owns evidence staging and live dispatch; do not fabricate a verdict.
            return report.emit(report.reconcile(command="judge", scope="render", incomplete=["stored-evidence arm staging not built: " + o.arm]))
        if rest:
            raise UsageError("unrecognized arguments: " + " ".join(rest))
        return report.emit(report.reconcile(command=ns.command, scope="render", incomplete=[ns.command + " pipeline not built"]))
    except UsageError as exc:
        return emit_error(args, "E_USAGE", 2, str(exc))
    except contracts.ContractError as exc:
        mapping = {"file_not_found": "E_IO", "io_error": "E_IO", "invalid_utf8": "E_DECODE", "E_STALE": "E_STALE"}
        code = mapping.get(exc.code, "E_SCHEMA")
        # report/judge/run use verdict rc; validate is a stage.
        rc = 4 if code == "E_STALE" and args and args[0] == "validate" else 2
        return emit_error(args, code, rc, exc.message)
    except OSError as exc:
        return emit_error(args, "E_IO", 2, str(exc))


if __name__ == "__main__":
    sys.exit(main())
