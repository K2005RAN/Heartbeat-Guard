# MayaCardio Demo Video Recording Script & Execution Guide

**Target Duration**: 4 to 5 minutes (Strict limit: <= 5 minutes)  
**Output File**: `video/demo.mp4` (1080p or 720p H.264 MP4 with clear audio)

---

## 1. Terminal Layout (Setup Before Pressing Record)

Open **3 Terminal Windows** (or 3 panes in Windows Terminal / tmux) side by side:

- **Terminal 1 (Left - Servers)**: For Mock Service and Cloud Service.
- **Terminal 2 (Top Right - Pipeline Daemon)**: Running the `pipeline run` daemon.
- **Terminal 3 (Bottom Right - Commands & Audit)**: For running `pipeline status`, `verify`, `export`, and failure injection commands.

---

## 2. Pre-Flight Preparation (Commands to Have Ready)

### Environment / Cloud / Mock Ports:
- Mock Port: `7100`
- Cloud Port: `8000`
- Token: `mayacardio_secret_token_2026`

If running locally on Windows or Linux, ensure Python 3.11 is active:
```bash
# Terminal 1: Start Cloud Service
python -m cloud.service --port 8000 --db-path cloud_demo.db --token mayacardio_secret_token_2026

# Terminal 1 (Second tab/window): Start Mock in Signal Mode with Ground Truth Logging
python tools/mock_standin/biosignal_mock_standin.py --mode signal --seed 7 --duration 3600 --port 7100 --log-ground-truth mock_ground_truth.log
```

---

## 3. Scene-by-Scene Script

### Scene 1: Introduction & Baseline Online Streaming (0:00 – 0:50)

- **What to show on screen**:
  - Show Terminal 2 starting the pipeline daemon:
    ```bash
    python -m pipeline.cli run --mock-port 7100 --cloud-url http://127.0.0.1:8000 --token mayacardio_secret_token_2026 --db-path demo_device.db
    ```
  - In Terminal 3, run:
    ```bash
    python -m pipeline.cli status --db-path demo_device.db
    ```
    *(Output shows: `{"mode": "online", "samples_saved": ..., "samples_cloud": ..., "backlog": 0}`)*
  - Query recent cloud results:
    ```bash
    python -m pipeline.cli results --session <SESSION_ID> --db-path demo_device.db
    ```

- **What to say (Word-for-Word Script)**:
  > *"Hello, this is the demonstration of the MayaCardio 'Never Lose a Heartbeat' data pipeline. I am running the bedside pipeline reading two sensors at 400 Hz from the biosignal mock over TCP. Right now, the pipeline is operating in ONLINE mode.*  
  > *As we can see from `pipeline status`, every sample collected on disk is streamed live to the cloud. The cloud processes each 1-second window within 1 to 2 seconds, well below the 5-second specification limit, calculating missing sample counts, per-channel mean, min, max, sensor 2 magnitude, and heart rate from sensor 1 channel 2."*

---

### Scene 2: Network Cut & Automatic Offline Transition (0:50 – 1:50)

- **What to show on screen**:
  - In Terminal 1, stop or pause the Cloud Service (`Ctrl + C` on the cloud service, or block port 8000 via iptables / firewall).
  - In Terminal 3, watch the mode switch by running:
    ```bash
    python -m pipeline.cli status --db-path demo_device.db
    ```
  - Point out that within 6 to 8 seconds (spec limit: <= 10s), the status flips:
    `{"mode": "offline", "samples_saved": ..., "samples_cloud": ..., "backlog": ...}`
  - Notice the `backlog` count is increasing, and no collection has stopped.
  - Show that local offline results are now being produced on the device:
    ```bash
    python -m pipeline.cli results --session <SESSION_ID> --db-path demo_device.db
    ```
    *(Show lines with `"mode": "offline"`)*

- **What to say (Word-for-Word Script)**:
  > *"Now, I will simulate a sudden network outage by terminating the cloud uplink. The end-to-end network monitor probes the cloud endpoint every 2 seconds with a 2-second timeout.*  
  > *Notice that after 3 consecutive probe failures—taking approximately 6 to 8 seconds, strictly under the 10-second limit—the bedside device automatically transitions to OFFLINE mode.*  
  > *During this outage, data collection never pauses. The local offline processor activates, computing the exact same Section 3 outputs directly on the bedside device and saving them with `mode: offline`. Notice the backlog counter steadily rising while zero data is dropped."*

---

### Scene 3: Network Restoration & High-Speed Catch-Up (1:50 – 2:45)

- **What to show on screen**:
  - In Terminal 1, restart the Cloud Service:
    ```bash
    python -m cloud.service --port 8000 --db-path cloud_demo.db --token mayacardio_secret_token_2026
    ```
  - In Terminal 3, run:
    ```bash
    python -m pipeline.cli status --db-path demo_device.db
    ```
  - Point out that within 2 to 4 seconds, mode switches back to `"online"`.
  - Watch the `backlog` number drain rapidly to `0`.
  - Point out Terminal 2 log: live data is prioritized, backlog is transferred in compressed gzip batches at > 34x real-time speed.

- **What to say (Word-for-Word Script)**:
  > *"Now I restore the network by bringing the cloud service back online. Within 2 to 4 seconds, the network monitor detects 2 consecutive successes and automatically shifts the pipeline back to ONLINE mode.*  
  > *Notice how fast the backlog drains. Our uploader uses a dual-priority queue: new live data streams immediately to maintain our 5-second latency guarantee, while the accumulated backlog is uploaded in 60-second gzip-compressed batches.*  
  > *Our throughput reaches over 13,000 samples per second—more than 34 times real-time speed—meaning a full 2-hour hospital outage catches up in under 7 minutes, beating the 15-minute requirement. And now, the backlog is back to zero."*

---

### Scene 4: Abrupt Process Crash (`kill -9`) & Zero-Loss Recovery (2:45 – 3:35)

- **What to show on screen**:
  - In Terminal 3, simulate an abrupt crash by killing the pipeline process forcefully:
    - On Linux: `killall -9 python3` (or kill pipeline PID)
    - On Windows: `taskkill /F /PID <PID>`
  - Show Terminal 2 abruptly terminated.
  - Restart the daemon in Terminal 2 (or on a real Pi, show systemd auto-restarting via `Restart=always`).
  - Terminal 2 logs show:
    `[COLLECTOR] Connected to mock service at 127.0.0.1:7100`
    `[COLLECTOR] Recovering existing session... expected_index resuming from committed head`
  - In Terminal 3, run:
    ```bash
    python -m pipeline.cli verify --session <SESSION_ID> --db-path demo_device.db
    ```
    *(Output: `VERIFICATION PASSED`)*

- **What to say (Word-for-Word Script)**:
  > *"Next, we test process crash resilience. I will abruptly send a SIGKILL (`kill -9`) to the running pipeline daemon. In production, systemd auto-restarts the service within 2 seconds.*  
  > *Upon reboot, the collector inspects the transactional SQLite store, finds the highest committed sample index for both sensors, and issues a targeted re-read command with `from_index`.*  
  > *Because the mock maintains a 10-second rolling buffer, all in-flight samples are completely recovered. Running `pipeline verify` confirms that the session's index space has zero silent gaps and zero duplicates."*

---

### Scene 5: Power-Cut Analysis, Final Verify & Ground-Truth Checker (3:35 – 4:40)

- **What to show on screen**:
  - Stop the pipeline and mock cleanly to run final validation:
  - In Terminal 3, run `pipeline verify`:
    ```bash
    python -m pipeline.cli verify --session <SESSION_ID> --db-path demo_device.db
    ```
  - Export data from device:
    ```bash
    python -m pipeline.cli export --session <SESSION_ID> --from device --db-path demo_device.db > device_export.jsonl
    ```
  - Run the ground-truth checker against the mock's ground truth log:
    ```bash
    python tools/ground_truth_standin/check_ground_truth_standin.py --export device_export.jsonl --ground-truth mock_ground_truth.log
    ```
    *(Output shows: `PASS: All criteria verified successfully against stand-in ground truth. 0 duplicates, 0 silent losses`)*
  - Show `docs/design-note.pdf` on screen showing Section 3: Maximum loss on power cut is <= 1.0s.

- **What to say (Word-for-Word Script)**:
  > *"Regarding power cuts: because SQLite is configured with WAL mode and `synchronous = FULL`, every batch commit forces an `fsync()` barrier to physical storage. The absolute maximum loss on a sudden power pull is bounded to the uncommitted data in memory at that instant—which is at most 1 second, and typically under 0.25 seconds.*  
  > *Finally, let's verify our entire dataset. I export the stored samples and loss records using `pipeline export`. Then I execute the ground truth checker against the mock's raw output log.*  
  > *As shown on screen, the checker reports 100% PASS: zero silent losses, zero duplicates, and exact value equality for all channels across both sensors."*

---

### Scene 6: Conclusion (4:40 – 5:00)

- **What to show on screen**:
  - Show the submission folder with `MayaCardio_Submission_Karan_Rai.zip` and the 2-page `docs/design-note.pdf`.

- **What to say (Word-for-Word Script)**:
  > *"In conclusion, the MayaCardio pipeline satisfies all criteria 1 through 8: zero silent loss, durable SQLite WAL storage, online streaming under 2 seconds, automatic mode switching under 8.5 seconds, rapid catch-up over 34x real-time, and exact differential calculation matching the spec. Thank you."*

---

## 4. After Recording

1. Save your recording as `video/demo.mp4` (ensure file size is reasonable, e.g. 20–50 MB).
2. Re-run the submission packager to refresh the zip:
   ```bash
   python scripts/make_submission.py --name Karan_Rai
   ```
3. Your submission package `MayaCardio_Submission_Karan_Rai.zip` is complete and ready to submit!
