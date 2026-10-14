#!/usr/bin/env bash
# scripts/run_all_tests.sh - Run all tests and update test evidence matrix

set -euo pipefail

echo "Running complete test suite..."
python3 scripts/run_all_tests.py
