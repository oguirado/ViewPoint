"""
Muse Source Contract Vectors and Verification Suite.

Offline contract tests for the task-source to Muse adapter boundary.
Uses only merged protocol specifications and a local in-memory/socket fake port.
The reserved musesource package is unmerged and remains AUSENTE.
"""

import json
import os
import socket
import struct
import tempfile
import pytest

# Numeric protocol constants CITED from src/w8muse/protocol.go and W8_WORKER_BANK.md
MAX_HEADER = 1 << 20      # 1,048,576 bytes (1 MiB)
MAX_BUNDLE = 512 << 20    # 536,870,912 bytes (512 MiB)
MAX_PATCH = 1 << 20       # 1,048,576 bytes (1 MiB)

# Exit code constants CITED from repository contract
EXIT_OK = 0
EXIT_INPUT = 2
EXIT_EXEC = 3
EXIT_UNAVAILABLE = 4
EXIT_GATE_CONTAINMENT = 5
EXIT_INDETERMINATE = 6     # EN_DUDA
EXIT_OPERATOR = 7

LIVE_ADAPTER_PARITY = "AUSENTE"


class MuseAdapterPort:
    """
    Local fakeable in-memory port defining the consumer contract for the task-source
    to Muse adapter boundary.
    """

    def __init__(self, registered_tasks=None, closed_seat=False):
        # registered_tasks: dict of task_id -> task_meta
        self.registered_tasks = registered_tasks or {}
        self.closed_seat = closed_seat
        self.submitted_handles = set()

    def parse_queue_line(self, line: str):
        """
        Queue input has exactly: id, absolute clean brief path, output path separated by TAB.
        """
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) != 3:
            raise ValueError("Queue line must have exactly 3 TAB-separated fields")

        task_id, brief_path, output_path = parts

        if not task_id or "\t" in task_id or "\n" in task_id:
            raise ValueError("Invalid task ID")

        # Absolute clean brief path
        if not brief_path.startswith("/") or ".." in brief_path.split("/"):
            raise ValueError("Brief path must be an absolute clean path without traversal")

        # Absolute clean output path
        if not output_path.startswith("/") or ".." in output_path.split("/"):
            raise ValueError("Output path must be an absolute clean path without traversal")

        return {
            "task_id": task_id,
            "brief_path": brief_path,
            "output_path": output_path,
        }

    def parse_queue_input(self, text: str, max_count=100, max_bytes=1 << 20):
        """
        Parses queue input with bounded count, bounded bytes, and unique registered ids.
        """
        raw_bytes = text.encode("utf-8")
        if len(raw_bytes) > max_bytes:
            return {"exit_code": EXIT_INPUT, "reason": "oversized_queue_bytes"}

        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) > max_count:
            return {"exit_code": EXIT_INPUT, "reason": "oversized_queue_count"}

        items = []
        seen_ids = set()

        for line in lines:
            try:
                item = self.parse_queue_line(line)
            except ValueError as e:
                return {"exit_code": EXIT_INPUT, "reason": str(e)}

            tid = item["task_id"]
            if tid in seen_ids:
                return {"exit_code": EXIT_INPUT, "reason": "duplicate_task_id"}
            seen_ids.add(tid)

            if tid not in self.registered_tasks:
                return {"exit_code": EXIT_INPUT, "reason": "unregistered_task"}

            if self.registered_tasks[tid].get("wave_open", False):
                return {"exit_code": EXIT_INPUT, "reason": "open_wave"}

            items.append(item)

        return {"exit_code": EXIT_OK, "items": items}

    def validate_brief(self, brief_content: str):
        """
        A generated brief cannot select a shell, grant authority, bypass the seat,
        or request inference inside a guest.
        """
        # Bounded brief size check
        if len(brief_content.encode("utf-8")) > MAX_HEADER:
            return {"exit_code": EXIT_INPUT, "reason": "oversized_brief"}

        forbidden_patterns = [
            "/bin/sh", "/bin/bash", "sudo", "chmod +s", "bypass_seat",
            "inference_in_guest", "model_chosen_argv", "eval("
        ]
        for pattern in forbidden_patterns:
            if pattern in brief_content:
                return {"exit_code": EXIT_GATE_CONTAINMENT, "reason": f"forbidden_brief_directive:{pattern}"}

        return {"exit_code": EXIT_OK}

    def process_item_output(self, raw_json_bytes: bytes):
        """
        Wave item output uses only task_id, attempt, objective, optional patch_path,
        fits MaxHeader, and disallows unknown fields.
        """
        if len(raw_json_bytes) == 0 or len(raw_json_bytes) > MAX_HEADER:
            return {"exit_code": EXIT_INPUT, "reason": "header_cap_exceeded"}

        try:
            data = json.loads(raw_json_bytes.decode("utf-8"))
        except Exception:
            return {"exit_code": EXIT_INPUT, "reason": "invalid_json"}

        if not isinstance(data, dict):
            return {"exit_code": EXIT_INPUT, "reason": "item_not_object"}

        allowed_keys = {"task_id", "attempt", "objective", "patch_path"}
        for k in data.keys():
            if k not in allowed_keys:
                return {"exit_code": EXIT_INPUT, "reason": f"unknown_field:{k}"}

        for required_key in ("task_id", "attempt", "objective"):
            if required_key not in data or not isinstance(data[required_key], str):
                return {"exit_code": EXIT_INPUT, "reason": f"missing_required_key:{required_key}"}

        return {"exit_code": EXIT_OK, "item": data}

    def submit_to_seat(self, handle: str, payload: dict):
        """
        Submits request to seat. A closed/unavailable seat yields EXIT_INDETERMINATE (6) / EN_DUDA.
        A second submit after uncertainty or with duplicate handle is refused without automatic retry.
        """
        if self.closed_seat:
            return {"exit_code": EXIT_INDETERMINATE, "reason": "seat_lost", "retry_attempted": False}

        if handle in self.submitted_handles:
            return {"exit_code": EXIT_EXEC, "reason": "second_submit_refused_no_retry"}

        self.submitted_handles.add(handle)
        return {"exit_code": EXIT_OK, "handle": handle, "ready": True}


# ============================================================================
# TEST SUITE
# ============================================================================

def test_live_adapter_parity_is_ausente():
    """Report that live adapter parity remains AUSENTE until merged."""
    assert LIVE_ADAPTER_PARITY == "AUSENTE"


def test_exit_codes_cited_contract():
    """Verify exit code mapping contracts."""
    assert EXIT_OK == 0
    assert EXIT_INPUT == 2
    assert EXIT_EXEC == 3
    assert EXIT_UNAVAILABLE == 4
    assert EXIT_GATE_CONTAINMENT == 5
    assert EXIT_INDETERMINATE == 6
    assert EXIT_OPERATOR == 7
    # Exit code 1 is never success or refusal
    assert 1 not in (EXIT_OK, EXIT_INPUT, EXIT_EXEC, EXIT_UNAVAILABLE, EXIT_GATE_CONTAINMENT, EXIT_INDETERMINATE, EXIT_OPERATOR)


def test_queue_input_positive():
    """Positive test: Queue line parsing with valid TAB separation, clean paths, registered tasks."""
    port = MuseAdapterPort(registered_tasks={
        "task-a": {"wave_open": False},
        "task-b": {"wave_open": False},
    })
    queue_text = "task-a\t/workspace/briefs/task-a.txt\t/workspace/out/task-a.json\ntask-b\t/workspace/briefs/task-b.txt\t/workspace/out/task-b.json\n"
    res = port.parse_queue_input(queue_text)
    assert res["exit_code"] == EXIT_OK
    assert len(res["items"]) == 2
    assert res["items"][0]["task_id"] == "task-a"
    assert res["items"][1]["task_id"] == "task-b"


def test_must_fail_queue_extra_fields_or_bad_sep():
    """Must-FAIL: Queue line without exactly 3 TAB fields."""
    port = MuseAdapterPort(registered_tasks={"task-a": {}})
    res = port.parse_queue_input("task-a\t/workspace/briefs/a.txt\n")
    assert res["exit_code"] == EXIT_INPUT
    assert "3 TAB" in res["reason"]


def test_must_fail_queue_path_traversal():
    """Must-FAIL: Path traversal in brief or output path."""
    port = MuseAdapterPort(registered_tasks={"task-a": {}})
    # Brief traversal
    res = port.parse_queue_input("task-a\t/workspace/../briefs/a.txt\t/out/a.json\n")
    assert res["exit_code"] == EXIT_INPUT
    assert "traversal" in res["reason"]

    # Output traversal
    res = port.parse_queue_input("task-a\t/workspace/briefs/a.txt\t/out/../a.json\n")
    assert res["exit_code"] == EXIT_INPUT
    assert "traversal" in res["reason"]

    # Relative path
    res = port.parse_queue_input("task-a\trelative/brief.txt\t/out/a.json\n")
    assert res["exit_code"] == EXIT_INPUT


def test_must_fail_queue_duplicate_ids():
    """Must-FAIL: Duplicate task IDs in queue."""
    port = MuseAdapterPort(registered_tasks={"task-a": {}})
    queue = "task-a\t/briefs/1.txt\t/out/1.json\ntask-a\t/briefs/2.txt\t/out/2.json\n"
    res = port.parse_queue_input(queue)
    assert res["exit_code"] == EXIT_INPUT
    assert res["reason"] == "duplicate_task_id"


def test_must_fail_queue_unregistered_task():
    """Must-FAIL: Task ID not in registered tasks."""
    port = MuseAdapterPort(registered_tasks={"task-registered": {}})
    queue = "task-unknown\t/briefs/1.txt\t/out/1.json\n"
    res = port.parse_queue_input(queue)
    assert res["exit_code"] == EXIT_INPUT
    assert res["reason"] == "unregistered_task"


def test_must_fail_queue_open_wave():
    """Must-FAIL: Task in open wave state."""
    port = MuseAdapterPort(registered_tasks={"task-a": {"wave_open": True}})
    queue = "task-a\t/briefs/1.txt\t/out/1.json\n"
    res = port.parse_queue_input(queue)
    assert res["exit_code"] == EXIT_INPUT
    assert res["reason"] == "open_wave"


def test_must_fail_queue_oversized_count_or_bytes():
    """Must-FAIL: Oversized queue count or byte size."""
    port = MuseAdapterPort(registered_tasks={"task-a": {}})
    # Oversized bytes
    res = port.parse_queue_input("task-a\t/b.txt\t/o.json\n", max_bytes=10)
    assert res["exit_code"] == EXIT_INPUT
    assert res["reason"] == "oversized_queue_bytes"

    # Oversized count
    res = port.parse_queue_input("task-a\t/b.txt\t/o.json\n", max_count=0)
    assert res["exit_code"] == EXIT_INPUT
    assert res["reason"] == "oversized_queue_count"


def test_brief_safety_positive():
    """Positive test: Clean brief content."""
    port = MuseAdapterPort()
    brief = "Solve objective using provided codebase context."
    res = port.validate_brief(brief)
    assert res["exit_code"] == EXIT_OK


def test_must_fail_brief_forbidden_directives():
    """Must-FAIL: Brief choosing shell, granting authority, seat bypass, or guest inference."""
    port = MuseAdapterPort()
    forbidden = [
        "Use /bin/sh to execute",
        "Run /bin/bash for setup",
        "Execute sudo apt-get install",
        "Call bypass_seat directly",
        "Enable inference_in_guest mode",
        "Pass model_chosen_argv to execution",
    ]
    for text in forbidden:
        res = port.validate_brief(text)
        assert res["exit_code"] == EXIT_GATE_CONTAINMENT
        assert "forbidden_brief_directive" in res["reason"]


def test_must_fail_brief_oversized():
    """Must-FAIL: Oversized brief content exceeding MaxHeader limit."""
    port = MuseAdapterPort()
    huge_brief = "a" * (MAX_HEADER + 1)
    res = port.validate_brief(huge_brief)
    assert res["exit_code"] == EXIT_INPUT
    assert res["reason"] == "oversized_brief"


def test_item_output_positive():
    """Positive test: Valid item output JSON with exact allowed fields."""
    port = MuseAdapterPort()
    valid_item = {
        "task_id": "os-f-w8-3-09-muse-source-contract",
        "attempt": "a1",
        "objective": "Offline contract test work",
        "patch_path": "/tmp/w8_patch.diff"
    }
    raw = json.dumps(valid_item).encode("utf-8")
    res = port.process_item_output(raw)
    assert res["exit_code"] == EXIT_OK
    assert res["item"]["task_id"] == "os-f-w8-3-09-muse-source-contract"
    assert res["item"]["attempt"] == "a1"


def test_must_fail_item_output_extra_fields():
    """Must-FAIL: Wave output item containing unknown extra fields."""
    port = MuseAdapterPort()
    item_extra = {
        "task_id": "os-f-w8-3-09-muse-source-contract",
        "attempt": "a1",
        "objective": "Offline contract test work",
        "extra_unknown_field": "disallowed"
    }
    raw = json.dumps(item_extra).encode("utf-8")
    res = port.process_item_output(raw)
    assert res["exit_code"] == EXIT_INPUT
    assert "unknown_field" in res["reason"]


def test_must_fail_item_output_oversized_header():
    """Must-FAIL: Wave output item exceeding MaxHeader limit."""
    port = MuseAdapterPort()
    huge_json = json.dumps({
        "task_id": "t1",
        "attempt": "a1",
        "objective": "x" * (MAX_HEADER + 10)
    }).encode("utf-8")
    res = port.process_item_output(huge_json)
    assert res["exit_code"] == EXIT_INPUT
    assert res["reason"] == "header_cap_exceeded"


def test_seat_closed_yields_en_duda_no_retry():
    """Test closed seat yields EN_DUDA (rc=6) and no automatic retry or fallback."""
    port = MuseAdapterPort(closed_seat=True)
    res = port.submit_to_seat("handle-1234", {"verb": "submit"})
    assert res["exit_code"] == EXIT_INDETERMINATE
    assert res["reason"] == "seat_lost"
    assert res["retry_attempted"] is False


def test_must_fail_second_submit_after_uncertainty():
    """Must-FAIL: Second submit for the same handle/attempt after uncertainty is refused."""
    port = MuseAdapterPort(closed_seat=False)
    res1 = port.submit_to_seat("handle-1234", {"verb": "submit"})
    assert res1["exit_code"] == EXIT_OK

    # Second submit attempt
    res2 = port.submit_to_seat("handle-1234", {"verb": "submit"})
    assert res2["exit_code"] == EXIT_EXEC
    assert res2["reason"] == "second_submit_refused_no_retry"


def test_af_unix_socket_contract():
    """
    Unit test binding an AF_UNIX socket:
    Must use a short temporary directory under /tmp with prefix w8,
    remove it in cleanup, and assert full socket path never exceeds 100 TARGET bytes.
    """
    tmp_dir = tempfile.TemporaryDirectory(prefix="w8", dir="/tmp")
    try:
        sock_path = os.path.join(tmp_dir.name, "s.sock")
        assert len(sock_path.encode("utf-8")) <= 100

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)

        client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client_sock.connect(sock_path)

        conn, _ = server_sock.accept()

        # Send uint32 length prefixed JSON frame
        msg = json.dumps({"verb": "health"}).encode("utf-8")
        header = struct.pack(">I", len(msg))
        client_sock.sendall(header + msg)

        received_header = conn.recv(4)
        msg_len = struct.unpack(">I", received_header)[0]
        received_msg = conn.recv(msg_len)

        data = json.loads(received_msg.decode("utf-8"))
        assert data["verb"] == "health"

        # Reply with seat response
        reply = json.dumps({"exit_code": 0, "ready": True}).encode("utf-8")
        reply_header = struct.pack(">I", len(reply))
        conn.sendall(reply_header + reply)

        res_header = client_sock.recv(4)
        res_len = struct.unpack(">I", res_header)[0]
        res_msg = client_sock.recv(res_len)
        res_data = json.loads(res_msg.decode("utf-8"))

        assert res_data["exit_code"] == 0
        assert res_data["ready"] is True

        conn.close()
        client_sock.close()
        server_sock.close()
    finally:
        tmp_dir.cleanup()
        assert not os.path.exists(tmp_dir.name)
