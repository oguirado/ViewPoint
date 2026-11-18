"""F0 skeleton gate. A successful test process is only one acceptance input."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

try:
    from asset_factory import core, proc as af_proc
except ModuleNotFoundError as exc:
    if exc.name not in ("asset_factory", "asset_factory.core", "asset_factory.proc"):
        raise
    core = None
    af_proc = None


SCOPE = "F0-skeleton"
STATUS = {0: "OK", 2: "ERROR", 3: "INCOMPLETE", 4: "STALE", 5: "FAIL",
          6: "BUDGET_EXCEEDED"}
VERDICT = {0: "PASS", 2: "ERROR", 3: "CANNOT_SEE", 4: "CANNOT_SEE",
           5: "FAIL", 6: "CANNOT_SEE"}
REQUIRED = ("F0-U1", "F0-U2", "F0-U3", "F0-U4", "F0-U5", "F0-U6-selftest",
            "F0-U6-docs-ratio", "F0-U6-licensing", "F0-stub-scan", "W3-CONTRACTS", "PROCESS-SAFETY")
UNITTEST_LEGS = REQUIRED[:6] + ("W3-CONTRACTS", "PROCESS-SAFETY")
LEGS = {name: ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-p",
               "test_f0u%d_*.py" % number]
        for number, name in enumerate(REQUIRED[:6], 1)}
LEGS["W3-CONTRACTS"] = ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-p",
                        "test_w3_contracts_*.py"]
LEGS["PROCESS-SAFETY"] = ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-p",
                          "test_safety_proc*.py"]
LEGS["F0-U6-docs-ratio"] = ["python3", "-B", "-m", "tools.docs_ratio", "--root", "."]
for _name, _function in (("F0-U6-licensing", "_licensing"), ("F0-stub-scan", "_stub_scan")):
    LEGS[_name] = ["python3", "-B", "-c",
                   "from pathlib import Path; from tools.gates import " + _function +
                   "; from tools.gates import _emit; raise SystemExit(_emit(" +
                   _function + "(Path('.'))))"]

MAX_OUTPUT_BYTES = 2 * 2**20
DEFAULT_WORKERS = 2
_LEG_CANCELLED = ContextVar("gate_leg_cancelled", default=None)
LEG_KEYS = {"name", "argv", "rc", "status", "checks_evaluated", "failures",
            "duration_ms", "evidence"}
REPORT_KEYS = {"schema", "scope", "rc", "verdict", "checks_evaluated", "legs",
               "source_files", "failures"}
RECEIPT_KEYS = {"schema", "scope", "report", "source_files", "tool_files", "evidence_files"}


class _GateInterrupted(Exception):
    pass


def _interrupt(signum, frame):
    raise _GateInterrupted()


def _emit(obj):
    # The missing-kernel refusal must also work before U1 is integrated.
    print(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False))
    return obj["rc"]


def _leg(name, argv, rc, checks=0, failures=(), duration_ms=0, evidence=None):
    return {"name": name, "argv": list(argv), "rc": rc, "status": STATUS[rc],
            "checks_evaluated": checks, "failures": list(failures),
            "duration_ms": duration_ms, "evidence": evidence or {}}


def _summary(rc, checks, failures=()):
    return {"rc": rc, "checks_evaluated": checks, "failures": list(failures)}


def _licensing(root):
    """Compare with the enclosing ViewPoint repository's license bytes."""
    try:
        root = root.resolve(strict=True)
        reference = root.parents[1] / "LICENSE"
        paths = [root / "LICENSE", root / "README.md", root / "NOTICE", reference]
        if any(not path.is_file() for path in paths):
            return _summary(3, 0, ["licensing_file_missing"])
        if any(path.is_symlink() for path in paths):
            return _summary(2, 0, ["licensing_symlink"])
        license_bytes, readme, notice, original = [path.read_bytes() for path in paths]
        failures = []
        if hashlib.sha256(license_bytes).digest() != hashlib.sha256(original).digest():
            failures.append("license_mismatch")
        if b"AGPL-3.0-only" not in readme:
            failures.append("spdx_election_missing")
        # This is the verified U6 notice, not a general third-party license auditor.
        if notice != NOTICE_TEXT.encode("utf-8"):
            failures.append("notice_provenance_unverified")
        return _summary(5 if failures else 0, 3, failures)
    except (OSError, ValueError, IndexError):
        return _summary(2, 0, ["licensing_io"])


NOTICE_TEXT = ("Asset Factory — ViewPoint\n"
               "SPDX-License-Identifier: AGPL-3.0-only\n\n"
               "LICENSE is copied verbatim from the enclosing ViewPoint repository's LICENSE.\n"
               "The license document credits the Free Software Foundation; that attribution\n"
               "does not establish copyright ownership of the component source.\n"
               "Component copyright attribution: AUSENTE — no inventar.\n\n"
               "F0-U6 adds original stdlib gate tooling. No The-Construct source, third-party\n"
               "implementation, model weights, or model outputs are included by this unit.\n"
               "Provider manifests describe separately supplied software and weights; their\n"
               "licences do not grant rights to this component's users automatically.\n")


def _stub_scan(root):
    try:
        paths = sorted(str(path.relative_to(root)) for path in root.rglob("_afstub_*.py"))
        return _summary(5 if paths else 0, 1, paths)
    except OSError:
        return _summary(2, 0, ["stub_scan_io"])


def _reject_constant(value):
    raise ValueError("non_integer_number")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _parse(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_float=_reject_constant,
                      parse_constant=_reject_constant)


def _kill_group(pid):
    try:
        af_proc.signal_group(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stream_evidence(data):
    decoded = data.decode("utf-8", errors="replace")
    evidence = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "text": decoded}
    if decoded.encode("utf-8") != data:
        evidence["raw_base64"] = base64.b64encode(data).decode("ascii")
    return evidence


def _stream_bytes(evidence):
    if (not isinstance(evidence, dict) or type(evidence.get("bytes")) is not int
            or not isinstance(evidence.get("text"), str)):
        raise ValueError("stream_evidence_contract")
    if "raw_base64" in evidence:
        data = base64.b64decode(evidence["raw_base64"], validate=True)
    else:
        data = evidence["text"].encode("utf-8")
    if evidence != _stream_evidence(data):
        raise ValueError("stream_evidence_mismatch")
    return data


def run_leg(name: str, argv: list[str], timeout_s: int) -> dict:
    """Run one bounded, owned process group; translate only declared boundaries."""
    started = time.monotonic_ns()
    if (name not in REQUIRED or not isinstance(argv, list) or not argv or
            any(not isinstance(arg, str) or not arg for arg in argv) or
            type(timeout_s) is not int or timeout_s <= 0 or timeout_s > 300):
        return _leg(name, argv if isinstance(argv, list) else [], 2,
                    failures=["leg_arguments"])
    env = os.environ.copy()
    env.update(PYTHONPATH=os.pathsep.join((str(Path.cwd()), str(Path.cwd()/'tests'))),
               PYTHONDONTWRITEBYTECODE="1", PYTHONOPTIMIZE="0")
    rc, checks, failures, evidence = 2, 0, [], {}
    cancelled = _LEG_CANCELLED.get()
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            if cancelled is not None and cancelled():
                raise _GateInterrupted()
            child = af_proc.popen(argv, cwd=Path.cwd(), env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, start_new_session=True, cooperative_abort=True)
            timed_out = False
            try:
                deadline = time.monotonic() + timeout_s
                while child.returncode is None:
                    if cancelled is not None and cancelled():
                        raise _GateInterrupted()
                    if max(os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size) > MAX_OUTPUT_BYTES:
                        evidence["output_over_budget"] = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        af_proc.wait_exit(child, timeout=min(remaining, 0.05))
                    except subprocess.TimeoutExpired:
                        continue
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Descendants cannot outlive their leg, even if the parent exited.
                errors = af_proc.cleanup(child)
                if errors:
                    evidence.setdefault('cleanup_errors', []).extend(errors)
                    # Preserve an exception already in flight from the result.
                    if sys.exception() is None:
                        raise af_proc.SignalRefusal(errors[0])
            logs = []
            for key, stream in (("stdout", out), ("stderr", err)):
                if stream.seek(0, os.SEEK_END) > MAX_OUTPUT_BYTES:
                    evidence["output_over_budget"] = True
                    continue
                stream.seek(0)
                data = stream.read(MAX_OUTPUT_BYTES + 1)
                if len(data) > MAX_OUTPUT_BYTES:
                    evidence["output_over_budget"] = True
                    continue
                evidence[key] = _stream_evidence(data)
                logs.append(evidence[key]["text"])
            evidence["child_rc"] = child.returncode
            if timed_out or evidence.get("output_over_budget"):
                rc, failures = 6, ["deadline_exceeded" if timed_out else "output_over_budget"]
            elif name in UNITTEST_LEGS:
                text = "\n".join(logs)
                counts = re.findall(r"(?m)^Ran (\d+) tests? in ", text)
                skipped = sum(int(n) for n in re.findall(r"skipped=(\d+)", text))
                expected = sum(int(n) for n in re.findall(r"expected failures=(\d+)", text))
                evidence["skipped"] = skipped
                evidence["expected_failures"] = expected
                if len(counts) == 1:
                    count = int(counts[0])
                    checks = max(0, count - skipped - expected)
                    evidence["tests_run"] = count
                    if count == 0:
                        rc, failures = 3, ["unittest_coverage_incomplete"]
                    elif child.returncode == 1:
                        rc, failures = 5, ["unittest_failed"]
                    elif child.returncode == 0:
                        rc = 3 if count == 0 or skipped or expected else 0
                        failures = ["unittest_coverage_incomplete"] if rc else []
                    else:
                        failures = ["unexpected_child_rc"]
                else:
                    failures = ["unittest_summary_missing_or_ambiguous"]
            else:
                payload = _parse(logs[0])
                if name == "F0-U6-docs-ratio":
                    _validate_ratio(payload)
                    checks = len(payload["files"]) if payload["code_schema_lines"] else 0
                else:
                    if (not isinstance(payload, dict) or
                            set(payload) != {"rc", "checks_evaluated", "failures"}):
                        raise ValueError("leg_payload")
                    checks = payload["checks_evaluated"]
                    failures = payload["failures"]
                code = payload["rc"]
                if (type(code) is not int or code not in STATUS or
                        type(checks) is not int or checks < 0 or
                        not isinstance(failures, list) or
                        any(not isinstance(item, str) for item in failures) or
                        child.returncode != code):
                    raise ValueError("child_rc_payload_mismatch")
                rc = 3 if code == 0 and checks == 0 else code
                evidence["payload"] = payload
    except af_proc.SignalRefusal as exc:
        rc, failures = exc.rc, [exc.code]
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        rc, failures = 2, ["leg_contract_or_io"]
    duration = (time.monotonic_ns() - started) // 1000000
    return _leg(name, argv, rc, checks, failures, duration, evidence)


def _run_legs(workers):
    scheduled = [(name, LEGS[name]) for name in REQUIRED if name in LEGS]
    if workers == 1:
        return [run_leg(name, argv, 300) for name, argv in scheduled]

    # Only this overlap was measured. Keep the other legs on the main thread
    # and join the early job at its original position in the roster.
    early = "F0-U6-selftest"
    stopped = False

    def request_stop(signum, frame):
        # Let each runner retire its own Job before returning to the caller.
        nonlocal stopped
        stopped = True

    previous_handler = signal.signal(signal.SIGTERM, request_stop)
    token = _LEG_CANCELLED.set(lambda: stopped)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            legs, future = [], None
            try:
                for name, argv in scheduled:
                    if stopped:
                        raise _GateInterrupted()
                    if name == "F0-U3" and early in LEGS and future is None:
                        future = pool.submit(copy_context().run, run_leg, early, LEGS[early], 300)
                    if name == early and future is not None:
                        legs.append(future.result())
                    else:
                        legs.append(run_leg(name, argv, 300))
            except BaseException:
                stopped = True
                raise
    finally:
        _LEG_CANCELLED.reset(token)
        signal.signal(signal.SIGTERM, previous_handler)
    if stopped:
        raise _GateInterrupted()
    return legs


def _validate_ratio(payload):
    if (not isinstance(payload, dict) or set(payload) !=
            {"schema", "rc", "markdown_lines", "code_schema_lines", "files"} or
            payload["schema"] != "af.docs-ratio/v1" or
            type(payload["rc"]) is not int or payload["rc"] not in STATUS or
            any(type(payload[key]) is not int or payload[key] < 0
                for key in ("markdown_lines", "code_schema_lines")) or
            not isinstance(payload["files"], list)):
        raise ValueError("docs_ratio_contract")
    for item in payload["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256", "lines", "class"}
                or type(item["lines"]) is not int or item["lines"] < 0
                or item["class"] not in ("markdown", "code_schema", "other")):
            raise ValueError("docs_ratio_inventory")
        _validate_refs([{"path": item["path"], "sha256": item["sha256"]}])


def _validate_leg(leg):
    if (not isinstance(leg, dict) or set(leg) != LEG_KEYS or
            leg["name"] not in REQUIRED or type(leg["rc"]) is not int or
            leg["rc"] not in STATUS or leg["status"] != STATUS[leg["rc"]] or
            not isinstance(leg["argv"], list) or
            any(not isinstance(arg, str) for arg in leg["argv"]) or
            any(type(leg[key]) is not int or leg[key] < 0
                for key in ("checks_evaluated", "duration_ms")) or
            not isinstance(leg["failures"], list) or
            any(not isinstance(item, str) for item in leg["failures"]) or
            (leg["rc"] == 0 and bool(leg["failures"])) or
            not isinstance(leg["evidence"], dict)):
        raise ValueError("leg_contract")
    for key in ("stdout", "stderr"):
        if key in leg["evidence"]:
            _stream_bytes(leg["evidence"][key])


def aggregate(legs: list[dict]) -> dict:
    """Fixed membership plus ERROR > FAIL > CANNOT_SEE > PASS (never max)."""
    if core is None:
        raise ModuleNotFoundError("asset_factory.core is required", name="asset_factory.core")
    valid, failures, names = [], [], set()
    if not isinstance(legs, list):
        legs = []
        failures.append("legs_not_array")
    for leg in legs:
        try:
            _validate_leg(leg)
            if leg["name"] in names:
                raise ValueError("duplicate_leg")
            names.add(leg["name"])
            item = copy.deepcopy(leg)
            if item["rc"] == 0 and item["checks_evaluated"] == 0:
                item.update(rc=3, status=STATUS[3], failures=["zero_checks"])
            elif item["rc"] == 0 and not item["evidence"]:
                item.update(rc=3, status=STATUS[3], failures=["missing_evidence"])
            valid.append(item)
        except (ValueError, TypeError, KeyError):
            failures.append("invalid_or_duplicate_leg")
    for name in REQUIRED:
        if name not in names:
            valid.append(_leg(name, LEGS.get(name, []), 3, failures=["missing_leg"]))
    valid.sort(key=lambda leg: REQUIRED.index(leg["name"]))
    rc = core.worst_rc([leg["rc"] for leg in valid] + ([2] if failures else []))
    for leg in valid:
        if leg["rc"]:
            failures.append(leg["name"] + ":" + ",".join(leg["failures"] or [leg["status"]]))
    return {"schema": "af.whole-gate/v1", "scope": SCOPE, "rc": rc,
            "verdict": VERDICT[rc], "checks_evaluated": sum(leg["checks_evaluated"] for leg in valid),
            "legs": valid, "source_files": [], "failures": failures}


def _self_test():
    passing = [_leg(name, LEGS.get(name, []), 0, 1, evidence={"synthetic_control": True})
               for name in REQUIRED]
    failing = copy.deepcopy(passing)
    failing[0].update(rc=5, status=STATUS[5], failures=["planted_failure"])
    empty = copy.deepcopy(passing)
    empty[0]["checks_evaluated"] = 0
    observed = [aggregate(passing)["rc"], aggregate(failing)["rc"], aggregate(empty)["rc"]]
    ok = observed == [0, 5, 3]
    return _summary(0 if ok else 5, 3, [] if ok else ["aggregate_mutant_survived"])


def _validate_refs(refs):
    if not isinstance(refs, list):
        raise ValueError("references_not_array")
    names = set()
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"} or
                not isinstance(ref["path"], str) or not ref["path"] or
                ref["path"] in names or not isinstance(ref["sha256"], str) or
                not re.fullmatch("[0-9a-f]{64}", ref["sha256"])):
            raise ValueError("invalid_reference")
        names.add(ref["path"])


def _validate_report(report):
    if (not isinstance(report, dict) or set(report) != REPORT_KEYS or
            report["schema"] != "af.whole-gate/v1" or report["scope"] != SCOPE):
        raise ValueError("report_contract")
    _validate_refs(report["source_files"])
    rebuilt = aggregate(report["legs"])
    for key in ("rc", "verdict", "checks_evaluated", "failures", "legs"):
        if (type(report[key]) is not type(rebuilt[key]) or report[key] != rebuilt[key]):
            raise ValueError("report_aggregation_mismatch")


def _validate_receipt(receipt):
    if (not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS or
            receipt["schema"] != "af.gate-receipt/v1" or receipt["scope"] != SCOPE):
        raise ValueError("receipt_contract")
    _validate_refs([receipt["report"]])
    for key in ("source_files", "tool_files", "evidence_files"):
        _validate_refs(receipt[key])


def _bound_groups(sources, root):
    tools = [ref for ref in sources if Path(ref["path"]) in
             (root / "tools/gates.py", root / "tools/docs_ratio.py")]
    evidence = [ref for ref in sources if Path(ref["path"]).is_relative_to(root / "tests")]
    return tools, evidence


def _provenance_rc(report, receipt, root, excluded):
    sources = report["source_files"]
    reference = root.parents[1] / "LICENSE"
    if any(not Path(ref["path"]).is_relative_to(root) and
           Path(ref["path"]) != reference for ref in sources):
        return 4
    tools, evidence = _bound_groups(sources, root)
    if (not sources or len(tools) != 2 or not evidence or
            not receipt["tool_files"] or not receipt["evidence_files"]):
        return 3
    for number in range(1, 7):
        if not any(Path(ref["path"]).parent == root / "tests" and
                   Path(ref["path"]).match("test_f0u%d_*.py" % number) for ref in evidence):
            return 3
    if receipt["tool_files"] != tools or receipt["evidence_files"] != evidence:
        return 4
    # Inventory membership and byte identity are independent obligations. The
    # digest checks below remain the single consumer decision on bound bytes.
    current = _snapshot(root, excluded)
    if [ref["path"] for ref in current] != [ref["path"] for ref in sources]:
        return 4
    for key in ("source_files", "tool_files", "evidence_files"):
        for ref in receipt[key]:
            path = Path(ref["path"])
            if not path.is_absolute() or not path.is_file():
                return 3
            if core.sha256_file(path) != ref["sha256"]:
                return 4
    return 0


def _verify_pair(report_path, receipt_path, *, root, _pending=()):
    """Check a pair against a trusted root and its complete current inventory.

    Only the two output paths are normally excluded. The publisher also passes
    its exact owned staging/backup paths while they are needed for rollback.
    """
    try:
        root = Path(root).resolve(strict=True)
        report_path, receipt_path = _safe_output(report_path), _safe_output(receipt_path)
        if not report_path.is_file() or not receipt_path.is_file():
            return 3
        report_data = report_path.read_bytes()
        receipt_data = receipt_path.read_bytes()
        if max(len(report_data), len(receipt_data)) > 32 * 2**20:
            return 6
        report, receipt = _parse(report_data), _parse(receipt_data)
        _validate_report(report)
        _validate_receipt(receipt)
        if (receipt["report"]["path"] != str(report_path.resolve()) or
                receipt["report"]["sha256"] != hashlib.sha256(report_data).hexdigest() or
                receipt["source_files"] != report["source_files"]):
            return 4
        provenance_rc = _provenance_rc(report, receipt, root,
                                        {report_path, receipt_path, *_pending})
        if provenance_rc:
            return provenance_rc
        return report["rc"]
    except (OSError, ValueError, TypeError, KeyError):
        return 2


def _stage(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _safe_output(path):
    path = Path(os.path.abspath(path))
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("output_symlink")
    if not path.parent.is_dir():
        raise ValueError("output_parent_missing")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("output_not_owned_regular_file")
    return path


def _publish(report, report_path, receipt_path, protected=(), *, root):
    """Stage both files; receipt is the final marker. Roll back caught failures.

    A process crash between replacements is detected by _verify_pair's hash
    binding. Cross-file crash atomicity is not claimed.
    """
    _validate_report(report)
    if report["rc"] != 0:
        return report["rc"]
    root = Path(root).resolve(strict=True)
    report_path, receipt_path = _safe_output(report_path), _safe_output(receipt_path)
    if report_path == receipt_path or any(path in protected for path in (report_path, receipt_path)):
        raise ValueError("output_alias_or_source_collision")
    sources = report["source_files"]
    tools, evidence = _bound_groups(sources, root)
    report_bytes = core.canonical_json(report).encode("utf-8")
    receipt = {"schema": "af.gate-receipt/v1", "scope": SCOPE,
               "report": {"path": str(report_path), "sha256": hashlib.sha256(report_bytes).hexdigest()},
               "source_files": sources,
               "tool_files": tools, "evidence_files": evidence}
    _validate_receipt(receipt)
    provenance_rc = _provenance_rc(report, receipt, root, {report_path, receipt_path})
    if provenance_rc:
        return provenance_rc
    receipt_bytes = core.canonical_json(receipt).encode("utf-8")
    paths, staged, backups, replaced = (report_path, receipt_path), [], [], []
    try:
        for path, data in zip(paths, (report_bytes, receipt_bytes)):
            staged.append(_stage(path, data))
            backups.append(_stage(path, path.read_bytes()) if path.exists() else None)
        for path, temporary in zip(paths, staged):
            # Record intent before replacement: a signal can arrive after the
            # kernel replaces the file but before os.replace returns to Python.
            replaced.append(path)
            os.replace(temporary, path)
        pending = [path for path in staged + backups if path is not None]
        if _verify_pair(report_path, receipt_path, root=root, _pending=pending) != 0:
            raise ValueError("published_pair_invalid")
    except BaseException:
        for path in reversed(replaced):
            backup = backups[paths.index(path)]
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backup, path)
        raise
    finally:
        for temporary in staged + backups:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return 0


def _snapshot(root, excluded):
    from tools.docs_ratio import MAX_FILES, MAX_SECONDS, measure_docs_ratio
    root = Path(root).resolve(strict=True)
    # The frozen references carry file bytes, not alias identities. Refuse all
    # source aliases rather than omit executable inputs from that contract.
    started, seen = time.monotonic(), 0
    def walk_error(exc):
        raise exc
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs + names:
            seen += 1
            if seen > MAX_FILES or time.monotonic() - started > MAX_SECONDS:
                raise ValueError("source_inventory_unavailable")
            if (Path(directory) / name).is_symlink():
                raise ValueError("source_symlink")
    inventory = measure_docs_ratio(root)
    if inventory["rc"] in (2, 6):
        raise ValueError("source_inventory_unavailable")
    files = [{"path": str(root / item["path"]), "sha256": item["sha256"]}
             for item in inventory["files"] if root / item["path"] not in excluded]
    # The license comparison consumes this file outside the component boundary.
    reference = root.parents[1] / "LICENSE"
    if reference.is_symlink():
        raise ValueError("source_symlink")
    if reference.is_file():
        files.append({"path": str(reference), "sha256": core.sha256_file(reference)})
    return sorted(files, key=lambda ref: ref["path"])


def _check_source_output(root, path, schema):
    if path.is_relative_to(root):
        if path.suffix != ".json" or path.name.endswith(".schema.json"):
            raise ValueError("source_output_collision")
        if path.exists():
            with path.open("rb") as stream:
                content = stream.read(32 * 2**20 + 1)
            if len(content) > 32 * 2**20:
                raise ValueError("output_over_budget")
            payload = _parse(content)
            if not isinstance(payload, dict) or payload.get("schema") != schema:
                raise ValueError("source_output_collision")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--st-schedule", type=Path,
                        help="opt in to a pinned impact and shard configuration")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=DEFAULT_WORKERS,
                        help="gate workers (default: %(default)s, 1 runs serially)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    if core is None:
        return _emit({"rc": 3, "status": STATUS[3], "reason_codes": ["kernel_missing"]})
    previous = Path.cwd()
    previous_handler = signal.signal(signal.SIGTERM, _interrupt)
    try:
        if args.self_test:
            return _emit(_self_test())
        try:
            # This runner supplies each leg's component/tests import path;
            # namespace admission itself remains mandatory before any launch.
            af_proc.check_test_environment(require_pythonpath=False)
        except af_proc.SignalRefusal as exc:
            return _emit({**exc.as_dict(), 'hint': af_proc.TEST_ENVIRONMENT})
        if any(value is None for value in (args.root, args.report, args.receipt)):
            raise ValueError("all_requires_root_report_receipt")
        root = args.root.resolve(strict=True)
        report_path, receipt_path = _safe_output(args.report), _safe_output(args.receipt)
        if report_path == receipt_path:
            raise ValueError("output_alias")
        _check_source_output(root, report_path, "af.whole-gate/v1")
        _check_source_output(root, receipt_path, "af.gate-receipt/v1")
        excluded = {report_path, receipt_path}
        before = _snapshot(root, excluded)
        protected = {Path(ref["path"]) for ref in before}
        if any(path in protected for path in excluded):
            raise ValueError("source_collision")
        if args.st_schedule is None:
            os.chdir(root)
            legs = _run_legs(args.workers)
        else:
            from tools.testing import shard
            config = shard.configuration(args.st_schedule)
            os.chdir(root)
            legs = shard.gate_legs(config, before, root, sys.modules[__name__])
        report = aggregate(legs)
        report["source_files"] = before
        after = _snapshot(root, excluded)
        if after != before:
            # Bind the operation to the original bytes; drift cannot be green.
            report["rc"], report["verdict"] = core.worst_rc([report["rc"], 4]), "CANNOT_SEE"
            if report["rc"] in (2, 5):
                report["verdict"] = VERDICT[report["rc"]]
            report["failures"].append("source_drift")
        if report["rc"] == 0:
            publication_rc = _publish(report, report_path, receipt_path, protected, root=root)
            if publication_rc:
                return _emit({"rc": publication_rc, "status": STATUS[publication_rc],
                              "reason_codes": ["publication_provenance_unavailable"]})
        return _emit(report)
    except _GateInterrupted:
        return _emit({"rc": 6, "status": STATUS[6], "reason_codes": ["deadline_exceeded"]})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _emit({"rc": 2, "status": STATUS[2], "reason_codes": [str(exc)]})
    finally:
        os.chdir(previous)
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
