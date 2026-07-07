# Phase 2: Schedule Writeback — Design

**Date:** 2026-07-07
**Status:** Design — pending user approval
**Author:** scarter4work (with Claude)

## Purpose

Let the rhythm-tailored schedule computed in Phase 1 be written back to the
feeders — the "save" step. Adds an `apply_schedule` MCP tool that takes a cat's
rhythm recommendation and edits its feeding plan on the PetLibro cloud, safely
and reversibly. This is the first feature in the project that **writes** recurring
schedules to live feeders.

Expands the deferred §4 of `2026-07-07-rhythm-feeding-design.md` (which assumed the
plan-edit endpoint had to be reverse-engineered). It does not.

## Key finding: the endpoint is already known (no reverse-engineering)

The actively-maintained community Home Assistant integration
[`jjjonesjr33/petlibro`](https://github.com/jjjonesjr33/petlibro) — sibling of our
vendored client, and it explicitly supports our exact device (`one_rfid_smart_feeder.py`,
PLAF301) — implements the full dry-feeder plan CRUD against the real cloud API. The
endpoints and payloads (verified in `custom_components/petlibro/api.py`):

- **`/device/feedingPlan/update`** — edit an existing row by `id`:
  `{id, deviceSn, executionTime, repeatDay, label, enable, enableAudio, audioTimes, grainNum, petIds}`
- **`/device/feedingPlan/add`** — create a row (`id: 0`, same shape)
- **`/device/feedingPlan/remove`** — delete a row: `{deviceSn, planId}`
- **`/device/feedingPlan/enable`** — toggle: `{deviceSn, planId, enable}`

These fields map 1:1 onto the plan objects we already dump via
`/device/feedingPlan/list` in Phase 1 (`id`, `executionTime`, `repeatDay`, `label`,
`enable`, `enableAudio`, `audioTimes`, `grainNum`). So any target schedule can be
expressed as a diff of update/add/remove calls, and the risky "discover the endpoint"
work collapses to "confirm the known payload round-trips on one real feeder."

(Aside: a public security write-up notes PetLibro's cloud is loosely authenticated.
Not blocking — we only use our own account's endpoints, same as the app — but a reason
to keep the write surface minimal and never broaden it.)

## Approach

Chosen (from brainstorming): **edit-in-place diff** for applying, and a
**one-feeder snapshot/restore spike** to prove the payload before building the tool.
Rejected: replace-all (brief zero-schedule window, discards ids) and update-only
(can't change meal count, which the rhythm engine needs).

### Components

**§1 Client facade write methods** (`client.py`, calling `self._api.session.post`
directly — the vendored client is NOT modified, matching Phase 1's `work_record`):
- `update_plan(serial: str, plan: dict) -> None` → `/device/feedingPlan/update`
- `add_plan(serial: str, plan: dict) -> None` → `/device/feedingPlan/add`
- `remove_plan(serial: str, plan_id: int) -> None` → `/device/feedingPlan/remove`

Payloads exactly as the community integration sends them (above).

**§2 Diff planner** (new pure module, e.g. `schedule_diff.py`, fully unit-testable):
- `diff_schedule(current_enabled: list[dict], target_rows: list[tuple[str, int]]) -> ScheduleActions`
- `ScheduleActions = {updates: list[dict], adds: list[dict], removes: list[int]}`
- Sort `current_enabled` and `target_rows` by time; pair by index:
  - overlap → an `update` payload carrying the existing row's `id`,
    `repeatDay`, `label`, `enableAudio`, `audioTimes` unchanged, with the target's
    `executionTime` + `grainNum`.
  - extra target rows → `add` payloads, inheriting `repeatDay`/`label`/`enableAudio`/
    `audioTimes` from a template (the first enabled current row) so audio/repeat stay
    consistent with the cat's existing setup.
  - surplus current rows → `remove` by `id`.
- Disabled rows are left untouched (not in `current_enabled`).
- Pure: no I/O, deterministic.

**§3 `apply_schedule` tool** (`tools.py`):
- `apply_schedule(config, client, pet, days=60, apply=False) -> list[dict]`
- Reuses the Phase-1 rhythm computation. To avoid duplication, extract the shared
  core of `analyze_rhythm` into a helper (e.g. `_compute_rhythm(client, feeder, days)
  -> {current, target_rows, total, eating_visits}`) that both tools call.
- Flow per feeder:
  1. **Snapshot** current plan (`client.feeding_plans`).
  2. Compute `target_rows` (rhythm) and the diff vs. enabled current rows.
  3. If **`apply` is False (default)** → return the diff as a preview (`would_update`/
     `would_add`/`would_remove`), no writes.
  4. If `apply` is True → execute removes, adds, updates; **read back**
     (`feeding_plans`); **verify** the resulting enabled schedule matches `target_rows`.
  5. On mismatch → **rollback** by diffing the broken readback back to the snapshot and
     applying that; re-verify. If rollback also fails → surface a loud error including
     the exact snapshot JSON for manual restore. Never leave a silently half-applied plan.

**§4 Register** `apply_schedule` on the MCP server (`server.py`): `Tool` def with schema
`{pet: string (required), days?: integer, apply?: boolean}` + dispatch branch.

### Safety model

- **Dry-run is the default.** Writing requires `apply=true` explicitly.
- **Total guard:** refuse to apply if the target daily total exceeds the current
  enabled total. The rhythm pipeline preserves the total, so this only fires on a bug —
  it's a backstop against ever *increasing* a cat's food. Also inherit the existing
  overfeed cap semantics.
- **Snapshot → verify-readback → rollback-on-mismatch**, as above.
- **Errors surface, never swallowed** (per project convention): per-feeder
  `{ok: False, error}` entries.

### The live spike (run once, supervised, before building §3)

Prove the payload against the real cloud on exactly one feeder:
1. Pick one feeder at a quiet time (far from any of its `executionTime`s — editing a
   plan does not dispense, but avoid the window regardless).
2. Snapshot its exact current plan JSON.
3. `update_plan` one row's `grainNum` by +1.
4. Read back via `feeding_plans`; confirm the change took.
5. Restore the snapshot verbatim (`update_plan` the row back); confirm restoration.

Net-zero change. Gates building the `apply_schedule` tool.

## Non-goals (Phase 2)

- No self-calibrating amounts (that's Phase 3 — this writes whatever total Phase 1's
  rhythm preserves).
- No bulk "apply all cats" in one shot for the first cut — `apply_schedule` takes one
  `pet` (an explicit, reviewable write). "all" can come later once trusted.
- No wet-feeding-plan support (different endpoints; our feeders are dry PLAF301).
- No changes to `vendored/`.

## Component architecture

| Module | Responsibility | New/changed |
|--------|---------------|-------------|
| `client.py` | `update_plan` / `add_plan` / `remove_plan` facade writes | changed |
| `schedule_diff.py` | pure diff: current + target → update/add/remove actions | new |
| `tools.py` | `_compute_rhythm` helper (extracted) + `apply_schedule` | changed |
| `server.py` | register `apply_schedule` | changed |

## Testing

- `schedule_diff.py`: unit-tested against fixtures — equal counts (all updates),
  target longer (updates + adds), target shorter (updates + removes), empty target
  (all removes), field preservation (repeatDay/label/audio carried on update).
- `client.py` writes: mocked `session.post`, assert endpoint + payload shape matches
  the community-proven schema.
- `apply_schedule`: fake client — dry-run returns a preview with no writes; apply path
  executes the diff, reads back, verifies; a forced-mismatch readback triggers rollback;
  total-guard refuses an over-total target.
- Live spike: the snapshot→edit→verify→restore round-trip (one real feeder).

## Risks & mitigations

- **A payload field differs for our firmware** → the live spike catches it before any
  tool is built; snapshot guarantees restore.
- **Partial write (some rows applied, then a call fails)** → verify-readback detects the
  divergence; rollback restores; loud error with snapshot if rollback can't.
- **Accidental over-feeding** → total guard + overfeed cap + dry-run default.
- **Editing during a scheduled feed** → spike and tool avoid execution-time windows;
  plan edits don't themselves dispense.
