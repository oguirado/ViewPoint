# SPDX-License-Identifier: AGPL-3.0-only
"""Live qualification checks against the actual REA faces and Tools store."""

from base64 import b64encode
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import StringIO
import json
import re

from ..projections import cli
from ..projections.mcp_lane import SlotProjector
from ..runtime import evidence_projection as projection
from ..runtime.binding import Binding
from ..runtime.conformance_authority import ConformanceLiveAuthority
from ..runtime.retention_deployment import import_tools, load_local_retention
from ..runtime.receipts import _retained_outer_receipt, read_object


class QualificationRejected(ValueError):
    """The named qualification control did not establish its required facts."""


@dataclass(frozen=True)
class LiveCall:
    """A host-owned request through the generated CLI or MCP projection."""

    projector: SlotProjector
    authority: ConformanceLiveAuthority
    route_id: str
    arguments: dict
    binding: Binding | None

    def invoke(self, face, clients):
        from .workload import LocalDispatcher

        dispatch = getattr(self.projector, "_dispatch", None)
        require(type(self.projector) is SlotProjector and type(dispatch) is LocalDispatcher
                and self.projector._retention is clients and dispatch.ports is clients
                and type(self.authority) is ConformanceLiveAuthority and dispatch.authority is self.authority
                and self.authority._retention is clients and self.arguments.get("dry_run", False) is False,
                "entry_point", "authenticated default-on composition is required")
        if self.route_id in ("host.behaviour.compare", "host.timeline.compare"):
            require(self.binding is None and clients.ready(record_pin=projection.V03_SHA, operation_id="compare"),
                    "entry_point", "authenticated host comparison readiness failed")
        else:
            authorized = type(self.binding) is Binding and self.authority.verify(
                binding=self.binding, operation_id=self.binding.operation_id,
                request_id=self.arguments["request_id"], attempt_id=None,
            )
            require(authorized is True, "entry_point",
                    f"default-on readiness failed for the installed binding ({self.authority.last_reason})")
        row = next((row for row in self.projector.bundle["router_rows"] if row["id"] == self.route_id), None)
        if row is None:
            raise QualificationRejected("entry_point: the qualified route is absent")
        if face == "mcp":
            mcp = self.projector.call_tool(row["mcp_name"], self.arguments)
            reply = mcp["structuredContent"]
            require(mcp["isError"] == (reply["ok"] is False), "mcp", "error flag disagrees with the control reply")
            return reply
        argv = row["cli_argv_shape"]
        marker = argv.index("<json>")
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main([*argv[:marker], json.dumps(self.arguments), *argv[marker + 1:]],
                              projector=self.projector)
        reply = json.loads(output.getvalue())
        require((status == 0) == (reply["ok"] is True)
                and (reply.get("code") != "UNCERTAIN_OUTCOME" or status == 13),
                "cli", "process status disagrees with the control reply")
        return reply


def require(condition, control, detail):
    if condition is not True:
        raise QualificationRejected(f"{control}: {detail}")


def status_is(status, call, control):
    remote = import_tools("retention_service.transport.client").RemoteError
    try:
        call()
    except remote as error:
        if error.status == status:
            return error.status
        raise QualificationRejected(f"{control}: expected HTTP {status}, received {error.status}") from error
    raise QualificationRejected(f"{control}: request unexpectedly succeeded")


def _recovery_bytes(writer, item, *, collect=False):
    offset = 0
    digest = sha256()
    raw = bytearray() if collect else None
    while True:
        page = writer.recovery_item(item["item"], offset=offset)
        require(page.offset == offset and page.total_bytes == item["bytes"]
                and (bool(page.data) or offset == item["bytes"]), "recovery", "item page binding differs")
        offset += len(page.data)
        digest.update(page.data)
        if raw is not None:
            raw.extend(page.data)
        if offset >= item["bytes"]:
            break
    require(offset == item["bytes"] and digest.hexdigest() == item["sha256"],
            "recovery", "authenticated item bytes differ from the inventory")
    return bytes(raw) if raw is not None else None


def snapshot(writer):
    reply = writer.recovery({"limit": 100})
    first = reply
    items = list(reply["items"])
    cursors = set()
    while (cursor := reply["next"]) is not None:
        require(cursor not in cursors, "recovery", "inventory cursor repeated")
        cursors.add(cursor)
        reply = writer.recovery({"limit": 100, "cursor": cursor})
        require(all(reply[key] == first[key] for key in ("snapshot", "manifest_item", "manifest_sha256")),
                "recovery", "inventory cursor switched snapshot")
        items.extend(reply["items"])
    require(bool(items) and items[0]["item"] == first["manifest_item"]
            and items[0]["sha256"] == first["manifest_sha256"],
            "recovery", "authenticated manifest binding differs")
    raw = _recovery_bytes(writer, items[0], collect=True)
    manifest = json.loads(raw)
    writer.validator.validate("Manifest", manifest)
    require(manifest["tenant"] == writer.tenant and items[1:] == manifest["items"],
            "recovery", "manifest tenant or paginated inventory differs")
    for item in manifest["items"]:
        _recovery_bytes(writer, item)
    return manifest


def inspect_reply(clients, reply, operation, control):
    require(reply.get("ok") is True and reply.get("code") == "OK", control,
            f"live operation was refused ({reply.get('code')})")
    result = reply.get("result", {})
    kind = result.get("kind")
    require(kind in ("host_result", "slot_exchange"), control, "unrecognized live result variant")
    payload = result.get("payload") if kind == "host_result" else result
    require(type(payload) is dict, control, "live result has no payload")
    outer = None
    if kind == "host_result" and operation != "compare":
        require(payload.get("ok") is True and payload.get("request_id") == reply.get("request_id"),
                control, "runtime control reply does not bind the projected request")
        wrapper = payload.get("result", {})
        require(wrapper.get("evidence_accepted") is True, control, "REA did not accept the observation")
        outer = wrapper.get("receipt")
        require(type(outer) is dict, control, "REA did not publish the outer receipt")
        adapter = outer.get("shared_receipt_adapter", {})
        require(adapter.get("live_use_permitted") is True, control, "REA did not authorize the published receipt")
        ref = adapter.get("shared_receipt_ref")
    else:
        ref = payload.get("receipt_ref")
    require(type(ref) is str and re.fullmatch(r"rcp_[0-9a-f]{32}", ref) is not None,
            control, "published reference is not a Tools receipt")
    try:
        retained = clients.read_source(ref)
        expected = _retained_outer_receipt(retained.profile, retained)
    except (OSError, ValueError, import_tools("retention_service.transport.client").RemoteError) as exc:
        raise QualificationRejected(f"{control}: inspector could not verify the published receipt") from exc
    admission = retained.inspection["admission"]
    require(retained.inspection["terminal"]["state"] == "observed"
            and admission["operation"] == operation and admission["request_id"] == reply["request_id"]
            and admission["profile_schema"] == clients.profile_schema
            and admission["profile_schema_sha256"] == clients.profile_sha256
            and (outer is None or outer == expected)
            and (kind != "host_result" or operation != "compare"
                 or payload.get("artifact_ref") == retained.inspection["artifact_ref"]),
            control, "readback does not bind the route, request and exact REA profile")
    return retained


def unchanged(clients, retained):
    ref = retained.inspection["receipt_ref"]
    try:
        current = clients.read_source(ref)
    except (OSError, ValueError, import_tools("retention_service.transport.client").RemoteError) as exc:
        raise QualificationRejected("readback: published inspector byte graph is unavailable") from exc
    require(current == retained, "readback", "published inspector byte graph changed")
    return ref


def start_retry(clients, retained):
    view = retained.inspection
    expected = {"attempt_id": view["attempt_id"], "start_sha256": view["start_sha256"],
                "request_sha256": view["admission"]["request_sha256"]}
    with clients.runner.client.session(clients.tenant) as writer:
        require(writer.start(view["attempt_id"], projection.record_bytes(view["start"]),
                             request_sha256=expected["request_sha256"]) == expected,
                "exact_start_retry", "original acknowledgement changed")
    return expected


def finish_retries(clients, retained):
    view = retained.inspection
    attempt = view["attempt_id"]
    terminal = projection.record_bytes(view["terminal"])
    artifact = retained.artifact
    with clients.runner.client.session(clients.tenant) as writer:
        def finish(raw):
            return writer.finish(attempt, raw, artifact, start_sha256=view["start_sha256"])

        expected = {key: view[key] for key in ("attempt_id", "start_sha256", "terminal_sha256")}
        require(finish(terminal) == expected, "exact_finish_retry", "original acknowledgement changed")
        changed = json.loads(terminal)
        changed["artifact"]["observation_id"] += "-conflict"
        writer.validator.validate("Terminal", changed)
        conflict = status_is(409, lambda: finish(projection.record_bytes(changed)), "conflicting_finish_retry")
    return {"exact": expected, "conflict_http": conflict}


def deletion_control(store, clients, live_receipts, target):
    """Advance only this disposable store's clock and use its actual scheduler."""
    with clients.runner.client.session(clients.tenant) as writer, \
            clients.inspector.client.session(clients.tenant) as reader:
        holds = []
        clock = store.clock
        try:
            for retained in (*live_receipts, target):
                attempt = retained.inspection["attempt_id"]
                writer.hold(attempt, "qualification")
                holds.append(attempt)
            attempt = target.inspection["attempt_id"]
            reference = target.inspection["receipt_ref"]
            manifest = snapshot(writer)
            entry = next((item for item in manifest["attempts"] if item["attempt_id"] == attempt), None)
            require(entry is not None and "qualification" in entry["holds"],
                    "hold", "writer hold is absent from recovery")
            expiry = datetime.fromisoformat(entry["expires_at"])
            store.clock = lambda: expiry + store.policy.purge_window
            store.sweep()
            require(reader.receipt(reference) == target.inspection, "hold", "held evidence expired")

            writer.hold(attempt, "qualification", release=True)
            holds.remove(attempt)
            store.sweep()
            missing_receipt = status_is(404, lambda: reader.receipt(reference), "deletion")
            missing_attempt = status_is(404, lambda: reader.inspect(attempt), "deletion")
            manifest = snapshot(writer)
            tombstones = [item for item in manifest["items"]
                          if item["attempt_id"] == attempt and item["kind"] == "tombstone"]
            require(len(tombstones) == 1, "deletion", "tombstone is absent or duplicated in recovery")
            tombstone = json.loads(_recovery_bytes(writer, tombstones[0], collect=True))
        finally:
            store.clock = clock
            for held_attempt in holds:
                writer.hold(held_attempt, "qualification", release=True)
        released = {}
        manifest = snapshot(writer)
        for item in manifest["items"]:
            if item["kind"] == "hold" and item["attempt_id"] in holds:
                state = json.loads(_recovery_bytes(writer, item, collect=True))
                stamp = datetime.fromisoformat(state["last_released_at"])
                require(not state["holds"] and stamp <= clock(), "hold",
                        "surviving evidence was released outside the real clock")
                released[item["attempt_id"]] = state["last_released_at"]
        require(set(released) == set(holds), "hold", "surviving release records are absent")
    return {"held_receipt": reference, "advanced_clock": (expiry + store.policy.purge_window).isoformat(),
            "receipt_http": missing_receipt, "inspect_http": missing_attempt,
            "tombstone": tombstones[0] | {"record": tombstone}, "surviving_releases": released}


def verify_lost_claim(call, clients, arm_loss, launch_count):
    request = call.arguments["request_id"]
    before = launch_count()
    with arm_loss() as lost:
        result = call.invoke("cli", clients)
    require(lost() and result.get("code") == "UNCERTAIN_OUTCOME" and result.get("ok") is False
            and launch_count() == before, "lost_claim_no_spawn", "durable claim loss did not suppress launch")
    with clients.runner.client.session(clients.tenant) as writer:
        manifest = snapshot(writer)
        starts = [item for item in manifest["items"] if item["kind"] == "start"]
        pending = []
        for item in starts:
            page = writer.recovery_item(item["item"])
            require(sha256(page.data).hexdigest() == item["sha256"] and len(page.data) == item["bytes"],
                    "recovery", "recovered start bytes differ")
            start = json.loads(page.data)
            if start.get("external_process", {}).get("request_id") == request:
                pending.append(start)
    require(len(pending) == 1, "recovery", "the unresolved start is absent or duplicated")
    with clients.inspector.client.session(clients.tenant) as reader:
        view = reader.inspect(pending[0]["attempt_id"])
    require(view["state"] == "unknown" and view["launch_claimed"] is True
            and view["terminal"] is None and view.get("receipt_ref") is None and view.get("artifact_ref") is None
            and view["start"] == pending[0],
            "recovery", "lost claim created a terminal or changed its start")
    retry = call.invoke("mcp", clients)
    require(retry.get("code") == "UNCERTAIN_OUTCOME" and retry.get("ok") is False and launch_count() == before,
            "lost_claim_retry", "identical cross-face retry launched or changed the outcome")
    with clients.runner.client.session(clients.tenant) as writer:
        ack = {key: view[key] for key in ("attempt_id", "start_sha256")}
        ack["request_sha256"] = view["admission"]["request_sha256"]
        status_is(409, lambda: writer.claim(ack), "lost_claim_retry")
    return view


def check_comparison(reference, candidate, compared):
    sources = (reference.inspection["attempt_id"], candidate.inspection["attempt_id"])
    view = compared.inspection
    require(sources[0] != sources[1] and view["start"].get("operation") == "compare"
            and "execution" not in view["terminal"] and view["terminal"]["verdict"]["result"] == "match"
            and all(sources == (record["reference_attempt_id"], record["candidate_attempt_id"])
                    for record in (view["admission"], view["start"])),
            "comparison", "readback does not bind both ordered sources and the expected match")
    return {"source_attempts": list(sources), "verdict": view["terminal"]["verdict"]["result"],
            "launch_claimed": False}


def cross_tenant_denials(clients, control_clients, record, own):
    denials = {}
    for label, endpoint, tenant, item in (
        ("denial_to_primary", control_clients.inspector, control_clients.tenant, record),
        ("denial_on_primary_tenant", control_clients.inspector, clients.tenant, record),
        ("primary_to_control", clients.inspector, clients.tenant, own),
        ("primary_on_control_tenant", clients.inspector, control_clients.tenant, own),
    ):
        view = item.inspection
        with endpoint.client.session(tenant) as denied:
            attempt = view["attempt_id"]
            check = "cross_tenant_denial"
            inspect_http = status_is(404, lambda: denied.inspect(attempt), check)
            receipt_http = status_is(404, lambda: denied.receipt(view["receipt_ref"]), check)
            artifact = view["terminal"]["artifact"]
            objects = item.index["objects"]
            require(bool(objects), check, "the retained positive control has no objects to probe")
            denials[label] = {"inspect_http": inspect_http, "receipt_http": receipt_http,
                              "artifact_http": status_is(404, lambda: denied.artifact(
                                  attempt, view["artifact_ref"], length=artifact["bytes"],
                                  digest=artifact["sha256"],
                              ), check),
                              "object_http": status_is(404, lambda: denied.object_page(
                                  attempt, objects[0]["object_ref"],
                              ), check)}
    return denials


def execute_controls(clients, control_clients, plan, store, arm_loss):
    initial_launches = plan.launch_count()
    record = inspect_reply(clients, plan.record.invoke("cli", clients), "record", "reference")
    require(plan.launch_count() == initial_launches + 1,
            "reference", "the independent launch witness did not observe the reference")
    replay = plan.replay(record.inspection["receipt_ref"])
    candidate = inspect_reply(clients, replay.invoke("mcp", clients), "replay", "candidate")
    require(plan.launch_count() == initial_launches + 2,
            "candidate", "the independent launch witness did not observe the replay")
    compare = plan.compare(record.inspection["receipt_ref"], candidate.inspection["receipt_ref"])
    compared = inspect_reply(clients, compare.invoke("mcp", clients), "compare", "comparison")
    comparison = check_comparison(record, candidate, compared)
    require(plan.launch_count() == initial_launches + 2, "comparison", "host comparison spawned a child")

    published = (record, candidate, compared)
    with clients.runner.client.session(clients.tenant) as writer:
        manifest = snapshot(writer)
    claims = {item["attempt_id"] for item in manifest["items"] if item["kind"] == "launch_claim"}
    require(claims == {record.inspection["attempt_id"], candidate.inspection["attempt_id"]},
            "comparison", "durable launch claims do not match the two external executions")
    pending = verify_lost_claim(plan.lost_claim, clients, arm_loss, plan.launch_count)
    disposable = inspect_reply(clients, plan.expiry_control.invoke("cli", clients), "record", "deletion_target")
    require(disposable.inspection["attempt_id"] not in {entry.inspection["attempt_id"] for entry in published},
            "deletion_target", "expiry must exercise a distinct attempt")
    deletion = deletion_control(store, clients, published, disposable)
    own = inspect_reply(control_clients, plan.denial_control.invoke("cli", control_clients),
                        "record", "denial_own_tenant")
    require(plan.observations(control_clients, own) == plan.changed_expected,
            "denial_own_tenant", "changed tape did not produce the independently expected result")
    receipts = (*published, own)
    ports = (clients, clients, clients, control_clients)
    after_deletion = [unchanged(port, item) for port, item in zip(ports, receipts, strict=True)]

    expected = start_retry(clients, record)
    retries = finish_retries(clients, record)
    after_retries = [unchanged(port, item) for port, item in zip(ports, receipts, strict=True)]
    denials = cross_tenant_denials(clients, control_clients, record, own)
    return receipts, pending, {
        "reference_cli": {"receipt_ref": after_retries[0], "face": "cli"},
        "candidate_mcp": {"receipt_ref": after_retries[1], "face": "mcp"},
        "comparison_mcp": {"receipt_ref": after_retries[2], "face": "mcp", **comparison},
        "denial_own_tenant": {"receipt_ref": after_retries[3], "tenant": control_clients.tenant},
        "cross_tenant_denial": denials,
        "exact_start_retry": {"ack": expected, "inspector_receipt": after_retries[0]},
        "exact_finish_retry": {"ack": retries["exact"], "inspector_receipts": after_retries},
        "conflicting_finish_retry": {"http": retries["conflict_http"], "inspector_receipts": after_retries},
        "lost_claim_no_spawn": {"launch_delta": 0, "state": pending["state"], "reply": "UNCERTAIN_OUTCOME"},
        "lost_claim_retry": {"face": "mcp", "launch_delta": 0, "reply": "UNCERTAIN_OUTCOME"},
        "hold": {key: deletion[key] for key in ("held_receipt", "advanced_clock")},
        "deletion": {**deletion, "inspector_receipts": after_deletion},
    }


def claim_reply_loss():
    """Inject an acknowledgement failure only after the store commits the claim."""
    storage_failure = import_tools("retention_service.store").StorageFailure
    armed = triggered = False

    def fault(operation, stage):
        nonlocal triggered
        if armed and not triggered and (operation, stage) == ("claim", "before_ack"):
            triggered = True
            raise storage_failure("qualification dropped the durable claim acknowledgement")

    @contextmanager
    def arm():
        nonlocal armed, triggered
        armed, triggered = True, False
        try:
            yield lambda: triggered
        finally:
            armed = False

    return fault, arm


def qualify_host(server, pins, scope, root, verify_readiness, revisions, *, reopen):
    """Qualify the isolated authority and verify the byte graph after a server restart."""
    from .workload import QualificationWorkload

    local = load_local_retention(root / "association.sqlite3")
    control = local.control_clients(root / "control-association.sqlite3")
    fault, arm_loss = claim_reply_loss()
    with server.open_local_server(witness_module=pins["witness_module"], fault=fault) as service:
        verify_readiness(local)
        plan = QualificationWorkload(root, local.clients, control)
        retained, pending, controls = execute_controls(local.clients, control, plan, service.backend.store, arm_loss)
        with local.clients.runner.client.session(local.clients.tenant) as writer:
            baseline = snapshot(writer)
    ports = (local.clients, local.clients, local.clients, control)
    with reopen(root, pins["witness_module"], local) as process:
        verify_readiness(local)
        reopened_start = start_retry(local.clients, retained[0])
        reopened_finish = finish_retries(local.clients, retained[0])
        reopened_refs = [unchanged(port, item) for port, item in zip(ports, retained, strict=True)]
        objects = []
        for port, item in zip(ports, retained, strict=True):
            encoded = {}
            with port.inspector.client.session(port.tenant) as reader:
                for entry in item.index["objects"]:
                    require(entry["bytes"] <= projection.RECORD_LIMIT, "readback",
                            "sealed qualification object exceeds the receipt bound")
                    raw = read_object(reader, item.inspection["attempt_id"], entry, collect=True)
                    encoded[entry["object_ref"]] = b64encode(raw).decode("ascii")
            objects.append(encoded)
        with local.clients.inspector.client.session(local.clients.tenant) as reader:
            require(reader.inspect(pending["attempt_id"]) == pending, "recovery",
                    "reopened unresolved claim differs")
        with local.clients.runner.client.session(local.clients.tenant) as writer:
            recovered = snapshot(writer)

    def inventory_identity(manifest):
        def stable(value):
            if isinstance(value, list):
                return [stable(item) for item in value]
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items() if key != "item"}
            return value

        return stable({key: manifest[key] for key in ("attempts", "items", "policy_revision",
                                                       "deletion_high_water", "tenant")})

    require(inventory_identity(recovered) == inventory_identity(baseline), "recovery",
            "restart changed the verified recovery inventory")
    controls["recovery"] = {"inspector_receipts": reopened_refs, "manifest": recovered,
                            "manifest_sha256": projection.record_digest(recovered),
                            "unresolved_attempt_id": pending["attempt_id"], "service_process": process,
                            "start_retry": reopened_start, "finish_retries": reopened_finish}
    inventory = {row["attempt_id"]: row for row in recovered["attempts"]}
    by_attempt = {item.inspection["attempt_id"]: item.inspection for item in retained}
    receipts = []
    for item, encoded in zip(retained, objects, strict=True):
        view = item.inspection
        primary = inventory.get(view["attempt_id"])
        lineage = primary["source_lineage"] if primary is not None else []
        if view["admission"]["operation"] == "compare":
            require({row["side"] for row in lineage} == {"reference", "candidate"} and len(lineage) == 2,
                    "recovery", "comparison source lineage is incomplete")
            for row in lineage:
                source_attempt = view["admission"][row["side"] + "_attempt_id"]
                source = by_attempt[source_attempt]
                require(row["source_attempt_id"] == source_attempt
                        and row["source_sha256"] == source["terminal"]["artifact"]["sha256"]
                        and row["source_terminal_sha256"] == source["terminal_sha256"],
                        "recovery", "comparison source lineage does not resolve to verified source bytes")
        receipts.append({key: view[key] for key in (
            "attempt_id", "tenant", "receipt_ref", "artifact_ref", "start_sha256", "terminal_sha256",
            "admission", "start", "terminal",
        )} | {
            "outer_receipt": _retained_outer_receipt(item.profile, item),
            "artifact_sha256": sha256(item.artifact).hexdigest(), "index": item.index,
            "objects": item.index["objects"], "object_bytes_b64": encoded,
            "lineage": lineage, "lineage_sha256": projection.record_digest(lineage),
        })
    return {
        "schema": "rea.retention.qualification/v1",
        "revisions": {**revisions, "policy": local.clients.policy_revision, "acl": local.clients.acl_revision},
        "pins": {"route": pins["successor_route"], "profile": pins["profile"],
                 "record": projection.V03_SHA, "index": projection.INDEX_SHA,
                 "witness_module": pins["witness_module"]},
        "scope": scope,
        "controls": {name: {"verdict": "pass", "evidence": value} for name, value in controls.items()},
        "live_receipts": receipts,
        "unresolved_claim": {key: pending[key] for key in (
            "attempt_id", "start", "start_sha256", "state", "launch_claimed", "admission",
        )},
    }
