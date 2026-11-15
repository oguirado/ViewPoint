# @module {"id":"wa.perceive.holdings_abstention","layer":2,"owner":"PERCEIVE","deps":[],"book":"HB-WA-s1-perceive"}
"""J8 Holdings: closed, source-agnostic, RAM-only evidence corroboration.

Adapter authors supply independent UI-tree, calibrated hook telemetry, capture
receipts and complete observer records. This module NEVER performs CDP/IPC IO,
launches a process, takes a screenshot, writes a file, or serializes a frame.
No framebuffer, opaque ticket, DOM dump, raw receipt or model rationale is
retained in a result or in the per-lane temporal index.
"""

from dataclasses import dataclass
import hashlib
import re
from typing import Mapping

import numpy as np


CORROBORATED = "CORROBORATED"
ABSTAIN = "ABSTAIN"
MISMATCH = "MISMATCH"

# Closed codes are operationally distinct. A foreign/unknown code is NOT proof.
FAILURE_CODES = frozenset({
    "FUENTE_DESCONOCIDA", "CAPTURA_NO_SOPORTADA", "CAPTURA_NO_VERIFICADA",
    "VINCULO_INVALIDO", "COMPROMISO_AUSENTE", "FRAME_OBSOLETO",
    "FRAME_OMITIDO", "GEOMETRIA_DESCONOCIDA", "INFORMACION_OCULTA",
    "ESTRUCTURA_AUSENTE", "ESTRUCTURA_DIVERGENTE", "ERROR_ESTRUCTURA",
    "TELEMETRY_AUSENTE", "TELEMETRIA_DIVERGENTE", "ERROR_TELEMETRIA",
    "TOLERANCIA_AUSENTE", "RESIDENCIA_NO_OBSERVADA",
    "RESIDENCIA_DIVERGENTE", "ERROR_RESIDENCIA", "EVIDENCIA_INVALIDA",
    "CODIGO_DESCONOCIDO",
})

_TARGET_KINDS = frozenset({"cdp_tab", "x11_window", "xvfb_screen", "capsule", "godot_window"})
_REQUIRED_OBSERVERS = ("procfs", "cgroup", "write_spy")
_REF = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z", re.ASCII)
_IDENT = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}\Z", re.ASCII)
_SHA = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class CorroborationResult:
    """Exactly eight approved fields; deliberately cannot carry raw evidence."""

    verdict: str
    failure_code: str | None
    tree_ref: str | None
    frame_ref: str | None
    commit_ref: str | None
    structure_checked: bool
    telemetry_checked: bool
    residency_checked: bool

    def metadata(self):
        """The ONLY supported persistence representation (bounded scalars)."""
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def describe_options():
    """Strict public describe contract; artifact emission cannot be enabled."""
    return {"save_artifacts": False, "save_frames": False,
            "include_pixels": False, "include_raw_receipts": False,
            "include_tree_dump": False, "output_path": None}


def _uint(value):
    return type(value) is int and value >= 0


def _positive(value):
    return type(value) is int and value > 0


def _ref(value):
    return value if isinstance(value, str) and _REF.fullmatch(value) else None


def _binding(handle):
    """Structural JQ2-A Handle or mapping, never a sibling JQ2-B import."""
    if isinstance(handle, Mapping):
        kind, ident, generation = (handle.get(k) for k in
                                   ("kind", "canonical_id", "generation"))
    else:
        kind, ident, generation = (getattr(handle, k, None) for k in
                                   ("kind", "canonical_id", "generation"))
    if (kind not in _TARGET_KINDS or not isinstance(ident, str)
            or not _IDENT.fullmatch(ident) or not _uint(generation)):
        return None
    return kind, ident, generation


def _capture_binding(record, frame):
    """Exact target generation, epoch, sequence, DPR, screen and query fence."""
    if not isinstance(record, Mapping):
        return False
    return (record.get("handle") is not None
            and _binding(record.get("handle")) == _binding(frame.get("handle"))
            and all(record.get(k) == frame.get(k) for k in
                    ("epoch", "frame_seq", "dpr_ppm", "screen_id", "query_id")))


def _safe_packet_refs(packet):
    if not isinstance(packet, Mapping):
        return None, None, None
    tree = packet.get("tree")
    frame = packet.get("frame")
    committed = packet.get("committed")
    return (_ref(tree.get("tree_ref")) if isinstance(tree, Mapping) else None,
            _ref(frame.get("frame_ref")) if isinstance(frame, Mapping) else None,
            _ref(committed.get("commit_ref")) if isinstance(committed, Mapping) else None)


def _frame_geometry(frame):
    if (not _positive(frame.get("width")) or not _positive(frame.get("height"))
            or not _positive(frame.get("dpr_ppm"))):
        return False
    rgb = frame.get("rgb")
    if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
            or rgb.shape != (frame["height"], frame["width"], 3)
            or not rgb.flags.c_contiguous):
        return False
    digest = frame.get("digest")
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        return False
    # Read-only in-RAM inspection; digest is only an integrity fence, never a
    # substitute for independent semantic structure/telemetry corroboration.
    return hashlib.sha256(rgb.tobytes(order="C")).hexdigest() == digest


def _command_commit(packet, frame):
    command, outcome, committed = (packet.get(k) for k in
                                   ("command", "outcome", "committed"))
    if not all(isinstance(v, Mapping) for v in (command, outcome, committed)):
        return False
    if (not _uint(command.get("tick")) or not _uint(command.get("seq"))
            or command.get("cmd") != "pause" or command.get("args") != {}):
        return False
    if (not _uint(outcome.get("seq")) or not _uint(outcome.get("requested_tick"))
            or not _uint(outcome.get("applied_tick"))
            or not _uint(outcome.get("order_seq_id"))
            or outcome["seq"] != command["seq"]
            or outcome["requested_tick"] != command["tick"]
            or outcome["applied_tick"] < command["tick"]
            or outcome.get("status") != "applied"
            or outcome.get("reason") != "ok"
            or not isinstance(outcome.get("state_sha256"), str)
            or not _SHA.fullmatch(outcome["state_sha256"])):
        return False
    if (committed.get("committed") is not True
            or not _capture_binding(committed, frame)
            or _ref(committed.get("commit_ref")) is None
            or committed.get("state_sha256") != outcome["state_sha256"]
            or committed.get("order_seq_id") != outcome["order_seq_id"]
            or committed.get("seq") != outcome["seq"]
            or committed.get("applied_tick") != outcome["applied_tick"]
            or frame.get("commit_ref") != committed.get("commit_ref")
            or not _uint(frame.get("capture_tick"))
            or frame["capture_tick"] < outcome["applied_tick"]):
        return False
    return True


def _source_policy(packet, frame):
    source = packet.get("source")
    if not isinstance(source, Mapping) or source.get("kind") not in ("cdp", "ipc"):
        return "FUENTE_DESCONOCIDA"
    if source.get("capture_supported") is not True:
        return "CAPTURA_NO_SOPORTADA"
    kind = source["kind"]
    if (kind == "cdp" and _binding(frame.get("handle"))[0] != "cdp_tab") or (
            kind == "ipc" and _binding(frame.get("handle"))[0] == "cdp_tab"):
        return "CAPTURA_NO_VERIFICADA"
    receipt = source.get("receipt")
    # A source label or `harvested=True` flag alone cannot establish provenance.
    # The backend must inject a trusted, volatile verifier for its own capture.
    verifier = source.get("verify_capture")
    if not callable(verifier):
        return "CAPTURA_NO_VERIFICADA"
    try:
        if verifier(receipt) is not True:
            return "CAPTURA_NO_VERIFICADA"
    except Exception:
        return "CAPTURA_NO_VERIFICADA"
    if (not _capture_binding(receipt, frame)
            or receipt.get("observed") is not True
            or receipt.get("frame_ref") != frame.get("frame_ref")
            or receipt.get("kind") != ("cdp_harvest" if kind == "cdp" else "brokered_ipc")):
        return "CAPTURA_NO_VERIFICADA"
    if kind == "cdp":
        # Only a CDP adapter's positive harvest attestation may claim CDP.
        if receipt.get("harvested") is not True:
            return "CAPTURA_NO_VERIFICADA"
    else:
        # Independent IPC session, trusted stream and ticket attestations.
        stream, ticket, session = (source.get(k) for k in
                                   ("trusted_stream", "ticket", "session"))
        if not all(isinstance(x, Mapping) for x in (stream, ticket, session)):
            return "CAPTURA_NO_VERIFICADA"
        if (stream.get("verified") is not True
                or ticket.get("verified") is not True
                or session.get("verified") is not True
                or any(_ref(x.get("id")) is None for x in (stream, ticket, session))
                or receipt.get("stream_id") != stream["id"]
                or receipt.get("ticket_id") != ticket["id"]
                or receipt.get("session_id") != session["id"]
                or any(not _capture_binding(x, frame) for x in
                       (stream, ticket, session))):
            return "CAPTURA_NO_VERIFICADA"
    return None


def _box(value):
    return (isinstance(value, (tuple, list)) and len(value) == 4
            and all(_uint(x) for x in value) and value[2] > 0 and value[3] > 0)


def _structure(packet, frame, tolerance, resolver):
    tree, spec = packet.get("tree"), packet.get("expected_structure")
    if not isinstance(tree, Mapping) or tree.get("observed") is not True:
        return "ESTRUCTURA_AUSENTE"
    if (tree.get("hidden_information") is True or frame.get("hidden_information") is True
            or isinstance(spec, Mapping) and spec.get("hidden_information") is True):
        return "INFORMACION_OCULTA"
    if not isinstance(spec, Mapping) or spec.get("origin") != "episode_spec":
        return "ESTRUCTURA_AUSENTE"
    if (not _capture_binding(tree, frame) or not _capture_binding(spec, frame)
            or tree.get("origin") != "independent_ui_tree"
            or tree.get("frame_ref") != frame.get("frame_ref")
            or tree.get("commit_ref") != frame.get("commit_ref")
            or spec.get("commit_ref") != frame.get("commit_ref")
            or _ref(tree.get("tree_ref")) is None):
        return "ESTRUCTURA_DIVERGENTE"
    actual, expected = tree.get("nodes"), spec.get("nodes")
    if (not isinstance(actual, list) or not isinstance(expected, list)
            or not expected or not actual):
        return "ESTRUCTURA_AUSENTE"
    # A known framebuffer plus DPR bounds the visible CSS viewport. Neither
    # an out-of-viewport AX rectangle nor a matching forged target can stand
    # in for observed geometry.
    css_width = frame["width"] * 1000000 // frame["dpr_ppm"]
    css_height = frame["height"] * 1000000 // frame["dpr_ppm"]
    if css_width < 1 or css_height < 1:
        return "GEOMETRIA_DESCONOCIDA"
    def visible_box(box):
        return (box[0] + box[2] <= css_width and
                box[1] + box[3] <= css_height)
    all_nodes = {}
    for node in actual:
        if (not isinstance(node, Mapping) or _ref(node.get("id")) is None
                or _ref(node.get("role")) is None or not _box(node.get("box"))
                or node.get("hidden") not in (True, False)
                or node["id"] in all_nodes):
            return "ESTRUCTURA_DIVERGENTE"
        if not visible_box(node["box"]):
            return "ESTRUCTURA_DIVERGENTE"
        all_nodes[node["id"]] = node
    for target in expected:
        if (not isinstance(target, Mapping) or _ref(target.get("id")) is None
                or _ref(target.get("role")) is None or not _box(target.get("box"))):
            return "ESTRUCTURA_AUSENTE"
        if not visible_box(target["box"]):
            return "ESTRUCTURA_DIVERGENTE"
        found = all_nodes.get(target["id"])
        if found is None:
            return "ESTRUCTURA_DIVERGENTE"
        if found.get("hidden") is True:
            return "INFORMACION_OCULTA"
        if (found["role"] != target["role"] or any(
                abs(a - b) > tolerance for a, b in zip(found["box"], target["box"]))):
            return "ESTRUCTURA_DIVERGENTE"
    if resolver is not None:
        # A trusted integration may perform additional, independent structure
        # checks. Never accept a bare boolean or a self-asserted hash equality.
        verdict = resolver(tree, spec, geometry_tolerance_raw=tolerance)
        if (not isinstance(verdict, Mapping) or verdict.get("checked") is not True
                or verdict.get("independent") is not True):
            return "ESTRUCTURA_AUSENTE"
        if verdict.get("matched") is not True:
            return "ESTRUCTURA_DIVERGENTE"
    return None


def _telemetry(packet, frame, metric_limits):
    hook, target = packet.get("telemetry"), packet.get("expected_telemetry")
    if not isinstance(hook, Mapping) or hook.get("observed") is not True:
        return "TELEMETRY_AUSENTE"
    if not isinstance(target, Mapping) or target.get("origin") != "calibrated_spec":
        return "TELEMETRY_AUSENTE"
    if (not _capture_binding(hook, frame) or not _capture_binding(target, frame)
            or hook.get("origin") != "trusted_hook"
            or hook.get("commit_ref") != frame.get("commit_ref")
            or target.get("commit_ref") != frame.get("commit_ref")):
        return "TELEMETRIA_DIVERGENTE"
    values, expected = hook.get("samples"), target.get("samples")
    if not isinstance(values, Mapping) or not isinstance(expected, Mapping) or not expected:
        return "TELEMETRY_AUSENTE"
    if not isinstance(metric_limits, Mapping):
        return "TOLERANCIA_AUSENTE"
    for name, desired in expected.items():
        if (_ref(name) is None or not isinstance(desired, Mapping)
                or not _uint(desired.get("value")) or _ref(desired.get("unit")) is None):
            return "TELEMETRY_AUSENTE"
        observed, limit = values.get(name), metric_limits.get(name)
        if not isinstance(observed, Mapping) or not _uint(observed.get("value")):
            return "TELEMETRY_AUSENTE"
        if (not isinstance(limit, Mapping) or not _positive(limit.get("absolute"))
                or limit.get("unit") != desired["unit"]):
            return "TOLERANCIA_AUSENTE"
        if (observed.get("unit") != desired["unit"] or
                abs(observed["value"] - desired["value"]) > limit["absolute"]):
            return "TELEMETRIA_DIVERGENTE"
    return None


def _residency(packet, frame, limits):
    evidence = packet.get("residency")
    if (not isinstance(evidence, Mapping) or evidence.get("observed") is not True
            or not _capture_binding(evidence, frame)):
        return "RESIDENCIA_NO_OBSERVADA"
    observers = evidence.get("observers")
    if not isinstance(observers, Mapping):
        return "RESIDENCIA_NO_OBSERVADA"
    measurements = {
        "procfs": ("resident_bytes", "swap_bytes"),
        "cgroup": ("memory_current_bytes", "swap_current_bytes"),
        "write_spy": ("persisted_bytes", "artifact_count"),
    }
    for name in _REQUIRED_OBSERVERS:
        row = observers.get(name)
        if (not isinstance(row, Mapping) or row.get("source") != name
                or row.get("observed") is not True
                or row.get("verified") is not True or not _uint(row.get("at_tick"))
                or not frame["capture_tick"] <= row["at_tick"] <= packet["now_tick"]):
            return "RESIDENCIA_NO_OBSERVADA"
        if any(not _uint(row.get(k)) for k in measurements[name]):
            return "RESIDENCIA_NO_OBSERVADA"
    p, c, w = (observers[n] for n in _REQUIRED_OBSERVERS)
    if (p["resident_bytes"] > limits["max_procfs_resident_bytes"]
            or c["memory_current_bytes"] > limits["max_cgroup_memory_bytes"]
            or max(p["swap_bytes"], c["swap_current_bytes"]) > limits["max_swap_bytes"]
            or w["persisted_bytes"] > limits["max_persisted_bytes"]
            or w["artifact_count"] > limits["max_artifact_count"]):
        return "RESIDENCIA_DIVERGENTE"
    return None


class HoldingsAbstention:
    """Fail-closed correlator with per-target+epoch+source temporal isolation.

    evaluate() never selects last-good evidence. It returns an immutable record;
    the only retained lane state is an integer frame sequence. An exception in a
    structural integration callback resets the current lane, not other lanes.
    """

    def __init__(self):
        self._last_sequence = {}

    def reset_lane(self, *, handle, epoch, source_kind):
        key = _binding(handle)
        if key is None or not _uint(epoch) or source_kind not in ("cdp", "ipc"):
            raise ValueError("invalid lane identity")
        self._last_sequence.pop((source_kind, *key, epoch), None)

    def evaluate(self, packet, *, structure_resolver=None):
        tree_ref, frame_ref, commit_ref = _safe_packet_refs(packet)
        flags = [False, False, False]
        lane = None

        def finish(verdict, code=None):
            if verdict not in (CORROBORATED, ABSTAIN, MISMATCH) or (
                    verdict == CORROBORATED and code is not None) or (
                    verdict != CORROBORATED and code not in FAILURE_CODES):
                verdict, code = ABSTAIN, "CODIGO_DESCONOCIDO"
            return CorroborationResult(verdict, code, tree_ref, frame_ref,
                                       commit_ref, *flags)

        try:
            if not isinstance(packet, Mapping):
                return finish(ABSTAIN, "EVIDENCIA_INVALIDA")
            frame = packet.get("frame")
            if not isinstance(frame, Mapping):
                return finish(ABSTAIN, "VINCULO_INVALIDO")
            key = _binding(frame.get("handle"))
            source = packet.get("source")
            kind = source.get("kind") if isinstance(source, Mapping) else None
            if key is None or not _uint(frame.get("epoch")) or not _uint(frame.get("frame_seq")):
                return finish(ABSTAIN, "VINCULO_INVALIDO")
            if kind in ("cdp", "ipc"):
                lane = (kind, *key, frame["epoch"])
            if (not _positive(frame.get("dpr_ppm"))
                    or _ref(frame.get("frame_ref")) is None
                    or _ref(frame.get("commit_ref")) is None
                    or _ref(frame.get("screen_id")) is None
                    or _ref(frame.get("query_id")) is None):
                return finish(ABSTAIN, "VINCULO_INVALIDO")
            if not _frame_geometry(frame):
                return finish(ABSTAIN, "GEOMETRIA_DESCONOCIDA")
            if frame.get("hidden_information") is True:
                return finish(ABSTAIN, "INFORMACION_OCULTA")
            if frame.get("skipped") is True:
                return finish(ABSTAIN, "FRAME_OMITIDO")
            if frame.get("stale") is True:
                return finish(ABSTAIN, "FRAME_OBSOLETO")
            if (frame.get("skipped") is not False or frame.get("stale") is not False):
                return finish(ABSTAIN, "FRAME_OMITIDO")
            if not _command_commit(packet, frame):
                return finish(ABSTAIN, "COMPROMISO_AUSENTE")
            source_error = _source_policy(packet, frame)
            if source_error:
                return finish(ABSTAIN, source_error)
            limits = packet.get("limits")
            names = ("max_frame_age_ticks", "max_procfs_resident_bytes",
                     "max_cgroup_memory_bytes", "max_swap_bytes",
                     "max_persisted_bytes", "max_artifact_count")
            if (not isinstance(limits, Mapping)
                    or any(not _uint(limits.get(k)) for k in names)
                    or not _uint(packet.get("now_tick"))):
                return finish(ABSTAIN, "EVIDENCIA_INVALIDA")
            if (packet["now_tick"] < frame["capture_tick"] or
                    packet["now_tick"] - frame["capture_tick"] > limits["max_frame_age_ticks"]):
                return finish(ABSTAIN, "FRAME_OBSOLETO")
            if lane in self._last_sequence:
                last = self._last_sequence[lane]
                if frame["frame_seq"] <= last:
                    return finish(ABSTAIN, "FRAME_OBSOLETO")
                if frame["frame_seq"] != last + 1:
                    return finish(ABSTAIN, "FRAME_OMITIDO")
            tolerances = packet.get("tolerances")
            if (not isinstance(tolerances, Mapping)
                    or not _positive(tolerances.get("geometry_css_px"))
                    or not isinstance(tolerances.get("metrics"), Mapping)):
                return finish(ABSTAIN, "TOLERANCIA_AUSENTE")
            structure_error = _structure(packet, frame, tolerances["geometry_css_px"],
                                         structure_resolver)
            flags[0] = structure_error not in ("ESTRUCTURA_AUSENTE", "INFORMACION_OCULTA")
            if structure_error:
                if structure_error == "INFORMACION_OCULTA":
                    return finish(ABSTAIN, structure_error)
                return finish(MISMATCH if structure_error == "ESTRUCTURA_DIVERGENTE"
                              else ABSTAIN, structure_error)
            flags[0] = True
            telemetry_error = _telemetry(packet, frame, tolerances["metrics"])
            flags[1] = telemetry_error not in ("TELEMETRY_AUSENTE", "TOLERANCIA_AUSENTE")
            if telemetry_error:
                return finish(MISMATCH if telemetry_error == "TELEMETRIA_DIVERGENTE"
                              else ABSTAIN, telemetry_error)
            flags[1] = True
            residency_error = _residency(packet, frame, limits)
            flags[2] = residency_error not in ("RESIDENCIA_NO_OBSERVADA",)
            if residency_error:
                return finish(MISMATCH if residency_error == "RESIDENCIA_DIVERGENTE"
                              else ABSTAIN, residency_error)
            flags[2] = True
            # Successful proof only; never cache failed or partial evidence.
            self._last_sequence[lane] = frame["frame_seq"]
            return finish(CORROBORATED)
        except Exception:
            # No untrusted exception text/receipt in persistent metadata. An
            # integration callback cannot poison another source/handle lane.
            if lane is not None:
                self._last_sequence.pop(lane, None)
            return finish(ABSTAIN, "EVIDENCIA_INVALIDA")


def corroborate(packet, *, structure_resolver=None):
    """Stateless convenience for a single captured Holdings episode."""
    return HoldingsAbstention().evaluate(packet, structure_resolver=structure_resolver)
