"""Report reconciliation, receipt writer, and atomic output for blender_visual_qa."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from blender_visual_qa import contracts


def make_metric(
    value: float | int | None,
    unit: str,
    provenance: str = "MEASURED",
    absence_reason: str | None = None,
) -> dict[str, Any]:
    """Create a metric entry obeying D-S1-9 rules."""
    import math
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
        raise contracts.ContractError("invalid measured metric", "invalid_metric", "report")
    if value is None:
        return {
            "value": None,
            "unit": unit,
            "provenance": "AUSENTE",
            "absence_reason": absence_reason or "AUSENTE — no inventar",
        }
    return {
        "value": float(value),
        "unit": unit,
        "provenance": provenance if provenance != "AUSENTE" else "MEASURED",
        "absence_reason": None,
    }


def default_metrics() -> dict[str, Any]:
    """Return default metrics block with AUSENTE provenance."""
    return {
        "gemini_calls": make_metric(None, "calls", absence_reason="not dispatched in this scope"),
        "frames": make_metric(None, "frames", absence_reason="no frames captured in this scope"),
        "clip_seconds": make_metric(None, "seconds", absence_reason="no clips captured in this scope"),
        "input_tokens": make_metric(None, "tokens", absence_reason="no model calls in this scope"),
        "output_tokens": make_metric(None, "tokens", absence_reason="no model calls in this scope"),
        "wall_seconds": make_metric(None, "seconds", absence_reason="not timed"),
        "cost_usd": make_metric(None, "usd", absence_reason="no provider billing in this scope"),
    }


def atomic_write_json(target_path: Path, data: Any) -> None:
    """Atomically write JSON data with fsync and rename."""
    target_path = Path(target_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".tmp.{target_path.name}.{os.getpid()}")

    content = contracts.canonical_json(data)
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp_path, target_path)


def collect_run_artifacts(run_dir: Path | str) -> list[dict[str, str]]:
    """Enumerate sorted actual evidence/telemetry/manifest/oracle/judge/log/provenance assets."""
    r_dir = Path(run_dir).resolve()
    artifacts: list[dict[str, str]] = []
    for root, dirs, files in os.walk(r_dir):
        dirs[:] = [d for d in dirs if not (Path(root) == r_dir and d == "config")]
        for fname in sorted(files):
            if (Path(root) == r_dir and fname in ("report.json", "receipt.json")) or fname.startswith(".tmp."):
                continue
            fpath = Path(root) / fname
            if fpath.is_symlink():
                try:
                    fpath.resolve().relative_to(r_dir)
                except ValueError:
                    continue
            rel_path = str(fpath.relative_to(r_dir)).replace("\\", "/")
            artifacts.append({
                "path": rel_path,
                "sha256": contracts.compute_file_sha256(fpath),
            })
    return sorted(artifacts, key=lambda x: x["path"])


def write_receipt(
    run_dir: Path | str,
    config: contracts.Config,
    report_dict: dict[str, Any],
    artifact_files: list[dict[str, str]] | None = None,
    tool_files: list[dict[str, str]] | None = None,
    gate_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Write receipt.json binding report hash and artifact files."""
    r_dir = Path(run_dir).resolve()
    report_file = r_dir / "report.json"
    if not report_file.is_file():
        raise contracts.ContractError("Cannot write receipt without existing report.json", code="missing_report", stage="receipt")

    tool_f = tool_files or config.tool_files

    # Input files: if config directory exists in run_dir, collect staged inputs
    config_dir = r_dir / "config"
    if config_dir.is_dir():
        input_f = []
        for root, dirs, files in os.walk(config_dir):
            for fname in sorted(files):
                fpath = Path(root) / fname
                rel_path = str(fpath.relative_to(r_dir)).replace("\\", "/")
                input_f.append({"path": rel_path, "sha256": contracts.compute_file_sha256(fpath)})
        input_f = sorted(input_f, key=lambda x: x["path"])
    else:
        input_f = config.input_files

    art_f = artifact_files if artifact_files is not None else collect_run_artifacts(r_dir)

    # Consumers authenticate capture, telemetry and fault witnesses through the
    # report as well as the receipt. Root report/receipt remain outside bindings
    # to avoid a hash cycle; nested receipts are ordinary evidence artefacts.
    report_bindings = {}
    for ref in input_f + art_f:
        if ref["path"] in ("report.json", "receipt.json"):
            continue
        if ref["path"] in report_bindings and report_bindings[ref["path"]] != ref["sha256"]:
            raise contracts.ContractError("Conflicting report evidence binding", "binding_conflict", "receipt")
        report_bindings[ref["path"]] = ref["sha256"]
    report_dict["bindings"] = [{"path": path, "sha256": report_bindings[path]} for path in sorted(report_bindings)]
    contracts.validate_schema(report_dict, "report")
    atomic_write_json(report_file, report_dict)
    report_sha256 = contracts.compute_file_sha256(report_file)

    if gate_refs is None:
        g_refs = []
        for gr in config.adapter.get("gate_refs", []):
            if isinstance(gr, dict):
                for k in ("gate_log", "ledger"):
                    if k in gr and isinstance(gr[k], dict) and "path" in gr[k] and "sha256" in gr[k]:
                        g_refs.append({"path": "config/" + gr[k]["path"], "sha256": gr[k]["sha256"]})
    else:
        g_refs = gate_refs

    receipt = {
        "schema": "vqa.receipt/v1",
        "run_id": report_dict["run_id"],
        "configuration_sha256": config.configuration_sha256,
        "tool_files": sorted(tool_f, key=lambda x: x["path"]),
        "input_files": sorted(input_f, key=lambda x: x["path"]),
        "artifact_files": sorted(art_f, key=lambda x: x["path"]),
        "report": {
            "path": "report.json",
            "sha256": report_sha256,
        },
        "gate_refs": sorted(g_refs, key=lambda x: x["path"]),
    }

    contracts.validate_schema(receipt, "receipt")
    receipt_file = r_dir / "receipt.json"
    atomic_write_json(receipt_file, receipt)
    return receipt


# @anchor:bv.report.reconcile
def reconcile(
    config: contracts.Config | None = None,
    capture_manifest: dict[str, Any] | None = None,
    deterministic_results: list[dict[str, Any]] | None = None,
    judge_results: dict[str, Any] | list[dict[str, Any]] | None = None,
    scope: str = "render",
    command: str = "run",
    stages: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    incomplete: list[str] | None = None,
    selftest: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    bindings: list[dict[str, str]] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Reconcile findings, compute exit precedence, and generate vqa.report/v1."""
    stages_list = list(stages or [])
    errors_list = list(errors or [])
    incomplete_list = list(incomplete or [])
    findings: list[dict[str, Any]] = []

    if scope == "render" and selftest is None and capture_manifest is None:
        incomplete_list.append("capture manifest missing")

    # Process capture manifest completeness and errors
    if capture_manifest is not None:
        for err in capture_manifest.get("errors", []):
            if isinstance(err, dict):
                errors_list.append(err)
            else:
                errors_list.append({"code": "capture_error", "stage": "drive_capture", "message": str(err)})
        if capture_manifest.get("scope") not in (None, "render"):
            incomplete_list.append("capture scope cannot qualify render")
        if not contracts.capture_recording_complete(capture_manifest):
            if "capture incomplete" not in incomplete_list:
                incomplete_list.append("capture incomplete")
        for inc in capture_manifest.get("incomplete", []):
            if inc not in incomplete_list:
                incomplete_list.append(inc)
        if config and config.plan:
            plan_points = {p.get("id") for p in config.plan.get("points", [])}
            cap_points = {p.get("point_id") or p.get("id") for p in capture_manifest.get("points", []) if p.get("complete", False)}
            missing_pts = plan_points - cap_points
            for m_pt in sorted(missing_pts):
                pt_inc_msg = f"Point {m_pt} declared in capture-plan is incomplete or missing in capture"
                if pt_inc_msg not in incomplete_list:
                    incomplete_list.append(pt_inc_msg)
        for st in stages_list:
            if st.get("id") == "drive_capture":
                if capture_manifest.get("errors"):
                    st["status"] = "ERROR"
                    st["reason"] = "; ".join(str(e) for e in capture_manifest["errors"])
                elif not contracts.capture_recording_complete(capture_manifest) or capture_manifest.get("incomplete"):
                    st["status"] = "INCOMPLETE"
                    st["reason"] = "; ".join(capture_manifest.get("incomplete") or ["native reference drift" if capture_manifest.get("drive_results") else "capture incomplete"])

    if config is not None and capture_manifest is not None:
        contracts.verify_oracle_bindings(config, capture_manifest, deterministic_results or [])
        if isinstance(judge_results, dict) and "results" in judge_results:
            contracts.verify_judge_bindings(config, capture_manifest, judge_results["results"])

    # Only point-specific results bind ordinary point checks. Cross-event results explicitly have no point.
    det_map = {}
    for result in deterministic_results or []:
        for dr in result.get("instances", [result]):
            contracts.validate_schema(dr, "oracle-result")
            cid, pid = dr["check_id"], dr.get("point_id")
            key = f"{cid}:{pid}" if pid else cid
            if key in det_map:
                raise contracts.ContractError("duplicate oracle instance", "invalid_oracle_results", "oracles")
            det_map[key] = dr
            if dr["oracle_id"] == "cards.variation":
                det_map[cid] = dr

    # Map judge results and batch outputs
    judge_output_map: dict[str, dict[str, Any]] = {}
    judge_result_map: dict[str, dict[str, Any]] = {}

    def _register_batch_output(item):
        contracts.validate_schema(item, "judge-output")
        for row in item["results"]:
            judge_output_map[row["check_id"]] = item

    if isinstance(judge_results, dict):
        if judge_results.get("schema") == "vqa.judge-output/v1":
            raise contracts.ContractError("raw judge responses are not acceptance authority", "invalid_judge_results", "judge")
        if "outputs" in judge_results and isinstance(judge_results["outputs"], list):
            for out_item in judge_results["outputs"]:
                _register_batch_output(out_item)
        elif "findings" in judge_results and isinstance(judge_results["findings"], list):
            for jf in judge_results["findings"]:
                _register_batch_output(jf)
        elif "results" not in judge_results:
            raise contracts.ContractError("normalized judge results missing", "invalid_judge_results", "judge")

        # Raw outputs supply evidence only; normalized final results alone authorize acceptance.
        judge_result_map.clear()
        if "results" in judge_results and isinstance(judge_results["results"], list):
            contracts.validate_normalized_judge(judge_results["results"])
            for r in judge_results["results"]:
                if isinstance(r, dict) and "check_id" in r:
                    c_id = r["check_id"]
                    p_id = r.get("point_id")
                    if p_id:
                        judge_result_map[f"{c_id}:{p_id}"] = r
                    judge_result_map[c_id] = r
    elif isinstance(judge_results, list):
        raise contracts.ContractError("raw judge batches are not acceptance authority", "invalid_judge_results", "judge")

    checks_evaluated = 0
    fails_count = 0
    cannot_see_count = 0
    expected_count = 0

    if config is not None and selftest is None:
        checklist_checks = config.checklist.get("checks", [])
        for ch in checklist_checks:
            cid = ch["id"]
            point_ids = ch.get("point_ids", [])
            advisory = ch.get("required") is False
            if not advisory:
                expected_count += len(point_ids)

            repo_entry = str((Path(__file__).resolve().parent.parent / "bin" / "blender-visual-qa"))
            reproduce_argv = [
                repo_entry,
                "run",
                "--adapter",
                str(config.adapter_path),
                "--checklist",
                str(config.checklist_path),
                "--capture-plan",
                str(config.plan_path),
                "--out",
                str(config.adapter_path.parent.parent / "reproduce_out"),
            ]

            for pid in point_ids:
                coverage_start = len(incomplete_list)
                det_res = det_map.get(f"{cid}:{pid}")
                shared = det_map.get(cid)
                if det_res is None and shared is not None and (len(point_ids) == 1 or ch.get("deterministic", {}).get("oracle_id") == "cards.variation"):
                    det_res = shared
                judge_out = judge_output_map.get(f"{cid}:{pid}") or judge_output_map.get(cid)
                judge_res = judge_result_map.get(f"{cid}:{pid}") or judge_result_map.get(cid)

                det_v = det_res.get("verdict") if det_res else None
                judge_v = judge_res.get("verdict") if judge_res else None

                # Dual oracle reconciliation
                disagreement = False
                final_verdict = "CANNOT_SEE"
                reason_parts: list[str] = []

                requires_det = ch.get("deterministic") is not None
                requires_judge = ch.get("judge") is not None

                if requires_det and requires_judge:
                    if det_v is None or judge_v is None:
                        coverage_deficit = f"Check {cid} point {pid}: missing required evaluator coverage (det={det_v}, judge={judge_v})"
                        incomplete_list.append(coverage_deficit)
                        if det_v == "FAIL":
                            final_verdict = "FAIL"
                            reason_parts.append("Deterministic evaluator reported FAIL")
                        elif judge_v == "FAIL":
                            final_verdict = "FAIL"
                            reason_parts.append("Judge evaluator reported FAIL")
                        else:
                            final_verdict = "CANNOT_SEE"
                            reason_parts.append(f"Missing required evaluator (det={det_v}, judge={judge_v})")
                    elif det_v == "PASS" and judge_v == "PASS":
                        final_verdict = "PASS"
                        reason_parts.append("Both deterministic and judge evaluators PASS")
                    elif det_v == "FAIL" and judge_v == "PASS":
                        final_verdict = "FAIL"
                        disagreement = True
                        reason_parts.append("Deterministic FAIL disagrees with judge PASS")
                    elif det_v == "PASS" and judge_v == "FAIL":
                        final_verdict = "FAIL"
                        disagreement = True
                        reason_parts.append("Deterministic PASS disagrees with judge FAIL")
                    elif det_v == "FAIL" and judge_v == "FAIL":
                        final_verdict = "FAIL"
                        reason_parts.append("Both deterministic and judge evaluators FAIL")
                    elif det_v == "FAIL" and judge_v == "CANNOT_SEE":
                        final_verdict = "FAIL"
                        coverage_deficit = f"Check {cid} point {pid}: required evaluator CANNOT_SEE (det={det_v}, judge={judge_v})"
                        incomplete_list.append(coverage_deficit)
                        reason_parts.append("Deterministic FAIL; judge CANNOT_SEE")
                    elif det_v == "CANNOT_SEE" and judge_v == "FAIL":
                        final_verdict = "FAIL"
                        coverage_deficit = f"Check {cid} point {pid}: required evaluator CANNOT_SEE (det={det_v}, judge={judge_v})"
                        incomplete_list.append(coverage_deficit)
                        reason_parts.append("Judge FAIL; deterministic CANNOT_SEE")
                    else:  # CANNOT_SEE cases
                        final_verdict = "CANNOT_SEE"
                        reason_parts.append(f"Unresolved observation: det={det_v}, judge={judge_v}")
                elif requires_det:
                    if det_v == "PASS":
                        final_verdict = "PASS"
                        reason_parts.append("Deterministic evaluator PASS")
                    elif det_v == "FAIL":
                        final_verdict = "FAIL"
                        reason_parts.append("Deterministic evaluator FAIL")
                    else:
                        final_verdict = "CANNOT_SEE"
                        reason_parts.append("Deterministic evaluator CANNOT_SEE or missing")
                        incomplete_list.append(f"Check {cid} point {pid}: missing required evaluator coverage (det={det_v}, judge=None)")
                elif requires_judge:
                    if judge_v == "PASS":
                        final_verdict = "PASS"
                        reason_parts.append("Judge evaluator PASS")
                    elif judge_v == "FAIL":
                        final_verdict = "FAIL"
                        reason_parts.append("Judge evaluator FAIL")
                    else:
                        final_verdict = "CANNOT_SEE"
                        reason_parts.append("Judge evaluator CANNOT_SEE or missing")
                        incomplete_list.append(f"Check {cid} point {pid}: missing required evaluator coverage (det=None, judge={judge_v})")

                # Collect evidence ids
                evidence_ids: list[str] = []
                if det_res and "evidence_ids" in det_res:
                    evidence_ids.extend(det_res["evidence_ids"])
                if judge_res and "evidence_ids" in judge_res:
                    for eid in judge_res["evidence_ids"]:
                        if eid not in evidence_ids:
                            evidence_ids.append(eid)
                elif judge_res and "observations" in judge_res and isinstance(judge_res["observations"], list):
                    for obs in judge_res["observations"]:
                        if isinstance(obs, dict) and obs.get("evidence_id"):
                            eid = obs["evidence_id"]
                            if eid not in evidence_ids:
                                evidence_ids.append(eid)
                if not evidence_ids and judge_out and "media_seen" in judge_out and isinstance(judge_out["media_seen"], list):
                    for mid in judge_out["media_seen"]:
                        if mid not in evidence_ids:
                            evidence_ids.append(mid)
                if not evidence_ids:
                    evidence_ids = [f"point_{pid}"]

                tick = None
                if det_res and det_res.get("tick") is not None:
                    tick = det_res["tick"]
                elif judge_res and judge_res.get("tick") is not None:
                    tick = judge_res["tick"]

                # Confidence: null unless accepted calibrated judge confidence
                confidence = None
                if judge_res and judge_res.get("confidence") is not None:
                    try:
                        c_val = float(judge_res["confidence"])
                        if 0.0 <= c_val <= 1.0:
                            confidence = c_val
                    except (ValueError, TypeError):
                        pass

                reason_text = "; ".join(reason_parts) if reason_parts else f"Verdict {final_verdict} for check {cid}"

                finding_entry = {
                    "check_id": cid,
                    "point_id": pid,
                    "verdict": final_verdict,
                    "evidence_ids": evidence_ids,
                    "tick": tick,
                    "reproduce_argv": reproduce_argv,
                    "confidence": confidence,
                    "deterministic": det_res,
                    "judge": judge_out,
                    "disagreement": disagreement,
                    "reason": reason_text,
                }
                findings.append(finding_entry)

                if advisory:
                    finding_entry["advisory"] = True
                    finding_entry["advisory_reason"] = ch["reason"]
                    # Evaluator coverage is reported on the finding but cannot gate.
                    del incomplete_list[coverage_start:]
                else:
                    checks_evaluated += 1
                    if final_verdict == "FAIL":
                        fails_count += 1
                    elif final_verdict == "CANNOT_SEE":
                        cannot_see_count += 1

    if capture_manifest:
        native_findings = {}
        for row in capture_manifest.get("drive_results", []):
            render = row["render_index"]
            matched = [p["point_id"] for p in capture_manifest.get("points", [])
                       if any(f["evidence_id"] in p["selected_frame_ids"] and f["render_index"] == render
                              for f in capture_manifest.get("frames", []))]
            if not matched:
                declared = {p["point_id"] for p in capture_manifest.get("points", [])}
                candidates = [f"render_{render}"] + [f"render_{render}.offpoint_{i}" for i in range(len(declared) + 1)]
                matched = [next(pid for pid in candidates if pid not in declared)]
            for pid in matched:
                key = (row["check_id"], pid)
                reason = row["reason"] + ": " + ",".join(row["mismatched_fields"])
                if key not in native_findings:
                    native_findings[key] = {"check_id": key[0], "point_id": pid, "verdict": "FAIL",
                        "evidence_ids": [], "tick": row["tick"],
                        "reproduce_argv": reproduce_argv if config and selftest is None and config.checklist["checks"] else [],
                        "confidence": None, "deterministic": None, "judge": None,
                        "disagreement": False, "reason": reason}
                finding = native_findings[key]
                finding["evidence_ids"] = sorted(set(finding["evidence_ids"]) | set(row["evidence_ids"]))
                finding["tick"] = min(finding["tick"], row["tick"])
                if reason not in finding["reason"]:
                    finding["reason"] += "; " + reason
        findings.extend(native_findings.values())
        checks_evaluated += len(native_findings)
        expected_count += len(native_findings)
        fails_count += len(native_findings)
    if selftest:
        for message in selftest.get("errors", []):
            errors_list.append({"code": "live_execution_error", "stage": "selftest", "message": str(message)})
        if selftest.get("execution_rc") == 2 and not selftest.get("errors"):
            errors_list.append({"code": "live_execution_error", "stage": "selftest", "message": "live backend returned ERROR"})
        if selftest.get("mode") == "live" and (selftest.get("execution_rc") == 3 or not selftest.get("qualified")):
            incomplete_list.append("live calibration coverage is not qualified")

    # Sort findings strictly by (check_id, point_id)
    findings.sort(key=lambda f: (f["check_id"], f["point_id"]))

    # Exit precedence: ERROR (2) > FAIL (1) > INCOMPLETE (3) > PASS (0)
    has_error_stage = any(s.get("status") == "ERROR" for s in stages_list)
    has_incomplete_stage = any(s.get("status") == "INCOMPLETE" for s in stages_list)
    selftest_survivor = False
    if selftest:
        checks_evaluated = selftest.get("checks", 0)
        expected_count = checks_evaluated
        fails_count = selftest.get("fails", 0)
        if selftest.get("execution_rc") == 1 or selftest.get("status") in ("SURVIVED", "FAIL") or selftest.get("fails", 0) > 0:
            selftest_survivor = True

    if errors_list or has_error_stage:
        rc = 2
        verdict = "ERROR"
        ok = False
    elif fails_count > 0 or selftest_survivor:
        rc = 1
        verdict = "FAIL"
        ok = False
    elif checks_evaluated == 0 or expected_count > checks_evaluated or cannot_see_count > 0 or incomplete_list or has_incomplete_stage:
        rc = 3
        verdict = "INCOMPLETE"
        ok = False
    else:
        rc = 0
        verdict = "PASS"
        ok = True

    # qualified: true only for a complete run with no errors and every required finding PASS under render scope
    qualified = (
        scope == "render"
        and rc == 0
        and ok
        and expected_count > 0
        and checks_evaluated == expected_count
        and fails_count == 0
        and cannot_see_count == 0
        and not errors_list
        and not incomplete_list
    )

    if selftest is not None and scope == "live-calibration":
        qualified = rc == 0 and selftest.get("qualified") is True

    summary = {
        "checks": checks_evaluated,
        "fails": fails_count,
        "cannot_see": cannot_see_count,
        "expected": expected_count,
    }

    full_metrics = default_metrics()
    if metrics:
        full_metrics.update(metrics)

    # Bindings exclude receipt.json to prevent hash cycle
    safe_bindings: list[dict[str, str]] = []
    if bindings:
        for b in bindings:
            if b.get("path") not in ("receipt.json", "./receipt.json"):
                safe_bindings.append(b)

    report: dict[str, Any] = {
        "schema": "vqa.report/v1",
        "scope": scope,
        "command": command,
        "run_id": run_id or (config.run_id if config else None),
        "ok": ok,
        "rc": rc,
        "verdict": verdict,
        "qualified": qualified,
        "summary": summary,
        "findings": findings,
        "advisory_findings": [f for f in findings if f.get("advisory") is True],
        "stages": stages_list,
        "errors": errors_list,
        "incomplete": incomplete_list,
        "bindings": safe_bindings,
        "metrics": full_metrics,
        "selftest": selftest,
    }

    contracts.validate_schema(report, "report")
    return report


# @anchor:bv.report.emit
def emit(report_dict: dict[str, Any], run_dir_or_none: Path | str | None = None) -> int:
    """Emit compact canonical JSON to stdout, machine line to stderr, and write report.json."""
    if run_dir_or_none is not None:
        try:
            r_dir = Path(run_dir_or_none).resolve()
            r_dir.mkdir(parents=True, exist_ok=True)
            report_file = r_dir / "report.json"
            atomic_write_json(report_file, report_dict)
        except OSError as exc:
            report_dict = dict(report_dict, rc=2, ok=False, verdict="ERROR", qualified=False,
                               errors=report_dict.get("errors", []) + [{"code": "filesystem_error", "stage": "report", "message": str(exc)}])

    # Canonical compact report JSON to stdout
    compact_stdout = contracts.canonical_json(report_dict)
    sys.stdout.write(compact_stdout)
    sys.stdout.flush()

    # Machine line to stderr
    summary = report_dict.get("summary", {})
    checks = summary.get("checks", 0)
    fails = summary.get("fails", 0)
    sys.stderr.write(f"HB_ORACLE checks={checks} fails={fails}\n")
    sys.stderr.flush()

    return int(report_dict.get("rc", 2))
