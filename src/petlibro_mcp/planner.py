"""Pure: combine a rhythm split with a daily total into concrete plan rows."""
from __future__ import annotations


def allocate_portions(fractions, total: int) -> list[int]:
    """Distribute `total` integer portions across `fractions` (largest remainder)."""
    raw = [f * total for f in fractions]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    order = sorted(range(len(fractions)), key=lambda i: raw[i] - floors[i], reverse=True)
    for k in range(remainder):
        floors[order[k]] += 1
    return floors


def plan_rows(split, total_portions: int) -> list[tuple[str, int]]:
    """('HH:MM', grain) rows for each meal, dropping any that round to zero."""
    fractions = [f for _, f in split]
    portions = allocate_portions(fractions, total_portions)
    rows = []
    for (minute, _frac), grain in zip(split, portions):
        if grain <= 0:
            continue
        rows.append((f"{minute // 60:02d}:{minute % 60:02d}", grain))
    return rows
