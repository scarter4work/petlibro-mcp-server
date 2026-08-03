"""Pure: how much to cut from a cat's daily portions, and from which meals.

The signal is GRAIN_OUTLET_BLOCK_REMOVED ("the grain outlet is blocked, cleared").
A block means food was already sitting in the chute at that hour — cumulative
surplus, i.e. fed too often around that time.

Measured across the live fleet, jam rate does NOT track meal size: colby's
jammiest slot is a 1-portion meal, while ricco dispenses 5 portions at 05:00 and
jammed once in 20 days. So the cut is placed by hour-of-day jam density, never by
which meal happens to be largest.
"""
from __future__ import annotations
from .rhythm import _circ_dist

MIN_DAILY_PORTIONS = 8      # hard floor on any cat's daily total
TRIM_PER_JAM_PER_DAY = 2    # portions removed per jam-per-day of evidence

_MINUTES_PER_DAY = 1440     # the circular clock is measured in minutes


def jam_rate(block_count: int, window_days: float) -> float:
    """Blocks per day. Zero for an empty window rather than dividing by zero."""
    if window_days <= 0:
        return 0.0
    return block_count / window_days


def trim_amount(rate: float, current_total: int) -> int:
    """Portions to remove from the daily total, floored at MIN_DAILY_PORTIONS.

    Rounding supplies the dead band for free: below 0.25 jams/day this is 0, so a
    feeder that rarely jams is never trimmed on noise.
    """
    want = round(TRIM_PER_JAM_PER_DAY * rate)
    room = current_total - MIN_DAILY_PORTIONS
    return max(0, min(want, room))


def _to_minute(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def attribute_blocks(block_minutes: list[int], meal_times: list[str]) -> dict[str, int]:
    """Count each block against the nearest meal on the 24h clock.

    Circular distance, so a 23:50 block belongs to a 00:10 meal — 20 minutes
    away, not 23 hours.
    """
    if not meal_times:
        return {}
    counts: dict[str, int] = {}
    for b in block_minutes:
        nearest = min(meal_times,
                      key=lambda t: _circ_dist(b, _to_minute(t), _MINUTES_PER_DAY))
        counts[nearest] = counts.get(nearest, 0) + 1
    return counts


def trim_schedule(rows: list[tuple[str, int]], blocks_by_time: dict[str, int],
                  trim: int) -> list[tuple[str, int]]:
    """Remove `trim` portions, jammiest meal first, never below 1 per meal.

    A meal trimmed to 0 would be DROPPED by planner.plan_rows rather than
    shrunk, silently deleting a feeding, so 1 is the per-meal floor. When every
    meal has reached it the remaining trim is abandoned — the daily floor in
    trim_amount is the backstop, not this loop.
    """
    portions = dict(rows)
    # jammiest first; ties broken by time so the result is deterministic
    order = sorted(portions, key=lambda t: (-blocks_by_time.get(t, 0), t))
    left = trim
    while left > 0:
        took = False
        for t in order:
            if left == 0:
                break
            if portions[t] > 1:
                portions[t] -= 1
                left -= 1
                took = True
        if not took:
            break  # every meal is at 1; nothing more to give
    return sorted(portions.items(), key=lambda kv: kv[0])
