#!/usr/bin/env bash
# scripts/make_submission.sh - Package and validate MayaCardio submission archive
# Usage: ./scripts/make_submission.sh [CANDIDATE_NAME]

set -euo pipefail

NAME="${1:-Karan_Rai}"

python3 scripts/make_submission.py --name "$NAME"
