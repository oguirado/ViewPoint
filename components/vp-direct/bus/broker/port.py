"""A provisioned local socket carries one agent's JSON requests across processes.

Only the supervisor owns the Broker. Pass the returned socket to its designated
worker at spawn time. Each client serializes requests on its own socket. EOF
does not release outstanding custody.
"""

import json
import socket
import threading

from jsonschema.exceptions import ValidationError

from .journal import BrokerError

# Use datagram boundaries so a truncated request can never become a second command.
MAX_PACKET = 65536
AGENT_OPS = frozenset(("enqueue", "human_needed", "try_acquire", "release", "quarantine", "dispatch"))


def _receive(sock):
    packet, _, flags, _ = sock.recvmsg(MAX_PACKET)
    if not packet:
        raise EOFError
    if flags & socket.MSG_TRUNC:
        raise ValueError("oversized packet")
    return json.loads(packet)


def _send(sock, value):
    packet = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(packet) > MAX_PACKET:
        raise ValueError("oversized packet")
    sock.sendall(packet)


def call(sock, op, **args):
    """One synchronous request, with no automatic retry after loss of a reply."""
    _send(sock, {"op": op, "args": args})
    reply = _receive(sock)
    if "error" in reply:
        raise BrokerError(reply["error"])
    return reply["result"]


def provision(broker, agent_id):
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    broker._ports.append(server)

    def serve():
        with server:
            try:
                while True:
                    try:
                        req = _receive(server)
                        if not isinstance(req, dict) or set(req) != {"args", "op"}:
                            raise BrokerError("BAD_INTENT")
                        op, args = req["op"], req["args"]
                        if op not in AGENT_OPS or not isinstance(args, dict) or "agent_id" in args or "by" in args:
                            raise BrokerError("NO_LEASE")
                        value = getattr(broker, op)(agent_id, **args)
                        reply = {"result": value}
                    except BrokerError as exc:
                        reply = {"error": str(exc)}
                    except (ValueError, TypeError, KeyError, ValidationError, RecursionError):
                        reply = {"error": "BAD_INTENT"}
                    _send(server, reply)
            except (EOFError, OSError):
                pass
            finally:
                with broker._mutex:
                    if server in broker._ports:
                        broker._ports.remove(server)

    threading.Thread(target=serve, name="broker-agent-port", daemon=True).start()
    return client
