"""GUI main-thread script bridge; launch explicitly with --python this_file.

Protocol matches bridge_A.py: inbox/*.py -> outbox/<stem>.out, OK/ERROR
first line. Both peers publish with atomic rename. Never use the take's dirs.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import traceback


def install(bpy):
    directory = os.environ.get("VQA3D_LIVE_DIR")
    if not directory:
        raise RuntimeError("VQA3D_LIVE_DIR_required")
    root = Path(directory).resolve()
    inbox, outbox = root / "inbox", root / "outbox"
    for directory in (root, inbox, outbox):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    (root / "bridge.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    namespace = bpy.app.driver_namespace
    key = "_vqa3d_live_bridge"
    if key in namespace:
        if namespace[key]["root"] != str(root):
            raise RuntimeError("live_bridge_already_attached_elsewhere")
        return

    def poll():
        for path in sorted(inbox.glob("*.py")):
            output = io.StringIO()
            status = "OK"
            try:
                source = path.read_bytes()
                path.unlink()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    exec(compile(source, str(path), "exec"), {"bpy": bpy, "__name__": "__bridge__"})
            except Exception:
                status = "ERROR"
                # Payload prints its structured ERROR last; keep it last.
                if "VQA3D_RESPONSE " not in output.getvalue():
                    output.write(traceback.format_exc())
            target = outbox / (path.stem + ".out")
            temp = outbox / (path.stem + ".out.tmp")
            with temp.open("w", encoding="utf-8") as stream:
                stream.write(status + "\n" + output.getvalue())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        return 0.05

    namespace[key] = {"root": str(root), "poll": poll}
    bpy.app.timers.register(poll, first_interval=0.1, persistent=True)
    print("VQA3D LIVE BRIDGE READY", str(root), flush=True)


if __name__ == "__main__":
    import bpy
    install(bpy)
