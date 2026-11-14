# @module {"id":"wa.learn.trace_determinism_verifier","layer":2,"owner":"LEARN","deps":["wa.learn.multiwin_scorecard","wa.learn.replay","wa.perceive.session_registry","wa.remember.skills","wa.remember.program_map","wa.state.diff","wa.state.schema"],"book":"HB-WA-s3-remember"}
"""Bit-exact complete-trace digest chain and cross-window bleed auditor.

Builds an explicit verifier-owned envelope chain over canonical target,
generation, epoch, step, action, outcome, and state digest without diagnostic
wall-clock fields. Adapts landed ``WindowEvidence`` rows through ``frame_seq``
and ``ops['event_index']`` rather than guessed step or checkpoint attributes.
Detects truncated or altered seal strings, reordered or duplicated steps,
foreign observations, altered or duplicate checkpoints, and incomplete reset
evidence while preserving unavailable metric denominators as ``None``.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import copy
from dataclasses import dataclass, fields
import hashlib
import importlib
import json
import re

from wender.learn import multiwin_scorecard as mws
from wender.learn import replay as replay_mod
from wender.perceive.session_registry import (
    FrameRef,
    FrameValidationError,
    Handle,
    Observation,
    PerceptionResult,
    RegistryError,
    SUPPORTED_KINDS,
)
from wender.remember import skills
from wender.remember.program_map import canonical_json
from wender.state import diff as state_diff
from wender.state.schema import dumps as schema_dumps


GENESIS_DIGEST = "0" * 64
_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_DIAGNOSTICS = frozenset(mws._DIAGNOSTICS)

__all__ = (
    "GENESIS_DIGEST",
    "TraceDeterminismError",
    "PrerequisiteAbsentError",
    "TraceEnvelope",
    "TraceChainLink",
    "TraceDigestChain",
    "BleedAuditReport",
    "load_sibling_deliverable",
    "canonical_target",
    "strip_diagnostics",
    "adapt_window_row",
    "build_trace_chain",
    "verify_envelope_chain",
    "audit_episode_log",
    "verify_replay_trace",
    "verify_admitted_skill_record",
)


class TraceDeterminismError(mws.EvidenceError):
    """Malformed, non-deterministic, or tampered trace evidence."""


class PrerequisiteAbsentError(TraceDeterminismError):
    """A pending sibling deliverable is AUSENTE in this snapshot."""


def load_sibling_deliverable(module_name: str):
    """Lazy-load a sibling deliverable; block on absence rather than fabricating APIs."""
    if not isinstance(module_name, str) or not module_name.strip():
        raise PrerequisiteAbsentError("sibling deliverable name is AUSENTE")
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise PrerequisiteAbsentError(f"sibling deliverable {module_name!r} is AUSENTE") from exc


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TraceDeterminismError(f"{name} must be an integer >= {minimum}")
    return value


def _nonempty_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise TraceDeterminismError(f"{name} must be a nonempty canonical string")
    value.encode("utf-8")
    return value


def _hex64(value: object, name: str) -> str:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise TraceDeterminismError(f"{name} must be 64 lowercase hex characters")
    return value


def canonical_target(target: object, generation: int | None = None) -> tuple[tuple[str, str], int]:
    """Normalize a Handle or target tuple into ((kind, canonical_id), generation)."""
    if hasattr(target, "kind") and hasattr(target, "canonical_id") and hasattr(target, "generation"):
        kind = target.kind
        cid = target.canonical_id
        gen = target.generation if generation is None else generation
        if generation is not None and target.generation != generation:
            raise TraceDeterminismError("handle generation conflicts with envelope generation")
    elif isinstance(target, (tuple, list)):
        if len(target) == 2:
            kind, cid = target
            gen = generation
        elif len(target) == 3:
            kind, cid, tuple_gen = target
            if generation is not None and tuple_gen != generation:
                raise TraceDeterminismError("target tuple generation conflicts with envelope generation")
            gen = tuple_gen
        else:
            raise TraceDeterminismError("target tuple must have (kind, canonical_id[, generation])")
    else:
        raise TraceDeterminismError("target must be a Handle or (kind, canonical_id) tuple")

    if kind not in SUPPORTED_KINDS:
        raise TraceDeterminismError("unsupported handle kind")
    _nonempty_str(cid, "canonical_id")
    gen_int = _exact_int(gen, "generation", minimum=1)
    try:
        validated = Handle(kind, cid, gen_int)
    except RegistryError as exc:
        raise TraceDeterminismError(str(exc)) from exc
    return (validated.kind, validated.canonical_id), validated.generation


def strip_diagnostics(value: object) -> object:
    """Recursively strip diagnostic wall-time and host fields and validate plain types."""
    if value is None or type(value) in (str, int, bool):
        if isinstance(value, str):
            value.encode("utf-8")
        return value
    if isinstance(value, Mapping):
        if any(type(k) is not str for k in value):
            raise TraceDeterminismError("canonical mapping keys must be strings")
        return {
            k: strip_diagnostics(v)
            for k, v in value.items()
            if k not in _DIAGNOSTICS
        }
    if isinstance(value, (tuple, list)):
        return [strip_diagnostics(v) for v in value]
    raise TraceDeterminismError("unsupported canonical value: " + type(value).__name__)


def _canonical_bytes(value: object) -> bytes:
    cleaned = strip_diagnostics(value)
    return json.dumps(
        cleaned,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_canonical(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _metric(numerator: int | None, denominator: int | None, passed: bool, **extra) -> dict:
    """Preserve unavailable denominators as None and fail when denominator is 0 or None."""
    if denominator is None or denominator == 0 or numerator is None:
        numerator_out, denominator_out, passed_out = None, None, False
    else:
        numerator_out = _exact_int(numerator, "numerator", minimum=0)
        denominator_out = _exact_int(denominator, "denominator", minimum=1)
        passed_out = bool(passed)
    return {
        "numerator": numerator_out,
        "denominator": denominator_out,
        "passed": passed_out,
        **extra,
    }


@dataclass(frozen=True)
class TraceEnvelope:
    """Explicit verifier-owned envelope for one multi-window or replay trace step."""

    target: tuple[str, str]
    generation: int
    epoch: int
    step: int
    action: dict | None
    outcome: dict
    state_digest: str

    def __post_init__(self) -> None:
        norm_target, norm_gen = canonical_target(self.target, self.generation)
        _exact_int(self.epoch, "epoch", minimum=0)
        _exact_int(self.step, "step", minimum=0)
        if self.action is not None:
            if not isinstance(self.action, Mapping):
                raise TraceDeterminismError("envelope action must be a mapping or None")
            cleaned_action = strip_diagnostics(dict(self.action))
        else:
            cleaned_action = None
        if not isinstance(self.outcome, Mapping):
            raise TraceDeterminismError("envelope outcome must be a mapping")
        cleaned_outcome = strip_diagnostics(dict(self.outcome))
        _hex64(self.state_digest, "state_digest")
        object.__setattr__(self, "target", norm_target)
        object.__setattr__(self, "generation", norm_gen)
        object.__setattr__(self, "action", cleaned_action)
        object.__setattr__(self, "outcome", cleaned_outcome)

    @property
    def handle(self) -> Handle:
        return Handle(self.target[0], self.target[1], self.generation)

    @property
    def handle_key(self) -> tuple[str, str, int]:
        return (self.target[0], self.target[1], self.generation)

    @property
    def frame_seq(self) -> int:
        seq = self.outcome.get("frame_seq", self.step)
        return _exact_int(seq, "frame_seq", minimum=0)

    @property
    def event_index(self) -> int:
        idx = self.outcome.get("event_index", self.step)
        return _exact_int(idx, "event_index", minimum=0)

    def to_canonical_dict(self) -> dict:
        return {
            "target": list(self.target),
            "generation": self.generation,
            "epoch": self.epoch,
            "step": self.step,
            "action": copy.deepcopy(self.action),
            "outcome": copy.deepcopy(self.outcome),
            "state_digest": self.state_digest,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_canonical_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class TraceChainLink:
    """One link in the bit-exact complete-trace digest chain."""

    index: int
    envelope: TraceEnvelope
    prev_digest: str
    envelope_digest: str
    chain_digest: str

    def __post_init__(self) -> None:
        _exact_int(self.index, "index", minimum=0)
        if not isinstance(self.envelope, TraceEnvelope):
            raise TraceDeterminismError("link envelope must be a TraceEnvelope")
        _hex64(self.prev_digest, "prev_digest")
        _hex64(self.envelope_digest, "envelope_digest")
        _hex64(self.chain_digest, "chain_digest")


@dataclass(frozen=True)
class TraceDigestChain:
    """Bit-exact complete-trace digest chain over a sequence of TraceEnvelopes."""

    links: tuple[TraceChainLink, ...]
    head_digest: str
    manifest_digest: str | None
    complete_trace_digest: str

    def __post_init__(self) -> None:
        links_tuple = tuple(self.links)
        for link in links_tuple:
            if not isinstance(link, TraceChainLink):
                raise TraceDeterminismError("links must contain TraceChainLink instances")
        _hex64(self.head_digest, "head_digest")
        if self.manifest_digest is not None:
            _hex64(self.manifest_digest, "manifest_digest")
        _hex64(self.complete_trace_digest, "complete_trace_digest")
        object.__setattr__(self, "links", links_tuple)

    @property
    def envelopes(self) -> tuple[TraceEnvelope, ...]:
        return tuple(link.envelope for link in self.links)


@dataclass(frozen=True)
class BleedAuditReport:
    """Result of complete-trace digest verification and cross-window bleed auditing."""

    passed: bool
    chain: TraceDigestChain
    scorecard: mws.Scorecard | None
    metrics: dict
    violations: tuple[str, ...]
    bleed_events: tuple[dict, ...]
    reset_evidence: dict
    absent: dict


def _null_state_digest(
    target: tuple[str, str],
    generation: int,
    epoch: int,
    frame_seq: int,
    event_index: int,
    *,
    dropped: bool,
    quarantined: bool,
) -> str:
    return _sha256_canonical(
        {
            "kind": "absent_observation_state",
            "target": list(target),
            "generation": generation,
            "epoch": epoch,
            "frame_seq": frame_seq,
            "event_index": event_index,
            "dropped": bool(dropped),
            "quarantined": bool(quarantined),
        }
    )


def adapt_window_row(row: mws.WindowEvidence) -> TraceEnvelope:
    """Adapt a landed WindowEvidence row into a TraceEnvelope via frame_seq and event_index."""
    if not isinstance(row, mws.WindowEvidence):
        raise TraceDeterminismError("row must be a WindowEvidence instance")
    mws._validate_row(row)
    target, generation = canonical_target(row.handle)
    epoch = _exact_int(row.epoch, "epoch", minimum=0)
    frame_seq = _exact_int(row.frame_seq, "frame_seq", minimum=0)

    if "event_index" not in row.ops:
        raise TraceDeterminismError("WindowEvidence.ops lacks required event_index")
    event_index = _exact_int(row.ops.get("event_index"), "event_index", minimum=0)

    raw_state_digest = row.ops.get("state_digest")
    dropped = bool(row.dropped)
    quarantined = bool(row.ops.get("quarantined", False))
    if raw_state_digest is None:
        if row.frame_supported and not dropped and not quarantined:
            raise TraceDeterminismError("supported active row lacks state_digest")
        state_digest = _null_state_digest(
            target,
            generation,
            epoch,
            frame_seq,
            event_index,
            dropped=dropped,
            quarantined=quarantined,
        )
        state_present = False
    else:
        state_digest = _hex64(raw_state_digest, "state_digest")
        state_present = True

    action_id = row.ops.get("action_id")
    if action_id is not None:
        _nonempty_str(action_id, "action_id")
        action_dict: dict | None = {
            "action_id": action_id,
            "admitted": bool(row.ops.get("admitted", False)),
            "sent": bool(row.ops.get("sent", False)),
            "charged_cost": _exact_int(row.ops.get("charged_cost", 0), "charged_cost", minimum=0),
            "resource_key": row.ops.get("resource_key"),
            "focus_domain": row.ops.get("focus_domain"),
            "scheduler_attempted": bool(row.ops.get("scheduler_attempted", False)),
            "dispatch": strip_diagnostics(row.ops.get("dispatch")),
        }
    else:
        action_dict = None

    claims_digest = row.ops.get("claims_digest")
    if claims_digest is not None:
        _hex64(claims_digest, "claims_digest")

    outcome_dict = {
        "scene": row.scene,
        "frame_seq": frame_seq,
        "event_index": event_index,
        "state_present": state_present,
        "terminal": row.terminal,
        "expected_terminal": row.expected_terminal,
        "frame_supported": row.frame_supported,
        "dropped": row.dropped,
        "drop_expected": bool(row.ops.get("drop_expected", False)),
        "exact": row.exact,
        "pixel_mismatches": _exact_int(row.ops.get("pixel_mismatches", 0), "pixel_mismatches", minimum=0),
        "torn": bool(row.ops.get("torn", False)),
        "tp": row.tp,
        "fp": row.fp,
        "fn": row.fn,
        "tn": row.tn,
        "counts": strip_diagnostics(row.ops.get("counts")),
        "expected_counts": strip_diagnostics(row.ops.get("expected_counts")),
        "isolated_confusion": strip_diagnostics(row.ops.get("isolated_confusion")),
        "isolated_match": row.ops.get("isolated_match"),
        "claims_digest": claims_digest,
        "dpr_ppm": row.ops.get("dpr_ppm"),
        "quarantined": quarantined,
        "stall_reason": row.ops.get("stall_reason"),
        "runtime_global_charged": row.ops.get("runtime_global_charged", 0),
        "runtime_target_charged": row.ops.get("runtime_target_charged", 0),
        "violations": list(row.violations),
    }

    return TraceEnvelope(
        target=target,
        generation=generation,
        epoch=epoch,
        step=event_index,
        action=action_dict,
        outcome=outcome_dict,
        state_digest=state_digest,
    )


def _compute_link_digest(index: int, prev_digest: str, envelope: TraceEnvelope) -> tuple[str, str]:
    env_digest = envelope.digest()
    chain_digest = _sha256_canonical(
        {
            "index": index,
            "prev_digest": prev_digest,
            "envelope_digest": env_digest,
            "envelope": envelope.to_canonical_dict(),
        }
    )
    return env_digest, chain_digest


def _compute_complete_trace_digest(
    head_digest: str,
    manifest_digest: str | None,
    link_count: int,
) -> str:
    return _sha256_canonical(
        {
            "version": "trace-determinism-chain-v1",
            "head_digest": head_digest,
            "manifest_digest": manifest_digest,
            "link_count": link_count,
        }
    )


def build_trace_chain(
    items: mws.TrialReport | Iterable[mws.WindowEvidence | TraceEnvelope],
    *,
    manifest: Mapping | None = None,
    preserve_input_order: bool = False,
) -> TraceDigestChain:
    """Build a bit-exact complete-trace digest chain from TrialReport, rows, or envelopes."""
    if isinstance(items, mws.TrialReport):
        raw_items = list(items.rows)
    else:
        raw_items = list(items)

    inferred_manifest = manifest
    envelopes: list[TraceEnvelope] = []
    all_window_rows = bool(raw_items) and all(isinstance(x, mws.WindowEvidence) for x in raw_items)

    if all_window_rows:
        rows = [x for x in raw_items if isinstance(x, mws.WindowEvidence)]
        if inferred_manifest is None:
            trial = rows[0].ops.get("_trial")
            if isinstance(trial, Mapping):
                inferred_manifest = trial
        if not preserve_input_order:
            rows = sorted(
                rows,
                key=lambda r: (
                    r.ops.get("event_index", -1)
                    if type(r.ops.get("event_index")) is int
                    else -1,
                    mws.handle_key(r.handle),
                    r.epoch,
                    r.frame_seq,
                ),
            )
        envelopes = [adapt_window_row(r) for r in rows]
    else:
        for item in raw_items:
            if isinstance(item, TraceEnvelope):
                envelopes.append(item)
            elif isinstance(item, mws.WindowEvidence):
                if inferred_manifest is None and isinstance(item.ops.get("_trial"), Mapping):
                    inferred_manifest = item.ops.get("_trial")
                envelopes.append(adapt_window_row(item))
            else:
                raise TraceDeterminismError(
                    "build_trace_chain requires WindowEvidence or TraceEnvelope items"
                )

    manifest_digest = (
        _sha256_canonical(dict(inferred_manifest))
        if isinstance(inferred_manifest, Mapping) and inferred_manifest
        else None
    )

    links: list[TraceChainLink] = []
    prev_digest = GENESIS_DIGEST
    for idx, env in enumerate(envelopes):
        env_digest, chain_digest = _compute_link_digest(idx, prev_digest, env)
        links.append(
            TraceChainLink(
                index=idx,
                envelope=env,
                prev_digest=prev_digest,
                envelope_digest=env_digest,
                chain_digest=chain_digest,
            )
        )
        prev_digest = chain_digest

    head_digest = prev_digest
    complete_digest = _compute_complete_trace_digest(head_digest, manifest_digest, len(links))
    return TraceDigestChain(
        links=tuple(links),
        head_digest=head_digest,
        manifest_digest=manifest_digest,
        complete_trace_digest=complete_digest,
    )


def verify_envelope_chain(
    chain_or_envelopes: TraceDigestChain | Iterable[TraceEnvelope],
    *,
    reference_chain: TraceDigestChain | None = None,
    expected_targets: Iterable[object] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Verify step ordering, uniqueness, link hashes, and target membership on a chain."""
    violations: list[str] = []
    if isinstance(chain_or_envelopes, TraceDigestChain):
        chain = chain_or_envelopes
        envelopes = list(chain.envelopes)
        prev = GENESIS_DIGEST
        for idx, link in enumerate(chain.links):
            if link.index != idx:
                violations.append("reordered_step")
            if link.prev_digest != prev:
                violations.append("chain_digest_mismatch")
            want_env_digest, want_chain_digest = _compute_link_digest(idx, prev, link.envelope)
            if link.envelope_digest != want_env_digest or link.chain_digest != want_chain_digest:
                violations.append("chain_digest_mismatch")
            prev = link.chain_digest
        if chain.head_digest != prev:
            violations.append("chain_digest_mismatch")
        want_complete = _compute_complete_trace_digest(
            chain.head_digest, chain.manifest_digest, len(chain.links)
        )
        if chain.complete_trace_digest != want_complete:
            violations.append("chain_digest_mismatch")
    else:
        envelopes = list(chain_or_envelopes)
        chain = build_trace_chain(envelopes, preserve_input_order=True)

    seen_steps: set[int] = set()
    seen_target_frames: set[tuple[tuple[str, str], int, int, int]] = set()
    last_local: dict[tuple[tuple[str, str], int], tuple[int, int]] = {}
    allowed_targets: set[tuple[tuple[str, str], int]] | None = None
    if expected_targets is not None:
        allowed_targets = {canonical_target(t) for t in expected_targets}

    for idx, env in enumerate(envelopes):
        if env.step in seen_steps:
            violations.append("duplicate_step")
        seen_steps.add(env.step)
        if env.step != idx:
            violations.append("reordered_step")

        tkey = (env.target, env.generation)
        if allowed_targets is not None and tkey not in allowed_targets:
            violations.append("foreign_observation")

        frame_key = (env.target, env.generation, env.epoch, env.frame_seq)
        if frame_key in seen_target_frames:
            violations.append("duplicate_step")
        seen_target_frames.add(frame_key)

        prev_ep_seq = last_local.get(tkey)
        if prev_ep_seq is not None and (
            env.epoch < prev_ep_seq[0]
            or (env.epoch == prev_ep_seq[0] and env.frame_seq <= prev_ep_seq[1])
        ):
            violations.append("reordered_step")
        last_local[tkey] = (env.epoch, env.frame_seq)

    if reference_chain is not None:
        if len(chain.links) != len(reference_chain.links):
            violations.append("chain_length_mismatch")
        ref_steps = [l.envelope.step for l in reference_chain.links]
        cur_steps = [e.step for e in envelopes]
        if Counter(cur_steps) != Counter(ref_steps):
            if len(set(cur_steps)) < len(cur_steps):
                violations.append("duplicate_step")
            else:
                violations.append("missing_or_foreign_rows")
        elif cur_steps != ref_steps:
            violations.append("reordered_step")
        for cur_link, ref_link in zip(chain.links, reference_chain.links):
            if (cur_link.envelope.target, cur_link.envelope.generation) != (
                ref_link.envelope.target,
                ref_link.envelope.generation,
            ):
                if cur_link.envelope.step == ref_link.envelope.step:
                    violations.append("foreign_observation")
                else:
                    violations.append("reordered_step")
            if cur_link.chain_digest != ref_link.chain_digest:
                violations.append("chain_digest_mismatch")
        if chain.complete_trace_digest != reference_chain.complete_trace_digest:
            violations.append("chain_digest_mismatch")

    deduped = tuple(dict.fromkeys(violations))
    return (len(deduped) == 0), deduped


def _classify_seal(
    rows: tuple[mws.WindowEvidence, ...],
    canonical_rows: tuple[mws.WindowEvidence, ...],
    manifest: dict,
    manifest_ok: bool,
) -> tuple[bool, list[str]]:
    if not rows:
        return False, ["empty_seal", "missing_mixed_or_invalid_seal"]

    seal_violations: list[str] = []
    seals: list[str] = []
    for row in rows:
        s = row.seal
        if not isinstance(s, str):
            raise TraceDeterminismError("seal must be a string")
        seals.append(s)
        if s == "":
            seal_violations.append("empty_seal")
        elif len(s) < 64:
            seal_violations.append("truncated_seal")
        elif len(s) > 64 or not _HEX64_RE.fullmatch(s):
            seal_violations.append("malformed_seal")

    unique_seals = set(seals)
    if len(unique_seals) > 1:
        seal_violations.append("mixed_seal")

    if seal_violations or not manifest_ok:
        seal_violations.append("missing_mixed_or_invalid_seal")
        return False, list(dict.fromkeys(seal_violations))

    candidate_seal = next(iter(unique_seals))
    expected_canonical_seal = mws._seal(canonical_rows, manifest)
    expected_input_seal = mws._seal(rows, manifest)
    if candidate_seal not in (expected_canonical_seal, expected_input_seal):
        return False, ["altered_seal", "missing_mixed_or_invalid_seal"]
    return True, []


def _audit_observations(
    observations: Iterable[object] | None,
    declared_targets: set[tuple[str, str, int]],
    expected_frames_by_identity: dict[tuple[str, str, int, int, int], str],
) -> tuple[int, int, list[str], list[dict]]:
    if observations is None:
        return 0, 0, [], []

    obs_list = list(observations)
    total = len(obs_list)
    good = 0
    violations: list[str] = []
    bleed_events: list[dict] = []

    for idx, item in enumerate(obs_list):
        obs: Observation | None = None
        if isinstance(item, PerceptionResult):
            try:
                p_target, p_gen = canonical_target(item.handle)
            except TraceDeterminismError:
                violations.append("foreign_observation")
                bleed_events.append({"index": idx, "reason": "invalid_perception_handle"})
                continue
            if (*p_target, p_gen) not in declared_targets:
                violations.append("foreign_observation")
                bleed_events.append(
                    {"index": idx, "handle": [*p_target, p_gen], "reason": "undeclared_perception_target"}
                )
                continue
            obs = item.observation
            if obs is None:
                good += 1
                continue
        elif isinstance(item, Observation) or (
            hasattr(item, "handle") and hasattr(item, "frame") and hasattr(item, "state")
        ):
            obs = item
        else:
            raise TraceDeterminismError("observations must be Observation or PerceptionResult records")

        try:
            o_target, o_gen = canonical_target(obs.handle)
            f_target, f_gen = canonical_target(obs.frame.handle)
        except TraceDeterminismError:
            violations.append("foreign_observation")
            bleed_events.append({"index": idx, "reason": "invalid_observation_handle"})
            continue

        o_key = (*o_target, o_gen)
        f_key = (*f_target, f_gen)
        row_bad = False

        if o_key not in declared_targets or f_key not in declared_targets:
            violations.append("foreign_observation")
            bleed_events.append({"index": idx, "handle": list(o_key), "reason": "undeclared_target"})
            row_bad = True

        if (o_key, obs.epoch, obs.frame_seq) != (f_key, obs.frame.epoch, obs.frame.frame_seq):
            violations.append("foreign_observation")
            bleed_events.append({"index": idx, "handle": list(o_key), "reason": "frame_observation_mismatch"})
            row_bad = True

        if isinstance(obs.state, Mapping) and "target" in obs.state:
            state_target = obs.state.get("target")
            if isinstance(state_target, (list, tuple)) and tuple(state_target) != o_key:
                violations.append("foreign_observation")
                violations.append("cross_window_bleed")
                bleed_events.append(
                    {
                        "index": idx,
                        "handle": list(o_key),
                        "state_target": list(state_target),
                        "reason": "foreign_state_target",
                    }
                )
                row_bad = True

        expected_state_digest = expected_frames_by_identity.get((o_key[0], o_key[1], o_key[2], obs.epoch, obs.frame_seq))
        if expected_state_digest is not None and isinstance(obs.state, Mapping):
            actual_state_digest = hashlib.sha256(mws._json(obs.state)).hexdigest()
            if actual_state_digest != expected_state_digest:
                violations.append("foreign_observation")
                violations.append("cross_window_bleed")
                bleed_events.append(
                    {
                        "index": idx,
                        "handle": list(o_key),
                        "reason": "observation_state_digest_mismatch",
                    }
                )
                row_bad = True

        if not row_bad:
            good += 1

    return good, total, violations, bleed_events


def _evaluate_reset_evidence(
    declared_targets: set[tuple[str, str, int]],
    rows: tuple[mws.WindowEvidence, ...],
    report_faults: tuple[dict, ...],
    manifest: dict,
    injected_reset_evidence: Mapping | None,
    *,
    require_reset: bool,
) -> tuple[dict, list[str]]:
    violations: list[str] = []
    reset_fault_handles: set[tuple[str, str, int]] = set()

    for fault in report_faults:
        if isinstance(fault, Mapping) and fault.get("stage") == "reset":
            h = fault.get("handle")
            if isinstance(h, (list, tuple)) and len(h) == 3:
                reset_fault_handles.add((h[0], h[1], h[2]))
            violations.append("incomplete_reset_evidence")

    for iso in manifest.get("isolated_faults", []):
        if isinstance(iso, Mapping):
            for fault in iso.get("faults", []):
                if isinstance(fault, Mapping) and fault.get("stage") == "reset":
                    h = fault.get("handle")
                    if isinstance(h, (list, tuple)) and len(h) == 3:
                        reset_fault_handles.add((h[0], h[1], h[2]))
                    violations.append("incomplete_reset_evidence")

    for fault in manifest.get("repeat_faults", []):
        if isinstance(fault, Mapping) and fault.get("stage") == "reset":
            h = fault.get("handle")
            if isinstance(h, (list, tuple)) and len(h) == 3:
                reset_fault_handles.add((h[0], h[1], h[2]))
            violations.append("incomplete_reset_evidence")

    for row in rows:
        if "reset_AUSENTE" in row.violations:
            reset_fault_handles.add(mws.handle_key(row.handle))
            violations.append("incomplete_reset_evidence")

    if not declared_targets:
        if require_reset:
            violations.append("incomplete_reset_evidence")
        return (
            {
                "required_targets": 0,
                "verified_targets": 0,
                "complete": False,
                "metric": _metric(None, None, False),
            },
            list(dict.fromkeys(violations)),
        )

    verified_targets = 0
    per_target_status: dict[str, bool] = {}

    for key in sorted(declared_targets):
        label = mws._key_text(key)
        target_ok = key not in reset_fault_handles and bool(rows)
        if injected_reset_evidence is not None:
            entry = injected_reset_evidence.get(key)
            if entry is None:
                entry = injected_reset_evidence.get(label)
            if entry is None:
                target_ok = False
            elif isinstance(entry, bool):
                target_ok = target_ok and entry
            elif isinstance(entry, int):
                target_ok = target_ok and (entry >= 1)
            elif isinstance(entry, Mapping):
                seeds = entry.get("seeds")
                count = entry.get("reset_count", len(seeds) if isinstance(seeds, (list, tuple)) else 0)
                ok_flag = entry.get("reset_ok", True)
                target_ok = target_ok and (type(ok_flag) is bool and ok_flag) and (type(count) is int and count >= 1)
                if isinstance(seeds, (list, tuple)) and len(set(seeds)) != 1:
                    target_ok = False
            else:
                target_ok = False
        per_target_status[label] = target_ok
        if target_ok:
            verified_targets += 1
        elif require_reset:
            violations.append("incomplete_reset_evidence")

    complete = verified_targets == len(declared_targets) and not violations
    return (
        {
            "required_targets": len(declared_targets),
            "verified_targets": verified_targets,
            "complete": complete,
            "targets": per_target_status,
            "metric": _metric(verified_targets, len(declared_targets), complete),
        },
        list(dict.fromkeys(violations)),
    )


def audit_episode_log(
    report_or_rows: mws.TrialReport | Iterable[mws.WindowEvidence],
    *,
    reference_chain: TraceDigestChain | None = None,
    reset_evidence: Mapping | None = None,
    observations: Iterable[object] | None = None,
    require_reset: bool = True,
) -> BleedAuditReport:
    """Audit a multi-window episode log for digest-chain integrity and cross-window bleed."""
    if isinstance(report_or_rows, mws.TrialReport):
        input_rows = tuple(report_or_rows.rows)
        report_faults = tuple(report_or_rows.faults)
        report_outcomes = dict(report_or_rows.outcomes)
    else:
        input_rows = tuple(report_or_rows)
        report_faults = ()
        report_outcomes = {}

    for row in input_rows:
        mws._validate_row(row)
        canonical_target(row.handle)

    violations: list[str] = []
    bleed_events: list[dict] = []

    manifests = [r.ops.get("_trial") for r in input_rows]
    manifest = manifests[0] if manifests and isinstance(manifests[0], dict) else {}
    manifest_ok = bool(manifest) and all(m == manifest for m in manifests)
    if not manifest_ok:
        violations.append("missing_or_mixed_manifest")

    declared_targets: set[tuple[str, str, int]] = set()
    for t in manifest.get("targets", []):
        if isinstance(t, (list, tuple)) and len(t) == 3:
            target_pair, gen = canonical_target(t)
            declared_targets.add((*target_pair, gen))
    if not declared_targets and isinstance(report_outcomes.get("gates"), Mapping):
        for g_info in report_outcomes["gates"].values():
            if isinstance(g_info, Mapping) and isinstance(g_info.get("evidence"), list):
                for entry in g_info["evidence"]:
                    if isinstance(entry, Mapping) and isinstance(entry.get("handle"), list) and len(entry["handle"]) == 3:
                        tp, gen = canonical_target(entry["handle"])
                        declared_targets.add((*tp, gen))

    expected_ids = [tuple(i) for i in manifest.get("identities", [])]
    input_identities = [mws._identity(r) for r in input_rows]

    seen_event_indices: set[int] = set()
    seen_identities: set[tuple] = set()
    missing_event_index = False
    duplicate_step = False
    for r in input_rows:
        ident = mws._identity(r)
        if ident in seen_identities:
            duplicate_step = True
        seen_identities.add(ident)
        ev_idx = r.ops.get("event_index")
        if type(ev_idx) is not int or ev_idx < 0:
            missing_event_index = True
        else:
            if ev_idx in seen_event_indices:
                duplicate_step = True
            seen_event_indices.add(ev_idx)

    if missing_event_index:
        violations.append("missing_event_index")
    if duplicate_step:
        violations.append("duplicate_step")

    if not missing_event_index and not duplicate_step and input_rows:
        canonical_rows = tuple(sorted(input_rows, key=lambda r: r.ops["event_index"]))
    else:
        canonical_rows = input_rows

    canonical_identities = [mws._identity(r) for r in canonical_rows]

    if Counter(input_identities) != Counter(expected_ids):
        violations.append("missing_or_foreign_rows")
        for r in input_rows:
            if mws._identity(r) not in expected_ids or mws.handle_key(r.handle) not in declared_targets:
                violations.append("foreign_observation")
                bleed_events.append(
                    {
                        "handle": list(mws.handle_key(r.handle)),
                        "scene": r.scene,
                        "epoch": r.epoch,
                        "frame_seq": r.frame_seq,
                        "reason": "undeclared_or_foreign_row_identity",
                    }
                )
    else:
        if canonical_identities != expected_ids:
            violations.append("reordered_step")
        if seen_event_indices != set(range(len(expected_ids))):
            violations.append("reordered_step")

    if {mws.handle_key(r.handle) for r in input_rows} != declared_targets:
        violations.append("missing_or_foreign_target")
        for r in input_rows:
            if mws.handle_key(r.handle) not in declared_targets:
                violations.append("foreign_observation")

    last_per_handle: dict[tuple[str, str, int], tuple[int, int]] = {}
    for r in canonical_rows:
        hkey = mws.handle_key(r.handle)
        prev = last_per_handle.get(hkey)
        if prev is not None and (r.epoch < prev[0] or (r.epoch == prev[0] and r.frame_seq <= prev[1])):
            violations.append("reordered_step")
        last_per_handle[hkey] = (r.epoch, r.frame_seq)

    seal_ok, seal_violations = _classify_seal(input_rows, canonical_rows, manifest, manifest_ok)
    violations.extend(seal_violations)

    base_card = mws.score_windows(canonical_rows)
    for v in base_card.violations:
        if v == "missing_mixed_or_invalid_seal" and seal_ok:
            continue
        violations.append(v)

    expected_state_by_id: dict[tuple[str, str, int, int, int], str] = {}
    for r in canonical_rows:
        hkey = mws.handle_key(r.handle)
        s_dig = r.ops.get("state_digest")
        if isinstance(s_dig, str) and _HEX64_RE.fullmatch(s_dig):
            expected_state_by_id[(hkey[0], hkey[1], hkey[2], r.epoch, r.frame_seq)] = s_dig
        elif r.frame_supported and not r.dropped and not r.ops.get("quarantined", False):
            violations.append("missing_or_invalid_state_digest")

        if r.ops.get("isolated_match") is not True:
            violations.append("cross_window_state_bleed")
            bleed_events.append(
                {
                    "handle": list(hkey),
                    "epoch": r.epoch,
                    "frame_seq": r.frame_seq,
                    "event_index": r.ops.get("event_index"),
                    "reason": "isolated_mismatch",
                }
            )
        if r.ops.get("counts") != r.ops.get("expected_counts"):
            violations.append("operation_totals_mismatch")
            bleed_events.append(
                {
                    "handle": list(hkey),
                    "epoch": r.epoch,
                    "frame_seq": r.frame_seq,
                    "event_index": r.ops.get("event_index"),
                    "reason": "operation_totals_mismatch",
                }
            )
        for rv in r.violations:
            if any(
                tok in rv
                for tok in (
                    "cross_window_observation",
                    "cross_window_frame",
                    "cross_window_change",
                    "cross_window_receipt",
                    "cross_window_resource",
                    "foreign",
                )
            ):
                violations.append("foreign_observation")
                bleed_events.append(
                    {
                        "handle": list(hkey),
                        "epoch": r.epoch,
                        "frame_seq": r.frame_seq,
                        "event_index": r.ops.get("event_index"),
                        "reason": rv,
                    }
                )

    obs_good, obs_total, obs_violations, obs_bleed = _audit_observations(
        observations, declared_targets, expected_state_by_id
    )
    violations.extend(obs_violations)
    bleed_events.extend(obs_bleed)

    if not missing_event_index:
        try:
            chain = build_trace_chain(
                canonical_rows,
                manifest=manifest if manifest_ok else None,
                preserve_input_order=True,
            )
            chain_ok, chain_violations = verify_envelope_chain(
                chain,
                reference_chain=reference_chain,
                expected_targets=declared_targets if declared_targets else None,
            )
            if not chain_ok:
                violations.extend(chain_violations)
        except TraceDeterminismError:
            violations.append("invalid_envelope_chain")
            chain = TraceDigestChain(
                links=(),
                head_digest=GENESIS_DIGEST,
                manifest_digest=None,
                complete_trace_digest=_compute_complete_trace_digest(GENESIS_DIGEST, None, 0),
            )
            chain_ok = False
    else:
        chain = TraceDigestChain(
            links=(),
            head_digest=GENESIS_DIGEST,
            manifest_digest=None,
            complete_trace_digest=_compute_complete_trace_digest(GENESIS_DIGEST, None, 0),
        )
        chain_ok = False

    reset_info, reset_violations = _evaluate_reset_evidence(
        declared_targets,
        canonical_rows,
        report_faults,
        manifest,
        reset_evidence,
        require_reset=require_reset,
    )
    violations.extend(reset_violations)

    if report_outcomes.get("blocked") is True:
        violations.append("trial_blocked_prerequisites")

    deduped_violations = tuple(dict.fromkeys(violations))

    total_rows = len(canonical_rows)
    clean_bleed_rows = sum(
        1
        for r in canonical_rows
        if r.ops.get("isolated_match") is True
        and r.ops.get("counts") == r.ops.get("expected_counts")
        and mws.handle_key(r.handle) in declared_targets
        and not any("cross_window" in v or "foreign" in v for v in r.violations)
    )
    bleed_passed = (
        total_rows > 0
        and clean_bleed_rows == total_rows
        and "foreign_observation" not in deduped_violations
        and "cross_window_state_bleed" not in deduped_violations
        and "missing_or_foreign_rows" not in deduped_violations
        and "missing_or_foreign_target" not in deduped_violations
    )

    step_order_good = (
        total_rows
        if total_rows > 0
        and "reordered_step" not in deduped_violations
        and "duplicate_step" not in deduped_violations
        and "missing_event_index" not in deduped_violations
        else (0 if total_rows > 0 else None)
    )
    chain_links_good = (
        len(chain.links)
        if len(chain.links) > 0
        and seal_ok
        and chain_ok
        and "chain_digest_mismatch" not in deduped_violations
        else (0 if len(chain.links) > 0 else None)
    )

    obs_denom = obs_total if observations is not None else total_rows
    obs_num = (
        obs_good
        if observations is not None
        else sum(
            1
            for r in canonical_rows
            if mws.handle_key(r.handle) in declared_targets
            and not any("cross_window_observation" in v or "foreign" in v for v in r.violations)
        )
    )

    metrics = copy.deepcopy(base_card.metrics)
    metrics["digest_chain_integrity"] = _metric(
        chain_links_good,
        len(chain.links) if len(chain.links) > 0 else None,
        bool(len(chain.links) > 0 and seal_ok and chain_ok and chain_links_good == len(chain.links)),
        head_digest=chain.head_digest,
        complete_trace_digest=chain.complete_trace_digest,
        seal_ok=seal_ok,
    )
    metrics["cross_window_bleed_audit"] = _metric(
        clean_bleed_rows if total_rows > 0 else None,
        total_rows if total_rows > 0 else None,
        bleed_passed,
        bleed_event_count=len(bleed_events),
    )
    metrics["step_order_integrity"] = _metric(
        step_order_good,
        total_rows if total_rows > 0 else None,
        bool(total_rows > 0 and step_order_good == total_rows),
    )
    metrics["reset_completeness"] = reset_info["metric"]
    metrics["observation_provenance"] = _metric(
        obs_num if obs_denom > 0 else None,
        obs_denom if obs_denom > 0 else None,
        bool(obs_denom > 0 and obs_num == obs_denom and "foreign_observation" not in deduped_violations),
    )

    passed = (
        not deduped_violations
        and seal_ok
        and chain_ok
        and reset_info["complete"]
        and all(m.get("passed") is True for m in metrics.values())
    )

    absent = copy.deepcopy(
        manifest.get(
            "absent",
            {
                "measured_host_speed": "AUSENTE",
                "native_capture": "AUSENTE",
                "G-T4": "AUSENTE",
                "G-T5": "AUSENTE",
            },
        )
    )

    return BleedAuditReport(
        passed=passed,
        chain=chain,
        scorecard=base_card,
        metrics=metrics,
        violations=deduped_violations,
        bleed_events=tuple(bleed_events),
        reset_evidence=reset_info,
        absent=absent,
    )


def _normalize_checkpoint_list(checkpoints: object) -> list[tuple[int, dict]]:
    if not isinstance(checkpoints, list) or not checkpoints:
        raise TraceDeterminismError("checkpoints must be a nonempty list")
    normalized: list[tuple[int, dict]] = []
    seen_indices: set[int] = set()
    seen_canonical: set[bytes] = set()
    for cp in checkpoints:
        if not isinstance(cp, Mapping):
            raise TraceDeterminismError("checkpoint must be a dict")
        indices = [cp[k] for k in ("after_step", "after") if k in cp]
        if not indices or any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != 1:
            raise TraceDeterminismError("checkpoint index must be a single nonnegative int")
        idx = indices[0]
        if "expect" in cp and "expected_post" in cp:
            raise TraceDeterminismError("checkpoint must use one expectation field")
        expect = cp.get("expect", cp.get("expected_post"))
        replay_mod._validate_expect(expect)
        cp_bytes = _canonical_bytes({"after_step": idx, "expect": expect})
        if idx in seen_indices or cp_bytes in seen_canonical:
            raise TraceDeterminismError("duplicate_checkpoint")
        seen_indices.add(idx)
        seen_canonical.add(cp_bytes)
        normalized.append((idx, strip_diagnostics(copy.deepcopy(expect))))
    return normalized


class _ResetAuditingDriverProxy:
    """Proxy around a replay Driver that records reset, observe, and act traces."""

    def __init__(self, driver: object) -> None:
        self._driver = driver
        self.reset_calls = 0
        self.run_traces: list[list[tuple[dict, dict, dict]]] = []
        self._current_run: list[tuple[dict, dict, dict]] = []
        self._last_pre_state: dict | None = None
        self._pending_step: dict | None = None

    def reset(self) -> None:
        reset_fn = getattr(self._driver, "reset", None)
        if not callable(reset_fn):
            raise TraceDeterminismError("incomplete_reset_evidence: driver.reset is AUSENTE")
        reset_fn()
        self.reset_calls += 1
        self._current_run = []
        self.run_traces.append(self._current_run)
        self._last_pre_state = None
        self._pending_step = None

    def observe(self):
        raw = self._driver.observe()
        state = raw[0] if isinstance(raw, tuple) and raw else raw
        if isinstance(state, Mapping):
            snap = copy.deepcopy(dict(state))
            if self._pending_step is not None and self._last_pre_state is not None:
                self._current_run.append(
                    (copy.deepcopy(self._last_pre_state), copy.deepcopy(self._pending_step), copy.deepcopy(snap))
                )
                self._pending_step = None
            self._last_pre_state = snap
        return raw

    def act(self, step: dict):
        self._pending_step = copy.deepcopy(step)
        return self._driver.act(step)


def verify_admitted_skill_record(
    admitted_json_or_dict: str | Mapping,
    *,
    reference_draft: Mapping | None = None,
    expected_sha256: str | None = None,
    known_versions: set[int] | None = None,
) -> dict:
    """Decode and audit an admitted skill record for seal, step, and checkpoint integrity."""
    if isinstance(admitted_json_or_dict, str):
        try:
            record = json.loads(admitted_json_or_dict)
        except json.JSONDecodeError as exc:
            raise TraceDeterminismError("admitted skill payload is not valid JSON") from exc
    elif isinstance(admitted_json_or_dict, Mapping):
        record = copy.deepcopy(dict(admitted_json_or_dict))
    else:
        raise TraceDeterminismError("admitted skill payload must be canonical JSON text or dict")

    if not isinstance(record, dict):
        raise TraceDeterminismError("decoded skill record must be a dict")

    evidence = record.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TraceDeterminismError("admitted skill record lacks evidence mapping")

    seal = evidence.get("sha256")
    if not isinstance(seal, str) or seal == "":
        raise TraceDeterminismError("empty_seal")
    if len(seal) < 64:
        raise TraceDeterminismError("truncated_seal")
    if len(seal) > 64 or not _HEX64_RE.fullmatch(seal):
        raise TraceDeterminismError("malformed_seal")
    if expected_sha256 is not None and seal != expected_sha256:
        raise TraceDeterminismError("altered_seal")

    version = evidence.get("version")
    _exact_int(version, "evidence.version", minimum=1)
    if known_versions is not None and version not in known_versions:
        raise TraceDeterminismError("unknown_version")

    steps = record.get("steps")
    if not isinstance(steps, list) or not steps:
        raise TraceDeterminismError("steps must be a nonempty list")

    norm_checkpoints = _normalize_checkpoint_list(record.get("checkpoints"))
    for cp_idx, _ in norm_checkpoints:
        if cp_idx >= len(steps):
            raise TraceDeterminismError("altered_checkpoint: checkpoint index out of step range")

    if reference_draft is not None:
        ref_steps = reference_draft.get("steps")
        if not isinstance(ref_steps, list):
            raise TraceDeterminismError("reference_draft must contain steps list")
        cur_step_bytes = [_canonical_bytes(s) for s in steps]
        ref_step_bytes = [_canonical_bytes(s) for s in ref_steps]
        if cur_step_bytes != ref_step_bytes:
            if Counter(cur_step_bytes) == Counter(ref_step_bytes):
                raise TraceDeterminismError("reordered_step")
            if len(cur_step_bytes) > len(ref_step_bytes) and len(set(cur_step_bytes)) < len(cur_step_bytes):
                raise TraceDeterminismError("duplicate_step")
            raise TraceDeterminismError("altered_step")

        ref_checkpoints = _normalize_checkpoint_list(reference_draft.get("checkpoints"))
        if norm_checkpoints != ref_checkpoints:
            raise TraceDeterminismError("altered_checkpoint")

    return record


def verify_replay_trace(
    draft: dict,
    driver: object,
    verified_by: str,
    *,
    runs: int = 2,
    reference_draft: Mapping | None = None,
    known_versions: set[int] | None = None,
    target: object = ("cdp_tab", "replay-target"),
    generation: int = 1,
    epoch: int = 0,
) -> dict:
    """Execute replay_and_admit with strict verifier-owned reset, checkpoint, and chain checks."""
    if not isinstance(draft, dict):
        raise TraceDeterminismError("draft must be a dict")
    _exact_int(runs, "runs", minimum=2)

    if not hasattr(driver, "reset") or not callable(getattr(driver, "reset", None)):
        raise TraceDeterminismError("incomplete_reset_evidence: driver.reset is required")

    prior_evidence = draft.get("evidence", {})
    if isinstance(prior_evidence, Mapping) and "version" in prior_evidence:
        prior_version = prior_evidence["version"]
        _exact_int(prior_version, "prior evidence.version", minimum=1)
        if known_versions is not None and prior_version not in known_versions:
            raise TraceDeterminismError("unknown_prior_version")

    norm_checkpoints = _normalize_checkpoint_list(draft.get("checkpoints"))
    if reference_draft is not None:
        ref_checkpoints = _normalize_checkpoint_list(reference_draft.get("checkpoints"))
        if norm_checkpoints != ref_checkpoints:
            raise TraceDeterminismError("altered_checkpoint")
        ref_steps = reference_draft.get("steps")
        cur_steps = draft.get("steps")
        if isinstance(ref_steps, list) and isinstance(cur_steps, list):
            cur_b = [_canonical_bytes(s) for s in cur_steps]
            ref_b = [_canonical_bytes(s) for s in ref_steps]
            if cur_b != ref_b:
                if Counter(cur_b) == Counter(ref_b):
                    raise TraceDeterminismError("reordered_step")
                raise TraceDeterminismError("duplicate_step" if len(cur_b) > len(ref_b) else "altered_step")

    proxy = _ResetAuditingDriverProxy(driver)
    admitted_text = replay_mod.replay_and_admit(
        copy.deepcopy(draft), proxy, verified_by=verified_by, runs=runs
    )
    if not isinstance(admitted_text, str):
        raise TraceDeterminismError("replay_and_admit must return canonical JSON text")

    if proxy.reset_calls != runs or len(proxy.run_traces) != runs:
        raise TraceDeterminismError("incomplete_reset_evidence")

    first_run = proxy.run_traces[0]
    summaries = [
        state_diff.summarize(state_diff.diff_states(pre_s, post_s))
        for pre_s, _step, post_s in first_run
    ]
    expected_sha = hashlib.sha256(schema_dumps(summaries).encode("utf-8")).hexdigest()

    decoded = verify_admitted_skill_record(
        admitted_text,
        reference_draft=reference_draft or draft,
        expected_sha256=expected_sha,
    )

    norm_target, norm_gen = canonical_target(target, generation)
    cp_by_step = {idx: exp for idx, exp in norm_checkpoints}
    envelopes: list[TraceEnvelope] = []
    for step_idx, (pre_s, step_action, post_s) in enumerate(first_run):
        diff_summary = summaries[step_idx]
        cp_expect = cp_by_step.get(step_idx)
        cp_passed = replay_mod._eval_expect(cp_expect, post_s) if cp_expect is not None else True
        if not cp_passed:
            raise TraceDeterminismError("altered_checkpoint")
        post_digest = _sha256_canonical(post_s)
        envelopes.append(
            TraceEnvelope(
                target=norm_target,
                generation=norm_gen,
                epoch=epoch,
                step=step_idx,
                action=strip_diagnostics(step_action),
                outcome={
                    "frame_seq": step_idx,
                    "event_index": step_idx,
                    "diff_summary": strip_diagnostics(diff_summary),
                    "checkpoint_expect": cp_expect,
                    "checkpoint_passed": cp_passed,
                    "terminal": "SUCCESS",
                },
                state_digest=post_digest,
            )
        )

    manifest = {
        "skill_id": decoded.get("id"),
        "verified_by": verified_by,
        "runs": runs,
        "reset_calls": proxy.reset_calls,
        "version": decoded["evidence"]["version"],
        "summary_sha256": expected_sha,
    }
    chain = build_trace_chain(envelopes, manifest=manifest, preserve_input_order=True)
    return {
        "admitted": decoded,
        "canonical_json": canonical_json(decoded),
        "chain": chain,
        "complete_trace_digest": chain.complete_trace_digest,
        "reset_evidence": {
            "required_runs": runs,
            "observed_resets": proxy.reset_calls,
            "complete": proxy.reset_calls == runs,
            "metric": _metric(proxy.reset_calls, runs, proxy.reset_calls == runs),
        },
    }
