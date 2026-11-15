"""Seal and verify the GUI benchmark before held-out access; fail closed, rc 5."""
import argparse
import hashlib
import gzip
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = Path(__file__).with_name("PREREG.json")
BUS_DIR = Path("~/Música/TeamWork_Antigravity2/campaigns/visual-qa-0/v12e/room_FXP1W")
SOURCE_NAMES = ("__init__", "rng", "font", "raster", "scenes", "gen", "labels", "splits", "prereg", "run", "score", "signflip")
SCHEMA = "vqa.gui_synth.prereg/v1"
HISTORY_NAME = "SEAL_HISTORY.jsonl"
ORIGINAL_SHA = "c015d66c1a0284dc90123b6c5e4977227a53a8cfaafc720607a3071373bef93e"


def _entry_sha(row):
    return hashlib.sha256(json.dumps({k: v for k, v in row.items() if k != "entry_sha256"},
                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _history(target):
    """Validate the pinned predecessor and every content-linked transition."""
    try:
        lines = target.with_name(HISTORY_NAME).read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
        if not rows:
            raise PreregError("missing seal history")
        previous = None
        for row in rows:
            if (not isinstance(row, dict) or row.get("entry_sha256") != _entry_sha(row)
                    or row.get("prev_entry_sha256") != (previous["entry_sha256"] if previous else None)
                    or row.get("prev_prereg_sha256") != (previous["new_prereg_sha256"] if previous else None)
                    or not isinstance(row.get("reason"), str) or not row["reason"].strip()
                    or any(not isinstance(row.get(key), str) or len(row[key]) != 64
                           or any(c not in "0123456789abcdef" for c in row[key])
                           for key in ("new_prereg_sha256", "sources_tree_sha256"))):
                raise PreregError("broken seal history chain")
            previous = row
        if rows[0]["new_prereg_sha256"] != ORIGINAL_SHA:
            raise PreregError("unknown original seal")
        return rows
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PreregError("seal history: " + str(exc)) from exc


def _result_refs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("prereg_sha256", "prereg_sha") and item is not None:
                yield item
            else:
                yield from _result_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _result_refs(item)


def _check_results(root, known):
    directory = Path(root or ROOT) / "eval/results/gui_synth"
    for path in sorted((*directory.glob("*.json"), *directory.glob("*.json.gz"),
                        *directory.glob("*.jsonl"))):
        try:
            with (gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz"
                  else path.open(encoding="utf-8")) as stream:
                data = ([json.loads(line) for line in stream if line.strip()]
                        if path.suffix == ".jsonl" else json.load(stream))
            if any(ref not in known for ref in _result_refs(data)):
                raise PreregError("unknown result prereg SHA: " + path.name)
        except (OSError, ValueError, TypeError, EOFError) as exc:
            raise PreregError("result evidence: " + path.name + ": " + str(exc)) from exc



class PreregError(ValueError):
    code = "E_PREREG_SHA"
    rc = 5

    def __init__(self, detail):
        super().__init__(self.code + ": " + str(detail))


def sha256(path=None):
    return hashlib.sha256(Path(path or DEFAULT_PATH).read_bytes()).hexdigest()


def source_hashes(root=None):
    root = Path(root or ROOT)
    result = {}
    for name in SOURCE_NAMES:
        rel = "eval/gui_synth/" + name + ".py"
        try:
            result[rel] = sha256(root / rel)
        except OSError as exc:
            raise PreregError("missing source " + rel) from exc
    return result


def tree_sha(sources):
    return hashlib.sha256("".join(p + "\t" + sources[p] + "\n" for p in sorted(sources)).encode()).hexdigest()


def verify(path=None, *, root=None):
    try:
        data = json.loads(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
        current = source_hashes(root)
        from .splits import RANGES, PARAMETER_SETS
        from .scenes import KINDS, KIND_PARAMS
        if (data.get("schema") != SCHEMA or data.get("sources") != current
                or data.get("sources_tree_sha256") != tree_sha(current)
                or data.get("card") != "P1" or data.get("kinds") != list(KINDS)
                or data.get("kind_params") != KIND_PARAMS
                or data.get("alpha") != "1/20" or data.get("unit") != "kind_parameter_stratum"
                or data.get("parameter_sets") != PARAMETER_SETS
                or data.get("frames_per_scene") != 120
                or data.get("resolutions") != [[320, 180], [1920, 1080]]
                or data.get("fps") != [30, 60]
                or data.get("splits") != {k: list(v) for k, v in RANGES.items()}
                or data.get("sealed_after") != ["FA DONE", "FC DONE"]
                or data.get("sealed_by") != "FB"
                or not isinstance(data.get("dev_sha_table_sha256"), str)
                or len(data["dev_sha_table_sha256"]) != 64):
            raise PreregError("seal/source/partition mismatch")
        history = _history(Path(path or DEFAULT_PATH))
        if (history[-1]["new_prereg_sha256"] != sha256(path)
                or history[-1]["sources_tree_sha256"] != data["sources_tree_sha256"]):
            raise PreregError("seal history does not match prereg")
        _check_results(root, {row["new_prereg_sha256"] for row in history})
        return data
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PreregError(str(exc)) from exc


def table_sha(table):
    return hashlib.sha256(json.dumps(table, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dev_sha_table():
    """Stream the full preregistered DEV bank, keeping only hashes on disk."""
    from .scenes import KINDS, build
    from .gen import render
    from .splits import seeds
    cpus = set(range(5, 10))
    os.sched_setaffinity(0, cpus)
    if os.sched_getaffinity(0) != cpus:
        raise PreregError("DEV generation requires CPUs 5-9")
    result = {}
    for kind in KINDS:
        for k in seeds("DEV"):
            scene = build(kind, k, w=320, h=180, fps=None, n=120, split="DEV")
            result[scene["scene_id"]] = [hashlib.sha256(frame.tobytes()).hexdigest() for _, frame in render(scene)]
    return result


def seal(path=None, *, root=None, bus_dir=None, dev_table=None, reseal=False, reason=None):
    """Seal only after both writer DONE pulses; hash the current tree last."""
    from .scenes import KINDS, KIND_PARAMS
    from .splits import RANGES, PARAMETER_SETS
    target = Path(path or DEFAULT_PATH)
    if target.exists() and (not reseal or not isinstance(reason, str) or not reason.strip()):
        raise PreregError("existing seal requires --reseal --reason <text>")
    if reseal and (not target.exists() or not isinstance(reason, str) or not reason.strip()):
        raise PreregError("reseal requires existing seal and nonempty reason")
    history = _history(target)
    if target.exists() and history[-1]["new_prereg_sha256"] != sha256(target):
        raise PreregError("existing seal does not match history")
    # JP1 is an explicitly authorized third link after the judged FXP1 seal.
    # Historical FA/FC global-status pulses no longer govern this new card.
    jp1 = reseal and reason == "JP1 fix: FLICKER strata + split-scoped claims"
    if jp1:
        if (len(history) != 2 or history[-1]["new_prereg_sha256"] !=
                "bd3896cd5cf5379ae7230ceaba30e6f9f945a0fb6980b1a83cb8df29b179e089"):
            raise PreregError("JP1 requires the judged FXP1 predecessor")
    else:
        bus_dir = Path(bus_dir or BUS_DIR)
        for lane in ("FA", "FC"):
            try:
                pulse = (bus_dir / ("bus_" + lane + ".md")).read_text(encoding="utf-8")
            except OSError as exc:
                raise PreregError("missing " + lane + " DONE") from exc
            states = [line for line in pulse.splitlines() if line in ("## " + lane + "DONE", "## " + lane + "BLOCKED")]
            if not states or states[-1] != "## " + lane + "DONE":
                raise PreregError("awaiting " + lane + " DONE")
    sources_before = source_hashes(root)
    if dev_table is None:
        dev_table = dev_sha_table()
    if not isinstance(dev_table, dict) or not dev_table:
        raise PreregError("missing DEV SHA table")
    from .scenes import build
    expected = {build(kind, k, w=320, h=180, fps=None, n=120, split="DEV")["scene_id"]
                for kind in KINDS for k in range(*RANGES["DEV"])}
    if set(dev_table) != expected:
        raise PreregError("DEV SHA table does not cover the full registered bank")
    for values in dev_table.values():
        if not isinstance(values, list) or len(values) != 120:
            raise PreregError("DEV SHA table frame count")
        for value in values:
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise PreregError("DEV SHA table invalid digest")
    sources = source_hashes(root)
    if sources != sources_before:
        raise PreregError("sources changed during DEV hash generation")
    engine_ref = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root or ROOT,
                                check=True, capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root or ROOT,
                            check=True, capture_output=True, text=True).stdout.strip()
    data = {"schema": SCHEMA, "card": "P1", "engine_ref": "ve-sealed-1 + " + branch + " " + engine_ref,
            "sources": sources, "sources_tree_sha256": tree_sha(sources), "kinds": list(KINDS),
            "kind_params": KIND_PARAMS, "parameter_sets": PARAMETER_SETS,
            "splits": {k: list(v) for k, v in RANGES.items()}, "resolutions": [[320, 180], [1920, 1080]],
            "fps": [30, 60], "frames_per_scene": 120, "grid": "visioncore.grid default", "unit": "kind_parameter_stratum", "scene_unit": "scene",
            "alpha": "1/20", "test": "paired stratum-mean sign-flip (kind, parameter value); scene p descriptive; two-sided, exact n<=20 else 100000 draws seed 0",
            "primary": [{"id": "P-MOVED-scroll", "metric": "recall_MOVED", "kinds": ["scroll_small", "scroll_large", "scroll_h", "scroll_smooth"]},
                        {"id": "P-FLICKER-blink", "metric": "recall_FLICKER", "kinds": ["caret_blink"]},
                        {"id": "P-MOVED-caret", "metric": "mv_exact", "kinds": ["caret_move", "typing"]}],
            "guards": [{"id": "G-FP", "metric": "fp_ppm", "kinds": ["static_clean", "aa_jitter"], "rule": "v2 <= v1"},
                       {"id": "G-CHANGED", "metric": "recall_CHANGED", "kinds": ["typing", "canvas_anim"], "rule": "v2 >= v1"},
                       {"id": "G-MS", "metric": "ms_per_frame_1080p", "rule": "v2 <= v1 * 12/10 [TARGET]"}],
            "tuning": "DEV only; GATE for go/no-go; TEST once per v2 candidate (test_access.jsonl)",
            "dev_sha_table_sha256": table_sha(dev_table), "sealed_by": "FB", "sealed_after": ["FA DONE", "FC DONE"]}
    target = Path(path or DEFAULT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()
    row = {"prev_prereg_sha256": history[-1]["new_prereg_sha256"],
           "new_prereg_sha256": hashlib.sha256(payload).hexdigest(),
           "prev_entry_sha256": history[-1]["entry_sha256"],
           "reason": reason or "initial seal", "sources_tree_sha256": tree_sha(sources)}
    row["entry_sha256"] = _entry_sha(row)
    _check_results(root, {r["new_prereg_sha256"] for r in history} | {row["new_prereg_sha256"]})
    if source_hashes(root) != sources:
        raise PreregError("sources changed while sealing")
    # No replacement of history: every successful seal adds exactly one entry.
    with target.with_name(HISTORY_NAME).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    target.write_bytes(payload)
    verify(target, root=root)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seal", "verify"))
    parser.add_argument("--path", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--bus-dir", type=Path)
    parser.add_argument("--dev-sha-table", type=Path)
    parser.add_argument("--reseal", action="store_true")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        if args.action == "verify":
            data = verify(args.path, root=args.root)
        else:
            table = json.loads(args.dev_sha_table.read_text()) if args.dev_sha_table else None
            data = seal(args.path, root=args.root, bus_dir=args.bus_dir, dev_table=table, reseal=args.reseal, reason=args.reason)
        print(json.dumps({"ok": True, "prereg_sha256": sha256(args.path), "sources_tree_sha256": data["sources_tree_sha256"]}))
        return 0
    except (PreregError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "E_PREREG_SHA", "detail": str(exc)}))
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
