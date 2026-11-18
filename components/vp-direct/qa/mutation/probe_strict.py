"""Unittest result and line-activation instrumentation for the mutation runner."""

import ast
from collections import defaultdict
import dis
from functools import cache
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading
from types import CodeType
import unittest


def scope_receipt():
    group = Path("/proc/self/cgroup").read_text().strip().removeprefix("0::")
    control = Path("/sys/fs/cgroup" + group)
    actual = {
        "cpus": sorted(os.sched_getaffinity(0)),
        "memory_high_bytes": int((control / "memory.high").read_text()),
        "memory_max_bytes": int((control / "memory.max").read_text()),
    }
    expected = {"cpus": [16, 17, 18, 19], "memory_high_bytes": 6 << 30, "memory_max_bytes": 8 << 30}
    if actual != expected:
        raise RuntimeError(f"unexpected suite scope: {actual}")
    return actual


def code_objects(source, filename):
    """Use code loaded from the pinned source, including nested code objects."""
    root = compile(source, filename, "exec", dont_inherit=True)
    pending = [root]
    objects = set()
    while pending:
        code = pending.pop()
        objects.add(code)
        pending.extend(value for value in code.co_consts if isinstance(value, CodeType))
    return objects


def instruction_offsets(code, mutant):
    """For comparisons and literals, require the instruction at the edit span."""
    family = mutant["family"]
    line, col = mutant["line"], mutant["col"]
    end = col + len(mutant["after"].encode())
    literal = None if family == "compare" else ast.literal_eval(mutant["after"])
    offsets = []
    for instruction in dis.get_instructions(code):
        span = instruction.positions
        if span.lineno is None or span.end_lineno is None or not span.lineno <= line <= span.end_lineno:
            continue
        if span.lineno == line and (span.col_offset is None or span.col_offset > col):
            continue
        if span.end_lineno == line and (span.end_col_offset is None or span.end_col_offset < end):
            continue
        if family == "compare":
            match = instruction.opname in {
                "COMPARE_OP", "IS_OP", "CONTAINS_OP", "POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE",
            }
        else:
            match = (instruction.opname in {"LOAD_CONST", "RETURN_CONST"}
                     and type(instruction.argval) is type(literal)
                     and instruction.argval == literal)
        if match:
            offsets.append(instruction.offset)
    return frozenset(offsets)


def run(config, arguments):
    limits = scope_receipt()
    root = Path(config["root"])
    targets = {str(root / path): set(lines) for path, lines in config["watch"].items()}
    sources = {name: Path(name).read_bytes() for name in targets}
    allowed = {name: code_objects(data, name) for name, data in sources.items()}
    evidence = {}
    mutant = config["mutant"]
    by_instruction = mutant is not None and mutant["family"] in {"compare", "off_by_one", "return_code"}
    if mutant:
        digest = sha256(sources[str(root / mutant["file"])]).hexdigest()
        if digest != config["sha256"]:
            raise RuntimeError("mutant source digest mismatch")
        evidence["mutant_sha256"] = digest
        evidence["activation_kind"] = "instruction" if by_instruction else "source_line"
    coverage = defaultdict(set)
    hit_cases = set()
    current_case = "<discovery>"

    @cache
    def offsets(code):
        return instruction_offsets(code, mutant)

    def observe(frame, event, _arg):
        filename = frame.f_code.co_filename
        if event == "line" and frame.f_lineno in targets[filename]:
            coverage[filename].add(frame.f_lineno)
            if mutant:
                hit_cases.add(current_case)
        elif event == "opcode" and frame.f_lasti in offsets(frame.f_code):
            hit_cases.add(current_case)
        return observe

    def trace(frame, _event, _arg):
        code = frame.f_code
        if code not in allowed.get(code.co_filename, ()):
            return None
        if by_instruction:
            if not offsets(code):
                return None
            frame.f_trace_lines = False
            frame.f_trace_opcodes = True
        return observe

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            nonlocal current_case
            current_case = test.id()
            super().startTest(test)

        def stopTest(self, test):
            nonlocal current_case
            super().stopTest(test)
            current_case = "<between tests>"

    # Python 3.12 enables instruction events if a frame requests them
    # when tracing is installed. Child threads then narrow by code object.
    probe_frame = sys._getframe()
    probe_frame.f_trace_opcodes = by_instruction
    sys.settrace(trace)
    threading.settrace(trace)
    try:
        runner = unittest.TextTestRunner(verbosity=2, resultclass=Result)
        result = unittest.main(module=None, argv=["unittest", *arguments], testRunner=runner,
                               testLoader=unittest.TestLoader(), exit=False).result
    finally:
        sys.settrace(None)
        threading.settrace(None)
        probe_frame.f_trace_opcodes = False
    if any(case.id().startswith("unittest.loader._FailedTest") for case, _ in result.errors):
        evidence["error"] = "unittest could not import or discover a test module"
    evidence.update(
        tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        skipped=len(result.skipped),
        fixed_outcomes=len(result.expectedFailures) + len(result.unexpectedSuccesses),
        failed_cases=[case.id() for case, _ in result.failures + result.errors],
        hit_cases=sorted(hit_cases), limits=limits,
    )
    if not config["mutant"]:
        evidence["coverage"] = {str(Path(path).relative_to(root)): sorted(lines)
                                for path, lines in coverage.items()}
    return evidence, int(not result.wasSuccessful())


if __name__ == "__main__":
    config_path, *arguments = sys.argv[1:]
    receipt, rc = run(json.loads(Path(config_path).read_text()), arguments)
    print("MUT_RESULT " + json.dumps(receipt, sort_keys=True), flush=True)
    raise SystemExit(rc)
