"""
pipeline/ledger.py - Loss ledger and index space continuity verifier.
Python 3.11+ standard library only.

Verifies Criterion 1 & 2:
- Stored sample ranges UNION loss ranges must form a continuous sequence 0 .. max_index
  without any unexplained gap.
- No stored sample may overlap with a loss range.
- No stored blocks may overlap with each other.
"""

from typing import List, Tuple
from pipeline.store import DeviceStore


def verify_session_continuity(store: DeviceStore, session_id: str) -> Tuple[bool, List[str]]:
    """Proves that for every sensor, stored ranges UNION loss ranges cover every index 0..max
    with no unexplained gap and no overlap.
    Returns (is_valid, list_of_issues).
    """
    issues = []

    for sensor in (1, 2):
        stored_ranges = store.get_stored_ranges(session_id, sensor)
        loss_ranges = store.get_loss_ranges(session_id, sensor)

        if not stored_ranges and not loss_ranges:
            continue

        # Check stored block overlaps
        for i in range(len(stored_ranges) - 1):
            curr_start, curr_end = stored_ranges[i]
            next_start, next_end = stored_ranges[i + 1]
            if curr_end >= next_start:
                issues.append(
                    f"Sensor {sensor}: Overlapping stored blocks [{curr_start}, {curr_end}] "
                    f"and [{next_start}, {next_end}]"
                )

        # Check stored vs loss overlaps
        for s_start, s_end in stored_ranges:
            for l_from, l_to, reason in loss_ranges:
                if l_to is None:
                    if s_end >= l_from:
                        issues.append(
                            f"Sensor {sensor}: Stored block [{s_start}, {s_end}] overlaps with "
                            f"open loss range [{l_from}, null] ({reason})"
                        )
                else:
                    if max(s_start, l_from) <= min(s_end, l_to):
                        issues.append(
                            f"Sensor {sensor}: Stored block [{s_start}, {s_end}] overlaps with "
                            f"loss range [{l_from}, {l_to}] ({reason})"
                        )

        # Determine max index
        max_idx = 0
        if stored_ranges:
            max_idx = max(max_idx, stored_ranges[-1][1])
        for l_from, l_to, _ in loss_ranges:
            if l_to is not None:
                max_idx = max(max_idx, l_to)
            else:
                max_idx = max(max_idx, l_from)

        # Check index continuity 0..max_idx
        # Merge all intervals into coverage map
        # Stored intervals: [(start, end)]
        all_intervals = []
        for start, end in stored_ranges:
            all_intervals.append((start, end, "stored"))
        for l_from, l_to, reason in loss_ranges:
            end = l_to if l_to is not None else max_idx
            all_intervals.append((l_from, end, f"loss:{reason}"))

        all_intervals.sort(key=lambda x: x[0])

        current_expected = 0
        for start, end, label in all_intervals:
            if start > current_expected:
                issues.append(
                    f"Sensor {sensor}: Unexplained gap between index {current_expected} "
                    f"and {start - 1}!"
                )
            current_expected = max(current_expected, end + 1)

    return (len(issues) == 0, issues)
