#!/usr/bin/env python3
"""
LANG-X X2 host comparison harness.
Compares X2 seed outputs for determinism, conformance, and timing medians.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile


def calculate_median(values):
    """Calculates median of a list of numbers (handles even and odd lengths)."""
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 1:
        return sorted_vals[n // 2]
    else:
        mid1 = sorted_vals[n // 2 - 1]
        mid2 = sorted_vals[n // 2]
        res = (mid1 + mid2) / 2.0
        if isinstance(mid1, int) and isinstance(mid2, int) and (mid1 + mid2) % 2 == 0:
            return int(res)
        return res


def first_differing_byte_offset(bytes1: bytes, bytes2: bytes) -> int:
    """Returns the index of the first differing byte between two bytes objects."""
    min_len = min(len(bytes1), len(bytes2))
    for i in range(min_len):
        if bytes1[i] != bytes2[i]:
            return i
    return min_len


def find_json_diff_path(obj1, obj2, path="$"):
    """
    Recursively finds the JSON path of the first differing value between two objects.
    Returns path string in JSONPath format (e.g. '$.key[0]').
    """
    if type(obj1) != type(obj2):
        return path

    if isinstance(obj1, dict):
        all_keys = sorted(set(obj1.keys()) | set(obj2.keys()))
        for k in all_keys:
            key_path = f"{path}.{k}"
            if k not in obj1 or k not in obj2:
                return key_path
            if obj1[k] != obj2[k]:
                diff = find_json_diff_path(obj1[k], obj2[k], key_path)
                if diff:
                    return diff
        return None

    if isinstance(obj1, list):
        min_len = min(len(obj1), len(obj2))
        for i in range(min_len):
            idx_path = f"{path}[{i}]"
            if obj1[i] != obj2[i]:
                diff = find_json_diff_path(obj1[i], obj2[i], idx_path)
                if diff:
                    return diff
        if len(obj1) != len(obj2):
            return f"{path}[{min_len}]"
        return None

    if obj1 != obj2:
        return path

    return None


def discover_pages(fixtures_dir: str):
    """Discovers page names from the fixtures directory."""
    if not fixtures_dir or not os.path.exists(fixtures_dir):
        return ["default"]

    manifest_path = os.path.join(fixtures_dir, "manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "pages" in data:
                    pages = data["pages"]
                    if isinstance(pages, list) and pages:
                        return pages
                    elif isinstance(pages, dict):
                        return sorted(list(pages.keys()))
        except Exception:
            pass

    items = sorted(os.listdir(fixtures_dir))
    pages = []
    for item in items:
        if item.endswith(".json") and item != "manifest.json":
            pages.append(item[:-5])
        elif item.endswith(".html"):
            pages.append(item[:-5])
        elif os.path.isdir(os.path.join(fixtures_dir, item)):
            pages.append(item)

    if pages:
        return pages
    return ["default"]


def run_comparison(
    seeds=None,
    fixtures_dir=None,
    reps=5,
    out_path=None,
    command_map=None,
    env=None,
    stdout_stream=None
):
    if env is None:
        env = dict(os.environ)
    if stdout_stream is None:
        stdout_stream = sys.stdout

    # Check CHROME_BIN
    if not env.get("CHROME_BIN"):
        stdout_stream.write("SKIP: CHROME_BIN environment variable is unset\n")
        stdout_stream.flush()
        return 0

    if seeds is None:
        seeds = ["py_w1", "py_w2", "py_w3", "ts_w1", "ts_w2", "ts_w3"]

    temp_fixtures_dir = None
    if not fixtures_dir:
        temp_fixtures_dir = tempfile.mkdtemp(prefix="x2_fixtures_")
        gen_script = os.path.join("langx", "x2", "fixtures", "gen_fixtures.py")
        if os.path.exists(gen_script):
            subprocess.run(
                [sys.executable, gen_script, "--out", temp_fixtures_dir],
                check=False,
                capture_output=True
            )
        fixtures_dir = temp_fixtures_dir

    try:
        pages = discover_pages(fixtures_dir)

        seed_status = {}
        for s in seeds:
            if command_map and s in command_map:
                seed_status[s] = "PRESENT"
            else:
                seed_dir = os.path.join("langx", "x2", s)
                if os.path.isdir(seed_dir):
                    seed_status[s] = "PRESENT"
                else:
                    seed_status[s] = "ABSENT"

        # Execution results storage
        # {page: {seed: [{"exit_code": int, "stdout_bytes": bytes, "sha256": str, "timing": dict}]}}
        raw_results = {p: {s: [] for s in seeds if seed_status[s] == "PRESENT"} for p in pages}

        for page in pages:
            page_steps_file = os.path.join(fixtures_dir, f"{page}.json")
            if not os.path.isfile(page_steps_file):
                page_steps_file = os.path.join(fixtures_dir, page, "steps.json")
            if not os.path.isfile(page_steps_file):
                page_steps_file = os.path.join(fixtures_dir, "steps.json")

            for s in seeds:
                if seed_status[s] == "ABSENT":
                    continue

                if command_map and s in command_map:
                    cmd_base = list(command_map[s])
                else:
                    if s.startswith("py_"):
                        cmd_base = [sys.executable, os.path.join("langx", "x2", s, "x2.py")]
                    elif s.startswith("ts_"):
                        cmd_base = ["node", os.path.join("langx", "x2", s, "x2.ts")]
                    else:
                        cmd_base = [sys.executable, os.path.join("langx", "x2", s, "x2.py")]

                proc_env = dict(env)
                proc_env["X2_WHERE"] = "host"

                for rep in range(reps):
                    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                        timing_file_path = tf.name

                    try:
                        cmd = list(cmd_base) + [
                            "--page", str(page),
                            "--steps", page_steps_file,
                            "--timing", timing_file_path,
                            "--timeout-s", "30"
                        ]

                        proc = subprocess.run(
                            cmd,
                            capture_output=True,
                            env=proc_env,
                            timeout=35
                        )

                        stdout_bytes = proc.stdout
                        sha256_hex = hashlib.sha256(stdout_bytes).hexdigest()
                        exit_code = proc.returncode

                        timing_data = {}
                        if os.path.isfile(timing_file_path):
                            try:
                                with open(timing_file_path, "r", encoding="utf-8") as tf_in:
                                    timing_data = json.load(tf_in)
                            except Exception:
                                timing_data = {}

                        raw_results[page][s].append({
                            "exit_code": exit_code,
                            "stdout_bytes": stdout_bytes,
                            "sha256": sha256_hex,
                            "timing": timing_data
                        })
                    finally:
                        if os.path.exists(timing_file_path):
                            try:
                                os.remove(timing_file_path)
                            except OSError:
                                pass

        # Process statistics
        report = {
            "seeds": seed_status,
            "pages": {},
            "timing": {}
        }

        # Timing aggregation per seed
        # launch_ms values and steps_ms values across all pages and reps
        seed_launch_ms = {s: [] for s in seeds if seed_status[s] == "PRESENT"}
        seed_steps_ms = {s: [] for s in seeds if seed_status[s] == "PRESENT"}

        for page in pages:
            page_report = {
                "determinism": {},
                "conformance": {
                    "majority_sha256": None,
                    "dissenting_seeds": []
                },
                "dissenting_details": {}
            }

            seed_sha256s = {}
            deterministic_seeds = []
            sha256_counts = {}
            majority_stdout_bytes = None
            majority_sha256 = None

            for s in seeds:
                if seed_status[s] == "ABSENT":
                    continue

                reps_data = raw_results[page][s]
                # Collect timing
                for rd in reps_data:
                    t = rd.get("timing", {})
                    if "launch_ms" in t and isinstance(t["launch_ms"], (int, float)):
                        seed_launch_ms[s].append(t["launch_ms"])
                    if "steps_ms" in t and isinstance(t["steps_ms"], list):
                        for step_ms in t["steps_ms"]:
                            if isinstance(step_ms, (int, float)):
                                seed_steps_ms[s].append(step_ms)

                # Check determinism: all reps have exit code 0 and same stdout sha256
                all_exits_zero = all(rd["exit_code"] == 0 for rd in reps_data)
                sha_list = [rd["sha256"] for rd in reps_data]
                is_deterministic = all_exits_zero and len(set(sha_list)) == 1

                page_report["determinism"][s] = is_deterministic

                if is_deterministic and reps_data:
                    deterministic_seeds.append(s)
                    sha = sha_list[0]
                    seed_sha256s[s] = sha
                    sha256_counts[sha] = sha256_counts.get(sha, 0) + 1

            if sha256_counts:
                majority_sha256 = max(sha256_counts.items(), key=lambda x: (x[1], x[0]))[0]
                page_report["conformance"]["majority_sha256"] = majority_sha256

                # Find majority stdout bytes from any seed that matched majority_sha256
                for s in deterministic_seeds:
                    if seed_sha256s[s] == majority_sha256:
                        majority_stdout_bytes = raw_results[page][s][0]["stdout_bytes"]
                        break

            dissenting_seeds = []
            for s in seeds:
                if seed_status[s] == "ABSENT":
                    continue
                # Dissenting if non-deterministic or stdout sha256 differs from majority
                if not page_report["determinism"][s] or seed_sha256s.get(s) != majority_sha256:
                    dissenting_seeds.append(s)

            page_report["conformance"]["dissenting_seeds"] = sorted(dissenting_seeds)

            # Details for dissenting seeds
            if majority_stdout_bytes is not None:
                maj_json = None
                try:
                    maj_json = json.loads(majority_stdout_bytes.decode("utf-8"))
                except Exception:
                    maj_json = None

                for ds in dissenting_seeds:
                    ds_reps = raw_results[page][ds]
                    if not ds_reps:
                        continue
                    ds_stdout_bytes = ds_reps[0]["stdout_bytes"]
                    byte_off = first_differing_byte_offset(majority_stdout_bytes, ds_stdout_bytes)

                    ds_json = None
                    try:
                        ds_json = json.loads(ds_stdout_bytes.decode("utf-8"))
                    except Exception:
                        ds_json = None

                    json_path = None
                    if maj_json is not None and ds_json is not None:
                        json_path = find_json_diff_path(maj_json, ds_json)

                    page_report["dissenting_details"][ds] = {
                        "first_differing_byte_offset": byte_off,
                        "first_differing_json_path": json_path
                    }

            report["pages"][page] = page_report

        for s in seeds:
            if seed_status[s] == "PRESENT":
                report["timing"][s] = {
                    "launch_ms_median": calculate_median(seed_launch_ms[s]),
                    "steps_ms_median": calculate_median(seed_steps_ms[s])
                }
            else:
                report["timing"][s] = "ABSENT"

        if out_path:
            out_dir = os.path.dirname(os.path.abspath(out_path))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, sort_keys=True)

        # Print Markdown table to stdout
        stdout_stream.write("| Seed | Status | Deterministic | Conforming | Median Launch (ms) | Median Steps (ms) |\n")
        stdout_stream.write("|---|---|---|---|---|---|\n")
        for s in sorted(seeds):
            st = seed_status[s]
            if st == "ABSENT":
                stdout_stream.write(f"| {s} | ABSENT | N/A | N/A | N/A | N/A |\n")
            else:
                det = all(p_rep["determinism"].get(s, False) for p_rep in report["pages"].values())
                det_str = "Yes" if det else "No"
                conf = all(s not in p_rep["conformance"]["dissenting_seeds"] for p_rep in report["pages"].values())
                conf_str = "Yes" if conf else "No"
                t_info = report["timing"][s]
                l_med = f"{t_info['launch_ms_median']:.2f}" if isinstance(t_info.get("launch_ms_median"), (int, float)) else "N/A"
                s_med = f"{t_info['steps_ms_median']:.2f}" if isinstance(t_info.get("steps_ms_median"), (int, float)) else "N/A"
                stdout_stream.write(f"| {s} | PRESENT | {det_str} | {conf_str} | {l_med} | {s_med} |\n")
        stdout_stream.flush()

        return 0

    finally:
        if temp_fixtures_dir and os.path.exists(temp_fixtures_dir):
            shutil.rmtree(temp_fixtures_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="LANG-X X2 host comparison harness")
    parser.add_argument("--seeds", type=str, default="py_w1,py_w2,py_w3,ts_w1,ts_w2,ts_w3", help="Comma separated seeds")
    parser.add_argument("--fixtures", type=str, default=None, help="Fixtures directory")
    parser.add_argument("--reps", type=int, default=5, help="Number of repetitions")
    parser.add_argument("--out", type=str, required=True, help="Output JSON path")

    args = parser.parse_args()
    seed_list = [s.strip() for s in args.seeds.split(",") if s.strip()]

    code = run_comparison(
        seeds=seed_list,
        fixtures_dir=args.fixtures,
        reps=args.reps,
        out_path=args.out
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
