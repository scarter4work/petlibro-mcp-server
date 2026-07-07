"""Fetch-agnostic parsing of PetLibro workRecord history into event series."""
from __future__ import annotations
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

_DUR = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
_ATE_FOR = re.compile(r"ate for ([0-9hms]+)")

_EAT_EVENT = "PET_IDENTIFY_LEAVE_EVENT_BIND_PET"
_DISPENSE_TYPE = "GRAIN_OUTPUT_SUCCESS"


def parse_duration(text: str) -> int:
    """Parse a PetLibro duration token like '01m37s' into whole seconds."""
    m = _DUR.fullmatch((text or "").strip())
    if not m:
        return 0
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


def parse_work_record(days) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
    """Split day-grouped workRecords into (eats, dispenses) event series.

    eats: (ts_epoch_s, eating_duration_s); dispenses: (ts_epoch_s, grain).
    """
    eats: list[tuple[float, int]] = []
    dispenses: list[tuple[float, int]] = []
    for day in days or []:
        for w in day.get("workRecords", []):
            ts = (w.get("recordTime") or 0) / 1000
            if w.get("eventType") == _EAT_EVENT:
                secs = 0
                try:
                    secs = parse_duration(json.loads(w.get("params") or "").get("seconds", ""))
                except (ValueError, TypeError, AttributeError):
                    secs = 0
                if not secs:
                    mt = _ATE_FOR.search(w.get("content") or "")
                    secs = parse_duration(mt.group(1)) if mt else 0
                eats.append((ts, secs))
            elif w.get("type") == _DISPENSE_TYPE:
                dispenses.append((ts, int(w.get("actualGrainNum") or 0)))
    return eats, dispenses


def time_of_day_minutes(ts_epoch_s: float, tz_name: str) -> int:
    """Minutes since local midnight (0-1439) for an epoch-second timestamp."""
    dt = datetime.fromtimestamp(ts_epoch_s, ZoneInfo(tz_name))
    return dt.hour * 60 + dt.minute
