"""FO3 end-to-end kill harness: tiny CPU render + TRUTH + vision + SD/PX oracles -> findings -> harness.qualify_mutant.

Host side, serial, one Blender at a time (taskset -c 15-19, <= 2 threads, 160x90, 4 frames, 2 spp).  numpy is allowed here
(adapters/ is outside the g_numpy_scope sources).  Judge checks are NOT executed (judge stage is wave B5): their rows are
absent and reported as such; kill status is computed on the sd|px part of expected_check_ids (see judgeless_entry).

STORAGE LAW v1 (W10F L): every scratch file of a run (base/mutant .blend, PNG frames, truth .npy, vision evidence) lives in
a vadapt.ramws RAM workspace (<RAMWS_ROOT>/blender-visual-qa/tests/<run_id>/, pinned wheel), never under build/;
Bench.close() (also __exit__, and __init__ on failure) releases it, even after an exception. `hook(stage, bench)` runs
after every Blender stage (tests check the disk mid-run with it).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from blender_visual_qa import vision_stage  # noqa: E402
from blender_visual_qa.truth import pin as _pin  # noqa: E402
from blender_visual_qa.drive import containment, png, worker  # noqa: E402
from blender_visual_qa.drive.launch import declared_env  # noqa: E402
from blender_visual_qa.oracles import harness, pixel, scene as scene_mod, vision  # noqa: E402

AD = ROOT / "adapters" / "example_scene"
BLENDER = "/usr/bin/blender"
TAGS = {"Subject": "closed|subject|animated", "Static": "closed|static", "TwoShell": "closed", "Textured": "open"}
FRAMES = (1, 2, 3, 4)
RES = "160x90"
SPP = 2
# Tiny-profile PX thresholds (DESIGN_CHOICE, uncalibrated: tiny-bench values, not a vqa.profile/v1 row).
PX = {"magenta_num": 1, "magenta_den": 1000, "black_dark_max": 8, "black_num": 9, "black_den": 10, "uniform_spread": 8,
      "subject_min_ppm": 2000}
RAM_TOOL, RAM_CAMPAIGN, RAM_QUOTA = "blender-visual-qa", "tests", 1 << 30
VISION_PARAMS = {"vc_flicker": {"min_frames": 2}, "vc_frozen_subregion": {"min_frames": 3}, "vc_slow_drift": {"min_frames": 1, "min_tiles": 2}}  # conductor ruling 2026-10-17: NR engine (H_DEAD 2) yields a 1-tile r_max 15 q4 DRIFT at 160x90 2 spp on the step-hold control; bm.drift.15 is KNOWN_OPEN either way (v12e/room_BREPIN/REPORT_P.md)


def blender(env_dir, args, blend=None, timeout=180):
    cmd = ["taskset", "-c", "15-19", BLENDER, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "9", "-t", "2"]
    if blend:
        cmd.append(str(blend))
    cmd += [str(a) for a in args]
    env_dir = Path(env_dir)
    inputs = [args[args.index(name) + 1] for name in ("--manifest", "--journal", "--thresholds", "--baseline", "--scene-json")
              if name in args]
    if blend:
        inputs.append(blend)
    try:
        rc, stdout, stderr, evidence = worker.run(cmd, env_dir, timeout, executable=BLENDER,
                                                 inputs=inputs, runtime=(ROOT / "bpy_side", AD),
                                                 env=declared_env(env_dir, 2))
    except containment.ContainmentError as exc:
        return 2, "containment:" + str(exc)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("blender timeout") from exc
    return rc, (stdout + stderr).decode(errors="replace")


def ramws():
    """vadapt.ramws of the hash-pinned wheel (config/vision.json), never a live engine checkout."""
    _pin.pin()
    from vadapt import ramws as mod
    return mod


def need(rc_out, what):
    if rc_out[0] != 0:
        raise RuntimeError("%s rc=%d: %s" % (what, rc_out[0], rc_out[1][-500:]))


class Bench:
    def __init__(self, run_id=None, hook=None):
        self.hook = hook
        self.ws = ramws().open_run(RAM_CAMPAIGN, run_id or "e2e-%d" % os.getpid(), RAM_QUOTA, tool=RAM_TOOL)
        try:
            self._setup()
        except BaseException:
            self.close()
            raise

    def close(self):
        """Release the RAM workspace (idempotent); the receipt of the last run stays in self.ram_receipt."""
        if not self.ws._closed:
            self.ram_receipt = self.ws.receipt()
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _stage(self, rc_out, what):
        need(rc_out, what)
        self.ws.account()
        if self.hook:
            self.hook(what, self)

    def _setup(self):
        self.tmp = self.ws.path("e2e/")
        self.base = self.tmp / "base.blend"
        self._stage(blender(self.tmp, ["--python", AD / "make_base.py", "--", "--out", self.base]), "make_base")
        self.scene = self.tmp / "scene.manifest.json"
        self.scene.write_text(json.dumps({
            "schema": "bvqa.scene/v1", "budgets": {"tris_object": 200000, "tris_scene": 1000000},
            "objects": [{"name": n, "tags": t.split("|")} for n, t in sorted(TAGS.items())]}))
        self.journal = self.tmp / "journal.json"
        self.journal.write_text(json.dumps({"schema": "bvqa.journal/v1", "frames": {"list": list(FRAMES)}, "camera": "Cam",
                                            "scene": "Scene", "view_layer": "ViewLayer"}))
        self.entries = {e["id"]: e for e in json.loads((AD / "mutant-manifest.json").read_text())["mutants"]}
        self.clean_report = None

    # --- stages ---------------------------------------------------------------------------------------------------
    def apply(self, entry):
        mid = entry["id"]
        spec = self.tmp / (mid + ".spec.json")
        spec.write_text(json.dumps({"id": mid, "fault": entry["fault"]}))
        out, rcpt = self.tmp / (mid + ".blend"), self.tmp / (mid + ".receipt.json")
        self._stage(blender(self.tmp, ["--python", ROOT / "bpy_side" / "apply_mutant.py", "--", "--spec", spec, "--out", out,
                                "--receipt", rcpt], blend=self.base), "apply " + mid)
        return out, json.loads(rcpt.read_text())

    def sd_rows(self, blend, name, baseline=None):
        out = self.tmp / (name + ".scene.jsonl")
        args = ["--python", ROOT / "bpy_side" / "scene_checks.py", "--", "--manifest", self.scene, "--journal", self.journal,
                "--thresholds", ROOT / "catalogue" / "thresholds.tsv", "--out", out]
        if baseline:
            args += ["--baseline", baseline]
        self._stage(blender(self.tmp, args, blend=blend), "scene_checks " + name)
        rows, _ = scene_mod.load_scene(str(out))
        return out, rows

    def render(self, blend, name, engine="CYCLES"):
        run = self.tmp / (name + "." + engine.lower())
        shutil.rmtree(run, ignore_errors=True)
        run.mkdir(parents=True)
        self._stage(blender(self.tmp, ["--python", AD / "render_run.py", "--", "--out", run, "--frames", "1:4", "--res", RES, "--spp",
                                SPP, "--engine", engine], blend=blend), "render " + name)
        return run

    def truth(self, run):
        (run / "journal.json").write_text(self.journal.read_text())
        (run / "scene.manifest.json").write_text(self.scene.read_text())
        self._stage(blender(self.tmp, ["--python", ROOT / "bpy_side" / "passes.py", "--", "--run", run, "--frames", "1:4",
                                "--truth-seed", 0, "--truth-spp", 1, "--scene-json", run / "scene.manifest.json",
                                "--journal", run / "journal.json", "--threads", 2], blend=run / "pi.blend"), "passes")

    def vision(self, run):
        """Engine = the hash-pinned wheel of config/vision.json in a fresh interpreter (vision_stage), never the live
        vision-engine source tree: its uncommitted work changes vc_* outcomes under this harness (BFX9d E)."""
        rcpt = vision_stage.run_stage(run, vision="on")
        if rcpt.get("status") != "COMPLETE":
            raise RuntimeError("vision stage: %r" % (rcpt,))
        if getattr(self, "hook", None):
            self.hook("vision", self)
        return rcpt

    # --- findings -------------------------------------------------------------------------------------------------
    @staticmethod
    def fold(findings, check_id, ri, verdict, reason):
        findings.append({"check_id": check_id, "render_index": ri, "verdict": verdict, "reason": reason})

    def sd_findings(self, rows):
        per = {}
        for r in rows:
            if "verdict" not in r:
                continue
            ri = None if r.get("frame") is None else r["frame"] - 1
            per.setdefault((r["oracle_id"], ri), []).append(r["verdict"])
        out = []
        for (oid, ri), vs in sorted(per.items(), key=lambda kv: (kv[0][0], -1 if kv[0][1] is None else kv[0][1])):
            self.fold(out, oid, ri, harness.max_precedence(vs), "sd")
        return out

    def px_findings(self, run, with_zfight=None):
        meta = json.loads((run / "meta.json").read_text())
        out = []
        rgbs, ma, ob = [], [], []
        for f in FRAMES:
            w, h, data = png.decode((run / "render" / ("frame_%04d.png" % f)).read_bytes())
            rgbs.append(np.frombuffer(bytes(data), np.uint8).reshape(h, w, 3))
            ma.append(np.load(run / "truth" / ("f%06d.ma.npy" % f)))
            ob.append(np.load(run / "truth" / ("f%06d.ob.npy" % f)))
        vt = meta["view_settings"]
        for i, rgb in enumerate(rgbs):
            v, why, _ = pixel.frame_magenta(rgb, PX["magenta_num"], PX["magenta_den"], vt)
            self.fold(out, "frame.magenta", i, v, why)
            v, why, _ = pixel.frame_black(rgb, PX["black_dark_max"], PX["black_num"], PX["black_den"])
            self.fold(out, "frame.black", i, v, why)
            v, why, _ = pixel.frame_uniform(rgb, PX["uniform_spread"])
            self.fold(out, "frame.uniform", i, v, why)
            v, why, _, names = pixel.magenta_attrib(rgb, ma[i], meta["materials"], vt)
            self.fold(out, "bp.magenta.attrib", i, v, why + ":" + ",".join(names))
        v, why, _ = pixel.frame_frozen(rgbs)
        self.fold(out, "frame.frozen", None, v, why)
        for i in range(len(FRAMES)):
            v, why, _ = pixel.subject_visible([ob[i]], meta["objects"]["Subject"], PX["subject_min_ppm"])
            self.fold(out, "bp.subject.visible", i, v, why)
        vis = load_engine_vision(run / "vision")
        for name, pred in vision.PREDICATES.items():
            v, why, _ = pred(vis, VISION_PARAMS[name], "on")
            self.fold(out, name, None, v, why)
        return out

    def zfight_finding(self, blend, name):
        """bp.zfight.pixel on Workbench (deterministic renderer): ob-id stand-in = packed beauty rgb of frames 1 and 2."""
        run = self.render(blend, name, "BLENDER_WORKBENCH")
        meta = json.loads((run / "meta.json").read_text())
        pk = []
        for f in (1, 2):
            w, h, data = png.decode((run / "render" / ("frame_%04d.png" % f)).read_bytes())
            a = np.frombuffer(bytes(data), np.uint8).reshape(h, w, 3).astype(np.int64)
            pk.append(a[..., 0] * 65536 + a[..., 1] * 256 + a[..., 2])
        box = [meta["bboxes"][n] for n in sorted(meta["bboxes"]) if n.startswith("Static")]
        v, why, _ = pixel.zfight_pixel(pk[0], pk[1], box, True)
        return [{"check_id": "bp.zfight.pixel", "render_index": None, "verdict": v, "reason": why}]

    def observe(self, blend, name, baseline=None, zfight=False):
        sd_path, rows = self.sd_rows(blend, name, baseline)
        run = self.render(blend, name)
        self.truth(run)
        self.vision(run)
        findings = self.sd_findings(rows) + self.px_findings(run)
        if zfight:
            findings += self.zfight_finding(blend, name)
        verdict = harness.max_precedence([f["verdict"] for f in findings])
        return {"verdict": verdict, "rc": harness.rc_of(verdict), "findings": findings}, sd_path

    def clean(self):
        if self.clean_report is None:
            self.clean_report, self.clean_sd = self.observe(self.base, "clean", zfight=True)
        return self.clean_report

    def qualify(self, mid):
        e = self.entries[mid]
        clean = self.clean()
        blend, receipt = self.apply(e)
        report, _ = self.observe(blend, mid, baseline=self.clean_sd, zfight=True)
        return harness.qualify_mutant(judgeless_entry(e), clean, report, receipt), report


def load_engine_vision(vdir):
    """Real visioncore output (vision.json manifest + frames.jsonl + regions.jsonl) -> the dict the vc_* predicates read.

    oracles.vision.load_vision expects inline frames/regions lists; the engine writes them as jsonl beside the manifest
    (OPEN ISSUE reported to FO2), so the harness assembles the view itself and fails closed (None) on anything missing.
    """
    try:
        man = json.loads((vdir / "vision.json").read_text())
        rows = lambda n: [json.loads(x) for x in (vdir / n).read_text().splitlines() if x.strip()]
        return {"schema": vision.VISION_SCHEMA, "frames": rows("frames.jsonl"), "regions": rows("regions.jsonl"),
                "n_frames": man.get("n_frames")}
    except (OSError, ValueError):
        return None


def judgeless_entry(e):
    """Entry restricted to the executed sd|px checks (judge rows come from wave B5); fault sha stays re-hashing."""
    j = dict(e)
    j["expected_check_ids"] = sorted(set(e["expected_sd"]).union(e["expected_px"]))
    return j


if __name__ == "__main__":  # usage: e2e.py [mutant_id ...]  (scratch in the RAM workspace, released at exit)
    with Bench() as b:
        for mid in sys.argv[1:] or sorted(b.entries):
            r, _ = b.qualify(mid)
            print(json.dumps(r, sort_keys=True), flush=True)
