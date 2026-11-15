# @module {"id":"vc.errors","layer":0,"stage":"L0","owner":"ARCH","deps":[],"book":"HB-VC-a0-arch"}
"""@module vc.errors L0 - error codes and exit codes (SPEC_INT R4). rc 1 is never used (v1.0 FAIL)."""
import json

# code -> rc class (R4 + SPEC_ARCH §9 + SPEC_CORE §12; lanes may namespace but keep the class)
CODES = {
    "OK": 0, "ANSWERED": 0,
    "E_USAGE": 2, "E_SCHEMA": 2, "E_GRID": 2, "E_IO": 2, "E_DECODE": 2, "E_FLOAT": 2,
    "E_VC_FRAME_SCHEMA": 2, "E_VC_FRAME_SHAPE": 2, "E_VC_FORMAT": 2,
    "E_VC_PARAM_UNKNOWN": 2, "E_VC_PARAM_RANGE": 2,
    "E_CITE": 3, "NOT_IN_VISUAL_MEMORY": 3, "E_ANCHOR": 3, "E_ID": 3,
    "E_VC_OUTPUT_EXISTS": 3,
    "E_VC_ID_SYNTAX": 3, "E_VC_ID_RANGE": 3, "E_VC_FRAME_SHA": 3, "E_VC_FRAME_SIZE": 3,
    "E_VC_PNG": 3, "E_NAV_ANCHOR": 3, "E_NAV_ABSTAIN": 3,
    "E_STALE": 4, "E_DRIFT": 4, "E_VQA_PIN": 4, "E_VC_GRID_MISMATCH": 4,
    "E_GATE": 5, "E_MUTANT_SURVIVED": 5, "E_DETERMINISM": 5, "E_GATE_LAYER_UP": 5,
    "E_GATE_LAYER_PKG": 5, "E_VC_GOLDEN": 5,
    "E_BUDGET": 6,
}
RC_CLASSES = (0, 2, 3, 4, 5, 6)


# @anchor:vc.errors.vqaerror
class VqaError(Exception):
    def __init__(self, code, detail=None, rc=None):
        base = CODES.get(code)
        if base is None and rc is None:
            raise ValueError("unregistered error code: %r" % (code,))
        rc = base if rc is None else rc
        if rc == 1 or rc not in RC_CLASSES:
            raise ValueError("rc %r not allowed" % (rc,))
        if rc == 0:
            raise ValueError("VqaError cannot carry rc 0")
        self.code, self.rc, self.detail = code, rc, dict(detail or {})
        super().__init__("%s: %s" % (code, self.detail))

    def to_obj(self):
        return result_obj(self.code, self.rc, self.detail)


def rc_of(code):
    return CODES[code]


def result_obj(code, rc=None, detail=None):
    rc = CODES[code] if rc is None else rc
    return {"code": code, "rc": rc, "detail": dict(detail or {})}


def result_line(obj):
    """One canonical JSON object (stdout contract of every engine CLI)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
