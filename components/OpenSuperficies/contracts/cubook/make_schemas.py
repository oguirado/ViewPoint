"""Writes contracts/cubook/schemas/*.schema.json (JSON Schema 2020-12, closed objects).

The strict() rule is the one of hbtools make_schemas.py (additionalProperties:false on every object that declares
properties); the definitions below are written from src/cubook, never read from a Go build. Run: python3 -B make_schemas.py
[--check] (--check exits 5 when a committed file differs from the generated text)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "schemas")


def strict(s):
    if isinstance(s, dict):
        s = {k: strict(v) for k, v in s.items()}
        if s.get("type") == "object" and "properties" in s and "additionalProperties" not in s and "x-open" not in s:
            s["additionalProperties"] = False
    elif isinstance(s, list):
        s = [strict(v) for v in s]
    return s


def obj(props, req=None):
    return {"type": "object", "properties": props, "required": sorted(props if req is None else req)}


I, S, B = {"type": "integer"}, {"type": "string"}, {"type": "boolean"}
H = lambda n: {"type": "string", "pattern": "^[0-9a-f]{%d}$" % n}
NUL = lambda t: {"oneOf": [t, {"type": "null"}]}
XY = {"type": "array", "items": I, "minItems": 2, "maxItems": 2}
ROI = {"oneOf": [{"const": "target"}, {"type": "array", "items": I, "minItems": 4, "maxItems": 4}]}
OPS = ["observe", "locate", "click", "type", "key", "drag", "scroll", "wait", "assert", "mcp_call"]
KINDS = ["changed_roi", "unchanged_outside", "screen_is", "a11y_state", "appear", "vanish", "telemetry", "receipt", "visual", "readback"]
VERDICTS = ["EFFECT_OK", "NO_EFFECT", "WRONG_EFFECT", "CANNOT_SEE", "NO_ACT", "STOPPED", "UNVERIFIED", "SIDE_EFFECT", "MCP_ERROR"]

TARGET = obj({"by": {"enum": ["a11y", "region", "mark", "mcp", "xy"]},
              "a11y": obj({"role": S, "name": S, "occ": I, "path": S}, ["role", "name", "occ"]),
              "region": {"type": "string", "pattern": "^r[0-9]+:[0-9a-f]{4}$"},
              "mark": {"type": "integer", "minimum": 0, "maximum": 4294967295},
              "mcp": obj({"server": S, "object": S}), "xy": XY}, ["by"])
EXPECT = obj({"kind": {"enum": KINDS}, "roi": ROI, "within_frames": I, "spill_ppm": I, "screen_id": H(16),
              "a11y": obj({"rid": S, "role": S, "name": S, "states": {"type": "array", "items": S}}, []),
              "receipt": obj({"path": S, "min_bytes": I, "test_import": S}, ["path", "min_bytes"]),
              "visual": obj({"via": S, "regions": {"type": "array", "items": S}, "forbid_change_outside": B}),
              "readback": obj({"via": S, "query": S, "eq": S})}, ["kind"])
ARGS = obj({"button": {"type": "integer", "minimum": 1, "maximum": 3}, "count": {"type": "integer", "minimum": 1, "maximum": 2},
            "mods": {"type": "array", "items": S}, "text_ref": S, "text_sha256": H(64), "len": I, "chord": S,
            "to": {"$ref": "#/$defs/target"}, "steps": {"type": "integer", "minimum": 2, "maximum": 32},
            "dx": {"type": "integer", "minimum": -32768, "maximum": 32767}, "dy": {"type": "integer", "minimum": -32768, "maximum": 32767},
            "until": {"$ref": "#/$defs/expect"}, "max_frames": I, "settle_k": I, "server": S, "tool": S, "script_ref": S,
            "params": {"type": "object"}, "timeout_ms": I}, [])
ACTION = obj({"schema": {"const": "vqa.action/v1"}, "step": I, "op": {"enum": OPS}, "target": {"$ref": "#/$defs/target"},
              "args": {"$ref": "#/$defs/args"},
              "pre": obj({"screen_is": H(16), "match_min": {"enum": ["L0", "L1", "L2", "L3", "L4"]}}, ["match_min"]),
              "expect": {"type": "array", "items": {"$ref": "#/$defs/expect"}},
              "risk": {"enum": ["read", "write", "irrev"]}, "allow": NUL(S), "fallback": {"enum": ["none", "gui"]},
              "on_fail": {"enum": ["stop", "retry_once", "ask"]}, "policy": {"enum": ["script", "explore", "judge"]},
              "reason_sha8": {"type": "string", "pattern": "^([0-9a-f]{8})?$"}},
             ["schema", "step", "op", "args", "pre", "expect", "risk", "allow", "fallback", "on_fail", "policy", "reason_sha8"])
DEFS = {"target": TARGET, "expect": EXPECT, "args": ARGS}
STEP = obj({"schema": {"const": "vqa.act-step/v1"}, "step": I, "action_sha256": H(64), "op": S,
            "located": obj({"by": S, "rid": S, "region": S, "bbox_px": {"type": "array", "items": I, "minItems": 4, "maxItems": 4},
                            "match_level": S}, ["by", "bbox_px", "match_level"]),
            "actuator_seq": I, "ri_before": I, "ri_after": I, "settle_frames": I, "unsettled": B,
            "screen_before": H(16), "screen_after": H(16), "verdict": {"enum": VERDICTS},
            "evidence": obj({"regions": {"type": "array", "items": S}, "frames": {"type": "array", "items": S},
                             "a11y_delta": obj({"added": {"type": "array", "items": S}, "removed": {"type": "array", "items": S},
                                                "moved": {"type": "array", "items": S}}),
                             "mcp": obj({"server": S, "tool": S, "result_sha256": S, "untrusted": B}),
                             "receipt_sha256": S}, ["regions", "frames", "a11y_delta"]),
            "input_sha256": H(64), "budget": obj({"steps": I, "tok": I, "wall_ms": I}), "stop": NUL(S), "rc": {"enum": [0, 2, 3, 4, 5, 6, 7]}})

WLSTEP_V2 = {"t_ms": {"type": "integer", "minimum": 0}, "op": {"enum": ["click", "scroll", "type", "wait", "move"]},
             "at_px": XY, "text": S, "dy": I, "path": {"type": "array", "items": XY}, "dt_ms": I}
WLSTEP_V3 = dict(WLSTEP_V2, op={"enum": ["click", "scroll", "type", "wait", "move", "hscroll", "key", "drag"]},
                 dx=I, keysym=S, fps=I)


def prereg(ver, stepprops):
    return {"type": "object", "required": ["schema", "items"],
            "x-open": "top level and item keys other than steps are not validated by the Go decoder (only steps are closed)",
            "properties": {"schema": {"const": "os.cu.prereg-wl/v%d" % ver},
                           "resolutions": {"type": "array", "items": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 32767}, "minItems": 2, "maxItems": 2}},
                           "items": {"type": "array", "items": {"type": "object", "required": ["id", "steps"], "x-open": "item keys other than id and steps are not validated by the Go decoder",
                                     "properties": {"id": S, "cursor_px": I,
                                                    "steps": {"type": "array", "items": obj(stepprops, ["t_ms", "op"])}}}}}}


WLCAP_COMMON = {"width": I, "height": I, "requested": I, "published": I, "frames": I, "overrun": I, "dropped": I, "gaps": I, "span_ms": I,
                "longest_run_ms": I, "capture_us_p50": I, "capture_us_p95": I, "write_us_per_frame": I, "peak_resident_bytes": I,
                "src": S, "det": {"enum": [0, 1]}, "status": S}
WLCAP_BENCH = dict(WLCAP_COMMON, resident_end_bytes=I, schema={"const": "os.cu.wlcap-bench/v1"})
WLCAP_RECEIPT = dict(WLCAP_COMMON, schema={"const": "os.cu.wlcap-receipt/v1"}, mode=S, chunk_frames=I, delta_ns=I, segment_ms=I,
                     quota_bytes=I, keep={"enum": [0, 1]}, chunks=I, continuous_required={"enum": [0, 1]}, analyze={"enum": [0, 1]},
                     analyze_timeouts=I)

SCHEMAS = {
    "vqa.action/v1": dict(ACTION, **{"$defs": DEFS}),
    "vqa.act-plan/v1": dict(obj({"schema": {"const": "vqa.act-plan/v1"}, "app": S, "scene": S, "grid": S,
                                 "source_distillate_sha256": NUL(S), "operator_pin_sha256": NUL(S),
                                 "actions": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/action"}}}),
                            **{"$defs": dict(DEFS, action=ACTION)}),
    "vqa.act-step/v1": STEP,
    "vqa.frame/v1": obj({"schema": {"const": "vqa.frame/v1"}, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1},
                         "channels": {"const": 3}, "pixel_format": {"const": "rgb24"}, "count": {"type": "integer", "minimum": 1},
                         "storage": {"const": "rgb24_raw"}, "raw_path": {"type": "string", "pattern": "^[^/]"},
                         "scope": obj({"app": S, "scene": S}),
                         "frames": {"type": "array", "minItems": 1, "items": obj({"i": I, "t_us": I, "render_index": I, "sha256": H(64),
                                                                                  "file_sha256": H(64), "raw_offset": I})}}),
    "os.cu.capture-receipt/v1": obj({"schema": {"const": "os.cu.capture-receipt/v1"}, "det_sha256": H(64), "frames": I,
                                     "geometry": {"type": "array", "items": I, "minItems": 3, "maxItems": 3},
                                     "xwd_header": obj({"header_size": I, "ncolors": I, "bpp": I, "bpl": I, "pixel_offset": I}),
                                     "ram_peak_bytes": I, "ramws_fstype": {"enum": ["tmpfs", "ramfs"]}}),
    "os.cu.prereg-wl/v2": prereg(2, WLSTEP_V2),
    "os.cu.prereg-wl/v3": prereg(3, WLSTEP_V3),
    "os.cu.wlcap-bench/v1": obj(WLCAP_BENCH),
    "os.cu.wlcap-receipt/v1": obj(WLCAP_RECEIPT),
    "os.cu.wlcap-start/v1": obj({"schema": {"const": "os.cu.wlcap-start/v1"}, "clock": {"const": "CLOCK_MONOTONIC"}, "t0_ns": I, "delta_ns": I,
                                 "frames": I, "w": I, "h": I, "chunk_frames": I, "mode": S, "run_nonce": S},
                                [k for k in ("schema", "clock", "t0_ns", "delta_ns", "frames", "w", "h", "chunk_frames", "mode")]),
    "os.cu.wl-act-step/v1": obj({"schema": {"const": "os.cu.wl-act-step/v1"}, "item": S, "actuator_seq": I, "step": I, "point": I,
                                 "op": S, "t_plan_us": I, "t_us": I, "rc": {"enum": [0, 2, 3, 4, 5, 6, 7]}}),
    "os.cu.ast/v1": obj({"schema": {"const": "os.cu.ast/v1"}, "verdict": {"enum": ["allow", "deny"]}, "code": S, "reason": S}),
    "os.cu.rc/v1": {"enum": [0, 2, 3, 4, 5, 6, 7], "description": "SPEC_INT R4 exit codes: 0 ok, 2 usage, 3 anchor, 4 stale, 5 gate, 6 budget, 7 human; 1 is never produced"},
    "os.cu.mcp-tools/v1": obj({"tools": {"type": "array", "minItems": 10, "maxItems": 10, "items": obj({
        "name": {"enum": ["cu_observe", "cu_locate", "cu_click", "cu_type", "cu_key", "cu_drag", "cu_scroll", "cu_wait", "cu_assert", "cu_mcp_call"]},
        "description": S,
        "inputSchema": {"type": "object", "required": ["type", "properties", "additionalProperties"],
                        "properties": {"type": {"const": "object"}, "properties": {"type": "object"}, "additionalProperties": {"const": False}},
                        "additionalProperties": False}})}}),
    "os.cu.mcp-wire/v1": {"description": "one JSON-RPC 2.0 response line of srf-cu-mcp", "oneOf": [
        obj({"jsonrpc": {"const": "2.0"}, "id": {}, "result": {"type": "object"}}),
        obj({"jsonrpc": {"const": "2.0"}, "id": {}, "error": obj({"code": {"enum": [-32700, -32600, -32601]}, "message": S})})]},
}


def text(sid):
    s = dict(strict(SCHEMAS[sid]))
    body = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": sid}
    body.update(s)
    return json.dumps(body, indent=1, sort_keys=True) + "\n"


def fname(sid):
    return sid.replace("/", "-") + ".schema.json"


def main(argv):
    bad = 0
    if "--check" not in argv:
        os.makedirs(OUT, exist_ok=True)
    for sid in SCHEMAS:
        p = os.path.join(OUT, fname(sid))
        t = text(sid)
        if "--check" in argv:
            if not os.path.exists(p) or open(p).read() != t:
                print("DIFF", sid)
                bad = 1
        else:
            open(p, "w").write(t)
    return 5 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
