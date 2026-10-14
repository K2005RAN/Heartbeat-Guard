"""
Integration test for Phase 2: Collector, Store, Ledger, and Stand-in Checker.
Tests that ramp mode data is read over TCP, stored in SQLite with WAL,
verified with pipeline verify, and passes check_ground_truth_standin.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tools.mock_standin.biosignal_mock_standin import BiosignalMockStandin
from tools.ground_truth_standin.check_ground_truth_standin import verify
from pipeline.store import DeviceStore
from pipeline.collector import Collector
from pipeline.ledger import verify_session_continuity


class TestPhase2CollectorStore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mayacardio_p2_")
        self.db_path = os.path.join(self.test_dir, "test_pipeline.db")
        self.gt_file = os.path.join(self.test_dir, "ground_truth.log")
        self.export_file = os.path.join(self.test_dir, "export.jsonl")
        self.port = 7122

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_collector_ramp_ground_truth(self):
        # 1. Start mock in ramp mode
        mock = BiosignalMockStandin(
            mode="ramp", seed=7, duration=30, port=self.port,
            ground_truth_file=self.gt_file
        )
        mock_thread = threading.Thread(target=mock.run, daemon=True)
        mock_thread.start()
        time.sleep(0.3)

        # 2. Run collector
        store = DeviceStore(self.db_path)
        collector = Collector("127.0.0.1", self.port, store, poll_interval=0.1)

        collector_thread = threading.Thread(target=collector.run_loop, daemon=True)
        collector_thread.start()

        # Let collector collect for ~3 seconds
        time.sleep(3.0)

        # Stop collector
        collector.stop()
        collector_thread.join(timeout=2.0)
        mock.running = False

        session_id = collector.current_session_id
        self.assertIsNotNone(session_id)

        # 3. Verify ledger continuity
        is_valid, issues = verify_session_continuity(store, session_id)
        self.assertTrue(is_valid, f"Continuity issues: {issues}")

        # 4. Check status
        saved, cloud, backlog = store.get_counts(session_id)
        self.assertGreater(saved, 1000)
        self.assertEqual(backlog, saved)

        # 5. Export to file
        cur = store.conn.cursor()
        from pipeline.store import unpack_24bit_samples
        import json
        with open(self.export_file, "w", encoding="utf-8") as f:
            for s in (1, 2):
                channels = 3 if s == 1 else 2
                cur.execute("""
                    SELECT first_index, count, data FROM blocks
                    WHERE session_id = ? AND sensor = ?
                    ORDER BY first_index ASC;
                """, (session_id, s))
                for first_idx, count, data_blob in cur.fetchall():
                    samples = unpack_24bit_samples(data_blob, channels)
                    for i, vals in enumerate(samples):
                        f.write(json.dumps({"sensor": s, "index": first_idx + i, "values": vals}) + "\n")
            for s in (1, 2):
                cur.execute("""
                    SELECT from_index, to_index, reason FROM losses
                    WHERE session_id = ? AND sensor = ?
                    ORDER BY from_index ASC;
                """, (session_id, s))
                for f_idx, t_idx, r in cur.fetchall():
                    f.write(json.dumps({"sensor": s, "from_index": f_idx, "to_index": t_idx, "reason": r}) + "\n")

        store.close()

        # 6. Verify against stand-in checker
        success = verify(self.export_file, self.gt_file)
        self.assertTrue(success, "Stand-in checker verification failed!")


if __name__ == "__main__":
    unittest.main()
