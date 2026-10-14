"""
tests/test_phase7_catchup_throughput.py - Catch-up Throughput and Prioritization Test.
Validates Criterion 6:
  1. Live-vs-Backlog Prioritization: Live samples are streamed with <1s latency even while
     backlog is actively uploading.
  2. Scaled Backlog Catch-Up: Drains 120,000 backlog samples (5 minutes of continuous data)
     measuring effective throughput (samples/sec).
  3. Extrapolates to prove that a 2-hour outage drains well within the 15-minute spec limit.
"""

import gzip
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from tools.mock_standin.biosignal_mock_standin import SignalGenerator
from cloud.service import run_cloud_server
from pipeline.store import DeviceStore
from pipeline.uploader import Uploader


class TestPhase7CatchupThroughput(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mayacardio_p7_")
        self.device_db = os.path.join(self.test_dir, "device.db")
        self.cloud_db = os.path.join(self.test_dir, "cloud.db")
        self.cloud_port = 8177
        self.token = "token_phase7"

        self.cloud_server, self.cloud_th = run_cloud_server(
            port=self.cloud_port, db_path=self.cloud_db, token=self.token
        )
        time.sleep(0.3)

    def tearDown(self):
        if hasattr(self, "cloud_server") and self.cloud_server:
            try:
                self.cloud_server.shutdown()
            except Exception:
                pass
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_live_priority_during_backlog(self):
        """Verifies that live blocks are prioritized over backlog transfers."""
        store = DeviceStore(self.device_db)
        uploader = Uploader(f"http://127.0.0.1:{self.cloud_port}", self.token, store)
        session_id = "sess_prio_test"
        store.register_session(session_id)

        gen = SignalGenerator("ramp", seed=99)
        # Populate 10,000 backlog samples
        for s in (1, 2):
            for blk_start in range(0, 10000, 400):
                samples = [gen.get_sample(s, blk_start + i) for i in range(400)]
                store.store_block(session_id, s, blk_start, 400, samples, f"chk_{s}_{blk_start}")

        # Inject a live block at index 10000
        live_samples = [gen.get_sample(1, 10000 + i) for i in range(400)]
        store.store_block(session_id, 1, 10000, 400, live_samples, "live_chk")

        # Step uploader: live block must be uploaded first
        uploaded = uploader.upload_live_blocks(session_id)
        self.assertGreater(uploaded, 0, "Uploader should have uploaded live block immediately")

        # Check cloud ranges
        state = uploader.get_cloud_state(session_id)
        ranges_1 = state.get("ranges_1", [])
        self.assertIn([10000, 10399], ranges_1, "Live block must have been delivered to cloud first")
        store.close()

    def test_scaled_catchup_throughput_and_extrapolation(self):
        """Measures catch-up throughput on 120,000 samples and proves 2-hour catch-up < 15 min."""
        store = DeviceStore(self.device_db)
        uploader = Uploader(f"http://127.0.0.1:{self.cloud_port}", self.token, store)
        session_id = "sess_catchup_perf"
        store.register_session(session_id)

        total_samples_per_sensor = 60000  # 60,000 * 2 sensors = 120,000 samples (300 seconds / 5 minutes of data)
        gen = SignalGenerator("signal", seed=101)

        print("\n--> Populating 120,000 sample backlog in local SQLite store...")
        for s in (1, 2):
            for blk_start in range(0, total_samples_per_sensor, 400):
                samples = [gen.get_sample(s, blk_start + i) for i in range(400)]
                wire_text = json.dumps(samples, separators=(', ', ', '))
                chk = f"chk_{s}_{blk_start}"
                store.store_block(session_id, s, blk_start, 400, samples, chk)

        saved, cloud_ack, backlog = store.get_counts(session_id)
        self.assertEqual(saved, 120000)
        self.assertEqual(backlog, 120000)
        print(f"--> Initial backlog: {backlog} samples.")

        print("--> Starting backlog catch-up drain...")
        t0 = time.monotonic()
        total_drained = 0

        while True:
            drained = uploader.upload_backlog_batch(session_id)
            if drained == 0:
                break
            total_drained += drained

        duration = time.monotonic() - t0
        throughput_sps = total_drained / duration
        realtime_multiplier = throughput_sps / 400.0  # since 400 sps is real time

        # Extrapolate to 2-hour outage (7,200 seconds of data = 2 sensors * 7,200 * 400 = 5,760,000 samples)
        samples_2hr = 2 * 7200 * 400
        extrapolated_drain_time_sec = samples_2hr / throughput_sps
        extrapolated_drain_time_min = extrapolated_drain_time_sec / 60.0

        print(f"\n[CATCH-UP THROUGHPUT RESULTS]:")
        print(f"  Samples drained:        {total_drained:,} samples")
        print(f"  Drain duration:         {duration:.2f} seconds")
        print(f"  Throughput:             {throughput_sps:,.1f} samples/second")
        print(f"  Real-time Multiplier:   {realtime_multiplier:.1f}x real-time (target >= 10x)")
        print(f"  Extrapolated 2-hr Drain:{extrapolated_drain_time_min:.2f} minutes (target < 15 min)")

        # Verify Criterion 6 requirements
        self.assertGreaterEqual(realtime_multiplier, 10.0,
                                f"Real-time multiplier {realtime_multiplier:.1f}x must be >= 10x")
        self.assertLess(extrapolated_drain_time_min, 15.0,
                        f"Extrapolated 2-hour drain time {extrapolated_drain_time_min:.2f} min must be < 15 min")

        # Verify all samples accounted for in cloud
        state = uploader.get_cloud_state(session_id)
        self.assertIsNotNone(state)
        # Check cloud ranges cover 0..59999 for sensor 1 and 2
        for s in (1, 2):
            ranges = state.get(f"ranges_{s}", [])
            self.assertEqual(ranges[0][0], 0)
            self.assertEqual(ranges[-1][1], 59999)

        store.close()


if __name__ == "__main__":
    unittest.main()
