# AI Disclosure Statement

## Development Methodology & Tools

In compliance with the assessment guidelines and ethical engineering practices, this document discloses that this submission was engineered using **Antigravity**, an advanced AI coding assistant developed by Google DeepMind.

### Role of the AI Agent
Antigravity operated as an autonomous pair-programmer under human direction to:
1. Parse and extract technical requirements and invariants from the assessment specification (*MayaCardio_Software_Systems_Engg_Candidate_Assessment_Oct_2026.docx*).
2. Author the stand-in mock service (`tools/mock_standin/biosignal_mock_standin.py`) and stand-in ground truth verification checker (`tools/ground_truth_standin/check_ground_truth_standin.py`) based strictly on Section 6.
3. Implement the bedside device pipeline (`pipeline/store.py`, `pipeline/collector.py`, `pipeline/ledger.py`, `pipeline/netmon.py`, `pipeline/uploader.py`, `pipeline/local_processor.py`, `pipeline/cli.py`) using Python 3.11+ standard library only.
4. Implement the cloud service (`cloud/service.py`, `cloud/Dockerfile`, `cloud/deploy.sh`).
5. Develop unit, integration, differential, fault injection, and stress test suites.
6. Generate documentation, architecture notes, and deployment scripts.

### Human Candidate Responsibilities
The human candidate must:
1. Conduct code review and verify all algorithms, formulas, and protocol assumptions.
2. Obtain the official `biosignal-mock` and `check_ground_truth` binaries from the organizers and run `scripts/validate_against_real_mock.sh`.
3. Deploy the cloud service to their personal free cloud account (e.g., Fly.io or Render) and populate `CLOUD_ACCESS.md`.
4. Perform the physical tests on real Raspberry Pi 4 hardware (power cut pull test, 1-hour resource measurement).
5. Record the demonstration video (`video/demo.mp4`) as detailed in `video/README.md`.
