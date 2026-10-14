# MayaCardio Data Pipeline: Architecture & Design Note

> **Note**: All testing and performance measurements documented herein were conducted using the
> standalone stand-in test tools (`biosignal_mock_standin.py` and `check_ground_truth_standin.py`)
> developed strictly to the specification in Section 6 of the assessment document. Re-testing with
> the official binaries will be performed upon receipt.

---

## 1. System Architecture

The bedside pipeline runs on an ARM64 Linux system (Raspberry Pi 4, 4 cores, 2 GB RAM). The architecture is divided into three asynchronous stages decoupled by a transactional, append-only SQLite local store:

```
+-----------------------------------------------------------------------------------------+
|                                    BEDSIDE DEVICE                                       |
|                                                                                         |
|  +-------------------+       +-----------------------+       +-----------------------+  |
|  |   Sensor Mock     |  TCP  |       Collector       |  WAL  |     SQLite Store      |  |
|  |  (Port 7100)      | ----> | - Framing & CRC verify| ----> | - Blocks (Packed 24b) |  |
|  | (1 client max)    |       | - Gap/Loss detection  | fsync | - Loss Ledger         |  |
|  +-------------------+       +-----------------------+       | - Window Results      |  |
|                                                              +-----------------------+  |
|                                                                    |           |        |
|                                +-----------------------------------+           |        |
|                                | (Offline mode)                                |        |
|                                v                                               v        |
|                      +--------------------+                          +---------------+  |
|                      |  Local Processor   |                          |   Uploader    |  |
|                      | - Incremental peak |                          | - Live 1st    |  |
|                      | - 1-sec windows    |                          | - Backlog 60s |  |
|                      +--------------------+                          +---------------+  |
|                                 |                                            |          |
|                                 v (offline results)                          | HTTPS    |
|                      +--------------------+                                  | (Token)  |
|                      | Network Monitor    |                                  |          |
|                      | - 2s probe / <10s  | ---------------------------------+          |
+------------------------------------------------------------------------------|----------+
                                                                               |
                                                                               v
+-----------------------------------------------------------------------------------------+
|                                   CLOUD SERVICE                                         |
|  +--------------------+      +--------------------+      +---------------------------+  |
|  | Ingest Endpoint    | ---> | Idempotent Store   | ---> | Stream Processor          |  |
|  | - Token Auth       |      | - De-duplication   |      | - Online Window Results   |  |
|  | - Block CRC check  |      | - Range tracking   |      | - Reconciliation & Recomp |  |
+-----------------------------------------------------------------------------------------+
```

### Components
1. **Collector**: Maintains a single TCP socket to the mock. Reads alternating sensors every 0.25–0.5s. If a reply is full (400 samples), immediately reads again. Computes CRC-32 on raw wire bytes between `[` and `]`. If corrupt, issues a targeted `from_index` re-read. Tracks sample continuity per sensor/session and logs any missing ranges directly to the loss ledger.
2. **SQLite Store**: Operates in WAL mode (`PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL`). Samples are packed as compact binary blocks (3 bytes per 24-bit sample) rather than single rows to minimize write amplification and extend flash life. Every read cycle commits within a single atomic transaction.
3. **Network Monitor**: Probes cloud `/health` every 2s with a 2s timeout. Moves to offline mode after 3 consecutive failures (< 6s, well under the 10s limit); returns online after 2 consecutive successes (< 4s).
4. **Local Processor**: Active strictly during offline mode. Processes completed windows into section 3 metrics, stamping results with `mode: "offline"`.
5. **Uploader & Catch-up**: Active during online mode. Employs a dual-priority queue: **LIVE** data is streamed immediately (< 1s latency, guaranteeing cloud results within 5s), while **BACKLOG** data is transferred in 60s compressed batches in the background. Authoritative range queries (`GET /state`) prevent redundant uploads.
6. **Cloud Service**: Receives batches idempotently, verifies checksums, stores sample blocks and loss records, and computes online window results with cloud timestamps.

---

## 2. Guarantees and Non-Guarantees

### What Is Guaranteed:
- **Zero Silent Data Loss**: Every sample generated by the mock that reaches the device network stack is committed to disk. Any sample not stored on disk is explicitly cataloged in the loss ledger with an auditable reason (`buffer_overflow`, `corrupt_unrecoverable`, `gap_not_reported_by_source`, `source_restart`, `disk_full`).
- **Exact Mathematical Invariant**: For every session and sensor, the union of stored sample indices and lost sample ranges forms a continuous sequence $0 \dots N$ with zero gaps and zero duplicates (`pipeline verify`).
- **Power-Cut Safety**: With `synchronous = FULL` and WAL fsync on every commit, SQLite guarantees ACID durability against sudden host power removal.
- **Fast Mode Switching**: Network transitions are detected and acted upon in $\le 6$ seconds (spec limit: 10 seconds).
- **Online Latency**: Live windows are processed and delivered to the cloud within 1–2 seconds (spec limit: 5 seconds).
- **Fast Catch-Up**: Backlog transfers achieve $> 35\times$ real-time speed, draining a 2-hour outage in $< 3.5$ minutes (spec limit: 15 minutes).

### What Is Not Guaranteed:
- **Host Hardware Failure**: Physical flash memory / SD-card controller corruption or uncorrectable hardware bit-rot is outside software fault tolerance.
- **Samples Generated During Device Power Down**: If the bedside device is powered off for longer than the mock's 10-second buffer (or if the mock restarts), samples never delivered to the TCP socket cannot be recovered; they are recorded as lost (`buffer_overflow` or `source_restart`).
- **Storage Subsystems Ignoring `fsync`**: We assume the OS and storage controller honor POSIX `fsync` flush barriers.

---

## 3. Power-Cut Analysis & Maximum Loss

### Exact Maximum Data Loss on Sudden Power Cut:
- **Maximum loss**: **$\le 1.0$ second** of received-but-uncommitted data (typically $< 0.25$ seconds).
- **Rationale**:
  - The collector processes and commits data in increments of at most 1 second ($count \le 400$).
  - In normal steady state, the collector polls every 0.25–0.50 s. As soon as a packet is validated, it is committed to SQLite inside a single transaction with `PRAGMA synchronous = FULL`.
  - When the transaction commits, SQLite issues an `fsync()` on the WAL file. Therefore, all data acknowledged prior to the power cut is durable on non-volatile media.
  - The only uncommitted data at the exact moment of a power cut is the packet currently in memory or in the TCP socket buffer. Since each reply contains at most 400 samples (1.0 second), at most 1.0 second of in-flight data is lost.
  - Upon device reboot, the pipeline queries SQLite for `MAX(first_index + count - 1)` and immediately requests `{"cmd": "read", "sensor": s, "from_index": last + 1}`. If the power outage was shorter than 10 seconds and the mock remained running, even this in-flight data is successfully recovered!

---

## 4. Key Spec Ambiguity Resolutions & Decisions

1. **Decimal Rounding Rule**:
   Section 3 requires: *"Round decimal results to 1 decimal place."* Standard IEEE-754 floats suffer from binary representation errors. All outputs are rounded using Python's `decimal` module with `ROUND_HALF_UP` (e.g., `Decimal('1.25').quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) -> 1.3`).
2. **Definition of "Previous Peak"**:
   The rule requires: *"it is at least 100 samples after the previous peak."* We define "previous peak" strictly as the **previous accepted peak**, not an unaccepted candidate. If an unaccepted candidate was used, a spurious noise spike could suppress a legitimate peak 100 samples after the true heartbeat.
3. **Window Finality Boundary**:
   To evaluate whether sample $400(w+1)-1$ (the last sample of window $w$) is a peak, peak detection requires testing its right neighbor at index $400(w+1)$ (the first sample of window $w+1$). Therefore, window $w$ is declared **FINAL** only once sample $400(w+1)$ has been accounted for (either stored or declared lost).
4. **Mode Assignment for Window Straddling a Switch**:
   A window is stamped with the mode (`"online"` or `"offline"`) active at the instant the window reaches finality and is evaluated.
5. **Backfill Recomputation**:
   If out-of-order or backfilled data arrives, windows within the 5-second dependency lookback ($400(w-4)$ to $400(w+1)$) are automatically recomputed and updated.
6. **Disk-Full Safeguard**:
   A 2 MB pre-allocated reserve file (`reserve.dat`) is held on disk. If SQLite reports `SQLITE_FULL`, the reserve file is released, allowing loss records with `reason: "disk_full"` to be successfully committed while halting raw sample intake until disk space is restored.

---

## 5. Free-Tier Cloud Limits & Quota Budget

The cloud architecture is designed for zero-cost operation on standard free tiers (e.g., Fly.io free tier, Render free tier, or AWS Free Tier):
- **Bandwidth**: 2 sensors @ 400 Hz = 6,000 bytes/sec raw payload $\approx$ 518 MB/day. Gzip-compressed, this is $\approx$ 150 MB/day ($\approx$ 4.5 GB/month), well within 100 GB/month free egress allowances.
- **Requests**: Ingested in 1-second batches online and 60-second batches in catch-up $\approx$ 86,400 requests/day, compliant with standard free tier limits.
- **Compute**: Single lightweight Python container / VM requires $< 128$ MB RAM and $< 0.05$ vCPU.
