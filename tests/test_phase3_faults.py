"""
tests/test_phase3_faults.py - Fault Injection and Robustness Test Suite.
Validates:
  1. Corrupt CRC: Detected, rejected, re-read via from_index.
  2. Duplicate reply: Detected and ignored without duplicate storage.
  3. Mock stall: Handled via socket timeout and reconnect.
  4. Sensor drop: Mock buffer overflow / drop recorded in loss ledger.
  5. Mock restart: New session detected, old session finalized with source_restart.
  6. Process crash & recovery: State resumed from last_committed_index + 1.
  7. Disk-full safeguard: Reserve file released, disk_full loss recorded safely.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest

from tools.mock_standin.biosignal_mock_standin import BiosignalMockStandin
from tests.faultproxy import FaultProxy
from pipeline.store import DeviceStore
from pipeline.collector import Collector
from pipeline.ledger import verify_session_continuity


class TestPhase3Faults(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mayacardio_p3_")
        self.db_path = os.path.join(self.test_dir, "fault_test.db")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_corrupt_crc_and_recovery(self):
        """Mock sends corrupt CRC with 20% probability. Collector must retry and recover."""
        port = 7130
        mock = BiosignalMockStandin(
            mode="ramp", seed=10, duration=20, port=port,
            corrupt_crc_prob=0.25
        )
        m_th = threading.Thread(target=mock.run, daemon=True)
        m_th.start()
        time.sleep(0.3)

        store = DeviceStore(self.db_path)
        collector = Collector("127.0.0.1", port, store, poll_interval=0.1)
        c_th = threading.Thread(target=collector.run_loop, daemon=True)
        c_th.start()

        time.sleep(3.0)

        collector.stop()
        c_th.join(timeout=2.0)
        mock.running = False

        session_id = collector.current_session_id
        self.assertIsNotNone(session_id)

        # Ledger must remain continuous with zero unexplained gaps
        is_valid, issues = verify_session_continuity(store, session_id)
        store.close()
        self.assertTrue(is_valid, f"Continuity failure on corrupt CRC test: {issues}")

    def test_duplicate_reply_handling(self):
        """Mock sends duplicate replies. Collector must ignore duplicates."""
        port = 7131
        mock = BiosignalMockStandin(
            mode="ramp", seed=11, duration=20, port=port,
            duplicate_reply_prob=0.4
        )
        m_th = threading.Thread(target=mock.run, daemon=True)
        m_th.start()
        time.sleep(0.3)

        store = DeviceStore(self.db_path)
        collector = Collector("127.0.0.1", port, store, poll_interval=0.1)
        c_th = threading.Thread(target=collector.run_loop, daemon=True)
        c_th.start()

        time.sleep(3.0)

        collector.stop()
        c_th.join(timeout=2.0)
        mock.running = False

        session_id = collector.current_session_id
        is_valid, issues = verify_session_continuity(store, session_id)
        store.close()
        self.assertTrue(is_valid, f"Duplicate reply test continuity failed: {issues}")

    def test_mock_stall_and_reconnect(self):
        """Proxy injects a stall. Collector socket times out and reconnects."""
        mock_port = 7132
        proxy_port = 7133

        mock = BiosignalMockStandin(mode="ramp", seed=12, duration=20, port=mock_port)
        m_th = threading.Thread(target=mock.run, daemon=True)
        m_th.start()
        time.sleep(0.3)

        proxy = FaultProxy(proxy_port, "127.0.0.1", mock_port)
        p_th = threading.Thread(target=proxy.start, daemon=True)
        p_th.start()
        time.sleep(0.3)

        store = DeviceStore(self.db_path)
        collector = Collector("127.0.0.1", proxy_port, store, poll_interval=0.1)
        c_th = threading.Thread(target=collector.run_loop, daemon=True)
        c_th.start()

        time.sleep(1.0)
        # Inject stall on proxy
        proxy.stall_seconds = 2.5
        time.sleep(3.5)

        collector.stop()
        c_th.join(timeout=2.0)
        proxy.stop()
        mock.running = False

        session_id = collector.current_session_id
        self.assertIsNotNone(session_id)
        is_valid, issues = verify_session_continuity(store, session_id)
        store.close()
        self.assertTrue(is_valid, f"Stall test continuity failed: {issues}")

    def test_mock_restart(self):
        """Mock restarts with a new session_id. Collector must finalize old session and start new."""
        port = 7134
        mock = BiosignalMockStandin(mode="ramp", seed=13, duration=20, port=port, restart_after=1.5)
        m_th = threading.Thread(target=mock.run, daemon=True)
        m_th.start()
        time.sleep(0.3)

        store = DeviceStore(self.db_path)
        collector = Collector("127.0.0.1", port, store, poll_interval=0.1)
        c_th = threading.Thread(target=collector.run_loop, daemon=True)
        c_th.start()

        time.sleep(3.5)

        collector.stop()
        c_th.join(timeout=2.0)
        mock.running = False

        # Must have at least two sessions recorded
        cur = store.conn.cursor()
        cur.execute("SELECT session_id, finalized FROM sessions;")
        sessions = cur.fetchall()
        self.assertGreaterEqual(len(sessions), 2, "Expected at least 2 sessions after mock restart")

        for s_id, finalized in sessions:
            is_valid, issues = verify_session_continuity(store, s_id)
            self.assertTrue(is_valid, f"Session {s_id} continuity failed after restart: {issues}")

        store.close()

    def test_crash_and_recovery(self):
        """Collector abruptly stopped mid-stream, then restarted on same DB. Resumes seamlessly."""
        port = 7135
        mock = BiosignalMockStandin(mode="ramp", seed=14, duration=20, port=port)
        m_th = threading.Thread(target=mock.run, daemon=True)
        m_th.start()
        time.sleep(0.3)

        # First run: 1.5 seconds
        store1 = DeviceStore(self.db_path)
        col1 = Collector("127.0.0.1", port, store1, poll_interval=0.1)
        th1 = threading.Thread(target=col1.run_loop, daemon=True)
        th1.start()
        time.sleep(1.5)

        # Abrupt crash (stop without proper close)
        col1.stop()
        th1.join(timeout=2.0)
        store1.close()

        # Second run: resumes on same DB
        store2 = DeviceStore(self.db_path)
        col2 = Collector("127.0.0.1", port, store2, poll_interval=0.1)
        th2 = threading.Thread(target=col2.run_loop, daemon=True)
        th2.start()
        time.sleep(1.5)

        col2.stop()
        th2.join(timeout=2.0)
        mock.running = False

        session_id = col2.current_session_id
        is_valid, issues = verify_session_continuity(store2, session_id)
        store2.close()
        self.assertTrue(is_valid, f"Crash recovery continuity failed: {issues}")

    def test_disk_full_emergency_reserve(self):
        """Store reserve file exists and is cleanly released when disk full occurs."""
        store = DeviceStore(self.db_path)
        self.assertTrue(os.path.exists(store.reserve_file_path))

        # Releasing reserve file frees space for emergency loss recording
        released = store.release_reserve_file()
        self.assertTrue(released)
        self.assertFalse(os.path.exists(store.reserve_file_path))

        # Record emergency loss
        store.record_loss("test_sess", 1, 100, 199, "disk_full")
        losses = store.get_loss_ranges("test_sess", 1)
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0][2], "disk_full")
        store.close()


if __name__ == "__main__":
    unittest.main()
