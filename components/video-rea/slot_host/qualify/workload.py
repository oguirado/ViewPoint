# SPDX-License-Identifier: AGPL-3.0-only
"""Bind the sealed REA workload to the qualification CLI and MCP dispatcher."""

import json
from uuid import uuid4

from ..compare import tools as comparison
from ..compare._model import Behaviour, Comparison, Episode, Policy
from ..measure.harness import REGISTRY_SCHEMAS, registry
from ..measure.runtime import INSTALLATION, Runtime
from ..measure.seal import DEFAULT_MANIFEST, Seal, read_regular
from ..projections import generate, jcs_bytes
from ..projections.mcp_lane import SlotProjector, control_reply
from ..projections.schema import digest
from ..runtime import binding, evidence_projection as projection, tools
from ..runtime.receipts import read_object
from .authority import authorities
from .controls import LiveCall, QualificationRejected, require


_CONTEXT = {"caller": "agent", "agent_instance": "rea-local-open-qualifier"}
_POLICY_REF = "policy:local-qualification@1"


class LocalDispatcher:
    """Route authenticated requests from both faces through the real host adapters."""

    def __init__(self, workload, ports, authority):
        self.workload = workload
        self.ports = ports
        self.authority = authority
        self.reference_ref = None
        self.intents = {}

    def resolve_tape(self, parent, checksum):
        if self.reference_ref is None:
            return None
        retained = self.ports.read_source(self.reference_ref)
        command = retained.inspection["start"]["command"]
        if (retained.profile["attempt"]["operation"] == "record"
                and retained.profile["episode"]["episode_id"] == parent
                and digest(json.loads(command["arguments"][-1])) == checksum
                and command["arguments"][-1].encode() == self.workload.tape):
            return {"parent_episode_id": parent, "input_tape_sha256": checksum}
        return None

    def __call__(self, envelope, *, retention):
        carrier = envelope["args"]
        route = carrier["route_id"]
        arguments = carrier["arguments"]
        request = envelope["request_id"]
        effective = digest(envelope)
        tool = route.removeprefix("host.")
        if envelope.get("context") != _CONTEXT or envelope.get("dry_run") is not False:
            raise QualificationRejected("entry_point: authenticated live qualification request required")
        try:
            if tool in ("behaviour.record", "behaviour.replay"):
                if retention is not self.ports:
                    raise QualificationRejected("entry_point: writer and inspector composition changed")
                policy = Runtime.policy(self.workload.manifest, arguments["payload"]["operation_id"])
                result = tools.dispatch(
                    tool, {"request_id": request, "payload": arguments["payload"]}, context=_CONTEXT,
                    host=self.workload.inventory, host_policy=policy, live_authority=self.authority,
                    tape_resolver=self.resolve_tape, retention=self.ports,
                )
                outcome = result["result"]["receipt"]["attempt"].get("control_outcome")
                if outcome == "UNCERTAIN_OUTCOME":
                    return control_reply(request, outcome, ok=False, effective=effective)
            elif tool == "behaviour.compare":
                self.intents[request] = effective
                result = comparison.handle(tool, arguments["payload"], request_id=request,
                                           host=self, retention=retention)
            else:
                raise QualificationRejected("entry_point: route is outside the disposable workload")
        except tools._ControlError as exc:
            return control_reply(request, exc.code, ok=False, effective=effective)
        return control_reply(request, "OK", ok=True, effective=effective,
                             result={"kind": "host_result", "outside_capsule": True, "payload": result})

    def _episode(self, retained):
        view = retained.inspection
        start = view["start"]
        tape_arg = start["command"]["arguments"][-1].encode()
        if tape_arg != self.workload.tape:
            raise QualificationRejected("comparison: retained input tape differs from the sealed workload")
        tape = json.loads(tape_arg)
        rows = self.workload.observations(self.ports, retained)
        expected = self.workload.expected[retained.profile["attempt"]["operation"]]
        require(rows == expected, "comparison", "retained provider output differs from the sealed oracle")
        mapped = {row["occurrence"]: row for row in tape}
        return Episode(episode_id=retained.profile["episode"]["episode_id"],
                       tape_sha256=digest(tape), behaviour=Behaviour(
                           cases=tuple({"case_id": row["occurrence"],
                                        "input_digest": digest(mapped[row["occurrence"]]),
                                        "observation": {"kind": "value", "payload": row["value"]}}
                                       for row in rows),
                           complete=True,
                       ))

    def resolve_comparison(self, tool, request, *, reference_ref, candidate_ref, policy_ref,
                           runner_client, inspector_client):
        require(policy_ref == _POLICY_REF and runner_client is self.ports.runner.client
                and inspector_client is self.ports.inspector.client, "comparison", "authority or policy differs")
        reference, candidate = (self.ports.read_source(ref) for ref in (reference_ref, candidate_ref))
        require(reference.profile["attempt"]["operation"] == "record"
                and candidate.profile["attempt"]["operation"] == "replay"
                and candidate.profile["episode"]["parent_episode_id"] == reference.profile["episode"]["episode_id"]
                and candidate.profile["replay"]["input_tape_sha256"] == self.workload.tape_sha,
                "comparison", "candidate lineage does not bind the exact reference and tape")
        pin = {"id": _POLICY_REF, "version": "1", "checks": ["structured"], "determinism": "exact"}
        policy = Policy(id=_POLICY_REF, version="1", sha256=digest(pin), comparator_id=tool,
                        comparator_version="1", checks=("structured",), determinism_modes=frozenset({"exact"}),
                        repeatable_effects_required=True, contrast_ids=(), negative_control_ids=())
        return Comparison(
            policy=policy, reference=self._episode(reference), candidate=self._episode(candidate),
            exchange_valid=True, evidence_bound=True, custody_verified=True, lineage_verified=True, fresh=True,
            determinism_mode="exact", effects_repeatable=True, perception_ceiling="none",
            vision_authorized=False, producer={"kind": "host_mini_tool", "id": tool, "process_bound": True},
            oracle_independent=False, contrast_outcomes={}, negative_controls=(),
            attempt_id=uuid4().hex, request_sha256=self.intents[request],
            reference_attempt_id=reference.inspection["attempt_id"],
            candidate_attempt_id=candidate.inspection["attempt_id"],
        )

    def verify_comparison_sources(self, _tool, _request, resolved, sources):
        return (self._episode(sources["reference"]), self._episode(sources["candidate"])) == (
            resolved.reference, resolved.candidate,
        )


class QualificationWorkload:
    def __init__(self, root, clients, control):
        fixture = Seal(DEFAULT_MANIFEST)
        self.manifest = fixture.inputs["slot"]
        self.expected = {
            operation: fixture.truth["assignments"][f"behaviour.{operation}/nominal"]["occurrences"]
            for operation in ("record", "replay")
        }
        self.changed_expected = fixture.truth["assignments"]["behaviour.record/changed"]["occurrences"]
        self.tape, self.changed = (
            jcs_bytes(fixture.inputs["assets"][ref]["data"]) for ref in ("tape:fixture", "tape:changed")
        )
        self.tape_sha = digest(json.loads(self.tape))
        path = root / "fixture-adapter"
        path.write_bytes(fixture.executable)
        path.chmod(0o500)
        self.inventory = {INSTALLATION: (self.manifest, path)}
        self.bindings = {
            operation: binding.verify_binding(
                self.manifest, path, operation_id=operation, requested_profile="external_process",
                capsule_perception_ceiling="none", installation_ref=INSTALLATION, retention=clients,
                **tools._auth_kwargs_from_policy(Runtime.policy(self.manifest, operation)),
            ) for operation in ("record", "replay")
        }
        self.witness = clients.custody
        self.primary, self.control = (LocalDispatcher(self, ports, authority) for ports, authority in zip(
            (clients, control), authorities(root, self.bindings, clients, control), strict=True,
        ))
        resources = registry(fixture, {path: read_regular(path) for path in REGISTRY_SCHEMAS})
        options = {
            "registry": resources,
            "policy": {"licence": "allowed", "perception_ceiling": "none", "evidence_schema_bound": True},
            "binding": {"installations": [INSTALLATION],
                        **{key: self.manifest["provider_ref"][key] for key in (
                            "provider_id", "provider_revision", "provider_sha256", "implementation_sha256",
                        )}, **{key: self.manifest["capabilities"]["adapter_identity"][key] for key in (
                            "adapter_id", "adapter_sha256",
                        )}},
        }
        bundle = generate(self.manifest, **options)
        self.projectors = {
            dispatcher: SlotProjector(self.manifest, bundle, dispatch=dispatcher, context=_CONTEXT,
                                     authenticated_context=_CONTEXT, retention=dispatcher.ports, **options)
            for dispatcher in (self.primary, self.control)
        }
        self.record = self._launch("record", "reference", self.tape, self.primary)
        self.lost_claim = self._launch("record", "lost-claim", self.tape, self.primary)
        self.expiry_control = self._launch("record", "expiry", self.tape, self.primary)
        self.denial_control = self._launch("record", "denial", self.changed, self.control)

    def _launch(self, operation, label, tape, dispatcher, **extra):
        request = f"req-qualify-{label}-{uuid4().hex}"
        payload = {"installation_ref": INSTALLATION, "operation_id": operation,
                   "argv": [request, tape.decode()], "tape_sha256": digest(json.loads(tape)),
                   **extra}
        return LiveCall(self.projectors[dispatcher], dispatcher.authority, "host.behaviour." + operation,
                        {"request_id": request, "payload": payload}, self.bindings[operation])

    def replay(self, reference):
        self.primary.reference_ref = reference
        source = self.primary.ports.read_source(reference)
        return self._launch("replay", "candidate", self.tape, self.primary,
                            parent_episode_id=source.profile["episode"]["episode_id"],
                            replay={"determinism_mode": "exact", "input_tape_sha256": self.tape_sha,
                                    "controls": dict.fromkeys(("clock", "rng", "external_effects"),
                                                              "not_applicable")})

    def compare(self, reference, candidate):
        return LiveCall(self.projectors[self.primary], self.primary.authority, "host.behaviour.compare", {
            "request_id": f"req-qualify-comparison-{uuid4().hex}",
            "payload": {"reference_ref": reference, "candidate_ref": candidate, "policy_ref": _POLICY_REF},
        }, None)

    def launch_count(self):
        return sum(1 for path in self.witness.directory.glob("*.process.json") if path.is_file())

    @staticmethod
    def observations(clients, retained):
        entries = [item for item in retained.index["objects"] if item["role"] == "provider_result"]
        require(len(entries) == 1, "oracle", "inspector index has no unique provider result")
        with clients.inspector.client.session(clients.tenant) as reader:
            return json.loads(read_object(reader, retained.inspection["attempt_id"], entries[0], collect=True))
