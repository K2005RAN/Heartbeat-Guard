# Protocol Assumptions & Wire Specification

> **STAND-IN NOTICE**: This document records all protocol and framing assumptions made for the
> MayaCardio data collection pipeline, derived from the assessment specification document
> (*MayaCardio_Software_Systems_Engg_Candidate_Assessment_Oct_2026.docx*). When the official
> `biosignal-mock` binary is obtained, cross-check this document immediately using
> `scripts/validate_against_real_mock.sh`.

---

## 1. Transport & Connection Model

- **Transport**: TCP over IPv4/IPv6 (default port `7100`).
- **Framing**: Newline-delimited JSON (`\n` or `\r\n`). Each line is a complete, self-contained JSON object.
- **Client Concurrency**: **Strictly ONE client at a time**. The mock service rejects or ignores new connections while a client is active. The pipeline collector must cleanly close any stale socket before opening a new socket.
- **Polling Cadence**: The client initiates all reads. Sensor 1 and Sensor 2 are read alternatively with a target poll interval of 0.25–0.50 s.
- **Burst / Drain Behavior**: If a response returns `count == 400` (1 full second), the client reads again immediately without sleeping to drain any buffered backlog.

---

## 2. Request Wire Format

Client requests sent to the mock are single-line JSON followed by `\n`:

### Normal read request:
```json
{"cmd": "read", "sensor": 1}
```

### Re-read / backfill request (e.g., after corruption or gap):
```json
{"cmd": "read", "sensor": 1, "from_index": 120000}
```

- `sensor`: Integer `1` or `2`.
- `from_index`: 0-indexed integer indicating the starting sample to request.

---

## 3. Response Wire Format

Mock responses are single-line JSON followed by `\n`:

```json
{"session_id": "5f0c2a9e", "sensor": 1, "first_index": 120000, "count": 3, "lost": [], "session_end": false, "crc32": "f9a5bf62", "data": [[720, 1200, 1560], [708, 1180, 1534], [726, 1210, 1573]]}
```

### Response Field Semantics:
| Field | Type | Description |
|---|---|---|
| `session_id` | `string` | Unique session identifier. Changes whenever the mock process restarts. Samples restart at index 0 on new session. |
| `sensor` | `integer` | `1` or `2`. |
| `first_index` | `integer` | Index of the first sample contained in `data`. Starts at 0 for each session. |
| `count` | `integer` | Number of samples in `data` (at most 400, corresponding to 1 second). |
| `lost` | `array` | List of ranges the mock could not deliver, e.g. `[{"from_index": 4000, "to_index": 4399, "reason": "buffer_overflow"}]`. |
| `session_end` | `boolean` | `true` only in the final reply of a completed session. |
| `crc32` | `string` | 8-character lowercase hexadecimal CRC-32 (standard IEEE 802.3 / zlib `crc32`). Computed strictly on the wire slice of `data`. |
| `data` | `array` | List of samples. Sensor 1: 3 signed 24-bit integers per sample. Sensor 2: 2 signed 24-bit integers per sample. |

---

## 4. CRC-32 Computation Rule (Critical)

- **Definition**: The `crc32` field is computed over the raw wire bytes of the `data` array value, starting strictly from the first opening bracket `[` of the data array to its matching closing bracket `]`.
- **Zero Serialization Assumption**: The client **MUST NOT** re-serialize the parsed JSON object to verify the CRC. The pipeline extracts the byte span `[raw_bracket_start : raw_bracket_end + 1]` directly from the incoming TCP byte stream.
- **Tolerance**: The framing parser handles variable whitespace and key order by locating the key `"data"` and extracting balanced bracket bounds on the raw stream. If CRC does not match, the packet is flagged as corrupt and discarded.

---

## 5. Buffer Limits & Retention

- **Mock Buffer**: The mock holds a rolling buffer of the last 10 seconds (4,000 samples) per sensor.
- **Buffer Overflow**: Data not requested before it falls off the 10-second window is permanently dropped by the mock and reported in the `lost` array with `reason: "buffer_overflow"`.

---

## 6. Fault Modes Injected by Judges

The pipeline handles all judge fault injections defined in Section 6.3:
1. **Mock stall**: Mock stops replying for several seconds. Addressed by socket timeout (1.5s), reconnect with exponential backoff.
2. **Corrupt reply**: `crc32` does not match raw `data` slice. Addressed by bounded re-read from `from_index` within 10s buffer; if buffer expired or unrecoverable, recorded as `corrupt_unrecoverable`.
3. **Duplicate reply**: Exact duplicate received. Detected via `(session, sensor, first_index, count)` check against store; duplicate ignored.
4. **Sensor stoppage**: One sensor stops sending while the other continues. Reported in `lost` by mock; recorded in ledger.
5. **Mock restart**: Mock killed and restarted with new `session_id`. Stored tail of previous session finalized with `to_index: null` and reason `source_restart`. New session begins at index 0.
6. **Operating environment faults**: Process kill (`kill -9`), sudden power interruption, network blackhole, disk full, and clock jumps.
