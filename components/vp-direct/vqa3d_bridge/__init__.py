"""Deterministic visual QA over pinned Blender checkpoint captures."""

from dataclasses import asdict, dataclass
from functools import lru_cache
import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from blender_visual_qa.drive import journal, png
from vqa3d.metrics import uniform_ppm


# Keep the existing vqa3d frame_not_blank threshold and integer scale.
MAX_UNIFORM_PPM = 995_000
PPM = 1_000_000

_FACTORY = "vp_factory_call"
_SCORE = "vqa3d.score"
_GATE = "vqa3d.gate"
_schema_dir = Path(__file__).resolve().parents[1] / "schemas"
_spec = importlib.util.spec_from_file_location("vp_vqa3d_validation", _schema_dir / "validation.py")
_validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validation)


def _pinned_reply_contract(op: str) -> str:
    if op not in (_SCORE, _GATE):
        raise journal.DriveError("E_SCHEMA", f"Unsupported VQA3D adapter op: {op}")
    contract = f"{_FACTORY}.{op}.result/v1"
    row = _validation._adapter_rows().get(_FACTORY, {}).get("ops", {}).get(op, {})
    schema = _validation._store().get("urn:" + contract.replace("/", ":"))
    if (row.get("reply_schema") != contract or schema is None
            or row.get("reply_schema_sha256") != journal.sha(_validation.canonical_bytes(schema))):
        raise journal.DriveError("E_SCHEMA", f"VQA3D reply schema pin mismatch: {contract}")
    return contract


@dataclass(frozen=True)
class RenderPredicate:
    render: str
    frame_not_blank: bool


@dataclass(frozen=True)
class QualityResult:
    """In-process result for a scene and its verified render evidence."""

    scene_digest: str
    score: int
    predicates: tuple[RenderPredicate, ...]

    @property
    def accepted(self) -> bool:
        return bool(self.predicates) and all(row.frame_not_blank for row in self.predicates)

    def to_adapter_result(self, op: str) -> dict:
        """Return a quality payload checked against the frozen factory result pin."""
        contract = _pinned_reply_contract(op)
        result = {
            "scene_digest": self.scene_digest,
            "score": self.score,
            "predicates": [asdict(row) for row in self.predicates],
        }
        if op == _GATE:
            result["accepted"] = self.accepted
        try:
            _validation.validate_contract(contract, result)
        except ValidationError as exc:
            raise journal.DriveError("E_SCHEMA", f"Invalid VQA3D result: {contract}") from exc
        return result


@lru_cache(maxsize=1)
def _receipt_validator():
    path = Path(journal.__file__).with_name("schemas") / "capture-receipt.v1.schema.json"
    return Draft202012Validator(json.loads(path.read_bytes()))


def _read_evidence(root: Path, relative: str) -> bytes:
    try:
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise journal.DriveError("E_IO", f"render evidence is outside the run or is not a file: {relative}")
        return path.read_bytes()
    except OSError as exc:
        raise journal.DriveError("E_IO", f"render evidence unavailable: {relative}") from exc


def _verify_receipt(raw: bytes, blend_sha256: str, receipt_sha256: str) -> dict:
    if journal.sha(raw) != receipt_sha256:
        raise journal.DriveError("E_DETERMINISM", "capture receipt digest mismatch", 5)
    try:
        receipt = json.loads(raw)
        # Uses the capture writer's encoding and refuses floats, including NaN.
        if journal.canonical(receipt) != raw:
            raise journal.DriveError("E_SCHEMA", "capture receipt is not canonical")
        _receipt_validator().validate(receipt)
    except (ValueError, ValidationError) as exc:
        raise journal.DriveError("E_SCHEMA", "invalid capture receipt") from exc
    if receipt["blend_sha256"] != blend_sha256:
        raise journal.DriveError("E_DETERMINISM", "Blender scene digest mismatch", 5)
    return receipt


def _measure_render(root: Path, relative: str, pixel_sha256: str, file_sha256: str | None = None) -> int:
    raw = _read_evidence(root, relative)
    if file_sha256 is not None and journal.sha(raw) != file_sha256:
        raise journal.DriveError("E_DETERMINISM", f"render file digest mismatch: {relative}", 5)
    _, _, pixels = png.decode(raw)
    if journal.sha(pixels) != pixel_sha256:
        raise journal.DriveError("E_DETERMINISM", f"render pixel digest mismatch: {relative}", 5)
    return uniform_ppm(pixels)


def _quality_score(uniformities: list[int]) -> int:
    # Use the worst render's share of pixels outside its dominant colour.
    return PPM - max(uniformities)


def evaluate_capture(run_dir: Path, *, blend_sha256: str, receipt_sha256: str) -> QualityResult:
    """Score all frames and stills under an independently pinned capture receipt.

    The scene digest binds the Blender checkpoint, rendering profile, and
    ordered pixel evidence. The score measures render non-uniformity in ppm.
    Each render also has the hard vqa3d nonblank predicate. Invalid or missing
    evidence raises the capture substrate's DriveError before returning a score.
    """
    root = Path(run_dir).resolve()
    receipt = _verify_receipt(_read_evidence(root, "capture.receipt.json"), blend_sha256, receipt_sha256)
    frames = receipt["frames"]
    stills = receipt["still"]
    journal.frame_list({"frames": {"list": [row["render_index"] for row in frames]}})
    if len(stills) > journal.STILL_CAP or [row["ri"] for row in stills] != list(range(1, len(stills) + 1)):
        raise journal.DriveError("E_SCHEMA", "invalid still sequence")
    frame_pins = [[row["render_index"], row["sha256"]] for row in frames]
    if journal.sha(journal.canonical(frame_pins)) != receipt["frames_digest"]:
        raise journal.DriveError("E_DETERMINISM", "frame sequence digest mismatch", 5)

    renders = [
        (f"render/frame_{row['render_index']:06d}.png", row["sha256"], row["file_sha256"])
        for row in frames
    ]
    renders.extend((f"render/still/still_{row['ri']:06d}.png", row["sha256"], None) for row in stills)
    uniformities = [_measure_render(root, *render) for render in renders]
    predicates = tuple(
        RenderPredicate(render[0], uniformity < MAX_UNIFORM_PPM)
        for render, uniformity in zip(renders, uniformities, strict=True)
    )
    scene_digest = journal.sha(journal.canonical([
        blend_sha256, receipt["profile_sha256"], receipt["frames_digest"], stills,
    ]))
    return QualityResult(scene_digest, _quality_score(uniformities), predicates)
