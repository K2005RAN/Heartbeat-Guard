#!/usr/bin/env bash
# scripts/measure.sh - Resource measurement runner
# Usage: ./scripts/measure.sh [DURATION_SECONDS] [OFFICIAL_MOCK_BIN]

set -euo pipefail

DURATION="${1:-3600}"
MOCK_BIN="${2:-}"

OUTPUT_CSV="evidence/resource_measurements.csv"
SUMMARY_MD="docs/resource-report.md"

mkdir -p evidence docs

echo "======================================================================"
echo "RUNNING PIPELINE RESOURCE MEASUREMENT"
echo "Duration: $DURATION seconds"
echo "======================================================================"

ARGS=("--duration" "$DURATION" "--output-csv" "$OUTPUT_CSV" "--summary-out" "$SUMMARY_MD")
if [[ -n "$MOCK_BIN" ]]; then
    ARGS+=("--mock-bin" "$MOCK_BIN")
fi

python3 scripts/measure.py "${ARGS[@]}"

echo "Measurements written to $OUTPUT_CSV"
echo "Summary written to $SUMMARY_MD"
