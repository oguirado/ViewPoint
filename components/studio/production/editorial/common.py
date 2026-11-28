# SPDX-License-Identifier: AGPL-3.0-only
"""Shared editorial I/O, command exits and frame-clock utilities."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager

ENV = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DOCS = {
    "brief": "brief", "clay-proxies": "clay", "upstream": "upstream", "cameras": "cameras",
    "lighting": "lighting", "cue-sheet": "cues", "blocking-plan": "blocking-plan",
}


class Refusal(Exception):
    def __init__(self, reason, retry_when, next_command):
        super().__init__(reason)
        self.receipt = {"reason": reason, "retry_when": retry_when, "next_command": next_command}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def require(condition, reason, retry_when, next_command="python3 -B editorial.py discover"):
    if not condition:
        raise Refusal(reason, retry_when, next_command)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def value_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes(), parse_constant=invalid_number)


def invalid_number(value):
    raise ValueError(f"nonfinite number: {value}")


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def project(root):
    return project_snapshot(root)[0]


def project_snapshot(root):
    docs, pins = {}, {}
    for key, schema in DOCS.items():
        name = f"{key}.json"
        data = (root / name).read_bytes()
        doc = json.loads(data, parse_constant=invalid_number)
        if not isinstance(doc, dict) or doc.get("schema") != f"rp4.editorial-{schema}/v1":
            raise ValueError(f"unsupported project document: {key}.json")
        docs[key] = doc
        pins[name] = hashlib.sha256(data).hexdigest()
    return docs, pins


def slot(args, output="--out"):
    args.add_argument("--project", type=Path, required=True)
    args.add_argument(output, type=Path, required=True)


def emit(receipt, stream=sys.stdout):
    print(json.dumps(receipt, sort_keys=True), file=stream)


def run(fn):
    try:
        emit(fn())
        return 0
    except Refusal as exc:
        emit(exc.receipt)
        return 1
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError, subprocess.SubprocessError) as exc:
        emit({"reason": "harness_error", "retry_when": "after correcting the command, inputs or runtime",
              "next_command": "python3 -B editorial.py discover", "detail": str(exc)}, sys.stderr)
        return 2


def ffmpeg(*args, data=None):
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1",
            "-filter_threads", "1", "-filter_complex_threads", "1", *map(str, args)]
    result = subprocess.run(argv, input=data, capture_output=True, env=ENV, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"ffmpeg rc {result.returncode}")
    return result.stdout


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, env=ENV, check=True)
    return json.loads(result.stdout)


@contextmanager
def destination(path):
    path = path.resolve()
    require(not path.exists(), "output_exists", "choose an unused output directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".editorial-", dir=path.parent) as work:
        stage = Path(work) / "output"
        stage.mkdir()
        yield stage
        if path.exists():
            raise FileExistsError(path)
        stage.rename(path)


def valid_slug(value):
    return isinstance(value, str) and bool(SLUG.fullmatch(value))


def stamp(frame, fps):
    ms = (1000 * frame + fps // 2) // fps
    seconds, ms = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def path_in(root, relative):
    require(isinstance(relative, str) and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts and (root / relative).resolve().is_relative_to(root.resolve()),
            "invalid_media_reference", "use the relative path of a media file inside the staged editorial bundle")
    return root / relative
