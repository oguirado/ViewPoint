"""Recompute a complete positive qualification receipt from retained evidence."""

from pathlib import Path

from asset_factory import core, proc
from .harness import (
    LOG_NAMES, RECEIPT_SCHEMA, SUPERVISION_FIELDS, combined_checks, descriptor,
    infrastructure, namespace_evidence, output_evidence, read_json_evidence, regular, rss_bytes, trace_counts,
)
from .manifest import JSON_LIMIT, Pins, Refusal, SHA256, check_candidate, fields
from .offline import PROBE_MESSAGE


def retained_json(path):
    if not regular(path):
        raise Refusal(3, "receipt_evidence_unavailable")
    value = core.load_json(path, max_bytes=JSON_LIMIT)
    if path.read_bytes() != core.canonical_json(value).encode("utf-8"):
        raise Refusal(4, "receipt_noncanonical")
    return value


def matches(left, right):
    return core.canonical_json(left) == core.canonical_json(right)


def verify_run(root, run, role, receipt, limits, *, cuda_record=None, cuda=False):
    fields(run, " ".join(SUPERVISION_FIELDS) + " logs network_namespace mount_namespace "
           "network_attempts denied_connects containment_attempts rss_peak_bytes "
           "cuda_peak_allocated_bytes cuda_peak_reserved_bytes cuda_peak_source " +
           ("fetch_probe_passed" if role == "denied-fetch" else "outputs"),
           "invalid_receipt_run")
    folder = root / role
    if folder.is_symlink() or not folder.is_dir() or run["role"] != role:
        raise Refusal(4, "receipt_role_mismatch")
    logs = {}
    for name in sorted(LOG_NAMES):
        path = folder / name
        if not regular(path):
            raise Refusal(3, "receipt_log_unavailable")
        logs[name] = descriptor(path)
    if not matches(logs, run["logs"]):
        raise Refusal(4, "receipt_log_mismatch")
    summary = {key: run[key] for key in SUPERVISION_FIELDS}
    if not matches(retained_json(folder / "supervision.json"), summary):
        raise Refusal(4, "receipt_supervision_mismatch")

    net, mount = namespace_evidence(folder, receipt["caller_netns_inode"], receipt["caller_mountns_inode"])
    counts = trace_counts(folder / "network.trace")
    expected_counts = (1, 1, 0) if role == "denied-fetch" else (0, 0, 0)
    recorded_counts = [run["network_attempts"], run["denied_connects"], run["containment_attempts"]]
    if (net is None or mount is None or not matches(net, run["network_namespace"]) or
            not matches(mount, run["mount_namespace"]) or counts != expected_counts or
            not matches(list(counts), recorded_counts)):
        raise Refusal(4, "receipt_isolation_mismatch")
    if (type(run["returncode"]) is not int or run["returncode"] != 0 or
            run["cleanup_proven"] is not True or run["cleanup_errors"] != [] or
            run["timed_out"] is not False or type(run["wall_ns"]) is not int or run["wall_ns"] <= 0 or
            run["cgroup_peak_source"] not in ("systemd_job", "pidns_test_scope")):
        raise Refusal(4, "receipt_supervision_mismatch")

    recomputed = dict(run)
    issues = combined_checks(recomputed, run["cgroup_peak_bytes"], rss_bytes(folder / "rss.kib"),
                            limits, cuda=cuda, cuda_record=cuda_record)
    if issues or not matches(recomputed, run):
        raise Refusal(4, "receipt_measurement_mismatch")
    if role == "denied-fetch" and (
        run["fetch_probe_passed"] is not True or
        (folder / "stdout").read_bytes() != (PROBE_MESSAGE + "\n").encode()
    ):
        raise Refusal(4, "receipt_fetch_probe_mismatch")


def verify_receipt(evidence_dir, *, expected_sha256):
    """Return the verified positive receipt, or raise Refusal on incomplete or changed evidence.

    The caller must supply the independently retained receipt digest.
    """
    if type(expected_sha256) is not str or SHA256.fullmatch(expected_sha256) is None:
        raise Refusal(4, "receipt_digest_mismatch")
    root = Path(evidence_dir)
    if root.is_symlink() or not root.is_dir():
        raise Refusal(3, "receipt_evidence_unavailable")
    try:
        path = root / "qualification.json"
        receipt = retained_json(path)
        if core.sha256_json(receipt) != expected_sha256:
            raise Refusal(4, "receipt_digest_mismatch")
        fields(receipt, "schema candidate_sha256 provider review_required caller_netns_inode "
               "caller_mountns_inode runs timeout_s memory_limits infrastructure rc status verdict "
               "reason_codes", "invalid_qualification_receipt")
        if (receipt["schema"] != RECEIPT_SCHEMA or type(receipt["rc"]) is not int or
                receipt["rc"] != 0 or receipt["status"] != core.STATUS[0] or
                receipt["verdict"] != core.VERDICT[0] or receipt["reason_codes"] != [] or
                receipt["review_required"] is not True or type(receipt["runs"]) is not list or
                len(receipt["runs"]) != 2):
            raise Refusal(4, "qualification_pass_unproven")
        if any(type(receipt[key]) is not int or receipt[key] <= 0
               for key in ("caller_netns_inode", "caller_mountns_inode", "timeout_s")):
            raise Refusal(2, "invalid_qualification_receipt")
        fields(receipt["memory_limits"], "high maximum swap", "invalid_receipt_limits")
        if receipt["timeout_s"] > 86400 or any(type(value) is not int for value in receipt["memory_limits"].values()):
            raise Refusal(2, "invalid_receipt_limits")
        limits = proc.MemoryLimits(**receipt["memory_limits"])

        candidate = retained_json(root / "candidate.json")
        pins = Pins()
        _, _, output_schema, _ = check_candidate(candidate, pins)
        if (core.sha256_json(candidate) != receipt["candidate_sha256"] or
                candidate["provider"] != receipt["provider"]):
            raise Refusal(4, "receipt_candidate_mismatch")
        _, trusted_infrastructure = infrastructure(pins)
        if trusted_infrastructure != receipt["infrastructure"]:
            raise Refusal(4, "receipt_infrastructure_mismatch")

        control, positive = receipt["runs"]
        verify_run(root, control, "denied-fetch", receipt, limits)
        outputs, valid = output_evidence(root / "output", candidate["outputs"], output_schema)
        if not valid or type(positive) is not dict or not matches(outputs, positive.get("outputs")):
            raise Refusal(4, "receipt_output_mismatch")
        cuda = candidate["runtime"]["cuda"]
        cuda_record = read_json_evidence(root / "output" / "cuda-peaks.json") if cuda else None
        verify_run(root, positive, "candidate", receipt, limits, cuda=cuda, cuda_record=cuda_record)
        pins.verify_unchanged()
        return receipt
    except Refusal:
        raise
    except (core.ContractError, OSError, TypeError, ValueError) as exc:
        raise Refusal(3, "receipt_evidence_unavailable") from exc
