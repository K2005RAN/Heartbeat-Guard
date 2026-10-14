"""
tests/test_phase6_mode_switching.py - Automatic Mode Switching and Offline Processing Test.
Validates Criterion 5:
  1. Detects network cut and transitions to OFFLINE mode within 10 seconds.
  2. Local processor computes window results on device during offline mode.
  3. Detects network restoration and transitions back to ONLINE mode within 10 seconds.
  4. No sample lost or duplicated across the mode switch.
  5. Backlog and offline results uploaded cleanly to cloud.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from tools.mock_standin.biosignal_mock_standin import BiosignalMockStandin
from tools.ground_truth_standin.check_ground_truth_standin import verify
from cloud.service import run_cloud_server
from pipeline.store import DeviceStore
from pipeline.collector import Collector
from pipeline.uploader import Uploader
from pipeline.netmon import NetworkMonitor
from pipeline.local_processor import LocalProcessor
from pipeline.ledger import verify_session_continuity


class TestPhase6ModeSwitching(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mayacardio_p6_")
        self.device_db = os.path.join(self.test_dir, "device.db")
        self.cloud_db = os.path.join(self.test_dir, "cloud.db")
        self.gt_file = os.path.join(self.test_dir, "ground_truth.log")
        self.cloud_port = 8166
        self.mock_port = 7166
        self.token = "token_phase6"

    def tearDown(self):
        if hasattr(self, "cloud_server") and self.cloud_server:
            try:
                self.cloud_server.shutdown()
            except Exception:
                pass
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_automatic_mode_switching_and_zero_loss(self):
        # 1. Start Cloud Server
        self.cloud_server, self.cloud_th = run_cloud_server(
            port=self.cloud_port, db_path=self.cloud_db, token=self.token
        )

        # 2. Start Mock
        mock = BiosignalMockStandin(
            mode="signal", seed=17, duration=30, port=self.mock_port,
            ground_truth_file=self.gt_file
        )
        m_th = threading.Thread(target=mock.run, daemon=True)
        m_th.start()
        time.sleep(0.3)

        store = DeviceStore(self.device_db)
        collector = Collector("127.0.0.1", self.mock_port, store, poll_interval=0.1)
        uploader = Uploader(f"http://127.0.0.1:{self.cloud_port}", self.token, store)
        local_proc = LocalProcessor(store, poll_interval=0.2)

        def on_mode_change(new_mode):
            local_proc.set_offline_state(new_mode == "offline")

        netmon = NetworkMonitor(f"http://127.0.0.1:{self.cloud_port}", on_mode_change=on_mode_change)

        c_th = threading.Thread(target=collector.run_loop, daemon=True)
        u_th = threading.Thread(target=uploader.run_loop, daemon=True)
        lp_th = threading.Thread(target=local_proc.run_loop, daemon=True)
        nm_th = threading.Thread(target=netmon.run_loop, daemon=True)

        c_th.start()
        u_th.start()
        lp_th.start()
        nm_th.start()

        # Step A: Run in online mode for 4 seconds
        time.sleep(4.0)
        self.assertEqual(netmon.get_mode(), "online", "Expected netmon to be online")

        # Step B: Inject network cut (shutdown cloud server)
        print("\n[TEST] Simulating network cut...")
        t_cut = time.monotonic()
        self.cloud_server.shutdown()
        self.cloud_server = None

        # Wait for netmon to switch to offline
        switched_to_offline = False
        while time.monotonic() - t_cut < 10.0:
            if netmon.get_mode() == "offline":
                switched_to_offline = True
                break
            time.sleep(0.2)

        t_offline_detected = time.monotonic() - t_cut
        print(f"[TEST] Switched to offline in {t_offline_detected:.2f}s (spec: <10s)")
        self.assertTrue(switched_to_offline, f"Offline switch took {t_offline_detected:.2f}s, exceeded 10s!")
        self.assertLessEqual(t_offline_detected, 10.0)

        # Step C: Stay offline for 3 seconds while data continues collecting
        time.sleep(3.0)
        session_id = collector.current_session_id
        self.assertIsNotNone(session_id)

        # Verify that local processor computed offline results
        local_results = store.get_results(session_id)
        self.assertGreater(len(local_results), 0, "Local processor should have produced offline results")
        print(f"[TEST] Local device computed {len(local_results)} offline results during outage")

        # Step D: Restore network (re-launch cloud server)
        print("[TEST] Simulating network restoration...")
        t_restore = time.monotonic()
        self.cloud_server, self.cloud_th = run_cloud_server(
            port=self.cloud_port, db_path=self.cloud_db, token=self.token
        )

        # Wait for netmon to switch back to online
        switched_to_online = False
        while time.monotonic() - t_restore < 10.0:
            if netmon.get_mode() == "online":
                switched_to_online = True
                break
            time.sleep(0.2)

        t_online_detected = time.monotonic() - t_restore
        print(f"[TEST] Switched back to online in {t_online_detected:.2f}s (spec: <10s)")
        self.assertTrue(switched_to_online, f"Online switch took {t_online_detected:.2f}s, exceeded 10s!")
        self.assertLessEqual(t_online_detected, 10.0)

        # Step E: Allow catch-up drain
        time.sleep(3.0)

        # Stop mock and drain
        mock.running = False
        time.sleep(0.3)
        collector.stop()
        c_th.join(timeout=2.0)

        # Final catch-up flush
        uploader.upload_live_blocks(session_id)
        uploader.upload_backlog_batch(session_id)

        uploader.stop()
        local_proc.stop()
        netmon.stop()

        # Step F: Verify ledger continuity on device
        is_valid, issues = verify_session_continuity(store, session_id)
        self.assertTrue(is_valid, f"Ledger continuity failed: {issues}")

        # Step G: Verify against stand-in checker from cloud export
        import urllib.request
        cloud_export_file = os.path.join(self.test_dir, "cloud_export.jsonl")
        export_url = f"http://127.0.0.1:{self.cloud_port}/export?session={session_id}"
        with urllib.request.urlopen(export_url) as resp, open(cloud_export_file, "wb") as f:
            f.write(resp.read())

        success = verify(cloud_export_file, self.gt_file)
        self.assertTrue(success, "Ground truth verification failed after mode switch!")


if __name__ == "__main__":
    unittest.main()
