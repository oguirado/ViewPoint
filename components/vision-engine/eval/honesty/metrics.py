"""Pure-Python integer-only honesty metrics for 7x7 tile-frame confusion matrices.

Rows represent ideal (ground-truth) classes and columns represent predicted classes
in canonical order: UNCHANGED, JITTER, CHANGED, MOVED, FLICKER, FROZEN, DRIFT.
All computations use exact integer arithmetic and floor division only.
"""

UNCHANGED, JITTER, CHANGED, MOVED, FLICKER, FROZEN, DRIFT = range(7)
CLASSES = (
    "UNCHANGED",
    "JITTER",
    "CHANGED",
    "MOVED",
    "FLICKER",
    "FROZEN",
    "DRIFT",
)
NAMES = CLASSES

REAL_CHANGE_CLASSES = (CHANGED, MOVED, FLICKER)
SILENT_CLASSES = (UNCHANGED, JITTER)

PPM_SCALE = 1_000_000
PERMILLE_SCALE = 1_000
PRECISION_FLOOR_PERMILLE = 900


def _validate_confusion(confusion):
    """Validate and normalize a 7x7 non-negative integer confusion matrix."""
    if isinstance(confusion, (str, bytes, bytearray)):
        raise TypeError("confusion matrix must be a 7x7 sequence of integers")
    try:
        rows = list(confusion)
    except TypeError as exc:
        raise TypeError("confusion matrix must be a 7x7 sequence of integers") from exc
    if len(rows) != 7:
        raise ValueError(f"confusion matrix must have 7 rows, got {len(rows)}")

    matrix = []
    for r_idx, row in enumerate(rows):
        if isinstance(row, (str, bytes, bytearray)):
            raise TypeError(f"row {r_idx} must be a sequence of 7 integers")
        try:
            cols = list(row)
        except TypeError as exc:
            raise TypeError(f"row {r_idx} must be a sequence of 7 integers") from exc
        if len(cols) != 7:
            raise ValueError(
                f"confusion matrix row {r_idx} must have 7 columns, got {len(cols)}"
            )
        norm_row = []
        for c_idx, val in enumerate(cols):
            if isinstance(val, bool) or not isinstance(val, int):
                raise TypeError(
                    f"confusion[{r_idx}][{c_idx}] must be an int, got {type(val).__name__}"
                )
            if val < 0:
                raise ValueError(
                    f"confusion[{r_idx}][{c_idx}] must be non-negative, got {val}"
                )
            norm_row.append(int(val))
        matrix.append(tuple(norm_row))
    return tuple(matrix)


def silent_miss_ppm(confusion):
    """Return parts-per-million of real-change tile-frames predicted as UNCHANGED or JITTER.

    Real-change ideal classes are CHANGED (2), MOVED (3), and FLICKER (4).
    Silent predicted classes are UNCHANGED (0) and JITTER (1).
    Uses integer floor division only. Returns 0 when total real-change support is 0.
    """
    matrix = _validate_confusion(confusion)
    real_change_total = 0
    silent_misses = 0
    for r_idx in REAL_CHANGE_CLASSES:
        row = matrix[r_idx]
        for count in row:
            real_change_total += count
        for c_idx in SILENT_CLASSES:
            silent_misses += row[c_idx]
    if real_change_total == 0:
        return 0
    return (silent_misses * PPM_SCALE) // real_change_total


def class_honesty(confusion):
    """Return per-class honesty metrics over a 7x7 integer confusion matrix.

    For each class in canonical order, computes:
    - predicted: total tile-frames predicted as this class (column sum)
    - support: total ideal tile-frames of this class (row sum)
    - tp: true positive count (diagonal entry)
    - precision: exact fraction string 'num/den' (tp/predicted, or '0/0' when predicted == 0)
    - permille: floor precision in permille ((tp * 1000) // predicted, or 0 when predicted == 0)
    - zero_support_emitted: True iff predicted > 0 and support == 0
    - low_precision: True iff predicted > 0 and permille < 900
    - flagged: True iff emitted with zero support or precision below 900 permille
    - flags: tuple of active flag strings ('zero_support', 'low_precision')
    """
    matrix = _validate_confusion(confusion)
    report = {}
    for idx, name in enumerate(CLASSES):
        support = sum(matrix[idx])
        predicted = sum(matrix[r_idx][idx] for r_idx in range(7))
        tp = matrix[idx][idx]
        precision_str = f"{tp}/{predicted}"
        permille = (tp * PERMILLE_SCALE) // predicted if predicted > 0 else 0
        zero_support_emitted = predicted > 0 and support == 0
        low_precision = predicted > 0 and permille < PRECISION_FLOOR_PERMILLE
        flagged = zero_support_emitted or low_precision
        active_flags = []
        if zero_support_emitted:
            active_flags.append("zero_support")
        if low_precision:
            active_flags.append("low_precision")
        report[name] = {
            "predicted": predicted,
            "support": support,
            "tp": tp,
            "precision": precision_str,
            "permille": permille,
            "zero_support_emitted": zero_support_emitted,
            "low_precision": low_precision,
            "flagged": flagged,
            "flags": tuple(active_flags),
        }
    return report
