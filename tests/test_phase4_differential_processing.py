"""
tests/test_phase4_differential_processing.py - Differential & Property Tests.
Compares pipeline/processing.py against tests/reference_processing.py under:
- Normal continuous ramp data
- Synthetic ECG signal data (60 bpm, 90 bpm)
- Random sample dropouts and gaps
- Window boundary conditions (w < 4 vs w >= 4)
- Out-of-order / backfill arrival recomputations
"""

import random
import unittest
from tools.mock_standin.biosignal_mock_standin import SignalGenerator
from pipeline.processing import compute_window_result, round_1dp
from tests.reference_processing import ref_compute_window, ref_round_1dp


class TestPhase4Processing(unittest.TestCase):

    def test_rounding_edge_cases(self):
        """Validates ROUND_HALF_UP behavior on exact midpoint values."""
        self.assertEqual(round_1dp(1.25), 1.3)
        self.assertEqual(round_1dp(1.35), 1.4)
        self.assertEqual(round_1dp(1.24), 1.2)
        self.assertEqual(round_1dp(1.26), 1.3)
        self.assertEqual(round_1dp(None), None)

    def test_differential_ramp_windows(self):
        """Differential comparison of fast vs naive reference on ramp data."""
        gen = SignalGenerator("ramp", seed=1)
        s1 = {i: gen.get_sample(1, i) for i in range(2500)}
        s2 = {i: gen.get_sample(2, i) for i in range(2500)}

        for w in range(6):
            fast_res = compute_window_result(w, s1, s2, "online", "sess_test")
            ref_res = ref_compute_window(w, s1, s2, "online", "sess_test")

            for key in ["missing_1", "missing_2", "complete", "mean_1", "min_1", "max_1",
                        "mean_2", "min_2", "max_2", "magnitude", "heart_rate"]:
                self.assertEqual(
                    fast_res[key], ref_res[key],
                    f"Mismatch on window {w}, field {key}: fast={fast_res[key]} vs ref={ref_res[key]}"
                )

    def test_differential_signal_ecg_windows(self):
        """Differential comparison on ECG signal data across rate transitions."""
        gen = SignalGenerator("signal", seed=42)
        # Generate 8 seconds = 3200 samples
        s1 = {i: gen.get_sample(1, i) for i in range(3200)}
        s2 = {i: gen.get_sample(2, i) for i in range(3200)}

        for w in range(7):
            fast_res = compute_window_result(w, s1, s2, "online", "sess_ecg")
            ref_res = ref_compute_window(w, s1, s2, "online", "sess_ecg")

            self.assertEqual(fast_res["complete"], ref_res["complete"])
            self.assertEqual(fast_res["magnitude"], ref_res["magnitude"])
            self.assertEqual(
                fast_res["heart_rate"], ref_res["heart_rate"],
                f"Heart rate mismatch on window {w}: fast={fast_res['heart_rate']} vs ref={ref_res['heart_rate']}"
            )

        # For w=4,5,6 in 60 bpm segment, heart rate should be around 60.0 bpm
        for w in [4, 5, 6]:
            res = compute_window_result(w, s1, s2, "online", "sess_ecg")
            self.assertIsNotNone(res["heart_rate"])
            self.assertAlmostEqual(res["heart_rate"], 60.0, delta=1.0)

    def test_property_random_gaps_and_dropouts(self):
        """Property test: injecting random missing sample ranges."""
        rng = random.Random(999)
        gen = SignalGenerator("signal", seed=999)
        total_samples = 3600

        s1_full = {i: gen.get_sample(1, i) for i in range(total_samples)}
        s2_full = {i: gen.get_sample(2, i) for i in range(total_samples)}

        # Drop 10 random bursts of 10-50 samples
        s1_gapped = dict(s1_full)
        s2_gapped = dict(s2_full)
        for _ in range(10):
            burst_start = rng.randint(400, total_samples - 100)
            burst_len = rng.randint(5, 40)
            for k in range(burst_start, min(total_samples, burst_start + burst_len)):
                s1_gapped.pop(k, None)
                if rng.random() < 0.5:
                    s2_gapped.pop(k, None)

        for w in range(8):
            fast_res = compute_window_result(w, s1_gapped, s2_gapped, "offline", "sess_gap")
            ref_res = ref_compute_window(w, s1_gapped, s2_gapped, "offline", "sess_gap")

            self.assertEqual(fast_res["missing_1"], ref_res["missing_1"])
            self.assertEqual(fast_res["missing_2"], ref_res["missing_2"])
            self.assertEqual(fast_res["complete"], ref_res["complete"])
            self.assertEqual(fast_res["heart_rate"], ref_res["heart_rate"])

            # Invariant: If any sample in last 5 seconds was missing, heart_rate MUST be None
            if w >= 4:
                r_start = 400 * (w - 4)
                r_end = 400 * (w + 1) - 1
                has_gap = any(k not in s1_gapped for k in range(r_start, r_end + 1))
                if has_gap:
                    self.assertIsNone(fast_res["heart_rate"], f"Window {w} should have null heart_rate due to gap")

    def test_backfill_recomputation(self):
        """Property test: backfilling data triggers recomputation matching full history."""
        gen = SignalGenerator("signal", seed=77)
        s1 = {i: gen.get_sample(1, i) for i in range(3000)}
        s2 = {i: gen.get_sample(2, i) for i in range(3000)}

        # Simulate missing window 4 (samples 1600..1999)
        s1_partial = {i: v for i, v in s1.items() if not (1600 <= i < 2000)}
        s2_partial = {i: v for i, v in s2.items() if not (1600 <= i < 2000)}

        # Initial calculation with gap: window 4 is incomplete, window 5 heart rate is None
        res_w4_initial = compute_window_result(4, s1_partial, s2_partial, "online", "sess_backfill")
        self.assertFalse(res_w4_initial["complete"])
        self.assertEqual(res_w4_initial["missing_1"], 400)

        # Backfill arrival: add the missing samples
        for i in range(1600, 2000):
            s1_partial[i] = s1[i]
            s2_partial[i] = s2[i]

        # Recompute
        res_w4_final = compute_window_result(4, s1_partial, s2_partial, "online", "sess_backfill")
        ref_w4_final = ref_compute_window(4, s1, s2, "online", "sess_backfill")

        self.assertTrue(res_w4_final["complete"])
        self.assertEqual(res_w4_final["missing_1"], 0)
        self.assertEqual(res_w4_final["heart_rate"], ref_w4_final["heart_rate"])


if __name__ == "__main__":
    unittest.main()
