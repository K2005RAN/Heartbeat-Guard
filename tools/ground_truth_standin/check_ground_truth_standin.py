#!/usr/bin/env python3
"""
# STAND-IN, not the official tools
# tools/ground_truth_standin/check_ground_truth_standin.py
#
# Stand-in ground truth checker for MayaCardio assessment.
# Compares a pipeline export (from device or cloud) against the stand-in mock's
# ground truth log.
#
# Checks performed:
#   1. Missing samples not listed as lost (silent loss).
#   2. Samples listed as lost that were actually stored (contradiction).
#   3. Duplicate sample indices.
#   4. Value discrepancies (corrupted or altered sample values).
#   5. Continuous index space verification: 0 .. max_index covered by stored UNION lost.
#
# Exit code: 0 on success (all checks passed), non-zero on any violation.
"""

import argparse
import json
import sys


def parse_export(export_path: str):
    """Parses a pipeline export file containing sample lines and loss lines.
    Returns:
        stored: dict[int, dict[int, list[int]]] -> sensor -> index -> values
        lost: dict[int, list[tuple[int, int | None, str]]] -> sensor -> [(from_idx, to_idx, reason)]
    """
    stored = {1: {}, 2: {}}
    lost = {1: [], 2: []}
    duplicates = {1: set(), 2: set()}

    with open(export_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Malformed JSON on line {line_no}: {line}") from e

            sensor = rec.get("sensor")
            if sensor not in (1, 2):
                raise ValueError(f"Invalid sensor {sensor} on line {line_no}")

            if "values" in rec:
                idx = rec["index"]
                vals = rec["values"]
                if idx in stored[sensor]:
                    duplicates[sensor].add(idx)
                stored[sensor][idx] = vals
            elif "from_index" in rec:
                from_idx = rec["from_index"]
                to_idx = rec.get("to_index")
                reason = rec.get("reason", "unknown")
                lost[sensor].append((from_idx, to_idx, reason))
            else:
                raise ValueError(f"Unrecognized record on line {line_no}: {rec}")

    return stored, lost, duplicates


def parse_ground_truth(gt_path: str):
    """Parses ground truth log produced by biosignal_mock_standin.
    Returns:
        gt_delivered: dict[int, dict[int, list[int]]] -> sensor -> index -> values
    """
    gt_delivered = {1: {}, 2: {}}
    with open(gt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rec = json.loads(line)
            sensor = rec["sensor"]
            idx = rec["index"]
            vals = rec["values"]
            gt_delivered[sensor][idx] = vals
    return gt_delivered


def is_in_loss_ranges(idx: int, loss_ranges: list[tuple[int, int | None, str]]) -> bool:
    for from_idx, to_idx, _ in loss_ranges:
        if to_idx is None:
            if idx >= from_idx:
                return True
        else:
            if from_idx <= idx <= to_idx:
                return True
    return False


def verify(export_path: str, gt_path: str) -> bool:
    print(f"[STAND-IN CHECKER] Verifying export: {export_path}")
    print(f"[STAND-IN CHECKER] Against ground truth: {gt_path}")

    stored, lost, duplicates = parse_export(export_path)
    gt_delivered = parse_ground_truth(gt_path)

    has_error = False

    # Check 1: Duplicate samples
    for sensor in (1, 2):
        if duplicates[sensor]:
            print(f"FAIL [Criterion 1]: Sensor {sensor} has {len(duplicates[sensor])} duplicate samples!")
            has_error = True

    # Check 2: Values match ground truth exactly
    for sensor in (1, 2):
        mismatched_values = 0
        for idx, stored_vals in stored[sensor].items():
            if idx in gt_delivered[sensor]:
                gt_vals = gt_delivered[sensor][idx]
                if stored_vals != gt_vals:
                    mismatched_values += 1
                    if mismatched_values <= 5:
                        print(f"FAIL [Criterion 1]: Sensor {sensor} sample {idx} value mismatch: stored={stored_vals}, gt={gt_vals}")
        if mismatched_values > 0:
            print(f"FAIL [Criterion 1]: Sensor {sensor} total value mismatches: {mismatched_values}")
            has_error = True

    # Check 3: Every sample mock delivered to the socket must be stored OR listed as lost
    for sensor in (1, 2):
        silent_loss = 0
        for idx in gt_delivered[sensor].keys():
            if idx not in stored[sensor]:
                # Check if it was recorded in lost
                if not is_in_loss_ranges(idx, lost[sensor]):
                    silent_loss += 1
                    if silent_loss <= 5:
                        print(f"FAIL [Criterion 2]: Sensor {sensor} sample {idx} delivered by mock but NOT stored and NOT listed as lost! (SILENT LOSS)")
        if silent_loss > 0:
            print(f"FAIL [Criterion 2]: Sensor {sensor} total silent lost samples: {silent_loss}")
            has_error = True

    # Check 4: No sample listed as lost is simultaneously stored
    for sensor in (1, 2):
        contradictions = 0
        for idx in stored[sensor].keys():
            if is_in_loss_ranges(idx, lost[sensor]):
                contradictions += 1
                if contradictions <= 5:
                    print(f"FAIL [Criterion 2]: Sensor {sensor} sample {idx} is stored but ALSO marked as lost!")
        if contradictions > 0:
            print(f"FAIL [Criterion 2]: Sensor {sensor} total stored-and-lost contradictions: {contradictions}")
            has_error = True

    # Check 5: Continuous index space verification (0 to max_stored)
    for sensor in (1, 2):
        if not stored[sensor] and not lost[sensor]:
            continue
        max_idx = max(stored[sensor].keys()) if stored[sensor] else 0
        for from_idx, to_idx, _ in lost[sensor]:
            if to_idx is not None and to_idx > max_idx:
                max_idx = to_idx

        # Check every index 0..max_idx
        unaccounted_gaps = []
        cur_gap_start = None
        for i in range(max_idx + 1):
            accounted = (i in stored[sensor]) or is_in_loss_ranges(i, lost[sensor])
            if not accounted:
                if cur_gap_start is None:
                    cur_gap_start = i
            else:
                if cur_gap_start is not None:
                    unaccounted_gaps.append((cur_gap_start, i - 1))
                    cur_gap_start = None
        if cur_gap_start is not None:
            unaccounted_gaps.append((cur_gap_start, max_idx))

        if unaccounted_gaps:
            print(f"FAIL [Criterion 2]: Sensor {sensor} has unaccounted gaps: {unaccounted_gaps[:5]}")
            has_error = True

    if not has_error:
        print("[STAND-IN CHECKER] PASS: All criteria verified successfully against stand-in ground truth.")
        for s in (1, 2):
            print(f"  Sensor {s}: {len(stored[s])} stored samples, {len(lost[s])} loss ranges, 0 duplicates, 0 silent losses.")
        return True
    else:
        print("[STAND-IN CHECKER] FAIL: Verification failed!")
        return False


def main():
    parser = argparse.ArgumentParser(description="STAND-IN, not the official tools: check_ground_truth stand-in")
    parser.add_argument("--export", required=True, help="Path to pipeline export JSONL file")
    parser.add_argument("--ground-truth", required=True, help="Path to ground truth JSONL file")
    args = parser.parse_args()

    success = verify(args.export, args.ground_truth)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
