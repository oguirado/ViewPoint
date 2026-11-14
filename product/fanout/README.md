# product/fanout

Track P unit P2 (`claude-fable-5-2`, non-Gemini product work). Standalone
reference code: Python 3 standard library only, no imports from the rest of
the repository. `swarm/` must never import it.

| File | Content |
|---|---|
| `fanout.py` | Single-producer, N-reader immutable frame ring on atomic filesystem snapshots |
| `attribution.py` | Integer-only split of `changed_regions` into own / spill / foreign-peer regions, with typed verdicts |
| `wender_leased_lock.patch` | Unified diff for `wender-agent` `wender/act/actor.py`: epoch + TTL lease with fencing |
| `WENDER_LEASED_LOCK.md` | Design of that patch |
| `test_fanout.py` | Tests for all three |

## Frame ring (`fanout.py`)

```python
from fanout import FrameBroadcaster, FrameReader, FrameEvicted

with FrameBroadcaster("/dev/shm/demo/frames", retention=8) as producer:
    producer.publish(png_bytes, view_epoch=3, scene_epoch=12,
                     changed_regions=[[600, 300, 80, 80]])

reader = FrameReader("/dev/shm/demo/frames")
try:
    frame = reader.next()          # None when nothing new
except FrameEvicted:
    frame = reader.resync()        # full re-observe from latest.json
```

- Files: `frame_<seq>.png`, `frame_<seq>.json`, `latest.json`. Each is written
  to a temp name and renamed, in the order image, sidecar, `latest.json`. The
  rename of `latest.json` is the commit point.
- Readers take no lock. Every returned frame has had its size and SHA-256
  checked against the sidecar; a mismatch raises `FrameCorrupt`
  (`FRAME_CORRUPT`).
- Retention `K`: with head `H`, frames `H-K+1 .. H` are retained. Reading
  `seq <= H-K` raises `FrameEvicted` (`FRAME_EVICTED`) with `requested`,
  `oldest` and `head`. The decision comes from `latest.json`, so it is the same
  whether or not the producer has unlinked the files yet. `next()` leaves the
  cursor where it was; the reader has to `resync()`.
- Reading past the head raises `FrameNotPublished`; `next()` returns `None`.
- One producer per directory, enforced with a non-blocking `flock`
  (`ProducerBusy`). A restarted producer continues from `latest.json` and
  removes temp files and out-of-ring frames.
- The sidecar is the contract's frame sidecar plus `size_bytes`, `retention`
  and `oldest_frame`.
- `encode_png` / `decode_png` handle 8-bit gray, RGB and RGBA without
  interlacing, enough to feed `attribution.diff_regions`.
- `fsync=True` makes frames durable across power loss; the default is off,
  since the ring is meant for `/dev/shm`.

## Attribution (`attribution.py`)

```python
from attribution import attribute

result = attribute(
    changed_regions=[[600, 300, 80, 80]],     # dispatch_capture_seq -> verify_frame_ref
    footprint=[600, 300, 80, 80],
    dispatch_capture_seq=44, verify_frame_ref=45,
    peer_expectations=[{"actor_id": "modeler-agent", "regions": [[0, 0, 100, 100]]}],
    based_on_frame=41, baseline_regions=[],   # changes 41 -> 44
)
result["verdict"], result["own_regions"], result["spill_regions"], result["foreign_peer_regions"]
```

Each changed pixel gets exactly one class:

| Class | Inside `footprint` | Expected by a peer |
|---|---|---|
| `own_regions` | yes | no |
| `spill_regions` | no | no |
| `foreign_peer_regions` | either | yes |
| `contested_regions` (subset of foreign) | yes | yes |

Verdicts, first match wins:

| Verdict | Condition |
|---|---|
| `FOOTPRINT_MISSING` | `footprint` is not an integer `[x, y, w, h]` with `w, h > 0` |
| `STALE_FRAME` | Frame order is inconsistent, or `baseline_regions` intersect the footprint or `target_region` |
| `FOREIGN_CHANGE_REFUSED` | A peer-expected change lies inside the footprint, or an unattributed change touches `target_region` outside the footprint |
| `NO_VISIBLE_CHANGE` | No changed region |
| `VERIFIED_WITH_SPILL` | Unattributed change outside the footprint, or nothing changed inside it |
| `VERIFIED` | Every changed pixel is inside the footprint and expected by no peer, apart from peer-expected changes away from the footprint |

Points a caller should know:

- `VERIFIED` is only returned with empty `spill_regions` and
  `contested_regions` and a valid baseline; the function asserts this on every
  success. A change a peer declared, away from the footprint, does not
  downgrade the verdict, but it is always listed in `foreign_peer_regions` and
  `foreign_by_actor`.
- `FOREIGN_CHANGE_REFUSED` is not in `swarm/CONTRACT.md` section 6. It is a
  Track P code for the case the contract's two-way `VERIFIED` /
  `VERIFIED_WITH_SPILL` split cannot express: a changed area inside the
  footprint that a peer also claimed, so nobody can say whose it is.
- `STALE_FRAME` from the baseline check is returned with `dispatched: false`;
  it is meant to be evaluated before the act is sent.
- Output rectangles are disjoint, sorted by `(y, x, w, h)`, and depend only on
  the covered pixels, not on input order or on how the input was cut into
  rectangles.
- Floats and bools are rejected with `ValueError`. Rectangles are grid-merged,
  so the cost grows with the square of the number of input rectangles.
- `diff_regions(before, after, width, height, channels, tile=16)` computes
  tile-aligned changed regions from two raw pixel buffers.

## Tests

From the worktree root:

```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 -m unittest discover -s product/fanout -p 'test_*.py' -v
```

- Ring: 1 producer and 8 reader threads over 400 frames with `K = 8`. Six
  readers race the producer and check every frame against an independently
  computed SHA-256. Two readers are parked on frame 1 until the head passes
  frame 10 and must then get `FRAME_EVICTED` for frame 2. Further cases cover
  the exact eviction boundary, tampering, a second producer and restart.
- Attribution: the cases in the table above, the negative controls, and 600
  seeded random cases compared pixel by pixel with a brute-force oracle.
- Lease: the code added by the patch is executed directly from the patch file.

Scratch directories are created under `product/fanout/` (`.tmp_ring_*`) and
removed after each test.
