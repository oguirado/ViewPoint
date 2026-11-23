#!/usr/bin/env python3
"""Build a candidate engine wheel and matching config in an SB0 handoff.

Run this script without arguments. A successful run prints one JSON object
with a repository-relative handoff_root and config/wheel paths relative to
that root. The caller verifies the pair against the source declarations
with g_pin in a staged project root before any separate activation.
Each successful invocation owns its handoff directory.
"""
import ast
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zlib
from email.parser import Parser
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
API_VERSION = "1.1.0"
COMMIT_ID = re.compile(r"[0-9a-f]{40}")
WHEEL_TAG = "py3-none-any"
STALE_README_LINE = b'License: none declared yet (all rights reserved until the owner adds a LICENSE file).\n'
PACKAGES = {"visioncore", "vmem", "vadapt"}
WHEEL_ARCHIVE_ERRORS = (
    BadZipFile, EOFError, OSError, OverflowError, RuntimeError, ValueError, struct.error, zlib.error,
)


class PackagingError(ValueError):
    """A bounded packaging diagnostic."""


def _resolve_repo(repo_spec, root):
    if not isinstance(repo_spec, str) or not repo_spec:
        raise PackagingError("invalid_source_repository")
    if repo_spec == "@monorepo":
        for parent in (root, *root.parents):
            if (parent / ".git").exists() and (parent / "components/vision-engine").is_dir():
                return parent
        repo_spec = "env:BVQA_VISION_ENGINE_SRC"
    if repo_spec.startswith("env:"):
        repo_spec = os.environ.get(repo_spec[4:], "")
        if not repo_spec:
            raise PackagingError("source_repository_missing")
    return Path(repo_spec).expanduser()


def _safe_name(name):
    return "\\" not in name and "\x00" not in name and all(part not in ("", ".", "..") for part in name.split("/"))


def source_commits(vendor):
    tooling_commit = vendor.get("commit")
    if not isinstance(tooling_commit, str) or not COMMIT_ID.fullmatch(tooling_commit):
        raise PackagingError("invalid_commit")
    engine_commit = vendor.get("engine_commit", tooling_commit)
    if not isinstance(engine_commit, str) or not COMMIT_ID.fullmatch(engine_commit):
        raise PackagingError("invalid_engine_commit")
    return tooling_commit, engine_commit


class Source:
    def __init__(self, root, vendor, commit):
        if not isinstance(commit, str) or not COMMIT_ID.fullmatch(commit):
            raise PackagingError("invalid_commit")
        self.tooling_commit, self.commit = source_commits(vendor)
        if commit != self.commit:
            raise PackagingError("engine_commit_mismatch")
        self.repo = _resolve_repo(vendor.get("src_repo"), root)
        if self.git("cat-file", "-t", commit) != b"commit\n":
            raise PackagingError("source_not_commit")
        if self.tooling_commit != commit and self.git("cat-file", "-t", self.tooling_commit) != b"commit\n":
            raise PackagingError("tooling_source_not_commit")
        subdir = vendor.get("src_subdir", "")
        if not isinstance(subdir, str):
            raise PackagingError("invalid_source_subdir")
        subdir = subdir.strip("/")
        if subdir and not _safe_name(subdir):
            raise PackagingError("invalid_source_subdir")
        self.ref = f"{commit}:{subdir}" if subdir else commit
        if subdir and self.git("cat-file", "-t", self.ref) != b"tree\n":
            raise PackagingError("source_not_tree")
        self.files = {}
        for row in self.git("ls-tree", "-rz", self.ref).decode().split("\x00")[:-1]:
            meta, name = row.split("\t", 1)
            mode, kind, oid = meta.split()
            if kind != "blob" or mode not in ("100644", "100755") or not _safe_name(name) or name in self.files:
                raise PackagingError("invalid_source_inventory")
            self.files[name] = (mode, oid)

    def git(self, *args):
        env = dict(os.environ, GIT_NO_LAZY_FETCH="1")
        return subprocess.check_output(
            ["git", "--no-replace-objects", "-C", str(self.repo), *args],
            stderr=subprocess.DEVNULL, env=env, timeout=30,
        )

    def read(self, name):
        if name not in self.files:
            raise PackagingError("source_file_missing")
        return self.git("cat-file", "blob", self.files[name][1])

    def extract(self, tree):
        dirs = {parent.as_posix() for name in self.files for parent in Path(name).parents} - {"."}
        archive = self.git("-c", "tar.umask=0022", "archive", "--format=tar", self.ref)
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for item in tar:
                name = item.name
                if name in seen or not ((item.isdir() and name in dirs) or (item.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and name in self.files)):
                    raise PackagingError("unsafe_archive_member")
                seen.add(name)
                if item.isdir():
                    continue
                with tar.extractfile(item) as member:
                    data = member.read()
                mode, oid = self.files[name]
                blob = f"blob {len(data)}\0".encode("ascii") + data
                if hashlib.sha1(blob, usedforsecurity=False).hexdigest() != oid:
                    raise PackagingError("archive_source_mismatch")
                dest = tree / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                dest.chmod(0o755 if mode == "100755" else 0o644)
        if seen - dirs != self.files.keys():
            raise PackagingError("archive_source_inventory")


def source_api(data):
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError):
        raise PackagingError("api_version_mismatch") from None
    stores = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "API_VERSION" and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores.append(node)
        elif isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) in ("API_VERSION", "*"):
            raise PackagingError("api_version_mismatch")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name == "API_VERSION":
            raise PackagingError("api_version_mismatch")
        elif isinstance(node, ast.MatchMapping) and node.rest == "API_VERSION":
            raise PackagingError("api_version_mismatch")
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and node.simple:
            target = node.target
        else:
            continue
        if stores == [target] and isinstance(node.value, ast.Constant) and node.value.value == API_VERSION:
            return API_VERSION
    raise PackagingError("api_version_mismatch")


def source_project(data):
    manifest = tomllib.loads(data.decode("utf-8"))
    project = manifest.get("project")
    if not isinstance(project, dict) or project.get("name") != "vision-engine":
        raise PackagingError("invalid_project_name")
    version = project.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
        raise PackagingError("invalid_project_version")
    return manifest


def wheel_name(version):
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
        raise PackagingError("invalid_project_version")
    return f"vision_engine-{version}-{WHEEL_TAG}.whl"


def _parse_wheel_headers(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fields = Parser().parsestr(text)
    if fields.defects or fields.is_multipart() or fields.get_content_maintype() == "multipart":
        return None
    return fields


def _wheel_metadata_valid(data):
    fields = _parse_wheel_headers(data)
    if fields is None:
        return False
    generators = fields.get_all("Generator", [])
    payload = fields.get_payload()
    return (
        isinstance(payload, str) and not payload.strip()
        and fields.get_all("Wheel-Version") == ["1.0"]
        and fields.get_all("Root-Is-Purelib") == ["true"]
        and fields.get_all("Tag") == [WHEEL_TAG]
        and fields.get_all("Build") is None
        and (not generators or (len(generators) == 1 and isinstance(generators[0], str) and bool(generators[0].strip())))
    )


def _record_valid(archive, record, names):
    try:
        text = archive.read(record).decode("utf-8")
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        seen = set()
        for name, checksum, size in rows:
            if name not in names or name in seen:
                return False
            seen.add(name)
            if name == record:
                if checksum or size:
                    return False
                continue
            algorithm, separator, expected = checksum.partition("=")
            if not separator or algorithm not in ("sha256", "sha384", "sha512"):
                return False
            data = archive.read(name)
            raw = hashlib.new(algorithm, data).digest()
            actual = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            if expected != actual or size != str(len(data)):
                return False
        return seen == names
    except (csv.Error, ValueError):
        return False


def wheel_failures(data, source, project, pinned_version):
    # The package payload consists of the tracked files under the engine package roots.
    expected = {name for name in source.files if name.partition("/")[0] in PACKAGES}
    try:
        failures, payload = _wheel_archive_failures(data, expected, project, pinned_version)
    except WHEEL_ARCHIVE_ERRORS:
        return ["wheel_archive"]
    for name, content in payload.items():
        if content != source.read(name):
            failures.append(name + ":wheel_source_mismatch")
    return failures


def _wheel_archive_failures(data, expected, project, pinned_version):
    prefix = f"vision_engine-{project['version']}.dist-info/"
    required = {prefix + name for name in ("METADATA", "WHEEL", "RECORD")}
    with ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        names = {entry.filename for entry in members if not entry.is_dir()}
        dirs = {parent.as_posix() for name in names for parent in Path(name).parents} - {"."}
        all_names = {entry.filename.rstrip("/") for entry in members}
        if len(all_names) != len(members) or names & dirs or all_names - names - dirs or any(
            not _safe_name(entry.orig_filename.removesuffix("/"))
            or entry.orig_filename != entry.filename
            or entry.flag_bits & 1
            or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
            or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
            for entry in members
        ):
            return ["wheel_archive_inventory"], {}
        if archive.testzip() is not None:
            return ["wheel_archive"], {}
        out = []
        if not required <= names or {name for name in names if name.endswith(".dist-info/METADATA")} != {prefix + "METADATA"}:
            out.append("wheel_metadata_inventory")
        else:
            fields = _parse_wheel_headers(archive.read(prefix + "METADATA"))
            if fields is None:
                out.append("wheel_metadata_mismatch")
            elif (fields.get_all("Name") != [project["name"]]
                    or fields.get_all("Version") != [project["version"]] or pinned_version != project["version"]):
                out.append("wheel_metadata_mismatch")
        if prefix + "WHEEL" in names and not _wheel_metadata_valid(archive.read(prefix + "WHEEL")):
            out.append("wheel_wheel_metadata")
        if prefix + "RECORD" in names and not _record_valid(archive, prefix + "RECORD", names):
            out.append("wheel_record")
        payload = {name for name in names if not name.startswith(prefix)}
        if not expected:
            out.append("wheel_package_missing")
        if payload != expected:
            out.append("wheel_package_inventory")
        return out, {name: archive.read(name) for name in sorted(payload & expected)}


def _prepare_metadata(tree, manifest):
    changes = []
    readme = tree / "README.md"
    lines = readme.read_bytes().splitlines(keepends=True)
    stale = [line for line in lines if line.rstrip(b"\r\n") == STALE_README_LINE.rstrip(b"\n")]
    if len(stale) > 1:
        raise PackagingError("ambiguous_license_notice")
    if stale:
        lines.remove(stale[0])
        readme.write_bytes(b"".join(lines))
        changes.append("remove exact obsolete license notice from README.md in scratch")
    tool = manifest.get("tool", {})
    setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
    packages = setuptools.get("packages") if isinstance(setuptools, dict) else None
    legacy = ["visioncore", "vmem", "vadapt"]
    current = [*legacy, "vadapt.blender"]
    if packages == legacy:
        metadata = tree / "pyproject.toml"
        original = metadata.read_bytes().decode("utf-8")
        before = 'packages = ["visioncore", "vmem", "vadapt"]'
        after = 'packages = ["visioncore", "vmem", "vadapt", "vadapt.blender"]'
        if original.count(before) != 1:
            raise PackagingError("unrecognized_package_manifest")
        patched = original.replace(before, after, 1)
        setuptools["packages"] = current
        if tomllib.loads(patched) != manifest:
            raise PackagingError("unrecognized_package_manifest")
        metadata.write_bytes(patched.encode("utf-8"))
        changes.append("add vadapt.blender to the scratch setuptools package list")
    elif packages != current:
        find = packages.get("find") if isinstance(packages, dict) else None
        if not isinstance(find, dict) or find.get("include") != ["visioncore*", "vmem*", "vadapt*"]:
            raise PackagingError("unrecognized_package_manifest")
    return "; ".join(changes) or "none"


def _build():
    vendor = json.loads((ROOT / "tools/VENDORED.json").read_text())
    if not isinstance(vendor, dict):
        raise PackagingError("invalid_vendor_metadata")
    _, commit = source_commits(vendor)
    source = Source(ROOT, vendor, commit)
    api_version = source_api(source.read("visioncore/__init__.py"))
    manifest = source_project(source.read("pyproject.toml"))
    for name in ("README.md", "vadapt/ramws.py", "vadapt/blender/__init__.py", "vadapt/blender/from_render_dir.py"):
        source.read(name)
    epoch = source.git("show", "-s", "--format=%ct", commit).decode("ascii").strip()
    if not epoch.isascii() or not epoch.isdecimal():
        raise PackagingError("invalid_source_epoch")
    base = ROOT / "build/room_SB0"
    if base.resolve() != base:
        raise PackagingError("invalid_scratch_root")
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="wheel-build-", dir=base))
    keep = False
    try:
        result = _build_in_work(work, source, manifest, api_version, epoch)
        keep = result["code"] == "OK"
        return result
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def _build_in_work(work, source, manifest, api_version, epoch):
    project = manifest["project"]
    version = project["version"]
    scratch = work / "scratch"
    tree, wheels = scratch / "source", scratch / "wheels"
    tree.mkdir(parents=True)
    wheels.mkdir()
    source.extract(tree)
    metadata_patch = _prepare_metadata(tree, manifest)
    env = dict(os.environ, SOURCE_DATE_EPOCH=epoch, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE="1")
    # Set the build umask in the child process so overlapping invocations stay independent.
    pip = 'import os, sys; os.umask(0o022); os.execv(sys.executable, [sys.executable, "-B", "-m", "pip", *sys.argv[1:]])'
    result = subprocess.run(
        [sys.executable, "-B", "-c", pip, "wheel", "--no-deps", "--no-build-isolation", "--no-cache-dir",
         str(tree), "--wheel-dir", str(wheels)],
        cwd=scratch, capture_output=True, env=env, timeout=120,
    )
    if result.returncode:
        return {"code": "E_IO", "rc": 2, "reason": "wheel_build"}
    files = sorted(wheels.glob("*.whl"))
    if [file.name for file in files] != [wheel_name(version)]:
        raise PackagingError("wheel_inventory")
    wheel = files[0]
    data = wheel.read_bytes()
    failures = wheel_failures(data, source, project, version)
    if failures:
        raise PackagingError(failures[0].rpartition(":")[2])
    wheel_digest = hashlib.sha256(data).hexdigest()
    engine_path = "tools/wheels/" + wheel.name
    config_path = "config/vision.json"
    config = {
        "mode": "shadow",
        "engine_path": engine_path,
        "tooling_commit": source.tooling_commit,
        "wheel_build": {"metadata_patch": metadata_patch,
                        "source": f"components/vision-engine@{source.commit[:8]} (tools/build_engine_wheel.py)"},
        "pin": {"version": version, "api_version": api_version, "tag": source.commit[:8],
                "commit": source.commit, "wheel_sha256": wheel_digest},
    }
    pending = work / "pending"
    (pending / engine_path).parent.mkdir(parents=True)
    (pending / config_path).parent.mkdir(parents=True)
    (pending / engine_path).write_bytes(data)
    (pending / config_path).write_text(_json(config) + "\n", encoding="utf-8")
    handoff = work / "handoff"
    shutil.rmtree(scratch)
    pending.rename(handoff)
    return {"code": "OK", "rc": 0, "handoff_root": handoff.relative_to(ROOT).as_posix(),
            "config": config_path, "wheel": engine_path, "wheel_sha256": wheel_digest}


def _json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv):
        result = {"code": "E_USAGE", "rc": 2, "reason": "no_arguments_supported"}
    else:
        try:
            result = _build()
        except PackagingError as exc:
            result = {"code": "E_IO", "rc": 2, "reason": str(exc)}
        except subprocess.TimeoutExpired:
            result = {"code": "E_BUDGET", "rc": 6, "reason": "timeout"}
        except subprocess.SubprocessError:
            result = {"code": "E_IO", "rc": 2, "reason": "source_git_error"}
        except (tarfile.TarError, BadZipFile, zlib.error, EOFError):
            result = {"code": "E_IO", "rc": 2, "reason": "invalid_archive"}
        except OSError:
            result = {"code": "E_IO", "rc": 2, "reason": "io_error"}
        except (ValueError, TypeError, RuntimeError):
            result = {"code": "E_IO", "rc": 2, "reason": "invalid_input"}
    print(_json(result))
    return result["rc"]


if __name__ == "__main__":
    sys.exit(main())
