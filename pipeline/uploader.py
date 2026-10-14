"""
pipeline/uploader.py - Dual-Priority HTTP Uploader for MayaCardio Bedside Device.
Python 3.11+ standard library only.

Features:
- Prioritizes LIVE data over backlog to guarantee cloud results within 5 seconds.
- Drains BACKLOG in 60-second compressed (gzip) batches in background.
- Authoritative state sync: queries GET /state to prevent redundant backlog uploads.
- Idempotent block transmission with SHA-256 checksums.
- Marks SQLite cloud_ack table on successful ingestion.
- Transmits local offline results to cloud for reconciliation.
"""

import gzip
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

from pipeline.store import DeviceStore, unpack_24bit_samples

LIVE_INTERVAL_SEC = 0.5
BACKLOG_BATCH_SAMPLES = 24000  # 60 seconds of data @ 400 sps


class Uploader:
    def __init__(self, cloud_url: str, token: str, store: DeviceStore):
        self.cloud_url = cloud_url.rstrip("/")
        self.token = token
        self.store = store
        self.running = False

        parsed = urllib.parse.urlparse(self.cloud_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.is_https = (parsed.scheme == "https")

        self.last_live_index = {1: -1, 2: -1}

    def _get_http_conn(self, timeout: float = 3.0):
        if self.is_https:
            import ssl
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ctx)
        else:
            return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def check_health(self) -> bool:
        """Probes /health endpoint end-to-end."""
        conn = None
        try:
            conn = self._get_http_conn(timeout=2.0)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status == 200
        except Exception:
            return False
        finally:
            if conn:
                conn.close()

    def get_cloud_state(self, session_id: str) -> Optional[dict]:
        """Queries cloud for authoritative list of ranges held for this session."""
        conn = None
        try:
            conn = self._get_http_conn(timeout=3.0)
            conn.request("GET", f"/state?session={session_id}")
            resp = conn.getresponse()
            if resp.status == 200:
                body = resp.read()
                return json.loads(body.decode("utf-8"))
            return None
        except Exception:
            return None
        finally:
            if conn:
                conn.close()

    def send_ingest_payload(self, payload: dict, compress: bool = False) -> bool:
        """Sends an ingest payload to POST /ingest."""
        conn = None
        try:
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}"
            }
            if compress:
                body = gzip.compress(body)
                headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(body))

            conn = self._get_http_conn(timeout=25.0)
            conn.request("POST", "/ingest", body=body, headers=headers)
            resp = conn.getresponse()
            if resp.status == 200:
                resp.read()  # consume response body
                return True
            else:
                sys.stderr.write(f"[UPLOADER] Ingest failed: HTTP {resp.status} - {resp.read().decode('utf-8', 'ignore')}\n")
                return False
        except Exception as e:
            sys.stderr.write(f"[UPLOADER] Ingest network exception: {e}\n")
            return False
        finally:
            if conn:
                conn.close()

    def upload_live_blocks(self, session_id: str) -> int:
        """Uploads newly committed live blocks immediately (<1s latency)."""
        uploaded_samples = 0
        blocks_to_send = []
        ack_list = []

        for sensor in (1, 2):
            unsent = self.store.get_live_blocks(session_id, sensor, limit=5)
            for b in unsent:
                blocks_to_send.append({
                    "sensor": sensor,
                    "first_index": b["first_index"],
                    "count": b["count"],
                    "samples": b["samples"],
                    "checksum": b["checksum"]
                })
                ack_list.append((sensor, b["first_index"], b["count"]))
                uploaded_samples += b["count"]

        if not blocks_to_send:
            return 0

        # Send uncompressed for minimal latency on live stream
        payload = {
            "session_id": session_id,
            "blocks": blocks_to_send,
            "losses": [],
            "offline_results": []
        }

        if self.send_ingest_payload(payload, compress=False):
            for sensor, f_idx, count in ack_list:
                self.store.mark_cloud_ack(session_id, sensor, f_idx, count)
            return uploaded_samples
        return 0

    def upload_backlog_batch(self, session_id: str) -> int:
        """Uploads a compressed batch of backlog data (up to 60s of data)."""
        blocks_to_send = []
        ack_list = []
        total_samples = 0

        # Sync with authoritative cloud state to avoid redundant uploads
        cloud_state = self.get_cloud_state(session_id)
        if cloud_state:
            # Mark blocks already held in cloud
            for sensor in (1, 2):
                held_ranges = cloud_state.get(f"ranges_{sensor}", [])
                for start, end in held_ranges:
                    self.store.mark_cloud_ack(session_id, sensor, start, end - start + 1)

        # Pull next unsent blocks
        for sensor in (1, 2):
            unsent = self.store.get_backlog_blocks(session_id, sensor, limit=60)
            for b in unsent:
                blocks_to_send.append({
                    "sensor": sensor,
                    "first_index": b["first_index"],
                    "count": b["count"],
                    "samples": b["samples"],
                    "checksum": b["checksum"]
                })
                ack_list.append((sensor, b["first_index"], b["count"]))
                total_samples += b["count"]
                if total_samples >= BACKLOG_BATCH_SAMPLES:
                    break
            if total_samples >= BACKLOG_BATCH_SAMPLES:
                break

        if not blocks_to_send:
            return 0

        # Include loss records
        losses = []
        for s in (1, 2):
            for f_idx, t_idx, reason in self.store.get_loss_ranges(session_id, s):
                losses.append({
                    "sensor": s,
                    "from_index": f_idx,
                    "to_index": t_idx,
                    "reason": reason
                })

        # Include any local offline results
        results_raw = self.store.get_results(session_id)
        offline_results = [json.loads(r) for r in results_raw]

        payload = {
            "session_id": session_id,
            "blocks": blocks_to_send,
            "losses": losses,
            "offline_results": offline_results
        }

        # Send gzip compressed
        if self.send_ingest_payload(payload, compress=True):
            for sensor, f_idx, count in ack_list:
                self.store.mark_cloud_ack(session_id, sensor, f_idx, count)
            return total_samples
        return 0

    def step(self, session_id: str) -> int:
        """Single uploader step: live stream takes precedence over backlog."""
        live_uploaded = self.upload_live_blocks(session_id)
        if live_uploaded > 0:
            return live_uploaded

        # If live stream has no immediate pending samples, drain backlog
        return self.upload_backlog_batch(session_id)

    def run_loop(self):
        self.running = True
        sys.stderr.write("[UPLOADER] Started upload loop\n")
        while self.running:
            session_id = self.store.get_latest_session()
            if not session_id:
                time.sleep(0.5)
                continue

            try:
                uploaded = self.step(session_id)
                if uploaded == 0:
                    time.sleep(LIVE_INTERVAL_SEC)
            except Exception as e:
                sys.stderr.write(f"[UPLOADER] Error in loop: {e}\n")
                time.sleep(1.0)
        sys.stderr.write("[UPLOADER] Upload loop stopped\n")

    def stop(self):
        self.running = False
