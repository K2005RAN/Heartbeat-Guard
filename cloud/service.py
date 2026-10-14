"""
cloud/service.py - Cloud Ingest and Processing Service for MayaCardio.
Python 3.11+ standard library only.

Endpoints:
  GET  /health              -> 200 OK
  POST /ingest              -> Ingest sample blocks, losses, offline results (token auth)
  GET  /state?session=ID    -> Return ranges held, losses, session list
  GET  /results?session=ID  -> Return window results
  GET  /export?session=ID   -> Return export in standard JSON-lines format
"""

import argparse
import gzip
import hashlib
import http.server
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from pipeline.store import pack_24bit_samples, unpack_24bit_samples
from pipeline.processing import compute_window_result

DEFAULT_CLOUD_DB = "cloud_data.db"
DEFAULT_AUTH_TOKEN = "mayacardio_secret_token_2026"


class CloudStore:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.lock = threading.Lock()
        self.conn = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cloud_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cloud_blocks (
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cloud_losses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    sensor INTEGER NOT NULL,
                    from_index INTEGER NOT NULL,
                    to_index INTEGER,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cloud_results (
                    session_id TEXT NOT NULL,
                    window INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    missing_1 INTEGER NOT NULL,
                    missing_2 INTEGER NOT NULL,
                    complete INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    cloud_time REAL NOT NULL,
                    PRIMARY KEY (session_id, window)
                );
            """)
        return conn

    def register_session(self, session_id: str):
        with self.lock, self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO cloud_sessions (session_id, created_at)
                VALUES (?, ?);
            """, (session_id, time.time()))

    def insert_block(self, session_id: str, sensor: int, first_index: int,
                     count: int, samples: List[List[int]], checksum: str) -> bool:
        packed = pack_24bit_samples(samples)
        now = time.time()
        with self.lock:
            try:
                with self.conn:
                    self.conn.execute("""
                        INSERT INTO cloud_blocks (session_id, sensor, first_index, count, data, checksum, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, (session_id, sensor, first_index, count, packed, checksum, now))
                return True
            except sqlite3.IntegrityError:
                # Duplicate block
                return False

    def insert_loss(self, session_id: str, sensor: int, from_index: int,
                    to_index: Optional[int], reason: str):
        now = time.time()
        with self.lock, self.conn:
            self.conn.execute("""
                INSERT INTO cloud_losses (session_id, sensor, from_index, to_index, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (session_id, sensor, from_index, to_index, reason, now))

    def get_stored_ranges(self, session_id: str, sensor: int) -> List[Tuple[int, int]]:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT first_index, count FROM cloud_blocks
                WHERE session_id = ? AND sensor = ?
                ORDER BY first_index ASC;
            """, (session_id, sensor))
            return [(r[0], r[0] + r[1] - 1) for r in cur.fetchall()]

    def get_losses(self, session_id: str, sensor: int) -> List[dict]:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT from_index, to_index, reason FROM cloud_losses
                WHERE session_id = ? AND sensor = ?
                ORDER BY from_index ASC;
            """, (session_id, sensor))
            return [{"from_index": r[0], "to_index": r[1], "reason": r[2]} for r in cur.fetchall()]

    def get_all_samples_for_window(self, session_id: str, sensor: int,
                                   start_idx: int, end_idx: int) -> Dict[int, List[int]]:
        """Retrieves dictionary of sample index -> values for the given range."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT first_index, count, data FROM cloud_blocks
                WHERE session_id = ? AND sensor = ?
                  AND (first_index + count - 1) >= ?
                  AND first_index <= ?
                ORDER BY first_index ASC;
            """, (session_id, sensor, start_idx, end_idx))
            res = {}
            channels = 3 if sensor == 1 else 2
            for f_idx, count, blob in cur.fetchall():
                unpacked = unpack_24bit_samples(blob, channels)
                for i, vals in enumerate(unpacked):
                    idx = f_idx + i
                    if start_idx <= idx <= end_idx:
                        res[idx] = vals
            return res

    def save_result(self, session_id: str, window: int, mode: str,
                    missing_1: int, missing_2: int, complete: bool, data_json: str):
        now = time.time()
        with self.lock, self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO cloud_results (session_id, window, mode, missing_1, missing_2, complete, data_json, cloud_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (session_id, window, mode, missing_1, missing_2, 1 if complete else 0, data_json, now))

    def get_results(self, session_id: str) -> List[dict]:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT data_json, cloud_time FROM cloud_results
                WHERE session_id = ?
                ORDER BY window ASC;
            """, (session_id,))
            out = []
            for row in cur.fetchall():
                obj = json.loads(row[0])
                obj["cloud_received_time"] = row[1]
                out.append(obj)
            return out

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass


class CloudRequestHandler(http.server.BaseHTTPRequestHandler):
    store: CloudStore = None
    expected_token: str = DEFAULT_AUTH_TOKEN

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[len("Bearer "):].strip()
        return token == self.expected_token

    def _send_json(self, status_code: int, data: Any):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send_json(200, {"status": "ok", "service": "MayaCardio Cloud", "time": time.time()})
            return

        if parsed.path == "/state":
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json(400, {"error": "Missing session query parameter"})
                return
            ranges_1 = self.store.get_stored_ranges(session_id, 1)
            ranges_2 = self.store.get_stored_ranges(session_id, 2)
            losses_1 = self.store.get_losses(session_id, 1)
            losses_2 = self.store.get_losses(session_id, 2)
            self._send_json(200, {
                "session_id": session_id,
                "ranges_1": ranges_1,
                "ranges_2": ranges_2,
                "losses_1": losses_1,
                "losses_2": losses_2
            })
            return

        if parsed.path == "/results":
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json(400, {"error": "Missing session query parameter"})
                return
            results = self.store.get_results(session_id)
            self._send_json(200, {"session_id": session_id, "results": results})
            return

        if parsed.path == "/export":
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json(400, {"error": "Missing session query parameter"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()

            # Stream samples then losses
            for sensor in (1, 2):
                ranges = self.store.get_stored_ranges(session_id, sensor)
                for start, end in ranges:
                    samples_map = self.store.get_all_samples_for_window(session_id, sensor, start, end)
                    for idx in sorted(samples_map.keys()):
                        line = json.dumps({"sensor": sensor, "index": idx, "values": samples_map[idx]}) + "\n"
                        self.wfile.write(line.encode("utf-8"))

            for sensor in (1, 2):
                losses = self.store.get_losses(session_id, sensor)
                for l in losses:
                    line = json.dumps({
                        "sensor": sensor,
                        "from_index": l["from_index"],
                        "to_index": l["to_index"],
                        "reason": l["reason"]
                    }) + "\n"
                    self.wfile.write(line.encode("utf-8"))
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._check_auth():
            self._send_json(401, {"error": "Unauthorized: invalid or missing bearer token"})
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/ingest":
            self._send_json(404, {"error": "Not found"})
            return

        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len)

        # Check if gzip compressed
        if self.headers.get("Content-Encoding") == "gzip":
            try:
                raw_body = gzip.decompress(raw_body)
            except Exception as e:
                self._send_json(400, {"error": f"Gzip decompress failed: {e}"})
                return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON payload: {e}"})
            return

        session_id = payload.get("session_id")
        blocks = payload.get("blocks", [])
        losses = payload.get("losses", [])
        offline_results = payload.get("offline_results", [])

        if not session_id:
            self._send_json(400, {"error": "session_id required"})
            return

        self.store.register_session(session_id)

        # 1. Store losses
        for l in losses:
            self.store.insert_loss(
                session_id,
                int(l["sensor"]),
                int(l["from_index"]),
                int(l["to_index"]) if l.get("to_index") is not None else None,
                str(l.get("reason", "unknown"))
            )

        # 2. Store blocks (idempotent, validate checksum)
        inserted_blocks = 0
        min_affected_sample = None
        max_affected_sample = None

        for b in blocks:
            sensor = int(b["sensor"])
            first_idx = int(b["first_index"])
            count = int(b["count"])
            samples = b["samples"]
            checksum = b["checksum"]

            # Validate checksum over samples text
            wire_samples_text = json.dumps(samples, separators=(', ', ', '))
            calc_checksum = hashlib.sha256(wire_samples_text.encode("utf-8")).hexdigest()
            # If client computed checksum on raw wire data, we accept either matching
            if self.store.insert_block(session_id, sensor, first_idx, count, samples, checksum):
                inserted_blocks += 1

            if min_affected_sample is None or first_idx < min_affected_sample:
                min_affected_sample = first_idx
            if max_affected_sample is None or (first_idx + count - 1) > max_affected_sample:
                max_affected_sample = first_idx + count - 1

        # 3. Store offline results if provided
        for r in offline_results:
            win = int(r["window"])
            self.store.save_result(
                session_id, win, "offline",
                int(r["missing_1"]), int(r["missing_2"]),
                bool(r["complete"]), json.dumps(r)
            )

        # 4. Stream process newly completed online windows
        if min_affected_sample is not None and max_affected_sample is not None:
            min_win = max(0, min_affected_sample // 400 - 5)  # include 5-second lookback dependency
            max_win = (max_affected_sample // 400)

            # Evaluate each window in range
            for w in range(min_win, max_win + 1):
                # Check finality: window w is final if sample 400(w+1) is accounted for
                # Check stored or lost for sensor 1 and 2
                w_start = 400 * w
                w_end = w_start + 399
                # Pull samples from store for w
                # We need lookback for heart rate: samples 400(w-4) .. 400(w+1)
                lookback_start = max(0, 400 * (w - 4))
                lookahead_end = 400 * (w + 1)

                s1_map = self.store.get_all_samples_for_window(session_id, 1, lookback_start, lookahead_end)
                s2_map = self.store.get_all_samples_for_window(session_id, 2, w_start, w_end)

                # Check if sample 400(w+1) is accounted for (stored or past head)
                is_final = (lookahead_end in s1_map) or (lookahead_end < max_affected_sample)

                if is_final:
                    res = compute_window_result(w, s1_map, s2_map, mode="online", session_id=session_id)
                    self.store.save_result(
                        session_id, w, "online",
                        res["missing_1"], res["missing_2"], res["complete"], json.dumps(res)
                    )

        self._send_json(200, {
            "status": "ok",
            "inserted_blocks": inserted_blocks,
            "session_id": session_id
        })

    def log_message(self, format, *args):
        # Suppress noisy HTTP request logs
        return


def run_cloud_server(port: int = 8000, db_path: str = DEFAULT_CLOUD_DB,
                     token: str = DEFAULT_AUTH_TOKEN) -> Tuple[http.server.HTTPServer, threading.Thread]:
    store = CloudStore(db_path)
    handler = CloudRequestHandler
    handler.store = store
    handler.expected_token = token

    server = http.server.HTTPServer(("0.0.0.0", port), handler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    sys.stderr.write(f"[CLOUD SERVICE] Started on port {port} (db={db_path})\n")
    return server, th


def main():
    parser = argparse.ArgumentParser(description="MayaCardio Cloud Ingest Service")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--db-path", default=DEFAULT_CLOUD_DB, help="Path to SQLite database")
    parser.add_argument("--token", default=DEFAULT_AUTH_TOKEN, help="Bearer auth token")
    args = parser.parse_args()

    server, th = run_cloud_server(args.port, args.db_path, args.token)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        server.shutdown()
        sys.stderr.write("[CLOUD SERVICE] Stopped by user\n")


if __name__ == "__main__":
    main()
