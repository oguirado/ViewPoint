"""Protocol adapter for the pinned headless controller and its Go launch owner."""
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys


def load_module(name, path, raw=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if raw is None:
        spec.loader.exec_module(module)
    else:
        exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def run(root, prefix):
    if sys.prefix != prefix or sys.base_prefix != prefix:
        raise ValueError("scoped controller interpreter prefix differs")
    loader = load_module("scoped_source_loader", root / "runner.py")
    spec = json.loads((root / "launch.json").read_bytes())
    bundle = {}
    for source in spec["files"]:
        relative = loader.relative_source(source["relative"])
        path = root / "bundle" / relative
        bundle[str(relative)] = (path, loader.checked(path, source["sha256"]))
    backend = load_module("arm_backend", *bundle["mc06-controller/arm_backend.py"])
    load_module("contract_guard", *bundle["mc06-controller/contract_guard.py"])
    controller_module = load_module("scoped_controller", *bundle["mc06-controller/controller.py"])
    config = spec["config"]

    with socket.socket(fileno=3) as owner, owner.makefile("rb") as replies:
        def call(op, **args):
            owner.sendall(json.dumps({"op": op, **args}).encode() + b"\n")
            raw = replies.readline(65537)
            if not raw.endswith(b"\n") or len(raw) > 65536:
                raise RuntimeError("launch owner unavailable")
            reply = json.loads(raw)
            if reply.get("error"):
                raise RuntimeError(reply["error"])
            return reply

        class ManagedProcess:
            def __init__(self, pid):
                self.pid = pid
                self.returncode = None

            def poll(self):
                if self.returncode is None:
                    self.returncode = call("poll")["exit_code"]
                return self.returncode

            def wait(self, timeout=None):
                if self.returncode is None:
                    timeout_ms = 30000 if timeout is None else max(1, int(timeout * 1000))
                    self.returncode = call("wait", timeout_ms=timeout_ms)["exit_code"]
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("scoped firecracker", timeout)
                return self.returncode

        class ScopedBackend(backend.ArmBackend):
            def _check_cgroup(self, child=None):
                proof = call("check_cgroup", child_pid=0 if child is None else child)["cgroup"]
                if proof["path"] != self.config["external_cgroup"]:
                    raise RuntimeError("scoped cgroup proof differs from controller configuration")
                return proof

            def _spawn(self):
                response = call("spawn", directory=str(self.directory))
                return ManagedProcess(response["pid"])

            def emergency_close(self):
                if self.process is not None and self.process.poll() is None:
                    raise RuntimeError("launch owner must reconcile outstanding custody")
                self._release()
                return {"closed": True, "outcome": "nothing_live"}

        controller = controller_module.Controller(config, spec["state_dir"], backend_factory=ScopedBackend)
        print(json.dumps({"mc09_ready": True, "cgroup": config["external_cgroup"]}), flush=True)
        try:
            while True:
                line = sys.stdin.buffer.readline(controller_module.MAX_RECORD + 1)
                if not line:
                    break
                if len(line) > controller_module.MAX_RECORD or not line.endswith(b"\n"):
                    raise ValueError("request bound")
                for event in controller.handle(json.loads(line), config["owner_id"]):
                    print(controller_module.encoded(event).decode(), flush=True)
        finally:
            controller.close()


run(Path(sys.argv[1]), sys.argv[2])
