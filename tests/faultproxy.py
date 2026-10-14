"""
tests/faultproxy.py - Fault-Injecting TCP Proxy for MayaCardio Assessment.
Python 3.11+ standard library only.

Sits between the bedside pipeline collector and the mock service:
  Collector <----> FaultProxy <----> Biosignal Mock
Injects:
  - stall: sleep N seconds before relaying
  - corrupt_crc: replace crc32 with invalid hex
  - duplicate_reply: send the same reply twice
  - drop_sensor: drop all packets for a given sensor for N seconds
  - disconnect: abrupt TCP socket close mid-stream
  - blackhole: accept packets but never reply (timeout test)
"""

import argparse
import select
import socket
import sys
import threading
import time


class FaultProxy:
    def __init__(self, listen_port: int, target_host: str, target_port: int):
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.running = True

        # Fault configurations
        self.stall_seconds: float = 0.0
        self.corrupt_crc: bool = False
        self.duplicate_reply: bool = False
        self.drop_sensor: int = 0
        self.drop_sensor_until: float = 0.0
        self.disconnect_after_bytes: int = 0
        self.blackhole: bool = False

        self.bytes_transferred: int = 0
        self.server_sock = None

    def start(self):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("0.0.0.0", self.listen_port))
        self.server_sock.listen(1)
        self.server_sock.settimeout(0.5)

        sys.stderr.write(f"[FAULT PROXY] Listening on port {self.listen_port} -> {self.target_host}:{self.target_port}\n")
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                sys.stderr.write(f"[FAULT PROXY] Accepted client {addr}\n")
                self._handle_client(client_sock)
            except socket.timeout:
                pass
            except Exception as e:
                if self.running:
                    sys.stderr.write(f"[FAULT PROXY] Accept error: {e}\n")

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

    def _handle_client(self, client_sock: socket.socket):
        try:
            target_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target_sock.connect((self.target_host, self.target_port))
        except Exception as e:
            sys.stderr.write(f"[FAULT PROXY] Could not connect to target: {e}\n")
            client_sock.close()
            return

        client_sock.setblocking(False)
        target_sock.setblocking(False)

        sockets = [client_sock, target_sock]
        c2t_buf = bytearray()
        t2c_buf = bytearray()

        while self.running:
            if self.disconnect_after_bytes > 0 and self.bytes_transferred >= self.disconnect_after_bytes:
                sys.stderr.write(f"[FAULT PROXY] Injecting mid-stream disconnect after {self.bytes_transferred} bytes\n")
                break

            r, w, x = select.select(sockets, [], sockets, 0.2)
            if x:
                break

            if client_sock in r:
                try:
                    data = client_sock.recv(4096)
                    if not data:
                        break
                    if not self.blackhole:
                        target_sock.sendall(data)
                except (BlockingIOError, socket.error):
                    break

            if target_sock in r:
                try:
                    data = target_sock.recv(4096)
                    if not data:
                        break
                    self.bytes_transferred += len(data)

                    t2c_buf.extend(data)
                    while b"\n" in t2c_buf:
                        newline_idx = t2c_buf.find(b"\n")
                        line = bytes(t2c_buf[:newline_idx])
                        del t2c_buf[:newline_idx + 1]
                        if not line:
                            continue

                        # Apply faults
                        if self.stall_seconds > 0:
                            sys.stderr.write(f"[FAULT PROXY] Injecting stall for {self.stall_seconds}s\n")
                            time.sleep(self.stall_seconds)
                            self.stall_seconds = 0

                        if self.blackhole:
                            continue

                        # Sensor drop fault
                        if self.drop_sensor > 0 and time.monotonic() < self.drop_sensor_until:
                            if f'"sensor": {self.drop_sensor}'.encode() in line or f'"sensor":{self.drop_sensor}'.encode() in line:
                                sys.stderr.write(f"[FAULT PROXY] Dropping sensor {self.drop_sensor} reply\n")
                                continue

                        # Corrupt CRC fault
                        if self.corrupt_crc:
                            if b'"crc32":' in line:
                                line = line.replace(b'"crc32": "', b'"crc32": "bad_')
                                sys.stderr.write("[FAULT PROXY] Injected corrupt CRC32\n")

                        # Send to client
                        client_sock.sendall(line + b"\n")

                        # Duplicate reply fault
                        if self.duplicate_reply:
                            client_sock.sendall(line + b"\n")
                            sys.stderr.write("[FAULT PROXY] Injected duplicate reply\n")

                except (BlockingIOError, socket.error):
                    break

        try:
            client_sock.close()
            target_sock.close()
        except Exception:
            pass
        sys.stderr.write("[FAULT PROXY] Connection closed\n")
