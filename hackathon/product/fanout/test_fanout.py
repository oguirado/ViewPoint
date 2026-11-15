"""Tests for product/fanout: frame ring, attribution and the leased-lock patch.

Run from the worktree root:
    python3 -m unittest discover -s product/fanout -p 'test_*.py' -v
"""
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import attribution as at  # noqa: E402
import fanout as fo  # noqa: E402

PATCH = os.path.join(HERE, "wender_leased_lock.patch")


def payload(seq):
    """Deterministic, seq-dependent, variable-size frame body."""
    seed = hashlib.sha256(b"frame-%d" % seq).digest()
    return seed * (600 + (seq * 37) % 900)  # 19 KB .. 48 KB


class RingCase(unittest.TestCase):
    def setUp(self):
        # Scratch stays under product/fanout/ (the unit's only writable tree).
        self.root = tempfile.mkdtemp(prefix=".tmp_ring_", dir=HERE)
        self.addCleanup(shutil.rmtree, self.root, True)


class TestRingBasics(RingCase):
    def test_publish_read_and_contract_sidecar(self):
        png = fo.encode_png(4, 2, bytes(range(24)), channels=3)
        with fo.FrameBroadcaster(self.root, retention=4) as prod:
            meta = prod.publish(png, view_epoch=3, scene_epoch=12,
                                changed_regions=[[600, 300, 80, 80]])
        reader = fo.FrameReader(self.root)
        frame = reader.read(1)
        self.assertEqual(frame.data, png)
        self.assertEqual(frame.meta, meta)
        self.assertEqual(frame.sha256, hashlib.sha256(png).hexdigest())
        for key in ("v", "frame", "sha256", "width", "height", "view_epoch",
                    "scene_epoch", "changed_regions", "ts_ms"):
            self.assertIn(key, frame.meta)
        self.assertEqual((frame.meta["width"], frame.meta["height"]), (4, 2))
        self.assertEqual(reader.latest(), meta)
        self.assertEqual(fo.decode_png(frame.data), (4, 2, 3, bytes(range(24))))

    def test_empty_ring(self):
        reader = fo.FrameReader(self.root)
        self.assertIsNone(reader.latest())
        self.assertIsNone(reader.read_latest())
        self.assertIsNone(reader.next())
        with self.assertRaises(fo.FrameNotPublished):
            reader.read(1)

    def test_eviction_boundary_is_exact(self):
        with fo.FrameBroadcaster(self.root, retention=4) as prod:
            for seq in range(1, 11):
                prod.publish(payload(seq))
        reader = fo.FrameReader(self.root)
        for seq in range(1, 7):  # head 10, K 4 -> evicted iff seq <= 6
            with self.assertRaises(fo.FrameEvicted) as ctx:
                reader.read(seq)
            self.assertEqual(ctx.exception.code, "FRAME_EVICTED")
            self.assertEqual((ctx.exception.requested, ctx.exception.oldest,
                              ctx.exception.head), (seq, 7, 10))
        for seq in range(7, 11):
            self.assertEqual(reader.read(seq).data, payload(seq))
        with self.assertRaises(fo.FrameNotPublished) as ctx:
            reader.read(11)
        self.assertEqual(ctx.exception.code, "FRAME_NOT_PUBLISHED")
        names = sorted(n for n in os.listdir(self.root) if n.startswith("frame_"))
        self.assertEqual(names, sorted("frame_%d.%s" % (s, e)
                                       for s in range(7, 11) for e in ("png", "json")))

    def test_slow_cursor_must_resync(self):
        with fo.FrameBroadcaster(self.root, retention=3) as prod:
            prod.publish(payload(1))
            reader = fo.FrameReader(self.root)
            self.assertEqual(reader.next().seq, 1)
            for seq in range(2, 8):
                prod.publish(payload(seq))
            with self.assertRaises(fo.FrameEvicted):
                reader.next()
            self.assertEqual(reader.cursor, 1)  # not advanced silently
            self.assertEqual(reader.resync().seq, 7)
            self.assertIsNone(reader.next())

    def test_logical_eviction_does_not_depend_on_unlink(self):
        with fo.FrameBroadcaster(self.root, retention=2) as prod:
            for seq in range(1, 4):
                prod.publish(payload(seq))
            # Resurrect an evicted file: the ring, not the directory, decides.
            for name in ("frame_1.png", "frame_1.json"):
                with open(os.path.join(self.root, name), "wb") as handle:
                    handle.write(b"{}")
            with self.assertRaises(fo.FrameEvicted):
                fo.FrameReader(self.root).read(1)

    def test_tampered_frame_is_refused(self):
        with fo.FrameBroadcaster(self.root, retention=4) as prod:
            prod.publish(payload(1))
            prod.publish(payload(2))
        path = os.path.join(self.root, "frame_1.png")
        body = bytearray(payload(1))
        body[100] ^= 1  # same size, one flipped bit
        with open(path, "wb") as handle:
            handle.write(body)
        with self.assertRaises(fo.FrameCorrupt) as ctx:
            fo.FrameReader(self.root).read(1)
        self.assertIn("sha256", str(ctx.exception))
        with open(os.path.join(self.root, "frame_2.png"), "ab") as handle:
            handle.write(b"x")
        with self.assertRaises(fo.FrameCorrupt):
            fo.FrameReader(self.root).read(2)
        os.unlink(path)  # retained but missing: corruption, not eviction
        with self.assertRaises(fo.FrameCorrupt):
            fo.FrameReader(self.root).read(1)

    def test_single_producer_and_restart(self):
        first = fo.FrameBroadcaster(self.root, retention=3)
        with self.assertRaises(fo.ProducerBusy):
            fo.FrameBroadcaster(self.root, retention=3)
        for seq in range(1, 6):
            first.publish(payload(seq))
        first.close()
        with open(os.path.join(self.root, ".tmp-frame_9.png.1"), "wb") as handle:
            handle.write(b"partial")  # crash leftover
        with fo.FrameBroadcaster(self.root, retention=3) as second:
            self.assertEqual(second.head, 5)
            self.assertEqual(second.publish(payload(6))["frame"], 6)
        self.assertFalse([n for n in os.listdir(self.root) if n.startswith(".tmp-")])
        self.assertEqual(fo.FrameReader(self.root).read_latest().data, payload(6))


class TestRingConcurrency(RingCase):
    FRAMES = 400
    RETENTION = 8
    FAST = 6
    SLOW = 2

    def test_one_producer_eight_readers(self):
        k, total = self.RETENTION, self.FRAMES
        expected = {seq: hashlib.sha256(payload(seq)).hexdigest()
                    for seq in range(1, total + 1)}
        first_out = threading.Event()   # frame 1 is readable
        slow_parked = threading.Barrier(self.SLOW + 1)
        lapped = threading.Event()      # head >= 1 + k + 1: frame 2 is evicted
        done = threading.Event()
        failures = []
        stats = {}

        def check(frame):
            if hashlib.sha256(frame.data).hexdigest() != expected[frame.seq]:
                raise AssertionError("torn read of frame %d" % frame.seq)
            if frame.meta["sha256"] != expected[frame.seq]:
                raise AssertionError("sidecar sha mismatch on frame %d" % frame.seq)
            if frame.meta["frame"] != frame.seq or frame.meta["size_bytes"] != len(frame.data):
                raise AssertionError("sidecar/frame mismatch on %d" % frame.seq)

        def producer():
            try:
                with fo.FrameBroadcaster(self.root, retention=k) as prod:
                    prod.publish(payload(1))
                    first_out.set()
                    slow_parked.wait(30)  # both slow readers hold cursor == 1
                    for seq in range(2, total + 1):
                        prod.publish(payload(seq))
                        if seq == k + 2:
                            lapped.set()
            except BaseException as exc:  # noqa: B902 - reported to main thread
                failures.append(("producer", repr(exc)))
                lapped.set()
            finally:
                first_out.set()
                done.set()

        def fast(name):
            reader = fo.FrameReader(self.root, name)
            seen, evicted, last = 0, 0, 0
            try:
                first_out.wait(30)
                while True:
                    finished = done.is_set()
                    try:
                        frame = reader.next() if seen % 2 else reader.read_latest()
                    except fo.FrameEvicted:
                        evicted += 1
                        frame = reader.resync()
                    if frame is not None:
                        check(frame)
                        if frame.seq < last:
                            raise AssertionError("time went backwards")
                        last = reader.cursor = frame.seq
                        seen += 1
                    if finished and last == total:
                        break
            except BaseException as exc:  # noqa: B902
                failures.append((name, repr(exc)))
            stats[name] = (seen, evicted, last)

        def slow(name):
            reader = fo.FrameReader(self.root, name)
            try:
                first_out.wait(30)
                check(reader.next())          # consumes frame 1
                slow_parked.wait(30)
                if not lapped.wait(60):
                    raise AssertionError("producer never lapped the slow reader")
                try:
                    reader.next()             # wants frame 2, head >= k + 2
                except fo.FrameEvicted as exc:
                    if exc.requested != 2 or exc.code != "FRAME_EVICTED":
                        raise AssertionError("wrong eviction: %r" % (exc,))
                    if exc.head - k + 1 != exc.oldest or exc.oldest <= 2:
                        raise AssertionError("inconsistent ring bounds: %r" % (exc,))
                else:
                    raise AssertionError("slow reader was not told FRAME_EVICTED")
                if reader.cursor != 1:
                    raise AssertionError("cursor moved on eviction")
                frame = reader.resync()
                check(frame)
                done.wait(60)
                check(reader.resync())
                stats[name] = (2, 1, reader.cursor)
            except BaseException as exc:  # noqa: B902
                failures.append((name, repr(exc)))

        threads = [threading.Thread(target=producer, name="producer")]
        threads += [threading.Thread(target=fast, args=("fast-%d" % i,)) for i in range(self.FAST)]
        threads += [threading.Thread(target=slow, args=("slow-%d" % i,)) for i in range(self.SLOW)]
        self.assertEqual(len(threads), 9)  # 1 producer + 8 readers
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
            self.assertFalse(thread.is_alive(), "%s hung" % thread.name)
        self.assertEqual(failures, [])
        self.assertEqual(len(stats), 8)
        for name, (seen, evicted, last) in sorted(stats.items()):
            self.assertGreaterEqual(seen, 1, name)
            self.assertEqual(last, total, name)
            if name.startswith("slow"):
                self.assertEqual(evicted, 1, name)
        # Ring invariant after the run: exactly K frames, all verifiable.
        reader = fo.FrameReader(self.root)
        pngs = [n for n in os.listdir(self.root) if n.endswith(".png")]
        self.assertEqual(len(pngs), k)
        for seq in range(total - k + 1, total + 1):
            check(reader.read(seq))
        with self.assertRaises(fo.FrameEvicted):
            reader.read(total - k)


FP = [600, 300, 80, 80]


class TestAttribution(unittest.TestCase):
    def test_contract_example_is_verified(self):
        out = at.attribute([[600, 300, 80, 80]], FP, 44, 45)
        self.assertEqual(out["verdict"], "VERIFIED")
        self.assertEqual(out["own_regions"], [[600, 300, 80, 80]])
        self.assertEqual(out["spill_regions"], [])
        self.assertEqual(out["foreign_peer_regions"], [])
        self.assertFalse(out["refused"])
        self.assertEqual((out["dispatch_capture_seq"], out["verify_frame_ref"]), (44, 45))

    def test_spill_outside_footprint_is_never_plain_verified(self):
        out = at.attribute([[640, 340, 100, 20]], FP, 44, 45)  # straddles the edge
        self.assertEqual(out["verdict"], "VERIFIED_WITH_SPILL")
        self.assertEqual(out["own_regions"], [[640, 340, 40, 20]])
        self.assertEqual(out["spill_regions"], [[680, 340, 60, 20]])

    def test_undeclared_foreign_change_elsewhere_is_spill(self):
        out = at.attribute([[610, 310, 10, 10], [0, 0, 50, 50]], FP, 44, 45)
        self.assertEqual(out["verdict"], "VERIFIED_WITH_SPILL")
        self.assertEqual(out["spill_regions"], [[0, 0, 50, 50]])
        self.assertEqual(out["foreign_peer_regions"], [])

    def test_declared_peer_change_away_from_footprint_is_attributed(self):
        peers = [{"actor_id": "modeler-agent", "regions": [[0, 0, 100, 100]]}]
        out = at.attribute([[610, 310, 10, 10], [20, 20, 30, 30]], FP, 44, 45,
                           peer_expectations=peers)
        self.assertEqual(out["verdict"], "VERIFIED")
        self.assertEqual(out["own_regions"], [[610, 310, 10, 10]])
        self.assertEqual(out["spill_regions"], [])
        self.assertEqual(out["foreign_peer_regions"], [[20, 20, 30, 30]])
        self.assertEqual(out["foreign_by_actor"], {"modeler-agent": [[20, 20, 30, 30]]})

    def test_negctl_peer_change_inside_footprint_is_refused(self):
        peers = [{"actor_id": "modeler-agent", "footprint": [650, 350, 200, 200]}]
        out = at.attribute([[600, 300, 80, 80]], FP, 44, 45, peer_expectations=peers)
        self.assertEqual(out["verdict"], "FOREIGN_CHANGE_REFUSED")
        self.assertTrue(out["refused"])
        self.assertEqual(out["contested_regions"], [[650, 350, 30, 30]])
        self.assertEqual(out["foreign_peer_regions"], [[650, 350, 30, 30]])
        self.assertEqual(at.area(out["own_regions"]), 80 * 80 - 30 * 30)
        # Control: the same peer expectation with no change in the overlap passes.
        calm = at.attribute([[600, 300, 40, 40]], FP, 44, 45, peer_expectations=peers)
        self.assertEqual(calm["verdict"], "VERIFIED")

    def test_negctl_unattributed_change_on_target_outside_footprint(self):
        out = at.attribute([[610, 310, 10, 10], [700, 300, 10, 10]], FP, 44, 45,
                           target_region=[600, 300, 200, 80])
        self.assertEqual(out["verdict"], "FOREIGN_CHANGE_REFUSED")
        self.assertEqual(out["spill_regions"], [[700, 300, 10, 10]])

    def test_negctl_baseline_invalidated_is_stale_frame(self):
        out = at.attribute([[600, 300, 80, 80]], FP, 44, 45, based_on_frame=41,
                           baseline_regions=[[670, 370, 50, 50]])
        self.assertEqual(out["verdict"], "STALE_FRAME")
        self.assertTrue(out["refused"])
        self.assertFalse(out["dispatched"])
        self.assertEqual(out["baseline_conflict_regions"], [[670, 370, 10, 10]])
        self.assertEqual(out["own_regions"], [])
        # A foreign change touching only the target (not the footprint) too.
        out = at.attribute([], FP, 44, 45, based_on_frame=41,
                           baseline_regions=[[900, 300, 5, 5]],
                           target_region=[600, 300, 400, 80])
        self.assertEqual(out["verdict"], "STALE_FRAME")

    def test_valid_rebase_when_baseline_change_is_elsewhere(self):
        out = at.attribute([[600, 300, 80, 80]], FP, 44, 45, based_on_frame=41,
                           baseline_regions=[[0, 0, 10, 10]])
        self.assertEqual(out["verdict"], "VERIFIED")
        self.assertTrue(out["rebased"])

    def test_frame_order_refusals(self):
        self.assertEqual(at.attribute([FP], FP, 44, 44)["verdict"], "STALE_FRAME")
        self.assertEqual(at.attribute([FP], FP, 44, 43)["verdict"], "STALE_FRAME")
        self.assertEqual(at.attribute([FP], FP, 44, 45, based_on_frame=45)["verdict"],
                         "STALE_FRAME")

    def test_footprint_missing_and_integer_only(self):
        for bad in (None, [1, 2, 3], [0, 0, 0, 5], [0, 0, 5.0, 5], [0, 0, True, 5], "abcd"):
            out = at.attribute([[0, 0, 1, 1]], bad, 44, 45)
            self.assertEqual(out["verdict"], "FOOTPRINT_MISSING", bad)
            self.assertTrue(out["refused"])
        with self.assertRaises(ValueError):
            at.attribute([[0, 0, 1.5, 1]], FP, 44, 45)
        with self.assertRaises(ValueError):
            at.attribute([[0, 0, 1, 1]], FP, 44.0, 45)

    def test_no_change_and_no_own_change(self):
        self.assertEqual(at.attribute([], FP, 44, 45)["verdict"], "NO_VISIBLE_CHANGE")
        peers = [{"actor_id": "p", "regions": [[0, 0, 9, 9]]}]
        out = at.attribute([[0, 0, 9, 9]], FP, 44, 45, peer_expectations=peers)
        self.assertEqual(out["verdict"], "VERIFIED_WITH_SPILL")  # nothing of ours changed

    def test_canonical_under_reordering_and_resplitting(self):
        whole = [[590, 290, 100, 100], [0, 0, 40, 40]]
        split = [[0, 0, 40, 15], [640, 290, 50, 100], [0, 15, 40, 25],
                 [590, 290, 50, 60], [590, 350, 50, 40], [600, 300, 20, 20]]
        peers_a = [{"actor_id": "b", "regions": [[0, 0, 20, 40]]},
                   {"actor_id": "a", "regions": [[10, 0, 30, 40]]}]
        ref = at.attribute(whole, FP, 1, 2, peer_expectations=peers_a)
        alt = at.attribute(split, FP, 1, 2, peer_expectations=list(reversed(peers_a)))
        self.assertEqual(json.dumps(ref, sort_keys=True), json.dumps(alt, sort_keys=True))
        self.assertEqual(ref["verdict"], "VERIFIED_WITH_SPILL")
        self.assertEqual(ref["own_regions"], [FP])
        self.assertEqual(at.area(ref["own_regions"]) + at.area(ref["spill_regions"])
                         + at.area(ref["foreign_peer_regions"]), at.area(whole))

    def test_law_against_brute_force_pixel_oracle(self):
        rng = random.Random(20261114)
        size = 24

        def rect():
            x, y = rng.randrange(size - 1), rng.randrange(size - 1)
            return [x, y, rng.randint(1, size - x), rng.randint(1, size - y)]

        def pixels(regions):
            return {(x, y) for r in regions
                    for x in range(r[0], r[0] + r[2]) for y in range(r[1], r[1] + r[3])}

        verdicts = {}
        for _ in range(600):
            footprint = rect()
            changed = [rect() for _ in range(rng.randint(0, 3))]
            peers = [{"actor_id": "peer-%d" % i, "regions": [rect()]}
                     for i in range(rng.randint(0, 2))]
            baseline = [rect()] if rng.random() < 0.2 else []
            out = at.attribute(changed, footprint, 5, 6, peer_expectations=peers,
                               based_on_frame=4, baseline_regions=baseline)
            verdicts[out["verdict"]] = verdicts.get(out["verdict"], 0) + 1
            c, f = pixels(changed), pixels([footprint])
            p = pixels([r for peer in peers for r in peer["regions"]])
            if pixels(baseline) & f:
                self.assertEqual(out["verdict"], "STALE_FRAME")
                continue
            self.assertEqual(pixels(out["own_regions"]), (c & f) - p)
            self.assertEqual(pixels(out["spill_regions"]), c - f - p)
            self.assertEqual(pixels(out["foreign_peer_regions"]), c & p)
            self.assertEqual(pixels(out["contested_regions"]), c & p & f)
            for key in ("own_regions", "spill_regions", "foreign_peer_regions"):
                self.assertEqual(at.area(out[key]), sum(r[2] * r[3] for r in out[key]), key)
            if c & p & f:
                self.assertEqual(out["verdict"], "FOREIGN_CHANGE_REFUSED")
            elif c - f - p:
                self.assertEqual(out["verdict"], "VERIFIED_WITH_SPILL")
            if out["verdict"] == "VERIFIED":
                self.assertTrue(c and c - p <= f and not (c & p & f))
        for verdict in ("VERIFIED", "VERIFIED_WITH_SPILL", "FOREIGN_CHANGE_REFUSED",
                        "STALE_FRAME"):
            self.assertGreater(verdicts.get(verdict, 0), 0, verdicts)


class TestPixelDiffThroughRing(RingCase):
    W, H = 96, 64

    def image(self, boxes):
        buf = bytearray(self.W * self.H * 3)
        for x, y, w, h in boxes:
            for row in range(y, y + h):
                start = (row * self.W + x) * 3
                buf[start:start + w * 3] = b"\xff\x80\x10" * w
        return bytes(buf)

    def test_diff_regions_tile_grid(self):
        before = self.image([])
        after = self.image([[16, 16, 16, 16], [70, 40, 3, 3]])
        self.assertEqual(at.diff_regions(before, after, self.W, self.H, 3, tile=16),
                         [[16, 16, 16, 16], [64, 32, 16, 16]])
        self.assertEqual(at.diff_regions(before, before, self.W, self.H, 3), [])
        ragged = at.diff_regions(before, self.image([[90, 60, 6, 4]]), self.W, self.H, 3,
                                 tile=40)
        self.assertEqual(ragged, [[80, 40, 16, 24]])  # clipped to the image

    def test_end_to_end_attribution_from_published_frames(self):
        footprint = [16, 16, 32, 16]
        frames = [self.image([]),
                  self.image([[16, 16, 16, 16]]),                    # own change
                  self.image([[16, 16, 16, 16], [70, 40, 3, 3]])]    # + foreign pixel
        with fo.FrameBroadcaster(self.root, retention=4) as prod:
            for buf in frames:
                prod.publish(fo.encode_png(self.W, self.H, buf))
        reader = fo.FrameReader(self.root)

        def changed(a, b):
            pa, pb = fo.decode_png(reader.read(a).data), fo.decode_png(reader.read(b).data)
            return at.diff_regions(pa[3], pb[3], pa[0], pa[1], pa[2], tile=16)

        own = at.attribute(changed(1, 2), footprint, 1, 2)
        self.assertEqual((own["verdict"], own["own_regions"]),
                         ("VERIFIED", [[16, 16, 16, 16]]))
        mixed = at.attribute(changed(1, 3), footprint, 1, 3)
        self.assertEqual(mixed["verdict"], "VERIFIED_WITH_SPILL")
        self.assertEqual(mixed["spill_regions"], [[64, 32, 16, 16]])
        # A second actor planned on frame 1 against a footprint the first one
        # just changed: its baseline is invalid.
        late = at.attribute([], [0, 0, 40, 40], 2, 3, based_on_frame=1,
                            baseline_regions=changed(1, 2))
        self.assertEqual(late["verdict"], "STALE_FRAME")


def load_patched_lock():
    """Execute the code added by wender_leased_lock.patch, and nothing else."""
    added, removed, files = [], [], []
    with open(PATCH, encoding="utf-8") as handle:
        for line in handle.read().split("\n"):
            if line.startswith("+++ "):
                files.append(line[4:].strip())
            elif line.startswith("--- "):
                continue
            elif line.startswith("+"):
                added.append(line[1:])
            elif line.startswith("-"):
                removed.append(line[1:])

    class ActorBusy(TypeError, RuntimeError):  # mirrors wender.act.actor.ActorBusy
        pass

    namespace = {"__name__": "wender_act_actor_patch", "threading": threading,
                 "ActorBusy": ActorBusy, "PATHS": ("api", "ax", "coordinates")}
    exec(compile("\n".join(added), PATCH, "exec"), namespace)
    return namespace, files, removed


class FakeClock(object):
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


SEC = 1000 * 1000 * 1000
TURN_TIMEOUT_NS = 60 * SEC


class TestLeasedLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns, cls.files, cls.removed = load_patched_lock()

    def setUp(self):
        self.clock = FakeClock()
        self.lock = self.ns["LeasedActorLock"](clock_ns=self.clock)
        self.x11 = []  # every injected input lands here

    def inject(self, who, key):
        self.x11.append((who, key))
        return "sent"

    def refused(self, code, fn, *args, **kwargs):
        with self.assertRaises(self.ns["LeaseRefused"]) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_patch_is_additive_and_scoped(self):
        self.assertEqual(self.files, ["b/wender/act/actor.py"])
        self.assertEqual(self.removed, [])  # legacy ActorLock is untouched
        self.assertEqual(self.ns["DEFAULT_TURN_TTL_NS"], TURN_TIMEOUT_NS)

    def test_grant_fields(self):
        lease = self.lock.grant("camera-agent")
        self.assertEqual(lease.as_dict(), {
            "holder_actor_id": "camera-agent", "lease_id": "lease-camera-agent-0001",
            "turn_epoch": 1, "expires_ns": 1000 + TURN_TIMEOUT_NS})
        self.assertEqual(self.lock.holder, "camera-agent")
        with self.assertRaises(AttributeError):
            lease.turn_epoch = 9
        with self.assertRaises(self.ns["ActorBusy"]):
            self.lock.grant("modeler-agent")
        for bad in (0, -1, TURN_TIMEOUT_NS + 1, 1.5, True):
            with self.assertRaises(ValueError):
                self.ns["LeasedActorLock"](clock_ns=self.clock).grant("a", bad)

    def test_stale_holder_cannot_actuate_after_turn_timeout(self):
        stale = self.lock.grant("camera-agent")
        self.assertEqual(self.lock.actuate(stale, self.inject, "camera-agent", "KP_1"), "sent")
        self.clock.now += TURN_TIMEOUT_NS - 1
        self.lock.actuate(stale, self.inject, "camera-agent", "KP_3")  # last valid ns
        self.clock.now += 1                                            # deadline reached
        self.refused("LEASE_EXPIRED", self.lock.actuate, stale, self.inject,
                     "camera-agent", "KP_7")
        self.assertIsNone(self.lock.holder)
        fresh = self.lock.grant("modeler-agent")
        self.assertEqual((fresh.turn_epoch, fresh.lease_id), (2, "lease-modeler-agent-0002"))
        # The stale holder wakes up while a new turn is live: still fenced out.
        self.refused("LEASE_EXPIRED", self.lock.actuate, stale, self.inject,
                     "camera-agent", "Home")
        self.refused("LEASE_EXPIRED", self.lock.release, stale)
        self.refused("LEASE_EXPIRED", self.lock.renew, stale)
        self.assertEqual(self.lock.holder, "modeler-agent")  # not released by the stale one
        self.lock.actuate(fresh, self.inject, "modeler-agent", "KP_4")
        self.assertEqual(self.x11, [("camera-agent", "KP_1"), ("camera-agent", "KP_3"),
                                    ("modeler-agent", "KP_4")])

    def test_same_actor_new_turn_does_not_revive_old_token(self):
        old = self.lock.grant("camera-agent")
        self.lock.release(old)
        new = self.lock.grant("camera-agent")
        self.assertEqual(new.turn_epoch, 2)
        self.refused("LEASE_EXPIRED", self.lock.actuate, old, self.inject, "camera-agent", "x")
        self.assertEqual(self.x11, [])

    def test_revoke_fences_immediately(self):
        lease = self.lock.grant("camera-agent")
        self.assertIs(self.lock.revoke(), lease)
        self.refused("LEASE_EXPIRED", self.lock.actuate, lease, self.inject, "camera-agent", "x")
        self.assertIsNone(self.lock.revoke())
        self.assertEqual(self.x11, [])

    def test_no_lease_and_forged_tokens(self):
        lease_cls = self.ns["Lease"]
        self.refused("NO_LEASE", self.lock.actuate, None, self.inject, "x", "x")
        self.refused("NO_LEASE", self.lock.actuate,
                     lease_cls("camera-agent", "lease-camera-agent-0001", 1, 1 << 62),
                     self.inject, "x", "x")  # nothing granted yet
        real = self.lock.grant("camera-agent")
        self.refused("NO_LEASE", self.lock.actuate,
                     lease_cls("modeler-agent", real.lease_id, 1, real.expires_ns),
                     self.inject, "x", "x")
        self.refused("NO_LEASE", self.lock.actuate,
                     lease_cls("camera-agent", "lease-camera-agent-0007", 7, 1 << 62),
                     self.inject, "x", "x")
        # A self-extended deadline is ignored: the lock's deadline is authoritative.
        self.clock.now += TURN_TIMEOUT_NS
        self.refused("LEASE_EXPIRED", self.lock.actuate,
                     lease_cls("camera-agent", real.lease_id, 1, 1 << 62),
                     self.inject, "x", "x")
        self.assertEqual(self.x11, [])

    def test_renew_and_budget_guard(self):
        lease = self.lock.grant("camera-agent", 10 * SEC)
        self.clock.now += 9 * SEC
        self.refused("LEASE_EXPIRED", self.lock.actuate, lease, self.inject,
                     "camera-agent", "x", budget_ns=2 * SEC)  # cannot finish in time
        renewed = self.lock.renew(lease, 10 * SEC)
        self.assertEqual((renewed.lease_id, renewed.turn_epoch),
                         (lease.lease_id, lease.turn_epoch))
        self.assertEqual(renewed.expires_ns, self.clock.now + 10 * SEC)
        self.lock.actuate(lease, self.inject, "camera-agent", "y", budget_ns=2 * SEC)
        self.assertEqual(self.x11, [("camera-agent", "y")])

    def test_execute_fenced(self):
        execute_fenced = self.ns["execute_fenced"]
        lease = self.lock.grant("camera-agent")
        calls = []
        out = execute_fenced(self.lock, lease, "coordinates", {"key": "KP_1"},
                             lambda path, action: calls.append((path, action)) or "ok")
        self.assertEqual((out, calls), ("ok", [("coordinates", {"key": "KP_1"})]))
        with self.assertRaises(ValueError):
            execute_fenced(self.lock, lease, "telepathy", {}, self.inject)
        self.lock.revoke()
        self.refused("LEASE_EXPIRED", execute_fenced, self.lock, lease, "coordinates",
                     {"key": "KP_1"}, lambda path, action: calls.append("late"))
        self.assertEqual(len(calls), 1)

    def test_threaded_stale_holder_wakes_after_timeout(self):
        asleep, wake = threading.Event(), threading.Event()
        outcome = []

        def sleepy_holder(lease):
            self.lock.actuate(lease, self.inject, "camera-agent", "KP_1")
            asleep.set()
            wake.wait(30)   # "thinking" straight through TURN_TIMEOUT
            try:
                self.lock.actuate(lease, self.inject, "camera-agent", "KP_Add")
                outcome.append("INJECTED")
            except self.ns["LeaseRefused"] as exc:
                outcome.append(exc.code)

        lease = self.lock.grant("camera-agent")
        thread = threading.Thread(target=sleepy_holder, args=(lease,))
        thread.start()
        self.assertTrue(asleep.wait(30))
        self.clock.now += TURN_TIMEOUT_NS + 1        # broker's TURN_TIMEOUT fires
        fresh = self.lock.grant("modeler-agent")     # next turn, epoch 2
        self.lock.actuate(fresh, self.inject, "modeler-agent", "KP_7")
        wake.set()
        thread.join(30)
        self.assertEqual(outcome, ["LEASE_EXPIRED"])
        self.assertEqual(self.x11, [("camera-agent", "KP_1"), ("modeler-agent", "KP_7")])

    def test_in_flight_act_drains_before_next_grant(self):
        entered, finish = threading.Event(), threading.Event()

        def slow_executor():
            entered.set()
            finish.wait(30)
            self.x11.append("slow-act-done")

        lease = self.lock.grant("camera-agent")
        thread = threading.Thread(target=self.lock.actuate, args=(lease, slow_executor))
        thread.start()
        self.assertTrue(entered.wait(30))
        self.clock.now += TURN_TIMEOUT_NS + 1  # lease expires while the act runs
        with self.assertRaises(self.ns["ActorBusy"]):
            self.lock.grant("modeler-agent")   # no overlap with the draining act
        self.assertEqual(self.lock.turn_epoch, 1)
        finish.set()
        thread.join(30)
        fresh = self.lock.grant("modeler-agent")
        self.assertEqual(fresh.turn_epoch, 2)
        self.refused("LEASE_EXPIRED", self.lock.actuate, lease, self.inject, "camera-agent", "x")
        self.assertEqual(self.x11, ["slow-act-done"])


if __name__ == "__main__":
    unittest.main()
