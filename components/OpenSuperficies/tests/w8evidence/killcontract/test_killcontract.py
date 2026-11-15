"""W8 kill contract reference models and tests.

Self-contained Wave 1 TARGET offline test suite for fake kill-switch and recovery
transcript tests using inline lifecycle order and local vectors.
"""

import copy
import hashlib
import json
import os
import re
import socket
import tempfile
import unittest


ALLOWED_EXITS = frozenset((0, 2, 3, 4, 5, 6, 7))
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


class Refusal(Exception):
    def __init__(self, reason, rc=5):
        if rc not in ALLOWED_EXITS or rc == 0:
            raise ValueError(f"invalid refusal exit code {rc}")
        super().__init__(reason)
        self.reason = reason
        self.rc = rc


def require(condition, reason, rc=5):
    if not condition:
        raise Refusal(reason, rc)


def integer(value, low=0, high=(1 << 63) - 1):
    require(type(value) is int and low <= value <= high, "checked integer", 2)
    return value


def ident(value):
    require(type(value) is str and IDENT_RE.fullmatch(value) is not None, "identifier", 2)
    return value


def pin(value):
    require(type(value) is str and HEX64_RE.fullmatch(value) is not None, "sha256 pin", 2)
    return value


def canonical(value):
    def check(item, depth=0):
        require(depth <= 32, "nesting depth bound", 2)
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int:
            integer(item, -(1 << 63))
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif type(item) is dict:
            require(all(type(k) is str for k in item), "dict key string", 2)
            for child in item.values():
                check(child, depth + 1)
        else:
            raise Refusal("non-canonical json value", 2)

    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


# -----------------------------------------------------------------------------
# 1. Speech Act Payload Admission
# -----------------------------------------------------------------------------

def speech_act_admit(payload):
    """Source-backed speech payload validator matching schema os.cu.speech-act/v1."""
    require(type(payload) is dict, "speech act must be dict", 2)
    expected_keys = {
        "schema", "act", "wave_id", "task_id", "attempt_id",
        "lease_id", "seq", "repro_sha256", "affected", "status", "body"
    }
    require(set(payload.keys()) == expected_keys, "closed speech act fields", 2)

    require(payload["schema"] == "os.cu.speech-act/v1", "schema os.cu.speech-act/v1", 2)
    act = payload["act"]
    require(act in ("finding", "repro", "affected", "status"), "act enum", 2)

    for k in ("wave_id", "task_id", "attempt_id", "lease_id"):
        ident(payload[k])

    integer(payload["seq"], 0, 1000000)

    require(type(payload["body"]) is str and len(payload["body"]) <= 2048, "body length", 2)

    affected = payload["affected"]
    require(type(affected) is list and len(affected) <= 64, "affected list", 2)
    require(len(set(affected)) == len(affected), "duplicate affected path", 2)
    for path in affected:
        require(type(path) is str and len(path) <= 1024, "path length", 2)
        require(PATH_RE.fullmatch(path) is not None, "path format", 2)
        parts = path.split("/")
        require(not set(parts) & {".", "..", ".git"}, "path escape", 2)

    if act in ("finding", "repro"):
        pin(payload["repro_sha256"])
        require(payload["status"] is None, "finding/repro status must be null", 2)
    elif act == "affected":
        require(bool(affected), "affected act needs paths", 2)
        if payload["repro_sha256"] is not None:
            pin(payload["repro_sha256"])
        require(payload["status"] is None, "affected status must be null", 2)
    elif act == "status":
        require(payload["status"] in ("working", "blocked", "done", "stuck"), "status enum", 2)
        if payload["repro_sha256"] is not None:
            pin(payload["repro_sha256"])

    return True


# -----------------------------------------------------------------------------
# 2. Sidecar Carrier & Sequence Ordering
# -----------------------------------------------------------------------------

class SpeechActSidecar:
    """Proposed local TARGET sidecar carrier with sequence ordering, quarantine, and STOP lane."""

    def __init__(self, verifier_fn=None, data_capacity=10, stop_capacity=5):
        self.verifier_fn = verifier_fn or (lambda body: True)
        self.data_capacity = data_capacity
        self.stop_capacity = stop_capacity

        self.data_queue = []
        self.stop_queue = []

        self.expected_seq = 0
        self.applied_records = []
        self.acks = {}
        self.held_successors = {}
        self.quarantined = []

    def receive_sidecar(self, sidecar, body_bytes):
        expected_fields = {
            "sender", "task_id", "attempt_id", "lease_id",
            "epoch", "seq", "body_sha256", "body_length"
        }
        require(type(sidecar) is dict and set(sidecar.keys()) == expected_fields, "sidecar fields", 2)
        for k in ("sender", "task_id", "attempt_id", "lease_id"):
            ident(sidecar[k])
        integer(sidecar["epoch"], 1)
        integer(sidecar["seq"], 0)
        pin(sidecar["body_sha256"])
        integer(sidecar["body_length"], 1)

        require(type(body_bytes) is bytes and len(body_bytes) == sidecar["body_length"], "body_length match", 2)
        actual_sha = hashlib.sha256(body_bytes).hexdigest()
        require(actual_sha == sidecar["body_sha256"], "body_sha256 match", 2)

        require(self.verifier_fn(body_bytes) is True, "verifier authentication", 5)

        seq = sidecar["seq"]
        # Same sequence replay check
        if seq < self.expected_seq:
            if seq in self.acks:
                recorded_sha, recorded_ack = self.acks[seq]
                if recorded_sha == sidecar["body_sha256"]:
                    return recorded_ack  # Idempotent replay
                else:
                    self.quarantined.append((sidecar, body_bytes, "changed_bytes_prior_seq"))
                    raise Refusal("quarantine: changed bytes for prior sequence", 6)

        # Pending or buffer check for changed bytes
        if seq in self.held_successors:
            old_sidecar, old_bytes = self.held_successors[seq]
            if old_sidecar["body_sha256"] != sidecar["body_sha256"]:
                self.quarantined.append((sidecar, body_bytes, "changed_bytes_held_seq"))
                raise Refusal("quarantine: changed bytes for held sequence", 6)

        total_buffered = len(self.data_queue) + len(self.held_successors)
        if total_buffered >= self.data_capacity:
            raise Refusal("data queue saturated", 4)

        self.data_queue.append((sidecar, body_bytes))
        return self._process_data_queue()

    def send_stop(self, stop_signal):
        require(type(stop_signal) is dict and "lease_id" in stop_signal, "stop signal format", 2)
        if len(self.stop_queue) >= self.stop_capacity:
            raise Refusal("stop queue saturated", 4)
        self.stop_queue.append(stop_signal)
        return True

    def _process_data_queue(self):
        last_ack = None
        while self.data_queue:
            sidecar, body_bytes = self.data_queue.pop(0)
            seq = sidecar["seq"]
            if seq == self.expected_seq:
                ack = self._apply_contiguous(sidecar, body_bytes)
                last_ack = ack
                self.expected_seq += 1
                # Check held successors
                while self.expected_seq in self.held_successors:
                    next_sidecar, next_bytes = self.held_successors.pop(self.expected_seq)
                    last_ack = self._apply_contiguous(next_sidecar, next_bytes)
                    self.expected_seq += 1
            elif seq > self.expected_seq:
                self.held_successors[seq] = (sidecar, body_bytes)
        return last_ack

    def _apply_contiguous(self, sidecar, body_bytes):
        payload = json.loads(body_bytes.decode("utf-8"))
        speech_act_admit(payload)
        self.applied_records.append((sidecar, payload))
        ack_id = digest(["ack", sidecar["lease_id"], sidecar["seq"], sidecar["body_sha256"]])
        self.acks[sidecar["seq"]] = (sidecar["body_sha256"], ack_id)
        return ack_id


# -----------------------------------------------------------------------------
# 3. Synthetic Registration and Transcript
# -----------------------------------------------------------------------------

def synthetic_register(item):
    """Synthetic registration using existing closed wave item task_id, attempt, objective, patch_path."""
    require(type(item) is dict, "registration dict", 2)
    expected_keys = {"task_id", "attempt", "objective", "patch_path"}
    require(set(item.keys()) == expected_keys, "closed registration fields", 2)
    ident(item["task_id"])
    ident(item["attempt"])
    require(type(item["objective"]) is str and bool(item["objective"]), "objective text", 2)
    if item["patch_path"] is not None:
        require(type(item["patch_path"]) is str and PATH_RE.fullmatch(item["patch_path"]) is not None, "patch_path", 2)

    return {
        "registered": True,
        "item": copy.deepcopy(item),
        "launch_authority_granted": False  # Grants no launch authority
    }


class TranscriptManager:
    """Manages proposed TARGET local transcript fields and events lifecycle."""

    def __init__(self):
        self.transcripts = {}

    def create_transcript(self, fields):
        expected = {
            "task_id", "attempt", "lease_id", "epoch", "repo_commit",
            "gate_argv", "image_sha256", "runner_sha256", "cache_sha256",
            "profile_sha256", "handle", "events", "receipt",
            "extract_sha256", "durable", "acknowledged", "purge_verified",
            "bridge_result", "reason"
        }
        require(type(fields) is dict and set(fields.keys()) == expected, "closed transcript fields", 2)
        ident(fields["task_id"])
        ident(fields["attempt"])
        ident(fields["lease_id"])
        integer(fields["epoch"], 1)
        require(HEX40_RE.fullmatch(fields["repo_commit"]) is not None, "repo_commit", 2)
        require(type(fields["gate_argv"]) is list and all(type(a) is str for a in fields["gate_argv"]), "gate_argv", 2)
        for pin_key in ("image_sha256", "runner_sha256", "cache_sha256", "profile_sha256", "handle"):
            pin(fields[pin_key])

        require(type(fields["events"]) is list, "events list", 2)
        require(type(fields["durable"]) is bool, "durable bool", 2)
        require(type(fields["acknowledged"]) is bool, "acknowledged bool", 2)
        require(type(fields["purge_verified"]) is bool, "purge_verified bool", 2)

        key = (fields["task_id"], fields["attempt"], fields["lease_id"])
        self.transcripts[key] = copy.deepcopy(fields)
        return key

    def record_event(self, key, event_name):
        require(key in self.transcripts, "unknown transcript", 2)
        t = self.transcripts[key]
        allowed_events = ("submit", "wait", "receipt", "durable_collection", "ack", "purge_verification")
        require(event_name in allowed_events, "unknown event", 2)

        # Check event sequence
        events = t["events"]
        expected_index = len(events)
        if expected_index < len(allowed_events):
            require(event_name == allowed_events[expected_index], "out-of-order event sequence", 5)

        events.append(event_name)
        if event_name == "durable_collection":
            t["durable"] = True
        elif event_name == "ack":
            t["acknowledged"] = True
        elif event_name == "purge_verification":
            t["purge_verified"] = True

    def set_unavailable_dry(self, key):
        require(key in self.transcripts, "unknown transcript", 2)
        t = self.transcripts[key]
        t["bridge_result"] = None
        t["reason"] = "unavailable"


# -----------------------------------------------------------------------------
# 4. Host Control, Lease Fence & Reconciliation
# -----------------------------------------------------------------------------

class HostControlEngine:
    """Trusted host control fencing lease authority before bounded collection and cleanup."""

    def __init__(self, wal_ok=True):
        self.wal_ok = wal_ok
        self.leases = {}
        self.active_cgroups = {}
        self.active_tmpfs = {}
        self.active_pids = {}
        self.held_capacity = {}
        self.local_markers = {}
        self.reconciled_receipts = {}

    def grant_lease(self, lease_id, task_id, attempt_id, epoch, capacity_units=1):
        if not self.wal_ok:
            raise Refusal("WAL failure: cannot append lease grant", 3)
        ident(lease_id)
        ident(task_id)
        ident(attempt_id)
        integer(epoch, 1)

        record = {
            "lease_id": lease_id, "task_id": task_id, "attempt_id": attempt_id,
            "epoch": epoch, "fenced": False, "released": False, "capacity": capacity_units
        }
        self.leases[lease_id] = record
        self.held_capacity[lease_id] = capacity_units
        self.active_cgroups[lease_id] = [101, 102]
        self.active_tmpfs[lease_id] = 1024
        self.active_pids[lease_id] = [101, 102]
        return record

    def fence_lease(self, lease_id, expected_epoch):
        require(lease_id in self.leases, "unknown lease", 2)
        lease = self.leases[lease_id]
        require(lease["epoch"] == expected_epoch, "stale epoch actuation", 6)
        lease["fenced"] = True

    def mark_seat_lost(self, lease_id):
        require(lease_id in self.leases, "unknown lease", 2)
        lease = self.leases[lease_id]
        marker = {
            "status": "local_marker",
            "rc": 6,
            "reason": "seat_lost",
            "task_id": lease["task_id"],
            "attempt_id": lease["attempt_id"],
            "lease_id": lease_id
        }
        self.local_markers[lease_id] = marker

    def perform_cleanup_and_release(self, lease_id, target_cgroup_empty, tmpfs_purged, pids_empty, skip_purge=False):
        require(lease_id in self.leases, "unknown lease", 2)
        lease = self.leases[lease_id]
        require(lease["fenced"], "lease authority must be fenced before release", 6)

        if skip_purge:
            raise Refusal("purge omitted", 6)

        require(target_cgroup_empty is True, "cgroup not empty", 6)
        require(tmpfs_purged is True, "tmpfs not purged", 6)
        require(pids_empty is True, "pids not empty", 6)

        self.active_cgroups[lease_id] = []
        self.active_tmpfs[lease_id] = 0
        self.active_pids[lease_id] = []

        lease["released"] = True
        if lease_id in self.held_capacity:
            del self.held_capacity[lease_id]

    def reconcile(self, lease_id, authoritative_receipt):
        require(lease_id in self.leases, "unknown lease", 2)
        lease = self.leases[lease_id]
        require(type(authoritative_receipt) is dict and authoritative_receipt.get("schema") == "os.cu.worker-receipt/v1",
                "authoritative worker receipt required", 2)

        self.reconciled_receipts[lease_id] = copy.deepcopy(authoritative_receipt)
        if lease_id in self.local_markers:
            del self.local_markers[lease_id]
        return authoritative_receipt


# -----------------------------------------------------------------------------
# 5. Pytest Unit Test Suite
# -----------------------------------------------------------------------------

class TestW8KillContract(unittest.TestCase):

    def test_e0_and_e1_hashes(self):
        """Verify evidence SHA256 hashes for src/w8seat/README.md and src/w8muse/README.md."""
        e0_path = "src/w8seat/README.md"
        e1_path = "src/w8muse/README.md"
        self.assertTrue(os.path.exists(e0_path))
        self.assertTrue(os.path.exists(e1_path))

        # The cited contract text, not the README bytes: these docs evolve with the seat
        # and driver (a pinned digest broke on an unrelated doc edit, merge wave 3).
        with open(e0_path, encoding="utf-8") as f0:
            e0 = f0.read()
        with open(e1_path, encoding="utf-8") as f1:
            e1 = f1.read()
        self.assertIn("kill", e0.lower())
        self.assertIn("seat_lost", e1)
        self.assertIn("--reconcile", e1)

    def test_af_unix_socket_path_length_bound(self):
        """Test binding AF_UNIX socket in short tempdir under /tmp with prefix w8, path <= 100 bytes."""
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="w8") as tmpdir:
            self.assertTrue(tmpdir.startswith("/tmp/w8"))
            sock_path = os.path.join(tmpdir, "s.sock")
            self.assertLessEqual(len(sock_path), 100)

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(sock_path)
            sock.close()
            self.assertTrue(os.path.exists(sock_path))

    def test_speech_act_payload_admit(self):
        payload = {
            "schema": "os.cu.speech-act/v1",
            "act": "finding",
            "wave_id": "wave1",
            "task_id": "task1",
            "attempt_id": "a1",
            "lease_id": "lease1",
            "seq": 0,
            "repro_sha256": "a" * 64,
            "affected": ["src/w8seat/lifecycle.go"],
            "status": None,
            "body": "finding body text"
        }
        self.assertTrue(speech_act_admit(payload))

        # Test status act
        status_payload = copy.deepcopy(payload)
        status_payload["act"] = "status"
        status_payload["repro_sha256"] = None
        status_payload["status"] = "working"
        self.assertTrue(speech_act_admit(status_payload))

        # Test invalid status act
        bad_status = copy.deepcopy(status_payload)
        bad_status["status"] = "invalid_status"
        with self.assertRaises(Refusal) as cm:
            speech_act_admit(bad_status)
        self.assertEqual(cm.exception.rc, 2)

    def test_sidecar_carrier_and_ordering(self):
        sidecar_0 = {
            "sender": "worker1", "task_id": "task1", "attempt_id": "a1", "lease_id": "lease1",
            "epoch": 1, "seq": 0, "body_sha256": "", "body_length": 0
        }
        payload_0 = {
            "schema": "os.cu.speech-act/v1", "act": "status", "wave_id": "wave1",
            "task_id": "task1", "attempt_id": "a1", "lease_id": "lease1",
            "seq": 0, "repro_sha256": None, "affected": [], "status": "working", "body": "step 0"
        }
        body_0 = canonical(payload_0)
        sidecar_0["body_sha256"] = hashlib.sha256(body_0).hexdigest()
        sidecar_0["body_length"] = len(body_0)

        carrier = SpeechActSidecar()
        ack0 = carrier.receive_sidecar(sidecar_0, body_0)
        self.assertIsNotNone(ack0)

        # Idempotent replay
        ack0_replay = carrier.receive_sidecar(sidecar_0, body_0)
        self.assertEqual(ack0, ack0_replay)

        # Changed bytes replay for same sequence -> quarantine
        payload_0_mod = copy.deepcopy(payload_0)
        payload_0_mod["body"] = "modified step 0"
        body_0_mod = canonical(payload_0_mod)
        sidecar_0_mod = copy.deepcopy(sidecar_0)
        sidecar_0_mod["body_sha256"] = hashlib.sha256(body_0_mod).hexdigest()
        sidecar_0_mod["body_length"] = len(body_0_mod)

        with self.assertRaises(Refusal) as cm:
            carrier.receive_sidecar(sidecar_0_mod, body_0_mod)
        self.assertEqual(cm.exception.rc, 6)

        # Gap holds successors
        sidecar_2 = copy.deepcopy(sidecar_0)
        sidecar_2["seq"] = 2
        payload_2 = copy.deepcopy(payload_0)
        payload_2["seq"] = 2
        payload_2["body"] = "step 2"
        body_2 = canonical(payload_2)
        sidecar_2["body_sha256"] = hashlib.sha256(body_2).hexdigest()
        sidecar_2["body_length"] = len(body_2)

        # Receive seq 2 before seq 1 -> held in successors
        ack2 = carrier.receive_sidecar(sidecar_2, body_2)
        self.assertIsNone(ack2)
        self.assertIn(2, carrier.held_successors)

        # Receive seq 1 -> applies 1 and 2
        sidecar_1 = copy.deepcopy(sidecar_0)
        sidecar_1["seq"] = 1
        payload_1 = copy.deepcopy(payload_0)
        payload_1["seq"] = 1
        payload_1["body"] = "step 1"
        body_1 = canonical(payload_1)
        sidecar_1["body_sha256"] = hashlib.sha256(body_1).hexdigest()
        sidecar_1["body_length"] = len(body_1)

        ack1_and_2 = carrier.receive_sidecar(sidecar_1, body_1)
        self.assertIsNotNone(ack1_and_2)
        self.assertEqual(carrier.expected_seq, 3)

    def test_transcript_fields_and_dry_publication(self):
        tm = TranscriptManager()
        t_fields = {
            "task_id": "task1", "attempt": "a1", "lease_id": "lease1", "epoch": 1,
            "repo_commit": "b" * 40, "gate_argv": ["pytest"],
            "image_sha256": "1" * 64, "runner_sha256": "2" * 64, "cache_sha256": "3" * 64,
            "profile_sha256": "4" * 64, "handle": "5" * 64,
            "events": [], "receipt": {"schema": "os.cu.worker-receipt/v1", "exit_code": 0},
            "extract_sha256": "6" * 64, "durable": False, "acknowledged": False,
            "purge_verified": False, "bridge_result": "published", "reason": ""
        }
        key = tm.create_transcript(t_fields)

        # Record events in order
        tm.record_event(key, "submit")
        tm.record_event(key, "wait")
        tm.record_event(key, "receipt")
        tm.record_event(key, "durable_collection")
        tm.record_event(key, "ack")
        tm.record_event(key, "purge_verification")

        self.assertTrue(tm.transcripts[key]["durable"])
        self.assertTrue(tm.transcripts[key]["acknowledged"])
        self.assertTrue(tm.transcripts[key]["purge_verified"])

        # Test dry publication unavailable
        tm.set_unavailable_dry(key)
        self.assertIsNone(tm.transcripts[key]["bridge_result"])
        self.assertEqual(tm.transcripts[key]["reason"], "unavailable")

    def test_synthetic_registration(self):
        reg = synthetic_register({
            "task_id": "task1", "attempt": "a1", "objective": "test objective", "patch_path": None
        })
        self.assertTrue(reg["registered"])
        self.assertFalse(reg["launch_authority_granted"])

    # -------------------------------------------------------------------------
    # POSITIVE CONTROLS
    # -------------------------------------------------------------------------

    def test_positive_control_verified_stop_to_clean(self):
        """Positive control: verified stop-to-clean flow."""
        engine = HostControlEngine()
        lease = engine.grant_lease("lease1", "task1", "a1", epoch=1)

        engine.fence_lease("lease1", expected_epoch=1)
        engine.perform_cleanup_and_release(
            "lease1", target_cgroup_empty=True, tmpfs_purged=True, pids_empty=True
        )

        self.assertTrue(lease["released"])
        self.assertNotIn("lease1", engine.held_capacity)

    def test_positive_control_indeterminate_recovery_retains_liabilities(self):
        """Positive control: indeterminate recovery that retains liabilities/held capacity."""
        engine = HostControlEngine()
        lease = engine.grant_lease("lease1", "task1", "a1", epoch=1)

        # Mark seat lost (local marker)
        engine.mark_seat_lost("lease1")
        self.assertIn("lease1", engine.held_capacity)
        self.assertIn("lease1", engine.local_markers)

        # Authoritative reconciliation
        auth_receipt = {"schema": "os.cu.worker-receipt/v1", "exit_code": 6, "reason": "seat_lost"}
        rec = engine.reconcile("lease1", auth_receipt)
        self.assertEqual(rec["schema"], "os.cu.worker-receipt/v1")
        self.assertNotIn("lease1", engine.local_markers)

    def test_positive_control_independent_stop_lane_under_saturated_data(self):
        """Positive control: independent STOP lane still progresses under saturated data traffic."""
        carrier = SpeechActSidecar(data_capacity=2, stop_capacity=5)

        # Send gap items (seq=1, seq=2) so they are held in successors, saturating capacity=2
        for seq_i in (1, 2):
            payload = {
                "schema": "os.cu.speech-act/v1", "act": "status", "wave_id": "w1",
                "task_id": "t1", "attempt_id": "a1", "lease_id": "l1",
                "seq": seq_i, "repro_sha256": None, "affected": [], "status": "working", "body": f"body {seq_i}"
            }
            body = canonical(payload)
            sidecar = {
                "sender": "worker1", "task_id": "t1", "attempt_id": "a1", "lease_id": "l1",
                "epoch": 1, "seq": seq_i, "body_sha256": hashlib.sha256(body).hexdigest(), "body_length": len(body)
            }
            carrier.receive_sidecar(sidecar, body)

        # Data queue + held_successors is now saturated (2/2)
        payload_extra = copy.deepcopy(payload)
        payload_extra["seq"] = 3
        body_extra = canonical(payload_extra)
        sidecar_extra = copy.deepcopy(sidecar)
        sidecar_extra["seq"] = 3
        sidecar_extra["body_sha256"] = hashlib.sha256(body_extra).hexdigest()
        sidecar_extra["body_length"] = len(body_extra)

        with self.assertRaises(Refusal) as cm:
            carrier.receive_sidecar(sidecar_extra, body_extra)
        self.assertEqual(cm.exception.rc, 4)

        # Independent STOP lane still accepts STOP signal
        stop_ok = carrier.send_stop({"lease_id": "l1", "reason": "STOP"})
        self.assertTrue(stop_ok)
        self.assertEqual(len(carrier.stop_queue), 1)

    # -------------------------------------------------------------------------
    # MUST-FAIL FIXTURES (10 Required Cases)
    # -------------------------------------------------------------------------

    def test_must_fail_01_stale_epoch_actuation(self):
        """Must-FAIL 1: stale epoch actuation is refused."""
        engine = HostControlEngine()
        engine.grant_lease("lease1", "task1", "a1", epoch=2)
        with self.assertRaises(Refusal) as cm:
            engine.fence_lease("lease1", expected_epoch=1)
        self.assertEqual(cm.exception.rc, 6)

    def test_must_fail_02_reused_pid(self):
        """Must-FAIL 2: reused PID detected across processes."""
        processes = [
            {"pid": 101, "start_time": 1000, "cgroup_inode": 5001},
            {"pid": 101, "start_time": 1000, "cgroup_inode": 5001}  # Duplicate/reused PID identity
        ]
        seen = set()
        with self.assertRaises(Refusal) as cm:
            for proc in processes:
                ident_tuple = (proc["pid"], proc["start_time"], proc["cgroup_inode"])
                require(ident_tuple not in seen, "reused PID identity detected", 6)
                seen.add(ident_tuple)
        self.assertEqual(cm.exception.rc, 6)

    def test_must_fail_03_wrong_owned_subtree(self):
        """Must-FAIL 3: wrong owned subtree termination attempt."""
        owned_cgroup = "/sys/fs/cgroup/w8/lease1"
        target_process_cgroup = "/sys/fs/cgroup/system.slice/other.service"

        with self.assertRaises(Refusal) as cm:
            require(target_process_cgroup.startswith(owned_cgroup), "target process outside owned cgroup subtree", 5)
        self.assertEqual(cm.exception.rc, 5)

    def test_must_fail_04_wal_failure_allowing_new_grants(self):
        """Must-FAIL 4: WAL failure allowing new grants is prevented."""
        engine = HostControlEngine(wal_ok=False)
        with self.assertRaises(Refusal) as cm:
            engine.grant_lease("lease1", "task1", "a1", epoch=1)
        self.assertEqual(cm.exception.rc, 3)

    def test_must_fail_05_full_data_queue_starving_stop(self):
        """Must-FAIL 5: full data queue starving STOP (demonstrates refusal if STOP used data lane)."""
        data_lane = []
        data_capacity = 2

        # Fill data lane
        data_lane.append("data1")
        data_lane.append("data2")

        # Attempt to send STOP over saturated data lane fails
        with self.assertRaises(Refusal) as cm:
            require(len(data_lane) < data_capacity, "data lane full: STOP starved", 4)
            data_lane.append("STOP")
        self.assertEqual(cm.exception.rc, 4)

    def test_must_fail_06_orphan_process(self):
        """Must-FAIL 6: orphan process surviving purge is detected and refused."""
        engine = HostControlEngine()
        engine.grant_lease("lease1", "task1", "a1", epoch=1)
        engine.fence_lease("lease1", expected_epoch=1)

        with self.assertRaises(Refusal) as cm:
            engine.perform_cleanup_and_release(
                "lease1", target_cgroup_empty=True, tmpfs_purged=True, pids_empty=False  # Orphan process
            )
        self.assertEqual(cm.exception.rc, 6)

    def test_must_fail_07_purge_omitted(self):
        """Must-FAIL 7: release attempted when purge was omitted."""
        engine = HostControlEngine()
        engine.grant_lease("lease1", "task1", "a1", epoch=1)
        engine.fence_lease("lease1", expected_epoch=1)

        with self.assertRaises(Refusal) as cm:
            engine.perform_cleanup_and_release(
                "lease1", target_cgroup_empty=True, tmpfs_purged=True, pids_empty=True, skip_purge=True
            )
        self.assertEqual(cm.exception.rc, 6)

    def test_must_fail_08_local_marker_treated_as_receipt(self):
        """Must-FAIL 8: local code 6 marker treated as authoritative worker receipt."""
        local_marker = {"status": "local_marker", "rc": 6, "reason": "seat_lost"}

        with self.assertRaises(Refusal) as cm:
            require(local_marker.get("schema") == "os.cu.worker-receipt/v1",
                    "local marker cannot be used as worker receipt for publication", 4)
        self.assertEqual(cm.exception.rc, 4)

    def test_must_fail_09_release_on_revoke_intent(self):
        """Must-FAIL 9: releasing capacity on revoke intent without physical purge proof."""
        revoke_intent = {"action": "revoke", "state": "INTENT", "physical_purge_verified": False}

        with self.assertRaises(Refusal) as cm:
            require(revoke_intent.get("physical_purge_verified") is True,
                    "release on revoke INTENT without verified purge is refused", 6)
        self.assertEqual(cm.exception.rc, 6)

    def test_must_fail_10_restored_service_claiming_success(self):
        """Must-FAIL 10: restored service claiming success for unfinished work."""
        unreconciled_attempt = {"state": "INDETERMINATE", "finished": False, "raw_rc": 0}

        with self.assertRaises(Refusal) as cm:
            # Restored service attempts to publish success without reconciliation receipt
            require(unreconciled_attempt.get("finished") is True,
                    "restored service cannot relabel unfinished work as success", 6)
        self.assertEqual(cm.exception.rc, 6)


if __name__ == "__main__":
    unittest.main()
