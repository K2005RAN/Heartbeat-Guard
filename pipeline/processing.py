"""
pipeline/processing.py - Stream & Window Processing Engine for MayaCardio.
Python 3.11+ standard library only. Shared across bedside device and cloud.

Features:
- Pure functional core: deterministic window computation from sample map and loss ledger.
- Monotonic deque for O(1) rolling 800-sample maximum calculation.
- Exact compliance with Section 3:
    * 1-second windows: samples 400w .. 400w + 399
    * missing_1, missing_2 count
    * complete: true iff missing_1 == 0 and missing_2 == 0
    * mean, min, max per channel over available samples (null if none)
    * magnitude: mean of sqrt(ch1^2 + ch2^2) over available sensor 2 samples (null if none)
    * heart_rate: Section 3 rule over last 5 seconds (samples 400(w-4) .. 400(w+1)-1)
    * ROUND_HALF_UP rounding to 1 decimal place using decimal module.
- Recomputation support for late/backfilled data.
"""

from collections import deque
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from typing import Any, Dict, List, Optional, Tuple

SAMPLE_RATE = 400


def round_1dp(val: Optional[float]) -> Optional[float]:
    """Rounds float to 1 decimal place using ROUND_HALF_UP (decimal module)."""
    if val is None or math.isnan(val) or math.isinf(val):
        return None
    # Use formatted string to avoid float binary representation artifacts
    d = Decimal(f"{val:.8f}")
    return float(d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


class MonotonicMaxDeque:
    """Maintains sliding window maximum in O(1) amortized time using a monotonic deque."""

    def __init__(self, window_size: int = 800):
        self.window_size = window_size
        self.deque: deque = deque()  # stores tuples (index, value)

    def add(self, index: int, value: int):
        # Remove elements outside the window [index - window_size, index - 1]
        while self.deque and self.deque[0][0] < index - self.window_size:
            self.deque.popleft()
        # Maintain decreasing order in deque
        while self.deque and self.deque[-1][1] <= value:
            self.deque.pop()
        self.deque.append((index, value))

    def get_max_before(self, index: int) -> Optional[int]:
        """Returns max of elements strictly before index within the window [index - window_size, index - 1]."""
        while self.deque and self.deque[0][0] < index - self.window_size:
            self.deque.popleft()
        # Filter any elements >= index just in case
        for idx, val in self.deque:
            if idx < index:
                return val
        return None


def detect_peaks_range(s1_ch2_map: Dict[int, int], start_idx: int, end_idx: int,
                       prev_accepted_peak: Optional[int] = None) -> Tuple[List[int], Optional[int]]:
    """Detects peaks within sample range [start_idx, end_idx].
    Peak criteria (Section 3):
      1. Samples i-800 .. i+1 must all be available.
      2. v[i] > v[i-1] and v[i] > v[i+1] (strict).
      3. v[i] > 0.5 * max(samples i-800 .. i-1).
      4. i - prev_peak >= 100 (where prev_peak is previous accepted peak).
    Returns (accepted_peaks, last_accepted_peak).
    """
    accepted_peaks = []
    last_peak = prev_accepted_peak

    # Efficient rolling max
    max_deque = MonotonicMaxDeque(window_size=800)

    # Pre-populate deque for the lookback window of start_idx
    lookback_start = max(0, start_idx - 800)
    for k in range(lookback_start, start_idx):
        if k in s1_ch2_map:
            max_deque.add(k, s1_ch2_map[k])

    for i in range(start_idx, end_idx + 1):
        if i in s1_ch2_map:
            max_deque.add(i, s1_ch2_map[i])

        # Candidate check requires i >= 800 and i in map
        if i < 800 or i not in s1_ch2_map:
            continue

        # Condition 1: 800 samples before it and sample after it must be available
        # Check neighbour samples first
        if (i - 1) not in s1_ch2_map or (i + 1) not in s1_ch2_map:
            continue

        # Condition 2: larger than both neighbours
        v_curr = s1_ch2_map[i]
        if not (v_curr > s1_ch2_map[i - 1] and v_curr > s1_ch2_map[i + 1]):
            continue

        # Condition 4 check (early pruning): at least 100 samples after previous peak
        if last_peak is not None and (i - last_peak) < 100:
            continue

        # Full check of 800 samples before it
        all_800_avail = True
        for k in range(i - 800, i):
            if k not in s1_ch2_map:
                all_800_avail = False
                break
        if not all_800_avail:
            continue

        # Condition 3: larger than half of the maximum of the 800 samples before it
        max_800 = max(s1_ch2_map[k] for k in range(i - 800, i))
        if not (v_curr > 0.5 * max_800):
            continue

        # Accepted peak
        accepted_peaks.append(i)
        last_peak = i

    return accepted_peaks, last_peak


def compute_heart_rate(s1_ch2_map: Dict[int, int], window: int) -> Optional[float]:
    """Computes heart rate for window w:
    Rule (Section 3):
      - null if w < 4
      - null if any sample in last 5 seconds (samples 400(w-4) .. 400(w+1)-1) is missing
      - take peaks in samples 400(w-4) .. 400(w+1)-1
      - null if fewer than 2 peaks
      - heart_rate = 60 / (mean gap between consecutive peaks in seconds)
    """
    if window < 4:
        return None

    range_start = 400 * (window - 4)
    range_end = 400 * (window + 1) - 1  # 2000 samples

    # Check that ALL 2000 samples in the 5-second interval are available
    for i in range(range_start, range_end + 1):
        if i not in s1_ch2_map:
            return None

    # Detect all peaks from index 800 up to range_end
    # Note: to test if sample range_end is a peak, we need range_end + 1 available
    if (range_end + 1) not in s1_ch2_map:
        return None

    peaks, _ = detect_peaks_range(s1_ch2_map, 800, range_end)

    # Filter peaks that fall within the last 5 seconds [range_start, range_end]
    window_peaks = [p for p in peaks if range_start <= p <= range_end]

    if len(window_peaks) < 2:
        return None

    # Calculate mean gap between consecutive peaks in seconds
    k = len(window_peaks)
    gap_samples_sum = window_peaks[-1] - window_peaks[0]
    mean_gap_sec = gap_samples_sum / ((k - 1) * 400.0)

    if mean_gap_sec <= 0:
        return None

    rate = 60.0 / mean_gap_sec
    return round_1dp(rate)


def compute_window_result(window: int, s1_samples: Dict[int, List[int]],
                          s2_samples: Dict[int, List[int]], mode: str,
                          session_id: str) -> Dict[str, Any]:
    """Computes Section 3 outputs for window w = samples 400w .. 400w + 399."""
    start_idx = 400 * window
    end_idx = start_idx + 399

    s1_avail = [s1_samples[i] for i in range(start_idx, end_idx + 1) if i in s1_samples]
    s2_avail = [s2_samples[i] for i in range(start_idx, end_idx + 1) if i in s2_samples]

    missing_1 = 400 - len(s1_avail)
    missing_2 = 400 - len(s2_avail)
    complete = (missing_1 == 0 and missing_2 == 0)

    # Channel stats Sensor 1 (3 channels)
    if s1_avail:
        mean_1 = [round_1dp(sum(s[c] for s in s1_avail) / len(s1_avail)) for c in range(3)]
        min_1 = [min(s[c] for s in s1_avail) for c in range(3)]
        max_1 = [max(s[c] for s in s1_avail) for c in range(3)]
    else:
        mean_1, min_1, max_1 = None, None, None

    # Channel stats Sensor 2 (2 channels)
    if s2_avail:
        mean_2 = [round_1dp(sum(s[c] for s in s2_avail) / len(s2_avail)) for c in range(2)]
        min_2 = [min(s[c] for s in s2_avail) for c in range(2)]
        max_2 = [max(s[c] for s in s2_avail) for c in range(2)]
        # Magnitude: mean of sqrt(ch1^2 + ch2^2) over available sensor 2 samples
        mag_sum = sum(math.sqrt(s[0] ** 2 + s[1] ** 2) for s in s2_avail)
        magnitude = round_1dp(mag_sum / len(s2_avail))
    else:
        mean_2, min_2, max_2 = None, None, None
        magnitude = None

    # Heart rate from sensor 1 channel 2
    s1_ch2_map = {idx: s1_samples[idx][1] for idx in s1_samples.keys()}
    heart_rate = compute_heart_rate(s1_ch2_map, window)

    result = {
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
        # Section 3 aliases for compatibility
        "mean": {"sensor_1": mean_1, "sensor_2": mean_2} if (mean_1 or mean_2) else None,
        "min": {"sensor_1": min_1, "sensor_2": min_2} if (min_1 or min_2) else None,
        "max": {"sensor_1": max_1, "sensor_2": max_2} if (max_1 or max_2) else None,
        "magnitude": magnitude,
        "heart_rate": heart_rate,
    }
    return result
