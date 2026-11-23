#!/usr/bin/env python3
"""Fake Gemini judge worker for blender_visual_qa offline testing and integration.

Emulates `newton run scr-obrero-gemini` by:
- Responding with attention answers matching expectations.json
- Inspecting active mutant state (via fault_receipt.json or VQA_MUTANT_ID)
- Failing the named expected checks for mutants, and passing clean runs
- Emitting valid vqa.judge-output/v1 records with token usage
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Fake Gemini Judge Worker")
    parser.add_argument("--encargo", required=True, help="Path to prompt/encargo file")
    parser.add_argument("--salida", required=True, help="Path to output worker.json")
    parser.add_argument("--modelo", default="gemini-3.8-flash")
    parser.add_argument("--esfuerzo", default="medium")
    parser.add_argument("--permiso", default="pleno")
    parser.add_argument("--eco", action="store_true", default=False)
    args, _ = parser.parse_known_args()

    prompt_path = Path(args.encargo).resolve()
    salida_path = Path(args.salida).resolve()

    attempt_dir = prompt_path.parent
    input_file = attempt_dir / "input.json"
    exp_file = attempt_dir / "expectations.json"

    if not input_file.is_file() or not exp_file.is_file():
        sys.stderr.write(f"Missing input.json or expectations.json in {attempt_dir}\n")
        return 2

    input_obj = json.loads(input_file.read_text(encoding="utf-8"))
    exp_obj = json.loads(exp_file.read_text(encoding="utf-8"))

    run_id = input_obj.get("run_id", "unknown_run")
    batch_id = input_obj.get("batch_id", "b0")
    attempt_id = input_obj.get("attempt_id", "a1")
    answers = exp_obj.get("attention", {})

    # Detect active mutant and expected failing checks
    run_dir = attempt_dir.parent.parent
    fault_receipt_path = run_dir / "capture" / "fault_receipt.json"

    active_mutant: str | None = None
    if fault_receipt_path.is_file():
        try:
            rcpt = json.loads(fault_receipt_path.read_text(encoding="utf-8"))
            active_mutant = rcpt.get("mutant_id")
        except Exception:
            pass

    if not active_mutant:
        active_mutant = os.environ.get("VQA_MUTANT_ID")

    expected_failing_checks: set[str] = set()

    # Search for mutant manifest in repo adapters
    repo_root = Path(__file__).resolve().parent.parent
    manifest_candidates = [
        run_dir / "mutant-manifest.json",
        repo_root / "adapters" / "example_scene" / "mutant-manifest.json",
    ]
    for mc in manifest_candidates:
        if mc.is_file():
            try:
                m_data = json.loads(mc.read_text(encoding="utf-8"))
                for m in m_data.get("mutants", []):
                    if m.get("id") == active_mutant:
                        expected_failing_checks.update(m.get("expected_check_ids", []))
            except Exception:
                pass

    # Built-in fallback map if manifest lookup did not find check
    fallback_map = {}
    if active_mutant and active_mutant in fallback_map:
        expected_failing_checks.update(fallback_map[active_mutant])

    # Evaluate checks
    media_by_id = {m["evidence_id"]: m for m in input_obj.get("media", [])}
    results_list: list[dict[str, Any]] = []
    for ch in input_obj.get("checks", []):
        c_id = ch["check_id"]
        e_ids = ch.get("evidence_ids", [])
        primary_eid = e_ids[0] if e_ids else "evidence:0"

        if c_id in expected_failing_checks:
            verdict = "FAIL"
            confidence = 0.95
            reason = f"Planted defect observed under mutant {active_mutant}"
            obs = [
                {
                    "evidence_id": primary_eid,
                    "bbox_norm": [0.1, 0.1, 0.5, 0.5],
                    "time_ms": None,
                    "text": f"Defect detected for check {c_id}",
                }
            ]
        else:
            verdict = "PASS"
            confidence = 0.95
            reason = f"Normal render presentation verified for {c_id}"
            obs = [
                {
                    "evidence_id": primary_eid,
                    "bbox_norm": [0.1, 0.1, 0.5, 0.5],
                    "time_ms": None,
                    "text": f"Verified clean check {c_id}",
                }
            ]

        if ch.get("evidence_requirement") == "ocr" and verdict == "PASS":
            obs = [{"kind": "ocr", "evidence_id": eid,
                    "bbox_norm": [0.1, 0.1, 0.5, 0.5],
                    "time_ms": 0 if media_by_id.get(eid, {}).get("kind") == "mp4" else None,
                    "text": f"Visible label for {c_id} in {eid}"} for eid in e_ids]
        else:
            for observation in obs:
                observation["kind"] = "ocr"
                if media_by_id.get(observation["evidence_id"], {}).get("kind") == "mp4":
                    observation["time_ms"] = 0

        results_list.append({
            "check_id": c_id,
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
            "observations": obs,
        })

    all_media = [m["evidence_id"] for m in input_obj.get("media", []) if "evidence_id" in m]

    judge_res = {
        "schema": "vqa.judge-output/v1",
        "run_id": run_id,
        "batch_id": batch_id,
        "attempt_id": attempt_id,
        "model_id": args.modelo,
        "input_sha256": input_obj.get("checklist_sha256", "0" * 64),
        "prompt_sha256": "0" * 64,
        "media_seen": all_media,
        "attention_answers": answers,
        "results": results_list,
    }

    worker_payload = [
        {"type": "system", "model": args.modelo, "effort": args.esfuerzo},
        {
            "type": "result",
            "fuente": "gemini-worker",
            "subtype": "success",
            "is_error": False,
            "modelo": args.modelo,
            "result": json.dumps(judge_res),
            "usage": {"input_tokens": 1200, "output_tokens": 150},
            "usage_raw": {"input_tokens": 1200, "output_tokens": 150},
            "usage_presente": {"input": True, "output": True},
        },
    ]

    salida_path.parent.mkdir(parents=True, exist_ok=True)
    salida_path.write_text(json.dumps(worker_payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
