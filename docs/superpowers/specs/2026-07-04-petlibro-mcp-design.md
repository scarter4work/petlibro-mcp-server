# PetLibro MCP Server — Design

**Date:** 2026-07-04
**Status:** Design — pending user approval
**Author:** scarter4work (with Claude)

## Purpose

Expose control of 6 PetLibro RFID smart feeders and 2 Dockstream smart fountains
to Claude via a Model Context Protocol (MCP) server, so commands like
"feed ferris 3 cups", "feed all", or "feed zeus and colby 2 cups" work
conversationally from any Claude client.

Mirrors the existing `deco-mcp-server` pattern: a thin, structured MCP wrapper
around a reverse-engineered cloud API client vendored from a community Home
Assistant integration.

## Non-goals (V1)

- No natural-language parsing in the server — Claude does that and calls
  structured tools. The server exposes clean, typed tools only.
- No local/LAN control — PetLibro's cloud is proprietary (non-Tuya), so all
  calls go through PetLibro's cloud API, same as the official app.
- No live camera/video (the feeders' stream API is not covered by the
  community client).
- No schedule editing in V1 — read-only for schedules; manual feed is the write
  path. (Deferred, not dropped.)
- No fountain control beyond status in V1 — association to pets is fuzzy by the
  owner's own description, so fountains are status-only (water level, pump,
  filter life).

## Architecture

Follows the `deco-mcp-server` layout exactly:

```
petlibro/
  pyproject.toml              # hatchling, mcp>=1.9, aiohttp, async-timeout
  pets.toml                   # the pet<->feeder<->chip map + calibration (NOT secret)
  README.md
  src/petlibro_mcp/
    __init__.py
    server.py                 # MCP entry point; registers tools; main()
    tools.py                  # structured tool defs -> client calls
    client.py                 # clean async wrapper (login, list, feed, status)
    config.py                 # loads pets.toml + env credentials
    vendored/                 # PetLibro cloud API client, ported from cd1zz HA integration
      __init__.py
      api.py                  # login, session, device list, manual feed, device attrs
      const.py
      exceptions.py
  tests/
    test_config.py            # pets.toml parsing, name resolution, cups->portions
    test_tools.py             # tool logic with a mocked client
```

### Layers and responsibilities

- **`vendored/api.py`** — the reverse-engineered PetLibro cloud client, ported
  from the `cd1zz/petlibro-homeassistant` integration. Owns auth (email/password
  login -> token), device discovery, the manual-feed endpoint, and device
  attribute reads. Treated as an external dependency we happen to carry in-tree;
  minimal edits, only what's needed to run standalone (strip Home Assistant
  imports). Also owns the manual lid-open command if the API exposes one.
- **`client.py`** — our clean async facade over `vendored/api.py`. Exposes only
  what the tools need: `login()`, `list_devices()`, `feed(serial, portions)`,
  `open_lid(serial)`, `feeder_status(serial)`, `fountain_status(serial)`.
  Normalizes the vendored
  client's raw dicts into small typed result objects.
- **`config.py`** — loads `pets.toml` (the map below) and reads credentials from
  env. Provides `resolve(names) -> [FeederConfig]` and `cups_to_portions(feeder,
  cups) -> int`.
- **`tools.py`** — the four MCP tools. Pure orchestration: resolve names, call
  client, shape results. No API knowledge.
- **`server.py`** — registers tools with the MCP SDK and runs stdio transport.

## Configuration

### `pets.toml` (checked into repo — contains no secrets)

```toml
[defaults]
portions_per_cup = 12      # global default until per-feeder calibration
region = "US"              # PetLibro cloud region
max_cups_per_command = 4   # overfeed guard; exceed requires force=true

[[pet]]
name = "zeus"
serial = "EXAMPLE_FEEDER_SN_ZEUS"
mac = "EXAMPLE_MAC_ZEUS"
chip = "100001"
# portions_per_cup = 12    # optional per-feeder override once calibrated

[[pet]]
name = "saffron"
serial = "EXAMPLE_FEEDER_SN_SAFFRON"
mac = "EXAMPLE_MAC_SAFFRON"
chip = "100002"

[[pet]]
name = "ferris"
serial = "EXAMPLE_FEEDER_SN_FERRIS"
mac = "EXAMPLE_MAC_FERRIS"
chip = "100003"

[[pet]]
name = "ricco"
serial = "EXAMPLE_FEEDER_SN_RICCO"
mac = "EXAMPLE_MAC_RICCO"
chip = "100004"

[[pet]]
name = "bridget"
serial = "EXAMPLE_FEEDER_SN_BRIDGET"
mac = "EXAMPLE_MAC_BRIDGET"
chip = "100005"

[[pet]]
name = "colby"
serial = "EXAMPLE_FEEDER_SN_COLBY"
mac = "EXAMPLE_MAC_COLBY"
chip = "100006"

[[fountain]]
name = "dockstream1"
serial = "EXAMPLE_FOUNTAIN_SN_1"
mac = "EXAMPLE_MAC_FOUNTAIN_1"
near = ["zeus", "colby", "bridget"]

[[fountain]]
name = "dockstream2"
serial = "EXAMPLE_FOUNTAIN_SN_2"
mac = "EXAMPLE_MAC_FOUNTAIN_2"
near = ["saffron", "ricco", "ferris", "colby"]
```

### Credentials (env vars — never in git)

- `PETLIBRO_EMAIL`
- `PETLIBRO_PASSWORD`
- Optional `PETLIBRO_REGION` overrides `pets.toml`.

## Tool surface (V1)

### `feed(pets, cups, force=false)`
- `pets`: list of pet names, or the string `"all"`.
- `cups`: number of cups (float ok; converted to nearest integer portions
  per feeder).
- **Overfeed guard:** if any resolved feed exceeds `max_cups_per_command`,
  refuse the whole call and return an error naming the offenders, unless
  `force=true`.
- Resolves each name -> serial, computes portions via
  `cups_to_portions(feeder, cups)`, calls `client.feed(serial, portions)`.
- Returns a per-pet result list: `{pet, cups, portions, ok, error?}`. Partial
  success is reported, not hidden — one feeder failing does not abort the rest,
  and every failure surfaces loudly (no silent fallback).

### `list_devices()`
- Returns all feeders and fountains with live status: online/offline, and the
  key attributes below. Used for "what have I got / is everything up".

### `feeder_status(pet=None)`
- One pet (by name) or all. Returns hopper food level, battery %, desiccant
  state, today's dispensed portions, next scheduled feed (read-only).

### `fountain_status(name=None)`
- One fountain (by name) or both. Returns water level, pump on/off, filter life
  remaining.

### `open_lid(pets)`
- `pets`: list of pet names, or `"all"`.
- Forces the RFID lid open on the named feeder(s) regardless of chip — lets any
  pet eat from that bowl, or opens it for cleaning/inspection.
- Resolves names -> serials, calls `client.open_lid(serial)`.
- Returns a per-pet result list `{pet, ok, error?}`, same partial-success
  reporting as `feed`.
- **Depends on the cloud API exposing a manual lid command for these feeders.**
  Confirmed during build by reading the vendored client; if unsupported for this
  feeder model, the tool is dropped from V1 and the limitation is documented
  loudly (not silently stubbed).

## Data flow: "feed ferris 3 cups"

1. Claude parses the sentence, calls `feed(pets=["ferris"], cups=3)`.
2. `tools.feed` -> `config.resolve(["ferris"])` -> Ferris's `FeederConfig`.
3. `config.cups_to_portions(ferris, 3)` -> `round(3 * 12)` = 36 portions
   (uses per-feeder override if present, else default).
4. Overfeed guard: 3 <= 4, allowed.
5. `client.feed("EXAMPLE_FEEDER_SN_FERRIS", 36)`.
6. `client` calls vendored `api.manual_feed(serial, portions)` over the
   authenticated cloud session.
7. Result shaped to `[{pet: "ferris", cups: 3, portions: 36, ok: true}]` and
   returned to Claude, which reports it in plain language.

## Error handling

- **Auth failure:** surfaced as a clear tool error ("PetLibro login failed —
  check PETLIBRO_EMAIL/PASSWORD"). No silent retry loop that masks bad creds.
- **Unknown pet name:** `resolve` raises with the list of valid names; tool
  returns that as an error rather than silently skipping.
- **Device offline / API 4xx-5xx:** reported per-device in the result; the real
  status code/message is included, never swallowed into a generic "ok".
- **Partial failure on multi-feed:** each pet's result carries its own `ok`
  and `error`; the tool never reports overall success when any feeder failed.

## Testing

- `test_config.py`: `pets.toml` parses; name resolution (case-insensitive,
  "all" expansion); `cups_to_portions` rounding and per-feeder overrides;
  overfeed guard math.
- `test_tools.py`: `feed` with a mocked client — single pet, multi pet, "all",
  overfeed refusal, `force=true` bypass, partial failure reporting, unknown name.
  `open_lid` — single pet, "all", partial failure, unknown name.
- Live API calls are NOT exercised in unit tests (mocked). A manual smoke script
  (`scripts/smoke.py`, git-ignored creds) verifies login + `list_devices`
  against the real account once, by hand.

## Open items to calibrate after build

- **`portions_per_cup`:** default 12 is a guess. Owner dispenses a known portion
  count into a measuring cup once, computes the real ratio, sets per-feeder
  overrides if the food differs by station.
- **Vendored client API shape:** exact method/attribute names come from reading
  the current `cd1zz/petlibro-homeassistant` source during the build; the facade
  in `client.py` insulates the tools from that.

## Future (explicitly deferred, not stub tickets)

- Schedule editing (create/modify feeding schedules).
- Fountain control (pump on/off) if the API supports it.
- RFID event history ("who ate and when") — the feeders report chip reads;
  could become a `recent_visits(pet?)` tool.
