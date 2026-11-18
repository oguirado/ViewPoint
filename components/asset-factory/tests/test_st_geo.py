"""ST-GEO controls for cache authority, early rejection and chunk coverage."""
from fractions import Fraction as F
from unittest import TestCase, mock

import numpy as np

import test_d18_geo_certificate as fixtures
from tools.mesh import certificate as geo, certificate_fast as fast


class CacheTests(TestCase):
    def setUp(self):
        from tools.mesh.certificate_cache import CertificateCache

        self.fixture = fixtures.Gates()
        self.fixture.setUp()
        self.cache = CertificateCache()

    def args(self, candidate=None):
        f = self.fixture
        dom = f.domain_pin()
        candidate = f.raw if candidate is None else candidate
        regs = [f.policy["raw_registration_digest"], f.registration(candidate, dom, f.patches)]
        return [f.raw, candidate], dict(domain_digest=dom, registration_digests=regs,
                                        read_checked=f.store.read)

    def test_hit_reopens_authority_and_returns_an_independent_receipt(self):
        blobs, kwargs = self.args()
        baseline = geo.certify(blobs, **kwargs)
        self.assertEqual((baseline["measurement_rc"], baseline["verdict"]), (0, "CANNOT_SEE"))
        first = geo.certify(blobs, **kwargs, certificate_cache=self.cache)
        self.assertEqual(first, baseline)
        first["levels"].clear()
        with mock.patch.object(geo, "_geometry", side_effect=AssertionError("cache missed")):
            self.assertEqual(geo.certify(blobs, **kwargs, certificate_cache=self.cache), baseline)
        del self.fixture.store["raw-intent", self.fixture.policy["intent"]]
        cold = geo.certify(blobs, **kwargs)
        self.assertEqual(cold["measurement_rc"], 3)
        self.assertEqual(geo.certify(blobs, **kwargs, certificate_cache=self.cache), cold)

    def test_stale_policy_cannot_reuse_a_previous_measurement(self):
        blobs, kwargs = self.args()
        before = geo.certify(blobs, **kwargs, certificate_cache=self.cache)
        self.assertEqual(before["measurement_rc"], 0)
        self.fixture.policy["face_budgets"] = [3]
        changed, other = self.args()
        self.assertNotEqual(kwargs["domain_digest"], other["domain_digest"])
        exact = geo.certify(changed, **other)
        self.assertEqual((exact["verdict"], exact["reason_codes"]), ("FAIL", ["lod_face_budget"]))
        old_entry = next(iter(self.cache._entries.values()))
        self.assertEqual(geo.certify(changed, **other, certificate_cache=self.cache), exact)
        new_key = next(reversed(self.cache._entries))
        self.cache._entries[new_key] = old_entry
        replayed = geo.certify(changed, **other, certificate_cache=self.cache)
        self.assertEqual(replayed["reason_codes"], ["lod_face_budget"])
        self.assertEqual(replayed, exact)

    def test_cache_key_binds_all_receipt_inputs(self):
        from tools.mesh.certificate_cache import measurement_key

        identity = dict(subjects=["sha256:" + "a"*64, "sha256:" + "b"*64],
                        policy="sha256:" + "c"*64, parameters={"refinements": 1},
                        scope={"kind": "native"}, registrations=["sha256:" + "d"*64],
                        checker="sha256:" + "e"*64, toolchain="sha256:" + "f"*64)
        original = measurement_key(**identity)
        for field, changed in (
            ("subjects", identity["subjects"][::-1]), ("subjects", identity["subjects"][:1] * 2),
            ("policy", "sha256:" + "0"*64), ("parameters", {"refinements": 2}),
            ("scope", {"kind": "qualified"}), ("registrations", []),
            ("checker", "sha256:" + "0"*64), ("toolchain", "sha256:" + "0"*64),
        ):
            with self.subTest(field=field, changed=changed):
                self.assertNotEqual(measurement_key(**dict(identity, **{field: changed})), original)


class CheapRejectTests(TestCase):
    def test_separated_planes_skip_edge_solves_but_crossing_remains_fail(self):
        first = ((0, 0, 0), (4, 0, 4), (0, 4, 4))
        separated = tuple((x, y, z + 1) for x, y, z in first)
        crossing = ((0, 0, 1), (4, 0, 3), (0, 3, 3))
        for triangle, expected in ((separated, None), (crossing, "forbidden_triangle_intersection")):
            rows = [(geo._box(t), "shell", t) for t in (first, triangle)]
            counts = []
            for enabled in (False, True):
                work = geo.Work(10, cheap_reject=enabled)
                reason = None
                with mock.patch.object(geo, "_edge_contacts", wraps=geo._edge_contacts) as contact:
                    try:
                        geo._embedded(rows, work)
                    except geo.InputIssue as exc:
                        reason = exc.code
                self.assertEqual((work.used, reason), (3, expected))
                counts.append(contact.call_count)
            if expected is None:
                self.assertEqual(counts, [6, 0])


class ChunkTests(TestCase):
    def test_omitted_chunk_cannot_supply_a_certificate(self):
        triangle = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
        tree = geo._TriangleBVH([triangle], geo.Work(20))
        batch = fast.Batch(tree.root)
        cells = [triangle] * 8

        def missing_chunk(coords, boxes, branches, triangles, scale, exact, affinity, workers):
            counts = np.ones(len(coords), dtype=np.int64)
            work = np.full(len(coords), 6, dtype=np.int64)
            choices = np.full((len(coords), fast._MAX_CANDIDATES), 15, dtype=np.int64)
            placement = np.zeros(workers, dtype=np.int8)
            placement[-1] = fast.CHUNK_UNWRITTEN
            return counts, work, choices, placement

        with mock.patch.object(fast, "_batch", missing_chunk):
            with self.assertRaisesRegex(geo.InputIssue, "geometry_chunk_coverage"):
                batch.evaluate(cells, workers=2)

    def test_chunked_directed_bounds_equal_exact_oracle(self):
        target = [((0, 0, 0), (2, 0, 0), (0, 2, 0))]
        source = [tuple((x, y, F(i, 97)) for x, y, _ in target[0]) for i in range(1, 98)]

        def measure(work):
            integral = []
            try:
                result = geo.directed_bounds(source, target, F(1, 2), 2, work, integral=integral)
                geo.decide(*result, F(1, 2), "scope")
                verdict = None
            except geo.InputIssue as exc:
                verdict = (exc.rc, exc.code)
            return locals().get("result"), integral, work.used, verdict

        for limit in (300, 10**6):
            with mock.patch.object(geo, "_BATCH_MIN_CELLS", len(source) + 1):
                exact = measure(geo.Work(limit))
            for workers in (1, 2, 4, 8):
                self.assertEqual(measure(geo.Work(limit, chunk_workers=workers)), exact)

    def test_inward_rounding_has_an_exact_counterexample(self):
        p = (F(2, 3), F(5, 7), F(7, 11))
        t = ((F(1, 3),) * 3,) * 3
        q, *vertices = np.array([p, *t], dtype=np.float64)
        exact = geo.triangle_d2(p, t)
        outer = fast._triangle_interval(q, np.array(vertices), fast._UNIT_PAD, False)
        inward = fast._triangle_interval(q, np.array(vertices), 0., True)
        self.assertLessEqual(F(outer[0]), exact)
        self.assertGreaterEqual(F(outer[1]), exact)
        self.assertFalse(F(inward[0]) <= exact <= F(inward[1]))
