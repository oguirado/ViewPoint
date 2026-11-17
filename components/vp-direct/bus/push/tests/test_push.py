import importlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from bus.push import ContextWallClient, PushError, notify_human, push

implementation = importlib.import_module("bus.push.push")
FRAME_LIMIT = 65536
FROM = "t.sender.1.m1"
TO = "t.receiver.1.m1"


def encode(message):
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def response(request_id, result):
    return encode({"jsonrpc": "2.0", "id": request_id, "result": result})


def inbox(events):
    return {"content": [{"type": "text", "text": json.dumps({"events": events})}]}


def handoff():
    return {"agent": "sender", "to": "receiver", "lamport": 6,
            "untrusted_text": f"tm1 handoff {FROM} -> {TO} ready"}


def broker_records():
    def enqueue(agent, mid, event_id, after):
        return {"rec": "enqueue", "agent_id": agent, "event_id": event_id,
                "tm": {"v": 1, "task_id": mid.rsplit(".", 1)[0], "microtask_id": mid,
                       "text": "Synthetic job", "after": after, "supersedes": None}}

    return [
        enqueue("sender", FROM, "evt-send", []),
        enqueue("receiver", TO, "evt-receive", [FROM]),
        {"rec": "turn", "agent_id": "sender"},
        {"rec": "close", "event_id": "evt-send", "verdict": "VERIFIED", "drops": 0},
    ]


def human_event():
    return {"rec": "HUMAN_NEEDED", "by": "broker", "v": 1, "seq": 3,
            "session_id": "s-fixture", "goal_id": "g-fixture", "reason": "login",
            "raised_by": "sender", "page_title": "SyntheticPageHeading",
            "page_content": "Synthetic view contents", "typed_text": "Synthetic entered value"}


class FakeWall(io.BytesIO):
    """A duplex peer which derives inbox events from sent post_status calls."""

    def __init__(self):
        super().__init__()
        self.requests = []
        self.events = []
        self.reply = io.BytesIO()

    def write(self, frame):
        message = json.loads(frame)
        self.requests.append(message)
        call = message["params"]
        arguments = call["arguments"]
        result = {"content": []}
        if call["name"] == "post_status" and "to" in arguments:
            self.events.append(dict(arguments, untrusted_text=arguments["text"], lamport=6))
        if call["name"] == "read_inbox":
            result = inbox([event for event in self.events if event["to"] == arguments["agent"]])
        self.reply = io.BytesIO(response(message["id"], result))
        return super().write(frame)

    def readline(self, size=-1):
        return self.reply.readline(size)


class PushTests(unittest.TestCase):
    def test_projection_reads_addressed_handoff_back_with_read_inbox(self):
        wall = FakeWall()
        summary = push(broker_records(), "demo", ContextWallClient(wall, wall), inbox_agent="receiver")
        by_name = [request["params"]["name"] for request in wall.requests]
        self.assertEqual(by_name[0], "post_plan")
        self.assertEqual(by_name[-1], "read_inbox")
        self.assertEqual(wall.requests[-1]["params"]["arguments"],
                         {"room": "demo", "agent": "receiver"})
        self.assertEqual(summary["calls"], len(by_name) - 1)
        self.assertEqual(summary["refused"], 0)
        self.assertEqual(summary["ok"], summary["calls"])
        self.assertEqual(summary["inbox_events"], 1)
        self.assertEqual(summary["handoffs_read_back"], [
            {"from": FROM, "to": TO, "from_agent": "sender", "to_agent": "receiver", "lamport": 6},
        ])

    def test_read_inbox_rejects_malformed_response(self):
        writer = io.BytesIO()
        reader = io.BytesIO(response(1, {"content": [{"type": "text", "text": "{}"}]}))
        with self.assertRaisesRegex(PushError, "invalid read_inbox result"):
            push([], "demo", ContextWallClient(reader, writer), inbox_agent="receiver")

    def test_wall_refusal_is_counted_and_not_retried(self):
        class RefusingWall(FakeWall):
            def write(self, frame):
                written = super().write(frame)
                request = self.requests[-1]
                if request["params"]["name"] == "post_plan":
                    self.reply = io.BytesIO(response(request["id"], {"isError": True}))
                return written

        wall = RefusingWall()
        summary = push(broker_records(), "demo", ContextWallClient(wall, wall), inbox_agent="receiver")
        requests = [request["params"]["name"] for request in wall.requests]
        self.assertEqual(summary["refused"], 2)
        self.assertEqual(requests.count("post_plan"), 2)
        self.assertEqual(requests[-1], "read_inbox")

    def test_mismatched_reply_id_stops_batch_without_retry(self):
        writer = io.BytesIO()
        client = ContextWallClient(io.BytesIO(response(2, {})), writer)
        with self.assertRaisesRegex(PushError, "uncorrelated wall response"):
            push(broker_records(), "demo", client)
        self.assertEqual(len(writer.getvalue().splitlines()), 1)
        with self.assertRaisesRegex(PushError, "requires reconciliation"):
            push(broker_records(), "demo", client)
        self.assertEqual(len(writer.getvalue().splitlines()), 1)

    def test_read_inbox_ignores_untrusted_other_handoffs(self):
        bogus = [
            dict(handoff(), agent="someone-else"),
            dict(handoff(), lamport="untrusted"),
            dict(handoff(), untrusted_text=handoff()["untrusted_text"] + "\n"),
            dict(handoff(), to="other", untrusted_text=f"tm1 handoff {FROM} -> t.other.1.m1 ready"),
        ]
        reader = io.BytesIO(response(1, inbox(bogus)))
        result = push([], "demo", ContextWallClient(reader, io.BytesIO()), inbox_agent="receiver")
        self.assertEqual(result["handoffs_read_back"], [])

    def _assert_forbidden_room(self):
        wall = FakeWall()
        with self.assertRaisesRegex(PushError, "wall room refused"):
            push(broker_records(), "viewpoint", ContextWallClient(wall, wall), inbox_agent="receiver")
        self.assertEqual(wall.getvalue(), b"")

    def test_refuses_viewpoint_before_any_transport(self):
        self._assert_forbidden_room()

    def test_mutant_removing_forbidden_rooms_is_killed(self):
        with patch.object(implementation, "FORBIDDEN_ROOMS", frozenset()):
            with self.assertRaises(AssertionError):
                self._assert_forbidden_room()

    def test_refuses_cred_re_before_any_projection_call(self):
        for text in ("ToKeN", "secret", "credential", "password", ".env", "ID_RSA", "auth.json"):
            with self.subTest(marker=text):
                records = broker_records()
                records[1]["tm"]["accept"] = "Synthetic " + text + " marker"
                wall = FakeWall()
                with self.assertRaisesRegex(PushError, "credential-like text refused"):
                    push(records, "demo", ContextWallClient(wall, wall))
                self.assertEqual(wall.getvalue(), b"")

    def test_credential_marker_beyond_projected_title_is_refused(self):
        wall = FakeWall()
        records = broker_records() + [
            {"rec": "task", "op": "open", "agent_id": "new", "task_id": "t.new.1",
             "title": "x" * 200 + " password", "plan": [{"microtask_id": "t.new.1.m1", "text": "Run"}]},
        ]
        with self.assertRaisesRegex(PushError, "credential-like text refused"):
            push(records, "demo", ContextWallClient(wall, wall))
        self.assertEqual(wall.getvalue(), b"")

    def test_uses_frozen_tm_contract(self):
        records = broker_records()
        del records[0]["tm"]["supersedes"]
        wall = FakeWall()
        with self.assertRaisesRegex(PushError, "invalid tm/v1 record"):
            push(records, "demo", ContextWallClient(wall, wall))
        self.assertEqual(wall.getvalue(), b"")

    def test_human_event_is_not_projected_to_context_wall(self):
        wall = FakeWall()
        result = push([human_event()], "demo", ContextWallClient(wall, wall))
        self.assertEqual(result["calls"], 0)
        self.assertEqual(wall.getvalue(), b"")

    @staticmethod
    def _text_at_size(size, request_id, text=""):
        message = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                   "params": {"name": "post_status", "arguments": {"text": text}}}
        return text + "x" * (size - len(encode(message)))

    def test_all_bytes_of_exact_limit_frame_are_accepted(self):
        writer = io.BytesIO()
        client = ContextWallClient(io.BytesIO(response(1, {})), writer)
        text = self._text_at_size(FRAME_LIMIT, 1, "é\n")
        self.assertEqual(client._call_batch([("post_status", {"text": text})]), [(True, {})])
        frame = writer.getvalue()
        self.assertEqual(len(frame), FRAME_LIMIT)
        self.assertEqual(frame.count(b"\n"), 1)
        self.assertEqual(json.loads(frame)["params"]["arguments"]["text"], text)

    def _assert_oversized_batch_refused(self):
        writer = io.BytesIO()
        reader = io.BytesIO(response(1, {}) + response(2, {}))
        client = ContextWallClient(reader, writer)
        text = self._text_at_size(FRAME_LIMIT + 1, 2, "é")
        with self.assertRaisesRegex(PushError, "wall frame exceeds"):
            client._call_batch([("post_status", {"text": "ok"}), ("post_status", {"text": text})])
        self.assertEqual(writer.getvalue(), b"")

    def test_oversized_frame_refuses_whole_batch_before_transport(self):
        self._assert_oversized_batch_refused()

    def test_mutant_allowing_oversized_frame_is_killed(self):
        with patch.object(implementation, "MAX_FRAME_BYTES", FRAME_LIMIT + 1):
            with self.assertRaises(AssertionError):
                self._assert_oversized_batch_refused()

    def test_oversized_response_retires_connection(self):
        huge = response(1, {"data": "x" * FRAME_LIMIT})
        writer = io.BytesIO()
        reader = io.BytesIO(huge)
        client = ContextWallClient(reader, writer)
        with self.assertRaisesRegex(PushError, "oversized or incomplete"):
            push([], "demo", client, inbox_agent="receiver")
        self.assertEqual(reader.tell(), FRAME_LIMIT + 1)
        before = writer.getvalue()
        with self.assertRaisesRegex(PushError, "requires reconciliation"):
            push([], "demo", client, inbox_agent="receiver")
        self.assertEqual(writer.getvalue(), before)


class NotifierTests(unittest.TestCase):
    def _assert_metadata_only(self):
        captured = []
        notify_human(human_event(), captured.append)
        self.assertEqual(captured, [{"session_id": "s-fixture", "goal_id": "g-fixture",
                                     "reason": "login", "raised_by": "sender"}])

    def test_notification_has_no_title_page_content_or_typed_text(self):
        self._assert_metadata_only()

    def test_mutant_forwarding_page_title_is_killed(self):
        with patch.object(implementation, "NOTICE_FIELDS", (*implementation.NOTICE_FIELDS, "page_title")):
            with self.assertRaises(AssertionError):
                self._assert_metadata_only()

    def test_untrusted_or_invalid_event_never_invokes_sink(self):
        captured = []
        for event in (dict(human_event(), by="agent"), dict(human_event(), reason="free-form"),
                      dict(human_event(), goal_id="secret-fixture")):
            with self.assertRaises(PushError):
                notify_human(event, captured.append)
        self.assertEqual(captured, [])
