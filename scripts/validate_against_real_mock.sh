#!/usr/bin/env bash
# scripts/validate_against_real_mock.sh
#
# Validates the pipeline against the OFFICIAL biosignal-mock and check_ground_truth binaries
# once received from the assessment organizers.
#
# Usage:
#   ./scripts/validate_against_real_mock.sh /path/to/biosignal-mock /path/to/check_ground_truth [PORT]

set -euo pipefail

REAL_MOCK="${1:-}"
REAL_CHECKER="${2:-}"
PORT="${3:-7100}"

if [[ -z "$REAL_MOCK" || -z "$REAL_CHECKER" ]]; then
    echo "ERROR: Missing arguments."
    echo "Usage: $0 /path/to/biosignal-mock /path/to/check_ground_truth [PORT]"
    exit 1
fi

if [[ ! -x "$REAL_MOCK" ]]; then
    echo "ERROR: Official mock '$REAL_MOCK' not found or not executable."
    exit 1
fi

if [[ ! -x "$REAL_CHECKER" ]]; then
    echo "ERROR: Official checker '$REAL_CHECKER' not found or not executable."
    exit 1
fi

echo "======================================================================"
echo "RUNNING VALIDATION AGAINST OFFICIAL ORGANIZER BINARIES"
echo "Mock:    $REAL_MOCK"
echo "Checker: $REAL_CHECKER"
echo "Port:    $PORT"
echo "======================================================================"

WORK_DIR=$(mktemp -d /tmp/mayacardio_real_val_XXXXXX)
DB_PATH="$WORK_DIR/pipeline_test.db"
EXPORT_PATH="$WORK_DIR/device_export.jsonl"
GT_PATH="$WORK_DIR/mock_ground_truth.log"

cleanup() {
    echo "Cleaning up processes..."
    kill "$MOCK_PID" 2>/dev/null || true
    kill "$PIPE_PID" 2>/dev/null || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo "--> Step 1: Starting official biosignal-mock in ramp mode (seed 7, 30s)..."
"$REAL_MOCK" --mode ramp --seed 7 --duration 30 --port "$PORT" > "$GT_PATH" 2>&1 &
MOCK_PID=$!
sleep 1

echo "--> Step 2: Running pipeline collector daemon for 20 seconds..."
python3 -m pipeline.cli run --mock-port "$PORT" --db-path "$DB_PATH" --offline-only &
PIPE_PID=$!
sleep 20

echo "--> Step 3: Stopping collector..."
kill -INT "$PIPE_PID" || true
wait "$PIPE_PID" 2>/dev/null || true

echo "--> Step 4: Exporting stored data from device database..."
SESSION_ID=$(python3 -c "import sqlite3; c=sqlite3.connect('$DB_PATH'); print(c.execute('SELECT session_id FROM sessions LIMIT 1').fetchone()[0])")
python3 -m pipeline.cli export --session "$SESSION_ID" --from device --db-path "$DB_PATH" > "$EXPORT_PATH"

echo "--> Step 5: Running pipeline verify..."
python3 -m pipeline.cli verify --session "$SESSION_ID" --db-path "$DB_PATH"

echo "--> Step 6: Validating with official check_ground_truth..."
"$REAL_CHECKER" "$EXPORT_PATH" "$GT_PATH"

echo "======================================================================"
echo "SUCCESS: Pipeline successfully validated against official organizer tools!"
echo "======================================================================"
