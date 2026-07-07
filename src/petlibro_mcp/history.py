"""Fetch-agnostic parsing of PetLibro workRecord history into event series."""
from __future__ import annotations
import json
import re

_DUR = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_ATE_FOR = re.compile(r"ate for ([0-9hms]+)")
_HHMM = re.compile(r"\b(\d{1,2}):(\d{2})")

_EAT_EVENT = "PET_IDENTIFY_LEAVE_EVENT_BIND_PET"
_DISPENSE_TYPE = "GRAIN_OUTPUT_SUCCESS"


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


def parse_work_record(days) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Split day-grouped workRecords into (eats, dispenses) event series.

    eats: (minute_of_day, eating_duration_s); dispenses: (minute_of_day, grain).
    Each event is placed by its formatRecordTime; events lacking a parseable
    one are skipped (they cannot be placed on the daily clock).
    """
    eats: list[tuple[int, int]] = []
    dispenses: list[tuple[int, int]] = []
    for day in days or []:
        for w in day.get("workRecords", []):
            minute = time_of_day_minutes(w.get("formatRecordTime") or "")
            if minute is None:
                continue
            if w.get("eventType") == _EAT_EVENT:
                secs = 0
                try:
                    secs = parse_duration(json.loads(w.get("params") or "").get("seconds", ""))
                except (ValueError, TypeError, AttributeError):
                    secs = 0
                if not secs:
                    mt = _ATE_FOR.search(w.get("content") or "")
                    secs = parse_duration(mt.group(1)) if mt else 0
                eats.append((minute, secs))
            elif w.get("type") == _DISPENSE_TYPE:
                dispenses.append((minute, int(w.get("actualGrainNum") or 0)))
    return eats, dispenses
