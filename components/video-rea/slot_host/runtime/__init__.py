# SPDX-License-Identifier: AGPL-3.0-only
"""REA slot host runtime: external executable binding and supervision."""

from .binding import Binding, BindingRejected, bind_executable, binding_conformance_identity, sha256_of_file, verify_binding, verify_conformance_for_binding
from . import process_lifecycle
from . import result_custody
from .executable_custody import ExecutableCustodyError, build_launch_argv, build_typed_tail, filtered_env, hash_verified_executable, open_launch_snapshot, stage_verified_executable
from .process_lifecycle import close_pipes, collect_output, create_run_scratch, create_stage_dir, destroy_run_scratch, destroy_stage_dir, effective_deadline, kill_only, safe_kill, terminate_only
from .result_custody import OMITTED_WITNESS, build_slot_result, decode_output, exchange_doc_valid, exchange_identity_mismatch, extract_exchange_meta, extract_observed_pid, host_result_sha256, output_sha256, producer_binding_verified, resolve_producer_origin, strict_single_json_object
from .conformance_authority import ConformanceDecision, ConformanceLiveAuthority, ConformanceReceipt, ReceiptStore, evaluate_default_on, is_live_enabled, create_fixture_receipt_for_binding, main_enrolment_available, tools_retention_available
from .live_park import LIVE_EXECUTION_PARKED, DenyLiveAuthority, LiveAuthority, authorize_live, make_conformance_authority
from .receipts import build_attempt_receipt, evidence_accepted, reconcile_run, receipt_digest
from . import evidence_projection
from .runner import execute, run_bound
from . import tools

__all__ = (
    "Binding",
    "BindingRejected",
    "ExecutableCustodyError",
    "build_launch_argv",
    "build_typed_tail",
    "filtered_env",
    "hash_verified_executable",
    "open_launch_snapshot",
    "stage_verified_executable",
    "process_lifecycle",
    "result_custody",
    "close_pipes",
    "collect_output",
    "create_run_scratch",
    "create_stage_dir",
    "destroy_run_scratch",
    "destroy_stage_dir",
    "effective_deadline",
    "kill_only",
    "safe_kill",
    "terminate_only",
    "OMITTED_WITNESS",
    "build_slot_result",
    "decode_output",
    "exchange_doc_valid",
    "exchange_identity_mismatch",
    "extract_exchange_meta",
    "extract_observed_pid",
    "host_result_sha256",
    "output_sha256",
    "producer_binding_verified",
    "resolve_producer_origin",
    "strict_single_json_object",
    "bind_executable",
    "binding_conformance_identity",
    "verify_binding",
    "verify_conformance_for_binding",
    "sha256_of_file",
    "ConformanceDecision",
    "ConformanceLiveAuthority",
    "ConformanceReceipt",
    "ReceiptStore",
    "evaluate_default_on",
    "is_live_enabled",
    "create_fixture_receipt_for_binding",
    "main_enrolment_available",
    "tools_retention_available",
    "build_attempt_receipt",
    "evidence_accepted",
    "evidence_projection",
    "reconcile_run",
    "receipt_digest",
    "execute",
    "run_bound",
    "tools",
    "LIVE_EXECUTION_PARKED",
    "LiveAuthority",
    "DenyLiveAuthority",
    "authorize_live",
    "make_conformance_authority",
)
