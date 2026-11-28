# SPDX-License-Identifier: AGPL-3.0-only
"""Run configured local stages with content-bound parents, evidence and resume."""
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from evidence import (Rejected, atomic, checked_json, confined, demand, digest, encode, file_sha,
                      pin, read_pin)
import scope

PLAN_SCHEMA = "viewpoint.motion-scene.plan/v1"
RECEIPT_SCHEMA = "viewpoint.motion-scene.stage/v1"
EVIDENCE_SCHEMA = "viewpoint.motion-scene.evidence/v1"
ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def names(value):
    demand(type(value) is list and all(type(name) is str and ID.fullmatch(name) for name in value)
           and len(set(value)) == len(value), "invalid_stage_names")
    return value


def argv(value):
    demand(type(value) is list and value and all(type(word) is str and word and "\0" not in word
           for word in value), "invalid_stage_argv")
    return value


def parse_plan(raw):
    plan = checked_json(raw)
    demand(type(plan) is dict and plan.keys() <= {"schema", "stages", "quiet_hours"}
           and plan.get("schema") == PLAN_SCHEMA and type(plan.get("stages")) is list
           and plan["stages"], "invalid_stage_plan")
    quiet = plan.get("quiet_hours")
    if quiet is not None:
        from datetime import time
        from zoneinfo import ZoneInfo
        demand(type(quiet) is list and len(quiet) == 3 and all(type(item) is str for item in quiet),
               "invalid_quiet_hours")
        demand(all(time.fromisoformat(item).tzinfo is None for item in quiet[:2]), "invalid_quiet_hours")
        ZoneInfo(quiet[2])
    seen = {}
    for stage in plan["stages"]:
        fields = {"name", "parents", "argv", "outputs", "evidence"}
        demand(type(stage) is dict and fields <= stage.keys() <= fields | {"producer"}, "invalid_stage")
        name = stage["name"]
        names([name])
        demand(name not in seen and set(names(stage["parents"])) <= seen.keys(), "invalid_stage_dependencies")
        names(stage["evidence"])
        argv(stage["argv"])
        if "producer" in stage:
            argv(stage["producer"])
            demand(bool(stage["evidence"]), "producer_without_evidence")
        outputs = stage["outputs"]
        demand(type(outputs) is dict and bool(outputs), "stage_outputs_missing")
        names(list(outputs))
        demand(all(type(relative) is str for relative in outputs.values())
               and len(set(outputs.values())) == len(outputs), "invalid_stage_outputs")
        for relative in outputs.values():
            path = Path(relative)
            demand(not path.is_absolute() and path.parts and relative == path.as_posix()
                   and ".." not in path.parts and path.parts[0] not in
                   {".inputs", "receipt.json", "command.log", "producer.log", "produced"},
                   "reserved_stage_output")
        seen[name] = stage
    return plan


def evidence_at(path, name, plan_sha, parents):
    demand(path.resolve() == path, "symlink_refused")
    raw = path.read_bytes()
    value = checked_json(raw)
    demand(type(value) is dict and value.keys() == {"schema", "stage", "plan_sha256", "parents", "files"}
           and value["schema"] == EVIDENCE_SCHEMA and value["stage"] == name
           and value["plan_sha256"] == plan_sha and value["parents"] == parents,
           "evidence_binding_mismatch:" + name)
    demand(type(value["files"]) is dict, "invalid_evidence_files")
    names(list(value["files"]))
    for ref in value["files"].values():
        read_pin(path.parent, ref)
    return value["files"], digest(raw)


def substitute(words, values):
    def replace(match):
        key = match[1]
        demand(key in values, "missing_stage_value:" + key)
        return str(values[key])
    command = [PLACEHOLDER.sub(replace, word) for word in words]
    if Path(command[0]).name == "blender":
        demand("-b" in command[1:] and "--disable-autoexec" in command[1:], "foreground_blender_refused")
    return command


def invoke(words, values, directory, log, quiet):
    scope.checkpoint(quiet)
    command = substitute(words, values)
    env = scope.environment() | {"TMPDIR": str(directory)}
    with log.open("xb") as transcript:
        result = subprocess.run(command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                stdout=transcript, stderr=subprocess.STDOUT, check=False)
    scope.checkpoint(quiet)
    demand(result.returncode == 0, f"stage_command_failed:{result.returncode}")
    return {"argv": words, "rc": result.returncode}


def replay(root, stage, plan_sha, parents):
    row = checked_json(confined(root, "receipt.json").read_bytes())
    demand(row["schema"] == RECEIPT_SCHEMA and row["status"] == "PASS"
           and row["stage"] == stage["name"] and row["plan_sha256"] == plan_sha
           and row["parents"] == parents and row["outputs"].keys() == stage["outputs"].keys()
           and row["inputs"].keys() == set(stage["evidence"]), "stage_lineage_mismatch")
    for key, ref in row["outputs"].items():
        demand(ref["path"] == stage["outputs"][key], "stage_output_path_mismatch")
        read_pin(root, ref)
    for key, ref in row["inputs"].items():
        demand(ref["path"] == ".inputs/" + key, "stage_input_path_mismatch")
        read_pin(root, ref)
    log_paths = {"stage": "command.log"}
    expected_commands = [{"argv": stage["argv"], "rc": 0}]
    if "producer" in row["logs"]:
        demand("producer" in stage, "unexpected_stage_producer")
        log_paths["producer"] = "producer.log"
        expected_commands.insert(0, {"argv": stage["producer"], "rc": 0})
    demand(row["commands"] == expected_commands and row["logs"].keys() == log_paths.keys(),
           "stage_command_lineage_mismatch")
    for key, ref in row["logs"].items():
        demand(ref["path"] == log_paths[key], "stage_log_path_mismatch")
        read_pin(root, ref)
    return row


def run_stage(root, stage, plan_sha, parents, prior, supplied, quiet):
    name = stage["name"]
    values = {"plan_sha256": plan_sha, "stage": name}
    values["parents"] = encode(parents).decode().strip()
    for parent in stage["parents"]:
        for key, ref in prior[parent]["outputs"].items():
            values[f"parent:{parent}:{key}"] = read_pin(root / parent, ref)
    temp = TemporaryDirectory(prefix="." + name + "-", dir=root)
    with temp as staging:
        staging = Path(staging)
        values["output"] = staging
        commands, logs, sources = [], {}, {}
        if stage["evidence"]:
            if name in supplied:
                manifest = supplied[name]
            elif "producer" in stage:
                produced = staging / "produced"
                produced.mkdir()
                manifest = produced / "evidence.json"
                log = staging / "producer.log"
                commands.append(invoke(stage["producer"], values | {"evidence_manifest": manifest},
                                       staging, log, quiet))
                logs["producer"] = pin(staging, log)
            else:
                raise Rejected("evidence_missing:" + name, f"provide a bound evidence manifest for {name}")
            files, manifest_sha = evidence_at(manifest, name, plan_sha, parents)
            demand(files.keys() == set(stage["evidence"]), "evidence_roster_mismatch")
            saved = staging / ".inputs"
            saved.mkdir()
            for key, ref in files.items():
                destination = confined(saved, key)
                shutil.copyfile(read_pin(manifest.parent, ref), destination)
                demand(file_sha(destination) == ref["sha256"], "evidence_changed_during_copy")
                sources[key] = pin(staging, destination)
                values[f"evidence:{key}"] = destination
            if "producer" in logs:
                shutil.rmtree(produced)
        else:
            manifest_sha = None
        log = staging / "command.log"
        commands.append(invoke(stage["argv"], values, staging, log, quiet))
        logs["stage"] = pin(staging, log)
        for ref in sources.values():
            read_pin(staging, ref)
        for parent in stage["parents"]:
            demand(file_sha(root / parent / "receipt.json") == parents[parent], "stage_parent_receipt_changed")
            for ref in prior[parent]["outputs"].values():
                read_pin(root / parent, ref)
        outputs = {key: pin(staging, confined(staging, relative)) for key, relative in stage["outputs"].items()}
        row = {"schema": RECEIPT_SCHEMA, "stage": name, "status": "PASS", "plan_sha256": plan_sha,
               "parents": parents, "input_manifest_sha256": manifest_sha, "inputs": sources,
               "outputs": outputs, "logs": logs, "commands": commands}
        atomic(staging / "receipt.json", encode(row))
        staging.rename(root / name)
    return row


def run(plan_file, root, supplied):
    raw = plan_file.read_bytes()
    plan = parse_plan(raw)
    plan_sha = digest(raw)
    demand(set(supplied) <= {stage["name"] for stage in plan["stages"]}, "unknown_evidence_stage")
    root.mkdir(parents=True, exist_ok=True)
    with confined(root, ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = confined(root, "plan.json")
        if identity.exists():
            demand(identity.read_bytes() == raw, "run_plan_changed")
        else:
            demand(all(path.name == ".lock" for path in root.iterdir()), "run_directory_occupied")
            atomic(identity, raw)
        receipts, prior = {}, {}
        for stage in plan["stages"]:
            scope.checkpoint(plan.get("quiet_hours"))
            name = stage["name"]
            parents = {parent: receipts[parent]["sha256"] for parent in stage["parents"]}
            directory = confined(root, name)
            if directory.exists():
                row = replay(directory, stage, plan_sha, parents)
                if name in supplied:
                    _, manifest_sha = evidence_at(supplied[name], name, plan_sha, parents)
                    demand(row["input_manifest_sha256"] == manifest_sha, "resumed_evidence_changed:" + name)
            else:
                row = run_stage(root, stage, plan_sha, parents, prior, supplied, plan.get("quiet_hours"))
            receipts[name] = pin(root, directory / "receipt.json")
            prior[name] = row
        result = {"schema": "viewpoint.motion-scene.run/v1", "status": "PASS", "plan_sha256": plan_sha,
                  "stages": receipts}
        receipt = confined(root, "receipt.json")
        if receipt.exists():
            demand(checked_json(receipt.read_bytes()) == result, "run_receipt_changed")
        else:
            atomic(receipt, encode(result))
        return result
