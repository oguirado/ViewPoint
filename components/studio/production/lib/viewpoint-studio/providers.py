# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve studio-configured provider bindings and review receipts."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

from registry import canonical, sha256

ENV = {"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "TZ": "UTC"}
MAX_JSON = 1024 * 1024
MAX_TIMEOUT = 3600


class Refusal(Exception):
    def __init__(self, reason, retry_when, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_when = retry_when
        self.detail = detail


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def token(value):
    return (isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None)


def fields(value, keys):
    return isinstance(value, dict) and value.keys() == set(keys.split())


@dataclass(frozen=True)
class FilePin:
    path: Path
    sha256: str

    @classmethod
    def parse(cls, base, value):
        if (not fields(value, "path sha256") or not digest(value["sha256"])
                or not isinstance(value["path"], str) or not value["path"].strip()
                or "\x00" in value["path"]):
            raise ValueError("invalid file pin")
        return cls(base / value["path"], value["sha256"])

    def verify(self, role, executable=False):
        retry = f"restore the pinned {role} or supply an approved profile for its replacement"
        try:
            path = self.path.resolve(strict=True)
            if not stat.S_ISREG(path.stat().st_mode):
                raise FileNotFoundError
            if executable and not os.access(path, os.X_OK):
                raise PermissionError
            actual = sha256(path)
        except FileNotFoundError as exc:
            raise Refusal(role + "_missing", retry) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise Refusal(role + "_unusable", retry) from exc
        if actual != self.sha256:
            raise Refusal(role + "_mismatch", retry)
        return path


@dataclass(frozen=True)
class Provider:
    provider_id: str
    revision: str
    slots: tuple
    executable: FilePin
    runtime: FilePin | None

    def identity(self):
        return {
            "provider_id": self.provider_id,
            "provider_revision": self.revision,
            "executable_sha256": self.executable.sha256,
            "runtime_sha256": self.runtime.sha256 if self.runtime else None,
        }

    def invoke(self, mode, decode, *, request=None, root=None):
        executable = self.executable.verify("executable", executable=True)
        command = [str(executable)]
        runtime = self.runtime.verify("runtime", executable=True) if self.runtime else executable
        try:
            with runtime.open("rb") as binary:
                if binary.read(4) != b"\x7fELF":
                    raise Refusal("runtime_unpinned", "the command is native or binds a native interpreter explicitly")
        except OSError as exc:
            raise Refusal("runtime_unusable", "the pinned runtime can be read and executed") from exc
        if self.runtime:
            command.insert(0, str(runtime))
        timeout = 5
        command.append(mode)
        if request is not None:
            command.extend(("--artifact-root", str(root)))
            timeout = min(request["context"]["timeout_s"], MAX_TIMEOUT)
        try:
            result = subprocess.run(command, input=canonical(request) if request is not None else b"",
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=root or executable.parent,
                                    env=ENV, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise Refusal("provider_timeout", "the pinned provider responds within the request budget") from exc
        except OSError as exc:
            raise Refusal("runtime_unusable", "the pinned command and its runtime dependencies can execute") from exc
        if result.returncode != 0:
            raise Refusal("provider_refused", "the configured provider accepts the command",
                          {"provider_rc": result.returncode})
        try:
            return decode(result.stdout)
        except (ValueError, RecursionError) as exc:
            raise Refusal("provider_protocol_invalid", "the provider returns one valid protocol response") from exc

    def probe(self, decode):
        found = self.invoke("--probe", decode)
        expected = {"schema": "vp.studio.provider-probe/v1", "provider_id": self.provider_id,
                    "provider_revision": self.revision, "slots": list(self.slots)}
        if found != expected:
            raise Refusal("provider_probe_mismatch", "the command reports the pinned identity and slot set")


@dataclass
class Profile:
    profile_id: str
    providers: dict
    qualifications: dict

    @classmethod
    def load(cls, path, expected, slots, decode):
        if path is None and expected is None:
            return cls("unconfigured", {}, {})
        if path is None or not digest(expected):
            raise Refusal("profile_pin_required", "the profile path and its approved SHA-256 are both supplied")
        try:
            with path.open("rb") as source:
                raw = source.read(MAX_JSON + 1)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise Refusal("profile_mismatch", "the selected profile matches its approved SHA-256")
            value = decode(raw)
            if (not fields(value, "schema profile_id providers qualifications")
                    or value["schema"] != "vp.studio.provider-profile/v1"
                    or not token(value["profile_id"]) or value["profile_id"] == "unconfigured"
                    or not isinstance(value["providers"], list) or not isinstance(value["qualifications"], dict)):
                raise ValueError("invalid profile")
            bindings = {}
            used_ids = set()
            base = path.resolve().parent
            for item in value["providers"]:
                if (not fields(item, "provider_id provider_revision slots executable runtime")
                        or not token(item["provider_id"]) or item["provider_id"] in used_ids
                        or not token(item["provider_revision"])
                        or item["provider_revision"].lower() in {"main", "master", "latest", "head"}
                        or not isinstance(item["slots"], list) or not item["slots"]
                        or any(not token(name) or name not in slots or name in bindings for name in item["slots"])
                        or len(set(item["slots"])) != len(item["slots"])):
                    raise ValueError("invalid or ambiguous provider binding")
                provider = Provider(item["provider_id"], item["provider_revision"], tuple(sorted(item["slots"])),
                                    FilePin.parse(base, item["executable"]),
                                    FilePin.parse(base, item["runtime"]) if item["runtime"] is not None else None)
                used_ids.add(provider.provider_id)
                for name in provider.slots:
                    bindings[name] = provider
            qualifications = {name: FilePin.parse(base, pin) for name, pin in value["qualifications"].items()}
            if qualifications.keys() - bindings.keys():
                raise ValueError("qualification without provider")
            return cls(value["profile_id"], bindings, qualifications)
        except (OSError, RuntimeError, ValueError, RecursionError) as exc:
            raise Refusal("profile_invalid", "the approved profile is readable and has unambiguous bindings") from exc

    def qualification(self, name, registry_sha256, decode):
        pin = self.qualifications.get(name)
        if pin is None:
            return None
        try:
            with pin.verify("qualification").open("rb") as source:
                raw = source.read(MAX_JSON + 1)
            if hashlib.sha256(raw).hexdigest() != pin.sha256:
                raise Refusal("qualification_mismatch", "the review receipt matches the profile pin")
            receipt = decode(raw)
        except (OSError, ValueError, RecursionError) as exc:
            raise Refusal("qualification_invalid", "the approved review receipt is readable protocol JSON") from exc
        provider = self.providers[name]
        expected = {"schema": "vp.studio.qualification/v1", "verdict": "ACCEPT",
                    "slot": name, "profile_id": self.profile_id, "registry_sha256": registry_sha256,
                    **provider.identity()}
        if (not fields(receipt, " ".join(expected) + " reviewer_id request_sha256 result_sha256")
                or any(receipt[key] != value for key, value in expected.items())
                or not token(receipt["reviewer_id"]) or receipt["reviewer_id"] == provider.provider_id
                or not digest(receipt["request_sha256"]) or not digest(receipt["result_sha256"])):
            raise Refusal("qualification_binding_mismatch", "an independent ACCEPT receipt matches this exact binding")
        return {"sha256": pin.sha256, "reviewer_id": receipt["reviewer_id"], "scope": self.profile_id}
