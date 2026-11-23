"""python -B -m gates.rank.rank --request F [--receipt F].

    stdout retains refusals and diagnostic measurements. A caller receipt is
    published only at rc 0; all other outcomes leave existing bytes unchanged.
    Run in the resource scope documented in README.md. Hard wall timeout owns
    the child's process group. Nothing launches models, downloads or a GPU.
"""
from asset_factory import proc as af_proc
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from asset_factory import core
from gates.rank.dino import offline_environment
from gates.rank.evaluate import MAX_JSON, _json, validate_request
from gates.rank.metrics import RankError


def _refusal(rc, reason):
    return {"schema": "af.seed-ranking/v1", "scope": "ordinal-ranking-only",
            "rc": rc, "verdict": core.VERDICT[rc], "asset_verdict": "CANNOT_SEE",
            "reason_codes": [reason], "winner_id": None, "preferred_id": None,
            "rankings": [], "model": None, "wall_ms": None, "peak_rss_kib": None}


def run(request):
    try:
        validate_request(request)
        env = {**os.environ, **offline_environment()}
        # Keep explicitly supplied offline dependency directories in the worker.
        # The component root remains first; no package is installed or fetched.
        component_root = str(Path(__file__).resolve().parents[2])
        inherited = os.environ.get("PYTHONPATH")
        env["PYTHONPATH"] = component_root + (os.pathsep + inherited if inherited else "")
        child = af_proc.popen([sys.executable, "-B", "-m", "gates.rank.worker"],
                                 input=core.canonical_json(request).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True, cwd=Path(__file__).resolve().parents[2], env=env)
        try:
            out, err = af_proc.communicate(child,
                                         timeout=request["policy"]["timeout_s"])
        except subprocess.TimeoutExpired:
            try:
                af_proc.signal_group(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            af_proc.communicate(child)
            return _refusal(6, "ranking_deadline_exceeded")
        if len(out) > 4 * MAX_JSON:
            return _refusal(6, "worker_output_budget_exceeded")
        result = _json(out)
        if type(result) is not dict or type(result.get("rc")) is not int or result.get("rc") not in core.RC:
            return _refusal(2, "worker_contract")
        if child.returncode != result["rc"] or result.get("schema") != "af.seed-ranking/v1":
            return _refusal(2, "worker_contract")
        core.validate(result, core.load_json(Path(__file__).with_name("result.schema.json"), max_bytes=MAX_JSON))
        if result.get("request_sha256") != core.sha256_json(request):
            return _refusal(4, "worker_request_stale")
        return result
    except af_proc.SignalRefusal as exc:
        return _refusal(exc.rc, exc.code)
    except RankError as exc:
        return _refusal(exc.rc, exc.reason)
    except core.ContractError as exc:
        return _refusal(2, exc.code)
    except (OSError, ValueError, TypeError, KeyError):
        return _refusal(2, "ranking_input_invalid")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise RankError(2, "usage")


def main(argv=None):
    parser = Parser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    try:
        args = parser.parse_args(argv)
        request = core.load_json(args.request, max_bytes=MAX_JSON)
        result = run(request)
        if result["rc"] == 0 and args.receipt:
            # Never let a publication path replace any authenticated input.
            outputs = args.receipt.resolve()
            inputs = [args.request.resolve()]
            root = Path(request["evidence_root"])
            for binding in request["reference"].values():
                inputs.append((root / binding["path"]).resolve())
            for candidate in request["candidates"]:
                for name in ("render", "mask", "artifact", "generation", "gate", "capture_receipt"):
                    if candidate[name] is not None:
                        inputs.append((root / candidate[name]["path"]).resolve())
            model_root = Path(request["model"]["root"])
            inputs.extend((model_root / name).resolve() for name in ("config.json", "model.safetensors"))
            if outputs in inputs or model_root.resolve() in outputs.parents:
                result = _refusal(2, "output_aliases_input")
            else:
                core.atomic_write_json(args.receipt, result)
    except RankError as exc:
        result = _refusal(exc.rc, exc.reason)
    except core.ContractError as exc:
        result = _refusal(2, exc.code)
    except (OSError, ValueError):
        result = _refusal(2, "ranking_io")
    print(core.canonical_json(result), end="")
    return result["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
