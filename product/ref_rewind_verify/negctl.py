"""R3 negative controls: remove one guard of judge.py at a time and require the suite to go red.

Track P (claude-fable-5-2). The mutated copy lives in a temp dir; judge.py is never modified.

    python3 negctl.py            exit 0 iff the unmutated suite is green and every mutant is red
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# (name, text to find exactly once in judge.py, replacement)
MUTANTS = [
    ("expect: no probe runner", "    if run_probe is None:\n        return", "    if False:\n        return"),
    ("expect: runner exception", "    except Exception as e:  # noqa: BLE001  (a runner", "    except ZeroDivisionError as e:  # (a runner"),
    ("expect: probe job failed", "    if not isinstance(pr, dict) or pr.get(\"ok\") is not True:", "    if False:"),
    ("expect: probe read-only", "    if not RW.digest_match(pr.get(\"digest\"), after_digest):", "    if False:"),
    ("expect: result equals", "    if \"equals\" not in ex or pr.get(\"result\") != ex[\"equals\"]:", "    if False:"),
    ("delta: digest present", "    if not (_is_digest(base_digest) and _is_digest(after_digest)):", "    if False:"),
    ("delta: f1 verdict committable", "    if verdict not in RW.COMMIT_VERDICTS:", "    if False:"),
    ("delta: no effect", "    if not delta:\n", "    if False:\n"),
    ("delta: inside write_set", "    if out:\n", "    if False:\n"),
    ("witness: differ error (forward)", "        touching, spill = split_regions(regions, footprint, slack)\n    except Exception as e:",
     "        touching, spill = split_regions(regions, footprint, slack)\n    except ZeroDivisionError as e:"),
    ("witness: gui spill", "    if strict and spill:", "    if False:"),
    ("witness: script lane not strict", "    strict = (intent.get(\"lane\") == \"gui\") if strict_outside is None else bool(strict_outside)",
     "    strict = True if strict_outside is None else bool(strict_outside)"),
    ("witness: change must show", "    if must_show and not touching:", "    if False:"),
    ("witness: slack at the border", "    spill = [r for r in boxes if not B._inside(r, footprint, slack)]", "    spill = [r for r in boxes if not B._inside(r, footprint, 0)]"),
    ("rollback witness: frames present", "    if not base_png or not restored_png:\n        return RW.oracle(\"witness\", False", "    if False:\n        return RW.oracle(\"witness\", False"),
    ("rollback witness: differ error", "        touching, spill = split_regions(regions, footprint, 0)\n    except Exception as e:",
     "        touching, spill = split_regions(regions, footprint, 0)\n    except ZeroDivisionError as e:"),
    ("rollback witness: not restored", "    if touching:\n", "    if False:\n"),
    ("rollback witness: footprint only", "    touching = [r for r in boxes if _intersects(r, footprint)]", "    touching = list(boxes)"),
    ("cost: tie-break", "    return min(picked, key=lambda ir: (cost_ms(ir[1]) is None, cost_ms(ir[1]) or 0, ir[0]))[1]", "    return picked[0][1]"),
    ("cost: never deciding", "    pick = all(x[\"ok\"] for x in oracles if x[\"name\"] != \"cost\")", "    pick = all(x[\"ok\"] for x in oracles)"),
    ("judge: pick needs every deciding oracle", "    pick = all(x[\"ok\"] for x in oracles if x[\"name\"] != \"cost\")", "    pick = True"),
    ("judge: always accept (dead oracle)", "    return RW.judge_rec(attempt[\"attempt\"], pick, oracles)",
     "    return RW.judge_rec(attempt[\"attempt\"], True, [RW.oracle(\"delta\", True, \"mutant\")])"),
]


def run_suite(judge_path):
    env = dict(os.environ, RW_JUDGE_PATH=judge_path)
    p = subprocess.run([sys.executable, "-m", "unittest", "test_judge"], cwd=HERE, env=env,
                       capture_output=True, text=True, timeout=300)
    tail = (p.stderr.strip().split("\n") or [""])[-1]
    return p.returncode, tail


def main():
    src = open(os.path.join(HERE, "judge.py"), encoding="utf-8").read()
    tmp = tempfile.mkdtemp(prefix="vprw_r3_neg_")
    bad = 0
    try:
        rc, tail = run_suite(os.path.join(HERE, "judge.py"))
        print("%-45s %s  (%s)" % ("unmutated", "GREEN" if rc == 0 else "RED", tail))
        bad += rc != 0
        for i, (name, old, new) in enumerate(MUTANTS):
            if src.count(old) != 1:
                print("%-45s STALE (anchor found %d times)" % (name, src.count(old)))
                bad += 1
                continue
            d = os.path.join(tmp, "m%02d" % i)
            os.makedirs(d)
            path = os.path.join(d, "judge.py")
            with open(path, "w", encoding="utf-8") as f:
                # the copy sits outside product/, so pin the import roots it would derive from its path
                f.write(src.replace(old, new).replace("_HERE = os.path.dirname(os.path.abspath(__file__))", "_HERE = %r" % HERE))
            rc, tail = run_suite(path)
            print("%-45s %s  (%s)" % (name, "RED" if rc != 0 else "GREEN = DEAD CHECK", tail))
            bad += rc == 0
    finally:
        shutil.rmtree(tmp, True)
    print("mutants: %d, not caught: %d" % (len(MUTANTS), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
