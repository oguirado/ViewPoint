#!/usr/bin/env python3
"""LIVE. Run with a new RT path under a provisioned parent directory.

The two local agents use separate processes and provisioned broker ports. The
first writes a fixture artifact. The second may consume it only after the broker
records its verified production. Leaves RT/events.jsonl for independent replay.
"""
import argparse
import hashlib
import json
from pathlib import Path
import selectors
import socket
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bus.broker import Broker, BrokerError, call


def agent(rt, role, fd):
    request_id = f"t.{role}.1.m1"
    tm = {"v": 1, "task_id": f"t.{role}.1", "microtask_id": request_id,
          "text": role, "after": ["t.producer.1.m1"] if role == "consumer" else [],
          "supersedes": None}
    target = {"target": role, "authority_epoch": 1, "incarnation": f"{role}-instance"}
    with socket.socket(fileno=fd) as sock:
        call(sock, "enqueue", event_id=request_id, tm=tm, **target)
        if role == "consumer":
            try:
                call(sock, "try_acquire", request_id=request_id, **target)
            except BrokerError as exc:
                if str(exc) != "MICROTASK_BUSY":
                    raise
            else:
                raise RuntimeError("consumer admitted before verified handoff")
        print("ready", flush=True)
        if sys.stdin.readline() != "run\n":
            raise RuntimeError("supervisor closed before dispatch")
        lock = call(sock, "try_acquire", request_id=request_id, **target)
        call(sock, "dispatch", lock_id=lock)
        artifact = rt / "handoff.bin"
        if role == "producer":
            with artifact.open("xb") as writer:
                writer.write(b"U3a handoff\n")
        print(json.dumps({"lock_id": lock, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}))


def live(rt):
    rt.mkdir(mode=0o700, exist_ok=False)
    processes = {}
    with Broker(rt) as broker:
        try:
            for role in ("producer", "consumer"):
                binding = {"schema": "vp.surface_binding/v1", "surface_id": role,
                           "endpoint": f"fixture/{role}", "owner": role, "purpose": "handoff",
                           "incarnation": f"{role}-instance", "authority_epoch": 1, "obligations": []}
                broker.bind(role, binding, lease_state="AGENT")
                with broker.agent_port(role) as sock:
                    proc = subprocess.Popen(
                        [sys.executable, "-I", "-B", __file__, str(rt), "--agent", role,
                         "--fd", str(sock.fileno())], pass_fds=(sock.fileno(),),
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                processes[role] = proc
                with selectors.DefaultSelector() as ready:
                    ready.register(proc.stdout, selectors.EVENT_READ)
                    if not ready.select(15) or proc.stdout.readline() != "ready\n":
                        raise RuntimeError(f"{role} did not enqueue")
            expected = hashlib.sha256(b"U3a handoff\n").hexdigest()
            for role, proc in processes.items():
                output, _ = proc.communicate("run\n", timeout=15)
                if proc.returncode:
                    raise RuntimeError(f"{role} exited with {proc.returncode}")
                result = json.loads(output)
                if result["sha256"] != expected or (rt / "handoff.bin").read_bytes() != b"U3a handoff\n":
                    raise RuntimeError("handoff evidence mismatch")
                broker.finish(role, result["lock_id"], "VERIFIED")
        finally:
            for proc in processes.values():
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdin.close()
                proc.stdout.close()
    rows = [json.loads(line) for line in (rt / "events.jsonl").read_bytes().splitlines()]
    completed = [r for r in rows if r["rec"] == "close" and r["verdict"] == "VERIFIED"]
    consumers = [r for r in rows if r["rec"] == "turn" and r["agent_id"] == "consumer"]
    if ([r["agent_id"] for r in completed] != ["producer", "consumer"] or len(consumers) != 1
            or consumers[0]["seq"] <= completed[0]["seq"]):
        raise RuntimeError("missing verified handoff edge")
    print(json.dumps({"status": "PASS", "handoff_edges": 1, "journal": str(rt / "events.jsonl")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rt", type=Path)
    parser.add_argument("--agent", choices=["producer", "consumer"])
    parser.add_argument("--fd", type=int)
    args = parser.parse_args()
    if args.agent:
        agent(args.rt, args.agent, args.fd)
    else:
        live(args.rt)
