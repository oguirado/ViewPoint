"""Declared Cycles CUDA cache; all other child state stays in scratch.

The namespace seals Blender version and complete kernel source content. Cycles
adds architecture and compiler flags to each cubin filename within that namespace.
Evidence is granted only for cubins explicitly named by the renderer log.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def private_directory(path):
    """Reject symlinks and writable-by-others cache entries, including key dirs."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        if not path.parent.exists():
            private_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError("unsafe_kernel_cache_directory:" + str(path))
    return path


def source_identity(root):
    """Hash relative names and every byte; reject ambiguous filesystem entries."""
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("kernel_source_not_directory")
    digest, count = hashlib.sha256(), 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("kernel_source_symlink:" + str(path))
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("kernel_source_not_regular_file:" + str(path))
        name = path.relative_to(root).as_posix().encode()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        count += 1
    if not count:
        raise ValueError("kernel_source_empty")
    return digest.hexdigest()


def namespace_key(version, source_sha256):
    if not isinstance(version, str) or not version:
        raise ValueError("kernel_cache_blender_version_absent")
    return hashlib.sha256(json.dumps({"blender_version": version,
        "source_sha256": source_sha256}, sort_keys=True).encode()).hexdigest()


def file_evidence(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022 or not info.st_size):
        raise ValueError("unsafe_kernel_cache_cubin:" + str(path))
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}


def log_lines(text):
    return [line for line in text.splitlines() if any(term in line for term in
        ("locally compiled kernel", ".cubin", "Compiling CUDA", "Kernel compilation finished"))][-64:]


class KernelCache:
    def __init__(self, directory, scene):
        raw = Path(directory).expanduser().absolute()
        # Refuse aliases as well as literal paths into the source scene folder.
        if raw.resolve().is_relative_to(Path(scene).resolve().parent):
            raise ValueError("kernel_cache_inside_source_directory")
        private_directory(raw)
        self.root = raw.resolve(strict=True)
        self.directory = None
        self.uses = []

    def bind(self, version, source, scratch):
        self.source = Path(source).resolve(strict=True)
        self.source_sha256 = source_identity(self.source)
        self.version = version
        self.namespace = namespace_key(version, self.source_sha256)
        self.directory = private_directory(self.root / self.namespace)
        self.kernels = private_directory(self.directory / "kernels")
        # Only kernels persist. HOME, config, data and other XDG caches do not.
        cycles = Path(scratch) / "xdg" / "cycles"
        cycles.mkdir(mode=0o700, parents=True, exist_ok=True)
        link = cycles / "kernels"
        if link.exists() or link.is_symlink():
            raise ValueError("kernel_cache_scratch_link_exists")
        link.symlink_to(self.kernels, target_is_directory=True)
        self.link = link
        self.check()

    def check(self):
        private_directory(self.root)
        private_directory(self.directory)
        private_directory(self.kernels)
        if self.link.resolve(strict=True) != self.kernels:
            raise ValueError("kernel_cache_link_changed")
        if source_identity(self.source) != self.source_sha256:
            raise ValueError("kernel_source_changed_during_session")
        # Validate existing entries before allowing Blender to read any of them.
        for path in self.kernels.iterdir():
            if not re.fullmatch(r"cycles_.*_(?:sm|compute)_\d+_[0-9a-fA-F]+\.(?:cubin|ptx)", path.name):
                raise ValueError("unexpected_kernel_cache_entry:" + str(path))
            file_evidence(path)

    def snapshot(self):
        self.check()
        return {p.name: file_evidence(p) for p in self.kernels.glob("*.cubin")}

    def finish(self, mode, before, lines, success):
        self.check()
        selected = []
        for line in lines:
            for name in re.findall(r"cycles_[^\s/\\\"']+\.cubin", line):
                if name not in selected:
                    selected.append(name)
        cubins = []
        for name in selected:
            path = self.kernels / name
            if not path.is_file():
                continue
            after = file_evidence(path)
            arch = re.search(r"_(sm_\d+)_", name)
            if arch is None:
                continue
            unchanged = before.get(name) == after
            hit = unchanged and any("Using locally compiled kernel." in line for line in lines)
            compiled = any("Compiling CUDA" in line for line in lines)
            outcome = "hit" if hit else "miss" if compiled and not unchanged else "unverified"
            cubins.append({"path": str(path), "arch": arch[1], **after,
                           "before": before.get(name), "outcome": outcome})
        outcome = ("hit" if cubins and all(c["outcome"] == "hit" for c in cubins) else
                   "miss" if cubins and any(c["outcome"] == "miss" for c in cubins) else "unverified")
        event = {"mode": mode, "render_completed": bool(success), "outcome": outcome,
                 "cubins": cubins, "log_lines": lines}
        self.uses.append(event)
        return event

    def receipt(self):
        return {"path": str(self.root), "namespace": self.namespace,
                "blender_version": self.version, "source_path": str(self.source),
                "source_sha256": self.source_sha256,
                "architecture_key": "Cycles cubin filename sm_<compute-capability>",
                "uses": self.uses}
