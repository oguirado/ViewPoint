"""Executed by the GUI bridge, bpy + stdlib only. No host imports.

Runtime-load blender_side.scene_state/apply to inherit C2a's schema after merge.
Draw and depsgraph counters are observation metadata, never object-state deltas.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import traceback

COUNTERS = "_vqa3d_live_counters"


def counters(bpy):
    ns = bpy.app.driver_namespace
    if COUNTERS not in ns:
        state = {"frame_seq": 0, "depsgraph_seq": 0, "pause_draw": False}

        def updated(*_):
            state["depsgraph_seq"] += 1

        def drawn():
            if not state["pause_draw"]:
                state["frame_seq"] += 1

        state["updated"] = updated
        state["drawn"] = drawn
        state["draw_handle"] = bpy.types.SpaceView3D.draw_handler_add(drawn, (), "WINDOW", "POST_PIXEL")
        bpy.app.handlers.depsgraph_update_post.append(updated)
        ns[COUNTERS] = state
    return ns[COUNTERS]


def redraw(bpy):
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def side_module():
    path = Path(__file__).with_name("blender_side.py")
    spec = importlib.util.spec_from_file_location("vqa3d_live_headless_state", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scene_state(bpy, side):
    state = side.scene_state(bpy)
    state["frame_current"] = int(bpy.context.scene.frame_current)
    state["active_object"] = bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None
    state["mode"] = bpy.context.mode
    return state


def render(bpy, request):
    scene = bpy.context.scene
    root = Path(request["live_dir"]).resolve()
    target = root / (request["request_id"] + ".png")
    side = side_module()
    with side.preserve_settings(bpy):
        side.configure_capture(bpy, request)
        scene.render.filepath = str(target)
        bpy.ops.render.render(write_still=True)


def viewport_capture(bpy, request):
    """Optional diagnostic only; never substitutes for the scene-camera image."""
    path = Path(request["live_dir"]) / (request["request_id"] + ".viewport.png")
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region:
                try:
                    with bpy.context.temp_override(window=window, area=area, region=region):
                        result = bpy.ops.screen.screenshot_area(filepath=str(path))
                    return {"available": "FINISHED" in result and path.is_file(), "path": str(path)}
                except Exception as exc:
                    return {"available": False, "reason": type(exc).__name__ + ":" + str(exc)}
    return {"available": False, "reason": "view3d_context_missing"}


def dispatch(bpy, request):
    seq = counters(bpy)
    side = side_module()
    scene = bpy.context.scene
    result = {"request_id": request["request_id"], "ok": True,
              "runtime_version": bpy.app.version_string, "background": bpy.app.background,
              "camera": scene.camera.name if scene.camera else None,
              "camera_source": scene.get("vqa3d_camera_source", "unknown") if scene.camera else "none",
              "has_view3d": any(area.type == "VIEW_3D" for window in bpy.context.window_manager.windows
                                for area in window.screen.areas)}
    if scene.camera and not scene.camera.get("vqa3d_fallback_camera", False):
        # GUI attachment records a pre-existing user camera explicitly.
        scene["vqa3d_camera_source"] = "scene"
        result["camera_source"] = "scene"
    if bpy.app.background:
        raise RuntimeError("blender_background_not_GUI")
    mode = request["mode"]
    if mode == "act":
        result["before_frame_seq"] = seq["frame_seq"]
        action = request["action"]
        if action["op"] == "run_script":
            source = base64.b64decode(request["script_base64"], validate=True)
            if hashlib.sha256(source).hexdigest() != request["script_sha256"]:
                raise ValueError("run_script_sha256_mismatch")
            exec(compile(source, action["path"], "exec"), {"bpy": bpy, "__name__": "__vqa3d_script__", "__file__": action["path"]})
        else:
            side.apply(bpy, action)
        bpy.context.view_layer.update()
    elif mode == "render_caps":
        result["render_capabilities"] = side.render_capabilities(bpy)
    elif mode == "gpu_probe":
        result["gpu_render_ok"] = side.gpu_probe(bpy, request["device_type"],
            width=request.get("probe_width", 8), height=request.get("probe_height", 8), samples=request.get("probe_samples", 1))
    elif mode not in ("ping", "state", "capture"):
        raise ValueError("unsupported_live_request")
    if mode == "capture":
        after = request.get("after_seq")
        if after is not None and seq["frame_seq"] <= after:
            result["reason"] = "stale_frame_timeout"
        elif scene.camera is None:
            result["reason"] = "scene_camera_missing"
        else:
            render(bpy, request)
            if request.get("viewport"):
                result["viewport"] = viewport_capture(bpy, request)
    result["state"] = scene_state(bpy, side)
    result.update(frame_seq=seq["frame_seq"], depsgraph_seq=seq["depsgraph_seq"])
    redraw(bpy)
    return result


def main(bpy, request):
    try:
        result = dispatch(bpy, request)
    except Exception as exc:
        result = {"request_id": request["request_id"], "ok": False,
                  "error": type(exc).__name__ + ":" + str(exc), "traceback": traceback.format_exc()}
        print("VQA3D_RESPONSE " + json.dumps(result, sort_keys=True), flush=True)
        raise
    print("VQA3D_RESPONSE " + json.dumps(result, sort_keys=True), flush=True)


if "VQA3D_REQUEST" in globals():
    main(bpy, VQA3D_REQUEST)
