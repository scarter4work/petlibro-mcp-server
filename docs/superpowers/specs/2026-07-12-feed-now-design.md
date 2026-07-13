# Feed Now — design

Date: 2026-07-12

## Problem

The web UI can analyze rhythm and write schedules, but there is no way to dispense
food immediately. The MCP server has a guarded `feed` tool; the dashboard does not
expose it. Feeding by hand currently means dropping to the MCP tool or the vendor app.

## Goal

A "Feed now" control in the pet header: a portion spinner plus a button that
dispenses immediately to the selected pet's feeder.

## Units and the guard

`tools.feed()` is the existing guarded write path. It takes **cups**, converts with
`cups_to_portions(feeder, cups)`, refuses non-positive amounts, and refuses anything
over `config.max_cups_per_command` (4) unless `force=true`.

The UI speaks **portions** everywhere. `portions_per_cup` is a per-feeder field whose
global default is 12; `pets.toml` currently sets it to 1, so cups and portions happen
to coincide today. That is config, not an invariant.

Therefore: convert at the web boundary (`cups = portions / feeder.portions_per_cup`)
and delegate to `tools.feed()`. One guarded write path, and the cap keeps its meaning
if the feeder is ever recalibrated.

## API

`POST /api/pets/{name}/feed`, body `{"portions": int}`.

**`force` is deliberately not part of the wire contract.** `tools.feed` supports a
`force=true` override (the MCP path uses it), but the dashboard is unauthenticated on
the LAN, and the cap is the only thing between a stray request and an emptied hopper.
Accepting `force` from the client would let any caller switch off the one guard that
matters, so the endpoint pins it to `False`. If a force path is ever needed it belongs
behind an authenticated admin route, not this one.

Returns the `tools.feed()` result dict for that feeder:
`{"pet", "cups", "portions", "ok", "error"}`. Failures return HTTP 200 with
`ok: false` and the real error text — never a silent fallback or a generic message.

## UI

- Pet header, right of the serial badge: existing `.stepper` component (− / N / +)
  bounded 1–4, default 1, plus a `Feed now` button.
- Below the cap: one click dispenses.
- At the cap (4): the button enters a confirm state — `Feed 4 portions to zeus?`
  `[Cancel] [Feed]` — and only the second click sends the request. `force` stays
  `false`; the browser bound is convenience, the server cap is the guarantee.
- In flight: button disabled, reads `Feeding…`.
- Success: existing `banner-success` — "Fed 2 portions to zeus." Spinner resets to 1.
- Failure: existing `banner-error` with the verbatim error from the API.

## Deliberately not doing

No analysis refresh after a feed. A manual feed does not change the schedule, and
re-running `analyze_rhythm` is a 60-day history fetch that would also discard pending
edits in the Proposed column.

## Testing

- `rhythm_api.feed` converts portions → cups for `portions_per_cup` of 1 and 12.
- Over-cap portions are refused (`ok: false`, cap message).
- Non-positive portions are refused.
- Unknown pet returns the `UnknownPetError` message.
- Endpoint wiring: `POST /api/pets/{name}/feed` reaches the tool with the right args.
