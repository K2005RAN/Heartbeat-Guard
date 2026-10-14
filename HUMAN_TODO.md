# Human Candidate Action Checklist (HUMAN_TODO.md)

> **CRITICAL RULE**: In accordance with the assessment's Absolute Rules ("HONESTY OVER APPEARANCE"),
> any test or action that requires physical hardware, organizer-provided binaries, or third-party cloud
> credentials cannot be fabricated. Execute the following steps in this exact order:

---

## Step 1: Validate Against Official Organizer Mock & Checker
1. Obtain the official `biosignal-mock` and `check_ground_truth` binaries from the organizers.
2. Make them executable: `chmod +x biosignal-mock check_ground_truth`.
3. Run the automated validator:
   ```bash
   ./scripts/validate_against_real_mock.sh /path/to/biosignal-mock /path/to/check_ground_truth 7100
   ```
4. If there are any framing, whitespace, or protocol discrepancies, update `pipeline/collector.py` and `docs/protocol-assumptions.md`.

---

## Step 2: Deploy Cloud Service to Free Cloud Provider
1. Create a free account on [Fly.io](https://fly.io) or [Render](https://render.com).
2. Deploy the cloud container using `cloud/deploy.sh` or standard Docker deploy:
   ```bash
   ./cloud/deploy.sh fly
   ```
3. Generate a strong secret bearer token.
4. Update `CLOUD_ACCESS.md` with your live deployment URL (do not commit the raw token).
5. Provide the token separately in your submission form/email to the judges.

---

## Step 3: Install on Physical Raspberry Pi 4 (ARM64)
1. Flash a fresh Raspberry Pi OS 64-bit image (Debian Bookworm ARM64) to an SD card or SSD.
2. Clone or copy this repository to the board.
3. Install following `README.md` instructions exclusively:
   ```bash
   sudo ./deploy/install.sh
   ```
4. Verify systemd daemon starts cleanly:
   ```bash
   sudo systemctl status mayacardio-pipeline
   ```

---

## Step 4: Execute Real Physical Power-Pull & Failure Tests
1. Start the mock in signal mode on the Raspberry Pi:
   ```bash
   biosignal-mock --mode signal --seed 7 --duration 3600 --port 7100 &
   ```
2. Start the pipeline in online mode.
3. Physically pull the power cable from the Raspberry Pi during active streaming.
4. Re-insert power and reboot the board.
5. Export data and verify zero silent loss:
   ```bash
   pipeline verify --session <SESSION_ID>
   pipeline export --session <SESSION_ID> --from device > device_powercut_export.jsonl
   check_ground_truth device_powercut_export.jsonl mock_ground_truth.log
   ```
6. Update `docs/test-evidence.md` with the observed recovery logs and change status from `NOT RUN` to `PASS`.

---

## Step 5: Execute 1-Hour Resource Measurement on ARM64
1. On the physical Raspberry Pi 4, run the 1-hour resource measurement under resource constraints:
   ```bash
   ./scripts/measure.sh 3600 /path/to/biosignal-mock
   ```
2. Inspect the generated `evidence/resource_measurements.csv` and `docs/resource-report.md`.
3. Confirm average CPU is well under 400% (typically 1–3%) and memory is under 50 MB (far below 2 GB limit).
4. Update `docs/resource-report.md` with the final measured values.

---

## Step 6: Record Demo Video (<= 5 Minutes)
1. Record a clear screen capture / camera demonstration ($\le 5$ minutes) following `video/README.md`:
   - Scene 1: `pipeline status` showing `mode: online`.
   - Scene 2: Network disconnection (cable unplug or router disconnect) $\rightarrow$ automatic switch to `mode: offline` within 10s.
   - Scene 3: Network restoration $\rightarrow$ automatic switch to `mode: online` within 10s $\rightarrow$ backlog drains to 0.
   - Scene 4: Abrupt process crash (`sudo killall -9 pipeline`) $\rightarrow$ systemd auto-restarts $\rightarrow$ continuous collection resumed.
   - Scene 5: Physical power pull and reboot $\rightarrow$ `pipeline verify` passes with zero silent loss $\rightarrow$ `check_ground_truth` passes.
2. Save the final video as `video/demo.mp4` (or place directly into the submission zip).

---

## Step 7: Final Review & Generate Submission Zip
1. Review `AI_DISCLOSURE.md`, `README.md`, and `docs/design-note.pdf`.
2. Generate the submission zip:
   ```bash
   ./scripts/make_submission.sh
   ```
3. Verify the zip output and submit.
