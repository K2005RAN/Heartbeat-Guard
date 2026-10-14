"""
pipeline/netmon.py - End-to-End Network Health Monitor for MayaCardio.
Python 3.11+ standard library only.

Features:
- Probes cloud /health endpoint every 2 seconds with a 2-second socket timeout.
- Transition rule:
    * Go OFFLINE after 3 consecutive failures (~6 seconds, strictly < 10 seconds).
    * Go ONLINE after 2 consecutive successes (~4 seconds, strictly < 10 seconds).
- Operates reliably even when packets are silently dropped (blackholed).
- Thread-safe mode state reporting and change callbacks.
"""

import http.client
import sys
import threading
import time
import urllib.parse
from typing import Callable, Optional

PROBE_INTERVAL = 2.0
PROBE_TIMEOUT = 2.0
FAILURES_TO_OFFLINE = 3  # 3 * 2s = 6s (< 10s spec limit)
SUCCESSES_TO_ONLINE = 2  # 2 * 2s = 4s (< 10s spec limit)


class NetworkMonitor:
    def __init__(self, cloud_url: str, on_mode_change: Optional[Callable[[str], None]] = None):
        self.cloud_url = cloud_url.rstrip("/")
        self.on_mode_change = on_mode_change
        self.current_mode = "offline"  # start conservative
        self.running = False

        parsed = urllib.parse.urlparse(self.cloud_url)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.is_https = (parsed.scheme == "https")

        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last_probe_time = 0.0
        self.last_switch_time = time.monotonic()
        self.lock = threading.Lock()

    def probe_once(self) -> bool:
        """Executes a single HTTP GET /health probe with strict timeout."""
        conn = None
        try:
            if self.is_https:
                import ssl
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(self.host, self.port, timeout=PROBE_TIMEOUT, context=ctx)
            else:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=PROBE_TIMEOUT)

            conn.request("GET", "/health")
            resp = conn.getresponse()
            return resp.status == 200
        except Exception:
            return False
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def check_and_update(self) -> str:
        """Runs a probe and updates state machine. Returns current mode."""
        is_healthy = self.probe_once()
        new_mode = None

        with self.lock:
            if is_healthy:
                self.consecutive_successes += 1
                self.consecutive_failures = 0
                if self.current_mode == "offline" and self.consecutive_successes >= SUCCESSES_TO_ONLINE:
                    new_mode = "online"
                    self.current_mode = "online"
                    self.last_switch_time = time.monotonic()
            else:
                self.consecutive_failures += 1
                self.consecutive_successes = 0
                if self.current_mode == "online" and self.consecutive_failures >= FAILURES_TO_OFFLINE:
                    new_mode = "offline"
                    self.current_mode = "offline"
                    self.last_switch_time = time.monotonic()

            curr = self.current_mode

        if new_mode and self.on_mode_change:
            sys.stderr.write(f"[NETMON] Mode changed to '{new_mode}' (detection within <10s)\n")
            try:
                self.on_mode_change(new_mode)
            except Exception as e:
                sys.stderr.write(f"[NETMON] Error in on_mode_change callback: {e}\n")

        return curr

    def get_mode(self) -> str:
        with self.lock:
            return self.current_mode

    def run_loop(self):
        self.running = True
        sys.stderr.write("[NETMON] Started network monitoring loop\n")
        while self.running:
            t0 = time.monotonic()
            self.check_and_update()
            elapsed = time.monotonic() - t0
            sleep_time = max(0.1, PROBE_INTERVAL - elapsed)
            time.sleep(sleep_time)
        sys.stderr.write("[NETMON] Network monitor loop stopped\n")

    def stop(self):
        self.running = False
