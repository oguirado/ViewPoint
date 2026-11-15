"""Definition-based geometry reference and whole-fixture label checks.

The reference uses no production geometry helpers. Only the fixture loader and
derivation entry point are shared with test_labels_oracle; its TestCase classes
are not imported into this module or run a second time here.
"""

import itertools
import json
from pathlib import Path
import random
import sys
import unittest

from bench.oracle import labels_oracle as LO
from bench.oracle.tests import test_labels_oracle as existing

try:
    import jsonschema
except ImportError as exc:
    jsonschema = None
    SCHEMA_SKIP = "jsonschema is not importable (%s); skipping only JSON Schema validation" % exc


# Contract values are independent of the implementation under test.
TILE_MIN, CELL_MAX, AXIS_MAX = 16, 4096, 255
RESOLUTIONS = ((1920, 1080, 32, 60, 34), (3840, 2160, 64, 60, 34))
SEED = 20261018


def reference_grid(width, height):
    """Select the smallest permitted power-of-two tile using ceiling division."""
    for exponent in itertools.count():
        size = TILE_MIN * 2 ** exponent
        columns = (width + size - 1) // size
        rows = (height + size - 1) // size
        if columns <= AXIS_MAX and rows <= AXIS_MAX and columns * rows <= CELL_MAX:
            return size, columns, rows


def reference_morton(tx, ty):
    """Interleave x and y bits one at a time, without a spreading bit mask."""
    code, bit = 0, 0
    while tx or ty:
        code += (tx & 1) << (2 * bit)
        code += (ty & 1) << (2 * bit + 1)
        tx //= 2
        ty //= 2
        bit += 1
    return code


def reference_order(columns, rows):
    cells = list(itertools.product(range(columns), range(rows)))
    cells.sort(key=lambda cell: reference_morton(*cell))
    return {cell: rank for rank, cell in enumerate(cells)}


def reference_tiles(rect, size, columns, rows, order):
    """Brute-force EXT: visit every tile, including padding past the viewport.

    labels_oracle.py line 67 quotes the rule verbatim:
    EXT rule: tile (tx,ty) intersects [x,y,w,h] iff tx*s < x+w and (tx+1)*s > x and ty*s < y+h and (ty+1)*s > y.
    """
    x, y, width, height = rect
    if width <= 0 or height <= 0:
        return []
    hits = []
    for tx in range(columns):
        for ty in range(rows):
            if (tx * size < x + width and (tx + 1) * size > x
                    and ty * size < y + height and (ty + 1) * size > y):
                hits.append(order[(tx, ty)])
    return sorted(hits)


def reference_straddle(rect, width, height, size):
    """Interior edges must belong to enumerated grid lines, even for empty rects."""
    for axis, extent in enumerate((width, height)):
        grid_lines = set(range(0, extent + 1, size))
        start = rect[axis]
        for edge in (start, start + rect[axis + 2]):
            if edge in range(1, extent) and edge not in grid_lines:
                return True
    return False


def reference_clip(rect, width, height):
    """Enumerate covered integer pixel positions on each viewport axis."""
    axes = []
    for axis, extent in enumerate((width, height)):
        covered = [pixel for pixel in range(extent)
                   if rect[axis] <= pixel < rect[axis] + rect[axis + 2]]
        if not covered:
            return None
        axes.append(covered)
    return [axes[0][0], axes[1][0], len(axes[0]), len(axes[1])]


def boundary_rects(width, height, size, rows):
    """Screen/tile contact, crossings, reversed/empty intervals, and padding."""
    return [
        [0, 0, width, height], [-1, -1, width + 2, height + 2],
        [0, 0, 1, 1], [width - 1, height - 1, 1, 1],
        [-size, 0, size, size], [width, 0, size, size],
        [0, -size, size, size], [0, height, size, 1],
        [-1, 0, 2, size], [width - 1, 0, 2, size],
        [0, -1, size, 2], [0, height - 1, size, 2],
        [-1, -1, 2, 2], [width - 1, height - 1, 2, 2],
        [size, size, size, size], [size - 1, size - 1, 2, 2],
        [size, size, size + 1, size + 1],
        [0, (rows - 1) * size, size, height - (rows - 1) * size],
        [0, rows * size - 1, size, 1], [0, rows * size, size, 1],
        [0, height, size, rows * size - height],
        [0, 0, 0, 0], [1, 0, 0, size], [size, 0, 0, size],
        [0, 1, size, 0], [0, size, size, 0],
        [width, height, 0, 0], [1, 1, -1, size], [1, 1, size, -1],
        [-2 * size, -2 * size, size, size],
        [width + size, height + size, size, size],
    ]


def random_rects(width, height, size):
    """200 reproducible rectangles, with forced edge contact/crossing/empties."""
    rng = random.Random(SEED)
    for index in range(200):
        x = rng.randint(-width, 2 * width)
        y = rng.randint(-height, 2 * height)
        w = rng.randint(1, width + 2 * size)
        h = rng.randint(1, height + 2 * size)
        kind = index % 12
        if kind == 0:
            x = 0
        elif kind == 1:
            x = width - w
        elif kind == 2:
            y = 0
        elif kind == 3:
            y = height - h
        elif kind == 4:
            x, y = -rng.randint(1, size), rng.randrange(height)
            w = -x + rng.randint(1, size)
        elif kind == 5:
            x, y = width - rng.randint(1, size), rng.randrange(height)
            w = width - x + rng.randint(1, size)
        elif kind == 6:
            x, y = rng.randrange(width), -rng.randint(1, size)
            h = -y + rng.randint(1, size)
        elif kind == 7:
            x, y = rng.randrange(width), height - rng.randint(1, size)
            h = height - y + rng.randint(1, size)
        elif kind == 8:
            w = 0
        elif kind == 9:
            h = 0
        yield [x, y, w, h]


class DifferentialGridReference(unittest.TestCase):
    def test_grid_and_every_dense_rank_against_definition(self):
        for width, height, size, columns, rows in RESOLUTIONS:
            with self.subTest(resolution=(width, height)):
                params = reference_grid(width, height)
                self.assertEqual(params, (size, columns, rows))
                self.assertEqual(LO.grid_params(width, height), params)
                expected = reference_order(columns, rows)
                actual = LO.order_table(width, height)
                self.assertEqual(actual, expected)
                self.assertEqual(sorted(actual.values()), list(range(columns * rows)))
                for cell, rank in actual.items():
                    self.assertIs(type(rank), int, cell)
                    self.assertTrue(0 <= rank < columns * rows, cell)
                    # Sparse codes coincide with early ranks; distinguish them
                    # wherever holes in the rectangular Morton grid require it.
                    code = reference_morton(*cell)
                    if expected[cell] != code:
                        self.assertNotEqual(rank, code, cell)
                self.assertEqual(reference_morton(0, 32), 2048)
                self.assertEqual(actual[(0, 32)], 1920)
                self.assertNotEqual(actual[(0, 32)], reference_morton(0, 32))

    def test_every_single_cell_returns_its_dense_rank(self):
        for width, height, _, _, _ in RESOLUTIONS:
            size, columns, rows = reference_grid(width, height)
            for (tx, ty), rank in reference_order(columns, rows).items():
                with self.subTest(resolution=(width, height), cell=(tx, ty)):
                    actual = LO.rect_tiles(width, height, [tx * size, ty * size, size, size])
                    self.assertEqual(actual, [rank])
                    self.assertIs(type(actual[0]), int)

    def test_rectangles_against_brute_force_geometry(self):
        for width, height, _, _, _ in RESOLUTIONS:
            size, columns, rows = reference_grid(width, height)
            order = reference_order(columns, rows)
            seeded = list(random_rects(width, height, size))
            self.assertEqual(len(seeded), 200)
            groups = (("boundary", boundary_rects(width, height, size, rows)), ("seeded", seeded))
            for group, rects in groups:
                for index, rect in enumerate(rects):
                    with self.subTest(resolution=(width, height), group=group,
                                      seed=SEED, index=index, rect=rect):
                        actual = LO.rect_tiles(width, height, rect)
                        self.assertEqual(actual, reference_tiles(rect, size, columns, rows, order))
                        self.assertEqual(actual, sorted(set(actual)))
                        for rank in actual:
                            self.assertIs(type(rank), int)
                            self.assertTrue(0 <= rank < columns * rows)
                        self.assertIs(LO.straddle(rect, width, height),
                                      reference_straddle(rect, width, height, size))
                        self.assertEqual(LO.clip_rect(rect, width, height),
                                         reference_clip(rect, width, height))


class FixtureLabelContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = []
        for name in ("oracle_samples_wl1s.json", "oracle_samples_wl4.json"):
            data = existing.fx(name)
            labels = existing.D(data["samples"], data["ops"])
            cls.runs.append((name, data, labels))

    def test_every_fixture_label_validates_and_matches_reference_tiles(self):
        width, height = existing.W, existing.H
        size, columns, rows = reference_grid(width, height)
        order = reference_order(columns, rows)
        for name, _, labels in self.runs:
            self.assertTrue(labels, name)
            for label in labels:
                with self.subTest(fixture=name, event_id=label["event_id"]):
                    LO.validate_label(label)
                    expected_tiles = set()
                    for rect in label["rects"]:
                        self.assertEqual(rect, reference_clip(rect, width, height))
                        expected_tiles.update(reference_tiles(rect, size, columns, rows, order))
                    self.assertEqual(label["tiles"], sorted(expected_tiles))
                    for rank in label["tiles"]:
                        self.assertIs(type(rank), int)
                        self.assertTrue(0 <= rank < columns * rows)
                    self.assertEqual("STRADDLE" in label["flags"],
                                     any(reference_straddle(r, width, height, size)
                                         for r in label["rects"]))

    def test_every_fixture_label_against_json_schema(self):
        if jsonschema is None:
            print("SKIP: " + SCHEMA_SKIP, file=sys.stderr)
            self.skipTest(SCHEMA_SKIP)
        path = Path(existing.HERE).parent / "label_schema.json"
        with path.open(encoding="utf-8") as stream:
            schema = json.load(stream)
        validator_type = jsonschema.validators.validator_for(schema)
        validator_type.check_schema(schema)
        validator = validator_type(schema)
        for name, _, labels in self.runs:
            self.assertTrue(labels, name)
            for label in labels:
                with self.subTest(fixture=name, event_id=label["event_id"]):
                    validator.validate(label)

    def test_moved_vectors_follow_fixture_deltas(self):
        sources = set()
        for name, data, labels in self.runs:
            with self.subTest(fixture=name):
                moved = [label for label in labels if label["ideal"] == 3]
                self.assertTrue(moved, name)
                sources.update(label["source"] for label in moved)
                # mv translates previous content: a positive scroll is negative
                # content motion. Expected values come from the raw samples.
                expected_scroll = [
                    [a["sx"] - b["sx"], a["sy"] - b["sy"]]
                    for a, b in zip(data["samples"], data["samples"][1:])
                    if (a["sx"], a["sy"]) != (b["sx"], b["sy"])
                ]
                self.assertEqual([label["mv"] for label in moved if label["source"] == "scroll"],
                                 expected_scroll)
                # WL4 is window motion, so compare element translation rather
                # than negating its unchanged document scroll coordinates.
                previous, expected_windows = {}, []
                for sample in data["samples"]:
                    if sample["nodes"] is None:
                        continue
                    current = {node[0]: node for node in sample["nodes"]}
                    for node_id in sorted(previous.keys() & current.keys()):
                        a, b = previous[node_id], current[node_id]
                        if a[3:5] == b[3:5] and a[1:3] != b[1:3]:
                            expected_windows.append((str(node_id), [b[1] - a[1], b[2] - a[2]]))
                    previous = current
                self.assertEqual([(label["source_ref"], label["mv"])
                                  for label in moved if label["source"] == "window"],
                                 expected_windows)
        self.assertEqual(sources, {"scroll", "window"})


if __name__ == "__main__":
    program = unittest.main(exit=False)
    sys.exit(0 if program.result.wasSuccessful() else 2)
