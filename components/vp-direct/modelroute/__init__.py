"""Provider selection for an operation identified by the C5 router."""

from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from statistics import median
from unicodedata import normalize


_schemas = Path(__file__).resolve().parents[1] / "schemas"
_spec = spec_from_file_location("vp_modelroute_validation", _schemas / "validation.py")
_validation = module_from_spec(_spec)
_spec.loader.exec_module(_validation)
canonical_bytes = _validation.canonical_bytes
validate_contract = _validation.validate_contract

_LOWER_IS_BETTER = frozenset({
    "wall_s", "cgroup_peak_bytes", "rss_peak_bytes", "cuda_allocated_max_bytes",
    "cuda_reserved_max_bytes",
})


def _model_key(name):
    return "".join(char for char in normalize("NFKC", name).casefold() if char.isalnum())


def _excluded(model):
    return (model["qualification"]["state"] == "excluded"
            or model["license"]["territory_ok"] is not True
            or _model_key(model["name"]) == "hunyuan3d21")


def _qualified(model, pins):
    qualification = model["qualification"]
    receipt = qualification["receipt"]
    return (qualification["state"] == "runnable" and receipt is not None
            and (model["name"], model["weights"]["tree_sha256"],
                 receipt["path"], receipt["sha256"]) in pins)


def _measurement_key(name, weights, fact):
    return name, weights, canonical_bytes(fact)


@dataclass(frozen=True)
class VerifiedMeasurement:
    """Fact and source coordinates attested independently of the manifest."""

    model_name: str
    weights_sha256: str
    fact: dict
    line_start: int
    line_end: int


def _measurement_citations(measurements):
    citations = {}
    for measurement in measurements:
        fact = measurement.fact
        if fact["tag"] != "MEASURED":
            continue
        start, end = measurement.line_start, measurement.line_end
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("Invalid verified measurement coordinates")
        key = _measurement_key(measurement.model_name, measurement.weights_sha256, fact)
        citation = {**fact["source"], "line_start": start, "line_end": end}
        if citations.setdefault(key, citation) != citation:
            raise ValueError("Conflicting coordinates for a verified measurement")
    return citations


def _model_facts(model, citations):
    found = {}
    for fact in model["measured"]:
        if fact["tag"] != "MEASURED":
            continue
        key = _measurement_key(model["name"], model["weights"]["tree_sha256"], fact)
        if key in citations:
            found[key[-1]] = (fact, citations[key])
    return [found[key] for key in sorted(found)]


@dataclass(frozen=True)
class ProviderCandidate:
    tool: str
    op: str | None
    provider: dict
    citations: tuple[dict, ...]


@dataclass(frozen=True)
class ProviderDecision:
    candidate: ProviderCandidate | None = None
    reason: str | None = None
    detail: str | None = None

    def abstention_bytes(self, index_sha256):
        """Render a refusal using C5's frozen reply contract."""
        if self.candidate is not None:
            raise ValueError("C5 must validate a candidate before rendering a match")
        reply = dict(verdict="ABSTAIN", code="ROUTE_ABSTAIN", reason=self.reason,
                     index_sha256=index_sha256, candidates=[])
        if self.detail is not None:
            reply["detail"] = self.detail
        validate_contract("vp.tool_route.reply/v1", reply)
        return canonical_bytes(reply)


def _candidate(tool, op, provider, facts):
    provider = deepcopy(provider)
    provider["model"]["measured"] = [deepcopy(fact) for fact, _ in facts]
    citations = {canonical_bytes(cite): cite for _, cite in facts}
    return ProviderCandidate(tool, op, provider,
                             tuple(citations[key] for key in sorted(citations)))


def _metric_value(candidate, metric, profile):
    samples = [Fraction(str(fact["value"])) for fact in candidate.provider["model"]["measured"]
               if fact["metric"] == metric and fact["profile"] == profile]
    return median(samples) if samples else None


class ModelIndex:
    """Index per-provider capability declarations without publishing another registry.

    Pass effective M1 capability entries, with verified qualification tuples in
    M1's (name, weights digest, receipt path, receipt digest) format. Evidence
    is supplied by the caller. Manifest-declared paths are never opened here.
    C5 still owns intent calibration, binding checks and the three call forms.
    """

    def __init__(self, capabilities, *, verified_qualifications, verified_measurements):
        pins = frozenset(verified_qualifications)
        citations = _measurement_citations(verified_measurements)
        self._providers = {}
        fact_rows = []
        for entry in capabilities:
            validate_contract("vp.capability/v1", entry)
            provider = entry.get("provider", {})
            model = provider.get("model")
            if model is None:
                continue
            name = _model_key(model["name"])
            weights = model["weights"]["tree_sha256"]
            if not name:
                raise ValueError("Model name has no alphanumeric characters")
            denial = None
            if _excluded(model):
                denial = "model_excluded"
            elif not _qualified(model, pins):
                denial = "model_unqualified"
            facts = _model_facts(model, citations) if denial is None else []
            rows = {row["op"]: row for row in entry.get("op_table", ())}
            for op in entry["ops"] or [None]:
                candidates = self._providers.setdefault((entry["tool"], op), {})
                if (name, weights) in candidates:
                    raise ValueError(f"Duplicate provider for {entry['tool']}/{op}")
                op_denial = denial
                row = rows.get(op)
                if op_denial is None and (not entry["supported"] or (
                    op is not None and (not row or not row["supported"] or row["binding"]["kind"] == "none")
                )):
                    op_denial = "op_unbound"
                candidates[name, weights] = op_denial or _candidate(entry["tool"], op, provider, facts)
                if op_denial is None:
                    fact_rows.append((entry["tool"], op or "", model["name"], weights, facts))
        fact_rows.sort(key=lambda row: row[:4])
        self._model_facts_sha256 = sha256(canonical_bytes(fact_rows)).hexdigest()

    @property
    def model_facts_sha256(self):
        return self._model_facts_sha256

    def select(self, tool, op, *, model_name=None, metric=None, profile=None):
        """Choose among providers for a known model-backed operation.

        Multiple providers require an explicit, common metric and profile.
        Compare medians of verified samples. Missing measurements and ties
        abstain. A model explicitly requested by name cannot silently fall back.
        """
        if ((metric is None) != (profile is None)
                or (metric is not None and (metric not in _LOWER_IS_BETTER or not profile))):
            raise ValueError("Ranking requires a supported metric and an explicit profile")
        requested = _model_key(model_name) if model_name is not None else None
        if requested == "hunyuan3d21":
            return ProviderDecision(reason="policy_denied", detail="model_excluded")
        providers = self._providers.get((tool, op or None), {})
        choices = [choice for (name, _), choice in providers.items()
                   if requested is None or requested == name]
        candidates = [choice for choice in choices if isinstance(choice, ProviderCandidate)]
        if not candidates:
            for detail in ("op_unbound", "model_unqualified", "model_excluded"):
                if detail in choices:
                    return ProviderDecision(reason="policy_denied", detail=detail)
            return ProviderDecision(reason="policy_denied", detail="model_unqualified")
        if len(candidates) == 1:
            return ProviderDecision(candidate=deepcopy(candidates[0]))
        if metric is not None:
            values = [_metric_value(candidate, metric, profile) for candidate in candidates]
            if None not in values:
                best = min(values)
                if values.count(best) == 1:
                    return ProviderDecision(candidate=deepcopy(candidates[values.index(best)]))
        return ProviderDecision(reason="ambiguous")
