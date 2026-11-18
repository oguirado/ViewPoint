"""Explicit C5 host acceptance runs; discovery launches no Blender processes."""
import argparse
import json
import os
from pathlib import Path
import time

from vqa3d import profile
from vqa3d.adapters.blender import BlenderAdapter
from vqa3d.check import verify_step, path_parts
from vqa3d.fakes import RecordingAdapter
from tests.test_vqa3d_profiles import selected

COMPONENT = Path(__file__).resolve().parents[1]
LOG_ROOT = COMPONENT / os.environ.get("VQA3D_LIVE_LOG_DIR", "build/vqa3d-live")
CAMPAIGN = COMPONENT / os.environ.get("VQA3D_C5_CAMPAIGN_DIR", "build/vqa3d-campaign")
EVIDENCE = COMPONENT / os.environ.get("VQA3D_C5_EVIDENCE_DIR", "build/vqa3d-evidence")
KERNEL = str(COMPONENT / os.environ.get("VQA3D_C5_KERNEL_DIR", "build/cycles_source_gb10"))


def write(label, obj):
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    path = LOG_ROOT / ("C5_" + label + ".json")
    path.write_text(json.dumps(obj, indent=2) + "\n")
    return str(path)


def gpu(with_env):
    label = "gpu_env" if with_env else "gpu_noenv"
    adapter = BlenderAdapter(EVIDENCE / "handoff_s3/final_scene_v3.blend", profile_session=True,
        timeout=420, nice=10, cpus="0-15", blender_env={"CYCLES_KERNEL_PATH": KERNEL} if with_env else {})
    policy = selected(profile.load("film-animation"), "gpu-render")
    started = time.monotonic()
    try:
        receipts, summary, trace = profile.run(policy, adapter)
        print(json.dumps({"label":label,"receipts":[{"id":r.get("check_id"),"verdict":r["final_verdict"],"error":r["error"],"sha256":r["receipt_sha256"]} for r in receipts]}),flush=True)
    finally:
        adapter.close()
    write(label + "_trace", trace)
    path = write(label, {"receipts":receipts,"summary":summary,"probe":trace["probe"],
                         "processes":adapter.process_logs,"wall_s":round(time.monotonic()-started,3)})
    print(json.dumps({"path":path}),flush=True)
    target = next(r for r in receipts if r.get("check_id") == "gpu-render:scene")
    assert target["final_verdict"] in ({"PASS", "CANNOT_SEE"} if with_env else {"CANNOT_SEE"})
    assert target.get("gpu_probe"), "GPU receipt must retain diagnostics"
    if with_env:
        diag=target["gpu_probe"]["CUDA"]
        assert diag["child_environment"]["CYCLES_KERNEL_PATH"]["path"] == KERNEL
        assert target["final_verdict"] == "PASS" or target["error"].startswith("gpu_probe_failed:CUDA:")


def animation():
    obligation=json.loads((CAMPAIGN / "feedback/scenarios_c5/n08b_set_frame_allow_animated.json").read_text())[0]
    source=EVIDENCE / "handoff_s1/motion.blend"
    for mutant in (False, True):
        adapter=BlenderAdapter(source,nice=10,cpus="0-15",profile_session=True,timeout=180)
        recorder=RecordingAdapter(adapter)
        static=None
        try:
            if mutant:
                state=adapter.scene_state()
                animated={path_parts(path)[1] for path in state["animation_paths"]}
                static=next(name for name,row in state["objects"].items() if name not in animated and row["type"] == "MESH")
                original=adapter.act
                def act(action):
                    result=original(action)
                    row=adapter.scene_state()["objects"][static]
                    value=list(row["location_um"]);value[0]+=10000
                    original({"op":"set_location","object":static,"value_um":value})
                    return result
                adapter.act=act
            receipt=verify_step(recorder,**obligation)
        finally: adapter.close()
        label="n08b_static_mutant" if mutant else "n08b_animated"
        path=write(label,{"receipt":receipt,"static_mutant_object":static,"processes":adapter.process_logs})
        write(label+"_trace",recorder.trace([obligation],[receipt],{}))
        print(json.dumps({"path":path,"verdict":receipt["final_verdict"],"error":receipt["error"],"receipt":receipt["receipt_sha256"]}),flush=True)
        assert receipt["final_verdict"] == ("FAIL" if mutant else "PASS"), receipt["error"]
        if mutant: assert static.replace("/","~1") in receipt["error"]


if __name__ == "__main__":
    parser=argparse.ArgumentParser();parser.add_argument("mode",choices=["gpu-env","gpu-noenv","animation"])
    mode=parser.parse_args().mode
    if mode == "animation": animation()
    else: gpu(mode == "gpu-env")
