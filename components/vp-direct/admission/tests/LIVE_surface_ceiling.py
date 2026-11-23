"""LIVE: test the P6 ceiling with kernel readings and synthetic incumbent IDs."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from admission import AdmissionGate, AdmissionPlan, UsageSnapshot


def main():
    paths = [line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
             if line.startswith("0::")]
    if len(paths) != 1:
        raise SystemExit("AUSENTE: current unified cgroup membership")
    scope = Path("/sys/fs/cgroup") / paths[0].lstrip("/")
    if ((scope / "memory.high").read_text().strip() != str(6 << 30)
            or (scope / "memory.max").read_text().strip() != str(8 << 30)):
        raise SystemExit("Run the LIVE script inside the capped test scope")

    surfaces = {f"fixture-{index}": None for index in range(15)}
    gate = AdmissionGate(lambda: UsageSnapshot(surfaces), scopes=[("live-fixture", paths[0])])
    with gate.reserve(AdmissionPlan("fixture-15", "ACTIVE", "cdp", False)) as sixteenth:
        if not sixteenth["admitted"] or sixteenth["surface_count"] != 16:
            raise SystemExit("The live memory evidence did not qualify the sixteenth surface")
        surfaces["fixture-15"] = None

    with gate.reserve(AdmissionPlan("fixture-16", "ACTIVE", "cdp", False)) as seventeenth:
        if (seventeenth["admitted"] or seventeenth["surface_count"] != 17
                or seventeenth["margin_percent"] < 15):
            raise SystemExit("The live measurement did not isolate the surface ceiling refusal")
    print(json.dumps([sixteenth, seventeenth], sort_keys=True))


if __name__ == "__main__":
    main()
