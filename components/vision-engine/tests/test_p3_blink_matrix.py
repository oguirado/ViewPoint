"""Task-specific acceptance matrix test for problems P3 and P4."""
import importlib
import json
import pkgutil
import unittest
import numpy as np

import eval.v2x as v2x

C_FLICKER = 4


def generate_test_sequences():
    """Generate in RAM per-tile uint64 hash sequences of 120 frames at 60 fps."""
    h_a = np.uint64(0x1111222233334444)
    h_b = np.uint64(0x9999AAAABBBBCCCC)
    h_c = np.uint64(0x5555666677778888)

    seqs = {}

    # Symmetric blink half periods 15, 30 and 45 frames
    seqs["sym_15"] = [h_a if (f // 15) % 2 == 0 else h_b for f in range(120)]
    seqs["sym_30"] = [h_a if (f // 30) % 2 == 0 else h_b for f in range(120)]
    seqs["sym_45"] = [h_a if (f // 45) % 2 == 0 else h_b for f in range(120)]

    # Asymmetric duty cycles 18 on 42 off, 42 on 18 off, and 10 on 50 off
    seq_asym_18_42 = []
    while len(seq_asym_18_42) < 120:
        seq_asym_18_42.extend([h_a] * 18)
        seq_asym_18_42.extend([h_b] * 42)
    seqs["asym_18_42"] = seq_asym_18_42[:120]

    seq_asym_42_18 = []
    while len(seq_asym_42_18) < 120:
        seq_asym_42_18.extend([h_a] * 42)
        seq_asym_42_18.extend([h_b] * 18)
    seqs["asym_42_18"] = seq_asym_42_18[:120]

    seq_asym_10_50 = []
    while len(seq_asym_10_50) < 120:
        seq_asym_10_50.extend([h_a] * 10)
        seq_asym_10_50.extend([h_b] * 50)
    seqs["asym_10_50"] = seq_asym_10_50[:120]

    # Jitter of plus or minus 1 frame with numpy seed 7
    np.random.seed(7)
    seq_jitter = []
    run_idx = 0
    while len(seq_jitter) < 120:
        j = int(np.random.choice([-1, 0, 1]))
        length = 15 + j
        val = h_a if run_idx % 2 == 0 else h_b
        seq_jitter.extend([val] * length)
        run_idx += 1
    seqs["jitter_15_seed7"] = seq_jitter[:120]

    # One-off change
    seqs["one_off_change"] = [h_a] * 60 + [h_b] * 60

    # Three-hash cycling
    hashes3 = [h_a, h_b, h_c]
    seqs["three_hash_cycle"] = [hashes3[(f // 15) % 3] for f in range(120)]

    # Slow fade made of unique hashes
    seqs["slow_fade"] = [np.uint64(1000 + f // 3) for f in range(120)]

    return seqs


# Explicit KNOWN_P3 table with observed declare or miss per variant and sequence
KNOWN_P3 = {
  "fl_autocorr": {
    "sym_15": {"declared": True, "first_frame": 45, "half_period": 15},
    "sym_30": {"declared": True, "first_frame": 90, "half_period": 30},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 45, "half_period": 16},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_blinkprior": {
    "sym_15": {"declared": True, "first_frame": 45, "half_period": 15},
    "sym_30": {"declared": True, "first_frame": 90, "half_period": 30},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 45, "half_period": 16},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_caretblink": {
    "sym_15": {"declared": True, "first_frame": 45, "half_period": 15},
    "sym_30": {"declared": True, "first_frame": 90, "half_period": 30},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 45, "half_period": 16},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_early3": {
    "sym_15": {"declared": True, "first_frame": 45, "half_period": 15},
    "sym_30": {"declared": True, "first_frame": 90, "half_period": 30},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 45, "half_period": 16},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_fadeguard": {
    "sym_15": {"declared": True, "first_frame": 45, "half_period": 15},
    "sym_30": {"declared": True, "first_frame": 90, "half_period": 30},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 45, "half_period": 15},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_hyst2": {
    "sym_15": {"declared": True, "first_frame": 45, "half_period": 15},
    "sym_30": {"declared": True, "first_frame": 90, "half_period": 30},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 45, "half_period": 15},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_majority": {
    "sym_15": {"declared": True, "first_frame": 75, "half_period": 15},
    "sym_30": {"declared": False, "first_frame": None, "half_period": None},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 74, "half_period": 15},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_pll": {
    "sym_15": {"declared": True, "first_frame": 60, "half_period": 15},
    "sym_30": {"declared": False, "first_frame": None, "half_period": None},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 74, "half_period": 15},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  },
  "fl_runhist": {
    "sym_15": {"declared": True, "first_frame": 60, "half_period": 15},
    "sym_30": {"declared": False, "first_frame": None, "half_period": None},
    "sym_45": {"declared": False, "first_frame": None, "half_period": None},
    "asym_18_42": {"declared": False, "first_frame": None, "half_period": None},
    "asym_42_18": {"declared": False, "first_frame": None, "half_period": None},
    "asym_10_50": {"declared": False, "first_frame": None, "half_period": None},
    "jitter_15_seed7": {"declared": True, "first_frame": 74, "half_period": 16},
    "one_off_change": {"declared": False, "first_frame": None, "half_period": None},
    "three_hash_cycle": {"declared": False, "first_frame": None, "half_period": None},
    "slow_fade": {"declared": False, "first_frame": None, "half_period": None}
  }
}


class TestP3BlinkMatrix(unittest.TestCase):
    def test_p3_blink_matrix(self):
        """Run the flicker matrix evaluation across all discovered eval/v2x fl_ variants."""
        discovered_modules = sorted([
            name for _, name, _ in pkgutil.iter_modules(v2x.__path__)
            if name.startswith("fl_")
        ])

        seqs = generate_test_sequences()
        matrix = {}

        for m_name in discovered_modules:
            mod = importlib.import_module(f"eval.v2x.{m_name}")
            self.assertTrue(hasattr(mod, "FlickerTracker"), f"Module {m_name} missing FlickerTracker")
            TrackerCls = getattr(mod, "FlickerTracker")

            matrix[m_name] = {}
            for seq_name, seq in seqs.items():
                tracker = TrackerCls(n_tiles=1, max_half=64)
                first_frame = None
                reported_hp = None

                for f, h in enumerate(seq):
                    states, hps = tracker.update([h])

                    # Assert README contracts
                    self.assertEqual(states.shape, (1,))
                    self.assertEqual(hps.shape, (1,))
                    st = int(states[0])
                    hp = int(hps[0])

                    if st == C_FLICKER:
                        self.assertGreater(hp, 0, f"FLICKER claim in {m_name}/{seq_name} frame {f} has hp=0")
                        self.assertLessEqual(hp, 64, f"FLICKER claim in {m_name}/{seq_name} frame {f} has hp > 64")
                        if first_frame is None:
                            first_frame = f
                            reported_hp = hp
                    else:
                        self.assertIn(st, (0, 2), f"Non-FLICKER state in {m_name}/{seq_name} frame {f} is {st}")

                matrix[m_name][seq_name] = {
                    "declared": first_frame is not None,
                    "first_frame": first_frame,
                    "half_period": reported_hp,
                }

        # Print the matrix as one JSON line for the host
        print(json.dumps(matrix))

        # Document specific problem findings required by specification:
        # fl_majority misses the 30-frame blink
        self.assertFalse(
            matrix["fl_majority"]["sym_30"]["declared"],
            "Documented finding: fl_majority misses the 30-frame blink"
        )
        # fl_early3 rejects asymmetric duty
        self.assertFalse(
            matrix["fl_early3"]["asym_18_42"]["declared"],
            "Documented finding: fl_early3 rejects asymmetric duty 18/42"
        )
        self.assertFalse(
            matrix["fl_early3"]["asym_42_18"]["declared"],
            "Documented finding: fl_early3 rejects asymmetric duty 42/18"
        )

        # Assert full matrix matches KNOWN_P3
        for m_name in KNOWN_P3:
            self.assertIn(m_name, discovered_modules, f"Pinned module {m_name} disappeared")
        new = sorted(set(discovered_modules) - set(KNOWN_P3))
        if new:   # new variants are reported, not failed; pinned after review
            print(f"P3 matrix: new modules not yet pinned: {new}: { {m: matrix[m] for m in new} }")
        for m_name in discovered_modules:
            if m_name not in KNOWN_P3:
                continue
            for seq_name in seqs:
                self.assertEqual(
                    matrix[m_name][seq_name],
                    KNOWN_P3[m_name][seq_name],
                    f"Mismatch for {m_name} on {seq_name}"
                )


if __name__ == "__main__":
    unittest.main()
