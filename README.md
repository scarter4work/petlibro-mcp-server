# PetLibro MCP Server

An MCP (Model Context Protocol) server that lets Claude control PetLibro RFID
pet feeders and Dockstream water fountains: check food/battery/water status,
dispense food by the cup, and force-open feeder lids — all backed by the
PetLibro cloud API (the same one the PetLibro mobile app uses).

Feeders and fountains are addressed by short names you define once in
`pets.toml` (e.g. `zeus`, `ferris`, `dockstream1`), not by raw device serials.

## Install

Requires Python >= 3.10.

```bash
cd petlibro
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

This installs the `petlibro-mcp` console script into `.venv/bin/`.

Run the test suite:

```bash
.venv/bin/pytest
```

## Configuration

### `.env` — PetLibro cloud credentials

Create a `.env` file at the repo root (already git-ignored — never commit
real credentials):

```
PETLIBRO_EMAIL=you@example.com
PETLIBRO_PASSWORD=your-petlibro-app-password
PETLIBRO_REGION=US
```

These are the same credentials you use to log into the PetLibro mobile app.
`PETLIBRO_REGION` selects the regional API base URL (see
`src/petlibro_mcp/vendored/api.py` for supported regions; `US` is the
default).

### `pets.toml` — your devices

`pets.toml` at the repo root maps your feeders and fountains to friendly
names:

```toml
[defaults]
portions_per_cup = 12       # default dispense ratio, overridable per feeder
region = "US"
max_cups_per_command = 4    # safety cap; "feed" refuses more unless force=true

[[pet]]
name = "ferris"
serial = "EXAMPLE_FEEDER_SN_FERRIS"   # from the device or the cloud device list
mac = "EXAMPLE_MAC_FERRIS"
chip = "100003"
# portions_per_cup = 10             # optional per-feeder override

[[fountain]]
name = "dockstream1"
serial = "EXAMPLE_FOUNTAIN_SN_1"
mac = "EXAMPLE_MAC_FOUNTAIN_1"
near = ["zeus", "colby", "bridget"]  # cosmetic: which pets drink from it
```

Serials are the values the PetLibro cloud reports for each device (`deviceSn`
in the API), not necessarily the exact string printed on the physical label.
Run the smoke script below after any config change to confirm every
configured serial actually matches something the cloud reports — a mismatch
means `feed`/`open_lid` calls for that device will fail.

### Verifying the connection (smoke test)

`scripts/smoke.py` is a small, read-only script (git-ignored, not part of the
package) that logs in and lists devices to confirm the whole stack — `.env`,
`pets.toml`, and the vendored API client — actually talks to your real
account:

```bash
.venv/bin/python scripts/smoke.py
```

It prints `login OK`, the number of devices the cloud reports, each cloud
device's serial/name, and then reconciles every configured feeder/fountain
serial against those cloud serials (`OK` or `MISSING from cloud`). This does
**not** feed anything or open any lid — it only calls login + list devices.

### Calibrating `portions_per_cup`

The feeders dispense in discrete "portions," but you usually think in cups.
`portions_per_cup` converts one to the other (`cups_to_portions` in
`config.py`), and the default of `12` is a guess — calibrate it for your
actual food:

1. Use the `feed` tool (or the PetLibro app) to dispense a known number of
   portions into an empty measuring cup (e.g. 12 portions).
2. Measure how many cups that actually filled.
3. Compute `portions_per_cup = portions_dispensed / cups_measured`.
4. Set it in `pets.toml`, either globally under `[defaults]` or per feeder
   (food density/kibble size can differ by station, so add a
   `portions_per_cup = N` line under any `[[pet]]` that needs its own ratio).

## Tools

The server exposes five MCP tools:

| Tool | Purpose |
|---|---|
| `feed` | Dispense food. Takes `pets` (name, list of names, or `"all"`) and `cups`. Refuses to exceed `max_cups_per_command` unless `force=true`. |
| `open_lid` | Force the RFID lid open on one, several, or all feeders. |
| `feeder_status` | Food level, battery, online state, and today's feeding total for one feeder or all of them. |
| `fountain_status` | Raw status payload for one fountain or all fountains. |
| `list_devices` | Configured feeders/fountains plus the live cloud device list — useful for spotting a serial mismatch. |

Example prompts once the server is registered with Claude:

- "Feed ferris 3 cups"
- "Open all the lids"
- "How much food does zeus have?"
- "Feed everyone 1 cup"
- "What's the status of dockstream2?"
- "List all my PetLibro devices"

## Registering with Claude

Add an entry to your MCP config (Claude Code's `.mcp.json`, or the Claude
Desktop config). Credentials should come from `.env`, not be hardcoded in a
config file that might get shared or committed. If your MCP host does not
support loading a `.env` file directly, point `command` at a small wrapper
that sources it before exec'ing the server, e.g.:

```json
{
  "mcpServers": {
    "petlibro": {
      "command": "bash",
      "args": [
        "-c",
        "set -a && source /home/scarter4work/projects/petlibro/.env && set +a && exec /home/scarter4work/projects/petlibro/.venv/bin/petlibro-mcp"
      ],
      "env": {
        "PETLIBRO_PETS_TOML": "/home/scarter4work/projects/petlibro/pets.toml"
      }
    }
  }
}
```

If you'd rather set credentials directly in the MCP config's `env` block
instead of sourcing `.env`, never write the real password into a file that
gets committed — use a placeholder and fill in the real value only in your
local, git-ignored config:

```json
{
  "mcpServers": {
    "petlibro": {
      "command": "/home/scarter4work/projects/petlibro/.venv/bin/petlibro-mcp",
      "env": {
        "PETLIBRO_EMAIL": "you@example.com",
        "PETLIBRO_PASSWORD": "REDACTED",
        "PETLIBRO_REGION": "US",
        "PETLIBRO_PETS_TOML": "/home/scarter4work/projects/petlibro/pets.toml"
      }
    }
  }
}
```

Either way, keep whichever file holds the real password (`.env` or the MCP
config itself) out of git.

## Safety notes

- `feed` enforces `max_cups_per_command` as a per-call overfeed guard;
  `force=true` bypasses it deliberately, not accidentally.
- Every tool call surfaces real errors (auth failures, unknown pet names,
  API errors) instead of swallowing them — a failed dispense or a serial
  mismatch should always be visible, never silently ignored.
- Always run `scripts/smoke.py` after changing `pets.toml` or rotating
  credentials, before trusting `feed`/`open_lid` against real hardware.

## Known gaps (deferred, not stubbed)

- `fountain_status` currently returns the raw cloud payload (`realInfo`)
  rather than named fields (water level, pump state, filter life) — pending
  seeing a real fountain payload to map the exact keys.
- Schedule editing, fountain pump control, and RFID visit history
  (`recent_visits`) are not implemented.
