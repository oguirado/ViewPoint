"""Pinned local geometry replay implementing the TRELLIS service protocol."""
from hashlib import sha256
from pathlib import Path
import re
import time

from asset_factory import core
from providers.trellis2.adapter import Refusal as ServiceRefusal, Response
from tools.blender.bake_support import read_bytes
from .engine import authenticate, fault, load, require


SCHEMA = "af.trellis2-replay/v1"
SCOPE = "provider-fixture-replay"
SHA256 = re.compile(r"[0-9a-f]{64}")


class ReplayTransport:
    """A digest-pinned fixture for one generation, without a model lifecycle."""

    def __init__(self, path, digest):
        require(type(digest) is str and SHA256.fullmatch(digest), 2, "replay_pin_invalid")
        path = Path(path).absolute()
        record_ref = {"path": str(path), "sha256": digest}
        authenticate(record_ref)
        record = load(path, expected_sha256=digest)
        require(type(record) is dict and record.get("schema") == SCHEMA, 2, "replay_record_invalid")
        require(record.get("scope") == "fixture" and "production_verdict" not in record,
                5, "replay_fixture_scope")
        require(type(record.get("request_id")) is str and bool(record["request_id"]),
                4, "replay_request_stale")
        require(record.keys() == {
            "schema", "scope", "request_id", "provider_revision", "provider_sha256",
            "candidate_sha256", "input_derivative_sha256", "params", "output",
        }, 2, "replay_record_invalid")
        require(all(type(record[key]) is str and SHA256.fullmatch(record[key]) for key in
                    ("provider_sha256", "candidate_sha256", "input_derivative_sha256")),
                2, "replay_pin_invalid")
        params = record["params"]
        require(type(params) is dict and params.keys() == {"seed", "pipeline_type"}
                and type(params["seed"]) is int and type(params["pipeline_type"]) is str,
                2, "replay_parameters_invalid")
        output = record["output"]
        require(type(output) is dict and output.keys() == {"path", "sha256"}
                and type(output["path"]) is str
                and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.glb", output["path"])
                and type(output["sha256"]) is str and SHA256.fullmatch(output["sha256"]),
                2, "replay_output_ref_invalid")
        output_ref = {"path": str(path.parent / output["path"]), "sha256": output["sha256"]}
        authenticate(output_ref)
        self.refs = (record_ref, output_ref)
        self.record = record

    def bind_admission(self, candidate_sha256, provider):
        require(self.record["candidate_sha256"] == candidate_sha256, 4, "replay_candidate_stale")
        require(self.record["provider_sha256"] == core.sha256_json(provider), 4, "replay_provider_stale")

    async def generate(self, image, request, *, deadline_ns):
        record = self.record
        if request["request_id"] != record["request_id"]:
            raise ServiceRefusal(4, "replay_request_stale")
        if request["provider_revision"] != record["provider_revision"]:
            raise ServiceRefusal(4, "replay_provider_stale")
        if request["params"] != record["params"]:
            raise ServiceRefusal(4, "replay_parameters_stale")
        if sha256(image).hexdigest() != record["input_derivative_sha256"]:
            raise ServiceRefusal(4, "replay_derivative_stale")
        if time.monotonic_ns() >= deadline_ns:
            raise ServiceRefusal(6, "deadline_exceeded")
        try:
            output = self.refs[1]
            raw = read_bytes(output["path"])
            require(sha256(raw).hexdigest() == output["sha256"], 4, "bytes_stale")
        except Exception as exc:
            rc, reason = fault(exc)
            raise ServiceRefusal(rc, reason) from exc

        async def chunks():
            yield raw

        return Response(request=request, provider_sha256=record["provider_sha256"],
                        output_sha256=output["sha256"], chunks=chunks(),
                        geometry_only=True, raw_preserved=True)

    async def cancel(self, request, *, deadline_ns):
        # No background work survives closing the local read and stream.
        return {"request_id": request["request_id"], "request_sha256": core.sha256_json(request),
                "quiescent": True}
