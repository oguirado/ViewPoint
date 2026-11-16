"""Read-only demonstrations of contract gaps; these are not acceptance tests."""

import ast
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
from test_f0u3_scheduler import inputs, observation, request, s


def collect():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        req = request(root / "lease")
        stage, provider, factory, decision = inputs()
        provider["runnable"] = False
        factory["commercial_use"] = True
        policy = {"schema": "af.territory-groups/v1", "countries": ["GB"], "groups": {}}
        # These current objects cannot be passed through the frozen execute API.
        evidence = req["evidence"]
        drift = {
            "provider_changed": s.core.sha256_json(provider) != req["provider_sha256"],
            "configuration_changed": s.core.sha256_json(factory) != evidence["decision"]["configuration_sha256"],
            "policy_changed": s.core.sha256_json(policy) != evidence["decision"]["policy_sha256"],
            "old_snapshot_admission_rc": s.admit(req, observation())["rc"],
        }
        reference = req["memory"]["measurement_ref"]
        reference_probe = {"path": reference["path"], "exists": Path(reference["path"]).exists(),
                           "admission_rc": s.admit(req, observation())["rc"]}
        long_timeout = deepcopy(req)
        long_timeout["context"]["timeout_s"] = sys.maxsize
        s._request(long_timeout)
        receipt_ast = ast.parse(inspect.getsource(s._receipt))
        start_ns_used = any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                            and node.id == "start_ns" for node in ast.walk(receipt_ast))
        fsync_types = []
        fsync = os.fsync
        def spy(fd):
            fsync_types.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return fsync(fd)
        target = root / "published.json"
        with patch.object(os, "fsync", side_effect=spy):
            s.core.atomic_write_json(target, {"rc": 0})
        schema = json.loads((ROOT / "tools/scheduler/schemas/request.schema.json").read_text())
        return {
            "finding_1_current_drift_gap": drift,
            "finding_4_missing_peak_reference": reference_probe,
            "finding_6_frozen_aggregation": {"stale_plus_cleanup_ok": s.core.worst_rc([4, 0]),
                                              "budget_plus_cleanup_ok": s.core.worst_rc([6, 0])},
            "finding_10_existing_extension": {"evidence_in_request": "evidence" in req,
                                                "evidence_in_schema": "evidence" in schema["properties"],
                                                "loaded_core": s.core.__name__},
            "finding_11_policy_choices": {"start_ns_used": start_ns_used,
                                            "large_positive_timeout_shape_accepted": True,
                                            "publication_mode": oct(stat.S_IMODE(target.stat().st_mode)),
                                            "fsync_targets": fsync_types},
        }


if __name__ == "__main__":
    print(json.dumps(collect(), sort_keys=True, separators=(",", ":")))
