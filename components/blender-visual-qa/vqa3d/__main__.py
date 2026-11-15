"""Verdict-command CLI. Scenario: list of {action, obligation} records."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from .canon import canonical_json, sha256_canonical
from .check import validate_obligation, verify_step
from .contract import AdapterNotImplemented
from .fakes import RecordingAdapter, atomic_json
from .registry import ADAPTERS, create_adapter
from .route_intent import route_intent
from .verdict import max_precedence, rc_of


def emit(value):
    print(canonical_json(value), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route")
    route.add_argument("intent")
    probe = sub.add_parser("probe")
    probe.add_argument("--adapter", choices=ADAPTERS)
    check = sub.add_parser("check")
    check.add_argument("--adapter", required=True, choices=ADAPTERS)
    check.add_argument("--scenario", required=True, type=Path)
    check.add_argument("--record-trace", type=Path)
    check.add_argument("--process-log", type=Path)
    check.add_argument("--source", type=Path, help="Blender .blend source (read only)")
    check.add_argument("--scratch-parent", type=Path, help="Parent for owned Blender scratch files")
    check.add_argument("--mjcf", type=Path, help="MuJoCo simulation MJCF source (read only)")
    args = parser.parse_args(argv)
    adapters, verdicts = [], []
    try:
        if args.command == "route":
            result = route_intent(args.intent)
            emit(result)
            return result["rc"]
        if args.command == "probe":
            for name in [args.adapter] if args.adapter else ADAPTERS:
                try:
                    adapter = create_adapter(name)
                    adapters.append(adapter)
                    result = adapter.probe()
                except Exception as exc:
                    result = {"id": name, "version": ADAPTERS[name].version, "status": ADAPTERS[name].status,
                              "available": False, "reason": type(exc).__name__ + ":" + str(exc),
                              "capabilities": {"capture_available": False, "scene_state": False, "act": False}}
                emit(result)
                verdicts.append("PASS" if result.get("available") and result["status"] == "implemented" else "CANNOT_SEE")
            return rc_of(max_precedence(verdicts))
        raw = args.scenario.read_bytes()
        scenario = json.loads(raw)
        canonical_json(scenario)
        if type(scenario) is not list or not scenario:
            raise ValueError("nonempty_scenario_list_required")
        for step in scenario:
            if type(step) is not dict or set(step) != {"action", "obligation"} or type(step["action"]) is not dict:
                raise ValueError("invalid_scenario_step")
            validate_obligation(step["obligation"])
            if args.adapter == "blender":
                from .adapters.blender import validate_action
                validate_action(step["action"])
        kwargs = {}
        if args.scratch_parent:
            if args.adapter != "blender":
                raise ValueError("scratch_parent_requires_blender")
            kwargs["scratch_parent"] = args.scratch_parent
        if args.source:
            if args.adapter != "blender":
                raise ValueError("source_requires_blender")
            kwargs["source"] = args.source
        if args.mjcf:
            if args.adapter != "mujoco":
                raise ValueError("mjcf_requires_mujoco_simulation")
            kwargs["mjcf"] = args.mjcf
        adapter = create_adapter(args.adapter, **kwargs)
        adapters.append(adapter)
        recorder = RecordingAdapter(adapter) if args.record_trace else adapter
        receipts = []
        for step in scenario:
            receipt = verify_step(recorder, step["action"], step["obligation"])
            receipts.append(receipt)
            emit(receipt)
            verdicts.append(receipt["final_verdict"])
        if args.record_trace:
            provenance = {"environment": "MuJoCo simulation" if args.adapter == "mujoco" else "Blender",
                          "scenario_sha256": hashlib.sha256(raw).hexdigest(),
                          "runtime_version": getattr(adapter, "runtime_version", None),
                          "recording": "live", "scope": "same host and runtime only"}
            if args.adapter == "blender":
                provenance["source_sha256"] = hashlib.sha256(adapter.source.read_bytes()).hexdigest()
            if args.adapter == "mujoco" and adapter.mujoco is not None:
                from .adapters.mujoco_sim import DEFAULT_MJCF
                provenance["runtime_version"] = adapter.mujoco.__version__
                provenance["backend"] = adapter.backend
                provenance["model_sha256"] = hashlib.sha256(args.mjcf.read_bytes() if args.mjcf else DEFAULT_MJCF.encode()).hexdigest()
            atomic_json(args.record_trace, recorder.trace(scenario, receipts, provenance))
        if args.process_log:
            atomic_json(args.process_log, {"adapter": args.adapter, "processes": getattr(adapter, "process_logs", [])})
        return rc_of(max_precedence(verdicts))
    except Exception as exc:
        emit({"final_verdict": "ERROR", "verdict_rc": 2, "error": type(exc).__name__ + ":" + str(exc)})
        return 2
    finally:
        for adapter in adapters:
            try:
                adapter.close()
            except AdapterNotImplemented:
                pass
            except Exception:
                if args.command != "probe":
                    raise


if __name__ == "__main__":
    sys.exit(main())
