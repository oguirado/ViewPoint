"""R2's synthetic scene using the real lifecycle, capture and local providers."""

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
from time import monotonic_ns

from ports import FramePorts, binding_for, demand_for, session_for
from runtime import MAX_XWD_BYTES, SCREEN_HEIGHT, SCREEN_WIDTH, WORK, fixture_site, open_browser, require


@contextmanager
def open_vision(display):
    from bus.broker import Broker
    from driver import enter_runner
    from frames.source import FrameSource
    from perception.baseline.inventory import collect
    from perception.demand import DemandCoordinator
    from perception.lifecycle import PerceptionLifecycle, RuntimeEvidence
    from perception.stream import PerceptionStream
    from privacy import PrivacySentinel
    from vadapt import cu_ocr, rtring, x11_ramcap
    from vision import VisionFrames, VisionMode, local_adapters

    tessdata = os.environ.get("QD_TESSDATA", "/usr/share/tesseract-ocr/5/tessdata")
    require(cu_ocr.preflight(tessdata)["ok"], "R2 needs a working Tesseract English OCR provider")
    with ExitStack() as stack:
        port = stack.enter_context(fixture_site())
        browser = stack.enter_context(open_browser(display, f"http://127.0.0.1:{port}/index.html"))
        require(browser.page.read_bool("""(() => {
            const scene = document.createElement('h1');
            scene.id = 'qd-scene';
            scene.textContent = 'Original synthetic scene';
            const button = document.createElement('button');
            button.textContent = 'Continue';
            button.style.cssText = 'font-size:32px;padding:16px 40px;border:3px solid #222';
            document.body.replaceChildren(scene, button);
            return true;
        })()"""), "could not open the synthetic R2 scene")

        frame = FramePorts(browser, scene=True)
        require(not frame.current().hidden, "R2 scene must be visible before starting")
        binding = binding_for(browser)
        session = session_for(browser)
        agent = demand_for(binding, "agent")
        capture = demand_for(binding, "vision")
        coordinator = DemandCoordinator(binding["surface_id"], interests={"VERIFIED"},
                                        authorize=lambda row: row in (agent, capture), drain=lambda: True)
        privacy_binding = frame.current().privacy_binding()
        sentinel = PrivacySentinel(privacy_binding, frame.metadata, record_gap=lambda *_: None,
                                   request_human=lambda _: None)

        def current(surface_id):
            return frame.current() if surface_id == binding["surface_id"] else None

        source = FrameSource(current, {"cdp": frame.acquire}, coordinator.register,
                             privacy_for=lambda state: sentinel if state.privacy_binding() == privacy_binding else None)
        vision_frames = VisionFrames(source, current, capture)
        config = x11_ramcap.SourceConfig(str(display.frame_dir), "Xvfb_screen0",
                                         SCREEN_WIDTH, SCREEN_HEIGHT, f"Xvfb process {display.server.pid} -nocursor")
        stream = PerceptionStream(binding, config, ring_slots=2, interval_ns=20_000_000,
                                  ttl_ns=3_000_000_000, max_frame_bytes=MAX_XWD_BYTES,
                                  max_ring_bytes=rtring.ring_bytes(2, config.width, config.height))
        stack.callback(stream.close)
        broker = stack.enter_context(Broker(WORK / "vision-broker"))
        broker.bind(session["session_id"], binding, lease_state="AGENT")

        def qualified(candidate):
            browser.check()
            return candidate == binding

        def runtime():
            scope, = collect()["scopes"]
            pids = {record["pid"] for record in scope["processes"]}
            require(os.getpid() in pids and not {browser.child.pid, display.server.pid} & pids,
                    "R2 producer identities overlap the runner")
            return RuntimeEvidence(binding["incarnation"], tuple(sorted(pids - {os.getpid()})),
                                   scope["current_bytes"])

        enter_runner()
        lifecycle = PerceptionLifecycle(broker, session["session_id"], binding, coordinator, agent, stream,
                                        qualified=qualified, runtime=runtime, drain_agent=lambda _: mode.drain())
        marks = Path("/dev/shm/vqa-cu")
        marks.mkdir(mode=0o700, exist_ok=True)
        components = Path(__file__).resolve().parents[3]
        adapters = local_adapters(ve_root=components / "vision-engine", wender_root=components / "wender-agent",
                                  timeout=5, marks_out=marks / "qd-vision.png", ocr_marks=True,
                                  tessdata_prefix=tessdata)
        mode = VisionMode(lifecycle, vision_frames, adapters, session_origin_ns=monotonic_ns(),
                          capsule_id=session["capsule_id"])
        stack.callback(mode.drain)

        def change_scene():
            browser.check()
            require(browser.page.read_bool("""(() => {
                const scene = document.getElementById('qd-scene');
                scene.textContent = 'Updated synthetic scene — visible change for local vision';
                return scene.isConnected;
            })()"""), "could not update the synthetic R2 scene")

        yield mode, lifecycle, change_scene
