#!/usr/bin/env python3
"""Write standards/schemas/*.json (JSON Schema 2020-12 contracts of DESIGN section 3; `d#x` expanded, additionalProperties:false)."""
import os, sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hblib2 as L  # noqa: E402

DEFS = {"sid": {"type": "string", "pattern": r"^[A-Za-z0-9_./-]{1,160}::[A-Za-z_][A-Za-z0-9_.]{0,127}(#b[0-9]{1,3})?$"},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "relpath": {"type": "string", "pattern": r"^(?!/)(?![A-Za-z]:)(?!.*(^|/)\.\.(/|$))[^\\\u0000]+$"},
        "dec": {"type": "string", "pattern": r"^-?[0-9]+(\.[0-9]+)?$"}}
D = lambda n: {"$ref": "hb.defs.v2#/$defs/" + n}
I = lambda **k: dict(type="integer", **k)
S = lambda **k: dict(type="string", **k)
OBJ = lambda req, props: {"type": "object", "required": req, "properties": props}


def strict(s):
    if isinstance(s, dict):
        s = {k: strict(v) for k, v in s.items()}
        if s.get("type") == "object" and "properties" in s:
            s["additionalProperties"] = False
    elif isinstance(s, list):
        s = [strict(v) for v in s]
    return s


SCHEMAS = {}
SCHEMAS["hb.symbol.v2"] = OBJ(["schema", "id", "kind", "path", "span", "sha256", "sha256_ast", "sig", "governs", "bytes"], {
    "schema": {"const": "hb.symbol.v2"}, "id": D("sid"),
    "kind": {"enum": ["module", "class", "function", "method", "constant", "test", "gd_func", "scene_node", "subanchor"]},
    "path": D("relpath"),
    "span": OBJ(["start_byte", "end_byte", "start_line", "end_line"], {"start_byte": I(minimum=0), "end_byte": I(minimum=0),
                                                                   "start_line": I(minimum=1), "end_line": I(minimum=1)}),
    "bytes": I(minimum=0), "sha256": D("sha256"), "sha256_ast": D("sha256"), "doc_sha256": D("sha256"), "signature_sha256": D("sha256"),
    "contract_fp": S(pattern="^[0-9a-f]{16}$"), "sha256_shell": D("sha256"), "sig": S(maxLength=160),
    "card": OBJ(["purpose"], {"purpose": S(maxLength=240), "tasks": {"type": "array", "maxItems": 3, "items": S(maxLength=120)},
                              "not_for": {"type": "array", "maxItems": 2, "items": S(maxLength=120)}}),
    "children": {"type": "array", "maxItems": 64, "items": D("sid")}, "skeleton": S(maxLength=800),
    "governs": OBJ(["sections", "inv", "au"], {
        "sections": {"type": "array", "items": S(pattern=r"^hb-s[0-9]{1,2}(\.[a-z0-9_-]+)?$")},
        "inv": {"type": "array", "items": S(pattern=r"^INV-[A-Za-z0-9_-]+$")},
        "au": {"type": "array", "items": S(pattern=r"^AU-[A-Za-z0-9_-]+$")},
        "none_declared": {"const": True},
        "claims": {"type": "array", "items": S(pattern=r"^C-[A-Z0-9-]+$")},
        "receipts": {"type": "array", "items": S(pattern=r"^R-[A-Za-z0-9-]+$")},
        "tests": {"type": "array", "items": D("sid")}})})
SCHEMAS["hb.anchor.v2"] = OBJ(["schema", "id", "path", "symbol", "role", "kind", "span", "sha256", "lang", "start_byte", "end_byte",
                               "sha256_ast", "doc_sha256", "status", "redirect_to"], {
    "schema": {"const": "hb.anchor.v2"}, "id": S(maxLength=200), "path": D("relpath"), "symbol": S(maxLength=512),
    "role": {"enum": ["symbol", "asset", "section", "scene"]},
    "kind": {"enum": ["module", "class", "function", "method", "constant", "test", "gd_func", "scene_node", "subanchor", "asset", "doc_section"]},
    "span": OBJ(["start_line", "end_line"], {"start_line": I(minimum=1), "end_line": I(minimum=1)}),
    "sha256": D("sha256"), "lang": {"enum": ["python", "gdscript", "markdown", "json", "other"]}, "signature_sha256": D("sha256"),
    "sha256_shell": D("sha256"), "start_byte": I(minimum=0), "end_byte": I(minimum=0), "sha256_ast": D("sha256"), "doc_sha256": D("sha256"),
    "status": {"enum": ["active", "alias", "tombstone"]}, "redirect_to": {"type": ["string", "null"]},
    "uid": S(pattern="^uid://[a-z0-9]{1,16}$")})
SCHEMAS["hb.claim.v2"] = OBJ(["schema", "id", "kind", "statement", "anchors", "etag", "receipts"], {
    "schema": {"const": "hb.claim.v2"}, "id": S(pattern="^C-[A-Z0-9-]+$"),
    "kind": {"enum": ["precondition", "postcondition", "invariant", "bound", "performance", "determinism", "behaviour"]},
    "statement": S(minLength=1, maxLength=600), "anchors": {"type": "array", "minItems": 1, "items": D("sid")},
    "etag": {"enum": ["MEASURED", "CITED", "TARGET", "SLOT"]}, "receipts": {"type": "array", "items": S(pattern="^R-[A-Za-z0-9-]+$")},
    "citation": S(maxLength=400),
    "value": OBJ(["metric_ref", "op", "number", "unit"], {
        "metric_ref": S(pattern=r"^[^:]{1,200}\.json:[A-Za-z0-9_.-]{1,200}$"), "op": {"enum": ["<=", "<", "==", ">=", ">", "~"]},
        "number": D("dec"), "unit": {"enum": ["ms", "s", "us", "ns", "B", "KiB", "MiB", "%", "count"]}})})
SCHEMAS["hb.claim-check.v2"] = OBJ(["schema", "claim", "source", "parsed", "receipt", "verdict", "code"], {
    "schema": {"const": "hb.claim-check.v2"}, "claim": S(maxLength=160), "source": {"enum": ["structured", "prose"]},
    "parsed": OBJ(["number", "decimals", "unit", "op"], {"number": D("dec"), "decimals": I(minimum=0, maximum=12),
                                                          "unit": S(maxLength=8), "op": S(maxLength=2)}),
    "receipt": OBJ(["ref", "outcome"], {"ref": S(maxLength=402), "file_sha256": D("sha256"), "value": D("dec"), "unit": S(maxLength=8),
                                        "outcome": {"enum": ["Hit", "KeyMiss", "ForgedEmpty", "DigestMismatch", "NotNumber"]}}),
    "tolerance": S(pattern="^[0-9]+/[0-9]+$"), "verdict": {"enum": ["supported", "contradicted", "unverifiable"]},
    "code": {"enum": ["OK", "E_REFUTED_CLAIM", "E_RECEIPT_KEY", "E_UNTAGGED_NUMBER"]}})
SCHEMAS["hb.impact-pack.v2"] = OBJ(["schema", "task_q", "targets", "change", "chg", "tests", "closure", "items", "bytes"], {
    "schema": {"const": "hb.impact-pack.v2"}, "task_q": S(pattern="^[0-9a-f]{16}$"),
    "targets": {"type": "array", "minItems": 1, "maxItems": 3, "items": D("sid")}, "change": {"enum": ["SIG", "BODY"]},
    "chg": {"type": "array", "maxItems": 5, "items": D("sid")},
    "tests": {"type": "array", "maxItems": 3, "items": OBJ(["id", "cmd"], {"id": D("sid"), "cmd": S(maxLength=200)})},
    "receipts": {"type": "array", "maxItems": 16, "items": S(pattern="^R-[A-Za-z0-9-]+$")},
    "claims": {"type": "array", "maxItems": 16, "items": S(pattern="^C-[A-Z0-9-]+$")},
    "scene": {"type": "array", "maxItems": 16, "items": S(maxLength=160)},
    "closure": OBJ(["d_max", "dirty", "visited", "truncated"], {"d_max": I(minimum=1, maximum=3), "dirty": I(minimum=0),
                                                                 "visited": I(minimum=0), "truncated": {"type": "boolean"}}),
    "items": {"type": "array", "maxItems": 32, "items": OBJ(["id", "level", "text"], {"id": S(maxLength=200), "level": {"enum": ["a", "b", "g"]},
                                                                                       "text": S(maxLength=4096)})},
    "bytes": I(minimum=0, maximum=12288)})
SCHEMAS["hb.alias-proposal.v2"] = OBJ(["schema", "from", "to", "op", "d_ppm", "runner_up_ppm", "tau_ppm", "margin_ppm", "auto_apply", "evidence"], {
    "schema": {"const": "hb.alias-proposal.v2"}, "from": D("sid"), "to": D("sid"),
    "op": {"enum": ["SHIFT", "FORMAT", "MOVE", "RENAME", "MOVE+EDIT", "SCENE_RENAME"]},
    "d_ppm": I(minimum=0, maximum=1000000), "runner_up_ppm": {"type": ["integer", "null"], "minimum": 0}, "tau_ppm": I(), "margin_ppm": I(),
    "auto_apply": {"type": "boolean"},
    "evidence": OBJ(["old_sha256", "new_sha256", "tokens_old", "tokens_new"], {"old_sha256": D("sha256"), "new_sha256": D("sha256"),
                                                                              "tokens_old": I(), "tokens_new": I(), "hist_l1": I()})})
SCHEMAS["hb.answer.v2"] = OBJ(["schema", "tool", "ix", "pr", "q", "class", "abstain", "r"], {
    "schema": {"const": "hb.answer.v2"}, "tool": S(pattern=r"^hb_query\.py/2\.[0-9]+$"), "ix": S(pattern="^[0-9a-f]{16}$"),
    "pr": S(pattern="^[0-9a-f]{16}$"), "q": S(pattern="^[0-9a-f]{16}$"), "class": {"enum": ["FIND", "IMPACT", "CLAIM", "ABSENT"]},
    "abstain": OBJ(["flag", "reason", "m", "cov", "oov"], {"flag": {"type": "boolean"},
                   "reason": {"enum": ["none", "no_query_term", "uncalibrated", "low_term_match", "below_tau", "oov"]},
                   "m": I(minimum=0), "cov": I(minimum=0, maximum=1000000), "oov": I(minimum=0, maximum=1000000)}),
    "r": {"type": "array", "maxItems": 20, "items": False, "prefixItems": []},
    "impact": OBJ(["change", "chg", "tests"], {"change": {"enum": ["SIG", "BODY"]}, "chg": {"type": "array", "maxItems": 5, "items": D("sid")},
                                               "tests": {"type": "array", "maxItems": 3, "items": D("sid")},
                                               "receipts": I(minimum=0), "pack": S(pattern="^[0-9a-f]{16}$")}),
    "claim": OBJ(["verdict", "value", "code"], {"verdict": {"enum": ["supported", "contradicted", "unverifiable"]},
                                                "value": {"type": ["string", "null"]}, "code": S(maxLength=24)}),
    "overflow": {"const": True}})
# rows are arrays [sid, score_ppm, level(, payload)]: expressed with prefixItems on a per-row schema
SCHEMAS["hb.answer.v2"]["properties"]["r"] = {"type": "array", "maxItems": 20, "items": {
    "type": "array", "minItems": 3, "maxItems": 4, "items": False,
    "prefixItems": [D("sid"), I(minimum=0), {"enum": ["a", "b", "g"]}, S(maxLength=1600)]}}
SCHEMAS["hb.visual-receipt.v2"] = OBJ(["schema", "scene", "tier", "godot", "gate", "telemetry_sha256", "verdict", "run"], {
    "schema": {"const": "hb.visual-receipt.v2"}, "scene": S(pattern="^res://[A-Za-z0-9_./-]{1,120}$"), "tier": {"enum": ["1a", "1b", "1c", "2"]},
    "godot": S(maxLength=40), "run": I(minimum=1, maximum=3),
    "gate": OBJ(["rc", "n_err", "nonce_ok", "timeout"], {"rc": I(), "n_err": I(minimum=0), "nonce_ok": {"type": "boolean"}, "timeout": {"type": "boolean"}}),
    "telemetry_sha256": D("sha256"), "frame_sha256": D("sha256"),
    "diff": OBJ(["changed_ppm", "delta_px", "rho_ppm"], {"changed_ppm": I(minimum=0, maximum=1000000), "delta_px": I(), "rho_ppm": I(),
                                                          "dhash_hamming": I(minimum=0, maximum=64)}),
    "findings": {"type": "array", "maxItems": 64, "items": OBJ(["anchor", "code"], {"anchor": S(maxLength=160), "code": {"enum": [
        "E_SCENE_LINK", "E_SCENE_RES", "NODE_MISSING", "NODE_ADDED", "XFORM", "RECT", "VISIBLE", "Z", "OWNER", "PIXELS"]}})},
    "verdict": {"enum": ["PASS", "FAIL", "NoFramebuffer", "AMBIGUOUS"]},
    "judge": OBJ(["family", "veto"], {"family": S(maxLength=24), "veto": {"type": "boolean"}, "canary_ok": {"type": "boolean"}})})
SCHEMAS["hb.section.v2"] = OBJ(["schema", "hb_id", "n", "anchor", "kind", "diataxis", "title", "purpose", "code_anchors", "claims", "route"], {
    "schema": {"const": "hb.section.v2"}, "hb_id": S(pattern="^HB-[A-Z0-9]+-[0-9]{3}$"), "n": I(minimum=0, maximum=10),
    "anchor": S(pattern=r"^hb-s[0-9]{1,2}(\.[a-z0-9_-]+)?$"), "kind": S(maxLength=24), "diataxis": {"enum": ["reference", "explanation", "how-to", "tutorial"]},
    "title": S(maxLength=240), "purpose": S(maxLength=600), "code_anchors": {"type": "array", "items": D("sid")}, "covers": D("relpath"),
    "claims": {"type": "array", "items": S(pattern="^C-[A-Z0-9-]+$")},
    "route": OBJ(["emit", "tags"], {"emit": {"type": "boolean"}, "tags": {"type": "array", "items": S(maxLength=40)}})})
SCHEMAS["hb.inv.v2"] = OBJ(["module", "inv", "au"], {
    "module": D("relpath"),
    "inv": {"type": "array", "items": OBJ(["id", "text", "symbols"], {"id": S(pattern="^INV-[A-Za-z0-9_-]+$"), "text": S(minLength=1, maxLength=600), "symbols": {"type": "array", "minItems": 1, "items": D("sid")}})},
    "au": {"type": "array", "items": OBJ(["id", "text", "symbols"], {"id": S(pattern="^AU-[A-Za-z0-9_-]+$"), "text": S(minLength=1, maxLength=600), "symbols": {"type": "array", "minItems": 1, "items": D("sid")}})}})
SCHEMAS["hb.receipt.v2"] = OBJ(["schema", "kind", "tool", "metrics"], {
    "schema": {"const": "hb.receipt.v2"},
    "kind": {"enum": ["test", "benchmark", "property", "mutation", "gate", "build", "trace", "visual"]}, "tool": S(maxLength=80),
    "metrics": {"type": "object", "additionalProperties": {"type": ["integer", "string", "null"]}},
    "inputs": {"type": "object"}, "nonce": S(pattern="^[0-9a-f]{24}$")})
SCHEMAS["hb.mutant.v2"] = OBJ(["id", "anchor", "file", "line", "op", "before", "after"], {
    "id": S(pattern="^MUT-[A-Z0-9-]+$"), "anchor": D("sid"), "file": D("relpath"), "line": I(minimum=1),
    "op": {"enum": ["cmp_flip", "cmp_strict", "tie_rev", "const_off_by_one", "return_none", "drop_branch"]},
    "before": S(minLength=1), "after": S(), "perm": {"type": "boolean"}, "tests": {"type": "array", "items": S()},
    "extra": S(maxLength=80), "cover": D("sid"),
    "tie": {"type": "object"}})


def build():
    out = {"hb.defs.v2": {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "hb.defs.v2", "$defs": DEFS}}
    for k, v in SCHEMAS.items():
        out[k] = dict(strict(v), **{"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": k})
    return out


def main(argv=None):
    d = argv[0] if argv else os.path.join(L.HB, "standards", "schemas")
    for k, v in build().items():
        L.write_json(os.path.join(d, k + ".schema.json"), v)
    print(len(SCHEMAS) + 1, "schemas")


if __name__ == "__main__":
    main(sys.argv[1:])
