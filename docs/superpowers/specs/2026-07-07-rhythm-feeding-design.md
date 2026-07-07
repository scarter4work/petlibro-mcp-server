# Rhythm-Based Feeding Schedules — Design

**Date:** 2026-07-07
**Status:** Design — pending user approval
**Author:** scarter4work (with Claude)

## Purpose

Derive each cat's feeding schedule — **when** to dispense and **how much** — purely
from cloud history, independent of human assumptions about who is "overfed." The
core idea: **feed by rhythm.** Each cat has a distinct daily eating pattern; the
schedule should place meals at the times the cat actually eats, split the day's
food across those times by demand, and let the daily total self-calibrate to the
smallest amount that doesn't leave the cat hungry.

Supersedes the two relevant non-goals in `2026-07-04-petlibro-mcp-design.md`
("No schedule editing", "No RFID event history") — both are now in scope.

## Motivating evidence (from live account, 2026-07-07)

All six feeders are currently on an identical 1.00 cup/day (12-portion) plan, so
the current config encodes no per-cat tailoring at all.

The `/device/workRecord/list` endpoint retains **~60 days** of per-cat history
(117–209 dispense events and 730–4,483 eating events per feeder). Because each cat
is RFID-bound to its own feeder, every event is already attributed to one cat.
Two event types matter:

- `PET_IDENTIFY_LEAVE_EVENT_BIND_PET` — a **visit**: timestamp + eating duration
  (e.g. `"Saffron came to eat and ate for 01m37s"`).
- `GRAIN_OUTPUT_SUCCESS` / `FEEDING_PLAN_SUCCESS` — a **dispense**: timestamp +
  `expectGrainNum` / `actualGrainNum`.

**Key limitation:** the bowl scale reads 0 (`weight`/`weightPercent` inactive), so
we can observe *when* and *how long* a cat eats and *what was dispensed*, but never
grams left in the bowl. Absolute grams consumed has no ground truth. This is why the
daily total is a control problem (§3), not a direct calculation.

Candidate supply signals show real variance but are entangled with eating *style*
(gorger vs. grazer vs. responder):

| Cat | uptake@30m | forage>3h |
|-----|-----------|-----------|
| saffron | 0.32 | 0.78 |
| ricco | 0.82 | 0.51 |
| zeus | 0.42 | 0.44 |
| ferris | 0.70 | 0.61 |
| bridget | 0.64 | 0.61 |
| colby | 0.65 | 0.56 |

A single cross-cat threshold would misread style as over-supply (e.g. zeus the
gorger and saffron the over-supplied grazer both show low uptake, for different
reasons). **Therefore the controller runs per-cat closed-loop, never comparing cats.**

## Non-goals

- No cross-cat comparison in the amount logic — each cat is tuned against its own
  baseline only.
- No gram-level intake measurement — not available on this hardware; the design
  works around it via behavioral signals.
- No fountain involvement — feeders only.
- No natural-language work in new modules — pure functions + typed MCP tools,
  matching the existing server pattern.

## Approach

Chosen: **Rhythm engine + self-calibrating total** ("Approach A"). Rejected
alternatives: one-shot proportional total (feeds heavy eaters most — entrenches
overfeeding) and timing-only with a flat human-set trim (dodges the data-driven
amount goal).

The system has three sub-problems with very different data support:

### §1 Data foundation

Generalize the currently-hardcoded `workRecord` call into
`work_record(serial, days=60, size=1000)`. Parse the response into two per-cat
event series:

- `eats: list[(ts, duration_s)]`
- `dispenses: list[(ts, grain)]`

Parsing is pure (no cloud coupling) and unit-tested against fixture JSON. Duration
strings (`"01m37s"`, `"24s"`, `"1h02m03s"`) parse via a small regex helper.

### §2 Rhythm engine (WHEN + SPLIT) — pure, no I/O

- **Circadian curve:** bin eating visits into the 24h clock (30-min bins), weighting
  each visit by its duration, with wrap-around smoothing (23:45 and 00:15 are
  neighbors).
- **Peaks:** find local maxima by prominence → the cat's natural meal times. Peak
  *count* is data-chosen (gorger → fewer, grazer → more), clamped to a sane range
  (e.g. 2–6 meals/day).
- **Split:** each meal's portion ∝ the eating-demand mass in the window around its
  peak, normalized to the daily total. Strong peak → big portion; weak peak → token.

§2 output alone — per-cat meal times + relative split — is deliverable with zero
writes (Phase 1).

### §3 Self-calibrating daily total — the controller

- **Objective:** the smallest daily total that keeps the cat *out of foraging*.
  Foraging-pressure (eating mass occurring long after the last dispense — hungry
  between meals) is the **floor** signal.
- **Observation:** per cat, over a trailing 7–14 day window, compute the composite
  supply signal (foraging-pressure, cross-checked with uptake), always vs. that
  cat's own rolling baseline.
- **Mechanism:** slow bang-bang / hill-climb. Each weekly cycle: foraging-pressure
  at/below baseline → decrement total by one portion; above the hysteresis band →
  increment (back off) and record that level as the floor estimate. Converges to
  *floor + small margin*.
- **Guardrails:** hard clamps (never above current total; never below an
  absolute-min portions/day), one step per cycle, hysteresis band against
  oscillation, and **hold** (no change) on any cycle with insufficient data.
- **Seed:** starts at today's total and walks *down* only until the data says stop.
- **State:** persist per-cat `{current_total, floor_estimate, last_adjust_ts,
  signal_history}` so the loop closes across runs.

### §4 Writeback + safety

The dry-feeder plan-*edit* endpoint is not in the vendored client (only
enable/disable + list). Phase 2 opens with a **de-risking spike**:

1. Snapshot one feeder's current plan JSON (already captured for all six).
2. Try editing a single row's `grainNum` against the likely endpoints
   (`/device/feedingPlan/save` | `edit` | `update`).
3. Verify via `feedingPlan/list` + `feedingPlan/todayNew`.
4. **Restore from snapshot.**

Only after one confirmed round-trip do we generalize. Every apply
snapshots-before-write (verbatim rollback), reads-back-to-confirm, inherits the
existing overfeed-cap guard, and never writes a total above current.

## Component architecture

New modules, each with one job (mostly pure → testable):

| Module | Responsibility | Depends on |
|--------|---------------|-----------|
| `history.py` | fetch (`work_record`) + parse into event series | vendored api |
| `rhythm.py` | circadian curve → peaks → times + split (pure) | — |
| `controller.py` | supply signals + step function + state I/O | history |
| `planner.py` | combine rhythm × total → `(time, grainNum)` rows (pure) | rhythm |
| `client.py` / `vendored/api.py` | `work_record`, plan-edit writeback | — |
| `tools.py` / `server.py` | new MCP tools | all above |

State file: per-cat controller state persisted as JSON alongside config.

## MCP tools

- `analyze_rhythm(pet | "all")` — recommend-only report: computed meal times, split,
  and the current-vs-recommended schedule. No writes.
- `apply_schedule(pet | "all")` — guarded write of the computed schedule; snapshots,
  writes, reads back to confirm.
- `tune_amounts(pet | "all")` — run one controller cycle (compute signal, step the
  total, persist state). Intended to be driven periodically (cron/script).

## Phasing

Each phase is independently valuable and shippable:

1. **§1 + §2 + `analyze_rhythm`** — recommend-only "feed by rhythm" report, zero writes.
2. **Writeback spike + `apply_schedule`** — guarded, verified, reversible.
3. **§3 controller + state + `tune_amounts` + periodic runner** — self-calibration.

## Testing

- `rhythm.py`, `controller.py`, `planner.py`: unit-tested against fixture event
  series, including synthetic gorger / grazer / responder profiles.
- Writeback: proven by the Phase-2 spike round-trip (snapshot → edit → verify →
  restore).
- MCP tools: tested with a fake client, matching the existing `tests/` pattern.

## Risks & mitigations

- **Undocumented edit endpoint** → de-risking spike + verbatim snapshot rollback
  before generalizing.
- **Weak total signal on gravity feeders** → controller is per-cat closed-loop and
  moves in small steps with a hold-on-sparse-data rule; if the signal proves too
  weak, amount logic gracefully degrades to holding the total constant while §2
  timing still applies.
- **History caps** → the parameterized `size`/`days` pull confirmed ~60 days
  available; queries use that window.
