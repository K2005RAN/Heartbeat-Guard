"""
pipeline/local_processor.py - Bedside Local Offline Processor for MayaCardio.
Python 3.11+ standard library only.

Features:
- Active strictly during OFFLINE mode (pauses during online mode).
- Uses identical processing logic from pipeline/processing.py.
- Computes Section 3 window results and commits them to the local SQLite results table
  with mode: 'offline'.
- Recomputes windows if late/backfilled data arrives.
"""

import json
import sys
import threading
import time
from typing import Dict, List, Optional

from pipeline.store import DeviceStore, unpack_24bit_samples
from pipeline.processing import compute_window_result


class LocalProcessor:
    def __init__(self, store: DeviceStore, poll_interval: float = 0.5):
        self.store = store
        self.poll_interval = poll_interval
        self.is_offline = True
        self.running = False
        self.last_processed_window: Dict[str, int] = {}

    def set_offline_state(self, is_offline: bool):
        self.is_offline = is_offline
        sys.stderr.write(f"[LOCAL PROCESSOR] Offline mode = {is_offline}\n")

    def _get_samples_map(self, session_id: str, sensor: int,
                           start_idx: int, end_idx: int) -> Dict[int, List[int]]:
        with self.store.lock:
            cur = self.store.conn.cursor()
            cur.execute("""
                SELECT first_index, count, data FROM blocks
                WHERE session_id = ? AND sensor = ?
                  AND (first_index + count - 1) >= ?
                  AND first_index <= ?
                ORDER BY first_index ASC;
            """, (session_id, sensor, start_idx, end_idx))
            res = {}
            channels = 3 if sensor == 1 else 2
            for f_idx, count, blob in cur.fetchall():
                samples = unpack_24bit_samples(blob, channels)
                for i, vals in enumerate(samples):
                    idx = f_idx + i
                    if start_idx <= idx <= end_idx:
                        res[idx] = vals
            return res

    def step(self, session_id: str):
        if not self.is_offline:
            return

        # Determine highest stored index for sensor 1 and 2
        max_s1 = self.store.get_max_stored_index(session_id, 1)
        max_s2 = self.store.get_max_stored_index(session_id, 2)
        if max_s1 < 400 or max_s2 < 400:
            return

        max_sample = min(max_s1, max_s2)
        max_complete_window = (max_sample // 400) - 1

        last_win = self.last_processed_window.get(session_id, -1)
        if max_complete_window <= last_win:
            return

        for w in range(last_win + 1, max_complete_window + 1):
            lookback_start = max(0, 400 * (w - 4))
            lookahead_end = 400 * (w + 1)

            s1_map = self._get_samples_map(session_id, 1, lookback_start, lookahead_end)
            s2_map = self._get_samples_map(session_id, 2, 400 * w, 400 * w + 399)

            res = compute_window_result(w, s1_map, s2_map, mode="offline", session_id=session_id)
            self.store.save_result(
                session_id, w, "offline",
                res["missing_1"], res["missing_2"], res["complete"], json.dumps(res)
            )
            self.last_processed_window[session_id] = w

    def run_loop(self):
        self.running = True
        sys.stderr.write("[LOCAL PROCESSOR] Started processor loop\n")
        while self.running:
            session_id = self.store.get_latest_session()
            if session_id and self.is_offline:
                try:
                    self.step(session_id)
                except Exception as e:
                    sys.stderr.write(f"[LOCAL PROCESSOR] Error in step: {e}\n")
            time.sleep(self.poll_interval)
        sys.stderr.write("[LOCAL PROCESSOR] Processor loop stopped\n")

    def stop(self):
        self.running = False
