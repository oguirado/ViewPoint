"""Run with MV_OBS_STAGES_PATH unset and PYTHONPATH=agents."""
import statistics
import timeit
import tracemalloc

from metaviewer_agents import obsstamps
from metaviewer_agents.obsstamps import ENABLED


def main():
    assert not obsstamps.ENABLED
    baseline = timeit.Timer("pass")
    guard = timeit.Timer("if enabled: obsstamps.current()", globals={
        "enabled": ENABLED, "obsstamps": obsstamps,
    })
    iterations = 2_000_000
    deltas = []
    for pair in range(12):
        if pair % 2:
            disabled, off_baseline = guard.timeit(iterations), baseline.timeit(iterations)
        else:
            off_baseline, disabled = baseline.timeit(iterations), guard.timeit(iterations)
        measured = (off_baseline * 1e9 / iterations, disabled * 1e9 / iterations)
        deltas.append(measured[1] - measured[0])
        print(f"{measured[0]:.3f} {measured[1]:.3f} {deltas[-1]:+.3f} ns/op")
    print(f"paired median delta {statistics.median(deltas):+.3f} ns/op")
    for name, timer in (("baseline", baseline), ("disabled", guard)):
        tracemalloc.start()
        timer.timeit(100_000)
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"{name}: {retained} B retained, {peak} B peak (whole batch)")


if __name__ == "__main__":
    main()
