#!/usr/bin/env python3
"""
# STAND-IN, not the official tools
# tools/mock_standin/biosignal_mock_standin.py
#
# Stand-in mock service for MayaCardio assessment (Never Lose a Heartbeat).
# Faithfully implements Section 6 of the assessment specification document:
#   - Newline-delimited JSON over TCP, one client at a time.
#   - 400 samples/second for Sensor 1 (3 channels) and Sensor 2 (2 channels).
#   - Signed 24-bit integers.
#   - Rolling 10-second buffer per sensor (4,000 samples). Older unread samples
#     are reported in `lost` with reason 'buffer_overflow'.
#   - Supports `--mode signal` and `--mode ramp`.
#   - Supports deterministic `--seed`, `--duration`, and `--port`.
#   - Fault injection flags for test suites:
#       --stall-seconds N
#       --corrupt-crc-prob P
#       --duplicate-reply-prob P
#       --drop-sensor S --drop-seconds D
#       --restart-after SECONDS
#       --log-ground-truth PATH
"""

import argparse
import json
import math
import os
import random
import select
import socket
import sys
import threading
import time
import uuid
import zlib

SAMPLE_RATE = 400  # samples per second
BUFFER_SECONDS = 10
BUFFER_SAMPLES = SAMPLE_RATE * BUFFER_SECONDS  # 4000 samples
MAX_SAMPLES_PER_REPLY = SAMPLE_RATE  # at most 1 second (400 samples)
MOD_24BIT = 8388608  # 2^23 for signed 24-bit modulo arithmetic


class SignalGenerator:
    """Generates synthetic sensor data for ramp and signal modes."""

    def __init__(self, mode: str, seed: int):
        self.mode = mode
        self.seed = seed
        self.rng = random.Random(seed)
        # Pre-seed deterministic noise tables for signal mode if needed
        self.s2_noise_seed = seed + 42

    def sample_ramp(self, sensor: int, index: int):
        """Ramp mode as specified in section 6.1:
        Sensor 1 channel c at sample i = (3i + c) mod 8,388,608
        Sensor 2 channel c at sample i = (2i + c) mod 8,388,608
        """
        if sensor == 1:
            return [(3 * index + c) % MOD_24BIT for c in range(3)]
        else:
            return [(2 * index + c) % MOD_24BIT for c in range(2)]

    def sample_signal(self, sensor: int, index: int):
        """Signal mode as specified in section 6.1:
        - Sensor 1 channel 2: ECG-like with sharp peak per heartbeat.
          Heart rate: 60 bpm for min 0-2 (samples 0..48000),
                      90 bpm for min 2-4 (samples 48000..96000),
                      72 bpm for min 4-6 (samples 96000..144000),
                      repeating every 6 minutes (144000 samples).
        - Sensor 1 channels 1 & 3: ch1 = ch2 * 0.6, ch3 = ch2 * 1.3
        - Sensor 2: ch1 near 400,000, ch2 near 150,000, varying slowly.
        """
        if sensor == 1:
            # 6-minute cycle = 360 seconds = 144,000 samples
            cycle_sample = index % (6 * 60 * SAMPLE_RATE)
            minute = cycle_sample / (60 * SAMPLE_RATE)

            if minute < 2.0:
                bpm = 60
                period_samples = 400.0  # 400 sps / 1.0 bps = 400 samples
            elif minute < 4.0:
                bpm = 90
                period_samples = 400.0 * 60.0 / 90.0  # ~266.6667 samples
            else:
                bpm = 72
                period_samples = 400.0 * 60.0 / 72.0  # ~333.3333 samples

            # Heartbeat phase [0, 1)
            phase = (cycle_sample % period_samples) / period_samples

            # Baseline wander and slight respiratory modulation
            baseline = 2000.0 + 300.0 * math.sin(2.0 * math.pi * index / (5.0 * SAMPLE_RATE))

            # Synthesize realistic ECG components: P wave, QRS complex, T wave
            # Peak candidate must satisfy:
            # - sharp peak at R wave > both neighbours
            # - R peak > 0.5 * max(previous 800 samples)
            # - R peak separation >= 100 samples
            val = baseline

            # P wave (around phase 0.15)
            if 0.10 <= phase <= 0.20:
                p_phase = (phase - 0.15) / 0.05
                val += 1200.0 * math.exp(-0.5 * (p_phase * 3.0) ** 2)

            # QRS complex (around phase 0.35)
            if 0.30 <= phase <= 0.40:
                qrs_phase = (phase - 0.35) / 0.02
                # Q dip
                if -1.5 <= qrs_phase < -0.4:
                    val -= 1500.0 * math.exp(-0.5 * ((qrs_phase + 0.9) * 4.0) ** 2)
                # R sharp peak (reaches ~20,000)
                elif -0.4 <= qrs_phase <= 0.4:
                    val += 18000.0 * math.exp(-0.5 * (qrs_phase * 6.0) ** 2)
                # S dip
                elif 0.4 < qrs_phase <= 1.5:
                    val -= 2500.0 * math.exp(-0.5 * ((qrs_phase - 0.8) * 4.0) ** 2)

            # T wave (around phase 0.60)
            if 0.50 <= phase <= 0.70:
                t_phase = (phase - 0.60) / 0.08
                val += 3000.0 * math.exp(-0.5 * (t_phase * 3.0) ** 2)

            # Small high-frequency noise deterministic with seed
            noise = (math.sin(index * 13.17 + self.seed) * 50.0)
            ch2 = int(round(val + noise))
            ch1 = int(round(ch2 * 0.6))
            ch3 = int(round(ch2 * 1.3))
            return [ch1, ch2, ch3]

        else:
            # Sensor 2: channel 1 near 400,000, channel 2 near 150,000, varying slowly
            t = index / SAMPLE_RATE
            ch1 = int(round(400000 + 5000.0 * math.sin(2.0 * math.pi * t / 60.0) +
                            1000.0 * math.sin(2.0 * math.pi * t / 11.0)))
            ch2 = int(round(150000 + 3000.0 * math.cos(2.0 * math.pi * t / 45.0) +
                            800.0 * math.cos(2.0 * math.pi * t / 7.0)))
            return [ch1, ch2]

    def get_sample(self, sensor: int, index: int):
        if self.mode == "ramp":
            return self.sample_ramp(sensor, index)
        else:
            return self.sample_signal(sensor, index)


class SensorBuffer:
    """Manages generated samples and a rolling 10-second buffer for one sensor."""

    def __init__(self, sensor: int, generator: SignalGenerator):
        self.sensor = sensor
        self.generator = generator
        self.last_delivered_index = -1
        self.lock = threading.Lock()

    def get_samples(self, from_index: int | None, current_head: int):
        """Returns (first_index, count, lost_ranges, samples) up to current_head.
        Limits count to at most 400 samples (1 second).
        """
        with self.lock:
            # Buffer oldest available index is max(0, current_head - BUFFER_SAMPLES + 1)
            oldest_available = max(0, current_head - BUFFER_SAMPLES + 1)

            if from_index is None:
                req_from = self.last_delivered_index + 1
            else:
                req_from = from_index

            lost_ranges = []
            if req_from < oldest_available:
                # Samples between req_from and oldest_available - 1 are lost
                lost_ranges.append({
                    "from_index": req_from,
                    "to_index": oldest_available - 1,
                    "reason": "buffer_overflow"
                })
                req_from = oldest_available

            if req_from > current_head:
                # No new samples yet
                return req_from, 0, lost_ranges, []

            count = min(MAX_SAMPLES_PER_REPLY, current_head - req_from + 1)
            samples = [
                self.generator.get_sample(self.sensor, req_from + i)
                for i in range(count)
            ]

            self.last_delivered_index = max(self.last_delivered_index, req_from + count - 1)
            return req_from, count, lost_ranges, samples


class BiosignalMockStandin:
    def __init__(self, mode="signal", seed=0, duration=3600, port=7100,
                 ground_truth_file=None, stall_seconds=0, corrupt_crc_prob=0.0,
                 duplicate_reply_prob=0.0, drop_sensor=0, drop_seconds=0,
                 restart_after=0):
        self.mode = mode
        self.seed = seed
        self.duration = duration
        self.port = port
        self.ground_truth_file = ground_truth_file

        # Fault injection parameters
        self.stall_seconds = stall_seconds
        self.corrupt_crc_prob = corrupt_crc_prob
        self.duplicate_reply_prob = duplicate_reply_prob
        self.drop_sensor = drop_sensor
        self.drop_seconds = drop_seconds
        self.restart_after = restart_after

        self.running = True
        self.session_id = uuid.uuid4().hex[:8]
        self.start_time = None
        self.generator = SignalGenerator(mode, seed)
        self.buf1 = SensorBuffer(1, self.generator)
        self.buf2 = SensorBuffer(2, self.generator)
        self.gt_lock = threading.Lock()

        if self.ground_truth_file:
            # Truncate / create ground truth log
            with open(self.ground_truth_file, "w", encoding="utf-8") as f:
                f.write(f"# Ground truth log for session {self.session_id}\n")

    def log_ground_truth(self, sensor: int, index: int, values: list):
        if not self.ground_truth_file:
            return
        line = json.dumps({"sensor": sensor, "index": index, "values": values}) + "\n"
        with self.gt_lock:
            with open(self.ground_truth_file, "a", encoding="utf-8") as f:
                f.write(line)

    def current_sample_head(self) -> int:
        if self.start_time is None:
            return 0
        elapsed = time.monotonic() - self.start_time
        return int(elapsed * SAMPLE_RATE)

    def handle_client(self, client_sock: socket.socket):
        client_sock.settimeout(0.5)
        recv_buf = bytearray()

        while self.running:
            # Check restart_after condition
            if self.restart_after > 0 and self.start_time is not None:
                elapsed = time.monotonic() - self.start_time
                if elapsed >= self.restart_after:
                    sys.stderr.write(f"[STAND-IN MOCK] Simulating process restart after {elapsed:.1f}s\n")
                    self.session_id = uuid.uuid4().hex[:8]
                    self.generator = SignalGenerator(self.mode, self.seed)
                    self.buf1 = SensorBuffer(1, self.generator)
                    self.buf2 = SensorBuffer(2, self.generator)
                    self.start_time = time.monotonic()
                    self.restart_after = 0  # one-time restart
                    # Close socket to simulate process restart
                    break

            try:
                data = client_sock.recv(4096)
                if not data:
                    break
                recv_buf.extend(data)
                while b"\n" in recv_buf:
                    newline_idx = recv_buf.find(b"\n")
                    line = bytes(recv_buf[:newline_idx])
                    del recv_buf[:newline_idx + 1]
                    line_str = line.decode("utf-8").strip()
                    if not line_str:
                        continue
                    try:
                        req = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue

                    if req.get("cmd") == "read":
                        sensor = int(req.get("sensor", 1))
                        from_index = req.get("from_index")
                        if from_index is not None:
                            from_index = int(from_index)

                        resp_bytes = self.process_read(sensor, from_index)
                        client_sock.sendall(resp_bytes)

                        # Check fault injection: duplicate reply
                        if self.duplicate_reply_prob > 0 and random.random() < self.duplicate_reply_prob:
                            client_sock.sendall(resp_bytes)

            except socket.timeout:
                continue
            except (ConnectionResetError, BrokenPipeError):
                break

        try:
            client_sock.close()
        except Exception:
            pass

    def process_read(self, sensor: int, from_index: int | None) -> bytes:
        # Check fault injection: stall
        if self.stall_seconds > 0:
            stall = self.stall_seconds
            self.stall_seconds = 0
            time.sleep(stall)

        # Check fault injection: drop sensor
        if self.drop_sensor == sensor and self.drop_seconds > 0:
            # Synthesize dropped sensor behavior
            time.sleep(min(0.2, self.drop_seconds))
            self.drop_seconds -= 0.2

        cur_head = self.current_sample_head()
        max_duration_samples = self.duration * SAMPLE_RATE
        cur_head = min(cur_head, max_duration_samples - 1)

        buf = self.buf1 if sensor == 1 else self.buf2
        first_idx, count, lost_ranges, samples = buf.get_samples(from_index, cur_head)

        session_end = (cur_head >= max_duration_samples - 1 and count == 0)

        # Log delivered samples to ground truth
        for i, sample_vals in enumerate(samples):
            self.log_ground_truth(sensor, first_idx + i, sample_vals)

        # Format data array text exactly to compute CRC-32
        data_text = json.dumps(samples, separators=(', ', ', '))
        raw_data_bytes = data_text.encode("utf-8")
        crc_int = zlib.crc32(raw_data_bytes) & 0xFFFFFFFF
        crc_hex = f"{crc_int:08x}"

        # Fault injection: corrupt CRC
        if self.corrupt_crc_prob > 0 and random.random() < self.corrupt_crc_prob:
            crc_hex = "deadbeef"

        # Construct full response line
        # Note: data_text must appear exactly as CRC was computed over it
        resp_obj = {
            "session_id": self.session_id,
            "sensor": sensor,
            "first_index": first_idx,
            "count": count,
            "lost": lost_ranges,
            "session_end": session_end,
            "crc32": crc_hex,
        }
        # Pre-serialize header without data
        hdr_json = json.dumps(resp_obj)
        # Stitch `"data": ` + data_text + `}\n`
        # hdr_json ends with '}', replace trailing '}' with ', "data": ' + data_text + '}\n'
        resp_str = hdr_json[:-1] + ', "data": ' + data_text + '}\n'
        return resp_str.encode("utf-8")

    def run(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("0.0.0.0", self.port))
        server_sock.listen(1)
        server_sock.settimeout(0.5)

        self.start_time = time.monotonic()
        sys.stderr.write(f"[STAND-IN MOCK] Running on port {self.port}, mode={self.mode}, "
                         f"seed={self.seed}, session={self.session_id}\n")

        restart_timer = self.restart_after
        while self.running:
            try:
                client_sock, addr = server_sock.accept()
                sys.stderr.write(f"[STAND-IN MOCK] Accepted connection from {addr}\n")
                # One client at a time!
                self.handle_client(client_sock)
                sys.stderr.write(f"[STAND-IN MOCK] Client disconnected\n")
            except socket.timeout:
                pass

            if self.restart_after > 0:
                elapsed = time.monotonic() - self.start_time
                if elapsed >= self.restart_after:
                    sys.stderr.write(f"[STAND-IN MOCK] Simulating restart after {elapsed:.1f}s\n")
                    self.session_id = uuid.uuid4().hex[:8]
                    self.generator = SignalGenerator(self.mode, self.seed)
                    self.buf1 = SensorBuffer(1, self.generator)
                    self.buf2 = SensorBuffer(2, self.generator)
                    self.start_time = time.monotonic()
                    self.restart_after = 0  # one-time restart

        server_sock.close()


def main():
    parser = argparse.ArgumentParser(
        description="STAND-IN, not the official tools: biosignal-mock service stand-in"
    )
    parser.add_argument("--mode", choices=["signal", "ramp"], default="signal",
                        help="Operating mode: signal (synthetic ECG) or ramp (monotonic mod formula)")
    parser.add_argument("--seed", type=int, default=0, help="Random seed for reproducible signal")
    parser.add_argument("--duration", type=int, default=3600, help="Session duration in seconds")
    parser.add_argument("--port", type=int, default=7100, help="TCP port to listen on")
    parser.add_argument("--log-ground-truth", type=str, default=None,
                        help="Path to write ground-truth delivered samples for verification")

    # Fault injection flags
    parser.add_argument("--stall-seconds", type=float, default=0.0,
                        help="Simulate mock hanging for N seconds on next read")
    parser.add_argument("--corrupt-crc-prob", type=float, default=0.0,
                        help="Probability of injecting corrupt CRC32")
    parser.add_argument("--duplicate-reply-prob", type=float, default=0.0,
                        help="Probability of sending duplicate reply")
    parser.add_argument("--drop-sensor", type=int, default=0, choices=[0, 1, 2],
                        help="Drop data from sensor 1 or 2")
    parser.add_argument("--drop-seconds", type=float, default=0.0,
                        help="Duration of sensor drop in seconds")
    parser.add_argument("--restart-after", type=float, default=0.0,
                        help="Simulate mock process restart after N seconds")

    args = parser.parse_args()

    mock = BiosignalMockStandin(
        mode=args.mode,
        seed=args.seed,
        duration=args.duration,
        port=args.port,
        ground_truth_file=args.log_ground_truth,
        stall_seconds=args.stall_seconds,
        corrupt_crc_prob=args.corrupt_crc_prob,
        duplicate_reply_prob=args.duplicate_reply_prob,
        drop_sensor=args.drop_sensor,
        drop_seconds=args.drop_seconds,
        restart_after=args.restart_after
    )
    try:
        mock.run()
    except KeyboardInterrupt:
        mock.running = False
        sys.stderr.write("[STAND-IN MOCK] Stopped by user\n")


if __name__ == "__main__":
    main()
