# @module {"id":"wa.tests.test_jq3_r_r4_j07_cdp_pipe_reader_writer_races","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.cdp_pipe"],"book":"HB-WA-s1-perceive"}
"""@module wa.tests.test_jq3_r_r4_j07_cdp_pipe_reader_writer_races - CDP reader and writer race tests and AST prove harness.
@anchor wa.tests.test_jq3_r_r4_j07_cdp_pipe_reader_writer_races.TestCdpPipeReaderWriterRaces
"""
import ast
import fcntl
import importlib
import json
import os
import random
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from wender.perceive.cdp_pipe import CdpError, CdpPipe, Deframer, EVENT_BUFFER, frame


class TestCdpPipeReaderWriterRaces(unittest.TestCase):
    # --- 1. Deframer and NUL boundary framing (seeded fuzzer) ---
    def test_deframer_nul_boundaries_and_chunking(self):
        seed = 1337
        rng = random.Random(seed)
        valid_cases = 0

        for case_idx in range(64):
            msg_count = rng.randint(1, 5)
            msgs = [{"id": case_idx * 100 + j, "result": {"v": rng.randint(0, 10000)}} for j in range(msg_count)]
            raw_stream = b"".join(frame(m) for m in msgs)

            chunks = []
            curr = 0
            while curr < len(raw_stream):
                sz = rng.randint(1, max(1, len(raw_stream) - curr))
                chunks.append(raw_stream[curr:curr + sz])
                curr += sz

            deframer = Deframer()
            parsed = []
            for chunk in chunks:
                parsed.extend(deframer.feed(chunk))

            self.assertEqual(parsed, msgs, f"Seed: {seed}, Case: {case_idx}, Chunks: {chunks}")
            valid_cases += 1

        self.assertEqual(valid_cases, 64)

    # --- 2. Pipe operations patching and reader EOF ---
    def test_reader_eof_and_teardown_resolves_all_waiters(self):
        cmd_r, cmd_w = os.pipe()
        rep_r, rep_w = os.pipe()

        pipe = CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=2.0)
        pipe._w = cmd_w
        pipe._r = rep_r
        pipe.proc = MagicMock()

        reader = threading.Thread(target=pipe._read_loop, name="test-cdp-reader", daemon=True)
        reader.start()
        pipe._reader = reader

        err_send = []
        err_wait = []

        def worker_send():
            try:
                pipe.send("Target.getTargets", timeout=2.0)
            except CdpError as e:
                err_send.append(e)

        def worker_wait():
            try:
                pipe.wait_event("Page.loadEventFired", timeout=2.0)
            except CdpError as e:
                err_wait.append(e)

        t_send = threading.Thread(target=worker_send)
        t_wait = threading.Thread(target=worker_wait)
        t_send.start()
        t_wait.start()

        time.sleep(0.05)
        os.close(rep_w)
        os.close(cmd_r)

        t_send.join(timeout=2.0)
        t_wait.join(timeout=2.0)
        reader.join(timeout=2.0)

        self.assertFalse(t_send.is_alive())
        self.assertFalse(t_wait.is_alive())
        self.assertFalse(reader.is_alive())

        self.assertTrue(pipe._closed)
        self.assertEqual(len(err_send), 1)
        self.assertEqual(len(err_wait), 1)
        self.assertIn("browser closed transport during Target.getTargets", str(err_send[0]))
        self.assertIn("browser closed transport waiting for Page.loadEventFired", str(err_wait[0]))

        pipe.close()

    # --- 3. Multiplexing, Out-of-order replies and Reply Retention Bounds ---
    def test_multiplexing_and_out_of_order_replies(self):
        cmd_r, cmd_w = os.pipe()
        rep_r, rep_w = os.pipe()

        pipe = CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=2.0)
        pipe._w = cmd_w
        pipe._r = rep_r
        pipe.proc = MagicMock()

        reader = threading.Thread(target=pipe._read_loop, name="test-cdp-reader", daemon=True)
        reader.start()

        results = {}

        def send_cmd(mid_expected, method):
            res = pipe.send(method, timeout=2.0)
            results[mid_expected] = res

        threads = []
        for i in range(1, 4):
            t = threading.Thread(target=send_cmd, args=(i, f"Method{i}"))
            threads.append(t)
            t.start()

        time.sleep(0.05)

        os.write(rep_w, frame({"id": 3, "result": {"seq": 3}}))
        os.write(rep_w, frame({"id": 1, "result": {"seq": 1}}))
        os.write(rep_w, frame({"id": 2, "result": {"seq": 2}}))

        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(results[1], {"seq": 1})
        self.assertEqual(results[2], {"seq": 2})
        self.assertEqual(results[3], {"seq": 3})

        os.close(cmd_r)
        os.close(rep_w)
        reader.join(timeout=2.0)
        pipe.close()

    def test_unsolicited_and_late_replies_bounded_without_restoring_pending(self):
        pipe = CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=2.0)
        with pipe._cv:
            for i in range(1100):
                pipe._store_reply_locked({"id": i, "result": {"idx": i}})

            self.assertLessEqual(len(pipe._replies), EVENT_BUFFER)
            self.assertEqual(len(pipe._replies), EVENT_BUFFER)
            self.assertEqual(len(pipe._pending_set()), 0)

            pipe._pending_set().add(9999)
            pipe._store_reply_locked({"id": 9999, "result": {"active": True}})
            self.assertIn(9999, pipe._replies)

            for i in range(2000, 3100):
                pipe._store_reply_locked({"id": i, "result": {"idx": i}})

            self.assertIn(9999, pipe._replies)
            self.assertLessEqual(len(pipe._replies), EVENT_BUFFER)

    def test_timeout_cancellation_and_wait_event(self):
        cmd_r, cmd_w = os.pipe()
        rep_r, rep_w = os.pipe()

        pipe = CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=0.05)
        pipe._w = cmd_w
        pipe._r = rep_r

        start_time = time.monotonic()
        with self.assertRaises(CdpError) as cm:
            pipe.send("Target.getTargets", timeout=0.05)
        elapsed = time.monotonic() - start_time

        self.assertIn("timeout waiting for Target.getTargets", str(cm.exception))
        self.assertGreaterEqual(elapsed, 0.04)

        start_time = time.monotonic()
        with self.assertRaises(CdpError) as cm_ev:
            pipe.wait_event("Page.loadEventFired", timeout=0.05)
        elapsed_ev = time.monotonic() - start_time

        self.assertIn("timeout waiting for event Page.loadEventFired", str(cm_ev.exception))
        self.assertGreaterEqual(elapsed_ev, 0.04)

        os.close(cmd_r)
        os.close(cmd_w)
        os.close(rep_r)
        os.close(rep_w)

    # --- 4. Threaded Schedule Concurrency Fuzzer ---
    def test_threaded_multi_producer_schedules(self):
        for schedule_idx in range(32):
            cmd_r, cmd_w = os.pipe()
            rep_r, rep_w = os.pipe()

            pipe = CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=2.0)
            pipe._w = cmd_w
            pipe._r = rep_r
            pipe.proc = MagicMock()

            reader = threading.Thread(target=pipe._read_loop, name=f"cdp-reader-{schedule_idx}", daemon=True)
            reader.start()

            barrier = threading.Barrier(4)
            errors = []
            events_received = []

            def producer(method_name):
                barrier.wait()
                try:
                    res = pipe.send(method_name, timeout=2.0)
                    if res.get("method") != method_name:
                        errors.append(f"Mismatch in {method_name}: {res}")
                except Exception as e:
                    errors.append(f"Producer {method_name} raised: {e}")

            def event_listener():
                barrier.wait()
                try:
                    ev = pipe.wait_event("Custom.event", timeout=2.0)
                    events_received.append(ev)
                except Exception as e:
                    errors.append(f"Listener raised: {e}")

            threads = [
                threading.Thread(target=producer, args=("MethodA",)),
                threading.Thread(target=producer, args=("MethodB",)),
                threading.Thread(target=producer, args=("MethodC",)),
                threading.Thread(target=event_listener),
            ]

            for t in threads:
                t.start()

            time.sleep(0.02)

            deframer = Deframer()
            received_cmds = []
            while len(received_cmds) < 3:
                chunk = os.read(cmd_r, 65536)
                received_cmds.extend(deframer.feed(chunk))

            for cmd in received_cmds:
                mid = cmd["id"]
                method = cmd["method"]
                os.write(rep_w, frame({"id": mid, "result": {"method": method}}))

            os.write(rep_w, frame({"method": "Custom.event", "params": {"ok": True}}))

            for t in threads:
                t.join(timeout=2.0)
                self.assertFalse(t.is_alive(), f"Thread leaked in schedule {schedule_idx}")

            os.close(cmd_r)
            os.close(cmd_w)
            os.close(rep_r)
            os.close(rep_w)
            reader.join(timeout=2.0)
            self.assertFalse(reader.is_alive())

            self.assertEqual(errors, [])
            self.assertEqual(len(events_received), 1)
            pipe.close()

    # --- 5. Start / FD Patching Verification (Positive Control) ---
    @patch("subprocess.Popen")
    @patch("tempfile.mkdtemp")
    @patch("os.pipe")
    def test_start_child_fds_positive_control(self, mock_pipe, mock_mkdtemp, mock_popen):
        mock_mkdtemp.return_value = "/tmp/wa_test_dir"
        mock_pipe.side_effect = [(10, 11), (12, 13)]
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pipe = CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=1.0)

        with patch("os.close"), patch("threading.Thread") as mock_thread:
            mock_reader = MagicMock()
            mock_thread.return_value = mock_reader
            pipe.start()

            kwargs = mock_popen.call_args.kwargs
            preexec_fn = kwargs.get("preexec_fn")
            self.assertIsNotNone(preexec_fn)

            with patch("fcntl.fcntl") as mock_fcntl, patch("os.dup2") as mock_dup2:
                preexec_fn()
                self.assertEqual(mock_fcntl.call_count, 2)
                args1 = mock_fcntl.call_args_list[0].args
                args2 = mock_fcntl.call_args_list[1].args
                self.assertEqual(args1[2], 10)
                self.assertEqual(args2[2], 10)

        pipe._w = None
        pipe._r = None
        pipe.close()


def _run_wait_event_harness(mod):
    pipe = mod.CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=0.01, clock=lambda: 100_000_000_000)
    waited_left = []

    def mock_wait(timeout=None):
        waited_left.append(timeout)
        raise mod.CdpError("stop loop")

    pipe._cv.wait = mock_wait
    try:
        pipe.wait_event("TestEvent")
    except mod.CdpError as e:
        if str(e) != "stop loop":
            raise

    if waited_left and waited_left[0] > 0.05:
        raise AssertionError(f"wait timeout left is too large: {waited_left[0]}")


def _run_child_fds_harness(mod):
    with patch("subprocess.Popen") as mock_popen, patch("tempfile.mkdtemp") as mock_mkdtemp, patch("os.pipe") as mock_pipe:
        mock_mkdtemp.return_value = "/tmp/wa_test_dir_m2"
        mock_pipe.side_effect = [(10, 11), (12, 13)]
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pipe = mod.CdpPipe(chrome_bin="/usr/bin/google-chrome", timeout=1.0)
        with patch("os.close"), patch("threading.Thread"):
            pipe.start()

        preexec_fn = mock_popen.call_args.kwargs.get("preexec_fn")
        with patch("fcntl.fcntl") as mock_fcntl, patch("os.dup2"):
            preexec_fn()
            arg2_val = mock_fcntl.call_args_list[0].args[2]
            if arg2_val != 10:
                raise AssertionError(f"M2 mutated code passed FD {arg2_val} instead of expected FD 10")


class Mutator1(ast.NodeTransformer):
    def __init__(self):
        super().__init__()
        self.found = False

    def visit_FunctionDef(self, node):
        if node.name == "wait_event":
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.BinOp)
                    and isinstance(child.op, ast.Sub)
                    and isinstance(child.left, ast.Name)
                    and child.left.id == "deadline_ns"
                ):
                    child.op = ast.Add()  # O: Sub -> Add
                    self.found = True
        self.generic_visit(node)
        return node


class Mutator2(ast.NodeTransformer):
    def __init__(self):
        super().__init__()
        self.found = False

    def visit_FunctionDef(self, node):
        if node.name == "_child_fds":
            stmt0 = node.body[0]  # a = fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10)
            arg2 = stmt0.value.args[2]
            if isinstance(arg2, ast.Constant) and arg2.value == 10:
                arg2.value = 11  # B: 10 -> 11
                self.found = True
        self.generic_visit(node)
        return node


# --- 6. AST Prove Harness and Catalogue Mutations ---
class TestASTProveHarness(unittest.TestCase):
    """In-memory AST mutation harness and proof runner for catalogue obligations."""

    def setUp(self):
        self.original_sys_modules = dict(sys.modules)

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self.original_sys_modules)

    def _get_cdp_pipe_ast(self):
        import wender.perceive.cdp_pipe as orig_mod
        src_path = orig_mod.__file__
        with open(src_path, "r", encoding="utf-8") as f:
            src = f.read()
        return ast.parse(src), src

    def _compile_and_import(self, tree, name):
        ast.fix_missing_locations(tree)
        code = compile(tree, f"<{name}>", "exec")
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader=None))
        sys.modules[name] = mod
        exec(code, mod.__dict__)
        return mod

    def test_m_wender_perceive_cdp_pipe_1_kill(self):
        """M-wender-perceive-cdp-pipe-1 O at wender/perceive/cdp_pipe.py:631, CdpPipe.wait_event, node 'deadline_ns - self._now_ns()'"""
        tree, src = self._get_cdp_pipe_ast()

        import wender.perceive.cdp_pipe as real_mod
        _run_wait_event_harness(real_mod)

        m1 = Mutator1()
        mutated_tree = m1.visit(tree)
        self.assertTrue(m1.found, "Anchor for M-wender-perceive-cdp-pipe-1 not found in AST")

        mutant_mod = self._compile_and_import(mutated_tree, "wender.perceive.cdp_pipe_m1")

        with self.assertRaises(AssertionError):
            _run_wait_event_harness(mutant_mod)

    def test_m_wender_perceive_cdp_pipe_2_kill(self):
        """M-wender-perceive-cdp-pipe-2 B at wender/perceive/cdp_pipe.py:173, CdpPipe.start._child_fds, node '10', body element 0.value.args element 2."""
        tree, src = self._get_cdp_pipe_ast()

        import wender.perceive.cdp_pipe as real_mod
        _run_child_fds_harness(real_mod)

        m2 = Mutator2()
        mutated_tree = m2.visit(tree)
        self.assertTrue(m2.found, "Anchor for M-wender-perceive-cdp-pipe-2 not found in AST")

        mutant_mod = self._compile_and_import(mutated_tree, "wender.perceive.cdp_pipe_m2")

        with self.assertRaises(AssertionError):
            _run_child_fds_harness(mutant_mod)


if __name__ == "__main__":
    unittest.main()
