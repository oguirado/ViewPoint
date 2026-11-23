"""Physical-line accounting for the F0 documentation budget (stdlib only)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import time
import zlib


MAX_BYTES = 64 * 2**20
MAX_FILES = 10000
MAX_SECONDS = 30


def _png_asset(path, data):
    """Admit intact PNG reference bytes as assets, never as line credit."""
    if path.suffix.lower() != ".png" or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset, kinds = 8, []
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + size
        if end > len(data):
            return False
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:end - 4]
        checksum = int.from_bytes(data[end - 4:end], "big")
        if zlib.crc32(kind + body) != checksum:
            return False
        if not kinds and (kind != b"IHDR" or size != 13 or
                          not int.from_bytes(body[:4], "big") or
                          not int.from_bytes(body[4:8], "big")):
            return False
        if kinds and kind == b"IHDR":
            return False
        kinds.append(kind)
        offset = end
        if kind == b"IEND":
            return size == 0 and offset == len(data) and b"IDAT" in kinds
    return False


def _glb_asset(path, data):
    """Check GLB container framing before including its digest in the inventory."""
    if path.suffix.lower() != ".glb" or len(data) < 20:
        return False
    magic, version, length = struct.unpack_from("<4sII", data)
    if (magic, version, length) != (b"glTF", 2, len(data)):
        return False
    offset = 12
    while offset < length:
        if length - offset < 8:
            return False
        size, kind = struct.unpack_from("<I4s", data, offset)
        if size % 4 or size > length - offset - 8:
            return False
        if offset == 12:
            if kind != b"JSON" or not size:
                return False
        elif kind == b"JSON":
            return False
        offset += 8 + size
    return True


def measure_docs_ratio(root: Path) -> dict:
    """Inventory disk files, including untracked files; never follow aliases.

    Internal symlinks are inspected for confinement but do not add line credit.
    Directory aliases are not traversed. No directory names are excluded.
    """
    result = {"schema": "af.docs-ratio/v1", "rc": 2, "markdown_lines": 0,
              "code_schema_lines": 0, "files": []}
    started = time.monotonic()
    consumed = 0
    seen = 0
    try:
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            return result
        pending = [root]
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir()):
                seen += 1
                if seen > MAX_FILES or time.monotonic() - started > MAX_SECONDS:
                    result["rc"] = 6
                    return result
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    return result
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    continue
                if stat.S_ISDIR(mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(mode):
                    return result
                with path.open("rb") as stream:
                    data = stream.read(MAX_BYTES - consumed + 1)
                consumed += len(data)
                if consumed > MAX_BYTES:
                    result["rc"] = 6
                    return result
                suffix = path.suffix.lower()
                if suffix == ".glb":
                    if not _glb_asset(path, data):
                        return result
                elif b"\x00" in data:
                    if not _png_asset(path, data):
                        return result
                kind = ("markdown" if suffix in (".md", ".markdown") else
                        "code_schema" if suffix in (".py", ".sh") or
                        path.name.endswith(".schema.json") else "other")
                lines = 0
                if kind != "other":
                    data.decode("utf-8", errors="strict")
                    # LF-delimited physical lines; final unterminated line counts.
                    lines = data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
                    result[kind + "_lines"] += lines
                result["files"].append({"path": path.relative_to(root).as_posix(),
                                        "lines": lines, "class": kind,
                                        "sha256": hashlib.sha256(data).hexdigest()})
        result["files"].sort(key=lambda item: item["path"])
        code = result["code_schema_lines"]
        result["rc"] = (3 if code == 0 else
                        0 if 2 * result["markdown_lines"] <= code else 5)
    except (OSError, ValueError, RuntimeError):
        result["rc"] = 2
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    result = measure_docs_ratio(args.root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False))
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
