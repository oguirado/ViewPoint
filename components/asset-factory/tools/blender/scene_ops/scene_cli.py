# SPDX-License-Identifier: AGPL-3.0-only
"""JSON entry point for pinned, background Blender scene operations."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from scene_contract import Refusal, canonical, fields, request, require

ENGINE = Path(__file__).with_name("scene_worker.py")
RUNTIME = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
SHA = re.compile(r"[a-f0-9]{64}")


def digest(path, copy_to=None):
    sha = hashlib.sha256()
    with path.open("rb") as source:
        if copy_to is None:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                sha.update(chunk)
        else:
            with copy_to.open("xb") as dest:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    sha.update(chunk)
                    dest.write(chunk)
    return sha.hexdigest()


def unique_keys(items):
    result = {}
    for key, value in items:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def decode(raw):
    def invalid_number(value):
        raise Refusal("INVALID_JSON_NUMBER", value)

    return json.loads(raw, object_pairs_hook=unique_keys, parse_constant=invalid_number)


def load(path):
    return decode(path.read_text(encoding="utf-8"))


def file_path(value, suffix):
    require(isinstance(value, str) and value != "" and "\x00" not in value,
            "INVALID_PATH")
    path = Path(value)
    require(path.suffix.lower() == suffix, "INVALID_PATH", str(path))
    return path


def output_path(path):
    require(path.parent.is_dir(), "OUTPUT_PARENT_MISSING", str(path.parent))
    require(not os.path.lexists(path), "OUTPUT_EXISTS", str(path))
    return path.parent.resolve(strict=True) / path.name


def run_engine(blend, command, directory):
    settings = directory / "engine-in.json"
    response = directory / "engine-out.json"
    settings.write_bytes(canonical(command))
    config = directory / "config"
    config.mkdir()
    argv = ["blender", "--background", "--factory-startup", "--disable-autoexec",
            "--threads", "1", "--python-exit-code", "11", str(blend),
            "--python", str(ENGINE), "--", str(settings), str(response)]
    env = dict(os.environ, **RUNTIME, BLENDER_USER_CONFIG=str(config),
               TMPDIR=str(directory), PYTHONDONTWRITEBYTECODE="1")
    with (directory / "blender.log").open("wb") as log:
        result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, env=env,
                                timeout=120, check=False)
    if result.returncode or not response.is_file():
        with (directory / "blender.log").open("rb") as log:
            log.seek(max(0, log.seek(0, os.SEEK_END) - 4096))
            tail = log.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Blender exit {result.returncode}: {tail}")
    reply = load(response)
    if not reply["ok"]:
        raise Refusal(reply["code"], reply["detail"], **reply["evidence"])
    return reply["result"]


def verify_pin(value):
    require(isinstance(value, str) and SHA.fullmatch(value), "INVALID_REVISION")


def snapshot(path, pin):
    path = file_path(path, ".json")
    require(path.is_file(), "SNAPSHOT_MISSING", str(path))
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    require(actual == pin, "SNAPSHOT_PIN_MISMATCH", str(path))
    try:
        reply = decode(raw)
    except (Refusal, UnicodeError, ValueError) as exc:
        raise Refusal("INVALID_SNAPSHOT", str(exc)) from exc
    require(isinstance(reply, dict) and reply.get("ok") is True
            and reply.get("operation") == "af.scene.inspect", "INVALID_SNAPSHOT")
    value = reply.get("result")
    require(isinstance(value, dict) and value.get("schema") == "vp3d.scene_snapshot/v1"
            and isinstance(value.get("objects"), dict), "INVALID_SNAPSHOT")
    verify_pin(value.get("revision_sha256"))
    return value


def publish(source, destination):
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise Refusal("OUTPUT_EXISTS", str(destination)) from exc


def execute(spec, reply_path):
    from scene_contract import compare

    operation = request(spec)
    if operation == "af.scene.diff":
        for key in ("before_sha256", "after_sha256"):
            verify_pin(spec[key])
        return compare(snapshot(spec["before"], spec["before_sha256"]),
                       snapshot(spec["after"], spec["after_sha256"]))

    source = file_path(spec["blend"], ".blend")
    require(source.is_file(), "BLEND_MISSING", str(source))
    source = source.resolve(strict=True)
    base = spec["base_revision_sha256"]
    verify_pin(base)
    edited = operation == "af.scene.edit"
    retry = spec.get("reviewed_retry_revision_sha256")
    if "reviewed_retry_revision_sha256" in spec:
        verify_pin(retry)
        require(retry != base, "INVALID_RETRY_REVISION")
    destination = output_path(file_path(spec["output_blend"], ".blend")) if edited else reply_path
    with tempfile.TemporaryDirectory(prefix=".scene-op-", dir=destination.parent) as temp:
        directory = Path(temp)
        blend = directory / "input.blend"
        actual = digest(source, blend)
        attempts = [{"expected_revision_sha256": base, "observed_revision_sha256": actual,
                     "status": "matched" if actual == base else "stale"}]
        if actual != base and retry is not None:
            blend.unlink()
            actual = digest(source, blend)
            attempts.append({"expected_revision_sha256": retry, "observed_revision_sha256": actual,
                             "status": "matched" if actual == retry else "stale"})
        require(attempts[-1]["status"] == "matched", "STALE_BASE_REVISION", attempts=attempts)
        command = {"operation": operation, "revision_sha256": actual}
        if edited:
            command.update(scene=spec["scene"], edits=spec["edits"],
                           output=str(directory / "candidate.blend"))
        try:
            result = run_engine(blend, command, directory)
        except Refusal as exc:
            exc.evidence["attempts"] = attempts
            raise
        if edited:
            candidate = directory / "candidate.blend"
            result.update(attempts=attempts, output_blend=str(destination),
                          source_revision_sha256=actual, revision_sha256=digest(candidate))
            current_revision = digest(source)
            require(current_revision == actual, "STALE_BASE_REVISION",
                    "source changed during edit", attempts=attempts,
                    pre_publish_observed_revision_sha256=current_revision)
            # A new branch is the only commit point. The base file is never opened for writing.
            publish(candidate, destination)
        return result


def write_reply(path, reply):
    with tempfile.TemporaryDirectory(prefix=".scene-reply-", dir=path.parent) as temp:
        staged = Path(temp) / "reply.json"
        staged.write_bytes(canonical(reply))
        publish(staged, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    operation = None
    try:
        destination = output_path(file_path(str(args.out), ".json"))
        try:
            spec = load(args.input)
            if isinstance(spec, dict) and isinstance(spec.get("operation"), str):
                operation = spec["operation"]
            result = execute(spec, destination)
            reply = {"ok": True, "operation": operation, "perception": "api", "result": result}
            code = 0
        except Refusal as exc:
            reply = {"ok": False, "operation": operation, "perception": "api",
                     "code": exc.code, "detail": exc.detail, "evidence": exc.evidence}
            code = 7 if exc.code in {"BLEND_MISSING", "SNAPSHOT_MISSING"} else 3
        except (OSError, ValueError, UnicodeError, RuntimeError, subprocess.SubprocessError) as exc:
            reply = {"ok": False, "operation": operation, "perception": "api",
                     "code": "HARNESS_ERROR", "detail": str(exc)}
            code = 2
        write_reply(destination, reply)
        return code
    except (OSError, Refusal) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
