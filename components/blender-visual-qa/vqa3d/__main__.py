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
    probe.add_argument("--render", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("--adapter", required=True, choices=ADAPTERS)
    check.add_argument("--scenario", required=True, type=Path)
    check.add_argument("--record-trace", type=Path)
    check.add_argument("--process-log", type=Path)
    check.add_argument("--source", type=Path, help="Blender .blend source (read only)")
    check.add_argument("--scratch-parent", type=Path, help="Parent for owned Blender scratch files")
    check.add_argument("--mjcf", type=Path, help="MuJoCo simulation MJCF source (read only)")
    check.add_argument("--profile", help="Apply profile defaults; explicit parameters win")
    profiles = sub.add_parser("profile")
    profile_sub = profiles.add_subparsers(dest="profile_command", required=True)
    profile_sub.add_parser("list")
    show = profile_sub.add_parser("show")
    show.add_argument("id")
    profile_check = profile_sub.add_parser("check")
    profile_check.add_argument("--profile", required=True)
    profile_check.add_argument("--adapter", choices=ADAPTERS)
    profile_check.add_argument("--source", type=Path)
    profile_check.add_argument("--mjcf", type=Path)
    profile_check.add_argument("--frames", help="Inclusive start,end,step")
    profile_check.add_argument("--record-trace", type=Path)
    profile_check.add_argument("--process-log", type=Path)
    for command in (check, profile_check, probe):
        command.add_argument("--blender-env", action="append", default=[], metavar="KEY=VALUE")
        command.add_argument("--nice", type=int, default=10)
        command.add_argument("--cpus")
        command.add_argument("--capture-engine", choices=("workbench", "eevee", "cycles"))
        command.add_argument("--capture-samples", type=int)
    profile_check.add_argument("--gates-only", action="store_true")
    profile_check.add_argument("--frame", type=Path)
    profile_check.add_argument("--floor", metavar="KEY=VALUE",
                               help="Contact floor of this scene: z_um=<int>, object=<name> or collection=<name>")
    profile_check.add_argument("--attach", action="append", default=[], metavar="OBJECT=MODE",
                               help="Prop of this scene: <object>=in_grip or <object>=rigid_follow (repeatable)")
    args = parser.parse_args(argv)
    def controls(kwargs):
        if getattr(args, "adapter", None) not in (None, "blender"):
            return kwargs
        declared = dict(kwargs.get("blender_env", {}))
        for item in args.blender_env:
            if "=" not in item:
                raise ValueError("blender_env_requires_key_value")
            key, value = item.split("=", 1)
            declared[key] = value
        kwargs.update(blender_env=declared, nice=args.nice, cpus=args.cpus)
        if args.capture_engine:
            kwargs["capture_engine"] = args.capture_engine
        if args.capture_samples is not None:
            kwargs["capture_samples"] = args.capture_samples
        return kwargs
    adapters, verdicts = [], []
    try:
        if args.command == "profile":
            from . import profile as policies
            if args.profile_command == "list":
                for path in sorted(policies.DIRECTORY.glob("*.json")):
                    policy = policies.load(path.stem)
                    emit({"id": policy["id"], "intent": policy["intent"], "profile": policies.identity(policy)})
                return 0
            if args.profile_command == "show":
                emit(policies.load(args.id))
                return 0
            policy = policies.load(args.profile)
            name = args.adapter or ("mujoco" if policy["id"] == "simulation" else "blender")
            if args.source and name != "blender":
                raise ValueError("source_requires_blender")
            if args.mjcf and name != "mujoco":
                raise ValueError("mjcf_requires_mujoco_simulation")
            floor = attach = None
            if args.floor is not None:
                key, _, value = args.floor.partition("=")
                if key not in {"z_um", "object", "collection"} or not value:
                    raise ValueError("floor_requires_z_um_object_or_collection")
                floor = policies.validate_floor({key: int(value) if key == "z_um" else value})
            if args.attach:
                if any("=" not in item for item in args.attach):
                    raise ValueError("attach_requires_object_mode")
                attach = policies.validate_attach(dict(item.rsplit("=", 1) for item in args.attach))
            capture_reason = None
            try:
                kwargs = policies.adapter_kwargs(policy, ADAPTERS[name])
            except policies.CannotSee as exc:
                kwargs, capture_reason = {}, str(exc)
            if args.source:
                kwargs["source"] = args.source
            if args.mjcf:
                kwargs["mjcf"] = args.mjcf
            if name == "blender":
                controls(kwargs)
                kwargs.update(profile_session=True, timeout=policy.get("sampling", {}).get("budget_s", 600))
            adapter = create_adapter(name, **kwargs)
            adapters.append(adapter)
            metadata = None
            if name == "blender":
                metadata = lambda: policies.blender_metadata(adapter)
            frames = None if args.frames is None else [int(v) for v in args.frames.split(",")]
            if frames is not None:
                policies.frames_from_spec(frames)
            records, result, trace = policies.run(policy, adapter, frames=frames, capture_reason=capture_reason,
                factory=lambda: create_adapter(name, **kwargs), metadata=metadata, emit=emit, gates_only=args.gates_only, frame_png=args.frame.read_bytes() if args.frame else None,
                floor=floor, attach=attach)
            if args.record_trace:
                atomic_json(args.record_trace, trace)
            if args.process_log:
                adapter.close()
                atomic_json(args.process_log, {"adapter": name, "processes": getattr(adapter, "process_logs", [])})
            return result["verdict_rc"]
        if args.command == "route":
            result = route_intent(args.intent)
            emit(result)
            return result["rc"]
        if args.command == "probe":
            for name in [args.adapter] if args.adapter else ADAPTERS:
                try:
                    adapter = create_adapter(name, **(controls({}) if name == "blender" else {}))
                    adapters.append(adapter)
                    result = adapter.probe(render=True) if args.render else adapter.probe()
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
        policy = None
        capture_reason = None
        if args.profile:
            from . import profile as policies
            policy = policies.load(args.profile)
            scenario = [{**step, "obligation": policies.apply_defaults(step["obligation"], policy)} for step in scenario]
        for step in scenario:
            if type(step) is not dict or set(step) != {"action", "obligation"} or type(step["action"]) is not dict:
                raise ValueError("invalid_scenario_step")
            validate_obligation(step["obligation"])
            if args.adapter == "blender":
                from .adapters.blender import validate_action
                validate_action(step["action"])
        kwargs = {}
        if policy:
            try:
                kwargs.update(policies.adapter_kwargs(policy, ADAPTERS[args.adapter]))
            except policies.CannotSee as exc:
                capture_reason = str(exc)
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
        if args.adapter == "blender":
            controls(kwargs)
        adapter = create_adapter(args.adapter, **kwargs)
        adapters.append(adapter)
        recorder = RecordingAdapter(adapter) if args.record_trace else adapter
        if policy:
            adapter.profile_identity = recorder.profile_identity = policies.identity(policy)
        receipts = []
        for step in scenario:
            if capture_reason and step["obligation"]["kind"] in policies.PIXELS:
                from .receipt import make_receipt
                receipt = make_receipt(recorder, step["obligation"], None, None,
                    [{"oracle": step["obligation"]["kind"], "verdict": "CANNOT_SEE", "reason": capture_reason}])
            else:
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
