"""F2-U1 public mesh API. Geometry-only acceptance, never appearance approval.

Every call reads immutable snapshots, evaluates in an isolated owned child and
returns data. No caller output path exists. The sibling component is resolved
relative to this component, not via cwd, PYTHONPATH or a system installation.
"""
import base64
import multiprocessing
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

from tools.mesh.contracts import (MAX_BYTES, MeshError, canonical, digest, finish,
                                 json_digest, loads, receipt, validate_policy,
                                 validate_receipt, validate_success)

ROOT = Path(__file__).resolve().parents[2]
SIBLING = ROOT.parent / "blender-visual-qa"
WORKER = Path(__file__).with_name("worker.py")
V_FILES = ("__init__.py", "assets/__init__.py", "assets/glb_core.py", "assets/limits.py",
           "assets/inspect_gate.py", "assets/lod_ladder.py", "assets/mesh_gate.py",
           "assets/ortho4.py", "assets/heal.py")


def toolchain_identity(deadline=None):
    """Content pin over the wrapper, schema and the sibling's import closure."""
    files = {"af/" + name: Path(__file__).with_name(name) for name in
             ("api.py", "worker.py", "contracts.py", "mesh-regression.schema.json")}
    files.update({"v/" + name: SIBLING / "blender_visual_qa" / name for name in V_FILES})
    try:
        return json_digest({key: digest(_acquire(path, deadline) if deadline is not None else _read(path))
                            for key, path in files.items()})
    except OSError as exc:
        raise MeshError(3, "evaluator_unavailable") from exc


def _regular_stream(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MeshError(2, "non_regular_input")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _read(path):
    with _regular_stream(path) as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise MeshError(6, "input_budget_exceeded")
    return data


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("mesh operation", 0)
    return remaining


def _acquire(path, deadline):
    """Bound open/read/final rehash in a disposable owned IO process.

    A thread cannot cancel a blocked filesystem read. Linux fork also keeps
    caller monkeypatches in fault-injection tests without touching caller paths.
    """
    _remaining(deadline)
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    def read_input():
        receiver.close()
        try:
            sender.send(("bytes", _read(path)))
        except MeshError as exc:
            sender.send(("mesh_error", (exc.rc, exc.code)))
        except OSError as exc:
            sender.send(("os_error", exc))
        except Exception:
            sender.send(("mesh_error", (2, "invalid_input")))
        finally:
            sender.close()
    child = context.Process(target=read_input)
    try:
        child.start()
        sender.close()
        if not receiver.poll(_remaining(deadline)):
            raise subprocess.TimeoutExpired("mesh input", 0)
        kind, value = receiver.recv()
        _remaining(deadline)
        if kind == "mesh_error":
            raise MeshError(*value)
        if kind == "os_error":
            raise value
        return value
    except EOFError as exc:
        raise MeshError(2, "input_reader_contract") from exc
    finally:
        sender.close()
        receiver.close()
        if child.pid is not None:
            if child.is_alive():
                child.kill()
            child.join()


def _spawn(request, timeout):
    """Bounded stdout spool; kill the owned process group on every abnormal exit."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child = subprocess.Popen([sys.executable, "-I", "-B", str(WORKER)],
                                 stdin=subprocess.PIPE, stdout=output, stderr=errors,
                                 cwd=ROOT, start_new_session=True,
                                 env={"PATH": os.defpath, "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            child.communicate(canonical(request).encode("utf-8"), timeout=timeout)
        finally:
            # Also reap a descendant left behind by a prematurely exiting child.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        if child.returncode not in (0, 2, 3, 4, 5, 6):
            raise MeshError(2, "evaluator_contract")
        if output.tell() > 3 * MAX_BYTES:
            raise MeshError(6, "output_budget_exceeded")
        output.seek(0)
        result = loads(output.read())
        if (not isinstance(result, dict) or set(result) != {"receipt", "artefact"}
                or not isinstance(result["receipt"], dict)):
            raise MeshError(2, "evaluator_contract")
        validate_receipt(result["receipt"])
        if result["receipt"]["rc"] != child.returncode:
            raise MeshError(2, "evaluator_exit_mismatch")
        return result


def _evaluate(operation, raw_glb, candidate_glb, policy, *, deadline=None):
    started = time.monotonic()
    rec = receipt()
    artefact = None
    try:
        validate_policy(policy)
        if deadline is None:
            deadline = started + policy["timeout_s"]
        policy = loads(canonical(policy))
        rec["policy_sha256"] = json_digest(policy)
        raw = _acquire(raw_glb, deadline)
        candidate = _acquire(candidate_glb, deadline) if candidate_glb is not None else None
        rec["input_sha256"] = digest(raw)
        rec["candidate_sha256"] = digest(candidate) if candidate is not None else None
        pin = toolchain_identity(deadline)
        rec["toolchain_sha256"] = pin
        if "toolchain_sha256" in policy and policy["toolchain_sha256"] != pin:
            raise MeshError(4, "toolchain_stale")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MeshError(6, "deadline_exceeded")
        result = _spawn({"operation": operation, "raw": base64.b64encode(raw).decode("ascii"),
                         "candidate": base64.b64encode(candidate).decode("ascii") if candidate is not None else None,
                         "policy": policy, "receipt": rec}, remaining)
        if not isinstance(result, dict) or set(result) != {"receipt", "artefact"}:
            raise MeshError(2, "evaluator_contract")
        evaluated = result["receipt"]
        validate_receipt(evaluated)
        validate_success(evaluated, operation)
        for key in ("input_sha256", "policy_sha256", "toolchain_sha256", "prev_sha256"):
            if evaluated[key] != rec[key]:
                raise MeshError(2, "evaluator_binding")
        if operation != "repair" and evaluated["candidate_sha256"] != rec["candidate_sha256"]:
            raise MeshError(2, "evaluator_binding")
        rec = evaluated
        if result["artefact"] is not None:
            artefact = base64.b64decode(result["artefact"], validate=True)
            if (operation != "repair" or rec["rc"] != 0 or len(artefact) > MAX_BYTES
                    or digest(artefact) != rec.get("artefact_sha256")
                    or digest(artefact) != rec["candidate_sha256"]):
                raise MeshError(2, "artefact_binding")
        elif operation == "repair" and rec["rc"] == 0:
            raise MeshError(2, "missing_artefact")
        if digest(_acquire(raw_glb, deadline)) != rec["input_sha256"]:
            raise MeshError(4, "input_stale")
        if candidate is not None and digest(_acquire(candidate_glb, deadline)) != rec["candidate_sha256"]:
            raise MeshError(4, "candidate_stale")
        if pin != toolchain_identity(deadline):
            raise MeshError(4, "toolchain_stale")
        if time.monotonic() > deadline:
            raise MeshError(6, "deadline_exceeded")
        return rec["rc"], rec, artefact
    except subprocess.TimeoutExpired:
        rc, rec = finish(rec, 6, "deadline_exceeded")
    except MeshError as exc:
        rc, rec = finish(rec, exc.rc, exc.code)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        rc, rec = finish(rec, 2, "invalid_input_or_evaluator")
    return rc, rec, None


def component_metrics(glb_path: Path, *, policy: dict) -> tuple[int, dict]:
    rc, rec, _ = _evaluate("components", glb_path, None, policy)
    return rc, rec


def compare_meshes(raw_glb: Path, candidate_glb: Path, *, policy: dict) -> tuple[int, dict]:
    rc, rec, _ = _evaluate("compare", raw_glb, candidate_glb, policy)
    return rc, rec


def repair_mesh(raw_glb: Path, *, policy: dict) -> tuple[int, dict, bytes | None]:
    return _evaluate("repair", raw_glb, None, policy)


def read_receipt(receipt_path: Path, raw_glb: Path, candidate_glb: Path, *, policy: dict) -> tuple[int, dict]:
    """Revalidate cached evidence against bound bytes and fresh measurements.

This is a local consumer helper, not paired artifact/receipt publication.
"""
    rec = receipt()
    started = time.monotonic()
    try:
        validate_policy(policy)
        deadline = started + policy["timeout_s"]
        try:
            previous = loads(_acquire(receipt_path, deadline))
        except FileNotFoundError:
            return finish(rec, 3, "receipt_missing")
        validate_receipt(previous)
        rec = previous
        bindings = {"input_sha256": digest(_acquire(raw_glb, deadline)),
                    "candidate_sha256": digest(_acquire(candidate_glb, deadline)),
                    "policy_sha256": json_digest(policy), "toolchain_sha256": toolchain_identity(deadline)}
        if any(previous[key] != value for key, value in bindings.items()):
            return finish(rec, 4, "receipt_stale")
        rc, current, _ = _evaluate("compare", raw_glb, candidate_glb, policy, deadline=deadline)
        if rc in (2, 6):
            return rc, current
        if previous != current:
            return finish(rec, 4, "receipt_evidence_stale")
        return rc, current
    except subprocess.TimeoutExpired:
        return finish(rec, 6, "deadline_exceeded")
    except MeshError as exc:
        return finish(rec, exc.rc, exc.code)
    except (OSError, ValueError, TypeError):
        return finish(rec, 2, "invalid_receipt")
