"""Unittest result and instruction-activation instrumentation for the mutation runner."""

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


def scope_receipt(cpus):
    group = Path("/proc/self/cgroup").read_text().strip().removeprefix("0::")
    control = Path("/sys/fs/cgroup" + group)
    actual = {
        "cpus": sorted(os.sched_getaffinity(0)),
        "memory_high_bytes": int((control / "memory.high").read_text()),
        "memory_max_bytes": int((control / "memory.max").read_text()),
    }
    expected = {"cpus": cpus, "memory_high_bytes": 6 << 30, "memory_max_bytes": 8 << 30}
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


def expression_span(node):
    return node.lineno, node.end_lineno, node.col_offset, node.end_col_offset


def activation_spans(source, mutant):
    """Resolve an operator token to the expression it belongs to in the mutant."""
    family = mutant["family"]
    line, col = mutant["line"], mutant["col"]
    end = col + len(mutant["after"].encode())
    if family not in {"compare", "boolean"}:
        return {(line, line, col, end)}
    for node in ast.walk(ast.parse(source)):
        if family == "compare" and isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = node.left, node.comparators[0]
        elif family == "boolean" and isinstance(node, ast.BoolOp) and len(node.values) == 2:
            left, right = node.values
        else:
            continue
        if left.end_lineno == right.lineno == line and left.end_col_offset <= col < end <= right.col_offset:
            spans = {expression_span(node)}
            if family == "boolean":
                # Value expressions assign the jump the BoolOp's span. A condition
                # assigns it the left operand's span, stripping any leading `not`.
                while isinstance(left, ast.UnaryOp) and isinstance(left.op, ast.Not):
                    left = left.operand
                spans.add(expression_span(left))
            return spans
    return set()


def instruction_offsets(code, mutant, spans):
    family = mutant["family"]
    if family == "compare":
        opnames = {"COMPARE_OP", "IS_OP", "CONTAINS_OP", "POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE"}
    elif family == "boolean":
        opnames = {"POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE", "POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE"}
    elif family == "remove_guard":
        # CPython folds `if False`. The position can survive on the NOP,
        # an implicit return, or the unconditional jump out of the guard.
        opnames = {"NOP", "RETURN_CONST", "JUMP_FORWARD", "JUMP_BACKWARD", "JUMP_BACKWARD_NO_INTERRUPT"}
    else:
        opnames = {"LOAD_CONST", "RETURN_CONST"}
        literal = ast.literal_eval(mutant["after"])
    return frozenset(
        ins.offset for ins in dis.get_instructions(code)
        if ins.opname in opnames and tuple(ins.positions) in spans
        and (family not in {"off_by_one", "return_code"}
             or (type(ins.argval) is type(literal) and ins.argval == literal))
    )


def run(config, arguments):
    limits = scope_receipt(config["cpus"])
    root = Path(config["root"])
    targets = {str(root / path): set(lines) for path, lines in config["watch"].items()}
    sources = {name: Path(name).read_bytes() for name in targets}
    allowed = {name: code_objects(data, name) for name, data in sources.items()}
    evidence = {}
    mutant = config["mutant"]
    if mutant:
        mutant_file = str(root / mutant["file"])
        source = sources[mutant_file]
        digest = sha256(source).hexdigest()
        if digest != config["sha256"]:
            raise RuntimeError("mutant source digest mismatch")
        evidence["mutant_sha256"] = digest
        evidence["activation_kind"] = "instruction"
        spans = activation_spans(source, mutant)
    coverage = defaultdict(set)
    hit_cases = {}
    current_test = None

    @cache
    def offsets(code):
        return instruction_offsets(code, mutant, spans)

    def observe(frame, event, _arg):
        filename = frame.f_code.co_filename
        if event == "line" and frame.f_lineno in targets[filename]:
            coverage[filename].add(frame.f_lineno)
        elif event == "opcode" and frame.f_lasti in offsets(frame.f_code):
            test = current_test
            if test is not None:
                case = test._subtest or test
                # Retain the object to prevent ID reuse. Different subtests
                # may have identical unittest labels and compare equal.
                hit_cases[id(case)] = case
        return observe

    def trace(frame, _event, _arg):
        code = frame.f_code
        if code not in allowed.get(code.co_filename, ()):
            return None
        if mutant:
            if code.co_filename != mutant_file or not offsets(code):
                return None
            frame.f_trace_lines = False
            frame.f_trace_opcodes = True
        return observe

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            nonlocal current_test
            current_test = test
            super().startTest(test)

        def stopTest(self, test):
            nonlocal current_test
            super().stopTest(test)
            current_test = None

    # Python 3.12 enables instruction events if a frame requests them
    # when tracing is installed. Child threads then narrow by code object.
    probe_frame = sys._getframe()
    probe_frame.f_trace_opcodes = mutant is not None
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
    failed = [case for case, _ in result.failures + result.errors]
    evidence.update(
        tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
        skipped=len(result.skipped),
        fixed_outcomes=len(result.expectedFailures) + len(result.unexpectedSuccesses),
        failed_cases=[case.id() for case in failed],
        hit_cases=sorted({case.id() for case in hit_cases.values()}),
        activated_failed_cases=[case.id() for case in failed if id(case) in hit_cases], limits=limits,
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
