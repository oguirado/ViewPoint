"""Inert bindings for the public ViewPoint JSON entry points."""

from contextlib import closing
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sqlite3
import sys


VP_DIRECT = Path(__file__).resolve().parents[3]
SCHEMAS = VP_DIRECT / "schemas"
EXAMPLES = SCHEMAS / "tests" / "examples"
sys.path.insert(0, str(VP_DIRECT))

from control.core import control
from expose import cli, mcp
from oflane import Mailbox


def read_json(path):
    return json.loads(path.read_bytes())


WINDOW = read_json(EXAMPLES / "vp.control.req_v1.valid.json")
CAPABILITY = read_json(EXAMPLES / "vp.capability_v1.valid.json")
IDENTITY = control.LaneIdentity(WINDOW["context"]["caller"], WINDOW["context"]["agent_instance"])
SESSION = WINDOW["context"]["session_id"]
REGISTRY = control.CapabilityRegistry([CAPABILITY])


def encode(value, *, sort_keys=False):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=sort_keys,
                       separators=(",", ":")) + "\n").encode("utf-8")


def expected_digest(request):
    return sha256(encode(request, sort_keys=True)).hexdigest()


def rpc(request):
    context = {name: value for name, value in request["context"].items()
               if name not in {"caller", "agent_instance"}}
    arguments = {**context, **request["args"]}
    arguments.update({key: request[key] for key in ("request_id", "dry_run") if key in request})
    return {"jsonrpc": "2.0", "id": "fixture", "method": "tools/call",
            "params": {"name": request["op"], "arguments": arguments}}


class Harness:
    def __init__(self, root, *, outcome=None, exit_after_send=False, database="requests.sqlite"):
        self.root = root
        self.root.mkdir(exist_ok=True)
        self.effects = root / "fixture-sends.jsonl"
        self.previews = []
        self.gates = []
        self.store = control.RequestStore(root / database)
        self.mailbox = Mailbox(root, IDENTITY)
        self.cli_binding = cli.TransportBinding(str(root / "unused.sock"), SESSION, "fixture-core",
                                                IDENTITY, 500, 2048, 4096)

        def send(request):
            with self.effects.open("ab") as ledger:
                ledger.write(encode(request, sort_keys=True))
                ledger.flush()
                os.fsync(ledger.fileno())
            if exit_after_send:
                os._exit(73)
            return control.Outcome(True, "OK") if outcome is None else outcome

        def preview(request):
            self.previews.append(request)
            return control.Outcome(True, "OK")

        def allow(request, _risk):
            self.gates.append(request["request_id"])
            return True

        self.dispatcher = control.Dispatcher(
            REGISTRY, self.store, pace_risk=allow, lease_epoch=allow,
            tools={WINDOW["op"]: control.Handler(send, preview)},
        )
        self.server = mcp.MCPServer(self.dispatcher, IDENTITY, binding_context={"session_id": SESSION})

    def call_c1(self, request):
        return self.dispatcher.dispatch_mailbox(request, IDENTITY)

    def call_mcp(self, wire):
        output = BytesIO()
        mcp.serve(self.server, BytesIO(wire), output)
        [response] = output.getvalue().splitlines()
        return json.loads(response)

    def call_cli(self, wire, *, verb=CAPABILITY["cli"]):
        request_file = self.root / "cli-input.json"
        request_file.write_bytes(wire)
        binding = self.cli_binding
        argv = [verb, "--socket", binding.socket, "--session-id", binding.session_id,
                "--agent-instance", IDENTITY.agent_instance, "--core-instance",
                binding.core_instance, "--timeout-ms", str(binding.timeout_ms),
                "--max-line-bytes", str(binding.max_line_bytes), "--max-reply-bytes",
                str(binding.max_reply_bytes), "--json-arg", str(request_file)]
        stdout, stderr = StringIO(), StringIO()
        rc = cli.run(argv, self.dispatcher, binding, stdout=stdout, stderr=stderr)
        return rc, stdout.getvalue(), stderr.getvalue()

    def call_mailbox(self, request):
        self.mailbox.submit(request)
        return self.mailbox.run_one(request["request_id"], self.dispatcher)

    def deliver_file(self, request_id, wire):
        path = self.root / (request_id + ".req.json")
        path.write_bytes(wire)
        return self.mailbox.run_one(request_id, self.dispatcher)

    def call(self, lane, request):
        if lane == "c1":
            return self.call_c1(request)
        if lane == "mcp":
            return json.loads(self.call_mcp(encode(rpc(request)))["result"]["content"][0]["text"])
        if lane == "cli":
            verb = REGISTRY.get(request["op"])["cli"]
            _, stdout, stderr = self.call_cli(encode(request), verb=verb)
            if stderr:
                raise AssertionError(stderr)
            return json.loads(stdout)
        if lane == "mailbox":
            return self.call_mailbox(request)
        raise ValueError(lane)

    def claims(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return db.execute("SELECT id, digest, reply FROM requests ORDER BY id").fetchall()

    def mailbox_state(self):
        return sorted(path.name for pattern in (".oflane-state/*", "*.reply.json", "*.done")
                      for path in self.root.glob(pattern))

    def sent(self):
        return self.effects.read_bytes() if self.effects.exists() else b""
