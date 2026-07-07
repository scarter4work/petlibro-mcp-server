# Rhythm Feeding — Phase 1: Analyze (read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute each cat's natural eating rhythm from ~60 days of cloud history and surface a recommend-only `analyze_rhythm` MCP tool that reports the current schedule alongside a rhythm-tailored one (same daily total, re-timed and re-split). Zero writes to any device.

**Architecture:** Four small, mostly-pure modules — `history.py` (fetch + parse work records into event series), `rhythm.py` (duration-weighted circadian curve → peaks → split), `planner.py` (split × total → concrete plan rows), and an `analyze_rhythm` tool that composes them. Two thin read-only methods are added to the `PetLibroClient` facade; the vendored API client is not modified.

**Tech Stack:** Python 3.10+, stdlib only (`re`, `json`, `datetime`, `zoneinfo`), `mcp>=1.9`, pytest + pytest-asyncio (`asyncio_mode = "auto"`).

## Global Constraints

- Python `>=3.10`; stdlib only for the new logic — no new third-party dependencies.
- Work entirely in device **portions** (`grainNum`); never convert to cups (this account sets `portions_per_cup = 1` and the ambiguity is irrelevant to rhythm).
- All rhythm/planner logic is **pure** (no I/O, no `datetime.now`) so it is deterministic and unit-testable; the only time-dependent code lives in the client facade (fetch window).
- Errors surface, never swallow: tool results carry `{"ok": False, "error": "<Type>: <msg>"}`, matching the existing `tools.py` convention.
- Do **not** modify `src/petlibro_mcp/vendored/` — it is a vendored external dependency.
- Tests use the existing patterns: `AsyncMock` fake clients, keyword-constructed `Config`, `pytest` under `asyncio_mode = "auto"`.
- Phase 1 is read-only: no schedule writes, no controller/state (those are Phases 2 and 3).

---

### Task 1: `history.py` — parse work records into event series

**Files:**
- Create: `src/petlibro_mcp/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `parse_duration(text: str) -> int` — `"01m37s"` → `97`, `"24s"` → `24`, `"1h02m03s"` → `3723`, unparseable → `0`.
  - `parse_work_record(days: list[dict]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]` — returns `(eats, dispenses)`; `eats` are `(minute_of_day, duration_s)`, `dispenses` are `(minute_of_day, grain)`. Each event is placed on the clock by its own `formatRecordTime`; events without a parseable one are skipped.
  - `time_of_day_minutes(format_record_time: str) -> int | None` — minutes since midnight (0–1439) parsed from the device's own `formatRecordTime` string (e.g. `"2026-07-07 07:27"`); `None` if unparseable.

**Why `formatRecordTime`, not epoch+timezone:** PetLibro's cloud reports each event's local wall-clock in `formatRecordTime`, using a fixed device offset (verified: it labels an event `07:27` where DST-aware `America/Indiana/Indianapolis` would say `08:27`). That wall-clock is the *same clock* the feeding-plan `executionTime` values use, so deriving meal times from it keeps recommendations and the schedule in one clock. Converting the epoch through a DST-aware `ZoneInfo` would shift every meal time an hour. No `Config.timezone` is needed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_history.py
from petlibro_mcp.history import parse_duration, parse_work_record, time_of_day_minutes


def test_parse_duration_variants():
    assert parse_duration("01m37s") == 97
    assert parse_duration("24s") == 24
    assert parse_duration("1h02m03s") == 3723
    assert parse_duration("") == 0
    assert parse_duration("garbage") == 0


def test_parse_work_record_splits_eats_and_dispenses():
    days = [{
        "recordTime": "2026/07/07",
        "workRecords": [
            {"type": "DETECTION_EVENT",
             "eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
             "formatRecordTime": "2026-07-07 07:27",
             "params": '{"petName":"Saffron","seconds":"01m37s"}',
             "content": "Saffron came to eat and ate for 01m37s."},
            {"type": "GRAIN_OUTPUT_SUCCESS", "eventType": "FEEDING_PLAN_SUCCESS",
             "formatRecordTime": "2026-07-07 05:40",
             "actualGrainNum": 3, "expectGrainNum": 3},
        ],
    }]
    eats, dispenses = parse_work_record(days)
    assert eats == [(7 * 60 + 27, 97)]        # 447 minutes
    assert dispenses == [(5 * 60 + 40, 3)]    # 340 minutes


def test_parse_work_record_falls_back_to_content_for_duration():
    days = [{"workRecords": [
        {"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
         "formatRecordTime": "2026-07-07 00:16", "params": "",
         "content": "Rico came to eat and ate for 24s."},
    ]}]
    eats, _ = parse_work_record(days)
    assert eats == [(16, 24)]


def test_parse_work_record_skips_events_without_formatted_time():
    days = [{"workRecords": [
        {"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
         "params": '{"seconds":"10s"}'},   # no formatRecordTime -> unplaceable
    ]}]
    assert parse_work_record(days) == ([], [])


def test_parse_work_record_handles_empty():
    assert parse_work_record([]) == ([], [])
    assert parse_work_record(None) == ([], [])


def test_time_of_day_minutes_parses_device_wall_clock():
    # PetLibro's own formatRecordTime string is authoritative (fixed offset).
    assert time_of_day_minutes("2026-07-07 07:27") == 7 * 60 + 27
    assert time_of_day_minutes("2026-07-07 07:27:49") == 7 * 60 + 27
    assert time_of_day_minutes("garbage") is None
    assert time_of_day_minutes("") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_history.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'petlibro_mcp.history'`

- [ ] **Step 3: Write the implementation**

```python
# src/petlibro_mcp/history.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_history.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/history.py tests/test_history.py
git commit -m "feat: parse PetLibro workRecord history into event series"
```

---

### Task 2: Client facade read methods

**Files:**
- Modify: `src/petlibro_mcp/client.py` (add `work_record`, `feeding_plans`)
- Test: `tests/test_client.py` (append)

**Interfaces:**
- Consumes: `PetLibroAPI.get_feeding_plans` (exists), `PetLibroAPI.session.request` (exists).
- Produces:
  - `PetLibroClient.work_record(serial: str, days: int = 60, size: int = 1000) -> list` — raw day-grouped workRecords.
  - `PetLibroClient.feeding_plans(serial: str) -> list` — current plan rows (each has `executionTime`, `grainNum`, `enable`).

Note: no `Config.timezone` — time-of-day comes from each event's `formatRecordTime` (see Task 1), so no timezone config is needed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_client.py  (append)
from unittest.mock import AsyncMock
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp.config import Config


def _cfg():
    return Config(feeders=[], fountains=[], region="US",
                  max_cups_per_command=4, email="a@b.com", password="pw")


async def test_work_record_posts_expected_payload():
    api = AsyncMock()
    api.session.request = AsyncMock(return_value=[{"workRecords": []}])
    client = PetLibroClient(_cfg(), api=api)
    out = await client.work_record("SN-1", days=30, size=500)
    assert out == [{"workRecords": []}]
    args, kwargs = api.session.request.call_args
    assert args[0] == "POST" and args[1] == "/device/workRecord/list"
    body = kwargs["json"]
    assert body["deviceSn"] == "SN-1" and body["size"] == 500
    assert body["startTime"] < body["endTime"]


async def test_feeding_plans_delegates_to_api():
    api = AsyncMock()
    api.get_feeding_plans = AsyncMock(return_value=[{"executionTime": "08:00", "grainNum": 3}])
    client = PetLibroClient(_cfg(), api=api)
    out = await client.feeding_plans("SN-1")
    assert out == [{"executionTime": "08:00", "grainNum": 3}]
    api.get_feeding_plans.assert_awaited_once_with("SN-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_client.py -k "work_record or feeding_plans" -v`
Expected: FAIL — `AttributeError: 'PetLibroClient' object has no attribute 'work_record'`.

- [ ] **Step 3: Add the facade methods to the client**

In `src/petlibro_mcp/client.py`, add the import and two methods:

```python
from datetime import datetime, timedelta, timezone
```

```python
    async def work_record(self, serial: str, days: int = 60, size: int = 1000) -> list:
        await self.ensure_login()
        now = datetime.now(timezone.utc)
        start = int((now - timedelta(days=days)).timestamp() * 1000)
        end = int(now.timestamp() * 1000)
        return await self._api.session.request("POST", "/device/workRecord/list", json={
            "deviceSn": serial, "startTime": start, "endTime": end, "size": size,
        })

    async def feeding_plans(self, serial: str) -> list:
        await self.ensure_login()
        return await self._api.get_feeding_plans(serial)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_client.py -v`
Expected: PASS (existing tests still pass; new ones green)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/client.py tests/test_client.py
git commit -m "feat: read-only work_record/feeding_plans facade methods"
```

---

### Task 3: `rhythm.py` — duration-weighted circadian curve

**Files:**
- Create: `src/petlibro_mcp/rhythm.py`
- Test: `tests/test_rhythm.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `BINS = 48` (module constant; 30-minute bins).
  - `circadian_curve(tod_weights: list[tuple[int, int]], bins: int = 48, smooth: int = 2) -> list[float]` — `tod_weights` are `(minute_of_day, weight)`; returns a length-`bins` circularly-smoothed intensity curve.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rhythm.py
from petlibro_mcp.rhythm import BINS, circadian_curve


def test_curve_length_and_bucketing():
    # one visit at 08:00 (minute 480) -> bin 16 (480//30)
    curve = circadian_curve([(480, 10)], bins=48, smooth=0)
    assert len(curve) == 48
    assert curve[16] == 10.0
    assert sum(curve) == 10.0


def test_curve_weights_by_duration():
    curve = circadian_curve([(480, 1), (485, 4)], bins=48, smooth=0)
    assert curve[16] == 5.0  # both fall in bin 16


def test_smoothing_spreads_mass_circularly():
    curve = circadian_curve([(0, 9)], bins=48, smooth=1)
    # window of 3 centered on bin 0 wraps to bin 47
    assert curve[0] == 3.0
    assert curve[1] == 3.0
    assert curve[47] == 3.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rhythm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'petlibro_mcp.rhythm'`

- [ ] **Step 3: Write the implementation**

```python
# src/petlibro_mcp/rhythm.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rhythm.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/rhythm.py tests/test_rhythm.py
git commit -m "feat: duration-weighted circadian eating curve"
```

---

### Task 4: `rhythm.py` — peak finding

**Files:**
- Modify: `src/petlibro_mcp/rhythm.py`
- Test: `tests/test_rhythm.py` (append)

**Interfaces:**
- Consumes: `circadian_curve` output (a length-`bins` list).
- Produces:
  - `find_peaks(curve: list[float], max_meals: int = 6, min_separation_bins: int = 3) -> list[int]` — sorted bin indices of the chosen meal peaks (circular local maxima, ranked by height, enforcing minimum separation).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rhythm.py  (append)
from petlibro_mcp.rhythm import find_peaks


def test_find_peaks_picks_separated_maxima():
    curve = [0.0] * 48
    curve[8] = 10.0    # 04:00
    curve[20] = 8.0    # 10:00
    curve[40] = 6.0    # 20:00
    peaks = find_peaks(curve, max_meals=6, min_separation_bins=3)
    assert peaks == [8, 20, 40]


def test_find_peaks_respects_max_meals():
    curve = [0.0] * 48
    for i, v in [(4, 10), (12, 9), (20, 8), (28, 7), (36, 6), (44, 5)]:
        curve[i] = float(v)
    peaks = find_peaks(curve, max_meals=3, min_separation_bins=3)
    assert peaks == [4, 12, 20]  # three tallest, still sorted by time


def test_find_peaks_enforces_separation():
    curve = [0.0] * 48
    curve[10] = 10.0
    curve[11] = 9.0   # adjacent -> excluded by separation
    curve[30] = 8.0
    peaks = find_peaks(curve, max_meals=6, min_separation_bins=3)
    assert peaks == [10, 30]


def test_find_peaks_empty_curve():
    assert find_peaks([0.0] * 48) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rhythm.py -k find_peaks -v`
Expected: FAIL with `ImportError: cannot import name 'find_peaks'`

- [ ] **Step 3: Write the implementation**

Append to `src/petlibro_mcp/rhythm.py`:

```python
def _circ_dist(a: int, b: int, bins: int) -> int:
    d = abs(a - b) % bins
    return min(d, bins - d)


def find_peaks(curve, max_meals: int = 6, min_separation_bins: int = 3) -> list[int]:
    """Bin indices of meal peaks: circular local maxima, tallest first, spaced."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rhythm.py -v`
Expected: PASS (all rhythm tests green)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/rhythm.py tests/test_rhythm.py
git commit -m "feat: peak-finding for meal times from the rhythm curve"
```

---

### Task 5: `rhythm.py` — split mass across peaks

**Files:**
- Modify: `src/petlibro_mcp/rhythm.py`
- Test: `tests/test_rhythm.py` (append)

**Interfaces:**
- Consumes: `curve` (from `circadian_curve`) and `peaks` (from `find_peaks`).
- Produces:
  - `split_at_peaks(curve: list[float], peaks: list[int]) -> list[tuple[int, float]]` — `(minute_of_day, fraction)` per peak, sorted by time, fractions summing to `1.0` (empty list if no peaks). Each bin's mass is assigned to its nearest peak (circularly).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rhythm.py  (append)
from petlibro_mcp.rhythm import split_at_peaks


def test_split_assigns_mass_to_nearest_peak():
    curve = [0.0] * 48
    curve[8] = 6.0     # bin 8 -> 04:00
    curve[40] = 2.0    # bin 40 -> 20:00
    split = split_at_peaks(curve, [8, 40])
    assert split[0][0] == 8 * 30   # 240 minutes = 04:00
    assert split[1][0] == 40 * 30  # 1200 minutes = 20:00
    assert round(split[0][1], 3) == 0.75
    assert round(split[1][1], 3) == 0.25
    assert round(sum(f for _, f in split), 6) == 1.0


def test_split_empty_peaks():
    assert split_at_peaks([1.0] * 48, []) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rhythm.py -k split -v`
Expected: FAIL with `ImportError: cannot import name 'split_at_peaks'`

- [ ] **Step 3: Write the implementation**

Append to `src/petlibro_mcp/rhythm.py`:

```python
def split_at_peaks(curve, peaks) -> list[tuple[int, float]]:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rhythm.py -v`
Expected: PASS (all rhythm tests green)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/rhythm.py tests/test_rhythm.py
git commit -m "feat: split daily food across meal peaks by demand mass"
```

---

### Task 6: `planner.py` — split × total → plan rows

**Files:**
- Create: `src/petlibro_mcp/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: `split` from `rhythm.split_at_peaks` (`list[(minute_of_day, fraction)]`).
- Produces:
  - `allocate_portions(fractions: list[float], total: int) -> list[int]` — largest-remainder rounding; result sums to exactly `total`.
  - `plan_rows(split: list[tuple[int, float]], total_portions: int) -> list[tuple[str, int]]` — `("HH:MM", grain)` rows, dropping any zero-portion meal.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_planner.py
from petlibro_mcp.planner import allocate_portions, plan_rows


def test_allocate_sums_to_total():
    out = allocate_portions([0.5, 0.3, 0.2], 12)
    assert sum(out) == 12
    assert out == [6, 4, 2]


def test_allocate_largest_remainder():
    # 10 * [0.333, 0.333, 0.334] -> floors [3,3,3]=9, remainder 1 to the largest frac
    out = allocate_portions([0.333, 0.333, 0.334], 10)
    assert sum(out) == 10
    assert out == [3, 3, 4]


def test_plan_rows_formats_times_and_drops_zeros():
    split = [(240, 0.75), (1200, 0.25)]  # 04:00, 20:00
    rows = plan_rows(split, 12)
    assert rows == [("04:00", 9), ("20:00", 3)]


def test_plan_rows_drops_zero_portion_meals():
    split = [(0, 0.9), (720, 0.1)]  # 00:00 gets 2, 12:00 rounds to 0 at total=2
    rows = plan_rows(split, 2)
    assert rows == [("00:00", 2)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_planner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'petlibro_mcp.planner'`

- [ ] **Step 3: Write the implementation**

```python
# src/petlibro_mcp/planner.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_planner.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/planner.py tests/test_planner.py
git commit -m "feat: allocate daily total into rhythm-timed plan rows"
```

---

### Task 7: `analyze_rhythm` tool — compose the pipeline (recommend-only)

**Files:**
- Modify: `src/petlibro_mcp/tools.py`
- Test: `tests/test_tools.py` (append)

**Interfaces:**
- Consumes: `history.parse_work_record`, `rhythm.circadian_curve`, `rhythm.find_peaks`, `rhythm.split_at_peaks`, `planner.plan_rows`; `client.work_record`, `client.feeding_plans`. (`parse_work_record` already yields minute-of-day, so no timezone handling here.)
- Produces:
  - `async def analyze_rhythm(config, client, pet=None, days: int = 60) -> list[dict]` — one dict per feeder: `{"pet", "serial", "ok", "days", "eating_visits", "daily_total_portions", "current_schedule": [{"time","portions"}], "recommended_schedule": [{"time","portions"}]}` or `{"pet","serial","ok": False,"error"}`.

The recommended schedule redistributes the **current** daily total (sum of enabled plan `grainNum`) across the rhythm — Phase 1 does not change totals.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools.py  (append)

def _work_record_days():
    # 10 eating visits all clustered at 08:00 (device wall-clock)
    recs = {"workRecords": []}
    for _ in range(10):
        recs["workRecords"].append({
            "eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
            "formatRecordTime": "2026-07-07 08:00",
            "params": '{"seconds":"02m00s"}',
        })
    return [recs]


def _client_for_analyze():
    m = AsyncMock()
    m.work_record = AsyncMock(return_value=_work_record_days())
    m.feeding_plans = AsyncMock(return_value=[
        {"executionTime": "08:00", "grainNum": 3, "enable": True},
        {"executionTime": "20:00", "grainNum": 2, "enable": True},
        {"executionTime": "23:00", "grainNum": 1, "enable": False},  # disabled: excluded
    ])
    return m


async def test_analyze_rhythm_reports_current_and_recommended():
    client = _client_for_analyze()
    res = await tools.analyze_rhythm(cfg(), client, "ferris")
    assert len(res) == 1
    r = res[0]
    assert r["ok"] is True and r["pet"] == "ferris"
    assert r["eating_visits"] == 10
    # current total = 3 + 2 (disabled row excluded) = 5
    assert r["daily_total_portions"] == 5
    assert sum(x["portions"] for x in r["recommended_schedule"]) == 5
    assert {"time", "portions"} <= set(r["recommended_schedule"][0])


async def test_analyze_rhythm_unknown_pet_reports_error():
    res = await tools.analyze_rhythm(cfg(), _client_for_analyze(), "mittens")
    assert res[0]["ok"] is False and "mittens" in res[0]["error"]


async def test_analyze_rhythm_surfaces_fetch_failure():
    client = _client_for_analyze()
    client.work_record = AsyncMock(side_effect=RuntimeError("offline"))
    res = await tools.analyze_rhythm(cfg(), client, "ferris")
    assert res[0]["ok"] is False and "offline" in res[0]["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tools.py -k analyze_rhythm -v`
Expected: FAIL with `AttributeError: module 'petlibro_mcp.tools' has no attribute 'analyze_rhythm'`

- [ ] **Step 3: Write the implementation**

Add imports at the top of `src/petlibro_mcp/tools.py`:

```python
from .history import parse_work_record
from .rhythm import circadian_curve, find_peaks, split_at_peaks
from .planner import plan_rows
```

Append the tool:

```python
async def analyze_rhythm(config: Config, client: PetLibroClient,
                         pet=None, days: int = 60) -> list[dict]:
    try:
        feeders = config.resolve_feeders("all" if pet is None else [pet])
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]

    results = []
    for f in feeders:
        try:
            raw = await client.work_record(f.serial, days=days)
            eats, _dispenses = parse_work_record(raw)

            plans = await client.feeding_plans(f.serial)
            current = [(p.get("executionTime"), int(p.get("grainNum") or 0))
                       for p in plans if p.get("enable", True)]
            total = sum(g for _, g in current)

            tod = [(minute, max(dur, 1)) for minute, dur in eats]
            curve = circadian_curve(tod)
            split = split_at_peaks(curve, find_peaks(curve))
            recommended = plan_rows(split, total)

            results.append({
                "pet": f.name, "serial": f.serial, "ok": True,
                "days": days, "eating_visits": len(eats),
                "daily_total_portions": total,
                "current_schedule": [{"time": t, "portions": g} for t, g in current],
                "recommended_schedule": [{"time": t, "portions": g} for t, g in recommended],
            })
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tools.py -v`
Expected: PASS (existing tool tests + 3 new analyze tests green)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/tools.py tests/test_tools.py
git commit -m "feat: analyze_rhythm tool (recommend-only rhythm schedule)"
```

---

### Task 8: Register `analyze_rhythm` on the MCP server

**Files:**
- Modify: `src/petlibro_mcp/server.py` (add `Tool` def + dispatch branch)
- Modify: `tests/test_server.py` (update expected tool-name set)

**Interfaces:**
- Consumes: `tools.analyze_rhythm`.
- Produces: MCP tool `analyze_rhythm` with schema `{pet?: string, days?: integer}`.

- [ ] **Step 1: Update the failing test**

Change the name-set assertion in `tests/test_server.py::test_lists_five_tools` and rename it:

```python
async def test_lists_all_tools():
    server = build_server(cfg(), AsyncMock())
    assert server.request_handlers
    handler = server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest())
    names = {t.name for t in result.root.tools}
    assert names == {"feed", "open_lid", "feeder_status",
                     "fountain_status", "list_devices", "analyze_rhythm"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_server.py -k lists_all_tools -v`
Expected: FAIL — assertion mismatch (`analyze_rhythm` missing from the registered set)

- [ ] **Step 3: Add the Tool definition**

Append to the `TOOL_DEFS` list in `src/petlibro_mcp/server.py`:

```python
    Tool(
        name="analyze_rhythm",
        description=("Recommend-only: compute each cat's natural eating rhythm "
                     "from ~60d of history and report the current vs. a "
                     "rhythm-timed schedule (same daily total). No writes. "
                     "Omit 'pet' for all."),
        inputSchema={
            "type": "object",
            "properties": {
                "pet": {"type": "string"},
                "days": {"type": "integer", "default": 60},
            },
        },
    ),
```

- [ ] **Step 4: Add the dispatch branch**

In `_call_tool` in `src/petlibro_mcp/server.py`, add before the `else` branch:

```python
        elif name == "analyze_rhythm":
            result = await T.analyze_rhythm(config, client, a.get("pet"),
                                            a.get("days", 60))
```

- [ ] **Step 5: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: PASS (entire suite green, including the updated server test)

- [ ] **Step 6: Commit**

```bash
git add src/petlibro_mcp/server.py tests/test_server.py
git commit -m "feat: register analyze_rhythm MCP tool"
```

---

## Manual verification (after Task 8)

Run the tool against the live account to eyeball the rhythm output (read-only,
no device writes):

```bash
.venv/bin/python -c "
import asyncio, json
from pathlib import Path
from petlibro_mcp.server import _load_env_file, PETS_TOML
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp import tools
_load_env_file()
async def main():
    cfg = load_config(PETS_TOML)
    client = PetLibroClient(cfg)
    print(json.dumps(await tools.analyze_rhythm(cfg, client), indent=2))
asyncio.run(main())
"
```

Expected: one block per cat with `current_schedule` and `recommended_schedule`,
the recommended times clustered on each cat's real eating peaks, and
`sum(recommended portions) == daily_total_portions`.

## Follow-on (not this plan)

- **Phase 2:** writeback spike (discover `/device/feedingPlan/*` edit endpoint) +
  guarded `apply_schedule` tool with snapshot/rollback — its own plan once the
  endpoint is confirmed.
- **Phase 3:** `controller.py` self-calibrating daily total + persisted state +
  `tune_amounts` + periodic runner — its own plan.
```
