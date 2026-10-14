# Video Demonstration Guide (video/README.md)

This directory is the designated location for the demonstration video `demo.mp4` required by the assessment guidelines.

---

## Technical Specifications
- **File Name**: `demo.mp4`
- **Maximum Duration**: 5 minutes (strict limit)
- **Resolution**: 1080p (1920x1080) or 720p (1280x720)
- **Codec**: H.264 / AAC MP4

---

## Suggested Shot List & Script (Chronological)

1. **Scene 1 (0:00 - 0:45) - Baseline Online Operation**:
   - Show `pipeline status` outputting JSON with `"mode": "online"`, samples streaming, and backlog = 0.
   - Show live window results appearing in the cloud within 1–2 seconds.

2. **Scene 2 (0:45 - 1:45) - Network Cut & Offline Switching**:
   - Sever network connectivity (e.g. unplug Ethernet or execute `sudo iptables -A OUTPUT -p tcp --dport 8000 -j DROP`).
   - Run `pipeline status` repeatedly: within 6–8 seconds (spec limit: 10s), mode transitions to `"offline"`.
   - Show that local processing starts on the device, producing offline window results.
   - Show backlog counter steadily increasing while collection continues without pause.

3. **Scene 3 (1:45 - 2:45) - Network Restoration & Rapid Catch-Up**:
   - Restore network connectivity (`sudo iptables -F`).
   - Run `pipeline status`: within 2–4 seconds (spec limit: 10s), mode transitions back to `"online"`.
   - Watch backlog drain rapidly at $> 30\times$ real-time speed until backlog returns to 0.

4. **Scene 4 (2:45 - 3:30) - Abrupt Process Crash (`kill -9`)**:
   - In a terminal, execute `sudo killall -9 pipeline` (or `kill -9 <PID>`).
   - Show systemd immediately resurrecting the daemon via `Restart=always`.
   - Show collector resuming from `last_committed_index + 1`, recovering buffered data with zero silent loss.

5. **Scene 5 (3:30 - 4:45) - Sudden Power Cut & Reboot**:
   - Physically pull the power cable from the Raspberry Pi 4 board.
   - Re-insert power and allow the board to boot.
   - Run `pipeline verify --session <ID>`: show exit code 0 (`VERIFICATION PASSED`).
   - Run `pipeline export --session <ID> --from device > device_export.jsonl`.
   - Run official `check_ground_truth device_export.jsonl mock_ground_truth.log`: show 0 silent loss and PASS.

6. **Scene 6 (4:45 - 5:00) - Wrap Up**:
   - Show summary of test evidence and resource usage table.
