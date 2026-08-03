# Reanalyze: jam-aware portion trimming, with an evidence loop

**Date:** 2026-08-03
**Status:** approved, ready to plan

## Problem

The Reanalyze button in the web UI appears to do nothing, and the schedules on all
six feeders are too aggressive with no way to bring them down.

Two independent causes, both verified against the live system:

### 1. Reanalyze is a no-op by construction

`tools._compute_rhythm` sets the recommended daily total from the schedule
already on the feeder:

```python
current = [(p.get("executionTime"), int(p.get("grainNum") or 0)) for p in enabled]
total = sum(g for _, g in current)
```

It then redistributes exactly that total across the peaks of the eating curve.
The recommendation is a pure function of (eating history, current total), and
eating history barely moves day to day — so a recompute returns the same plan.
`_apply_one` additionally refuses any target total greater than the current one,
so the system can only ever hold or lower the total, while the recommender never
proposes a lower one. There is no code path from "12 portions/day, too aggressive"
to a smaller number.

Confirmed in the browser: clicking Reanalyze issues
`GET /api/pets/bridget/analysis?days=60` → 200, zero console errors, and the page
repaints identically ("Daily total 12→12 portions"). There is also no loading
state during the multi-second round trip, so nothing indicates it ran.

### 2. A failed analysis is an unrecoverable dead end

`renderPetView` (index.html:678) returns early when `a.ok === false`, before the
actions row is rendered — so a failed analysis shows **no Reanalyze button**.
`selectPet` (index.html:793) early-returns when the pet is already selected, so
re-clicking the same cat does nothing. Recovery requires selecting another cat and
returning, or reloading the page.

## Evidence from the live fleet

All six cats are scheduled at exactly **12 portions/day** — a flat default nobody
tuned. That is the root of "too aggressive."

Every feeder logs `GRAIN_OUTLET_BLOCK_REMOVED` ("The grain outlet is blocked,
cleared"), which `history.py` does not parse:

| cat | window | blocks | blocks/day | scheduled/day | delivered/day |
|---------|--------|--------|------------|---------------|---------------|
| bridget | 27d | 29 | 1.07 | 12 | 9.7 |
| ferris | 13d | 12 | 0.92 | 12 | 7.7 |
| colby | 30d | 24 | 0.80 | 12 | 9.4 |
| zeus | 43d | 31 | 0.72 | 12 | 10.7 |
| saffron | 29d | 20 | 0.69 | 12 | 7.1 |
| ricco | 20d | 3 | 0.15 | 12 | 12.0 |

The delivered/day column above is measured over the whole returned history and
**includes manual feeds**, because that is what `parse_work_record` currently
returns (see finding 4). zeus's 35 manual feeds over 43 days inflate his figure by
roughly 0.8/day. Scheduled-only delivery will be lower for every cat; the fix is
in scope, and the evidence loop must use the scheduled-only figure.

**Jam rate does not track meal size — it tracks time of day.** colby's jammiest
slot is 14:00 with a 1-portion meal (8 blocks); ricco dispenses 5 portions at
05:00 and jammed once in 20 days; bridget's 3-portion 10:30 meal is clean while
her 2-portion 14:00 meal jammed 10 times. So "shrink the big meals" would be the
wrong rule. A block marks *food already sitting in the bowl at that hour* —
cumulative surplus, i.e. fed too often around that time.

**There is no bowl scale on this hardware.** `realInfo` reports `weight: 0.0`,
`weightPercent: 0`, `weightState: "NORMAL"` on feeders — those are fountain
fields. No grams, no intake field. Eating *seconds* are the only time-resolved
consumption proxy, and they cannot be trusted as absolute intake: ferris logs
3316 eating-seconds/day across 72 visits/day on only 7.7 portions delivered
(431 s/portion vs bridget's 110 s/portion). A cat at an *empty* bowl still logs
eating time, so high seconds can mean hungry, not well-fed.

**ferris and saffron are a supply failure, not over-feeding.** They deliver 7.7
and 7.1 of 12 scheduled portions, and ferris reported
`FEEDING_PLAN_GRAIN_NOT_ENOUGH` on 11 of 42 dispense events. Trimming their
schedule is still safe — it makes the plan honest about what the machine
delivers — but it will not fix them, so it must come with a warning rather than
silently looking like a diet fix.

## Design

### Trim policy (pure, in a new `trim.py`)

- **Size:** `trim = round(TRIM_PER_JAM_PER_DAY × jams_per_day)` with
  `TRIM_PER_JAM_PER_DAY = 2`. The dead band falls out naturally — below 0.25
  jams/day rounds to 0, so ricco is never touched. On today's data: bridget −2,
  ferris −2, colby −2, zeus −1, saffron −1, ricco 0.
- **Window:** jam rate is measured over the *since-last-change* window defined by
  the evidence loop below — not a fixed 14 days. That window is what makes the
  rate attributable to the schedule currently in force. A trim requires at least
  7 days in it (see `too_early`).
- **Placement:** blocks are attributed to the nearest *proposed* meal time
  (circular distance, same helper as `rhythm._circ_dist`), and portions come off
  the jammiest slots first.
- **Never below 1 portion per meal.** If the jammiest slot is already at 1, the
  cut moves to the next-jammiest. This matters because `plan_rows` silently drops
  zero-portion rows, so an unguarded trim would delete a meal rather than shrink
  it.
- **Hard floor of 8 portions/day**, as a single named module constant
  (`MIN_DAILY_PORTIONS`) so it is trivial to revise per-fleet later. Maximum
  journey is 12→8 over several weeks. Not per-cat configurable in this work —
  all six cats currently share the same 12, so a global floor is sufficient and
  avoids adding `pets.toml` surface area we cannot yet justify.

### Evidence loop (new `evidence.py`)

The last schedule change is dated from the device: `max(updateTime)` across
enabled plan rows. bridget's rows all read `updateTime: 1783503554000` (one
transaction, 26 days ago); ricco's read `1783881119000`.

Because a pure-deletion apply leaves surviving rows' `updateTime` stale, our own
apply path also appends `(timestamp, schedule, total)` to a local JSON journal.
The journal is authoritative; the device's `updateTime` is the fallback for edits
made in the PetLibro phone app.

The since-change window is clamped to the history actually returned: if the last
change predates the fetched history (changed 90 days ago, 60 days fetched), the
window is the fetched span and the "before" period is empty — reported as
unavailable rather than computed from nothing.

The analysis response gains an `evidence` block: days since last change, jam rate
before vs since, delivered-vs-scheduled for both periods, and a verdict —
`improving` / `no_change` / `worse` / `too_early` (under 7 days of post-change
data).

**When the verdict is `too_early`, the recompute still runs and still shows the
numbers, but the trim is withheld** with "give the last change N more days." This
is what prevents a 12→10→8 ratchet: the trim is computed against the total
currently on the feeder, so without this guard, three clicks off the same 60-day
history would trim three times before the first change had any chance to show an
effect.

Retroactive comparison is thin and the UI must say so rather than fake it:
bridget's last change was 26 days ago and her history returns only 27 day-groups,
leaving ~1 day of pre-change data. Clean before/after starts from the next change
applied through this tool.

### UI (`webui/static/index.html`)

- **Recompute is visibly in flight** — the proposed column shows a loading state.
  Once the response lands, the window it actually used is named ("26 days since
  the last change"); the loading text itself stays window-agnostic, since the
  window is not known until the device reports `updateTime`.
- **A new plan announces itself** — a banner stating the daily-total delta and
  where the cut landed ("New plan: 12 → 10 portions/day, trimmed at 14:00 and
  22:00"), plus a "why" line naming the jam counts behind it.
- **An identical plan says so explicitly** — "No change; same plan as before."
  This is the honest version of today's silent no-op.
- **Manual controls rebuild against the new plan** and stay adjustable, with each
  row's jam count shown beside it so the machine's call can be overridden.
- **Pending edits are not silently destroyed** — if the steppers have been
  touched, Reanalyze confirms before overwriting.
- **Reanalyze is always reachable**, including on a failed analysis (fixes the
  dead end above).

### Code structure

- `history.py` — add `parse_device_events`, returning block and grain-low events
  with time-of-day, alongside the existing eats/dispenses. Split scheduled from
  manual dispenses (see finding 8).
- `trim.py` (new) — `jam_rate`, `attribute_blocks`, `trim_schedule`. Pure, so the
  health-critical arithmetic is unit-testable without the cloud.
- `evidence.py` (new) — `last_change_at`, `compare_windows`, `verdict`.
- `journal.py` (new) — append-only applied-schedule log.
- `tools.py` — `_compute_rhythm` stops hardcoding `total = current` and takes the
  trimmed total; `analyze_rhythm` returns the `evidence` and `trim` blocks.
- `server.py` — the `analyze_rhythm` tool description currently promises "same
  daily total," which this change makes false. Update it.

### Testing

TDD, each guard proven red before green: dead band, floor of 8, min 1 per meal,
cut-moves-to-next-jammiest, `too_early` withholds the trim, manual feeds excluded
from delivery. Existing suite is 105 tests across 12 files, all green at baseline;
follow its fixture style.

## Adjacent defects found in review

In scope for this work (they interact with it directly):

1. **Failed analysis is a dead end** — no Reanalyze button, and re-clicking the
   same pet is a no-op. index.html:678, 793.
2. **Steppers can zero a meal and write `grainNum: 0`.** `min="0"` plus
   `Math.max(0, …)` lets a row reach 0; `diff_schedule` has no zero filter, so it
   emits an update with `grainNum: 0`, while the recommend path drops zero rows in
   `plan_rows`. The two paths disagree about what zero means. Fix: treat a zeroed
   row as a removal in `diff_schedule`, matching `plan_rows`.
   schedule_diff.py:41-52, index.html:455, 490.
3. **`syncCurrentSchedule` desynchronizes the total.** It refreshes
   `current_schedule` but leaves `daily_total_portions` stale, so `refreshTotals`
   compares against the old number — the over-cap warning and Save's disabled
   state can both be wrong after an apply. Becomes materially wrong once trims
   change totals. index.html:516-519, 462-481.
4. **Manual feeds inflate the delivery metric.** `parse_work_record` lumps
   `MANUAL_FEEDING_SUCCESS` in with `FEEDING_PLAN_SUCCESS` under
   `GRAIN_OUTPUT_SUCCESS`; zeus has 35 manual feeds in 43 days. The new
   delivered-vs-scheduled comparison must exclude them. history.py:63-64.
5. **Reanalyze silently discards manual edits** — `loadAnalysis` overwrites
   `state.edited` unconditionally. index.html:815-817.

Also fixed here, cheap and adjacent:

6. **Stale in-flight results paint over a newer analysis.** `loadAnalysis` guards
   with `requestToken`, but `doPreview`/`doSave`/`doFeed` guard only on
   `state.selected !== name`. Extend the token guard to all four.
   index.html:841, 860, 653.
7. **Remove-diff shows raw plan ids** ("row id 5534603 removed") instead of the
   meal time. index.html:535-537.
8. **`state.feedResult` survives a reanalyze**, leaving a stale "Fed 3 portions"
   banner beside a fresh plan. index.html:830-834.

Explicitly out of scope, not deferred as tickets:

9. **Maintenance fields are not surfaced.** `data/realInfo` reports
   `desiccantState: "OVER_DUE"`, `remainingDesiccantDays: -169`, and
   `exceptionMessage: "Foreign objects in the food outlet"` (live on bridget) —
   none of which `feeder_status` returns. Relevant to jamming, but it is a
   status-reporting change with its own scope; **dropped from this work
   deliberately**, to be raised as its own piece if the trim results suggest
   physical maintenance is the dominant factor.

## Non-goals

- No auto-apply. Every schedule write stays behind the existing preview →
  confirm → verify → rollback path.
- No change to the manual `feed` cap or its guard.
- No attempt to infer grams. The hardware has no scale; portions stay the unit.
