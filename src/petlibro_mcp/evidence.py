"""Pure: when did the schedule last change, and has it helped since?

The device dates its own edits: every enabled feeding-plan row carries an
updateTime, and a single edit transaction stamps them all identically. That is
enough to split history into "before this schedule" and "under this schedule".

A pure-deletion apply leaves surviving rows' updateTime stale, so tools.py also
journals what it wrote; the journal wins when present, and updateTime covers
edits made in the PetLibro phone app.
"""
from __future__ import annotations
from .history import Event

MIN_EVIDENCE_DAYS = 7        # a trim needs at least this much post-change data
MS_PER_DAY = 86_400_000

_IMPROVED_AT = 0.75          # since-rate at or below this fraction is "improving"
_WORSENED_AT = 1.25          # at or above this fraction is "worse"


def last_change_at(plans: list[dict], journal_at_ms: int | None) -> int | None:
    """Epoch ms of the last schedule change, or None if nothing tells us."""
    if journal_at_ms is not None:
        return journal_at_ms
    stamps = [int(p["updateTime"]) for p in plans
              if p.get("enable", True) and p.get("updateTime")]
    return max(stamps) if stamps else None


def split_events(events: list[Event], change_at_ms: int) -> tuple[list[Event], list[Event]]:
    """Partition into (before the change, since the change), each in time order."""
    before = sorted((e for e in events if e.at_ms < change_at_ms),
                    key=lambda e: e.at_ms)
    since = sorted((e for e in events if e.at_ms >= change_at_ms),
                   key=lambda e: e.at_ms)
    return before, since


def window_days(start_ms: int, end_ms: int) -> float:
    """Length of a window in days; 0.0 if it is empty or inverted."""
    return max(0.0, (end_ms - start_ms) / MS_PER_DAY)


def verdict(before_rate: float | None, since_rate: float, days_since: float) -> str:
    """One of too_early / unavailable / improving / worse / no_change.

    too_early wins over everything: with under a week of post-change data there
    is nothing to conclude, whatever the rates happen to say.
    """
    if days_since < MIN_EVIDENCE_DAYS:
        return "too_early"
    if before_rate is None:
        return "unavailable"
    if since_rate <= before_rate * _IMPROVED_AT:
        return "improving"
    if since_rate >= before_rate * _WORSENED_AT:
        return "worse"
    return "no_change"


def may_trim(days_since: float | None) -> bool:
    """Whether a trim is allowed to be proposed at all.

    Independent of whether a 'before' window exists: a cat can have weeks of jam
    evidence under the current schedule and still no comparable window before it.
    That is enough to act on — the before/after is for reporting.
    """
    return days_since is not None and days_since >= MIN_EVIDENCE_DAYS
