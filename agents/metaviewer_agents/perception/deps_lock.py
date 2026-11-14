"""Verify private dependency provenance without importing private packages.

[TARGET, unit decision] Public handoff: resolve_locked_component(name, *,
lock_path=None) returns the canonical checkout/site-packages root. The default
lock is the repository deps.lock. Wheel roots may also be venv prefixes or the
distribution's own .dist-info directory; only PEP 610 Git provenance is admitted.
Git checkouts reject any untracked or ignored executable/Python file, and wheel
roots verify installed RECORD file digests and reject unrecorded files or
startup hooks (sitecustomize.py, usercustomize.py, .pth). Ambiguous metadata
and malformed provenance fail closed as unavailable.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from ..observe import ObservationError

__all__ = ["DependencyLockError", "resolve_locked_component"]

_ROOT_VARIABLES = {
    "vision-engine": "VQA_VE_ROOT",
    "wender-agent": "MV_WENDER_ROOT",
}
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_LOCK_PATH = Path(__file__).resolve().parents[3] / "deps.lock"
_FORBIDDEN_CHECKOUT_NAMES = frozenset((
    "sitecustomize.py",
    "usercustomize.py",
    "__init__.py",
    "pyvenv.cfg",
))
_STARTUP_HOOK_PREFIXES = ("sitecustomize.", "usercustomize.")
_PAYLOAD_SUFFIXES = frozenset((
    ".py",
    ".pyw",
    ".pyi",
    ".pyc",
    ".pyo",
    ".pth",
    ".so",
    ".pyd",
    ".dll",
    ".dylib",
    ".whl",
    ".egg",
    ".zip",
    ".sh",
    ".bash",
))


class DependencyLockError(ObservationError, RuntimeError):
    """A dependency could not be proven to match its lock."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code, message)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"),
                      object_pairs_hook=_unique_object)


def _locked_entries(lock_path: Path) -> dict[str, dict]:
    try:
        lock = _read_json(lock_path)
        if not isinstance(lock, dict) or not isinstance(lock.get("components"), list):
            raise ValueError("components must be a list")
        entries: dict[str, dict] = {}
        for entry in lock["components"]:
            if not isinstance(entry, dict):
                raise ValueError("component must be an object")
            component, commit = entry.get("name"), entry.get("commit")
            if (not isinstance(component, str) or not component
                    or component in entries or not isinstance(commit, str)
                    or _SHA.fullmatch(commit) is None):
                raise ValueError("component names must be unique with full Git SHAs")
            vendored_path = entry.get("vendored_path")
            if vendored_path is not None:
                if (not isinstance(vendored_path, str) or not vendored_path
                        or Path(vendored_path).is_absolute()
                        or ".." in Path(vendored_path).parts):
                    raise ValueError("invalid vendored_path")
            vendored_sha = entry.get("vendored_tree_sha256")
            if vendored_sha is not None:
                if (not isinstance(vendored_sha, str)
                        or _SHA256_HEX.fullmatch(vendored_sha) is None):
                    raise ValueError("invalid vendored_tree_sha256")
            entries[component] = entry
        if not _ROOT_VARIABLES.keys() <= entries.keys():
            raise ValueError("missing or unsupported private component")
        return entries
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise DependencyLockError("bad_lockfile", "invalid dependency lock") from error


def _locked_commit(name: str, lock_path: Path) -> str:
    entries = _locked_entries(lock_path)
    if name not in _ROOT_VARIABLES or name not in entries:
        raise DependencyLockError("bad_lockfile", "invalid dependency lock")
    return entries[name]["commit"]


def _git(root: Path, *arguments: str) -> str:
    # Never inherit GIT_DIR, GIT_WORK_TREE, Python settings, credentials, or
    # user/global Git configuration. PATH remains the caller's tool locator.
    environment = {"PATH": os.environ.get("PATH", os.defpath),
                   "HOME": os.devnull, "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1"}
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments],
            env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="strict", timeout=2.0,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise DependencyLockError("unavailable", "cannot inspect dependency checkout") from error
    if result.returncode:
        raise DependencyLockError("unavailable", "cannot inspect dependency checkout")
    return result.stdout.strip()


def _is_startup_hook_name(filename: str) -> bool:
    lowered = filename.lower()
    if lowered in ("sitecustomize.py", "usercustomize.py"):
        return True
    if lowered.startswith(_STARTUP_HOOK_PREFIXES):
        return True
    return any(s.lower() == ".pth" for s in Path(filename).suffixes)


def _is_untracked_payload(path: Path, info: os.stat_result, root: Path) -> bool:
    if not stat.S_ISREG(info.st_mode):
        return True
    if bool(info.st_mode & 0o111):
        return True
    lowered = path.name.lower()
    if lowered in _FORBIDDEN_CHECKOUT_NAMES or lowered.startswith(_STARTUP_HOOK_PREFIXES):
        return True
    if any(s.lower() in _PAYLOAD_SUFFIXES for s in path.suffixes):
        return True
    rel_parts = path.relative_to(root).parts[:-1]
    return any(part.lower().endswith((".dist-info", ".egg-info")) for part in rel_parts)


def _wheel_info(root: Path, name: str) -> tuple[str, Path]:
    distribution = re.sub(r"[-_.]+", "_", name)
    pattern = re.compile(re.escape(distribution) + r"-[^-]+\.dist-info\Z", re.I)
    try:
        if root.name.endswith(".dist-info"):
            candidates = [root] if pattern.fullmatch(root.name) else []
        else:
            locations = [root, root / "Lib" / "site-packages"]
            locations.extend(root.glob("lib/python*/site-packages"))
            candidates = [entry for location in locations if location.is_dir()
                          for entry in location.iterdir()
                          if entry.is_dir() and pattern.fullmatch(entry.name)]
        if len(candidates) != 1:
            raise ValueError("missing or ambiguous distribution metadata")
        dist_info = candidates[0]
        provenance = _read_json(dist_info / "direct_url.json")
        vcs = provenance.get("vcs_info") if isinstance(provenance, dict) else None
        if (not isinstance(vcs, dict) or vcs.get("vcs") != "git"
                or not isinstance(vcs.get("commit_id"), str)
                or _SHA.fullmatch(vcs["commit_id"]) is None):
            raise ValueError("missing full Git provenance")
        return vcs["commit_id"], dist_info
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise DependencyLockError("unavailable", "cannot verify wheel provenance") from error


def _wheel_commit(root: Path, name: str) -> str:
    commit, _ = _wheel_info(root, name)
    return commit


def _verify_record_file(record_path: Path, dist_info: Path, site_root: Path) -> set[Path]:
    verified: set[Path] = {
        record_path.resolve(),
        (dist_info / "direct_url.json").resolve(),
    }
    raw_lines = record_path.read_text(encoding="utf-8").splitlines()
    rows = [row for row in csv.reader(raw_lines) if row]
    if not rows:
        raise ValueError("empty RECORD")
    for row in rows:
        if len(row) != 3:
            raise ValueError("malformed RECORD row")
        rel_str, hash_spec, size_str = row
        rel_path = Path(rel_str)
        if not rel_str or rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError("invalid RECORD path")
        if _is_startup_hook_name(rel_path.name):
            raise ValueError("startup hook forbidden in wheel RECORD")
        target = site_root / rel_path
        if site_root == dist_info and not target.exists():
            if len(rel_path.parts) >= 2 and rel_path.parts[0] == dist_info.name:
                target = dist_info.joinpath(*rel_path.parts[1:])
        resolved_target = target.resolve()
        if resolved_target == record_path.resolve() and not hash_spec and not size_str:
            verified.add(resolved_target)
            continue
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("recorded wheel entry is not a regular file")
        if not hash_spec.startswith("sha256="):
            raise ValueError("missing sha256 digest in RECORD")
        expected_digest = hash_spec[len("sha256="):]
        if not expected_digest:
            raise ValueError("empty sha256 digest in RECORD")
        data = target.read_bytes()
        if size_str:
            if not size_str.isdigit() or int(size_str) != len(data):
                raise ValueError("recorded wheel entry size mismatch")
        raw_digest = hashlib.sha256(data).digest()
        hex_digest = raw_digest.hex()
        b64_digest = base64.urlsafe_b64encode(raw_digest).rstrip(b"=").decode("ascii")
        if expected_digest not in (hex_digest, b64_digest):
            raise ValueError("recorded wheel entry digest mismatch")
        verified.add(resolved_target)
    return verified


def _verify_wheel_tree(root: Path, dist_info: Path, name: str) -> None:
    """Verify installed wheel files against RECORD and reject untracked payloads."""
    site_root = dist_info if root == dist_info else dist_info.parent
    record_path = dist_info / "RECORD"
    try:
        verified_files: set[Path] = set()
        has_record = record_path.exists()
        if has_record:
            info = record_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("RECORD is not a regular file")
            verified_files.update(_verify_record_file(record_path, dist_info, site_root))
            if site_root != dist_info and site_root.is_dir():
                for sibling in site_root.iterdir():
                    if (sibling != dist_info and sibling.is_dir()
                            and sibling.name.lower().endswith(".dist-info")):
                        sibling_record = sibling / "RECORD"
                        if sibling_record.is_file():
                            verified_files.update(
                                _verify_record_file(sibling_record, sibling, site_root)
                            )
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            for dirname in list(dirnames):
                subdir = current / dirname
                if subdir.is_symlink():
                    raise ValueError("symlink directory in wheel root")
                if dirname == "__pycache__":
                    dirnames.remove(dirname)
                    for pyc_name in os.listdir(subdir):
                        pyc_path = subdir / pyc_name
                        pyc_info = pyc_path.lstat()
                        if not stat.S_ISREG(pyc_info.st_mode):
                            raise ValueError("non-regular file in __pycache__")
                        stem = pyc_name.partition(".")[0]
                        parent_py = (current / f"{stem}.py").resolve()
                        if (not pyc_name.endswith((".pyc", ".pyo"))
                                or stem.lower() in ("sitecustomize", "usercustomize")
                                or (has_record and parent_py not in verified_files)
                                or (not has_record and not parent_py.is_file())):
                            raise ValueError("unverified bytecode in wheel __pycache__")
            for filename in filenames:
                file_path = current / filename
                file_info = file_path.lstat()
                if not stat.S_ISREG(file_info.st_mode):
                    raise ValueError("non-regular file in wheel root")
                if _is_startup_hook_name(filename):
                    raise ValueError("startup hook in wheel root")
                if has_record:
                    if file_path.resolve() not in verified_files:
                        raise ValueError("unrecorded file in wheel root")
                else:
                    if any(s.lower() in (".so", ".pyd", ".dll", ".dylib", ".whl", ".egg", ".zip", ".sh", ".bash", ".pyc", ".pyo")
                           for s in file_path.suffixes):
                        raise ValueError("unverified binary or archive in wheel root")
    except (OSError, ValueError, csv.Error) as error:
        raise DependencyLockError("dirty_tree", f"{name} differs from pinned wheel tree") from error


def _verify_tree(root: Path, commit: str, name: str) -> None:
    """Hash tracked bytes directly and reject untracked executable/Python files."""
    tracked: set[str] = set()
    for entry in _git(root, "ls-tree", "--full-tree", "-r", "-z", commit).split("\0"):
        if not entry:
            continue
        try:
            metadata, relative = entry.split("\t", 1)
            mode, kind, expected = metadata.split()
            path = root / relative
            info = path.lstat()
            if mode == "120000" and stat.S_ISLNK(info.st_mode):
                data = os.fsencode(os.readlink(path))
            elif mode in ("100644", "100755") and stat.S_ISREG(info.st_mode):
                if bool(info.st_mode & 0o111) != (mode == "100755"):
                    raise ValueError("tracked mode changed")
                data = path.read_bytes()
            else:
                raise ValueError("unsupported or changed tracked type")
            digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if kind != "blob" or digest != expected:
                raise ValueError("tracked bytes changed")
            tracked.add(Path(relative).as_posix())
        except (OSError, ValueError) as error:
            raise DependencyLockError("dirty_tree", f"{name} differs from pinned tree") from error
    if not tracked:
        raise DependencyLockError("dirty_tree", f"{name} has empty pinned tree")
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if ".git" in dirnames:
                dirnames.remove(".git")
            current = Path(dirpath)
            for dirname in list(dirnames):
                subdir = current / dirname
                if subdir.is_symlink():
                    rel_dir = subdir.relative_to(root).as_posix()
                    if rel_dir not in tracked:
                        raise ValueError("untracked symlink directory")
                    dirnames.remove(dirname)
                elif dirname == "__pycache__":
                    dirnames.remove(dirname)
                    for pyc_name in os.listdir(subdir):
                        pyc_path = subdir / pyc_name
                        pyc_info = pyc_path.lstat()
                        if not stat.S_ISREG(pyc_info.st_mode):
                            raise ValueError("non-regular entry in __pycache__")
                        stem = pyc_name.partition(".")[0]
                        parent_py = (current / f"{stem}.py").relative_to(root).as_posix()
                        if (not pyc_name.endswith((".pyc", ".pyo"))
                                or stem.lower() in ("sitecustomize", "usercustomize")
                                or parent_py not in tracked):
                            raise ValueError("untracked bytecode in __pycache__")
            for filename in filenames:
                file_path = current / filename
                rel_file = file_path.relative_to(root).as_posix()
                if rel_file in tracked:
                    if _is_startup_hook_name(filename):
                        raise ValueError("startup hook in checkout")
                    continue
                file_info = file_path.lstat()
                if _is_untracked_payload(file_path, file_info, root):
                    raise ValueError(f"untracked executable or Python file: {rel_file}")
    except (OSError, ValueError) as error:
        raise DependencyLockError("dirty_tree", f"{name} differs from pinned tree") from error


def _verify_monorepo_subtree(root: Path, expected: str, name: str) -> bool:
    """Admit only the declared in-repository subtree with pinned tree and bytes.

    This leaves the standalone checkout and wheel contracts intact. Suppressed
    index entries, tracked changes and untracked executable/Python files still
    fail the same provenance gate.
    """
    repo = _LOCK_PATH.parent.resolve()
    if root != repo / 'components' / name:
        return False
    if Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() != repo:
        raise DependencyLockError('unavailable', 'component is not in the owning repository')
    prefix = 'components/' + name
    if _git(repo, 'rev-parse', 'HEAD:' + prefix) != _git(repo, 'rev-parse', expected + '^{tree}'):
        raise DependencyLockError('commit_mismatch', name + ' subtree differs from pinned tree')
    flags = _git(repo, 'ls-files', '-v', '-z', '--', prefix)
    if not flags or any(entry and (entry[0].islower() or entry[0] == 'S')
                        for entry in flags.split('\0')):
        raise DependencyLockError('dirty_tree', name + ' has suppressed or missing index entries')
    _verify_tree(root, expected, name)
    if _git(repo, 'status', '--porcelain', '--untracked-files=no', '--', prefix):
        raise DependencyLockError('dirty_tree', name + ' has tracked modifications')
    return True


def _vendored_tree_sha256(root: Path) -> str:
    """Compute deterministic sha256 over sorted relative paths + bytes, excluding __pycache__ and *.pyc."""
    hasher = hashlib.sha256()
    entries: list[tuple[str, Path, os.stat_result]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        current = Path(dirpath)
        for dirname in list(dirnames):
            subdir = current / dirname
            info = subdir.lstat()
            if stat.S_ISLNK(info.st_mode):
                dirnames.remove(dirname)
                entries.append((subdir.relative_to(root).as_posix(), subdir, info))
        for filename in sorted(filenames):
            if filename.endswith(".pyc"):
                continue
            file_path = current / filename
            info = file_path.lstat()
            entries.append((file_path.relative_to(root).as_posix(), file_path, info))
    if not entries:
        raise ValueError("empty vendored tree")
    entries.sort(key=lambda item: item[0])
    for rel, path, info in entries:
        rel_bytes = rel.encode("utf-8")
        if stat.S_ISLNK(info.st_mode):
            target_bytes = os.fsencode(os.readlink(path))
            hasher.update(
                b"L\0" + rel_bytes + b"\0"
                + str(len(target_bytes)).encode("ascii") + b"\0"
                + target_bytes + b"\0"
            )
        elif stat.S_ISREG(info.st_mode):
            hasher.update(b"F\0" + rel_bytes + b"\0" + str(info.st_size).encode("ascii") + b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    hasher.update(chunk)
            hasher.update(b"\0")
        else:
            raise ValueError(f"unsupported file type in vendored tree: {rel}")
    return hasher.hexdigest()


def _verify_vendored_tree(root: Path, expected_sha256: str, name: str) -> None:
    """Verify a vendored component directory against its pinned content digest."""
    try:
        actual = _vendored_tree_sha256(root)
    except (OSError, ValueError, UnicodeError) as error:
        raise DependencyLockError("dirty_tree", f"{name} differs from pinned vendored tree") from error
    if actual != expected_sha256:
        raise DependencyLockError(
            "dirty_tree",
            f"{name} vendored tree sha256 {actual}, expected {expected_sha256}",
        )


def _has_wheel_metadata(root: Path, name: str) -> bool:
    distribution = re.sub(r"[-_.]+", "_", name)
    pattern = re.compile(re.escape(distribution) + r"-[^-]+\.dist-info\Z", re.I)
    if root.name.endswith(".dist-info"):
        return True
    locations = [root, root / "Lib" / "site-packages"]
    locations.extend(root.glob("lib/python*/site-packages"))
    for location in locations:
        if location.is_dir():
            for entry in location.iterdir():
                if entry.is_dir() and pattern.fullmatch(entry.name):
                    return True
    return False


def resolve_locked_component(name: str, *, lock_path: str | Path | None = None) -> Path:
    """Resolve a component and prove its pinned revision or vendored content hash.

    Raises DependencyLockError with unavailable, commit_mismatch, dirty_tree,
    or bad_lockfile. Any untracked executable/Python checkout file, unrecorded
    wheel file, or vendored tree hash mismatch fails closed.
    """
    resolved_lock = _LOCK_PATH if lock_path is None else Path(lock_path)
    entries = _locked_entries(resolved_lock)
    if name not in _ROOT_VARIABLES or name not in entries:
        raise DependencyLockError("bad_lockfile", "invalid dependency lock")
    entry = entries[name]
    expected = entry["commit"]
    from .compiled import compiled_executable
    executable = compiled_executable(name, lock_path=lock_path)
    if executable is not None:
        return executable.parent
    raw = os.environ.get(_ROOT_VARIABLES[name])
    vendored_rel = entry.get("vendored_path")
    vendored_sha = entry.get("vendored_tree_sha256")
    vendored_root: Path | None = None
    if vendored_rel is not None:
        try:
            vendored_root = (resolved_lock.parent / vendored_rel).resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            vendored_root = (resolved_lock.parent / vendored_rel).resolve()
    if raw is None:
        if vendored_rel is None:
            raise DependencyLockError("unavailable", f"{_ROOT_VARIABLES[name]} is required")
        candidate = resolved_lock.parent / vendored_rel
    elif not raw.strip():
        raise DependencyLockError("unavailable", f"{_ROOT_VARIABLES[name]} is required")
    else:
        candidate = Path(raw)
    try:
        root = candidate.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("not a directory")
    except (OSError, ValueError, RuntimeError) as error:
        raise DependencyLockError("unavailable", "dependency root is not a directory") from error
    if (root / ".git").exists():
        actual = _git(root, "rev-parse", "--verify", "HEAD")
        if actual != expected:
            raise DependencyLockError("commit_mismatch", f"{name} at {actual}, expected {expected}")
        flags = _git(root, "ls-files", "-v", "-z")
        if any(entry_flag and (entry_flag[0].islower() or entry_flag[0] == "S")
               for entry_flag in flags.split("\0")):
            raise DependencyLockError("dirty_tree", f"{name} has suppressed index entries")
        _verify_tree(root, expected, name)
        if _git(root, "status", "--porcelain", "--untracked-files=no"):
            raise DependencyLockError("dirty_tree", f"{name} has tracked modifications")
    elif vendored_root is not None and root == vendored_root:
        if vendored_sha is None:
            raise DependencyLockError("bad_lockfile", f"{name} missing vendored_tree_sha256")
        _verify_vendored_tree(root, vendored_sha, name)
    elif vendored_sha is not None and not _has_wheel_metadata(root, name):
        _verify_vendored_tree(root, vendored_sha, name)
    elif _verify_monorepo_subtree(root, expected, name):
        pass
    else:
        actual, dist_info = _wheel_info(root, name)
        if actual != expected:
            raise DependencyLockError("commit_mismatch", f"{name} at {actual}, expected {expected}")
        _verify_wheel_tree(root, dist_info, name)
    return root
