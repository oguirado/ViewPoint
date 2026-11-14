# @module {"id":"vm.ingest","layer":5,"stage":"S7","owner":"VMEM","deps":["vc.canon","vc.errors","vm.blobs","vm.receipts","vm.store"],"book":"HB-VC-s7-vmem"}
"""@module vm.ingest - CLI verbs that persist: `ingest` (SPEC_VMEM §5), `compact`, `gc`, `pin` (§4, §7). Stdlib only."""
import os
import re
import shutil

from visioncore import canon, ids, layout
from visioncore.errors import VqaError
from . import compact as C, router, units as U
from .blobs import blob_path, get_blob, iter_blobs
from .receipts import receipt_sha256
from .store import Store

SPOOL_SCHEMA = "vmem.spool/v1"
EPISODE_REQUIRED = ("schema", "episode", "run_id", "gop", "f0", "f1", "n_frames", "cut", "gop_blob_sha256",
                    "verdicts", "fg", "classes_ppm", "delta_bytes", "grid", "params_sha256")
REGION_REQUIRED = ("id", "frame", "cls", "bbox_px", "xh64", "sig64", "render_index", "look", "desc_hex")
PRED = {"CHANGED": "changed", "MOVED": "moved_from", "FLICKER": "flicker", "FROZEN": "frozen", "DRIFT": "drift"}
ALIGN = ("render_index", "step", "none")
DIGEST_MAX = 6000


def _bad(reason, **d):
    d["reason"] = reason
    raise VqaError("E_SCHEMA", d)


def _look_region(body, scope):
    """Normalize the §3.4 static-look carrier without requiring dirty-region fields."""
    b = dict(body, cls="UNCHANGED")
    if "id" in b:
        return b  # existing expanded region carrier
    required = {"frame", "render_index", "role", "m0", "m1", "look", "xh64", "sig64"}
    if not required <= set(b):
        _bad("look_missing_field", missing=sorted(required - set(b)))
    W, H = (int(x) for x in scope["res"].split("x"))
    s, _gw, _gh = layout.grid_params(W, H)
    b["id"] = ids.region_id(b["frame"], b["m0"], W, H)
    ids.track_key(scope["app"], scope["scene"], W, H, b["role"], b["m0"], b["m1"])
    U.validate_look(b["look"])
    for k in ("xh64", "sig64"):
        values = b[k]
        if not isinstance(values, list) or len(values) != b["look"]["n"] or any(
                not isinstance(v, str) or not U.HEX16.fullmatch(v) for v in values):
            _bad("look_hashes", field=k)
        b[k] = values[0]
    x0, y0 = layout.unmorton(b["m0"])
    x1, y1 = layout.unmorton(b["m1"])
    b["bbox_px"] = [x0 * s, y0 * s, min(W, (x1 + 1) * s), min(H, (y1 + 1) * s)]
    return b


# ---------------------------------------------------------------- spool validation
def parse_spool(raw):
    """-> (run, episodes, regions, verdicts); every failure is E_SCHEMA/E_ID (rc 2/3) before anything is written."""
    lines = [canon.loads(l) for l in raw.decode("utf-8").splitlines() if l.strip()]
    if not lines:
        _bad("empty_spool")
    for i, o in enumerate(lines):
        if not isinstance(o, dict) or o.get("schema") != SPOOL_SCHEMA or o.get("seq") != i \
                or o.get("type") not in ("run", "episode", "region", "look", "verdict") or not isinstance(o.get("body"), dict):
            _bad("spool_line", seq=i)
    run = lines[0]
    if run["type"] != "run":
        _bad("first_not_run")
    rid = run["run_id"]
    if not isinstance(rid, str) or ids.parse_any(rid)[0] != "run_id":
        _bad("run_id")
    if any(o["run_id"] != rid for o in lines):
        _bad("run_id_mixed")
    rb = run["body"]
    if not {"scope", "receipt_sha256", "profile_sha256", "align_source"} <= set(rb) or rb["align_source"] not in ALIGN:
        _bad("run_body")
    U.validate_scope(rb["scope"])
    if not (isinstance(rb["receipt_sha256"], str) and U.HEX64.match(rb["receipt_sha256"])):
        _bad("receipt")
    eps = [o["body"] for o in lines if o["type"] == "episode"]
    for b in eps:
        miss = [k for k in EPISODE_REQUIRED if k not in b]
        if miss:
            _bad("episode_missing_field", missing=miss)
        if b["f1"] != b["f0"] + b["n_frames"] - 1 or b["run_id"] != rid:
            _bad("episode_f1")
        if not U.HEX64.match(str(b["gop_blob_sha256"])):
            _bad("episode_gop_sha")
    # type "look" (SPEC_VMEM §3.4): anchor-keyframe tile looks, ingested as UNCHANGED regions
    regs = [_look_region(o["body"], rb["scope"]) if o["type"] == "look" else o["body"]
            for o in lines if o["type"] in ("region", "look")]
    for b in regs:
        miss = [k for k in REGION_REQUIRED if k not in b and not (
            k == "desc_hex" and isinstance(b.get("look"), dict) and b["look"].get("desc_blob"))]
        if miss:
            _bad("region_missing_field", missing=miss)
        if b["cls"] not in PRED and b["cls"] not in ("UNCHANGED", "JITTER"):
            _bad("region_cls")
        ids.parse_any(ids.cross_cite(rid, b["id"]))
    vds = [o["body"] for o in lines if o["type"] == "verdict"]
    byid = {b["id"] for b in regs}
    for b in vds:
        if b.get("verdict") not in ("PASS", "FAIL", "CANNOT_SEE") or not b.get("pointers") \
                or any(p not in byid for p in b["pointers"]) or not isinstance(b.get("check_id"), str):
            _bad("verdict_body")
    if not regs:
        _bad("no_regions")
    return rb, eps, regs, vds


def _quarantine(root, spool, run_id):
    d = os.path.join(root, "quarantine", re.sub(r"[^A-Za-z0-9_]", "_", run_id or "unknown")[:40])
    os.makedirs(d, exist_ok=True)
    shutil.move(spool, os.path.join(d, os.path.basename(spool)))
    return d


def _digest(units):
    fg = [u for u in units if U.is_fg(u)]
    lines = ["# VISUAL_DIGEST", "units=%d fg=%d" % (len(units), len(fg))]
    for u in sorted(fg, key=lambda x: x["id"])[:40]:
        lines.append("%s %s %s" % (u["id"], u["pin"], u["summary"][:60]))
    return ("\n".join(lines) + "\n").encode("utf-8")[:DIGEST_MAX]


def _carry_episodes(st):
    """SPEC_VMEM §2: gen/<n>/episodes/<run_id>.jsonl files of the parent generation are carried verbatim."""
    cur = st.current()
    out = {}
    if cur is None:
        return out
    d = os.path.join(st.root, "gen", "%06d" % cur, "episodes")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for nme in names:
        with open(os.path.join(d, nme), "rb") as f:
            out["episodes/" + nme] = f.read()
    return out


def write_gen(st, active, archive, blobs=(), extra=None, episodes=None):
    """Stage and commit generation n+1 from unit lists: catalogue, indexes and digest rebuilt; commit gates apply.
    `episodes` = {run_id: [episode dict]} added to the episodes carried over from the parent generation."""
    everything = list(active) + list(archive)
    files = _carry_episodes(st)
    for rid, eps in sorted((episodes or {}).items()):
        files["episodes/%s.jsonl" % rid] = "".join(canon.dumps(e) for e in eps).encode("utf-8")
    files.update({"units/active.jsonl": U.dump_units(active).encode("utf-8"),
                  "catalogue/foreground.tsv": U.render_catalogue(everything).encode("utf-8"),
                  "VISUAL_DIGEST.md": _digest(everything)})
    if archive:
        files["units/archive.jsonl"] = U.dump_units(archive).encode("utf-8")
    files.update(router.build_tk_index(active))
    files.update(router.build_sig_index(active))
    return st.commit(files, blobs, extra)


def _next(units, prefix):
    n = 0
    for u in units:
        if u["id"].startswith("V-%s-" % prefix):
            n = max(n, int(u["id"][-6:]))
    return n


def _load(st):
    cur = st.current()
    if cur is None:
        return [], []
    from .store import _parse_units
    out = {}
    for rel in ("units/active.jsonl", "units/archive.jsonl"):
        try:
            with open(os.path.join(st.root, "gen", "%06d" % cur, rel), "rb") as f:
                out[rel] = _parse_units(f.read())
        except OSError:
            out[rel] = []
    return out["units/active.jsonl"], out["units/archive.jsonl"]


# ---------------------------------------------------------------- ingest
def ingest(root, spool):  # @anchor:vm.ingest.ingest
    try:
        with open(spool, "rb") as f:
            raw = f.read()
    except OSError:
        raise VqaError("E_IO", {"reason": "spool_unreadable"})
    run_id = None
    try:
        for l in raw.decode("utf-8", "replace").splitlines()[:1]:
            run_id = canon.loads(l).get("run_id")
    except (VqaError, AttributeError):
        pass
    st = Store(root)
    with st.lock():
        try:
            run, eps, regs, vds = parse_spool(raw)
        except VqaError as e:
            q = _quarantine(root, spool, run_id if isinstance(run_id, str) else None)
            e.detail["quarantine"] = os.path.relpath(q, root)
            raise
        except (UnicodeError, ValueError, KeyError, TypeError) as e:
            q = _quarantine(root, spool, run_id if isinstance(run_id, str) else None)
            raise VqaError("E_SCHEMA", {"reason": "spool_malformed", "quarantine": os.path.relpath(q, root)}) from e
        rid = canon.loads(raw.decode().splitlines()[0])["run_id"]
        active, archive = _load(st)
        if any(a["run_id"] == rid for u in active + archive for a in u["anchors"]):
            return {"run_id": rid, "already_ingested": True, "gen": st.current()}
        scope = dict(run["scope"])
        gen = (st.current() or 0) + 1
        W, H = (int(x) for x in scope["res"].split("x"))
        rcpt = run["receipt_sha256"]
        counters = {k: _next(active + archive, k) for k in ("BAS", "EVT", "EPI", "VER", "DEF")}

        def nxt(k):
            counters[k] += 1
            return counters[k]

        blobs, anchors, tk_of, new = [], {}, {}, []
        fail_run = any(v["verdict"] == "FAIL" for v in vds)

        def gop_fail(frame):
            """Per-GOP verdict (§4): the episode whose [f0, f1] covers the region frame; run-level FAIL if none does."""
            for e in eps:
                if e["f0"] <= frame <= e["f1"]:
                    return any(x.get("verdict") == "FAIL" for x in e["verdicts"])
            return fail_run
        for b in regs:
            desc = bytes.fromhex(b["desc_hex"]) if "desc_hex" in b else get_blob(root, b["look"]["desc_blob"])
            blobs.append((desc, "bin"))
            crop = None
            if b.get("crop_hex"):
                crop = canon.sha256_bytes(bytes.fromhex(b["crop_hex"]))
                blobs.append((bytes.fromhex(b["crop_hex"]), "png"))
            tk = b.get("tk") or ids.track_key(scope["app"], scope["scene"], W, H, b.get("role", "tile"),
                                              b["m0"], b["m1"])
            tk_of.setdefault(b["id"], tk)  # verdict pointers resolve to the first record of an id (regions before looks)
            b["_tk"] = tk  # per record: a hint look and a macroblock look may share m0, hence the region id (JC3-1)
            lk = dict(b["look"], grid=scope["grid"], desc_blob=canon.sha256_bytes(desc))
            an = {"run_id": rid, "cite": ids.cross_cite(rid, b["id"]), "frame": b["frame"],
                                "render_index": b["render_index"] if run["align_source"] != "none" else None,
                                "bbox_px": b["bbox_px"],
                                "mv_px": b.get("mv_px") or [0, 0], "xh64": b["xh64"], "sig64": b["sig64"],
                                "crop": crop, "desc_blob": lk["desc_blob"]}
            anchors.setdefault(b["id"], an)
            b["_look"], b["_an"] = lk, an
        # §5 step 5: compare with the latest GOLDEN of the tk
        loaded = lambda sha: get_blob(root, sha, "bin") if sha in {s for s, _ in iter_blobs(root)} \
            else dict((canon.sha256_bytes(d), d) for d, _e in blobs)[sha]
        bg_counter = {}
        for b in regs:
            tk, lk, an = b["_tk"], b["_look"], b["_an"]
            chain = router.golden_chain(active + archive + new, tk)
            if not chain:
                if gop_fail(b["frame"]):
                    new.append(U.make_unit(nxt("EVT"), "region_event", scope, tk, predicate=PRED.get(b["cls"], "appeared"),
                                           cls=b["cls"], gen=gen, anchors=[an], look=lk, receipt_sha256=rcpt, pin="FAIL",
                                           summary="first_seen"))
                else:
                    new.append(U.make_unit(nxt("BAS"), "baseline", scope, tk, pin="GOLDEN", gen=gen, anchors=[an],
                                           look=lk, verdict="PASS", receipt_sha256=rcpt, summary="baseline"))
                continue
            rel = U.look_eq(chain[-1]["look"], lk, load_blob=loaded)
            if rel != "DIFFERENT":
                bg_counter[tk] = bg_counter.get(tk, 0) + 1
                continue
            gf = gop_fail(b["frame"])
            new.append(U.make_unit(nxt("EVT"), "region_event", scope, tk, predicate=PRED.get(b["cls"], "look_changed"),
                                   cls=b["cls"], gen=gen, anchors=[an], look=lk, receipt_sha256=rcpt,
                                   pin="FAIL" if gf else None,
                                   summary=PRED.get(b["cls"], "look_changed")))
            if not gf:
                new.append(U.make_unit(nxt("BAS"), "baseline", scope, tk, pin="GOLDEN", gen=gen, anchors=[an], look=lk,
                                       verdict="PASS", receipt_sha256=rcpt, summary="new look"))
        for tk, n in sorted(bg_counter.items()):
            new.append(U.make_unit(nxt("BAS"), "baseline", scope, tk, gen=gen, value="@unchanged", value_num=n,
                                   receipt_sha256=rcpt, summary="@unchanged n=%d" % n))
        first_tk = tk_of[regs[0]["id"]]
        for v in vds:
            ans = [anchors[p] for p in v["pointers"]][:6]
            tk = tk_of[v["pointers"][0]]
            new.append(U.make_unit(nxt("VER"), "verdict", scope, tk, gen=gen, anchors=ans, verdict=v["verdict"],
                                   check_id=v["check_id"][:64], receipt_sha256=rcpt,
                                   summary="%s %s" % (v["check_id"][:40], v["verdict"])))
            if v["verdict"] == "FAIL":
                new.append(U.make_unit(nxt("DEF"), "defect", scope, tk, predicate="changed", gen=gen, anchors=ans,
                                       verdict="FAIL", check_id=v["check_id"][:64], receipt_sha256=rcpt,
                                       summary="defect %s" % v["check_id"][:40]))
        for e in eps:
            gop = {"run_id": rid, "cite": ids.cross_cite(rid, ids.global_id(e["f0"])), "frame": e["f0"],
                   "render_index": None, "bbox_px": [0, 0, 0, 0], "mv_px": [0, 0], "xh64": "0" * 16, "sig64": "0" * 16,
                   "crop": None, "desc_blob": None, "gop_blob": e["gop_blob_sha256"]}
            ev = any(x.get("verdict") == "FAIL" for x in e["verdicts"])
            hint = bool(e["fg"])
            new.append(U.make_unit(nxt("EPI"), "episode", scope, first_tk, gen=gen, anchors=[gop],
                                   verdict="FAIL" if ev else "PASS", receipt_sha256=rcpt,
                                   summary="episode g%s f%d-%d%s" % (e["gop"], e["f0"], e["f1"],
                                                                      " fg_hint_mismatch" if hint != ev else "")))
        everything = U.apply_pins(active + new)
        # I10 (ATIME FC-T6): the gop blob is keyed by sha256 of the stored bytes. An episode may carry `gop_blob_hex`
        # (must hash to gop_blob_sha256). A FAIL episode whose blob is neither supplied nor present -> rc 5
        # g_blob_integrity; a PASS episode keeps the pointer only when the blob exists (no silent loss on FAIL).
        for e in eps:
            if e.get("gop_blob_hex"):
                raw_b = bytes.fromhex(e["gop_blob_hex"])
                if canon.sha256_bytes(raw_b) != e["gop_blob_sha256"]:
                    raise VqaError("E_GATE", {"reason": "g_blob_integrity", "gop": e["gop"], "why": "hash_mismatch"})
                blobs.append((raw_b, "bin"))
        have = {s for s, _ in iter_blobs(root)} | {canon.sha256_bytes(d) for d, _ in blobs}
        size = {canon.sha256_bytes(d): len(d) for d, _ in blobs}
        for e in eps:  # SPEC_VMEM BUDGETS: FG per FAIL episode <= 100 KB = GOP blob + <= 6 crops (ADV-S1-3), refused before any write
            if not any(x.get("verdict") == "FAIL" for x in e["verdicts"]):
                continue
            crops = sorted({r["_an"]["crop"] for r in regs if r["_an"]["crop"] and e["f0"] <= r["frame"] <= e["f1"]})
            gb = size.get(e["gop_blob_sha256"])
            if gb is None and e["gop_blob_sha256"] in have:
                gb = os.stat(blob_path(root, e["gop_blob_sha256"], "bin")).st_size
            fg_b = (gb or 0) + sum(size.get(c, 0) for c in crops)
            if len(crops) > FG_EPISODE_CROPS or fg_b > FG_EPISODE_MAX_B:
                raise VqaError("E_BUDGET", {"reason": "fg_episode_budget", "gop": e["gop"], "bytes": fg_b,
                                            "crops": len(crops), "limit": FG_EPISODE_MAX_B})
        for e in eps:
            if e["gop_blob_sha256"] not in have and any(x.get("verdict") == "FAIL" for x in e["verdicts"]):
                raise VqaError("E_GATE", {"reason": "g_blob_integrity", "missing": [e["gop_blob_sha256"]]})
        everything = [dict(u, anchors=[dict(a, gop_blob=None) if a.get("gop_blob") and a["gop_blob"] not in have
                                       else a for a in u["anchors"]]) for u in everything]
        new_active = [u for u in everything]
        ep_lines = [{k: v for k, v in e.items() if k != "gop_blob_hex"} for e in eps]
        res = write_gen(st, new_active, archive, blobs, {"run_id": rid}, {rid: ep_lines} if ep_lines else None)
        st.journal.append("ingest_begin", {"run_id": rid, "gen": res["gen"]})
    done = os.path.join(os.path.dirname(os.path.abspath(spool)), "done")
    os.makedirs(done, exist_ok=True)
    os.replace(spool, os.path.join(done, os.path.basename(spool)))
    return {"run_id": rid, "gen": res["gen"], "units_added": len(new), "manifest_sha256": res["manifest_sha256"]}


FG_EPISODE_MAX_B = 102400  # SPEC_VMEM BUDGETS "FG per FAIL episode <= 100 KB" (KiB reading, FX7 lane O)
FG_EPISODE_CROPS = 6

# ---------------------------------------------------------------- compact / gc / pin
def compact_run(root):
    st = Store(root)
    with st.lock():
        active, archive = _load(st)
        sizes = {s: os.stat(blob_path(root, s, e)).st_size for s, e in iter_blobs(root)}
        plan = C.compact(active, sizes, gen=(st.current() or 0) + 1)
        res = write_gen(st, plan["active"], archive + plan["archive"])
        st.journal.append("compact_begin", {"gen": res["gen"]})
    return {"gen": res["gen"], "retired": len(plan["retired"]), "summaries": len(plan["summaries"]),
            "fg_ids_sha256": plan["fg_ids_sha256"]}


def gc_run(root):
    st = Store(root)
    with st.lock():
        gone = st.gc_blobs()
        base = st.compact_journal()
    return {"deleted": len(gone), "journal_base": base["seq"] if base else None}


def pin_run(root, uid, receipt):
    try:
        with open(receipt, "rb") as f:
            rsha = receipt_sha256(f.read())
    except OSError:
        raise VqaError("E_IO", {"reason": "receipt_unreadable"})
    st = Store(root)
    with st.lock():
        active, archive = _load(st)
        hit = [u for u in active if u["id"] == uid]
        if not hit:
            raise VqaError("E_ID", {"reason": "unit_not_found", "id": uid[:20]})
        new = [U.pin_unit(dict(u, receipt_sha256=rsha), "CATALOGUED") if u["id"] == uid and not U.is_fg(u) else u
               for u in active]
        res = write_gen(st, new, archive)
        st.journal.append("pin", {"id": uid, "gen": res["gen"]})
    return {"gen": res["gen"], "id": uid, "pin": "CATALOGUED"}


def run_verb(verb, root, a):
    def need(v, n):
        if v is None:
            raise VqaError("E_USAGE", {"reason": "missing_arg", "arg": n})
        return v
    if verb == "ingest":
        return ingest(root, need(a.spool, "--spool"))
    if verb == "compact":
        return compact_run(root)
    if verb == "gc":
        return gc_run(root)
    return pin_run(root, need(a.id, "ID"), need(a.receipt, "--receipt"))
