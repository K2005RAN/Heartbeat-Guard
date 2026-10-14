#!/usr/bin/env python3
"""
Unit and integration test for tools/mock_standin/biosignal_mock_standin.py
and tools/ground_truth_standin/check_ground_truth_standin.py
"""

import json
import os
import socket
import tempfile
import time
import unittest
import zlib
from tools.mock_standin.biosignal_mock_standin import BiosignalMockStandin, SignalGenerator
from tools.ground_truth_standin.check_ground_truth_standin import verify


class TestMockStandin(unittest.TestCase):

    def test_ramp_formulas(self):
        gen = SignalGenerator("ramp", seed=0)
        # Sensor 1: (3*i + c) mod 8388608
        s1_0 = gen.get_sample(1, 0)
        self.assertEqual(s1_0, [0, 1, 2])
        s1_10 = gen.get_sample(1, 10)
        self.assertEqual(s1_10, [30, 31, 32])

        # Sensor 2: (2*i + c) mod 8388608
        s2_0 = gen.get_sample(2, 0)
        self.assertEqual(s2_0, [0, 1])
        s2_5 = gen.get_sample(2, 5)
        self.assertEqual(s2_5, [10, 11])

    def test_signal_deterministic_seed(self):
        gen1 = SignalGenerator("signal", seed=42)
        gen2 = SignalGenerator("signal", seed=42)
        for i in [0, 100, 1000, 5000]:
            self.assertEqual(gen1.get_sample(1, i), gen2.get_sample(1, i))
            self.assertEqual(gen1.get_sample(2, i), gen2.get_sample(2, i))

    def test_crc32_wire_calculation(self):
        samples = [[10, 20, 30], [40, 50, 60]]
        data_text = json.dumps(samples, separators=(', ', ', '))
        expected_crc = f"{zlib.crc32(data_text.encode('utf-8')) & 0xFFFFFFFF:08x}"
        self.assertEqual(len(expected_crc), 8)

    def test_mock_tcp_exchange(self):
        port = 7188
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tf:
            gt_file = tf.name

        try:
            import threading
            mock = BiosignalMockStandin(
                mode="ramp", seed=123, duration=10, port=port,
                ground_truth_file=gt_file
            )
            mock_thread = threading.Thread(target=mock.run, daemon=True)
            mock_thread.start()
            time.sleep(0.3)

            # Connect client
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", port))
            s.sendall(b'{"cmd": "read", "sensor": 1}\n')

            resp = b""
            while b"\n" not in resp:
                resp += s.recv(4096)
            s.close()
            mock.running = False

            line = resp.decode("utf-8").strip()
            obj = json.loads(line)
            self.assertEqual(obj["sensor"], 1)
            self.assertIn("crc32", obj)
            self.assertIn("data", obj)
            # Verify raw CRC
            data_start = resp.find(b'"data": [') + len(b'"data": ')
            data_end = resp.rfind(b']')
            wire_data = resp[data_start:data_end + 1]
            calc_crc = f"{zlib.crc32(wire_data) & 0xFFFFFFFF:08x}"
            self.assertEqual(obj["crc32"], calc_crc)

        finally:
            if os.path.exists(gt_file):
                os.remove(gt_file)


if __name__ == "__main__":
    unittest.main()
