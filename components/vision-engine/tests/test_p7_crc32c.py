"""Unit tests for CRC32C research prototype (P7)."""

import random
import unittest
import numpy as np

from eval.p7_crc32c import (
    CHECK_VALUE_123456789,
    crc32c_bitwise,
    crc32c_frame_tiles,
    crc32c_numpy,
    crc32c_slice8,
)


class TestP7CRC32C(unittest.TestCase):
    """Test suite pinning P7 paper claims for CRC32C implementations."""

    def test_check_value(self):
        """Check value 0xE3069283 for ASCII string '123456789'."""
        data = b"123456789"
        expected = CHECK_VALUE_123456789

        c_bit = crc32c_bitwise(data)
        c_slice8 = crc32c_slice8(data)
        c_np = crc32c_numpy(data)

        self.assertEqual(c_bit, expected)
        self.assertEqual(c_slice8, expected)
        self.assertEqual(c_np, expected)

    def test_three_implementations_agree_2000_seeded_buffers(self):
        """All three implementations agree on 2000 seeded buffers of length 0 to 4096."""
        py_rng = random.Random(42)
        np_rng = np.random.default_rng(42)

        for _ in range(2000):
            length = py_rng.randint(0, 4096)
            buf = bytes(np_rng.integers(0, 256, size=length, dtype=np.uint8))

            c_bit = crc32c_bitwise(buf)
            c_slice8 = crc32c_slice8(buf)
            c_np = crc32c_numpy(buf)

            self.assertEqual(
                c_bit,
                c_slice8,
                f"Mismatch between bitwise ({hex(c_bit)}) and slice8 ({hex(c_slice8)})",
            )
            self.assertEqual(
                c_bit,
                c_np,
                f"Mismatch between bitwise ({hex(c_bit)}) and numpy ({hex(c_np)})",
            )

    def test_single_bit_flips_change_crc(self):
        """Every single-bit flip in a 4096-byte block changes the CRC."""
        np_rng = np.random.default_rng(2026)
        orig_block = np_rng.integers(0, 256, size=4096, dtype=np.uint8)
        orig_crc = crc32c_bitwise(orig_block.tobytes())

        num_bits = 4096 * 8
        mod_blocks = np.tile(orig_block, (num_bits, 1))
        bit_positions = np.arange(num_bits)
        byte_indices = bit_positions // 8
        bit_indices = bit_positions % 8
        mod_blocks[
            np.arange(num_bits), byte_indices
        ] ^= (1 << bit_indices).astype(np.uint8)

        mod_crcs = crc32c_numpy(mod_blocks)
        self.assertFalse(
            np.any(mod_crcs == orig_crc),
            "A single-bit flip failed to change the CRC32C value",
        )

    def test_burst_errors_change_crc(self):
        """Every burst error of length 1 to 32 bits at 64 positions changes the CRC."""
        np_rng = np.random.default_rng(2027)
        orig_block = np_rng.integers(0, 256, size=4096, dtype=np.uint8)
        orig_crc = crc32c_bitwise(orig_block.tobytes())

        num_bits = 4096 * 8
        positions = np.linspace(0, num_bits - 32, 64, dtype=int)

        mod_blocks_list = []
        for burst_len in range(1, 33):
            for pos in positions:
                if burst_len == 1:
                    burst_pattern = 1
                elif burst_len == 2:
                    burst_pattern = 3
                else:
                    mid = int(np_rng.integers(0, 1 << (burst_len - 2)))
                    burst_pattern = (1 << (burst_len - 1)) | (mid << 1) | 1

                mod_block = orig_block.copy()
                for b in range(burst_len):
                    if (burst_pattern >> b) & 1:
                        curr_bit = int(pos) + b
                        byte_idx = curr_bit // 8
                        bit_idx = curr_bit % 8
                        mod_block[byte_idx] ^= 1 << bit_idx
                mod_blocks_list.append(mod_block)

        mod_blocks_arr = np.array(mod_blocks_list, dtype=np.uint8)
        mod_crcs = crc32c_numpy(mod_blocks_arr)

        self.assertFalse(
            np.any(mod_crcs == orig_crc),
            "A burst error failed to change the CRC32C value",
        )

    def test_per_tile_crc_1920x1080_ram_frame(self):
        """Per-tile CRCs of a 1920 by 1080 RAM frame equal the bitwise reference on 50 sampled tiles."""
        np_rng = np.random.default_rng(1080)
        frame = np_rng.integers(0, 256, size=(1080, 1920), dtype=np.uint8)

        s = 32
        gh = 1080 // s
        gw = 1920 // s

        all_tile_crcs = crc32c_frame_tiles(frame, s=s)
        self.assertEqual(len(all_tile_crcs), gh * gw)

        # Extract tile blocks in RAM
        tiles_reshaped = (
            frame[: gh * s, : gw * s]
            .reshape(gh, s, gw, s)
            .swapaxes(1, 2)
            .reshape(gh * gw, s, s)
        )

        sample_indices = np_rng.choice(len(all_tile_crcs), size=50, replace=False)

        for idx in sample_indices:
            idx = int(idx)
            tile_bytes = tiles_reshaped[idx].tobytes()
            expected_crc = crc32c_bitwise(tile_bytes)
            actual_crc = int(all_tile_crcs[idx])

            self.assertEqual(
                actual_crc,
                expected_crc,
                f"Tile {idx} CRC mismatch: actual {hex(actual_crc)} vs expected {hex(expected_crc)}",
            )


if __name__ == "__main__":
    unittest.main()
