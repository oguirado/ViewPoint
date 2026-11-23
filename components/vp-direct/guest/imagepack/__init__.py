"""Read-only catalogue for the operator-pinned ARM64 graphical image pack."""

from copy import deepcopy
import hashlib
import importlib.util
import os
from pathlib import Path
import platform
import stat


_SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_SPEC = importlib.util.spec_from_file_location("vp_imagepack_validation", _SCHEMAS / "validation.py")
_VALIDATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VALIDATION)

_PACK = "vp.image_pack/v1"
_GUEST_CALL = "vp_guest_call"
_ARM64 = {"aarch64", "arm64"}
_ARTIFACTS = ("kernel", "initrd", "root")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MIB = 1 << 20


class ImagePackError(ValueError):
    """The requested image pack could not be verified."""


def _validate_contract(contract, value):
    try:
        _VALIDATION.validate_contract(contract, value)
    except _VALIDATION.ValidationError as error:
        location = ".".join(map(str, error.absolute_path))
        raise ImagePackError(f"{contract} refused at {location or 'record'}") from None


def _check_profile(pack):
    _validate_contract(_PACK, pack)
    # The native launcher uses QEMU/KVM and the virtio block and serial devices.
    # The frozen pack example supplies the capture, input and snapshot vocabulary.
    config = pack["resolved"]
    if (config["capture"], config["input"], config["snapshot"]) != (
        "virtio-serial", "guest-agent", "unsupported"
    ) or set(config["devices"]) != {"virtio-blk", "virtio-serial"}:
        raise ImagePackError("mode or devices outside the notepad-vm profile")
    # The native configuration takes whole MiB and enforces these resource ranges.
    if not 1 <= config["cpu"] <= 5 or not (
        512 * _MIB <= config["ram_bytes"] <= 7168 * _MIB
        and config["ram_bytes"] % _MIB == 0
    ):
        raise ImagePackError("resources outside the notepad-vm profile")


def _open_artifact(path):
    """Resolve each component read-only, with no symlinks or FIFO waits."""
    directory = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, _FILE_FLAGS, dir_fd=directory)
    finally:
        os.close(directory)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _verify_artifact(name, pin):
    path = Path(pin["path"])
    if (path.anchor != "/" or not path.name or ".." in path.parts
            or str(path) != pin["path"] or "\0" in pin["path"]):
        raise ImagePackError(f"{name}: canonical absolute path required")

    try:
        with os.fdopen(_open_artifact(path), "rb") as artifact:
            before = os.fstat(artifact.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
                raise ImagePackError(f"{name}: nonempty regular file required")
            header = artifact.read(min(before.st_size, 64)) if name == "kernel" else b""
            digest = hashlib.sha256(header)
            remaining = before.st_size - len(header)
            while remaining:
                chunk = artifact.read(min(remaining, _MIB))
                if not chunk:
                    raise ImagePackError(f"{name}: artifact changed during validation")
                digest.update(chunk)
                remaining -= len(chunk)
            if artifact.read(1) or _identity(before) != _identity(os.fstat(artifact.fileno())):
                raise ImagePackError(f"{name}: artifact changed during validation")
        current = _open_artifact(path)
        try:
            if _identity(before) != _identity(os.fstat(current)):
                raise ImagePackError(f"{name}: artifact changed during validation")
        finally:
            os.close(current)
    except FileNotFoundError:
        raise ImagePackError(f"{name}: AUSENTE — artifact is missing") from None
    except OSError:
        raise ImagePackError(f"{name}: artifact is inaccessible or unsafe") from None

    if digest.hexdigest() != pin["sha256"]:
        raise ImagePackError(f"{name}: sha256 mismatch")
    # Match the raw ARM64 Image check used by the ARM64 capsule setup.
    if name == "kernel" and (len(header) < 64 or header[56:60] != b"ARM\x64"):
        raise ImagePackError("kernel: raw ARM64 Linux Image required")


class ImagePackCatalogue:
    """Accept one pack supplied by trusted configuration at construction time.

    The pack in an operation is only a candidate. Successful validation describes
    the local artifacts at read time. Guest creation requires downstream admission.
    """

    def __init__(self, trusted_pack=None):
        self._trusted_pack = deepcopy(trusted_pack)
        if self._trusted_pack is not None:
            _check_profile(self._trusted_pack)

    def _verify_trusted_pack(self):
        if platform.machine() not in _ARM64:
            raise ImagePackError("ARM64 host required")
        config = self._trusted_pack["resolved"]
        for name in _ARTIFACTS:
            if name == "initrd" and name not in config:
                continue
            _verify_artifact(name, config[name])

    def pack_list(self):
        packs = []
        if self._trusted_pack is not None:
            self._verify_trusted_pack()
            packs.append(deepcopy(self._trusted_pack))
        result = {"packs": packs}
        _validate_contract(f"{_GUEST_CALL}.pack_list.result/v1", result)
        return result

    def pack_validate(self, pack):
        candidate = deepcopy(pack)
        _check_profile(candidate)
        if self._trusted_pack is None:
            raise ImagePackError("AUSENTE — operator-pinned notepad-vm pack")
        if _VALIDATION.canonical_bytes(candidate) != _VALIDATION.canonical_bytes(self._trusted_pack):
            raise ImagePackError("candidate differs from the operator-pinned notepad-vm pack")
        self._verify_trusted_pack()
        _validate_contract(f"{_GUEST_CALL}.pack_validate.result/v1", candidate)
        return candidate

    def handle(self, args):
        """Local handler for the G1 operations in the frozen guest adapter."""
        _validate_contract(f"{_GUEST_CALL}.args/v1", args)
        op = args["op"]
        if op == "pack_list":
            return self.pack_list()
        if op == "pack_validate":
            return self.pack_validate(args["pack"])
        if op == "pack_import":
            raise ImagePackError("pack_import is unsupported")
        raise ImagePackError(f"{op} is outside image-pack authority")
