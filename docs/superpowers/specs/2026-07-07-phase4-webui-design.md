# Phase 4: Web UI on scott-server — Design

**Date:** 2026-07-07
**Status:** Design (autonomous — user directive: "keep working till the UI is ready on scott-server")
**Author:** scarter4work (with Claude)

## Purpose

A web dashboard for the rhythm-feeding system, hosted on scott-server, so the
per-cat rhythm analysis and schedule writeback (Phases 1–2) are usable from a
browser without going through Claude. Realizes the UI the user sketched: left
sidebar of pets; right panel with each cat's eating trend, current schedule, a
rhythm recommendation with adjustable amounts, a "reanalyze from recent history"
action, and a "save to feeder" action that pushes to the PetLibro cloud.

Builds directly on the `petlibro_mcp` library (`analyze_rhythm`, `apply_schedule`,
`config`, `client`). Phase 3 (auto-tuning amounts) is deferred and can later
surface as an in-UI button; not required for this UI.

## Decisions (made autonomously per the directive)

- **Backend:** FastAPI reusing `petlibro_mcp` in-process (no MCP layer). Serves a
  small JSON API plus the static frontend. Async, matches the existing async client.
- **Frontend:** one self-contained `index.html` — vanilla JS + CSS + inline SVG
  charts. No build step, no CDN (works offline, no CSP issues, trivial to deploy).
- **Hosting:** systemd service running `uvicorn` on `0.0.0.0:8080` on the PVE host
  (192.168.68.53). LAN-only, no auth (home network) — documented as a caveat.
- **Writes stay explicit:** the UI previews (dry-run) by default; the user must
  click "Save to feeder" to apply. `apply_schedule`'s snapshot/verify/rollback
  guarantees still hold — the UI is a thin caller.

## API

- `GET /api/pets` → `[{name, serial}]` from config.
- `GET /api/pets/{name}/analysis?days=60` → `{pet, ok, eating_visits, daily_total_portions,
  current_schedule:[{time,portions}], recommended_schedule:[{time,portions}],
  rhythm_curve:[48 floats], error?}`. Composes `analyze_rhythm` plus the 48-bin
  circadian curve (for the trend chart) computed from the same history.
- `POST /api/pets/{name}/apply` body `{schedule:[{time,portions}], apply:bool}` →
  runs `apply_schedule`-style diff/verify/rollback for the provided (possibly
  slider-edited) target. Dry-run unless `apply:true`. Returns the tool result dict.
  - Note: `apply_schedule` currently computes the target internally from rhythm.
    Phase 4 needs to apply a *user-adjusted* target, so the web layer builds the
    diff from the posted schedule via `schedule_diff` + the same
    `_apply_with_rollback` path, OR a small `apply_target` helper is added to
    `tools.py` that takes an explicit `target_rows`. The plan will add
    `apply_target_rows` (reuses `_apply_with_rollback`) so the UI can apply edited
    amounts. The total guard applies (refuse target total > current).

## Frontend layout

- **Left sidebar:** app title + list of pets (click to select). Selected pet
  highlighted.
- **Right panel (per selected pet):**
  1. **Trend chart** — 24h eating-rhythm curve (SVG area/bars over the 48 bins),
     plus headline stats (eating visits, daily total).
  2. **Schedule table** — rows of the recommended schedule; each row shows time +
     a portions stepper/slider the user can adjust; a running daily-total readout.
     Current schedule shown alongside for comparison.
  3. **Actions** — "Reanalyze" (refetch analysis), "Preview" (dry-run apply → show
     the diff), "Save to feeder" (apply=true, with a confirm). Result/status banner.

## Component architecture

| Path | Responsibility |
|------|---------------|
| `webui/app.py` | FastAPI app: routes, composes petlibro_mcp tools, serves static |
| `webui/rhythm_api.py` | thin functions: analysis payload (+curve), apply-target |
| `webui/static/index.html` | self-contained SPA (HTML/CSS/JS/SVG) |
| `src/petlibro_mcp/tools.py` | add `apply_target_rows(...)` reusing `_apply_with_rollback` |
| `pyproject.toml` | add `web` optional-deps: fastapi, uvicorn |
| `deploy/petlibro-webui.service` | systemd unit |
| `deploy/deploy.sh` | rsync repo to scott-server, venv, install, enable service |

## Testing

- `tools.apply_target_rows`: unit-tested with the stateful fake (dry-run preview,
  apply+verify, rollback on mismatch, total guard) — mirrors `apply_schedule` tests.
- `webui/rhythm_api.py`: unit-tested with a fake client (analysis payload shape incl.
  48-bin curve; apply-target delegates correctly).
- `webui/app.py`: FastAPI `TestClient` — `/api/pets`, `/api/pets/{name}/analysis`
  (mocked client), `/api/pets/{name}/apply` dry-run; index served.
- Deployment: smoke-check the live service responds on 192.168.68.53:8080 and the
  pets list loads (read-only; no schedule writes during deploy).

## Non-goals (Phase 4)

- No auth / TLS (LAN-only home use) — documented caveat.
- No Phase-3 auto-tuning (deferred).
- No live camera/video.
- No multi-user state; single shared account view.

## Risks & mitigations

- **Applying user-edited amounts** goes through the same snapshot/verify/rollback
  path as `apply_schedule` — safety preserved; total guard prevents over-feeding.
- **Credentials on scott-server** (.env) — file perms 600, LAN-only service.
- **Deploy on PVE host** — a self-contained venv under `/opt/petlibro-webui`,
  systemd-managed; no PVE system packages touched.
