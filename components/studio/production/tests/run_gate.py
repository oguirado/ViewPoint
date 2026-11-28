# SPDX-License-Identifier: AGPL-3.0-only
"""Run the selected gate in a verified CPU-only scope."""
import json
import sys
import unittest

from test_production import NegativeTests, ProductionTests, ROOT

sys.path.insert(0, str(ROOT / "review"))
from rpr_host import containment, checkpoint


def main(mode):
    scope = containment()
    memory = checkpoint()
    if not scope["contained"] or memory["status"] != "ADMIT":
        print(json.dumps({"reason": "effective_scope_or_ram_mismatch", "retry_when": "use the admitted CPU scope",
                          "next_command": ["./check.sh", mode], "scope": scope, "checkpoint": memory}, sort_keys=True))
        return 2
    tests = unittest.defaultTestLoader.loadTestsFromTestCase(ProductionTests if mode == "positive" else NegativeTests)
    outcome = unittest.TextTestRunner(verbosity=2).run(tests)
    good = outcome.wasSuccessful() and outcome.testsRun > 0 and not outcome.skipped
    evidence = {"mode": mode, "passed": outcome.testsRun - len(outcome.failures) - len(outcome.errors) - len(outcome.skipped),
                "executed": outcome.testsRun, "scope": scope, "checkpoint": memory,
                "status": "PASS" if good and mode == "positive" else "EXPECTED_REFUSALS" if good else "HARNESS_ERROR"}
    if mode == "negative" or not good:
        evidence.update(reason="negative_controls_observed" if good else "test_failed",
                        retry_when="after correcting the input or runtime", next_command=["./check.sh", "positive"])
    print(json.dumps(evidence, sort_keys=True))
    return (0 if mode == "positive" else 1) if good else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
