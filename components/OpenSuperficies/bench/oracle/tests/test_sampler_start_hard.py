import json
import os
import stat
import tempfile
import unittest

from bench.oracle import sampler as S

HERE = os.path.dirname(os.path.abspath(__file__))
PROPOSED_MUTANTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
    "scripts", "cu", "mutants_proposed", "os-g-hard-start-dupkeys.json"
)


class StartDuplicateKeysHardening(unittest.TestCase):

    def _write_start(self, run_dir, content_str):
        p = os.path.join(run_dir, "START")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content_str)
        return p

    def test_duplicate_t0_ns_equal_values_refused(self):
        raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000,"t0_ns":1000}'
        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw)
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_duplicate_run_nonce_refused(self):
        nonce = "0123456789abcdef0123456789abcdef"
        raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000,"run_nonce":"%s","run_nonce":"%s"}' % (nonce, nonce)
        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw)
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_duplicate_schema_refused(self):
        sch = "os.cu.wlcap-start/v1"
        raw = '{"schema":"%s","schema":"%s","clock":"CLOCK_MONOTONIC","t0_ns":1000}' % (sch, sch)
        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw)
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_duplicate_nested_key_refused(self):
        raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000,"meta":{"sub":1,"sub":2}}'
        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw)
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_case_differing_keys_accepted(self):
        raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000,"foo":1,"FOO":2}'
        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw)
            d = S.read_start(td)
            self.assertEqual(d["foo"], 1)
            self.assertEqual(d["FOO"], 2)

    def test_t0_ns_bool_refused(self):
        for val in ("true", "false"):
            raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":%s}' % val
            with tempfile.TemporaryDirectory() as td:
                self._write_start(td, raw)
                with self.assertRaises(ValueError):
                    S.read_start(td)

    def test_t0_ns_float_refused(self):
        raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1.0}'
        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw)
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_run_nonce_invalid_format_refused(self):
        bad_nonces = [
            "a" * 31,
            "a" * 33,
            "0123456789ABCDEF0123456789ABCDEF",  # uppercase hex
        ]
        for bad in bad_nonces:
            raw = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000,"run_nonce":"%s"}' % bad
            with tempfile.TemporaryDirectory() as td:
                self._write_start(td, raw)
                with self.assertRaises(ValueError):
                    S.read_start(td)

    def test_start_file_size_boundary(self):
        prefix = '{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000,"pad":"'
        suffix = '"}'
        needed_pad_4096 = 4096 - len(prefix) - len(suffix)
        self.assertGreater(needed_pad_4096, 0)
        raw_4096 = prefix + ("x" * needed_pad_4096) + suffix
        self.assertEqual(len(raw_4096.encode("utf-8")), 4096)

        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw_4096)
            d = S.read_start(td)
            self.assertEqual(d["t0_ns"], 1000)

        raw_4097 = raw_4096 + " "
        self.assertEqual(len(raw_4097.encode("utf-8")), 4097)

        with tempfile.TemporaryDirectory() as td:
            self._write_start(td, raw_4097)
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_start_symlink_refused(self):
        with tempfile.TemporaryDirectory() as td:
            real_file = os.path.join(td, "real_start")
            with open(real_file, "w", encoding="utf-8") as f:
                f.write('{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000}')
            os.symlink(real_file, os.path.join(td, "START"))
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_start_fifo_nonblocking_refused(self):
        with tempfile.TemporaryDirectory() as td:
            fifo_p = os.path.join(td, "START")
            os.mkfifo(fifo_p)
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_start_second_hard_link_refused(self):
        with tempfile.TemporaryDirectory() as td:
            orig = os.path.join(td, "START_ORIG")
            with open(orig, "w", encoding="utf-8") as f:
                f.write('{"schema":"os.cu.wlcap-start/v1","clock":"CLOCK_MONOTONIC","t0_ns":1000}')
            os.link(orig, os.path.join(td, "START"))
            with self.assertRaises(ValueError):
                S.read_start(td)

    def test_t_us_of_floor_division_negative_offsets(self):
        t0 = 7_000_000_000
        self.assertEqual(S.t_us_of(t0 - 1_500_000, t0), -1500)
        self.assertEqual(S.t_us_of(t0 - 1_500, t0), -2)
        self.assertEqual(S.t_us_of(t0 - 1_000, t0), -1)
        self.assertEqual(S.t_us_of(t0 - 999, t0), -1)
        self.assertEqual(S.t_us_of(t0 - 2_000, t0), -2)


class ProposedMutantsValidation(unittest.TestCase):

    def test_proposed_mutants_schema_and_structure(self):
        self.assertTrue(os.path.isfile(PROPOSED_MUTANTS_PATH), "proposed mutants file missing: %s" % PROPOSED_MUTANTS_PATH)
        with open(PROPOSED_MUTANTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("schema"), "os.cu.mutants/v1")
        mutants = data.get("mutants")
        self.assertIsInstance(mutants, list)
        self.assertGreater(len(mutants), 0)

        gate_cmd = "python3 -B -m unittest bench.oracle.tests.test_sampler_start_hard bench.oracle.tests.test_sampler"
        required_keys = {"id", "file", "find", "replace", "precheck", "oracle"}
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

        for m in mutants:
            self.assertEqual(set(m.keys()), required_keys, "Mutant keys mismatch: %s" % m)
            self.assertTrue(m["id"].startswith("prop.start-dupkeys."), "Mutant ID must start with prop.start-dupkeys.: %s" % m["id"])
            self.assertEqual(m["oracle"], gate_cmd, "Oracle must match task gate command: %s" % m["id"])

            file_abs = os.path.join(repo_root, m["file"])
            self.assertTrue(os.path.isfile(file_abs), "Mutant target file missing: %s" % m["file"])
            with open(file_abs, "r", encoding="utf-8") as tf:
                content = tf.read()

            find_str = m["find"]
            occurrences = content.count(find_str)
            self.assertEqual(occurrences, 1, "find string in %s for %s must occur exactly once, found %d" % (m["file"], m["id"], occurrences))


if __name__ == "__main__":
    unittest.main()
