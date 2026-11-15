"""Golden-fixture generator + checker for vadapt.synth. Run `python3 -m tests.synth_gen write` to regenerate."""
import sys
from pathlib import Path

from visioncore import canon
from vadapt.synth import SCENARIOS, render_scenario
from vadapt.synth.synthetic import build_manifest, frame_sha, frames_sha256

GOLDEN = Path(__file__).resolve().parent / "golden"
SEED = 7


def expectation(name):
    fr, truth, hints = render_scenario(name, SEED)
    shas = [frame_sha(f) for f in fr]
    return build_manifest(fr, name, SEED), {"schema": "vqa.synth-golden/v1", "scenario": name, "seed": SEED,
                                           "frames_sha256": frames_sha256(shas), "frame_sha256": shas,
                                           "truth": truth, "hints": hints}


def write_all():
    for name in SCENARIOS:
        man, exp = expectation(name)
        d = GOLDEN / name
        (d / "expect").mkdir(parents=True, exist_ok=True)
        (d / "frames.manifest.json").write_text(canon.dumps(man))
        (d / "expect" / "synth.json").write_text(canon.dumps(exp))
        (d / "expect" / "frames.sha256").write_text(exp["frames_sha256"] + "\n")


if __name__ == "__main__" and sys.argv[1:] == ["write"]:
    write_all()
