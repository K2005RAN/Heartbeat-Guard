"""
tests/reference_processing.py - Deliberately naive reference implementation straight from spec.
Used strictly for differential and property testing against pipeline/processing.py.
"""

from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Any, Dict, List, Optional


def ref_round_1dp(val: Optional[float]) -> Optional[float]:
    if val is None or math.isnan(val) or math.isinf(val):
        return None
    d = Decimal(f"{val:.8f}")
    return float(d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def ref_detect_peaks(values_map: Dict[int, int], max_index: int) -> List[int]:
    """Naive peak detection: scans every sample i sequentially, checking all 4 conditions verbatim."""
    accepted_peaks = []
    prev_peak = None

    for i in range(800, max_index):
        # 1. Check if 800 samples before it and sample after it are available
        all_avail = True
        for k in range(i - 800, i + 2):
            if k not in values_map:
                all_avail = False
                break
        if not all_avail:
            continue

        val = values_map[i]
        val_prev = values_map[i - 1]
        val_next = values_map[i + 1]

        # 2. Larger than both neighbours
        if not (val > val_prev and val > val_next):
            continue

        # 3. Larger than half of the maximum of the 800 samples before it
        max_prev = max(values_map[k] for k in range(i - 800, i))
        if not (val > 0.5 * max_prev):
            continue

        # 4. At least 100 samples after the previous accepted peak
        if prev_peak is not None and (i - prev_peak) < 100:
            continue

        accepted_peaks.append(i)
        prev_peak = i

    return accepted_peaks


def ref_compute_window(window: int, s1_samples: Dict[int, List[int]],
                       s2_samples: Dict[int, List[int]], mode: str,
                       session_id: str) -> Dict[str, Any]:
    """Naive reference computation of window w."""
    start_idx = 400 * window
    end_idx = start_idx + 399

    s1_avail = [s1_samples[i] for i in range(start_idx, end_idx + 1) if i in s1_samples]
    s2_avail = [s2_samples[i] for i in range(start_idx, end_idx + 1) if i in s2_samples]

    missing_1 = 400 - len(s1_avail)
    missing_2 = 400 - len(s2_avail)
    complete = (missing_1 == 0 and missing_2 == 0)

    if s1_avail:
        mean_1 = [ref_round_1dp(sum(s[c] for s in s1_avail) / len(s1_avail)) for c in range(3)]
        min_1 = [min(s[c] for s in s1_avail) for c in range(3)]
        max_1 = [max(s[c] for s in s1_avail) for c in range(3)]
    else:
        mean_1, min_1, max_1 = None, None, None

    if s2_avail:
        mean_2 = [ref_round_1dp(sum(s[c] for s in s2_avail) / len(s2_avail)) for c in range(2)]
        min_2 = [min(s[c] for s in s2_avail) for c in range(2)]
        max_2 = [max(s[c] for s in s2_avail) for c in range(2)]
        mag_sum = sum(math.sqrt(s[0] ** 2 + s[1] ** 2) for s in s2_avail)
        magnitude = ref_round_1dp(mag_sum / len(s2_avail))
    else:
        mean_2, min_2, max_2 = None, None, None
        magnitude = None

    # Heart rate
    heart_rate = None
    if window >= 4:
        r_start = 400 * (window - 4)
        r_end = 400 * (window + 1) - 1
        s1_ch2 = {i: s1_samples[i][1] for i in s1_samples if 1 < len(s1_samples[i])}

        # Check all 2000 samples available in 5 seconds
        all_present = all(i in s1_ch2 for i in range(r_start, r_end + 1))
        if all_present and (r_end + 1 in s1_ch2):
            peaks = ref_detect_peaks(s1_ch2, r_end + 1)
            win_peaks = [p for p in peaks if r_start <= p <= r_end]
            if len(win_peaks) >= 2:
                k = len(win_peaks)
                span = win_peaks[-1] - win_peaks[0]
                mean_gap_sec = span / ((k - 1) * 400.0)
                if mean_gap_sec > 0:
                    heart_rate = ref_round_1dp(60.0 / mean_gap_sec)

    return {
        "session_id": session_id,
        "window": window,
        "mode": mode,
        "missing_1": missing_1,
        "missing_2": missing_2,
        "complete": complete,
        "mean_1": mean_1,
        "min_1": min_1,
        "max_1": max_1,
        "mean_2": mean_2,
        "min_2": min_2,
        "max_2": max_2,
        "magnitude": magnitude,
        "heart_rate": heart_rate,
    }
