# SPDX-License-Identifier: AGPL-3.0-only
"""Start and qualify an isolated local-open Tools deployment."""

import argparse
from contextlib import contextmanager
from hashlib import sha256
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from tempfile import NamedTemporaryFile, TemporaryDirectory

from ..runtime import evidence_projection as projection
from ..runtime.process_lifecycle import proc_starttime, terminate_only
from ..runtime.retention_deployment import import_tools, local_open_profile, tools_source
from .controls import QualificationRejected, qualify_host


_COMPONENT = Path(__file__).resolve().parents[2]
_FROZEN_SUMS = "3af141229845b2e88dc36e698e29c62d36d4494b55c7b19653fef2ecd18c962d"


def scope_properties():
    """Measure the memory properties of the qualification process's scope."""
    memberships = Path("/proc/self/cgroup").read_text().splitlines()
    unified = [line.removeprefix("0::") for line in memberships if line.startswith("0::")]
    uid = os.geteuid()
    user_scope = rf"/user\.slice/user-{uid}\.slice/user@{uid}\.service/(?:[^/]+/)*[^/]+\.scope"
    if len(unified) != 1 or re.fullmatch(user_scope, unified[0]) is None:
        raise QualificationRejected("scope: a dedicated systemd user scope is required")
    directory = Path("/sys/fs/cgroup") / unified[0].lstrip("/")
    expected = {"memory.high": 6 << 30, "memory.max": 8 << 30, "memory.swap.max": 0}
    actual = {key: (directory / key).read_text().strip() for key in expected}
    if actual != {key: str(value) for key, value in expected.items()}:
        raise QualificationRejected("scope: the prescribed memory properties are required")
    actual = {key: int(value) for key, value in actual.items()}
    return actual


@contextmanager
def isolated_deployment():
    """Isolate XDG state and sockets before opening any local retention session."""
    with TemporaryDirectory(prefix="rea-qualify-") as temporary:
        root = Path(temporary).resolve()
        paths = {"XDG_STATE_HOME": root / "state", "XDG_RUNTIME_DIR": root / "run"}
        original = {name: os.environ.get(name) for name in paths}
        for name, path in paths.items():
            path.mkdir(mode=0o700)
            os.environ[name] = str(path)
        try:
            yield root
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def verify_readiness(local):
    """Require the expected authenticated read roles and all live write verbs."""
    clients = local.clients
    remote = import_tools("retention_service.transport.client").RemoteError
    try:
        denial_ready = local.check_denial_endpoint()
    except (OSError, ValueError, remote) as exc:
        raise QualificationRejected("denial_endpoint: authenticated readiness failed") from exc
    if denial_ready is not True:
        raise QualificationRejected("denial_endpoint: authenticated readiness failed")
    inspector = clients.inspector
    try:
        with inspector.client.session(clients.tenant) as session:
            reply = session.ready(
                "inspect", principal=inspector.principal,
                policy_revision=clients.policy_revision, acl_revision=clients.acl_revision,
            )
    except (OSError, ValueError, remote) as exc:
        raise QualificationRejected("inspector: authenticated readiness failed") from exc
    challenge = reply.get("challenge")
    if (reply.get("record_pin"), reply.get("index_pin")) != (projection.V03_SHA, projection.INDEX_SHA):
        raise QualificationRejected("inspector: frozen record and index pins differ")
    if reply != {"challenge": reply.get("challenge"), "verb": "inspect", "tenant": clients.tenant,
                 "principal": inspector.principal, "policy_revision": clients.policy_revision,
                 "acl_revision": clients.acl_revision, "record_pin": projection.V03_SHA,
                 "index_pin": projection.INDEX_SHA} or type(challenge) is not str or not re.fullmatch(r"[0-9a-f]{64}", challenge):
        raise QualificationRejected("inspector: authenticated readiness bindings differ")
    for operation in ("record", "replay", "compare"):
        if not clients.ready(record_pin=projection.V03_SHA, operation_id=operation):
            raise QualificationRejected(
                f"{operation}: PROFILE_NOT_ADMITTED (owner-pending: T03, live REA witness unavailable or readiness failed)"
            )


def measure_revisions(pins, frozen):
    """Measure the REA checkout and the independently pinned Tools schema bytes."""
    git = subprocess.run(
        ("git", "-C", str(_COMPONENT), "rev-parse", "--verify", "HEAD"),
        env={"PATH": os.defpath, "LC_ALL": "C"}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=True, timeout=10,
    ).stdout.decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", git) is None:
        raise QualificationRejected("revision: REA git HEAD is unavailable")
    tools = tools_source()
    frozen = frozen.resolve(strict=True)
    if not frozen.is_relative_to(tools):
        raise QualificationRejected("revision: frozen schema directory is outside REA_RETENTION_SRC")
    sums = (frozen / "FROZEN_SHA256SUMS").read_bytes()
    if sha256(sums).hexdigest() != _FROZEN_SUMS:
        raise QualificationRejected("revision: REA_RETENTION_SRC frozen manifest differs from the pinned bytes")
    measured = {}
    for line in sums.decode("ascii").splitlines():
        expected, name = line.split("  ", maxsplit=1)
        measured[name] = sha256((frozen / name).read_bytes()).hexdigest()
        if measured[name] != expected:
            raise QualificationRejected(f"revision: frozen schema {name} differs from its pinned bytes")
    sources = {
        str(path.relative_to(_COMPONENT)): sha256(path.read_bytes()).hexdigest()
        for area in ("qualify", "runtime", "projections", "compare")
        for path in sorted((_COMPONENT / "slot_host" / area).glob("*.py"))
    }
    tools_sources = {}
    package = tools / "retention_service"
    for path in sorted(package.rglob("*.py")):
        if not path.resolve(strict=True).is_relative_to(tools):
            raise QualificationRejected("revision: retention source is outside REA_RETENTION_SRC")
        tools_sources[path.relative_to(tools).as_posix()] = sha256(path.read_bytes()).hexdigest()
    if not tools_sources:
        raise QualificationRejected("revision: REA_RETENTION_SRC has no retention Python sources")
    return {
        "rea": git, "rea_source_sha256": projection.record_digest(sources),
        "tools": pins["tools_revision"], "tools_revision_basis": "configured archival revision",
        "tools_source_sha256": projection.record_digest(tools_sources),
        "tools_frozen_manifest_sha256": _FROZEN_SUMS, "tools_frozen_sha256": measured,
    }


@contextmanager
def reopen_service(root, witness_module, local):
    """Restart the actual Tools server in a fresh process with the persisted witness."""
    code = ("from slot_host.runtime.retention_deployment import import_tools; "
            "raise SystemExit(import_tools('retention_service.server.__main__').main())")
    env = {key: os.environ[key] for key in ("XDG_STATE_HOME", "XDG_RUNTIME_DIR", "REA_RETENTION_SRC")}
    with (root / "service-reopen.log").open("w+b") as log:
        child = subprocess.Popen(
            (sys.executable, "-B", "-c", code, "--witness-module", witness_module),
            cwd=_COMPONENT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
        starttime = proc_starttime(child.pid)
        try:
            if starttime is None:
                raise QualificationRejected("recovery: could not identify the restarted service process")
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    if local.check_denial_endpoint():
                        break
                except (OSError, ValueError, import_tools("retention_service.transport.client").RemoteError):
                    pass
                time.sleep(0.05)
            else:
                log.seek(0)
                diagnostic = log.read(1024).decode("utf-8", "replace").strip()
                raise QualificationRejected(f"recovery: restarted service did not become ready ({diagnostic})")
            yield {"pid": child.pid, "starttime_ticks": starttime}
        finally:
            if child.poll() is None and not terminate_only(child.pid, child, 2.0, starttime):
                raise QualificationRejected("recovery: restarted service retirement was not observed")


def main(argv=None):
    """Qualify a sealed workload with disposable authority under a capped user scope."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--out", required=True, type=Path, help="qualification receipt JSON path")
    args = parser.parse_args(argv)
    try:
        scope = scope_properties()
        pins, _ = local_open_profile()
        server = import_tools("retention_service.server.__main__")
        revisions = measure_revisions(pins, server.FROZEN_DIR)
        with isolated_deployment() as root:
            receipt = qualify_host(server, pins, scope, root, verify_readiness, revisions, reopen=reopen_service)
        write_receipt(args.out, receipt)
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        print(f"qualification refused: {exc}", file=sys.stderr)
        return 1
    return 0


def write_receipt(target, receipt):
    """Publish one completed qualification receipt with an atomic rename."""
    with NamedTemporaryFile(dir=target.parent, prefix=".qualification-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(projection.record_bytes(receipt) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
