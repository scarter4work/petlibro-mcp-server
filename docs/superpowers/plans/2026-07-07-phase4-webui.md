# Phase 4: Web UI on scott-server — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A browser dashboard for rhythm feeding, deployed and running on scott-server (192.168.68.53:8080): pick a cat, see its eating trend + schedule, adjust amounts, and save the schedule to the feeder.

**Architecture:** FastAPI backend reusing `petlibro_mcp` in-process (JSON API + serves a self-contained SPA); vanilla-JS/SVG frontend (no build step, no CDN); systemd+uvicorn service on the PVE host.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, existing `petlibro_mcp`; pytest (+ FastAPI TestClient via `httpx`). Frontend: HTML/CSS/vanilla JS/inline SVG.

## Global Constraints

- Python `>=3.10`; backend stdlib + FastAPI/uvicorn only; reuse `petlibro_mcp` (do not duplicate its logic).
- Work in device **portions**; never cups.
- **Writes stay explicit:** the API dry-runs unless `apply:true`; all writes go through the existing `_apply_with_rollback` (snapshot/verify/rollback) — the web layer never writes to feeders directly.
- Total guard holds (refuse target total > current).
- Errors surface as JSON `{ok:false,error}`; never swallow.
- Frontend is one self-contained file (no external network fetches / CDNs).
- Do not modify `vendored/`.
- Tests: pytest; FastAPI `TestClient` with a fake/mocked client — no live cloud calls in unit tests.

---

### Task 1: Refactor apply into `_apply_one` + add `apply_target_rows`

**Files:**
- Modify: `src/petlibro_mcp/tools.py`
- Test: `tests/test_tools.py` (append)

**Interfaces:**
- Produces:
  - `async def _apply_one(client, serial, enabled, total, target_rows, apply) -> dict` — the per-feeder core (total guard → diff → dry-run preview OR apply+verify+rollback). Returns the result dict WITHOUT `pet`/`serial`.
  - `async def apply_target_rows(config, client, pet, target_rows, apply=False) -> list[dict]` — applies an EXPLICIT (user-edited) target; fetches current enabled rows via `feeding_plans` (no rhythm compute).
  - `apply_schedule` refactored to delegate to `_apply_one` (behavior unchanged).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools.py  (append)
async def test_apply_target_rows_dry_run_previews():
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)], _work_record_days())
    res = await tools.apply_target_rows(cfg(), client, "ferris", [("09:00", 4)])  # apply defaults False
    r = res[0]
    assert r["ok"] is True and r["dry_run"] is True
    assert r["target_schedule"] == [{"time": "09:00", "portions": 4}]
    # total 4 <= current 5 -> allowed; nothing written
    assert {(p["executionTime"], p["grainNum"]) for p in await client.feeding_plans("x")} == {("08:00", 3), ("20:00", 2)}


async def test_apply_target_rows_applies_and_verifies():
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)], _work_record_days())
    res = await tools.apply_target_rows(cfg(), client, "ferris", [("09:00", 4)], apply=True)
    r = res[0]
    assert r["ok"] is True and r.get("applied") is True
    got = sorted((p["executionTime"], p["grainNum"]) for p in await client.feeding_plans("x") if p.get("enable", True))
    assert got == [("09:00", 4)]


async def test_apply_target_rows_total_guard():
    client = _FakePlanClient([_plan(1, "08:00", 5)], _work_record_days())
    res = await tools.apply_target_rows(cfg(), client, "ferris", [("08:00", 9)], apply=True)
    assert res[0]["ok"] is False and "Refusing" in res[0]["error"]


async def test_apply_target_rows_unknown_pet():
    client = _FakePlanClient([], _work_record_days())
    res = await tools.apply_target_rows(cfg(), client, "mittens", [("08:00", 1)])
    assert res[0]["ok"] is False and "mittens" in res[0]["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tools.py -k apply_target_rows -v`
Expected: FAIL — `AttributeError: module 'petlibro_mcp.tools' has no attribute 'apply_target_rows'`

- [ ] **Step 3: Refactor + implement**

In `src/petlibro_mcp/tools.py`, add `_apply_one`, add `apply_target_rows`, and rewrite `apply_schedule`'s per-feeder body to use `_apply_one`:

```python
async def _apply_one(client, serial: str, enabled: list[dict], total: int,
                     target_rows, apply: bool) -> dict:
    """Per-feeder apply core: total guard -> diff -> dry-run preview OR
    apply+verify+rollback. Returns a result dict without pet/serial."""
    target_total = sum(g for _, g in target_rows)
    if target_total > total:
        return {"ok": False,
                "error": (f"Refusing: target total {target_total} exceeds "
                          f"current {total} portions.")}
    actions = diff_schedule(enabled, target_rows)
    if not apply:
        return {"ok": True, "dry_run": True,
                "would_update": actions["updates"], "would_add": actions["adds"],
                "would_remove": actions["removes"],
                "target_schedule": [{"time": t, "portions": g} for t, g in target_rows]}
    snapshot = [dict(p) for p in enabled]
    return await _apply_with_rollback(client, serial, actions, snapshot, target_rows)


async def apply_target_rows(config: Config, client: PetLibroClient, pet,
                            target_rows, apply: bool = False) -> list[dict]:
    """Apply an explicit (e.g. user-edited) target schedule to a feeder."""
    try:
        feeders = config.resolve_feeders(pet)
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]
    results = []
    for f in feeders:
        try:
            enabled = [p for p in await client.feeding_plans(f.serial) if p.get("enable", True)]
            total = sum(int(p.get("grainNum") or 0) for p in enabled)
            res = await _apply_one(client, f.serial, enabled, total, list(target_rows), apply)
            results.append({"pet": f.name, "serial": f.serial, **res})
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results
```

Then replace `apply_schedule`'s per-feeder try body (the part after computing `rc`) with a delegation to `_apply_one`:

```python
        try:
            rc = await _compute_rhythm(client, f, days)
            res = await _apply_one(client, f.serial, rc["enabled"], rc["total"],
                                   rc["target_rows"], apply)
            results.append({"pet": f.name, "serial": f.serial, **res})
        except Exception as exc:
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -W error::DeprecationWarning -q`
Expected: PASS (existing apply_schedule tests unchanged + 4 new). Pristine.

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/tools.py tests/test_tools.py
git commit -m "feat: apply_target_rows + shared _apply_one core"
```

---

### Task 2: `webui/rhythm_api.py` — analysis payload (+curve) and apply wrapper

**Files:**
- Create: `webui/__init__.py`, `webui/rhythm_api.py`
- Test: `tests/test_rhythm_api.py`

**Interfaces:**
- Consumes: `history.parse_work_record`, `rhythm.circadian_curve`, `tools.analyze_rhythm`, `tools.apply_target_rows`, `client`.
- Produces:
  - `async def analysis(config, client, pet, days=60) -> dict` — `{pet, ok, eating_visits, daily_total_portions, current_schedule, recommended_schedule, rhythm_curve, error?}`. `rhythm_curve` is the 48-bin curve from the same history (for the trend chart).
  - `async def apply(config, client, pet, schedule, apply_flag=False) -> dict` — converts `schedule` (`[{time,portions}]`) to `target_rows` and calls `tools.apply_target_rows`; returns the single result dict.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rhythm_api.py
from unittest.mock import AsyncMock
from petlibro_mcp.config import Config, Feeder
from webui import rhythm_api


def _cfg():
    return Config(feeders=[Feeder("ferris", "SN-F", "", "", 12)], fountains=[],
                  region="US", max_cups_per_command=4, email="a@b.com", password="pw")


def _client():
    m = AsyncMock()
    days = [{"workRecords": [
        {"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
         "formatRecordTime": "2026-07-07 08:00", "params": '{"seconds":"02m00s"}'}
        for _ in range(6)
    ]}]
    m.work_record = AsyncMock(return_value=days)
    m.feeding_plans = AsyncMock(return_value=[
        {"executionTime": "08:00", "grainNum": 3, "enable": True},
        {"executionTime": "20:00", "grainNum": 2, "enable": True}])
    return m


async def test_analysis_includes_curve_and_schedules():
    out = await rhythm_api.analysis(_cfg(), _client(), "ferris")
    assert out["ok"] is True and out["pet"] == "ferris"
    assert out["daily_total_portions"] == 5
    assert len(out["rhythm_curve"]) == 48
    assert sum(x["portions"] for x in out["recommended_schedule"]) == 5


async def test_apply_delegates_to_target_rows():
    client = _client()
    out = await rhythm_api.apply(_cfg(), client, "ferris",
                                 [{"time": "09:00", "portions": 4}], apply_flag=False)
    assert out["ok"] is True and out["dry_run"] is True
    assert out["target_schedule"] == [{"time": "09:00", "portions": 4}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_rhythm_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webui'`

- [ ] **Step 3: Write the implementation**

```python
# webui/__init__.py
```
(empty file)

```python
# webui/rhythm_api.py
"""Thin web-facing composition over petlibro_mcp tools."""
from __future__ import annotations
from petlibro_mcp.history import parse_work_record
from petlibro_mcp.rhythm import circadian_curve
from petlibro_mcp import tools


async def analysis(config, client, pet: str, days: int = 60) -> dict:
    base = (await tools.analyze_rhythm(config, client, pet, days))[0]
    if not base.get("ok"):
        return base
    raw = await client.work_record(base["serial"], days=days)
    eats, _ = parse_work_record(raw)
    curve = circadian_curve([(m, max(d, 1)) for m, d in eats])
    return {**base, "rhythm_curve": curve}


async def apply(config, client, pet: str, schedule, apply_flag: bool = False) -> dict:
    target_rows = [(row["time"], int(row["portions"])) for row in schedule]
    res = await tools.apply_target_rows(config, client, pet, target_rows, apply_flag)
    return res[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_rhythm_api.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add webui/__init__.py webui/rhythm_api.py tests/test_rhythm_api.py
git commit -m "feat: webui rhythm_api (analysis+curve, apply wrapper)"
```

---

### Task 3: `webui/app.py` — FastAPI routes + static serving

**Files:**
- Create: `webui/app.py`
- Modify: `pyproject.toml` (add `web` optional-deps: fastapi, uvicorn, httpx for tests)
- Test: `tests/test_webapp.py`

**Interfaces:**
- Produces a FastAPI `app` with:
  - `GET /api/pets` → `[{name, serial}]`
  - `GET /api/pets/{name}/analysis?days=60` → `rhythm_api.analysis(...)`
  - `POST /api/pets/{name}/apply` body `{schedule, apply}` → `rhythm_api.apply(...)`
  - `GET /` → serves `webui/static/index.html`
  - App holds a module-level `Config` + `PetLibroClient` (built at startup via `petlibro_mcp.server._load_env_file` + `load_config`). For tests, a factory `build_app(config, client)` allows injection.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_webapp.py
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from petlibro_mcp.config import Config, Feeder
from webui.app import build_app


def _cfg():
    return Config(feeders=[Feeder("ferris", "SN-F", "", "", 12)], fountains=[],
                  region="US", max_cups_per_command=4, email="a@b.com", password="pw")


def _client():
    m = AsyncMock()
    m.work_record = AsyncMock(return_value=[{"workRecords": [
        {"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
         "formatRecordTime": "2026-07-07 08:00", "params": '{"seconds":"02m00s"}'}]}])
    m.feeding_plans = AsyncMock(return_value=[
        {"executionTime": "08:00", "grainNum": 3, "enable": True}])
    return m


def test_pets_endpoint():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.get("/api/pets")
    assert r.status_code == 200
    assert r.json() == [{"name": "ferris", "serial": "SN-F"}]


def test_analysis_endpoint():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.get("/api/pets/ferris/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and len(body["rhythm_curve"]) == 48


def test_apply_dry_run_endpoint():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.post("/api/pets/ferris/apply",
               json={"schedule": [{"time": "08:00", "portions": 3}], "apply": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["dry_run"] is True


def test_index_served():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
```

- [ ] **Step 2: Install web deps + run tests to verify they fail**

Run: `.venv/bin/pip install -q fastapi uvicorn httpx && .venv/bin/pytest tests/test_webapp.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webui.app'`

- [ ] **Step 3: Add deps to pyproject.toml**

Add to `[project.optional-dependencies]` — `httpx` is TEST-only (Starlette
TestClient); production serves via uvicorn, so `web` stays minimal. Do NOT add
`httpx2` (Starlette's deprecation notice suggests it, but it's an unvetted
package — keep trusted `httpx` for tests and filter the benign warning instead):

```toml
dev = ["pytest>=8", "pytest-asyncio>=0.23", "httpx>=0.27"]
web = ["fastapi>=0.110", "uvicorn>=0.29"]
```

And add a pytest filter for Starlette's test-client deprecation notice so
`pytest` stays pristine without installing httpx2:

```toml
[tool.pytest.ini_options]
filterwarnings = [
    "error::DeprecationWarning",
    "ignore:Using `httpx` with `starlette.testclient` is deprecated",
]
```
(Pristine check going forward: plain `.venv/bin/pytest -q` — the ini filters
enforce error-on-DeprecationWarning except the one benign Starlette notice.)

- [ ] **Step 4: Write the implementation**

```python
# webui/app.py
"""FastAPI app: rhythm-feeding dashboard over petlibro_mcp."""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp.server import _load_env_file, PETS_TOML
from webui import rhythm_api

_STATIC = Path(__file__).resolve().parent / "static"


def build_app(config, client) -> FastAPI:
    app = FastAPI(title="PetLibro Rhythm Feeding")

    @app.get("/api/pets")
    async def pets():
        return [{"name": f.name, "serial": f.serial} for f in config.feeders]

    @app.get("/api/pets/{name}/analysis")
    async def analysis(name: str, days: int = 60):
        return await rhythm_api.analysis(config, client, name, days)

    @app.post("/api/pets/{name}/apply")
    async def apply(name: str, body: dict):
        return await rhythm_api.apply(config, client, name,
                                      body.get("schedule", []), bool(body.get("apply", False)))

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "index.html")

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    return app


def create_app() -> FastAPI:
    _load_env_file()
    config = load_config(PETS_TOML)
    return build_app(config, PetLibroClient(config))
```

(Task 4 creates `webui/static/index.html`; `_STATIC.is_dir()` guards let tests run before it exists — but Task 4 lands before deploy. For `test_index_served`, create a minimal `webui/static/index.html` placeholder in THIS task so the route works, to be replaced by Task 4.)

Create a placeholder `webui/static/index.html`:

```html
<!doctype html><html><head><meta charset="utf-8"><title>PetLibro Rhythm</title></head>
<body><div id="app">loading…</div></body></html>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_webapp.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add webui/app.py webui/static/index.html pyproject.toml tests/test_webapp.py
git commit -m "feat: FastAPI web app (pets/analysis/apply + static)"
```

---

### Task 4: Frontend SPA (`webui/static/index.html`)

**Files:**
- Replace: `webui/static/index.html` (self-contained SPA)

**REQUIRED SUB-SKILL:** load `frontend-design` for aesthetic direction before building.

This is a build+visual-verify task (not unit TDD). Requirements:
- Self-contained: all HTML/CSS/JS/SVG inline; NO external network/CDN.
- **Left sidebar:** app title, list of pets from `GET /api/pets` (click selects; selected highlighted).
- **On select:** `GET /api/pets/{name}/analysis` and render:
  1. **Trend chart** — an SVG area/bar chart of `rhythm_curve` (48 bins over 24h), x-axis hour labels; headline stats (eating visits, daily total portions).
  2. **Schedule editor** — one row per `recommended_schedule` entry: time label + a number stepper/slider for portions; a live "daily total" readout; the `current_schedule` shown for comparison. A guard/notice if the edited total exceeds the current total (mirrors the backend total guard).
  3. **Actions** — "Reanalyze" (re-GET analysis), "Preview" (`POST apply {apply:false}` → show would-update/add/remove diff), "Save to feeder" (confirm dialog → `POST apply {apply:true}` → show result banner: applied / refused / rolled-back).
- Handle `ok:false` responses by showing the `error` in a banner (surface, never hide).
- Responsive, works in a desktop browser on the LAN.

- [ ] **Step 1:** Load `frontend-design`; build `webui/static/index.html` to the requirements above.
- [ ] **Step 2:** Verify locally: run `.venv/bin/uvicorn webui.app:create_app --factory --port 8099` (background), load `http://127.0.0.1:8099/` in Playwright/curl, confirm the pets list renders and selecting a pet loads the analysis (against the live account, read-only). Screenshot for the controller to eyeball.
- [ ] **Step 3:** Commit.

```bash
git add webui/static/index.html
git commit -m "feat: rhythm feeding dashboard SPA"
```

---

### Task 5: Deploy to scott-server (controller-run)

**Files:**
- Create: `deploy/petlibro-webui.service`, `deploy/deploy.sh`

Controller-run infrastructure task (not a subagent; involves SSH + secrets to 192.168.68.53).

- [ ] **Step 1: Create the systemd unit** `deploy/petlibro-webui.service`:

```ini
[Unit]
Description=PetLibro Rhythm Feeding Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/petlibro-webui
Environment=PETLIBRO_PETS_TOML=/opt/petlibro-webui/pets.toml
Environment=PETLIBRO_ENV=/opt/petlibro-webui/.env
ExecStart=/opt/petlibro-webui/.venv/bin/uvicorn webui.app:create_app --factory --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create `deploy/deploy.sh`** (rsync repo minus junk, build venv, install, restart service):

```bash
#!/usr/bin/env bash
set -euo pipefail
HOST=root@192.168.68.53
DEST=/opt/petlibro-webui
rsync -az --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude '.superpowers' \
  ./ "$HOST:$DEST/"
scp .env "$HOST:$DEST/.env"
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/petlibro-webui
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e '.[web]'
chmod 600 .env
cp deploy/petlibro-webui.service /etc/systemd/system/petlibro-webui.service
systemctl daemon-reload
systemctl enable --now petlibro-webui
sleep 2
systemctl is-active petlibro-webui
curl -fsS http://127.0.0.1:8080/api/pets | head -c 400
REMOTE
```

- [ ] **Step 3: Run the deploy** (controller): `bash deploy/deploy.sh`. Confirm `systemctl is-active` → `active` and the `/api/pets` JSON lists the cats.
- [ ] **Step 4: Live smoke** (controller): from this box, `curl -fsS http://192.168.68.53:8080/api/pets` returns the pets; `curl -fsS http://192.168.68.53:8080/api/pets/ferris/analysis` returns ok with a 48-bin curve (read-only — no writes). Screenshot the loaded UI.
- [ ] **Step 5: Commit deploy assets.**

```bash
git add deploy/petlibro-webui.service deploy/deploy.sh
git commit -m "chore: scott-server deploy (systemd unit + deploy script)"
```

---

## Definition of done
- Service `active` on scott-server; `http://192.168.68.53:8080/` serves the dashboard.
- Pets list loads; selecting a cat shows its trend + current/recommended schedule.
- Preview (dry-run) shows the diff; "Save to feeder" applies via the safe path.
- Full test suite green (backend), no live writes during deploy/smoke.
