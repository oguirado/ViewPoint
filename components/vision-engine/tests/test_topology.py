"""CPU topology (CDESIGN_CENG F4): cpuinfo parsing, spec resolution, pinning."""
import os
import unittest

from visioncore import topology as T

SAMPLE = "".join("processor\t: %d\nBogoMIPS\t: 1\nCPU part\t: %s\n\n" % (i, "0xd85" if i in (2, 3) else "0xd87")
                 for i in range(5))


class TestTopology(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(T.parse_cpuinfo(SAMPLE), {0: 0xd87, 1: 0xd87, 2: 0xd85, 3: 0xd85, 4: 0xd87})
        self.assertEqual(T.big_cpus(SAMPLE), [2, 3])
        self.assertEqual(T.little_cpus(SAMPLE), [0, 1, 4])

    def test_interleaved_not_assumed(self):
        txt = "".join("processor : %d\nCPU part : %s\n" % (i, "0xd87" if i < 5 or 10 <= i < 15 else "0xd85")
                      for i in range(20))
        self.assertEqual(T.big_cpus(txt), list(range(5, 10)) + list(range(15, 20)))

    def test_no_big_and_garbage(self):
        self.assertEqual(T.big_cpus("processor : 0\nmodel name : x86\n"), [])
        self.assertEqual(T.big_cpus(""), [])
        self.assertEqual(T.resolve_cpus("big", "garbage"), [])

    def test_cpu_list(self):
        self.assertEqual(T.parse_cpu_list("5-9,15-17,6"), [5, 6, 7, 8, 9, 15, 16, 17])
        for bad in ("", "a", "3-1", "1,,2", "-2"):
            with self.assertRaises(ValueError):
                T.parse_cpu_list(bad)

    def test_resolve_intersects_allowed(self):
        allowed = sorted(os.sched_getaffinity(0))
        self.assertEqual(T.resolve_cpus(str(allowed[0])), [allowed[0]])
        self.assertEqual(T.resolve_cpus("9999"), [])

    def test_pin_roundtrip(self):
        before = os.sched_getaffinity(0)
        try:
            self.assertFalse(T.pin([]))
            first = sorted(before)[0]
            self.assertTrue(T.pin([first]))
            self.assertEqual(os.sched_getaffinity(0), {first})
        finally:
            os.sched_setaffinity(0, before)

    def test_host_big_cores_when_present(self):
        big = T.big_cpus()
        for c in big:
            self.assertIn(c, T.cpus_by_part()[T.PART_X925])


if __name__ == "__main__":
    unittest.main()
