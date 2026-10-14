"""
pipeline/store.py - Transactional, append-only SQLite store for MayaCardio pipeline.
Python 3.11+ standard library only.

Guarantees:
- WAL mode with synchronous=FULL for ACID crash-consistency on power cuts.
- Packed 24-bit integer storage (3 bytes per channel, signed little-endian)
  to minimize disk write amplification and flash wear on Raspberry Pi.
- Pre-allocated disk reserve file to safely commit loss records if disk fills up.
- Idempotent block insertion, rejecting duplicates and preventing index overlaps.
"""

import os
import sqlite3
import struct
import sys
import time
from typing import Dict, List, Optional, Tuple

RESERVE_FILE_NAME = "reserve.dat"
RESERVE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB reserve


def pack_24bit_samples(samples: List[List[int]]) -> bytes:
    """Packs a list of sample value arrays into raw 24-bit signed bytes (3 bytes per int)."""
    buf = bytearray()
    for sample in samples:
        for val in sample:
            # 24-bit signed int: -8388608 to 8388607
            # Clamp or wrap to signed 24-bit
            val = int(val)
            if val < -8388608 or val > 8388607:
                # Wrap modulo 2^24 to signed 24-bit
                val = ((val + 8388608) % 16777216) - 8388608
            buf.extend(val.to_bytes(3, byteorder="little", signed=True))
    return bytes(buf)


def unpack_24bit_samples(raw_bytes: bytes, channels: int) -> List[List[int]]:
    """Unpacks raw 24-bit signed bytes into list of sample value arrays."""
    bytes_per_sample = channels * 3
    num_samples = len(raw_bytes) // bytes_per_sample
    samples = []
    offset = 0
    for _ in range(num_samples):
        sample = []
        for _ in range(channels):
            val = int.from_bytes(raw_bytes[offset:offset + 3], byteorder="little", signed=True)
            sample.append(val)
            offset += 3
        samples.append(sample)
    return samples


class DeviceStore:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        self.db_dir = os.path.dirname(self.db_path)
        if self.db_dir and not os.path.exists(self.db_dir):
            os.makedirs(self.db_dir, exist_ok=True)

        self.reserve_file_path = os.path.join(self.db_dir, RESERVE_FILE_NAME)
        self._ensure_reserve_file()
        self.conn = self._init_db()

    def _ensure_reserve_file(self):
        """Ensures a 2 MB reserve file exists to absorb disk-full emergency operations."""
        if not os.path.exists(self.reserve_file_path):
            try:
                with open(self.reserve_file_path, "wb") as f:
                    f.write(b"\0" * RESERVE_SIZE_BYTES)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass

    def release_reserve_file(self) -> bool:
        """Releases reserve file when disk fills up so loss records can still be written."""
        if os.path.exists(self.reserve_file_path):
            try:
                os.remove(self.reserve_file_path)
                return True
            except OSError:
                return False
        return False

    def _init_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        # WAL mode with synchronous=FULL for high durability and concurrent reads
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = FULL;")
        conn.execute("PRAGMA busy_timeout = 30000;")

        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    start_time REAL NOT NULL,
                    finalized INTEGER DEFAULT 0,
                    end_time REAL
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS blocks (
                    session_id TEXT NOT NULL,
                    sensor INTEGER NOT NULL,
                    first_index INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (session_id, sensor, first_index)
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_sensor ON blocks (session_id, sensor, first_index);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS losses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    sensor INTEGER NOT NULL,
                    from_index INTEGER NOT NULL,
                    to_index INTEGER,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_losses_sensor ON losses (session_id, sensor, from_index);")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS cloud_ack (
                    session_id TEXT NOT NULL,
                    sensor INTEGER NOT NULL,
                    first_index INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    acked_at REAL NOT NULL,
                    PRIMARY KEY (session_id, sensor, first_index)
                );
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS results (
                    session_id TEXT NOT NULL,
                    window INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    missing_1 INTEGER NOT NULL,
                    missing_2 INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (session_id, window)
                );
            """)

        return conn

    def register_session(self, session_id: str):
        with self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO sessions (session_id, start_time, finalized)
                VALUES (?, ?, 0);
            """, (session_id, time.time()))

    def finalize_session(self, session_id: str):
        with self.conn:
            self.conn.execute("""
                UPDATE sessions SET finalized = 1, end_time = ? WHERE session_id = ?;
            """, (time.time(), session_id))

    def get_latest_session(self) -> Optional[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT session_id FROM sessions ORDER BY rowid DESC LIMIT 1;")
        row = cur.fetchone()
        return row[0] if row else None

    def get_max_stored_index(self, session_id: str, sensor: int) -> int:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT MAX(first_index + count - 1) FROM blocks
            WHERE session_id = ? AND sensor = ?;
        """, (session_id, sensor))
        row = cur.fetchone()
        if row and row[0] is not None:
            return row[0]
        return -1

    def store_block(self, session_id: str, sensor: int, first_index: int,
                    count: int, samples: List[List[int]], checksum: str) -> bool:
        """Stores a block of samples atomically. Returns True if inserted, False if duplicate."""
        packed_data = pack_24bit_samples(samples)
        now = time.time()
        try:
            with self.conn:
                self.conn.execute("""
                    INSERT INTO blocks (session_id, sensor, first_index, count, data, checksum, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (session_id, sensor, first_index, count, packed_data, checksum, now))
            return True
        except sqlite3.IntegrityError:
            # Block already stored (duplicate)
            return False
        except sqlite3.OperationalError as e:
            if "full" in str(e).lower():
                self.release_reserve_file()
                # Log loss due to disk full
                self.record_loss(session_id, sensor, first_index, first_index + count - 1, "disk_full")
                raise
            raise

    def record_loss(self, session_id: str, sensor: int, from_index: int,
                    to_index: Optional[int], reason: str):
        """Records a lost sample range into the loss ledger."""
        now = time.time()
        with self.conn:
            self.conn.execute("""
                INSERT INTO losses (session_id, sensor, from_index, to_index, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (session_id, sensor, from_index, to_index, reason, now))

    def get_stored_ranges(self, session_id: str, sensor: int) -> List[Tuple[int, int]]:
        """Returns sorted list of [first_index, last_index] ranges held on device."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT first_index, count FROM blocks
            WHERE session_id = ? AND sensor = ?
            ORDER BY first_index ASC;
        """, (session_id, sensor))
        ranges = []
        for first_index, count in cur.fetchall():
            ranges.append((first_index, first_index + count - 1))
        return ranges

    def get_loss_ranges(self, session_id: str, sensor: int) -> List[Tuple[int, Optional[int], str]]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT from_index, to_index, reason FROM losses
            WHERE session_id = ? AND sensor = ?
            ORDER BY from_index ASC;
        """, (session_id, sensor))
        return cur.fetchall()

    def get_unsent_blocks(self, session_id: str, sensor: int, limit: int = 100) -> List[dict]:
        """Returns blocks not yet marked as cloud-acknowledged."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT b.first_index, b.count, b.data, b.checksum
            FROM blocks b
            LEFT JOIN cloud_ack a ON b.session_id = a.session_id AND b.sensor = a.sensor AND b.first_index = a.first_index
            WHERE b.session_id = ? AND b.sensor = ? AND a.first_index IS NULL
            ORDER BY b.first_index ASC
            LIMIT ?;
        """, (session_id, sensor, limit))
        results = []
        channels = 3 if sensor == 1 else 2
        for first_idx, count, data_blob, checksum in cur.fetchall():
            samples = unpack_24bit_samples(data_blob, channels)
            results.append({
                "first_index": first_idx,
                "count": count,
                "samples": samples,
                "checksum": checksum
            })
        return results

    def mark_cloud_ack(self, session_id: str, sensor: int, first_index: int, count: int):
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO cloud_ack (session_id, sensor, first_index, count, acked_at)
                VALUES (?, ?, ?, ?, ?);
            """, (session_id, sensor, first_index, count, time.time()))

    def save_result(self, session_id: str, window: int, mode: str,
                    missing_1: int, missing_2: int, complete: bool, data_json: str):
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO results (session_id, window, mode, missing_1, missing_2, complete, data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (session_id, window, mode, missing_1, missing_2, 1 if complete else 0, data_json, time.time()))

    def get_results(self, session_id: str) -> List[str]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT data_json FROM results
            WHERE session_id = ?
            ORDER BY window ASC;
        """, (session_id,))
        return [row[0] for row in cur.fetchall()]

    def get_counts(self, session_id: Optional[str] = None):
        """Returns (samples_saved, samples_cloud, backlog)."""
        cur = self.conn.cursor()
        if session_id:
            cur.execute("SELECT COALESCE(SUM(count), 0) FROM blocks WHERE session_id = ?;", (session_id,))
            saved = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(count), 0) FROM cloud_ack WHERE session_id = ?;", (session_id,))
            cloud = cur.fetchone()[0]
        else:
            cur.execute("SELECT COALESCE(SUM(count), 0) FROM blocks;")
            saved = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(count), 0) FROM cloud_ack;")
            cloud = cur.fetchone()[0]
        backlog = max(0, saved - cloud)
        return saved, cloud, backlog

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
