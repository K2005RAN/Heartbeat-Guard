# MayaCardio – Never Lose a Heartbeat

[![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi%204%20(ARM64)-brightgreen.svg)]()
[![Device Dependencies](https://img.shields.io/badge/Device%20Dependencies-Python%203.11%2B%20Stdlib%20Only-blue.svg)]()
[![Durability](https://img.shields.io/badge/Durability-SQLite%20WAL%20FULL-orange.svg)]()
[![License](https://img.shields.io/badge/License-MIT-lightgrey.svg)]()

> **Submission Notice**: This repository contains the complete, production-grade candidate assessment
> submission for the **MayaCardio – Never Lose a Heartbeat** Software Systems Engineering Challenge.
> In compliance with the assessment guidelines, all automated tests were executed against the
> specification-compliant **STAND-IN** test tools (`biosignal_mock_standin.py` and `check_ground_truth_standin.py`).
> Physical and official hardware tests are detailed in [HUMAN_TODO.md](file:///c:/Users/karan/OneDrive/Desktop/Assesment_software_Mayacardio/HUMAN_TODO.md).

---

## 1. System Overview

A bedside patient monitoring device continuously reads two physiological sensors (Sensor 1: 3 channels, Sensor 2: 2 channels @ 400 Hz, signed 24-bit integers) over TCP and streams data to the cloud. The system is designed to survive real-world hospital telemetry failures:
1. **Zero Silent Data Loss**: Every sample received is committed atomically in SQLite WAL mode. Any lost samples (buffer overflows, sensor dropouts, source restarts) are cataloged in an unbroken loss ledger.
2. **Online Streaming & Low Latency**: In online mode, 1-second window results appear in the cloud within 1–2 seconds (spec limit: 5 seconds).
3. **Local Offline Processing**: When network drops, the device automatically shifts to offline mode within $\le 8.4$ seconds (spec limit: 10 seconds) and computes window results locally.
4. **Fast Catch-Up**: Upon network recovery, backlog is drained in compressed batches at $\ge 34.5\times$ real-time speed, recovering a 2-hour outage in $< 7$ minutes (spec limit: 15 minutes). Live data maintains priority during catch-up.
5. **Power-Cut Safety**: With `synchronous=FULL` and WAL fsync on each commit, maximum data loss on sudden power removal is strictly bounded to received-but-uncommitted data ($\le 1.0$ second).

---

## 2. Repository Layout

```
├── README.md               # Main instructions, installation, usage, repo map
├── AI_DISCLOSURE.md        # Disclosure of Antigravity AI agent usage
├── CLOUD_ACCESS.md         # Instructions for judges to access cloud service
├── HUMAN_TODO.md           # Step-by-step checklist of pending physical actions
├── pipeline/               # Device-side application (Python 3.11+ stdlib only)
│   ├── cli.py              # CLI entry point (status, export, results, verify, run)
│   ├── collector.py        # TCP collector, wire CRC validator, index continuity
│   ├── store.py            # SQLite WAL store, 24-bit packing, reserve file
│   ├── ledger.py           # Loss ledger and continuous sample accounting
│   ├── netmon.py           # End-to-end network health probe (<10s mode switch)
│   ├── uploader.py         # Dual-priority live/backlog HTTP uploader
│   ├── processing.py       # Shared stream processor (incremental deque peak detector)
│   └── local_processor.py  # Device-side offline processor
├── cloud/                  # Cloud ingest service & simulator
│   ├── service.py          # HTTP ingest, window aggregator, export endpoint
│   ├── Dockerfile          # Container definition for Fly.io, Render, AWS, Docker
│   ├── deploy.sh           # One-command deployment script
│   └── README.md           # Cloud documentation
├── deploy/                 # Production device packaging
│   ├── install.sh          # Idempotent installer for Raspberry Pi OS 64-bit
│   ├── mayacardio-pipeline.service # Systemd service unit (auto-restart, cgroups)
│   ├── config.example.toml # Default configuration template
│   └── mayacardio.logrotate# Bounded log rotation
├── tools/                  # Test harness (clearly labeled STAND-IN)
│   ├── mock_standin/       # Faithfully implements Section 6 biosignal-mock
│   └── ground_truth_standin/# Ground truth verification checker
├── tests/                  # Automated test suites
│   ├── test_mock_standin.py
│   ├── test_phase2_collector_store.py
│   ├── test_phase3_faults.py
│   ├── test_phase4_differential_processing.py
│   ├── test_phase5_online_cloud.py
│   ├── test_phase6_mode_switching.py
│   ├── test_phase7_catchup_throughput.py
│   ├── reference_processing.py # Naive reference implementation straight from spec
│   └── faultproxy.py       # Fault-injecting TCP proxy
├── scripts/                # Utility and measurement scripts
│   ├── measure.py / .sh    # Resource usage measurement harness
│   ├── run_all_tests.py / .sh # Full test suite runner
│   ├── validate_against_real_mock.sh # Organizer binary validation script
│   └── make_submission.sh  # Packaging and validation script
├── docs/                   # Architecture, protocols, and audit reports
│   ├── design-note.md      # Full architecture & design note
│   ├── design-note.pdf     # 2-Page Executive Design Note (HARD LIMIT: <= 2 pages)
│   ├── protocol-assumptions.md # Wire framing, CRC, and JSON protocol details
│   ├── test-evidence.md    # Automated test results and evidence matrix
│   └── resource-report.md  # Real measured resource figures
├── evidence/               # Raw timestamped logs from test runs
└── video/                  # Video demonstration directory
    └── README.md           # Instructions and shot list for demo.mp4
```

---

## 3. Fresh Board Installation (Raspberry Pi 4)

The device pipeline requires **Python 3.11+ standard library only**. It has zero external package or compilation dependencies.

### Step 1: Run Idempotent Installer
On a fresh Raspberry Pi OS 64-bit installation (ARM64), run:
```bash
sudo ./deploy/install.sh
```

This installer:
1. Verifies Python 3.11+ and SQLite3.
2. Creates `/var/lib/mayacardio` on persistent storage with a 2 MB emergency reserve file (`reserve.dat`).
3. Installs the `pipeline` executable to `/usr/local/bin/pipeline`.
4. Copies the systemd unit `/etc/systemd/system/mayacardio-pipeline.service` and enables it at boot.

### Step 2: Configure Endpoints
Edit `/etc/mayacardio/config.toml` (or set environment variables):
```toml
[mock]
host = "127.0.0.1"
port = 7100

[cloud]
url = "https://your-cloud-service.fly.dev"
auth_token = "your_secret_bearer_token"
```

### Step 3: Start Daemon
```bash
sudo systemctl start mayacardio-pipeline
sudo systemctl status mayacardio-pipeline
```

---

## 4. Required CLI Commands

The `pipeline` executable is globally available on `$PATH`.

### 1. `pipeline status`
Outputs **ONE JSON line** containing the current mode, samples saved on device, samples delivered to cloud, and backlog count:
```bash
pipeline status
```
```json
{"mode": "online", "samples_saved": 240000, "samples_cloud": 240000, "backlog": 0}
```

### 2. `pipeline export --session <ID> --from device|cloud`
Streams all samples as JSON lines followed by all loss records:
```bash
pipeline export --session 5f0c2a9e --from device
```
Output:
```json
{"sensor": 1, "index": 0, "values": [0, 1, 2]}
{"sensor": 1, "index": 1, "values": [3, 4, 5]}
...
{"sensor": 2, "from_index": 4000, "to_index": 4399, "reason": "buffer_overflow"}
```

Export from cloud:
```bash
pipeline export --session 5f0c2a9e --from cloud
```

### 3. `pipeline results --session <ID>`
Outputs **one JSON line per window and mode** containing Section 3 outputs:
```bash
pipeline results --session 5f0c2a9e
```
```json
{"session_id": "5f0c2a9e", "window": 4, "mode": "online", "missing_1": 0, "missing_2": 0, "complete": true, "mean_1": [720.0, 1200.0, 1560.0], "min_1": [700, 1150, 1500], "max_1": [750, 1250, 1600], "mean_2": [400010.5, 150020.1], "min_2": [399990, 149980], "max_2": [400030, 150050], "magnitude": 427215.3, "heart_rate": 60.0}
```

### 4. `pipeline verify --session <ID>`
Verifies continuous index accounting (stored $\cup$ lost covers $0 \dots N$ without gaps or overlaps):
```bash
pipeline verify --session 5f0c2a9e
```
```
VERIFICATION PASSED: Session 5f0c2a9e has continuous index space with zero silent loss.
```
*(Exit code: 0 on pass, 1 on failure).*

---

## 5. Cloud Service Setup & Deployment

The cloud service runs on any free container runtime (Fly.io, Render, AWS Free Tier, Docker).

### Option A: Local Evaluation Simulator (Zero Dependencies)
```bash
python3 -m cloud.service --port 8000 --db-path /tmp/cloud.db --token mayacardio_secret_token_2026
```

### Option B: Deploy to Fly.io Free Tier
```bash
./cloud/deploy.sh fly
```

### Option C: Run Local Docker Container
```bash
./cloud/deploy.sh local
```

---

## 6. Running Tests & Evidence Generation

Run the entire automated test suite:
```bash
python3 scripts/run_all_tests.py
# or: ./scripts/run_all_tests.sh
```
This executes:
1. `tests/test_mock_standin.py`: Section 6 ramp formulas, signal seeds, wire CRC-32 parsing.
2. `tests/test_phase2_collector_store.py`: Ingest ramp data, SQLite WAL, verify against ground truth checker.
3. `tests/test_phase3_faults.py`: Fault injection (corrupt CRC, duplicate replies, stalls, drops, mock restart, crash recovery, disk full reserve).
4. `tests/test_phase4_differential_processing.py`: Differential comparison of fast deque processor against naive reference.
5. `tests/test_phase5_online_cloud.py`: Token auth, online latency verification ($\le 5$s).
6. `tests/test_phase6_mode_switching.py`: Network cut/restore mode transitions ($\le 10$s) with zero loss.
7. `tests/test_phase7_catchup_throughput.py`: Live-vs-backlog prioritization, $34.5\times$ real-time catch-up, 2-hour drain extrapolation ($< 7$ min).
8. Kill -9 stress loop: 50 consecutive abrupt kills and recoveries during active collection.

All test outputs are recorded in [docs/test-evidence.md](file:///c:/Users/karan/OneDrive/Desktop/Assesment_software_Mayacardio/docs/test-evidence.md) and [evidence/](file:///c:/Users/karan/OneDrive/Desktop/Assesment_software_Mayacardio/evidence/).

---

## 7. Validating Against Official Mock Binaries

When the official `biosignal-mock` and `check_ground_truth` binaries are received, run:
```bash
./scripts/validate_against_real_mock.sh /path/to/biosignal-mock /path/to/check_ground_truth 7100
```
This runs the full integration test using the organizer binaries and logs the verification results.

---

## 8. Resource Measurement

To measure CPU, peak RSS memory, disk writes, and network bandwidth over a 1-hour run:
```bash
./scripts/measure.sh 3600
```
Results are saved to `evidence/resource_measurements.csv` and summarized in `docs/resource-report.md`.
*(Note: A 10-second smoke run was executed in this environment confirming 1.2% CPU and 25 MB RAM. Full 1-hour run on physical Pi 4 is detailed in HUMAN_TODO.md).*
