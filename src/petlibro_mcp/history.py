"""Fetch-agnostic parsing of PetLibro workRecord history into event series."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass

_DUR = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_ATE_FOR = re.compile(r"ate for ([0-9hms]+)")
_HHMM = re.compile(r"\b(\d{1,2}):(\d{2})")

_EAT_EVENT = "PET_IDENTIFY_LEAVE_EVENT_BIND_PET"
_DISPENSE_TYPE = "GRAIN_OUTPUT_SUCCESS"
_BLOCK_TYPE = "GRAIN_OUTLET_BLOCK_REMOVED"
_MANUAL_EVENT = "MANUAL_FEEDING_SUCCESS"
_GRAIN_LOW_EVENTS = frozenset({
    "FEEDING_PLAN_GRAIN_NOT_ENOUGH",
    "MANUAL_FEEDING_GRAIN_NOT_ENOUGH",
})


@dataclass(frozen=True)
class Event:
    """One workRecord entry, placed on both the 24h clock and the calendar.

    `minute` is minute-of-day from the device wall clock (the same clock the
    feeding plans use); `at_ms` is the absolute recordTime, needed to split
    history before/after a schedule change. `amount` is eating seconds for
    "eat", portions for either dispense kind, and 0 otherwise.
    """
    kind: str
    minute: int
    at_ms: int
    amount: int


def parse_duration(text: str) -> int:
    """Parse a PetLibro duration token like '01m37s' into whole seconds."""
    m = _DUR.fullmatch((text or "").strip())
    if not m:
        return 0
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


def time_of_day_minutes(format_record_time: str) -> int | None:
    """Minutes since midnight (0-1439) from a 'YYYY-MM-DD HH:MM[:SS]' string.

    Uses PetLibro's own device wall-clock (fixed offset), which is the same
    clock the feeding-plan executionTime values use. Returns None if the
    string has no parseable HH:MM.
    """
    m = _HHMM.search(format_record_time or "")
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mn < 60):
        return None
    return h * 60 + mn


def _eat_seconds(w: dict) -> int:
    """Eating duration for a visit: the params JSON first, the content string second."""
    secs = 0
    try:
        secs = parse_duration(json.loads(w.get("params") or "").get("seconds", ""))
    except (ValueError, TypeError, AttributeError):
        secs = 0
    if not secs:
        mt = _ATE_FOR.search(w.get("content") or "")
        secs = parse_duration(mt.group(1)) if mt else 0
    return secs


def classify(w: dict) -> list[Event]:
    """Typed events for ONE workRecord — 0, 1 or 2 of them.

    Zero when the record has no parseable formatRecordTime (it cannot be placed
    on the daily clock) or is a type we do not model. Two when a dispense also
    carries the hopper-running-out warning: it did deliver food, and it also
    warns the hopper is emptying.

    Split out of parse_events so callers that need the originating record
    alongside its events — the SQLite store, which keys on the record id — do
    not have to re-implement this classification and drift from it.
    """
    minute = time_of_day_minutes(w.get("formatRecordTime") or "")
    if minute is None:
        return []
    at_ms = int(w.get("recordTime") or 0)
    etype = w.get("eventType")
    wtype = w.get("type")
    if etype == _EAT_EVENT:
        return [Event("eat", minute, at_ms, _eat_seconds(w))]
    if wtype == _DISPENSE_TYPE:
        grain = int(w.get("actualGrainNum") or 0)
        kind = "dispense_manual" if etype == _MANUAL_EVENT else "dispense_plan"
        out = [Event(kind, minute, at_ms, grain)]
        if etype in _GRAIN_LOW_EVENTS:
            out.append(Event("grain_low", minute, at_ms, 0))
        return out
    if wtype == _BLOCK_TYPE:
        return [Event("block", minute, at_ms, 0)]
    return []


def iter_records(days: list[dict] | None):
    """Yield every raw workRecord across the day groups, ungrouped."""
    for day in days or []:
        for w in day.get("workRecords", []):
            yield w


def parse_events(days: list[dict] | None) -> list[Event]:
    """Flatten day-grouped workRecords into a typed event stream.

    Records without a parseable formatRecordTime are skipped — they cannot be
    placed on the daily clock. A grain-low dispense yields TWO events: it did
    deliver food, and it also warns that the hopper is running out.
    """
    out: list[Event] = []
    for w in iter_records(days):
        out.extend(classify(w))
    return out


def hopper_warned_on_last_dispense(days: list[dict] | None) -> bool | None:
    """Did the most recent dispense carry the hopper-running-out warning?

    The firmware reports a low hopper by tagging the dispense record itself:
    the food is still delivered (``actualGrainNum == expectGrainNum``), so the
    eventType is the only thing that says anything is wrong. Judging only the
    LATEST dispense makes this self-clearing — the first feed after a refill
    comes back as a plain success. Returns None when there is no dispense to
    judge.
    """
    latest_at: int | None = None
    warned: bool | None = None
    for day in days or []:
        for w in day.get("workRecords", []):
            if w.get("type") != _DISPENSE_TYPE:
                continue
            at_ms = int(w.get("recordTime") or 0)
            if latest_at is None or at_ms > latest_at:
                latest_at = at_ms
                warned = w.get("eventType") in _GRAIN_LOW_EVENTS
    return warned


def parse_work_record(days: list[dict] | None) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Split day-grouped workRecords into (eats, dispenses) event series.

    eats: (minute_of_day, eating_duration_s); dispenses: (minute_of_day, grain).
    Kept for callers that only need the 24h-clock view. `dispenses` deliberately
    includes BOTH scheduled and manual feeds, preserving the original contract —
    callers measuring schedule *delivery* must use parse_events and filter to
    "dispense_plan" instead, or hand-feeding will look like the plan working.
    """
    events = parse_events(days)
    eats = [(e.minute, e.amount) for e in events if e.kind == "eat"]
    dispenses = [(e.minute, e.amount) for e in events
                 if e.kind in ("dispense_plan", "dispense_manual")]
    return eats, dispenses
