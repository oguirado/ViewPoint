#!/usr/bin/env python3
"""Validate offline inputs and atomically prepare a Docker context; never build.

Run with python3 -I. Missing production inputs are CANNOT_SEE, never a build
receipt. All external commands are git read/apply operations on owned scratch.
"""
import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

STATUS = {0: "OK", 2: "ERROR", 3: "INCOMPLETE", 4: "STALE", 5: "FAIL", 6: "BUDGET_EXCEEDED"}
VERDICT = {0: "PASS", 2: "ERROR", 3: "CANNOT_SEE", 4: "CANNOT_SEE", 5: "FAIL", 6: "CANNOT_SEE"}
HERE = Path(__file__).resolve().parent
MAX_BYTES = 128 * 2**20  # DESIGN_CHOICE [TARGET], per source archive/command.
MAX_FILES = 20000       # DESIGN_CHOICE [TARGET], per source archive.
SHA = re.compile(r"[0-9a-f]{64}\Z")
REV = re.compile(r"[0-9a-f]{40}\Z")
IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[0-9a-f]{64}\Z")


class Refusal(Exception):
    def __init__(self, rc, reason):
        self.rc, self.reason = rc, reason
        super().__init__(reason)


def canonical(value):
    def numbers(item):
        if isinstance(item, float):
            raise Refusal(2, "non_integer_number")
        if isinstance(item, dict):
            for child in item.values():
                numbers(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                numbers(child)
    numbers(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal(2, "duplicate_key")
        result[key] = value
    return result


def _number(_):
    raise Refusal(2, "non_integer_number")


def read_json(path):
    if path.stat().st_size > 2**20:
        raise Refusal(6, "json_budget")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                      parse_float=_number, parse_constant=_number)


def relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\n\r\t\\"):
        raise Refusal(2, "path_escape")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..") for part in value.split("/")):
        raise Refusal(2, "path_escape")
    return path


def inside(root, value):
    path = root.joinpath(*relative(value).parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise Refusal(2, "path_escape")
    return path


def keys(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise Refusal(2, "input_shape")


def check_patches(lock, recipe=HERE):
    seen = set()
    if not isinstance(lock["patches"], list) or not lock["patches"]:
        raise Refusal(3, "patches_absent")
    for patch in lock["patches"]:
        keys(patch, ("source", "path", "sha256", "files"))
        if patch["source"] not in lock["sources"] or not SHA.fullmatch(str(patch["sha256"])):
            raise Refusal(2, "patch_identity")
        path = inside(recipe, patch["path"])
        if not path.is_file() or sha(path.read_bytes()) != patch["sha256"]:
            raise Refusal(4, "patch_stale")
        if not isinstance(patch["files"], list) or not patch["files"]:
            raise Refusal(2, "patch_files_absent")
        for item in patch["files"]:
            keys(item, ("path", "before_sha256", "after_sha256"))
            relative(item["path"])
            identity = (patch["source"], item["path"])
            if identity in seen or not all(SHA.fullmatch(str(item[k])) for k in ("before_sha256", "after_sha256")):
                raise Refusal(2, "patch_identity")
            seen.add(identity)


def check_inputs(lock, recipe=HERE):
    keys(lock, ("architecture", "bases", "sources", "patches", "wheels", "debs", "compiler_sha256"))
    if lock["architecture"] != "aarch64":
        raise Refusal(4, "architecture_stale")
    keys(lock["bases"], ("builder", "runtime"))
    keys(lock["debs"], ("builder", "runtime"))
    if not isinstance(lock["sources"], dict) or not lock["sources"]:
        raise Refusal(3, "sources_absent")
    if set(lock["sources"]) != {"trellis2", "cumesh", "flexgemm", "cubvh"}:
        raise Refusal(2, "source_inventory_invalid")
    for name, source in lock["sources"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise Refusal(2, "source_name")
        keys(source, ("directory", "revision", "archive_sha256", "licence_path", "licence_sha256", "gitlinks"))
        if not SHA.fullmatch(str(source["archive_sha256"])):
            raise Refusal(4, "source_archive_unpinned")
        relative(source["directory"])
        relative(source["licence_path"])
        if not REV.fullmatch(str(source["revision"])):
            raise Refusal(4, "source_unpinned")
        if not SHA.fullmatch(str(source["licence_sha256"])) or not isinstance(source["gitlinks"], dict):
            raise Refusal(2, "licence_or_gitlinks_invalid")
        for path, link in source["gitlinks"].items():
            relative(path)
            keys(link, ("revision", "replacement"))
            if not REV.fullmatch(str(link["revision"])):
                raise Refusal(4, "gitlink_unpinned")
            replacement = link["replacement"]
            if replacement != "system-eigen" and (
                replacement not in lock["sources"] or lock["sources"][replacement]["revision"] != link["revision"]
            ):
                raise Refusal(4, "gitlink_stale")
    check_patches(lock, recipe)
    missing = []
    if lock["compiler_sha256"] is None:
        missing.append("compiler_sha256")
    elif not SHA.fullmatch(str(lock["compiler_sha256"])):
        raise Refusal(4, "compiler_unpinned")
    for name, image in lock["bases"].items():
        if image is None:
            missing.append("base_" + name)
        elif not isinstance(image, str) or not IMAGE.fullmatch(image):
            raise Refusal(4, "base_unpinned")
    groups = [("wheels", lock["wheels"]), *[("debs_" + k, v) for k, v in lock["debs"].items()]]
    for name, group in groups:
        if group is None or group == []:
            missing.append(name)
            continue
        if not isinstance(group, list):
            raise Refusal(2, "dependency_shape")
        seen = set()
        for item in group:
            keys(item, ("path", "sha256", "requirement") if name == "wheels" else ("path", "sha256"))
            path = relative(item["path"])
            reject_restricted(str(path))
            if len(path.parts) != 1 or str(path) in seen or not SHA.fullmatch(str(item["sha256"])):
                raise Refusal(2, "dependency_identity")
            seen.add(str(path))
            if name == "wheels":
                if not str(path).endswith(".whl") or not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", item["requirement"]):
                    raise Refusal(2, "wheel_unpinned")
                reject_restricted(item["requirement"])
                distribution, version = item["requirement"].split("==")
                filename = str(path).split("-")
                if len(filename) not in (5, 6) or normalise(filename[0]) != normalise(distribution) or filename[1] != version:
                    raise Refusal(2, "wheel_requirement_mismatch")
            elif not str(path).endswith(".deb"):
                raise Refusal(2, "deb_identity")
    if missing:
        raise Refusal(3, "inputs_absent:" + ",".join(sorted(missing)))


def normalise(name):
    return re.sub(r"[-_.]+", "_", name).lower()


def reject_restricted(value):
    if any(name in normalise(value) for name in ("nvdiffrast", "nvdiffrec")):
        raise Refusal(5, "restricted_dependency")


def inspect_package(data, item, group):
    """Inspect package metadata and installed names without installing anything."""
    if group == "wheels":
        with zipfile.ZipFile(io.BytesIO(data)) as wheel:
            names = wheel.namelist()
            if len(names) > MAX_FILES or sum(i.file_size for i in wheel.infolist()) > MAX_BYTES:
                raise Refusal(6, "package_budget")
            metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise Refusal(2, "wheel_metadata_invalid")
            expected_name, expected_version = item["requirement"].split("==")
            from email.parser import BytesParser
            fields = BytesParser().parsebytes(wheel.read(metadata[0]))
            if normalise(fields.get("Name", "")) != normalise(expected_name) or fields.get("Version") != expected_version:
                raise Refusal(2, "wheel_requirement_mismatch")
            for name in names:
                relative(name.rstrip("/"))
                reject_restricted(name)
                if name.endswith(".dist-info/top_level.txt"):
                    reject_restricted(wheel.read(name).decode("utf-8"))
        return
    if not data.startswith(b"!<arch>\n"):
        raise Refusal(2, "deb_archive_invalid")
    offset, entries = 8, {}
    while offset < len(data):
        header = data[offset:offset + 60]
        if len(header) != 60 or header[58:] != b"`\n":
            raise Refusal(2, "deb_archive_invalid")
        size = int(header[48:58])
        name = header[:16].decode("ascii").strip().rstrip("/")
        if size < 0 or offset + 60 + size > len(data) or name in entries:
            raise Refusal(2, "deb_archive_invalid")
        entries[name] = data[offset + 60:offset + 60 + size]
        offset += 60 + size + size % 2
    if entries.get("debian-binary") != b"2.0\n":
        raise Refusal(2, "deb_archive_invalid")
    for prefix in ("control.tar", "data.tar"):
        selected = [name for name in entries if name == prefix or name.startswith(prefix + ".")]
        if len(selected) != 1:
            raise Refusal(2, "deb_archive_invalid")
        total = 0
        with tarfile.open(fileobj=io.BytesIO(entries[selected[0]]), mode="r:*") as tar:
            for count, member in enumerate(tar, 1):
                total += member.size
                if count > MAX_FILES or total > MAX_BYTES:
                    raise Refusal(6, "package_budget")
                reject_restricted(member.name)
                if member.issym() or member.islnk():
                    reject_restricted(member.linkname)
                if prefix == "control.tar" and member.name.removeprefix("./") == "control":
                    if not member.isfile():
                        raise Refusal(2, "deb_metadata_invalid")
                    from email.parser import BytesParser
                    fields = BytesParser().parsebytes(tar.extractfile(member).read())
                    reject_restricted(fields.get("Package", ""))


def context_paths(root):
    paths = []
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        relative(name)
        if path.is_symlink():
            if os.readlink(path) != "/usr/include/eigen3":
                raise Refusal(4, "context_symlink_stale")
        elif path.is_dir():
            continue
        elif not path.is_file():
            raise Refusal(2, "context_file_invalid")
        paths.append("./" + name)
    return sorted(paths)


def seal_context(root):
    links = [name for name in context_paths(root) if (root / name).is_symlink()]
    (root / "context.links").write_text("".join(name + "\n" for name in links), encoding="utf-8")
    inventory = sorted(context_paths(root) + ["./context.paths", "./context.sha256"])
    (root / "context.paths").write_text("".join(name + "\n" for name in inventory), encoding="utf-8")
    contents = "".join(sha((root / name).read_bytes()) + "  " + name + "\n" for name in context_paths(root)
                       if not (root / name).is_symlink())
    (root / "context.sha256").write_text(contents, encoding="utf-8")
    return sha(contents.encode())


def verify_context(root, expected):
    try:
        manifest = (root / "context.sha256").read_bytes()
        if sha(manifest) != expected:
            raise Refusal(4, "context_manifest_stale")
        for line in manifest.decode().splitlines():
            digest, name = line.split("  ", 1)
            if sha(inside(root, name.removeprefix("./")).read_bytes()) != digest:
                raise Refusal(4, "context_bytes_stale")
        if context_paths(root) != (root / "context.paths").read_text().splitlines():
            raise Refusal(4, "context_inventory_stale")
        actual_links = [name for name in context_paths(root) if (root / name).is_symlink()]
        if actual_links != (root / "context.links").read_text().splitlines():
            raise Refusal(4, "context_symlink_stale")
    except (OSError, ValueError):
        raise Refusal(4, "context_bytes_stale") from None


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(root):
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()), key=lambda p: len(p.parts), reverse=True):
        sync_directory(path)
    sync_directory(root)


def wait_group_empty(pgid):
    """Observe termination, rather than treating signal delivery as cleanup."""
    deadline = time.monotonic() + 2  # Separate cleanup budget [TARGET].
    while True:
        live = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, IndexError):
                raise Refusal(3, "cleanup_unproven") from None
            if len(fields) < 3:
                raise Refusal(3, "cleanup_unproven")
            if fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
                live = True
                break
        if not live:
            return
        if time.monotonic() >= deadline:
            raise Refusal(3, "cleanup_unproven")
        time.sleep(0.01)


def run_owned(argv, *, cwd=None, timeout_s=30, ceiling=None):
    """Own the process group; bounded output is spooled inside owned scratch."""
    if type(timeout_s) is not int or not 0 < timeout_s <= 300:
        raise Refusal(2, "timeout_invalid")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_ATTR_NOSYSTEM="1",
               PYTHONDONTWRITEBYTECODE="1")
    for key in tuple(env):
        if key.startswith("GIT_") and key not in {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_NO_REPLACE_OBJECTS", "GIT_ATTR_NOSYSTEM"}:
            del env[key]
    if ceiling is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    with tempfile.TemporaryFile(dir=cwd) as output, tempfile.TemporaryFile(dir=cwd) as control:
        supervisor = [sys.executable, "-I", "-B", str(HERE / "process_guard.py"), str(timeout_s), str(control.fileno()), *argv]
        process = subprocess.Popen(supervisor, cwd=cwd, env=env, stdout=output,
                                   stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(control.fileno(),))
        # WNOWAIT retains ownership of the leader PID until all group signals
        # and cleanup observations have completed; never signal a recycled PID.
        expired = False
        deadline = time.monotonic() + timeout_s + 4  # Supervisor cleanup and control budget [TARGET].
        try:
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                if time.monotonic() >= deadline:
                    expired = True
                    break
                time.sleep(0.01)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                wait_group_empty(process.pid)
            finally:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    raise Refusal(3, "cleanup_unproven") from None
        if expired:
            raise Refusal(6, "deadline_exceeded")
        control.seek(0)
        try:
            supervised = json.loads(control.read(4096))
        except (ValueError, TypeError):
            raise Refusal(3, "supervision_unproven") from None
        if supervised["rc"] != 0:
            raise Refusal(supervised["rc"], supervised["reason"])
        if process.returncode != 0:
            raise Refusal(2, "supervisor_failed")
        returncode = supervised["returncode"]
        if type(returncode) is not int:
            raise Refusal(2, "supervisor_invalid")
        if returncode < 0:
            raise Refusal(2, "command_signalled")
        if returncode != 0:
            raise Refusal(4, "command_or_patch_stale")
        if output.tell() > MAX_BYTES:
            raise Refusal(6, "command_output_budget")
        output.seek(0)
        return output.read()


def apply_patches(lock, source_roots, recipe=HERE):
    check_patches(lock, recipe)
    for patch in lock["patches"]:
        root = source_roots[patch["source"]]
        for item in patch["files"]:
            path = inside(root, item["path"])
            if not path.is_file() or sha(path.read_bytes()) != item["before_sha256"]:
                raise Refusal(4, "source_bytes_stale")
        argv = ["git", "apply", "--whitespace=nowarn"]
        diff = str(inside(recipe, patch["path"]).resolve())
        run_owned(argv + ["--check", diff], cwd=root, ceiling=root.parent)
        run_owned(argv + [diff], cwd=root, ceiling=root.parent)
        for item in patch["files"]:
            path = inside(root, item["path"])
            data = path.read_bytes()
            if sha(data) != item["after_sha256"]:
                raise Refusal(4, "patched_bytes_stale")
            if path.suffix == ".py":
                try:
                    ast.parse(data, filename=item["path"])
                except (SyntaxError, ValueError):
                    raise Refusal(5, "patched_python_invalid") from None


def export_sources(lock, sources_root, stage, recipe=HERE):
    roots = {}
    for name, source in lock["sources"].items():
        source_dir = inside(sources_root, source["directory"])
        if not source_dir.is_dir():
            raise Refusal(3, "source_absent")
        # Git must not discover an ancestor repository for a missing checkout.
        try:
            top = run_owned(["git", "-C", str(source_dir), "rev-parse", "--show-toplevel"], cwd=stage).decode().strip()
        except Refusal as exc:
            if exc.reason == "command_or_patch_stale":
                raise Refusal(2, "source_not_repository") from None
            raise
        if Path(top).resolve() != source_dir.resolve():
            raise Refusal(2, "source_not_repository")
        attributes = run_owned(["git", "-C", str(source_dir), "rev-parse", "--git-path", "info/attributes"], cwd=stage).decode().strip()
        attributes = Path(attributes)
        if not attributes.is_absolute():
            attributes = source_dir / attributes
        if attributes.exists() or attributes.is_symlink():
            raise Refusal(4, "source_attributes_present")
        # Always export the committed revision, never a dirty checkout overlay.
        actual = run_owned(["git", "-C", str(source_dir), "rev-parse", "HEAD"], cwd=stage).decode().strip()
        if actual != source["revision"]:
            raise Refusal(4, "source_revision_stale")
        licence = run_owned(["git", "-C", str(source_dir), "show", actual + ":" + source["licence_path"]], cwd=stage)
        if sha(licence) != source["licence_sha256"]:
            raise Refusal(4, "licence_stale")
        tree = run_owned(["git", "-C", str(source_dir), "ls-tree", "-rz", actual], cwd=stage)
        links = {}
        for entry in tree.split(b"\x00"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                mode, kind, identity = metadata.decode().split()
                if mode == "160000":
                    links[path.decode()] = identity
        if links != {k: v["revision"] for k, v in source["gitlinks"].items()}:
            raise Refusal(4, "gitlink_inventory_stale")
        archive = run_owned(["git", "-C", str(source_dir), "archive", "--format=tar", actual], cwd=stage)
        if sha(archive) != source["archive_sha256"]:
            raise Refusal(4, "source_archive_stale")
        target = stage / "sources" / name
        target.mkdir(parents=True)
        total = 0
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for count, member in enumerate(tar, 1):
                total += member.size
                if count > MAX_FILES or total > MAX_BYTES:
                    raise Refusal(6, "source_budget")
                path = inside(target, member.name.rstrip("/"))
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as incoming, path.open("wb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    path.chmod(0o755 if member.mode & 0o111 else 0o644)
                else:
                    raise Refusal(2, "source_link_unsupported")
        roots[name] = target
    apply_patches(lock, roots, recipe)
    completed, visiting = set(), set()
    def populate(name):
        if name in completed:
            return
        if name in visiting:
            raise Refusal(2, "gitlink_cycle")
        visiting.add(name)
        source = lock["sources"][name]
        for path, link in source["gitlinks"].items():
            target = inside(roots[name], path)
            if target.exists():
                target.rmdir()  # A gitlink in git archive is an empty directory.
            if link["replacement"] == "system-eigen":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to("/usr/include/eigen3", target_is_directory=True)
            else:
                populate(link["replacement"])
                shutil.copytree(roots[link["replacement"]], target, symlinks=True)
        visiting.remove(name)
        completed.add(name)
    for name in sorted(roots):
        populate(name)


def prepare(lock, *, sources_root, packages_root, output, recipe=HERE):
    check_inputs(lock, recipe)
    sources_root = sources_root.resolve()
    packages_root = packages_root.resolve()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise Refusal(2, "output_exists")
    if not output.parent.is_dir():
        raise Refusal(2, "output_parent_absent")
    with tempfile.TemporaryDirectory(prefix=".trellis-build-", dir=output.parent) as scratch:
        stage = Path(scratch) / "context"
        stage.mkdir()
        for name, group in [("wheels", lock["wheels"]), *[("debs/" + k, v) for k, v in lock["debs"].items()]]:
            dest = stage / name
            dest.mkdir(parents=True)
            for item in group:
                original = inside(packages_root, name + "/" + item["path"])
                if not original.is_file() or original.stat().st_size > MAX_BYTES:
                    raise Refusal(3, "dependency_absent_or_oversize")
                data = original.read_bytes()
                if sha(data) != item["sha256"]:
                    raise Refusal(4, "dependency_stale")
                inspect_package(data, item, name)
                (dest / item["path"]).write_bytes(data)
        export_sources(lock, sources_root, stage, recipe)
        (stage / "requirements.lock").write_text("".join(
            item["requirement"] + " --hash=sha256:" + item["sha256"] + "\n" for item in lock["wheels"]
        ), encoding="utf-8")
        for name in ("Dockerfile", "build.py", "runtime_probe.py", "process_guard.py", "THIRD_PARTY_NOTICES.txt"):
            shutil.copyfile(recipe / name, stage / name)
        (stage / "build-inputs.json").write_text(canonical(lock), encoding="utf-8")
        context_sha256 = seal_context(stage)
        sync_tree(stage)
        # Rename only a completely validated context into an unoccupied name.
        # The destination must be reserved by this caller; concurrent publishers
        # must use distinct output paths.
        if output.exists() or output.is_symlink():
            raise Refusal(2, "output_exists")
        os.replace(stage, output)
        sync_directory(output.parent)
    return {"rc": 0, "status": "OK", "verdict": "PASS", "prepared": True, "build_executed": False,
            "reason_codes": [], "inputs_sha256": sha(canonical(lock).encode()), "context_sha256": context_sha256,
            "build_argv": ["docker", "build", "--network=none", "--pull=false", "--platform=linux/arm64",
                           "--build-arg", "CONTEXT_SHA256=" + context_sha256,
                           "--build-arg", "BUILDER_IMAGE=" + lock["bases"]["builder"],
                           "--build-arg", "RUNTIME_IMAGE=" + lock["bases"]["runtime"],
                           "--file", str(output / "Dockerfile"), str(output)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=HERE / "build-inputs.json")
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--prepare", type=Path)
    try:
        args = parser.parse_args(argv)
        lock = read_json(args.lock)
        if args.prepare is not None:
            if args.sources is None or args.packages is None:
                raise Refusal(2, "prepare_inputs_required")
            result = prepare(lock, sources_root=args.sources, packages_root=args.packages, output=args.prepare)
        else:
            check_inputs(lock)
            result = {"rc": 0, "status": "OK", "verdict": "PASS", "prepared": False, "build_executed": False, "reason_codes": []}
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        result = {"rc": 2, "status": "ERROR", "verdict": "ERROR", "reason_codes": ["usage_invalid"],
                  "build_executed": False}
    except Refusal as exc:
        result = {"rc": exc.rc, "status": STATUS[exc.rc], "verdict": VERDICT[exc.rc],
                  "reason_codes": [exc.reason], "build_executed": False}
    except (OSError, ValueError, TypeError, KeyError, tarfile.TarError, zipfile.BadZipFile):
        result = {"rc": 2, "status": "ERROR", "verdict": "ERROR", "reason_codes": ["input_or_io_invalid"],
                  "build_executed": False}
    print(canonical(result), end="")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
