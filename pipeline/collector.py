"""
pipeline/collector.py - Robust TCP Collector for MayaCardio bedside device.
Python 3.11+ standard library only.

Features:
- Single client TCP connection to mock on host:port.
- Alternating sensor polling (~0.25-0.5s) with immediate drain on full buffers (count==400).
- Wire-level bracket parser for exact raw CRC-32 validation (no re-serialization).
- Bounded re-reading on corruption or unexpected index gaps.
- Verbatim recording of mock buffer overflow / sensor drops into the loss ledger.
- Clean session lifecycle handling (source restart tail loss, session end).
- Monotonic clock for all timeouts, backoff, and poll cadence.
"""

import hashlib
import json
import socket
import sys
import time
import zlib
from typing import Dict, Optional, Tuple
from pipeline.store import DeviceStore


def extract_wire_data_bytes(wire_line: bytes) -> Optional[bytes]:
    """Extracts the exact raw wire bytes between the opening '[' of the 'data' field
    and its matching closing ']', preserving wire whitespace and formatting.
    """
    data_key_idx = wire_line.find(b'"data"')
    if data_key_idx == -1:
        return None
    colon_idx = wire_line.find(b":", data_key_idx)
    if colon_idx == -1:
        return None
    start_bracket = wire_line.find(b"[", colon_idx)
    if start_bracket == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start_bracket, len(wire_line)):
        b = wire_line[i]
        if escape:
            escape = False
            continue
        if b == ord('\\'):
            escape = True
            continue
        if b == ord('"'):
            in_string = not in_string
            continue
        if not in_string:
            if b == ord('['):
                depth += 1
            elif b == ord(']'):
                depth -= 1
                if depth == 0:
                    return wire_line[start_bracket:i + 1]
    return None


class Collector:
    def __init__(self, host: str, port: int, store: DeviceStore, poll_interval: float = 0.25):
        self.host = host
        self.port = port
        self.store = store
        self.poll_interval = poll_interval
        self.running = False
        self.sock: Optional[socket.socket] = None

        self.current_session_id: Optional[str] = None
        self.expected_index: Dict[int, int] = {1: 0, 2: 0}
        self.recv_buffer = bytearray()

    def connect(self) -> bool:
        """Connects to the mock service over TCP with timeout and backoff."""
        self.close_socket()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((self.host, self.port))
            self.sock = s
            self.recv_buffer.clear()
            sys.stderr.write(f"[COLLECTOR] Connected to mock service at {self.host}:{self.port}\n")
            return True
        except (socket.error, OSError) as e:
            sys.stderr.write(f"[COLLECTOR] Connection failed: {e}\n")
            self.close_socket()
            return False

    def close_socket(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def send_request(self, sensor: int, from_index: Optional[int] = None) -> bool:
        if not self.sock:
            return False
        if from_index is not None:
            cmd = json.dumps({"cmd": "read", "sensor": sensor, "from_index": from_index}) + "\n"
        else:
            cmd = json.dumps({"cmd": "read", "sensor": sensor}) + "\n"
        try:
            self.sock.sendall(cmd.encode("utf-8"))
            return True
        except (socket.error, OSError):
            self.close_socket()
            return False

    def read_line(self, timeout_sec: float = 2.0) -> Optional[bytes]:
        """Reads a single newline-delimited wire line from TCP stream."""
        if not self.sock:
            return None
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            newline_pos = self.recv_buffer.find(b"\n")
            if newline_pos != -1:
                line = bytes(self.recv_buffer[:newline_pos + 1])
                del self.recv_buffer[:newline_pos + 1]
                return line

            remaining = max(0.1, deadline - time.monotonic())
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.close_socket()
                    return None
                self.recv_buffer.extend(chunk)
            except socket.timeout:
                continue
            except (socket.error, OSError):
                self.close_socket()
                return None
        return None

    def process_reply_line(self, wire_line: bytes, requested_sensor: int) -> Tuple[bool, int]:
        """Validates CRC on wire slice, parses JSON, checks continuity, and commits to store.
        Returns (success: bool, sample_count: int).
        """
        raw_data = extract_wire_data_bytes(wire_line)
        if raw_data is None:
            sys.stderr.write("[COLLECTOR] Malformed reply: 'data' field missing or unclosed\n")
            return False, 0

        # Validate CRC-32 on exact raw wire text
        calculated_crc = f"{zlib.crc32(raw_data) & 0xFFFFFFFF:08x}"

        try:
            obj = json.loads(wire_line.decode("utf-8").strip())
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[COLLECTOR] JSON decode error: {e}\n")
            return False, 0

        reported_crc = str(obj.get("crc32", "")).lower()
        session_id = obj.get("session_id")
        sensor = int(obj.get("sensor", requested_sensor))
        first_index = int(obj.get("first_index", 0))
        count = int(obj.get("count", 0))
        lost = obj.get("lost", [])
        session_end = bool(obj.get("session_end", False))
        samples = obj.get("data", [])

        # CRC mismatch handling
        if calculated_crc != reported_crc:
            sys.stderr.write(
                f"[COLLECTOR] CRC mismatch on sensor {sensor} index {first_index}! "
                f"calc={calculated_crc}, wire={reported_crc}\n"
            )
            return False, count

        # Session lifecycle
        if self.current_session_id is None:
            self.current_session_id = session_id
            self.store.register_session(session_id)
            # Check if recovering existing session on device
            for s in (1, 2):
                max_stored = self.store.get_max_stored_index(session_id, s)
                if max_stored >= 0:
                    self.expected_index[s] = max_stored + 1
                else:
                    self.expected_index[s] = 0
        elif self.current_session_id != session_id:
            # Source restart detected
            sys.stderr.write(
                f"[COLLECTOR] Mock restarted: old session {self.current_session_id} -> new session {session_id}\n"
            )
            # Finalize old session tail as lost
            for s in (1, 2):
                self.store.record_loss(
                    self.current_session_id, s, self.expected_index[s], None, "source_restart"
                )
            self.store.finalize_session(self.current_session_id)
            self.current_session_id = session_id
            self.store.register_session(session_id)
            self.expected_index = {1: 0, 2: 0}

        # 1. Process reported losses from mock
        for item in lost:
            l_from = int(item["from_index"])
            l_to = int(item["to_index"])
            reason = str(item.get("reason", "buffer_overflow"))
            self.store.record_loss(session_id, sensor, l_from, l_to, reason)
            if l_to >= self.expected_index[sensor]:
                self.expected_index[sensor] = l_to + 1

        # 2. Check index continuity
        if count > 0:
            if first_index < self.expected_index[sensor]:
                # Duplicate or overlapping reply
                sys.stderr.write(
                    f"[COLLECTOR] Ignoring duplicate/past reply for sensor {sensor}: "
                    f"first_index={first_index}, expected={self.expected_index[sensor]}\n"
                )
                return True, count

            if first_index > self.expected_index[sensor]:
                # Unreported gap
                gap_from = self.expected_index[sensor]
                gap_to = first_index - 1
                sys.stderr.write(
                    f"[COLLECTOR] Unreported gap detected for sensor {sensor}: {gap_from}..{gap_to}\n"
                )
                self.store.record_loss(session_id, sensor, gap_from, gap_to, "gap_not_reported_by_source")
                self.expected_index[sensor] = first_index

            # 3. Store valid block
            checksum = hashlib.sha256(raw_data).hexdigest()
            self.store.store_block(session_id, sensor, first_index, count, samples, checksum)
            self.expected_index[sensor] = first_index + count

        if session_end:
            sys.stderr.write(f"[COLLECTOR] Session {session_id} ended by source\n")
            self.store.finalize_session(session_id)

        return True, count

    def read_sensor_with_retries(self, sensor: int) -> Tuple[bool, int]:
        """Attempts to read next samples for sensor, retrying with from_index on corruption."""
        if not self.sock:
            if not self.connect():
                return False, 0

        # Normal read
        if not self.send_request(sensor):
            return False, 0

        line = self.read_line(timeout_sec=2.0)
        if line is None:
            return False, 0

        success, count = self.process_reply_line(line, sensor)
        if success:
            return True, count

        # If CRC was corrupt or line was bad, retry with from_index
        expected = self.expected_index.get(sensor, 0)
        sys.stderr.write(f"[COLLECTOR] Re-reading sensor {sensor} from_index={expected} after failure\n")

        for attempt in range(3):
            if not self.send_request(sensor, from_index=expected):
                if not self.connect():
                    break
                self.send_request(sensor, from_index=expected)

            line = self.read_line(timeout_sec=2.0)
            if line is not None:
                retry_success, retry_count = self.process_reply_line(line, sensor)
                if retry_success:
                    return True, retry_count

        # If unrecoverable after 3 retries
        sys.stderr.write(f"[COLLECTOR] Sensor {sensor} read unrecoverable at index {expected}\n")
        if self.current_session_id:
            self.store.record_loss(self.current_session_id, sensor, expected, expected, "corrupt_unrecoverable")
            self.expected_index[sensor] = expected + 1
        return False, 0

    def step(self) -> int:
        """Executes one polling cycle alternating sensor 1 and sensor 2.
        Returns total samples received.
        """
        total_samples = 0
        for sensor in (1, 2):
            success, count = self.read_sensor_with_retries(sensor)
            total_samples += count
            # Drain immediately if buffer was full (400 samples)
            while success and count == 400 and self.running:
                success, count = self.read_sensor_with_retries(sensor)
                total_samples += count
        return total_samples

    def run_loop(self):
        self.running = True
        sys.stderr.write(f"[COLLECTOR] Started collection loop (poll_interval={self.poll_interval}s)\n")
        while self.running:
            t0 = time.monotonic()
            try:
                samples = self.step()
            except Exception as e:
                sys.stderr.write(f"[COLLECTOR] Exception in loop: {e}\n")
                self.close_socket()
                time.sleep(1.0)
                continue

            elapsed = time.monotonic() - t0
            sleep_time = max(0.01, self.poll_interval - elapsed)
            time.sleep(sleep_time)

        self.close_socket()
        sys.stderr.write("[COLLECTOR] Collection loop stopped\n")

    def stop(self):
        self.running = False
        self.close_socket()
