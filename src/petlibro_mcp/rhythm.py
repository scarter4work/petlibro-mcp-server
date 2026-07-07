"""Pure functions: turn per-cat eating events into a rhythm (times + split)."""
from __future__ import annotations

BINS = 48  # 30-minute bins across 24h


def circadian_curve(tod_weights: list[tuple[int, int]], bins: int = BINS, smooth: int = 2) -> list[float]:
    """Duration-weighted intensity curve over the 24h clock.

    tod_weights: (minute_of_day, weight). Returns a length-`bins` list; when
    `smooth > 0`, applies a circular moving average of window 2*smooth+1.
    """
    width = 1440 // bins
    hist = [0.0] * bins
    for minute, weight in tod_weights:
        hist[(minute // width) % bins] += float(weight)
    if smooth <= 0:
        return hist
    out = [0.0] * bins
    window = 2 * smooth + 1
    for i in range(bins):
        acc = 0.0
        for d in range(-smooth, smooth + 1):
            acc += hist[(i + d) % bins]
        out[i] = acc / window
    return out


def _circ_dist(a: int, b: int, bins: int) -> int:
    """Minimum circular distance between indices a and b in a cycle of bins."""
    d = abs(a - b) % bins
    return min(d, bins - d)


def find_peaks(curve: list[float], max_meals: int = 6, min_separation_bins: int = 3) -> list[int]:
    """Bin indices of meal peaks: circular local maxima, tallest first, spaced.

    Returns sorted bin indices of the chosen meal peaks, enforcing minimum
    separation in circular distance and limiting to max_meals peaks.
    """
    bins = len(curve)
    cands = [
        i for i in range(bins)
        if curve[i] > 0
        and curve[i] >= curve[(i - 1) % bins]
        and curve[i] > curve[(i + 1) % bins]
    ]
    cands.sort(key=lambda i: curve[i], reverse=True)
    chosen: list[int] = []
    for i in cands:
        if all(_circ_dist(i, j, bins) >= min_separation_bins for j in chosen):
            chosen.append(i)
        if len(chosen) >= max_meals:
            break
    return sorted(chosen)


def split_at_peaks(curve: list[float], peaks: list[int]) -> list[tuple[int, float]]:
    """Fraction of the day's food per peak, by mass nearest each peak."""
    if not peaks:
        return []
    bins = len(curve)
    width = 1440 // bins
    mass = {p: 0.0 for p in peaks}
    for i in range(bins):
        nearest = min(peaks, key=lambda p: _circ_dist(i, p, bins))
        mass[nearest] += curve[i]
    total = sum(mass.values()) or 1.0
    return [(p * width, mass[p] / total) for p in sorted(peaks)]
