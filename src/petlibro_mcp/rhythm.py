"""Pure functions: turn per-cat eating events into a rhythm (times + split)."""
from __future__ import annotations

BINS = 48  # 30-minute bins across 24h


def circadian_curve(tod_weights, bins: int = 48, smooth: int = 2) -> list[float]:
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
