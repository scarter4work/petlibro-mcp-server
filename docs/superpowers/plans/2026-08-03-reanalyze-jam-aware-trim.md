# Jam-Aware Reanalyze Trimming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the web UI's Reanalyze button able to *lower* a cat's daily portions, using the feeder's own grain-outlet-block events as evidence of over-feeding, and show whether the last change actually helped.

**Architecture:** Three new pure modules (`trim.py`, `evidence.py`) plus one tiny I/O module (`journal.py`) sit behind the existing `tools.py` composition layer. `history.py` gains a single richer event parser that the old `parse_work_record` is then rebuilt on top of, so the 105 existing tests act as its regression guard. The health-critical arithmetic is all pure and unit-tested without touching the cloud. The UI is a single static `index.html` with no build step.

**Tech Stack:** Python 3.10+, pytest 8 with `asyncio_mode = "auto"`, `unittest.mock.AsyncMock` for the cloud client, FastAPI + Starlette `TestClient` for endpoints, vanilla ES5-style JS in one static file.

## Global Constraints

- `MIN_DAILY_PORTIONS = 8` — hard floor on any cat's daily total. Never write a schedule below it.
- `TRIM_PER_JAM_PER_DAY = 2` — trim size is `round(TRIM_PER_JAM_PER_DAY * jams_per_day)`.
- `MIN_EVIDENCE_DAYS = 7` — a trim requires at least this many days since the last schedule change.
- No meal may be trimmed below **1** portion. `plan_rows` drops zero rows, so trimming to 0 would delete a meal instead of shrinking it.
- No auto-apply. Every write stays behind the existing preview → confirm → verify → rollback path.
- Portions are the only unit. The hardware has no scale (`weight: 0.0`, `weightPercent: 0` on feeders); never infer grams.
- All time-of-day work uses the device wall clock via `formatRecordTime`, matching `executionTime`. Never use local server time for time-of-day.
- `now_ms` is always an explicit parameter on pure functions — never call `datetime.now()` inside them, so tests can pin time.
- `filterwarnings = ["error::DeprecationWarning", ...]` is active. A `DeprecationWarning` fails the suite.
- Run the full suite with `.venv/bin/python -m pytest -q` from the repo root. Baseline is **105 passed**.

## File Structure

| File | Responsibility |
|------|----------------|
| `src/petlibro_mcp/history.py` (modify) | Parse workRecord into a typed event stream. Add `parse_events`; rebuild `parse_work_record` on it. |
| `src/petlibro_mcp/trim.py` (new) | Pure trim policy: jam rate → trim size → placement across meals. |
| `src/petlibro_mcp/evidence.py` (new) | Pure: locate the last schedule change, split history before/after it, render a verdict. |
| `src/petlibro_mcp/journal.py` (new) | Append-only JSONL log of schedules this tool applied. Only module here that touches disk. |
| `src/petlibro_mcp/schedule_diff.py` (modify) | Treat a 0-portion target row as a removal, matching `plan_rows`. |
| `src/petlibro_mcp/tools.py` (modify) | Compose the above; `_compute_rhythm` stops hardcoding `total = current`. |
| `src/petlibro_mcp/server.py` (modify) | `analyze_rhythm` tool description no longer promises "same daily total". |
| `webui/rhythm_api.py` (modify) | Pass `trim` and `evidence` through to the browser. |
| `webui/static/index.html` (modify) | Reanalyze lifecycle + the correctness fixes. |
| `tests/test_trim.py`, `tests/test_evidence.py`, `tests/test_journal.py` (new) | Unit tests for the new modules. |
| `tests/test_history.py`, `tests/test_schedule_diff.py`, `tests/test_tools.py`, `tests/test_rhythm_api.py`, `tests/test_webapp.py` (modify) | Extend existing suites. |

---

### Task 1: Typed event parsing in `history.py`

Today `parse_work_record` returns only `(minute_of_day, value)` pairs, discarding the date — so nothing can be split before/after a schedule change. It also lumps manual feeds in with scheduled ones, which would make the delivery metric lie (zeus has 35 manual feeds in 43 days).

**Files:**
- Modify: `src/petlibro_mcp/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Event` frozen dataclass with fields `kind: str`, `minute: int`, `at_ms: int`, `amount: int`.
    `kind` is one of `"eat"`, `"dispense_plan"`, `"dispense_manual"`, `"block"`, `"grain_low"`.
    `amount` is eating seconds for `"eat"`, portions for either dispense kind, `0` otherwise.
  - `parse_events(days: list[dict] | None) -> list[Event]`
  - `parse_work_record(days) -> tuple[list[tuple[int,int]], list[tuple[int,int]]]` — unchanged signature and behavior, now derived from `parse_events`. Its `dispenses` include **both** dispense kinds, preserving today's behavior exactly.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
from petlibro_mcp.history import Event, parse_events


def _day(*records):
    return [{"workRecords": list(records)}]


def test_parse_events_reads_block_with_timestamp_and_minute():
    days = _day({
        "type": "GRAIN_OUTLET_BLOCK_REMOVED",
        "eventType": "GRAIN_OUTLET_BLOCK_REMOVED",
        "recordTime": 1785608794000,
        "formatRecordTime": "2026-08-01 13:26",
    })
    events = parse_events(days)
    assert events == [Event(kind="block", minute=13 * 60 + 26,
                            at_ms=1785608794000, amount=0)]


def test_parse_events_separates_manual_from_scheduled_dispense():
    days = _day(
        {"type": "GRAIN_OUTPUT_SUCCESS", "eventType": "FEEDING_PLAN_SUCCESS",
         "recordTime": 1000, "formatRecordTime": "2026-08-01 10:30",
         "actualGrainNum": 3},
        {"type": "GRAIN_OUTPUT_SUCCESS", "eventType": "MANUAL_FEEDING_SUCCESS",
         "recordTime": 2000, "formatRecordTime": "2026-08-01 11:21",
         "actualGrainNum": 2},
    )
    kinds = {e.kind: e.amount for e in parse_events(days)}
    assert kinds == {"dispense_plan": 3, "dispense_manual": 2}


def test_parse_events_reads_grain_low():
    days = _day({
        "type": "GRAIN_OUTPUT_SUCCESS",
        "eventType": "FEEDING_PLAN_GRAIN_NOT_ENOUGH",
        "recordTime": 3000, "formatRecordTime": "2026-08-01 06:00",
        "actualGrainNum": 3,
    })
    events = parse_events(days)
    # a grain-low dispense still delivered food, so it is BOTH a delivery and a warning
    assert sorted(e.kind for e in events) == ["dispense_plan", "grain_low"]
    assert [e.amount for e in events if e.kind == "dispense_plan"] == [3]


def test_parse_events_reads_eat_seconds():
    days = _day({
        "type": "DETECTION_EVENT",
        "eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
        "recordTime": 4000, "formatRecordTime": "2026-08-01 14:05",
        "params": '{"petName":"Bridget","seconds":"01m37s"}',
    })
    assert parse_events(days) == [Event(kind="eat", minute=14 * 60 + 5,
                                        at_ms=4000, amount=97)]


def test_parse_events_skips_records_without_parseable_time():
    days = _day({"type": "GRAIN_OUTLET_BLOCK_REMOVED",
                 "eventType": "GRAIN_OUTLET_BLOCK_REMOVED",
                 "recordTime": 5000, "formatRecordTime": ""})
    assert parse_events(days) == []


def test_parse_work_record_still_counts_both_dispense_kinds():
    """Regression guard: the old contract lumps manual in with scheduled."""
    days = _day(
        {"type": "GRAIN_OUTPUT_SUCCESS", "eventType": "FEEDING_PLAN_SUCCESS",
         "recordTime": 1000, "formatRecordTime": "2026-08-01 10:30",
         "actualGrainNum": 3},
        {"type": "GRAIN_OUTPUT_SUCCESS", "eventType": "MANUAL_FEEDING_SUCCESS",
         "recordTime": 2000, "formatRecordTime": "2026-08-01 11:21",
         "actualGrainNum": 2},
    )
    _eats, dispenses = parse_work_record(days)
    assert sorted(dispenses) == [(10 * 60 + 30, 3), (11 * 60 + 21, 2)]
```

Ensure `parse_work_record` is imported at the top of the file if it is not already.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_history.py -q`
Expected: FAIL — `ImportError: cannot import name 'Event' from 'petlibro_mcp.history'`

- [ ] **Step 3: Implement `parse_events` and rebuild `parse_work_record` on it**

In `src/petlibro_mcp/history.py`, add the dataclass import and constants near the existing ones:

```python
from dataclasses import dataclass

_BLOCK_TYPE = "GRAIN_OUTLET_BLOCK_REMOVED"
_MANUAL_EVENT = "MANUAL_FEEDING_SUCCESS"
_GRAIN_LOW_EVENTS = frozenset({
    "FEEDING_PLAN_GRAIN_NOT_ENOUGH",
    "MANUAL_FEEDING_GRAIN_NOT_ENOUGH",
})


@dataclass(frozen=True)
class Event:
    """One workRecord entry, placed on both the 24h clock and the calendar.

    `minute` is minute-of-day from the device wall clock (same clock the feeding
    plans use); `at_ms` is the absolute recordTime, needed to split history
    before/after a schedule change. `amount` is eating seconds for "eat",
    portions for either dispense kind, and 0 otherwise.
    """
    kind: str
    minute: int
    at_ms: int
    amount: int
```

Then add the parser and rewrite `parse_work_record` to delegate:

```python
def _eat_seconds(w: dict) -> int:
    secs = 0
    try:
        secs = parse_duration(json.loads(w.get("params") or "").get("seconds", ""))
    except (ValueError, TypeError, AttributeError):
        secs = 0
    if not secs:
        mt = _ATE_FOR.search(w.get("content") or "")
        secs = parse_duration(mt.group(1)) if mt else 0
    return secs


def parse_events(days: list[dict] | None) -> list[Event]:
    """Flatten day-grouped workRecords into a typed event stream.

    Records without a parseable formatRecordTime are skipped — they cannot be
    placed on the daily clock. A grain-low dispense yields TWO events: it did
    deliver food, and it also warns the hopper is running out.
    """
    out: list[Event] = []
    for day in days or []:
        for w in day.get("workRecords", []):
            minute = time_of_day_minutes(w.get("formatRecordTime") or "")
            if minute is None:
                continue
            at_ms = int(w.get("recordTime") or 0)
            etype = w.get("eventType")
            wtype = w.get("type")
            if etype == _EAT_EVENT:
                out.append(Event("eat", minute, at_ms, _eat_seconds(w)))
            elif wtype == _DISPENSE_TYPE:
                grain = int(w.get("actualGrainNum") or 0)
                kind = ("dispense_manual" if etype == _MANUAL_EVENT
                        else "dispense_plan")
                out.append(Event(kind, minute, at_ms, grain))
                if etype in _GRAIN_LOW_EVENTS:
                    out.append(Event("grain_low", minute, at_ms, 0))
            elif wtype == _BLOCK_TYPE:
                out.append(Event("block", minute, at_ms, 0))
    return out


def parse_work_record(days: list[dict] | None) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Split day-grouped workRecords into (eats, dispenses) event series.

    eats: (minute_of_day, eating_duration_s); dispenses: (minute_of_day, grain).
    Kept for callers that only need the 24h-clock view. `dispenses` deliberately
    includes BOTH scheduled and manual feeds, preserving the original contract —
    callers measuring schedule delivery must use parse_events and filter to
    "dispense_plan" instead.
    """
    events = parse_events(days)
    eats = [(e.minute, e.amount) for e in events if e.kind == "eat"]
    dispenses = [(e.minute, e.amount) for e in events
                 if e.kind in ("dispense_plan", "dispense_manual")]
    return eats, dispenses
```

Delete the old body of `parse_work_record` (the `for day in days or []` loop and its
inline eat/dispense parsing) — `_eat_seconds` now holds that logic.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_history.py -q`
Expected: PASS

Then the whole suite, to confirm the rebuild broke nothing:
Run: `.venv/bin/python -m pytest -q`
Expected: PASS, count is **105 + 6 = 111**

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/history.py tests/test_history.py
git commit -m "feat: typed event parsing with timestamps and manual/scheduled split"
```

---

### Task 2: A zero-portion target row means "remove the meal"

The UI steppers allow 0 (`min="0"`, `Math.max(0, …)`). `plan_rows` drops zero rows on the recommend path, but `diff_schedule` has no zero filter — so a user-zeroed meal becomes an update with `grainNum: 0`, writing a junk row instead of removing the meal. The two paths disagree about what zero means.

**Files:**
- Modify: `src/petlibro_mcp/schedule_diff.py`
- Test: `tests/test_schedule_diff.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `diff_schedule(current_enabled, target_rows)` — unchanged signature. Target rows with `grain <= 0` are now treated as absent: an existing row at that time is removed, and no add is emitted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schedule_diff.py`:

```python
def test_zero_portion_target_removes_an_existing_row():
    current = [{"id": 7, "executionTime": "14:00", "grainNum": 2, "enable": True},
               {"id": 8, "executionTime": "22:00", "grainNum": 4, "enable": True}]
    actions = diff_schedule(current, [("14:00", 0), ("22:00", 4)])
    assert actions["removes"] == [7]
    assert [u["id"] for u in actions["updates"]] == [8]
    assert actions["adds"] == []


def test_zero_portion_target_is_never_added():
    actions = diff_schedule([], [("09:00", 0)])
    assert actions == {"updates": [], "adds": [], "removes": []}


def test_zero_portion_target_does_not_write_grainnum_zero():
    """Regression: a zeroed meal must not survive as a 0-portion plan row."""
    current = [{"id": 1, "executionTime": "10:00", "grainNum": 3, "enable": True}]
    actions = diff_schedule(current, [("10:00", 0)])
    assert all(u["grainNum"] != 0 for u in actions["updates"])
    assert all(a["grainNum"] != 0 for a in actions["adds"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_schedule_diff.py -q`
Expected: FAIL — `test_zero_portion_target_removes_an_existing_row` reports `removes == []` because the 14:00 row is matched and updated to `grainNum: 0`.

- [ ] **Step 3: Filter zero rows when building the target map**

In `src/petlibro_mcp/schedule_diff.py`, change the target-map construction:

```python
    target: dict[str, int] = {}
    for time, grain in target_rows:
        # A zero-portion row means "no meal at this time", matching planner.plan_rows
        # which drops them. Keeping it would write a useless grainNum=0 row instead of
        # removing the meal, and the two paths would disagree about what zero means.
        if grain <= 0:
            continue
        target[time] = grain  # last wins; the planner does not emit duplicate times
```

The existing remove loop already removes any current row whose time is not in
`target`, so a zeroed time now falls out as a removal with no further change.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_schedule_diff.py -q`
Expected: PASS

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **114**

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/schedule_diff.py tests/test_schedule_diff.py
git commit -m "fix: treat a zero-portion target row as a meal removal"
```

---

### Task 3: The trim policy (`trim.py`)

Pure arithmetic, isolated so the health-critical rules are testable without the cloud. Jam rate does not track meal size — it tracks time of day — so blocks are attributed to the nearest *proposed* meal and the cut lands on the jammiest slots.

**Files:**
- Create: `src/petlibro_mcp/trim.py`
- Test: `tests/test_trim.py`

**Interfaces:**
- Consumes: `history.Event` (Task 1), and `rhythm._circ_dist` for circular distance.
- Produces:
  - `MIN_DAILY_PORTIONS = 8`, `TRIM_PER_JAM_PER_DAY = 2`
  - `jam_rate(block_count: int, window_days: float) -> float` — 0.0 when `window_days <= 0`.
  - `trim_amount(rate: float, current_total: int) -> int` — `round(TRIM_PER_JAM_PER_DAY * rate)`, clamped so `current_total - trim >= MIN_DAILY_PORTIONS`; never negative.
  - `attribute_blocks(block_minutes: list[int], meal_times: list[str]) -> dict[str, int]` — each block counted to the nearest meal time by circular clock distance. `{}` when `meal_times` is empty.
  - `trim_schedule(rows: list[tuple[str, int]], blocks_by_time: dict[str, int], trim: int) -> list[tuple[str, int]]` — removes `trim` portions, jammiest meal first, never taking any meal below 1. Returns rows in time order.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trim.py`:

```python
from petlibro_mcp.trim import (
    MIN_DAILY_PORTIONS, TRIM_PER_JAM_PER_DAY,
    attribute_blocks, jam_rate, trim_amount, trim_schedule,
)


def test_jam_rate_is_blocks_per_day():
    assert jam_rate(29, 27) == 29 / 27


def test_jam_rate_of_empty_window_is_zero():
    assert jam_rate(5, 0) == 0.0


def test_trim_amount_scales_with_rate():
    # bridget: 1.07 jams/day -> round(2 * 1.07) == 2
    assert trim_amount(1.07, 12) == 2
    # zeus: 0.72/day -> round(1.44) == 1
    assert trim_amount(0.72, 12) == 1


def test_trim_amount_dead_band_protects_a_clean_feeder():
    """ricco jams 0.15/day; round(0.30) == 0, so he is never trimmed."""
    assert trim_amount(0.15, 12) == 0


def test_trim_amount_respects_the_floor():
    assert trim_amount(5.0, 9) == 9 - MIN_DAILY_PORTIONS  # only 1 portion of room
    assert trim_amount(5.0, MIN_DAILY_PORTIONS) == 0      # already at the floor


def test_trim_amount_never_negative_below_the_floor():
    assert trim_amount(2.0, 4) == 0


def test_trim_per_jam_constant_is_two():
    assert TRIM_PER_JAM_PER_DAY == 2


def test_attribute_blocks_counts_to_nearest_meal():
    blocks = [14 * 60 + 10, 14 * 60 - 5, 22 * 60 + 2]
    assert attribute_blocks(blocks, ["14:00", "22:00"]) == {"14:00": 2, "22:00": 1}


def test_attribute_blocks_wraps_around_midnight():
    """23:50 is 20 minutes from 00:10, not 23 hours from it."""
    assert attribute_blocks([23 * 60 + 50], ["00:10", "12:00"]) == {"00:10": 1}


def test_attribute_blocks_with_no_meals_is_empty():
    assert attribute_blocks([600], []) == {}


def test_trim_schedule_takes_from_the_jammiest_meal():
    rows = [("10:30", 3), ("14:00", 2), ("22:00", 4)]
    out = trim_schedule(rows, {"14:00": 10, "22:00": 9, "10:30": 1}, 2)
    assert out == [("10:30", 3), ("14:00", 1), ("22:00", 3)]


def test_trim_schedule_never_takes_a_meal_below_one():
    """14:00 is jammiest but already at 1, so the cut moves to 22:00."""
    rows = [("14:00", 1), ("22:00", 4)]
    out = trim_schedule(rows, {"14:00": 10, "22:00": 2}, 2)
    assert out == [("14:00", 1), ("22:00", 2)]


def test_trim_schedule_stops_when_every_meal_is_at_one():
    rows = [("08:00", 1), ("20:00", 1)]
    assert trim_schedule(rows, {"08:00": 5, "20:00": 5}, 3) == rows


def test_trim_schedule_of_zero_is_a_noop():
    rows = [("08:00", 2), ("20:00", 3)]
    assert trim_schedule(rows, {"08:00": 9}, 0) == rows


def test_trim_schedule_returns_time_order():
    rows = [("22:00", 4), ("06:00", 3)]
    out = trim_schedule(rows, {"22:00": 8}, 1)
    assert [t for t, _ in out] == ["06:00", "22:00"]


def test_trim_schedule_untouched_meals_keep_their_portions():
    rows = [("06:00", 3), ("14:00", 3)]
    out = trim_schedule(rows, {"14:00": 7}, 1)
    assert dict(out)["06:00"] == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_trim.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'petlibro_mcp.trim'`

- [ ] **Step 3: Implement `trim.py`**

Create `src/petlibro_mcp/trim.py`:

```python
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

_BINS_PER_DAY = 1440        # circular clock is measured in minutes


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
                      key=lambda t: _circ_dist(b, _to_minute(t), _BINS_PER_DAY))
        counts[nearest] = counts.get(nearest, 0) + 1
    return counts


def trim_schedule(rows: list[tuple[str, int]], blocks_by_time: dict[str, int],
                  trim: int) -> list[tuple[str, int]]:
    """Remove `trim` portions, jammiest meal first, never below 1 per meal.

    A meal trimmed to 0 would be DROPPED by planner.plan_rows rather than
    shrunk, silently deleting a feeding, so 1 is the per-meal floor. When every
    meal has reached it the remaining trim is abandoned — the daily floor is the
    backstop, not this loop.
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
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_trim.py -q`
Expected: PASS

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **131**

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/trim.py tests/test_trim.py
git commit -m "feat: pure jam-rate trim policy with per-meal and daily floors"
```

---

### Task 4: The evidence window and verdict (`evidence.py`)

The trim is computed against the total currently on the feeder, so without a
window tied to the *current* schedule, three clicks off the same 60-day history
would ratchet 12→10→8 before the first cut had any chance to show an effect.

**Files:**
- Create: `src/petlibro_mcp/evidence.py`
- Test: `tests/test_evidence.py`

**Interfaces:**
- Consumes: `history.Event` (Task 1).
- Produces:
  - `MIN_EVIDENCE_DAYS = 7`
  - `MS_PER_DAY = 86_400_000`
  - `last_change_at(plans: list[dict], journal_at_ms: int | None) -> int | None` — the journal timestamp when present (authoritative), else `max(updateTime)` across enabled plans, else `None`.
  - `split_events(events: list[Event], change_at_ms: int) -> tuple[list[Event], list[Event]]` — `(before, since)`, partitioned on `at_ms >= change_at_ms`.
  - `window_days(start_ms: int, end_ms: int) -> float` — clamped at 0.0.
  - `verdict(before_rate: float | None, since_rate: float, days_since: float) -> str` — `"too_early"` when `days_since < MIN_EVIDENCE_DAYS`; `"unavailable"` when `before_rate is None`; `"improving"` when `since_rate <= before_rate * 0.75`; `"worse"` when `since_rate >= before_rate * 1.25`; else `"no_change"`.
  - `may_trim(days_since: float | None) -> bool` — `True` only when `days_since` is not None and `>= MIN_EVIDENCE_DAYS`. Deliberately independent of whether a "before" window exists.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evidence.py`:

```python
from petlibro_mcp.evidence import (
    MIN_EVIDENCE_DAYS, MS_PER_DAY,
    last_change_at, may_trim, split_events, verdict, window_days,
)
from petlibro_mcp.history import Event


def test_last_change_at_prefers_the_journal():
    plans = [{"updateTime": 1000, "enable": True}]
    assert last_change_at(plans, 9999) == 9999


def test_last_change_at_falls_back_to_max_update_time():
    plans = [{"updateTime": 1000, "enable": True},
             {"updateTime": 3000, "enable": True}]
    assert last_change_at(plans, None) == 3000


def test_last_change_at_ignores_disabled_rows():
    plans = [{"updateTime": 1000, "enable": True},
             {"updateTime": 9_000_000, "enable": False}]
    assert last_change_at(plans, None) == 1000


def test_last_change_at_none_when_nothing_is_known():
    assert last_change_at([], None) is None


def test_split_events_partitions_on_the_change():
    before = Event("block", 600, 1000, 0)
    after = Event("block", 600, 5000, 0)
    at_change = Event("block", 600, 3000, 0)
    b, s = split_events([before, after, at_change], 3000)
    assert b == [before]
    assert s == [at_change, after]


def test_window_days_converts_ms():
    assert window_days(0, 3 * MS_PER_DAY) == 3.0


def test_window_days_never_negative():
    assert window_days(5 * MS_PER_DAY, 0) == 0.0


def test_verdict_too_early_dominates():
    assert verdict(1.0, 0.1, MIN_EVIDENCE_DAYS - 1) == "too_early"


def test_verdict_unavailable_without_a_before_window():
    assert verdict(None, 0.5, 30) == "unavailable"


def test_verdict_improving_on_a_clear_drop():
    assert verdict(1.0, 0.5, 30) == "improving"


def test_verdict_worse_on_a_clear_rise():
    assert verdict(1.0, 1.5, 30) == "worse"


def test_verdict_no_change_within_the_band():
    assert verdict(1.0, 1.0, 30) == "no_change"


def test_may_trim_requires_a_full_evidence_window():
    assert may_trim(MIN_EVIDENCE_DAYS) is True
    assert may_trim(MIN_EVIDENCE_DAYS - 0.5) is False
    assert may_trim(None) is False


def test_may_trim_does_not_depend_on_a_before_window():
    """bridget has 26 days under the current schedule but no usable 'before'.
    That is enough to trim on; the before/after is only for reporting."""
    assert may_trim(26) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_evidence.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'petlibro_mcp.evidence'`

- [ ] **Step 3: Implement `evidence.py`**

Create `src/petlibro_mcp/evidence.py`:

```python
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

    Independent of whether a 'before' window exists: a cat can have weeks of
    jam evidence under the current schedule and still no comparable window
    before it. That is enough to act on — the before/after is for reporting.
    """
    return days_since is not None and days_since >= MIN_EVIDENCE_DAYS
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_evidence.py -q`
Expected: PASS

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **147**

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/evidence.py tests/test_evidence.py
git commit -m "feat: evidence window, verdict, and the trim eligibility gate"
```

---

### Task 5: The applied-schedule journal (`journal.py`)

**Files:**
- Create: `src/petlibro_mcp/journal.py`
- Modify: `.gitignore`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DEFAULT_JOURNAL_NAME = ".petlibro-journal.jsonl"`
  - `append(path, serial: str, schedule: list[tuple[str, int]], total: int, at_ms: int) -> None` — appends one JSON object per line; creates the file and parent directory if absent.
  - `last_at_ms(path, serial: str) -> int | None` — `at_ms` of the newest entry for that serial, or `None` if the file is missing, unreadable, or has no entry for it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_journal.py`:

```python
import json
from petlibro_mcp import journal


def test_append_then_read_back(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    assert journal.last_at_ms(p, "SN-A") == 1000


def test_last_at_ms_returns_the_newest_entry(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    journal.append(p, "SN-A", [("08:00", 1)], 1, 5000)
    assert journal.last_at_ms(p, "SN-A") == 5000


def test_entries_are_isolated_per_serial(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    journal.append(p, "SN-B", [("09:00", 3)], 3, 7000)
    assert journal.last_at_ms(p, "SN-A") == 1000
    assert journal.last_at_ms(p, "SN-B") == 7000


def test_missing_file_reads_as_none(tmp_path):
    assert journal.last_at_ms(tmp_path / "nope.jsonl", "SN-A") is None


def test_unknown_serial_reads_as_none(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    assert journal.last_at_ms(p, "SN-Z") is None


def test_append_creates_missing_parent_directory(tmp_path):
    p = tmp_path / "deep" / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    assert p.is_file()


def test_corrupt_line_does_not_break_reading(tmp_path):
    """A half-written line must not hide the good entries around it."""
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    with p.open("a") as fh:
        fh.write("{not json\n")
    journal.append(p, "SN-A", [("08:00", 1)], 1, 9000)
    assert journal.last_at_ms(p, "SN-A") == 9000


def test_line_shape_is_stable(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2), ("20:00", 3)], 5, 1234)
    row = json.loads(p.read_text().splitlines()[0])
    assert row == {"serial": "SN-A", "at_ms": 1234, "total": 5,
                   "schedule": [["08:00", 2], ["20:00", 3]]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_journal.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'petlibro_mcp.journal'`

- [ ] **Step 3: Implement `journal.py`**

Create `src/petlibro_mcp/journal.py`:

```python
"""Append-only record of the schedules this tool wrote.

The device's own updateTime dates most edits, but an apply that only REMOVES
rows leaves the survivors' updateTime stale — so the evidence window would be
misdated exactly when a schedule shrank. Journalling our own writes closes that
gap; the device remains the fallback for edits made in the phone app.

One JSON object per line, so a partial write costs at most the last entry.
"""
from __future__ import annotations
import json
from pathlib import Path

DEFAULT_JOURNAL_NAME = ".petlibro-journal.jsonl"


def append(path, serial: str, schedule: list[tuple[str, int]], total: int,
           at_ms: int) -> None:
    """Record one applied schedule. Creates the file and its directory if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"serial": serial, "at_ms": int(at_ms), "total": int(total),
           "schedule": [[t, int(g)] for t, g in schedule]}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def last_at_ms(path, serial: str) -> int | None:
    """Newest recorded change time for `serial`, or None if there is none.

    Unparseable lines are skipped rather than raised: a truncated tail must not
    hide the good entries around it, and this feeds a reporting window, not a
    write path.
    """
    p = Path(path)
    if not p.is_file():
        return None
    newest: int | None = None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("serial") != serial:
            continue
        at = row.get("at_ms")
        if isinstance(at, int) and (newest is None or at > newest):
            newest = at
    return newest
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_journal.py -q`
Expected: PASS

- [ ] **Step 5: Ignore the journal file**

Append to `.gitignore`:

```
.petlibro-journal.jsonl
```

- [ ] **Step 6: Run the full suite and commit**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **155**

```bash
git add src/petlibro_mcp/journal.py tests/test_journal.py .gitignore
git commit -m "feat: append-only journal of applied schedules"
```

---

### Task 6: Wire the trim and evidence into `tools.py`

This is where Reanalyze stops being a no-op: `_compute_rhythm` currently sets
`total = sum(g for _, g in current)` — the total already on the feeder — so the
recommendation can only ever redistribute it.

**Files:**
- Modify: `src/petlibro_mcp/tools.py:148-188` (`_compute_rhythm`, `analyze_rhythm`), and `apply_target_rows` / `apply_schedule` for journalling
- Modify: `src/petlibro_mcp/server.py:68-80` (tool description)
- Test: `tests/test_tools.py`

**Interfaces:**
- Consumes: `history.parse_events` (Task 1), all of `trim` (Task 3), all of `evidence` (Task 4), `journal.last_at_ms` / `journal.append` (Task 5).
- Produces: `analyze_rhythm` result dicts gain two keys:
  - `"trim"`: `{"applied": int, "jam_rate": float, "blocks_by_time": dict[str, int], "withheld_reason": str | None}`
  - `"evidence"`: `{"days_since_change": float | None, "jam_rate_before": float | None, "jam_rate_since": float, "delivered_per_day_since": float, "scheduled_per_day": int, "grain_low_since": int, "verdict": str}`
  - `_compute_rhythm(client, feeder, days, now_ms, journal_path)` gains `now_ms: int` and `journal_path` parameters and returns the extra keys `"trim"` and `"evidence"` alongside its existing ones.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools.py`:

```python
from petlibro_mcp.evidence import MS_PER_DAY

_NOW_MS = 100 * MS_PER_DAY


def _jammy_client(blocks=30, days_since=30, total_rows=(("14:00", 2), ("22:00", 4))):
    """A feeder whose schedule changed `days_since` ago and has jammed `blocks` times since."""
    change_at = _NOW_MS - days_since * MS_PER_DAY
    records = []
    for i in range(blocks):
        at = change_at + int((i + 1) * (days_since * MS_PER_DAY) / (blocks + 1))
        records.append({"type": "GRAIN_OUTLET_BLOCK_REMOVED",
                        "eventType": "GRAIN_OUTLET_BLOCK_REMOVED",
                        "recordTime": at, "formatRecordTime": "2026-08-01 14:05"})
    for i in range(40):
        records.append({"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
                        "recordTime": change_at + i, "formatRecordTime": "2026-08-01 14:00",
                        "params": '{"seconds":"02m00s"}'})
    m = AsyncMock()
    m.work_record = AsyncMock(return_value=[{"workRecords": records}])
    m.feeding_plans = AsyncMock(return_value=[
        {"id": i + 1, "executionTime": t, "grainNum": g, "enable": True,
         "updateTime": change_at}
        for i, (t, g) in enumerate(total_rows)])
    return m


async def test_analyze_rhythm_trims_a_jammy_feeder_below_its_current_total():
    client = _jammy_client(blocks=30, days_since=30)
    res = await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                     now_ms=_NOW_MS)
    out = res[0]
    assert out["ok"] is True
    assert out["daily_total_portions"] == 6
    recommended = sum(r["portions"] for r in out["recommended_schedule"])
    assert recommended < 6, "a jammy feeder must be recommended LESS than it gets now"
    assert out["trim"]["applied"] > 0


async def test_analyze_rhythm_leaves_a_clean_feeder_alone():
    client = _jammy_client(blocks=1, days_since=30)
    out = (await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                      now_ms=_NOW_MS))[0]
    assert out["trim"]["applied"] == 0
    assert sum(r["portions"] for r in out["recommended_schedule"]) == 6


async def test_analyze_rhythm_withholds_the_trim_when_too_early():
    """A change 2 days old has no verdict yet, so no further cut is proposed."""
    client = _jammy_client(blocks=30, days_since=2)
    out = (await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                      now_ms=_NOW_MS))[0]
    assert out["trim"]["applied"] == 0
    assert out["trim"]["withheld_reason"] == "too_early"
    assert out["evidence"]["verdict"] == "too_early"


async def test_analyze_rhythm_never_recommends_below_the_daily_floor():
    client = _jammy_client(blocks=300, days_since=30,
                           total_rows=(("08:00", 5), ("20:00", 4)))
    out = (await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                      now_ms=_NOW_MS))[0]
    assert sum(r["portions"] for r in out["recommended_schedule"]) >= 8


async def test_analyze_rhythm_reports_blocks_per_meal():
    client = _jammy_client(blocks=10, days_since=30)
    out = (await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                      now_ms=_NOW_MS))[0]
    assert sum(out["trim"]["blocks_by_time"].values()) == 10


async def test_analyze_rhythm_evidence_reports_days_since_change():
    client = _jammy_client(blocks=5, days_since=30)
    out = (await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                      now_ms=_NOW_MS))[0]
    assert round(out["evidence"]["days_since_change"]) == 30


async def test_apply_target_rows_journals_what_it_wrote(tmp_path):
    from petlibro_mcp import journal
    client = _jammy_client(blocks=1, days_since=30)
    client.remove_plan = AsyncMock(return_value=None)
    client.update_plan = AsyncMock(return_value=None)
    client.add_plan = AsyncMock(return_value=None)
    client.feeding_plans = AsyncMock(side_effect=[
        [{"id": 1, "executionTime": "14:00", "grainNum": 2, "enable": True,
          "updateTime": 1}],
        [{"id": 1, "executionTime": "14:00", "grainNum": 1, "enable": True,
          "updateTime": 1}],
    ])
    jpath = tmp_path / "j.jsonl"
    res = await tools.apply_target_rows(cfg(), client, "ferris", [("14:00", 1)],
                                        apply=True, now_ms=_NOW_MS,
                                        journal_path=jpath)
    assert res[0]["ok"] is True
    assert journal.last_at_ms(jpath, "SN-F") == _NOW_MS


async def test_dry_run_does_not_journal(tmp_path):
    from petlibro_mcp import journal
    client = _jammy_client(blocks=1, days_since=30)
    jpath = tmp_path / "j.jsonl"
    await tools.apply_target_rows(cfg(), client, "ferris", [("14:00", 1)],
                                  apply=False, now_ms=_NOW_MS, journal_path=jpath)
    assert journal.last_at_ms(jpath, "SN-F") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_tools.py -q`
Expected: FAIL — `TypeError: analyze_rhythm() got an unexpected keyword argument 'now_ms'`

- [ ] **Step 3: Implement the composition**

In `src/petlibro_mcp/tools.py`, extend the imports:

```python
from datetime import datetime, timezone
from pathlib import Path
from . import evidence as EV
from . import journal as J
from . import trim as TR
from .history import parse_events, parse_work_record
```

Add a default journal path and a clock helper near the top:

```python
def _default_journal_path() -> Path:
    """Beside pets.toml, which is where .env already lives."""
    from .server import PETS_TOML
    return Path(PETS_TOML).parent / J.DEFAULT_JOURNAL_NAME


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
```

Replace `_compute_rhythm` entirely:

```python
async def _compute_rhythm(client, feeder, days: int, now_ms: int,
                          journal_path) -> dict:
    """Rhythm analysis for a feeder: current schedule, evidence, trim, target rows.

    The daily total is NO LONGER simply the total already on the device. It is
    that total minus a jam-driven trim, which is what lets a recompute lower an
    over-aggressive schedule. The trim is withheld until the current schedule has
    had MIN_EVIDENCE_DAYS to prove itself, so repeated clicks cannot ratchet the
    total down before the last change has shown an effect.
    """
    raw = await client.work_record(feeder.serial, days=days)
    events = parse_events(raw)
    eats, _dispenses = parse_work_record(raw)

    plans = await client.feeding_plans(feeder.serial)
    enabled = [p for p in plans if p.get("enable", True)]
    current = [(p.get("executionTime"), int(p.get("grainNum") or 0)) for p in enabled]
    total = sum(g for _, g in current)

    change_at = EV.last_change_at(
        enabled, J.last_at_ms(journal_path, feeder.serial))
    if change_at is None:
        days_since = None
        before, since = [], events
        rate_before = None
    else:
        days_since = EV.window_days(change_at, now_ms)
        before, since = EV.split_events(events, change_at)
        history_start = min((e.at_ms for e in events), default=change_at)
        before_days = EV.window_days(history_start, change_at)
        rate_before = (TR.jam_rate(len([e for e in before if e.kind == "block"]),
                                   before_days)
                       if before_days > 0 else None)

    since_days = days_since if days_since else EV.window_days(
        min((e.at_ms for e in since), default=now_ms), now_ms)
    blocks_since = [e for e in since if e.kind == "block"]
    rate_since = TR.jam_rate(len(blocks_since), since_days)

    # Meal TIMES come from the rhythm; the trim then lands on the jammiest of them.
    tod = [(minute, max(dur, 1)) for minute, dur in eats]
    curve = circadian_curve(tod)
    split = split_at_peaks(curve, find_peaks(curve))
    blocks_by_time = TR.attribute_blocks(
        [e.minute for e in blocks_since],
        [f"{m // 60:02d}:{m % 60:02d}" for m, _ in split])

    allowed = EV.may_trim(days_since)
    want = TR.trim_amount(rate_since, total) if allowed else 0
    withheld = None if allowed else ("too_early" if days_since is not None
                                     else "no_known_change")

    target_rows = plan_rows(split, total)
    if want > 0:
        target_rows = TR.trim_schedule(target_rows, blocks_by_time, want)
    applied = total - sum(g for _, g in target_rows)

    delivered = sum(e.amount for e in since if e.kind == "dispense_plan")
    return {
        "enabled": enabled, "current": current, "total": total,
        "eating_visits": len(eats), "target_rows": target_rows,
        "trim": {"applied": applied, "jam_rate": rate_since,
                 "blocks_by_time": blocks_by_time, "withheld_reason": withheld},
        "evidence": {
            "days_since_change": days_since,
            "jam_rate_before": rate_before,
            "jam_rate_since": rate_since,
            "delivered_per_day_since": (delivered / since_days
                                        if since_days > 0 else 0.0),
            "scheduled_per_day": total,
            "grain_low_since": len([e for e in since if e.kind == "grain_low"]),
            "verdict": EV.verdict(rate_before, rate_since,
                                  days_since if days_since is not None else 0.0),
        },
    }
```

Update `analyze_rhythm` to take and thread the new parameters and surface the new blocks:

```python
async def analyze_rhythm(config: Config, client: PetLibroClient,
                         pet=None, days: int = 60, now_ms: int | None = None,
                         journal_path=None) -> list[dict]:
    try:
        feeders = config.resolve_feeders("all" if pet is None else [pet])
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]

    now_ms = _now_ms() if now_ms is None else now_ms
    journal_path = _default_journal_path() if journal_path is None else journal_path

    results = []
    for f in feeders:
        try:
            rc = await _compute_rhythm(client, f, days, now_ms, journal_path)
            results.append({
                "pet": f.name, "serial": f.serial, "ok": True,
                "days": days, "eating_visits": rc["eating_visits"],
                "daily_total_portions": rc["total"],
                "current_schedule": [{"time": t, "portions": g} for t, g in rc["current"]],
                "recommended_schedule": [{"time": t, "portions": g} for t, g in rc["target_rows"]],
                "trim": rc["trim"],
                "evidence": rc["evidence"],
            })
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results
```

Add journalling to `apply_target_rows` — give it the same two new parameters and
record only a confirmed write:

```python
async def apply_target_rows(config: Config, client: PetLibroClient, pet,
                            target_rows, apply: bool = False,
                            now_ms: int | None = None, journal_path=None) -> list[dict]:
    """Apply an explicit (e.g. user-edited) target schedule to a feeder."""
    try:
        feeders = config.resolve_feeders(pet)
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]
    now_ms = _now_ms() if now_ms is None else now_ms
    journal_path = _default_journal_path() if journal_path is None else journal_path
    rows = list(target_rows)
    results = []
    for f in feeders:
        try:
            enabled = [p for p in await client.feeding_plans(f.serial) if p.get("enable", True)]
            total = sum(int(p.get("grainNum") or 0) for p in enabled)
            res = await _apply_one(client, f.serial, enabled, total, rows, apply)
            # Journal only a CONFIRMED write: a dry run changed nothing, and a
            # rolled-back failure left the old schedule in force.
            if apply and res.get("ok") and res.get("applied"):
                J.append(journal_path, f.serial, rows,
                         sum(g for _, g in rows), now_ms)
            results.append({"pet": f.name, "serial": f.serial, **res})
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results
```

Apply the same treatment to `apply_schedule`: add `now_ms: int | None = None,
journal_path=None` to its signature, resolve both defaults the same way, pass
`now_ms` and `journal_path` into its `_compute_rhythm` call, and journal after a
confirmed write using `rc["target_rows"]`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_tools.py -q`
Expected: PASS

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **163**

- [ ] **Step 5: Correct the MCP tool description**

In `src/petlibro_mcp/server.py`, the `analyze_rhythm` description currently
promises "(same daily total)", which this change makes false. Replace that
`description` value with:

```python
        description=("Recommend-only: compute each cat's natural eating rhythm "
                     "from ~60d of history and report the current vs. a "
                     "rhythm-timed schedule. May recommend a LOWER daily total "
                     "when the feeder's grain-outlet-block history shows "
                     "over-feeding. No writes. Omit 'pet' for all."),
```

- [ ] **Step 6: Run the suite and commit**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **163**

```bash
git add src/petlibro_mcp/tools.py src/petlibro_mcp/server.py tests/test_tools.py
git commit -m "feat: recommend a lower daily total from jam evidence"
```

---

### Task 7: Expose `trim` and `evidence` through the web API

**Files:**
- Modify: `webui/rhythm_api.py`
- Test: `tests/test_rhythm_api.py`, `tests/test_webapp.py`

**Interfaces:**
- Consumes: `tools.analyze_rhythm` (Task 6).
- Produces: `GET /api/pets/{name}/analysis` response includes `trim` and `evidence` verbatim alongside `rhythm_curve`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rhythm_api.py`:

```python
async def test_analysis_passes_trim_and_evidence_through():
    out = await rhythm_api.analysis(_cfg(), _client(), "ferris")
    assert "trim" in out and "evidence" in out
    assert set(out["trim"]) == {"applied", "jam_rate", "blocks_by_time",
                                "withheld_reason"}
    assert "verdict" in out["evidence"]


async def test_analysis_on_a_feeder_with_no_known_change_withholds_trim():
    """The fixture's plans carry no updateTime, so there is no window to judge."""
    out = await rhythm_api.analysis(_cfg(), _client(), "ferris")
    assert out["trim"]["applied"] == 0
    assert out["trim"]["withheld_reason"] == "no_known_change"
```

Append to `tests/test_webapp.py`:

```python
def test_analysis_endpoint_includes_trim_and_evidence():
    c = TestClient(build_app(_cfg(), _client()))
    body = c.get("/api/pets/ferris/analysis").json()
    assert body["ok"] is True
    assert "trim" in body and "evidence" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_rhythm_api.py tests/test_webapp.py -q`
Expected: FAIL — `KeyError: 'trim'` / `assert "trim" in out`

- [ ] **Step 3: Confirm the passthrough**

`rhythm_api.analysis` already spreads `base` into its return value:

```python
    return {**base, "rhythm_curve": curve}
```

Since Task 6 added `trim` and `evidence` to `analyze_rhythm`'s result, they flow
through unchanged and the tests should pass with **no code change**. Run Step 4;
if they fail, the cause is in Task 6's result shape, not here — fix it there.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_rhythm_api.py tests/test_webapp.py -q`
Expected: PASS

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **166**

- [ ] **Step 5: Commit**

```bash
git add tests/test_rhythm_api.py tests/test_webapp.py
git commit -m "test: pin trim and evidence in the analysis API contract"
```

---

### Task 8: Reanalyze lifecycle in the UI

The button already fires — the request goes out and returns 200 with no console
error — but nothing on screen changes and there is no feedback. Separately, a
*failed* analysis renders no Reanalyze button at all, and `selectPet` early-returns
on the already-selected pet, so a single cloud hiccup strands the user.

There is no JS test framework in this repo (tests are pytest only, and
`index.html` has no build step), so these steps are verified in a real browser.

**Files:**
- Modify: `webui/static/index.html`

**Interfaces:**
- Consumes: `trim` and `evidence` from the analysis response (Task 7).
- Produces: no Python-visible interface.

- [ ] **Step 1: Add the analyzing flag and a previous-plan snapshot to state**

In the `state` object (index.html:280-292), add three fields:

```javascript
    analyzing: false,
    prevPlan: null,
    planNote: null,
```

- [ ] **Step 2: Show a loading state and keep Reanalyze reachable on failure**

In `renderPetView`, replace the `a.ok === false` early-return block
(index.html:678-683) so the actions row is still rendered — this is the dead-end
fix. The window is not known until the response lands, so the loading text stays
window-agnostic:

```javascript
    if (a.ok === false){
      petView.innerHTML =
        '<div class="pet-header"><h1>' + esc(state.selected) + '</h1></div>' +
        '<div class="banner banner-error" role="alert"><span class="banner-icon">⚠</span><span>' +
          esc(a.error || "Could not load analysis.") + '</span></div>' +
        '<div class="card"><h2>Actions</h2>' +
          '<div class="actions-row" id="actions-row">' +
            '<button class="btn btn-ghost" id="btn-reanalyze" type="button"' +
              (state.analyzing ? " disabled" : "") + '>&#8635; ' +
              (state.analyzing ? "Reading history&hellip;" : "Try again") +
            '</button>' +
          '</div></div>';
      document.getElementById("btn-reanalyze")
        .addEventListener("click", function(){ reanalyze(); });
      return;
    }
```

And in the success path, make the Reanalyze button reflect `state.analyzing`
(index.html:712):

```javascript
        '<button class="btn btn-ghost" id="btn-reanalyze" type="button"' +
          (state.analyzing ? " disabled" : "") + '>&#8635; ' +
          (state.analyzing ? "Reading history&hellip;" : "Reanalyze") + '</button>' +
```

- [ ] **Step 3: Render the new-plan banner**

Add a renderer above `renderPetView`:

```javascript
  // A recompute is deterministic given the same history, so an identical plan is a
  // real and common outcome. Saying so is the honest version of the old silent no-op.
  function renderPlanNote(){
    if (!state.planNote) return "";
    var n = state.planNote;
    return '<div class="banner ' + (n.changed ? "banner-success" : "banner-info") + '">' +
      '<span class="banner-icon">' + (n.changed ? "&#10003;" : "&#8635;") + '</span>' +
      '<span>' + n.html + '</span></div>';
  }

  function planNoteFor(a, prev){
    var rows = a.recommended_schedule || [];
    var total = rows.reduce(function(s, r){ return s + r.portions; }, 0);
    var ev = a.evidence || {};
    var trim = a.trim || {};
    var since = ev.days_since_change == null ? null : Math.round(ev.days_since_change);
    var window = since == null ? "all available history"
      : (since + " day" + (since === 1 ? "" : "s") + " since the last change");

    if (trim.withheld_reason === "too_early"){
      return {changed: false, html: "<strong>No trim yet.</strong> The last change is only " +
        esc(since) + " day" + (since === 1 ? "" : "s") + " old &mdash; give it " +
        esc(7 - since) + " more before cutting again. Read over " + esc(window) + "."};
    }
    if (trim.applied > 0){
      var cut = Object.keys(trim.blocks_by_time || {}).sort(function(x, y){
        return trim.blocks_by_time[y] - trim.blocks_by_time[x];
      }).slice(0, 2);
      return {changed: true, html: "<strong>New plan: " + esc(a.daily_total_portions) +
        " &rarr; " + esc(total) + " portions/day.</strong> Trimmed " + esc(trim.applied) +
        " at the jammiest times (" + esc(cut.join(", ")) + "). Jams ran " +
        esc((trim.jam_rate || 0).toFixed(2)) + "/day over " + esc(window) + "."};
    }
    if (prev && JSON.stringify(prev) === JSON.stringify(rows)){
      return {changed: false, html: "<strong>No change.</strong> Same plan as before &mdash; " +
        "jams ran " + esc((trim.jam_rate || 0).toFixed(2)) + "/day over " + esc(window) +
        ", which is not enough to justify a cut."};
    }
    return {changed: true, html: "<strong>Plan recomputed</strong> from " + esc(window) +
      ". Daily total unchanged at " + esc(total) + " portions."};
  }
```

Render it in `renderPetView` immediately after `renderFeedResult()` (index.html:723):

```javascript
      renderFeedResult() +
      renderPlanNote() +
```

- [ ] **Step 4: Show each meal's jam count beside its stepper**

Replace `renderProposedColumn` so it takes the block counts and labels each row.
Keep `min="1"` — Task 2 made a zero mean "delete this meal", which should be a
deliberate act, not one stepper click past 1:

```javascript
  function renderProposedColumn(rows, blocksByTime){
    if (!rows.length){
      return '<div class="empty-row">Nothing proposed.</div>';
    }
    var blocks = blocksByTime || {};
    return rows.map(function(r, i){
      var jams = blocks[r.time] || 0;
      return '<div class="sched-row">' +
        '<span class="sched-time">' + esc(r.time) +
          (jams ? ' <span class="jam-tag" title="grain-outlet blocks near this meal">' +
            esc(jams) + ' jam' + (jams === 1 ? "" : "s") + '</span>' : "") +
        '</span>' +
        '<div class="stepper" data-idx="' + i + '">' +
          '<button class="stepper-btn" type="button" data-action="dec" aria-label="Decrease portions for ' + esc(r.time) + '">&minus;</button>' +
          '<input class="stepper-input" type="number" min="1" max="30" step="1" value="' + esc(r.portions) + '" inputmode="numeric" aria-label="Portions at ' + esc(r.time) + '">' +
          '<button class="stepper-btn" type="button" data-action="inc" aria-label="Increase portions for ' + esc(r.time) + '">&plus;</button>' +
        '</div>' +
      '</div>';
    }).join("");
  }
```

Update its one call site (index.html:700) to pass the counts:

```javascript
            '<div id="proposed-col">' + renderProposedColumn(state.edited, (a.trim || {}).blocks_by_time) + '</div>' +
```

Also raise the stepper's lower clamp from 0 to 1 in `attachStepperHandlers`
(index.html:490 and 500), so the steppers cannot silently delete a meal:

```javascript
      var next = Math.max(1, (Number(state.edited[idx].portions) || 0) + delta);
```

```javascript
      var next = Math.max(1, Math.round(Number(input.value) || 0));
```

- [ ] **Step 5: Confirm before discarding manual edits, and set the flag**

Replace `reanalyze` and `loadAnalysis` (index.html:809-834):

```javascript
  function hasPendingEdits(){
    var a = state.analysis;
    if (!a || !a.recommended_schedule) return false;
    return JSON.stringify(a.recommended_schedule.map(function(r){
      return {time: r.time, portions: r.portions};
    })) !== JSON.stringify(state.edited);
  }

  function loadAnalysis(){
    var token = ++state.requestToken;
    var name = state.selected;
    var prev = state.analysis && state.analysis.recommended_schedule;
    state.analyzing = true;
    renderMain();
    API.analysis(name, 60).then(function(res){
      if (token !== state.requestToken) return; // stale response, ignore
      state.analyzing = false;
      state.analysis = res;
      state.edited = (res.recommended_schedule || []).map(function(r){
        return {time: r.time, portions: r.portions};
      });
      state.resultPanel = null;
      state.planNote = res.ok === false ? null : planNoteFor(res, prev);
      if (res.ok === false) showError(res.error || "Could not load analysis for " + name + ".");
      else clearError();
      renderMain();
    }).catch(function(err){
      if (token !== state.requestToken) return;
      state.analyzing = false;
      state.analysis = {ok: false, error: "Network error while loading analysis: " + err};
      state.planNote = null;
      showError(state.analysis.error);
      renderMain();
    });
  }

  function reanalyze(){
    if (hasPendingEdits() &&
        !window.confirm("Reanalyze will replace your edited portions with a fresh plan. Continue?")){
      return;
    }
    state.resultPanel = null;
    state.planNote = null;
    state.confirmingSave = false;
    state.feedResult = null;   // a stale "Fed 3 portions" must not sit beside a new plan
    loadAnalysis();
  }
```

- [ ] **Step 6: Add styles for the two new classes**

In the `<style>` block, alongside the existing `.banner-*` rules, add:

```css
    .banner-info { background: var(--surface); border-left: 3px solid var(--ink-faint); }
    .jam-tag { font-family: var(--font-mono); font-size: 10.5px; color: var(--rust);
               background: color-mix(in srgb, var(--rust) 12%, transparent);
               border-radius: 3px; padding: 1px 4px; margin-left: 4px; }
```

- [ ] **Step 7: Verify in a real browser**

The deployed UI is at `http://192.168.68.53:8080/`. Restart the service so the
static file is re-served (`ssh root@192.168.68.53 systemctl restart petlibro-web`
— confirm the unit name against `deploy/`), then check each of these:

1. Select **bridget**. While the request is in flight the Reanalyze button reads
   "Reading history…" and is disabled.
2. When it lands, a banner states either a new total or "No change", naming the
   window in days.
3. Meals with jams show a jam tag; bridget's 14:00 and 22:00 should carry the
   highest counts.
4. Step a portion down, then click Reanalyze — a confirm dialog appears. Cancel
   leaves the edit intact.
5. Step a meal down repeatedly — it stops at 1, never 0.
6. Confirm zero console errors throughout.
7. To exercise the dead-end fix, stop the service mid-analysis (or point at an
   offline serial) and confirm the error card now carries a working "Try again"
   button.

- [ ] **Step 8: Commit**

```bash
git add webui/static/index.html
git commit -m "feat: reanalyze feedback, jam counts, and edit protection in the UI"
```

---

### Task 9: UI correctness fixes

Four defects that become materially wrong once totals start moving.

**Files:**
- Modify: `webui/static/index.html`

**Interfaces:**
- Consumes: nothing beyond Task 8.
- Produces: no Python-visible interface.

- [ ] **Step 1: Keep the daily total in sync with the refreshed schedule**

`syncCurrentSchedule` (index.html:516-519) refreshes `current_schedule` but leaves
`daily_total_portions` stale, so `refreshTotals` compares the edited plan against
the *old* total — the over-cap warning and Save's disabled state can both be
wrong right after a save. Replace it:

```javascript
  function syncCurrentSchedule(res){
    if (!res || !res.current_schedule || !state.analysis) return;
    state.analysis.current_schedule = res.current_schedule;
    // The total must move with the rows. Leaving it stale makes refreshTotals
    // compare against a number the device no longer holds, so the over-cap guard
    // and the Save button disagree with what a write would actually be refused for.
    state.analysis.daily_total_portions = res.current_schedule.reduce(
      function(sum, r){ return sum + (Number(r.portions) || 0); }, 0);
  }
```

- [ ] **Step 2: Guard preview, save, and feed against stale responses**

`loadAnalysis` uses `state.requestToken`, but `doPreview`, `doSave`, and `doFeed`
check only `state.selected !== name` — so a preview begun before a Reanalyze can
still paint its result over the newer plan. Capture the token in each and bail if
it moved.

In `doPreview` (index.html:836-848), after `var name = state.selected;` add
`var token = state.requestToken;`, and change the two guards:

```javascript
    API.apply(name, state.edited, false).then(function(res){
      if (state.selected !== name || token !== state.requestToken) return;
      if (res.ok === false) showError(res.error || "Preview failed.");
      renderPreviewResult(res);
    }).catch(function(err){
      if (state.selected !== name || token !== state.requestToken) return;
      renderPreviewResult({ok: false, error: "Network error during preview: " + err});
    });
```

In `doSave` (index.html:855-868), the same — add `var token = state.requestToken;`
and guard both callbacks:

```javascript
    API.apply(name, state.edited, true).then(function(res){
      if (state.selected !== name || token !== state.requestToken) return;
      if (res.ok === false) showError(res.error || "Save failed.");
      else clearError();
      renderApplyResult(res);
    }).catch(function(err){
      if (state.selected !== name || token !== state.requestToken) return;
      renderApplyResult({ok: false, error: "Network error while saving: " + err});
    });
```

In `doFeed` (index.html:646-669), add `var token = state.requestToken;` after
`var portions = state.feedPortions;` and extend both guards the same way.

- [ ] **Step 3: Name the meal in the remove diff**

`renderPreviewResult` (index.html:535-537) prints "row id 5534603 removed", which
tells the user nothing. The removes are plan ids, so look the time up in the
schedule we are showing:

```javascript
    var nowRows = (state.analysis && state.analysis.current_schedule) || [];
    var removes = renderDiffList(res.would_remove, function(id){
      // would_remove carries plan ids; the id->time map is not in the response, so
      // fall back to the id when the row is not in the schedule on screen.
      var hit = null;
      for (var i = 0; i < nowRows.length; i++){
        if (nowRows[i].id === id){ hit = nowRows[i]; break; }
      }
      return hit ? (esc(hit.time) + " &mdash; meal removed")
                 : ("row id " + esc(id) + " removed");
    });
```

- [ ] **Step 4: Verify in the browser**

Restart the service and confirm:

1. Select **bridget**, click **Preview changes** — the Remove column names a time
   (or falls back to an id) rather than only an id.
2. Save a schedule, then read the "Daily total X → Y" readout: X must be the
   number just written, not the pre-save one.
3. Click **Preview changes** and immediately click **Reanalyze**; the preview
   result must not appear over the new plan.
4. Zero console errors.

- [ ] **Step 5: Commit**

```bash
git add webui/static/index.html
git commit -m "fix: stale total after save, stale in-flight results, remove-diff labels"
```

---

### Task 10: End-to-end verification against the live fleet

**Files:**
- Create: `scripts/dump_trim.py`

**Interfaces:**
- Consumes: `tools.analyze_rhythm` (Task 6).
- Produces: a read-only report; no writes to any device.

- [ ] **Step 1: Write the read-only report script**

Create `scripts/dump_trim.py`, following the `.env` bootstrap pattern the other
scripts in that directory already use:

```python
"""Read-only: what would Reanalyze now propose for every cat, and why?"""
import asyncio, os
from datetime import datetime, timezone
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp import tools

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


async def main():
    cfg = load_config(str(ROOT / "pets.toml"))
    client = PetLibroClient(cfg)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for res in await tools.analyze_rhythm(cfg, client, None, 60, now_ms=now_ms):
        if not res.get("ok"):
            print(f"{res['pet']:9s} ERROR {res.get('error')}")
            continue
        ev, tr = res["evidence"], res["trim"]
        now_total = res["daily_total_portions"]
        rec = sum(r["portions"] for r in res["recommended_schedule"])
        since = ev["days_since_change"]
        print(f"\n{res['pet']:9s} {now_total} -> {rec} portions/day"
              f"   trim={tr['applied']} withheld={tr['withheld_reason']}")
        print(f"  jams {tr['jam_rate']:.2f}/day"
              f"   days_since_change={since if since is None else round(since)}"
              f"   verdict={ev['verdict']}   grain_low={ev['grain_low_since']}")
        print(f"  delivered {ev['delivered_per_day_since']:.1f}/day"
              f" of {ev['scheduled_per_day']} scheduled")
        print(f"  proposed: {[(r['time'], r['portions']) for r in res['recommended_schedule']]}")

asyncio.run(main())
```

- [ ] **Step 2: Run it and sanity-check against the spec's table**

Run: `.venv/bin/python scripts/dump_trim.py`

Expected, from the jam rates measured while writing the spec:
- **ricco** (0.15 jams/day) is trimmed **0** — the dead band holds.
- **bridget** (1.07/day) is proposed a lower total than 12, cut at her jammiest
  times, unless her window is under 7 days in which case `withheld_reason` reads
  `too_early`.
- No cat is ever proposed below **8** portions/day.
- No meal in any `proposed` list is below **1**.
- `delivered/day` is now scheduled-only, so it should read **lower** than the
  spec table's manual-inclusive figures — most visibly for zeus, whose 35 manual
  feeds inflated that column by roughly 0.8/day.

If any of those four invariants is violated, stop and fix the responsible module
before proceeding — they are the guarantees the whole design rests on.

- [ ] **Step 3: Run the full suite one last time**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, **166**

- [ ] **Step 4: Commit**

```bash
git add scripts/dump_trim.py
git commit -m "chore: read-only script reporting the proposed trim per cat"
```

---

## Deliberately out of scope

`feeder_status` does not surface `desiccantState` (bridget's is 169 days overdue),
`remainingDesiccantDays`, or the live `exceptionMessage` ("Foreign objects in the
food outlet"). Confirmed with the user as **dropped**, not deferred — it is a
status-reporting change with its own scope, and it is not needed for the trim to
work.
