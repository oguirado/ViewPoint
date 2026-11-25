# SPDX-License-Identifier: AGPL-3.0-only
"""Offline native contact replay against a pinned orthogonal recess reference."""

import argparse
from hashlib import sha256
import json
from math import prod
from pathlib import Path
import re
import resource
import sys
from time import perf_counter_ns
from xml.etree import ElementTree as ET

from . import recess_oracle as oracle


SCHEMA = "vp3d.collision.recess.fixture/v1"
REFERENCE = "vp3d.collision.recess.reference/v1"
BASIS = "right-handed-world-x-travel-y-lateral-z-up"
BODY = re.compile(r"[a-z][a-z0-9_]{0,31}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_UM = 10_000_000


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def read_pinned(path, pin):
    require(isinstance(pin, str) and DIGEST.fullmatch(pin), "INVALID_SHA256")
    with path.open("rb") as handle:
        blob = handle.read(262_145)
    require(len(blob) <= 262_144 and sha256(blob).hexdigest() == pin, "INPUT_PIN_MISMATCH")

    def unique(items):
        result = dict(items)
        require(len(result) == len(items), "DUPLICATE_JSON_KEY")
        return result

    return json.loads(blob, object_pairs_hook=unique)


def integer(x, low, high):
    return type(x) is int and low <= x <= high


def vector(value, size, low=-MAX_UM):
    require(isinstance(value, list) and len(value) == size
            and all(integer(x, low, MAX_UM) for x in value), "COORDINATE_RANGE")
    return tuple(value)


def boxes(value):
    require(isinstance(value, list) and 1 <= len(value) <= 64, "HULL_COUNT")
    result = []
    for entry in value:
        lo, hi = (vector(point, 3) for point in entry)
        require(all(a < b for a, b in zip(lo, hi)), "INVALID_BOX")
        result.append((lo, hi))
    # Per-body sums are physical volumes only for non-overlapping solids.
    for i, a in enumerate(result):
        for b in result[i + 1:]:
            require(any(a[1][k] <= b[0][k] or b[1][k] <= a[0][k] for k in range(3)),
                    "OVERLAPPING_PROXY_HULLS")
    return tuple(result)


def load_fixture(path, pin):
    fixture = read_pinned(path, pin)
    require(set(fixture) == {"schema", "length_unit", "basis", "engine", "motion",
                             "tolerances", "proxy", "reference"}
            and fixture["schema"] == SCHEMA and fixture["length_unit"] == "um"
            and fixture["basis"] == BASIS, "FIXTURE_CONTRACT")
    source = fixture["reference"]
    require(set(source) == {"file", "sha256"}
            and re.fullmatch(r"[a-z0-9_-]+[.]json", source["file"]), "REFERENCE_PATH")
    reference_path = path.parent / source["file"]
    require(not reference_path.is_symlink(), "REFERENCE_SYMLINK")
    reference = read_pinned(reference_path, source["sha256"])
    require(set(reference) == {"schema", "length_unit", "basis", "bodies"}
            and reference["schema"] == REFERENCE and reference["length_unit"] == "um"
            and reference["basis"] == BASIS, "REFERENCE_CONTRACT")
    require(isinstance(fixture["proxy"], dict) and 1 <= len(fixture["proxy"]) <= 16
            and set(fixture["proxy"]) == set(reference["bodies"]), "BODY_COVERAGE")
    proxy, fine = {}, {}
    for body in sorted(fixture["proxy"]):
        require(BODY.fullmatch(body) and body != "peg", "BODY_NAME")
        proxy[body] = boxes(fixture["proxy"][body])
        part = reference["bodies"][body]
        require(set(part) == {"outline_xy_um", "z_um"}, "REFERENCE_EXTRUSION")
        z = vector(part["z_um"], 2)
        require(z[0] < z[1], "REFERENCE_Z_RANGE")
        outline = [vector(point, 2) for point in part["outline_xy_um"]]
        fine[body] = oracle.strips(outline, z)

    motion = fixture["motion"]
    require(set(motion) == {"start_um", "peg_half_um", "step_x_um", "ticks", "timestep_us"},
            "MOTION_CONTRACT")
    start = vector(motion["start_um"], 3)
    half = vector(motion["peg_half_um"], 3, low=1)
    ticks, step, dt = motion["ticks"], motion["step_x_um"], motion["timestep_us"]
    require(integer(ticks, 1, 5_000) and integer(step, 1, MAX_UM)
            and integer(dt, 100, 100_000)
            and max(abs(start[0]), abs(start[0] + ticks * step)) + half[0] <= MAX_UM
            and all(abs(start[i]) + half[i] <= MAX_UM for i in (1, 2)), "MOTION_RANGE")
    tolerance = fixture["tolerances"]
    require(set(tolerance) == {"ambiguity_um", "clearance_um", "native_depth_um"}
            and all(integer(value, 0, 1000) for value in tolerance.values())
            and tolerance["clearance_um"] <= tolerance["ambiguity_um"], "TOLERANCE_CONTRACT")
    return fixture, proxy, fine, start, half


def proxy_clearance(peg, hulls):
    return min(max(max(hull[0][i] - peg[1][i], peg[0][i] - hull[1][i]) for i in range(3))
               for hull in hulls)


def volume(boxes):
    return sum(prod(b - a for a, b in zip(lo, hi)) for lo, hi in boxes)


def aggregates(proxy, fine):
    pairs = {}
    for body, hulls in proxy.items():
        outer, expected = oracle.envelope(hulls), oracle.envelope(fine[body])
        pairs[body] = {
            "hull_count": len(hulls),
            "envelope_um": outer,
            "fine_envelope_um": expected,
            "envelope_delta_um": [[a - b for a, b in zip(x, y)] for x, y in zip(outer, expected)],
            "proxy_volume_um3": volume(hulls),
            "fine_volume_um3": volume(fine[body]),
            "volume_delta_um3": volume(hulls) - volume(fine[body]),
        }
    return pairs


def metres(coords):
    return " ".join(f"{coord / 1_000_000:.7f}" for coord in coords)


def native_model(mj, proxy, half, start, dt):
    root = ET.Element("mujoco", model="offline_recess")
    ET.SubElement(root, "compiler", fusestatic="false")
    ET.SubElement(root, "option", timestep=metres((dt,)), gravity="0 0 0")
    world = ET.SubElement(root, "worldbody")
    for name, hulls in proxy.items():
        body = ET.SubElement(world, "body", name=name)
        for lo, hi in hulls:
            ET.SubElement(body, "geom", type="box", margin="0",
                          pos=metres([(a + b) / 2 for a, b in zip(lo, hi)]),
                          size=metres([(b - a) / 2 for a, b in zip(lo, hi)]))
    peg = ET.SubElement(world, "body", name="peg", pos=metres(start))
    ET.SubElement(peg, "joint", type="slide", axis="1 0 0")
    ET.SubElement(peg, "geom", type="box", margin="0", size=metres(half), mass="1")
    model = mj.MjModel.from_xml_string(ET.tostring(root, encoding="unicode"))
    require(model.nq == model.nv == 1 and round(model.opt.timestep * 1_000_000) == dt
            and set(model.body(i).name for i in range(1, model.nbody)) == {*proxy, "peg"},
            "NATIVE_MODEL_BINDING")
    return model, mj.MjData(model)


def native_contacts(mj, model, data, offset):
    # Evaluate contact at the prescribed pre-integration pose. Each sample resets
    # dynamical state, so a false collision cannot prevent later observations.
    mj.mj_resetData(model, data)
    data.qpos[0] = offset / 1_000_000
    start_ns = perf_counter_ns()
    mj.mj_step(model, data)
    step_ns = perf_counter_ns() - start_ns
    contacts = {}
    for point in data.contact:
        names = [model.body(int(model.geom_bodyid[g])).name for g in (point.geom1, point.geom2)]
        require("peg" in names and names[0] != names[1], "UNEXPECTED_NATIVE_PAIR")
        if point.dist <= 0:
            body = names[1 - names.index("peg")]
            penetration = round(-point.dist * 1_000_000)
            contacts[body] = max(contacts.get(body, 0), penetration)
    return contacts, step_ns


def inspect_tick(tick, time_us, peg, contacts, proxy, fine, trees,
                 mode, tolerance):
    rows = []
    for body, hulls in proxy.items():
        coarse = proxy_clearance(peg, hulls)
        parts = fine[body]
        if mode == "exhaustive":
            measured = min(oracle.box_clearance(peg, box) for box in parts)
            probes = len(parts)
        else:
            measured, probes = oracle.selective_clearance(peg, trees[body])
        ambiguous = min(coarse, measured) <= tolerance["ambiguity_um"]
        if mode == "adaptive" and ambiguous and probes < len(parts):
            # Recheck the whole pair at the contact boundary, including strips
            # eliminated by pruning. A singleton already has full coverage.
            confirmed = min(oracle.box_clearance(peg, box) for box in parts)
            require(confirmed == measured, "FINE_BOUND_DISAGREEMENT")
            probes += len(parts)
        native_depth = contacts.get(body)
        native_matches_proxy = ((native_depth is not None) == (coarse <= 0)
                                and (native_depth is None or
                                     abs(native_depth + coarse) <= tolerance["native_depth_um"]))
        reason = None
        if not native_matches_proxy:
            reason = "NATIVE_PROXY_MISMATCH"
        elif coarse <= 0 < measured:
            reason = "FALSE_PROXY_CONTACT"
        elif measured <= 0 < coarse:
            reason = "MISSED_PROXY_CONTACT"
        elif abs(coarse - measured) > tolerance["clearance_um"]:
            reason = "CLEARANCE_MISMATCH"
        rows.append({"tick": tick, "time_us": time_us, "pair": ["peg", body],
                     "proxy_clearance_um": coarse, "fine_clearance_um": measured,
                     "native_penetration_um": native_depth, "ambiguous_pair": ambiguous,
                     "fine_strips_inspected": probes, "reason": reason})
    return rows


def contact_trace(rows, field, *, native=False):
    return [{"tick": row["tick"], "time_us": row["time_us"], "pair": row["pair"]}
            for row in rows if row[field] is not None and (native or row[field] <= 0)]


def run(path, pin, mode, mj):
    started = perf_counter_ns()
    fixture, proxy, fine, start, half = load_fixture(path, pin)
    require(fixture["engine"] == {"backend": "native_mujoco", "version": mj.__version__},
            "NATIVE_RUNTIME_PIN")
    motion, tolerance = fixture["motion"], fixture["tolerances"]
    dt = motion["timestep_us"]
    trees = {body: oracle.index(parts) for body, parts in fine.items()}
    model, data = native_model(mj, proxy, half, start, dt)
    rows, step_ns = [], 0
    for tick in range(motion["ticks"] + 1):
        offset = tick * motion["step_x_um"]
        centre = (start[0] + offset, *start[1:])
        peg = (tuple(c - h for c, h in zip(centre, half)),
               tuple(c + h for c, h in zip(centre, half)))
        contacts, native_ns = native_contacts(mj, model, data, offset)
        step_ns += native_ns
        rows.extend(inspect_tick(tick, tick * dt, peg, contacts, proxy, fine,
                                 trees, mode, tolerance))

    traces = {
        "proxy": contact_trace(rows, "proxy_clearance_um"),
        "fine": contact_trace(rows, "fine_clearance_um"),
        "native": contact_trace(rows, "native_penetration_um", native=True),
    }
    faults = [row for row in rows if row["reason"]]
    worst = max(rows, key=lambda row: abs(row["proxy_clearance_um"] - row["fine_clearance_um"]))
    contact_fields = ("proxy_clearance_um", "fine_clearance_um")
    signed_penetration = {name: max(0, -min(row[field] for row in rows))
                          for name, field in zip(("proxy", "fine"), contact_fields)}
    receipt = {
        "schema": "vp3d.collision.recess.receipt/v1",
        "scope": "local_offline_fixture", "live_use_permitted": False,
        "fixture_sha256": pin, "reference_sha256": fixture["reference"]["sha256"],
        "basis": BASIS, "length_unit": "um", "engine": fixture["engine"],
        "native_engine_step_us": dt, "mode": mode, "tolerances": tolerance,
        "verdict": "REJECT" if faults else "PASS",
        "aggregates": aggregates(proxy, fine), "ordered_contact_trace": traces,
        "first_contacts": {name: trace[0] if trace else None for name, trace in traces.items()},
        "worst_signed_clearance_delta": worst,
        "worst_penetration_um": signed_penetration | {
            "native": max((row["native_penetration_um"] or 0 for row in rows), default=0),
        },
        "first_fault": faults[0] if faults else None,
        "first_contact_fault": next((row for row in faults if row["reason"] in {
            "FALSE_PROXY_CONTACT", "MISSED_PROXY_CONTACT", "NATIVE_PROXY_MISMATCH"}), None),
        "pair_samples": rows,
        "fine_strips_inspected": sum(row["fine_strips_inspected"] for row in rows),
        "ambiguous_pair_samples": sum(row["ambiguous_pair"] for row in rows),
        "metrics": {"native_step_ns": step_ns,
                    "native_steps": motion["ticks"] + 1,
                    "native_steps_per_s": (motion["ticks"] + 1) * 1_000_000_000 / step_ns,
                    "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024},
    }
    receipt["metrics"]["fixture_elapsed_ns"] = perf_counter_ns() - started
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--fixture-sha256", required=True)
    parser.add_argument("--mode", choices=("exhaustive", "adaptive"), required=True)
    args = parser.parse_args()
    try:
        import mujoco
        result = run(args.fixture, args.fixture_sha256, args.mode, mujoco)
    except (ImportError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print(f"RECESS_HARNESS_ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return int(result["verdict"] == "REJECT")


if __name__ == "__main__":
    sys.exit(main())
