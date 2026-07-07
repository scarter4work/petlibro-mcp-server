# Rhythm Feeding — Phase 2: Schedule Writeback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `apply_schedule` MCP tool that writes a cat's Phase-1 rhythm schedule back to its feeder via edit-in-place diff, safely (dry-run default, snapshot → verify → rollback) over the community-verified `/device/feedingPlan/update|add|remove` endpoints.

**Architecture:** Three thin write methods on the `PetLibroClient` facade (vendored client untouched); a pure `schedule_diff` module that turns (current rows, target rows) into update/add/remove actions; an `apply_schedule` tool that reuses Phase-1's rhythm compute (extracted into a shared helper), previews by default, and on `apply=True` executes the diff, reads back, verifies, and rolls back to a snapshot on any mismatch. A one-feeder live spike proves the payload before the tool is trusted.

**Tech Stack:** Python 3.10+, stdlib only for new logic, `mcp>=1.9`, pytest + pytest-asyncio (`asyncio_mode = "auto"`). Builds on Phase 1 modules `history`, `rhythm`, `planner`.

## Global Constraints

- Python `>=3.10`; stdlib only for new logic — no new third-party dependencies.
- Work entirely in device **portions** (`grainNum`); never convert to cups.
- The diff logic (`schedule_diff.py`) is **pure** (no I/O, no wall-clock reads), deterministic, unit-testable.
- **Writes are opt-in:** `apply_schedule` defaults to a dry-run preview; real writes require `apply=True`.
- **Total guard:** never apply a target whose daily total exceeds the current enabled total.
- **Safety:** snapshot current plan → apply → read back → verify → roll back to snapshot on mismatch; if rollback also fails to verify, surface a loud error carrying the snapshot JSON. Never leave a silently half-applied schedule.
- Errors surface as `{"ok": False, "error": "<Type>: <msg>"}` per feeder — never swallowed.
- Do **not** modify `src/petlibro_mcp/vendored/` (vendored external dependency). New write methods live on the `PetLibroClient` facade and call `self._api.session.post` directly (same pattern as Phase 1's `work_record`).
- Write payloads match the community-verified schema exactly (see Task 1).
- Tests use existing patterns: `AsyncMock`/stateful fakes, keyword-constructed `Config`, pristine output (env is Python 3.14).

---

### Task 1: Client facade write methods

**Files:**
- Modify: `src/petlibro_mcp/client.py`
- Test: `tests/test_client.py` (append)

**Interfaces:**
- Consumes: `self._api.session.post(path, json=...)` (exists on the vendored session).
- Produces (all `await self.ensure_login()` first):
  - `PetLibroClient.update_plan(serial: str, plan: dict) -> None` → POST `/device/feedingPlan/update`
  - `PetLibroClient.add_plan(serial: str, plan: dict) -> None` → POST `/device/feedingPlan/add`
  - `PetLibroClient.remove_plan(serial: str, plan_id: int) -> None` → POST `/device/feedingPlan/remove`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_client.py  (append)
async def test_update_plan_posts_community_payload():
    api = AsyncMock()
    api.session.post = AsyncMock(return_value=None)
    client = PetLibroClient(_cfg(), api=api)
    plan = {"id": 42, "executionTime": "08:00", "grainNum": 3,
            "repeatDay": "[1,2,3,4,5,6,7]", "label": "Zeus",
            "enableAudio": True, "audioTimes": 2, "enable": True}
    await client.update_plan("SN-1", plan)
    api.login.assert_awaited()  # ensure_login gate
    api.session.post.assert_awaited_once_with("/device/feedingPlan/update", json={
        "id": 42, "deviceSn": "SN-1", "executionTime": "08:00",
        "repeatDay": "[1,2,3,4,5,6,7]", "label": "Zeus", "enable": True,
        "enableAudio": True, "audioTimes": 2, "grainNum": 3, "petIds": [],
    })


async def test_add_plan_posts_with_id_zero():
    api = AsyncMock()
    api.session.post = AsyncMock(return_value=None)
    client = PetLibroClient(_cfg(), api=api)
    plan = {"executionTime": "20:00", "grainNum": 2,
            "repeatDay": "[1,2,3,4,5,6,7]", "label": "", "enableAudio": False,
            "audioTimes": 2}
    await client.add_plan("SN-1", plan)
    _, kwargs = api.session.post.call_args
    body = kwargs["json"]
    assert body["id"] == 0 and body["deviceSn"] == "SN-1"
    assert body["executionTime"] == "20:00" and body["grainNum"] == 2
    assert body["enable"] is True and body["petIds"] == []


async def test_remove_plan_posts_plan_id():
    api = AsyncMock()
    api.session.post = AsyncMock(return_value=None)
    client = PetLibroClient(_cfg(), api=api)
    await client.remove_plan("SN-1", 42)
    api.session.post.assert_awaited_once_with("/device/feedingPlan/remove", json={
        "deviceSn": "SN-1", "planId": 42,
    })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_client.py -k "update_plan or add_plan or remove_plan" -v`
Expected: FAIL — `AttributeError: 'PetLibroClient' object has no attribute 'update_plan'`

- [ ] **Step 3: Write the implementation**

Append to `src/petlibro_mcp/client.py`:

```python
    async def update_plan(self, serial: str, plan: dict) -> None:
        """Edit an existing feeding-plan row (by id). Community-verified payload."""
        await self.ensure_login()
        await self._api.session.post("/device/feedingPlan/update", json={
            "id": plan["id"],
            "deviceSn": serial,
            "executionTime": plan.get("executionTime"),
            "repeatDay": plan.get("repeatDay", "[]"),
            "label": plan.get("label", ""),
            "enable": plan.get("enable", True),
            "enableAudio": plan.get("enableAudio", False),
            "audioTimes": plan.get("audioTimes", 2),
            "grainNum": plan.get("grainNum"),
            "petIds": [],
        })

    async def add_plan(self, serial: str, plan: dict) -> None:
        """Create a new feeding-plan row (id=0). Community-verified payload."""
        await self.ensure_login()
        await self._api.session.post("/device/feedingPlan/add", json={
            "id": 0,
            "deviceSn": serial,
            "executionTime": plan.get("executionTime"),
            "repeatDay": plan.get("repeatDay", "[]"),
            "label": plan.get("label", ""),
            "enable": True,
            "enableAudio": plan.get("enableAudio", False),
            "audioTimes": plan.get("audioTimes", 2),
            "grainNum": plan.get("grainNum"),
            "petIds": [],
        })

    async def remove_plan(self, serial: str, plan_id: int) -> None:
        """Delete a feeding-plan row by id."""
        await self.ensure_login()
        await self._api.session.post("/device/feedingPlan/remove", json={
            "deviceSn": serial,
            "planId": plan_id,
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_client.py -v`
Expected: PASS (existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/client.py tests/test_client.py
git commit -m "feat: feeding-plan write facade (update/add/remove)"
```

---

### Task 2: Live-spike script (payload proof) — controller-run gate

**Files:**
- Create: `scripts/spike_writeback.py`

**Interfaces:**
- Consumes: `PetLibroClient.feeding_plans`, `update_plan` (Task 1).
- Produces: a runnable, self-restoring script `scripts/spike_writeback.py <pet>` that proves the `update` payload round-trips against the real cloud with net-zero change.

> **Note for the implementer subagent:** create the script only. **Do NOT run it** — it writes to a live feeder. The controller runs it, supervised, choosing the feeder and confirming timing with the human. There are no unit tests for this task; it is a live-verification gate.

- [ ] **Step 1: Write the script**

```python
# scripts/spike_writeback.py
"""Live payload proof: snapshot one feeder's plan, bump one row's grainNum by 1,
verify it took, then restore the snapshot verbatim. Net-zero. Reads .env.

Usage: .venv/bin/python scripts/spike_writeback.py <pet-name>
"""
import asyncio, os, sys, json
from datetime import datetime
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient

for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


async def main(pet: str):
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "pets.toml"))
    feeder = cfg.resolve_feeders([pet])[0]
    client = PetLibroClient(cfg)

    plans = await client.feeding_plans(feeder.serial)
    enabled = [p for p in plans if p.get("enable", True) and p.get("grainNum") is not None]
    if not enabled:
        print(f"No enabled rows on {pet}; aborting."); return
    # pick the enabled row whose executionTime is farthest from 'now' (avoid its window)
    now_min = datetime.now().hour * 60 + datetime.now().minute
    row = max(enabled, key=lambda p: min((_minutes(p["executionTime"]) - now_min) % 1440,
                                         (now_min - _minutes(p["executionTime"])) % 1440))
    original = int(row["grainNum"])
    print(f"[{pet}] target row id={row['id']} time={row['executionTime']} grainNum={original}")
    print("SNAPSHOT:", json.dumps(row))

    # 1. bump +1
    await client.update_plan(feeder.serial, {**row, "grainNum": original + 1})
    back = await client.feeding_plans(feeder.serial)
    got = next((int(p["grainNum"]) for p in back if p.get("id") == row["id"]), None)
    print(f"after bump: grainNum={got}  (expected {original + 1})  -> {'OK' if got == original + 1 else 'FAIL'}")

    # 2. restore
    await client.update_plan(feeder.serial, {**row, "grainNum": original})
    back = await client.feeding_plans(feeder.serial)
    restored = next((int(p["grainNum"]) for p in back if p.get("id") == row["id"]), None)
    print(f"after restore: grainNum={restored}  (expected {original})  -> {'OK' if restored == original else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Commit the script**

```bash
git add scripts/spike_writeback.py
git commit -m "chore: live writeback spike script (snapshot/bump/verify/restore)"
```

- [ ] **Step 3: LIVE SPIKE GATE — controller runs, supervised**

The controller (not the implementer subagent) performs this, after confirming the
feeder and a quiet time window with the human:

Run: `.venv/bin/python scripts/spike_writeback.py <chosen-pet>`
Expected output: `after bump ... OK` then `after restore ... OK`.

If the bump does not take (payload rejected / field wrong), STOP — do not proceed to
the diff/tool tasks; diagnose the payload against the community schema first. The
snapshot line is printed so the row can be restored by hand if anything goes wrong.

---

### Task 3: `schedule_diff.py` — pure diff (current + target → actions)

**Files:**
- Create: `src/petlibro_mcp/schedule_diff.py`
- Test: `tests/test_schedule_diff.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `diff_schedule(current_enabled: list[dict], target_rows: list[tuple[str, int]]) -> dict` returning `{"updates": list[dict], "adds": list[dict], "removes": list[int]}`.
    - Both inputs sorted by time, paired by index.
    - Overlap → an `update` dict carrying the existing row's `id`, `repeatDay`, `label`, `enableAudio`, `audioTimes` unchanged, plus target `executionTime`+`grainNum` and `enable: True`.
    - Extra target rows → `add` dicts inheriting `repeatDay`/`label`/`enableAudio`/`audioTimes` from a template (first sorted current row, or sensible defaults if none).
    - Surplus current rows → `removes` (their `id`s).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_schedule_diff.py
from petlibro_mcp.schedule_diff import diff_schedule


def _cur(id, t, g):
    return {"id": id, "executionTime": t, "grainNum": g,
            "repeatDay": "[1,2,3,4,5,6,7]", "label": "L",
            "enableAudio": True, "audioTimes": 2}


def test_equal_counts_all_updates():
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    target = [("07:30", 4), ("21:00", 1)]
    d = diff_schedule(current, target)
    assert d["removes"] == [] and d["adds"] == []
    assert [(u["id"], u["executionTime"], u["grainNum"]) for u in d["updates"]] == [
        (1, "07:30", 4), (2, "21:00", 1)]
    # preserved fields carried through
    assert d["updates"][0]["repeatDay"] == "[1,2,3,4,5,6,7]"
    assert d["updates"][0]["enableAudio"] is True and d["updates"][0]["enable"] is True


def test_target_longer_updates_plus_adds():
    current = [_cur(1, "08:00", 3)]
    target = [("08:00", 3), ("20:00", 2)]
    d = diff_schedule(current, target)
    assert [u["id"] for u in d["updates"]] == [1]
    assert d["removes"] == []
    assert len(d["adds"]) == 1
    add = d["adds"][0]
    assert add["executionTime"] == "20:00" and add["grainNum"] == 2
    assert add["repeatDay"] == "[1,2,3,4,5,6,7]"  # inherited from template row


def test_target_shorter_updates_plus_removes():
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    target = [("08:00", 5)]
    d = diff_schedule(current, target)
    assert [u["id"] for u in d["updates"]] == [1]
    assert d["adds"] == []
    assert d["removes"] == [2]


def test_empty_target_removes_all():
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    d = diff_schedule(current, [])
    assert d["updates"] == [] and d["adds"] == []
    assert sorted(d["removes"]) == [1, 2]


def test_add_uses_defaults_when_no_current_template():
    d = diff_schedule([], [("09:00", 2)])
    assert d["updates"] == [] and d["removes"] == []
    assert d["adds"][0]["repeatDay"] == "[1,2,3,4,5,6,7]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_diff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'petlibro_mcp.schedule_diff'`

- [ ] **Step 3: Write the implementation**

```python
# src/petlibro_mcp/schedule_diff.py
"""Pure: turn (current enabled rows, target rows) into update/add/remove actions."""
from __future__ import annotations

_DEFAULT_REPEAT = "[1,2,3,4,5,6,7]"


def diff_schedule(current_enabled: list[dict], target_rows: list[tuple[str, int]]) -> dict:
    """Edit-in-place diff: pair sorted current rows with sorted target rows by index."""
    cur = sorted(current_enabled, key=lambda p: p.get("executionTime", ""))
    tgt = sorted(target_rows, key=lambda r: r[0])
    template = cur[0] if cur else {}

    updates: list[dict] = []
    adds: list[dict] = []
    removes: list[int] = []

    n = min(len(cur), len(tgt))
    for i in range(n):
        row = cur[i]
        time, grain = tgt[i]
        updates.append({
            "id": row["id"],
            "executionTime": time,
            "grainNum": grain,
            "repeatDay": row.get("repeatDay", _DEFAULT_REPEAT),
            "label": row.get("label", ""),
            "enableAudio": row.get("enableAudio", False),
            "audioTimes": row.get("audioTimes", 2),
            "enable": True,
        })
    for i in range(n, len(tgt)):
        time, grain = tgt[i]
        adds.append({
            "executionTime": time,
            "grainNum": grain,
            "repeatDay": template.get("repeatDay", _DEFAULT_REPEAT),
            "label": template.get("label", ""),
            "enableAudio": template.get("enableAudio", False),
            "audioTimes": template.get("audioTimes", 2),
        })
    for i in range(n, len(cur)):
        removes.append(cur[i]["id"])

    return {"updates": updates, "adds": adds, "removes": removes}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule_diff.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/schedule_diff.py tests/test_schedule_diff.py
git commit -m "feat: pure edit-in-place schedule diff"
```

---

### Task 4: Extract the shared rhythm-compute helper

**Files:**
- Modify: `src/petlibro_mcp/tools.py`
- Test: `tests/test_tools.py` (append; existing `analyze_rhythm` tests must stay green)

**Interfaces:**
- Consumes: `history.parse_work_record`, `rhythm.*`, `planner.plan_rows`, `client.work_record`, `client.feeding_plans`.
- Produces:
  - `async def _compute_rhythm(client, feeder, days: int) -> dict` returning `{"enabled": list[dict], "current": list[tuple[str,int]], "total": int, "eating_visits": int, "target_rows": list[tuple[str,int]]}`.
  - `analyze_rhythm` refactored to build its result from `_compute_rhythm` (behavior unchanged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools.py  (append)
async def test_compute_rhythm_returns_enabled_and_target():
    client = _client_for_analyze()  # existing helper: 10 visits @ 08:00, plans 3+2 enabled, 1 disabled
    feeder = cfg().resolve_feeders(["ferris"])[0]
    rc = await tools._compute_rhythm(client, feeder, 60)
    assert rc["total"] == 5                 # enabled 3+2, disabled row excluded
    assert rc["eating_visits"] == 10
    assert len(rc["enabled"]) == 2          # disabled row excluded
    assert sum(g for _, g in rc["target_rows"]) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_tools.py -k compute_rhythm -v`
Expected: FAIL — `AttributeError: module 'petlibro_mcp.tools' has no attribute '_compute_rhythm'`

- [ ] **Step 3: Refactor**

In `src/petlibro_mcp/tools.py`, add the helper and rewrite `analyze_rhythm` to use it:

```python
async def _compute_rhythm(client, feeder, days: int) -> dict:
    raw = await client.work_record(feeder.serial, days=days)
    eats, _dispenses = parse_work_record(raw)
    plans = await client.feeding_plans(feeder.serial)
    enabled = [p for p in plans if p.get("enable", True)]
    current = [(p.get("executionTime"), int(p.get("grainNum") or 0)) for p in enabled]
    total = sum(g for _, g in current)
    tod = [(minute, max(dur, 1)) for minute, dur in eats]
    curve = circadian_curve(tod)
    split = split_at_peaks(curve, find_peaks(curve))
    target_rows = plan_rows(split, total)
    return {"enabled": enabled, "current": current, "total": total,
            "eating_visits": len(eats), "target_rows": target_rows}
```

Replace the body of `analyze_rhythm`'s per-feeder `try` block with:

```python
        try:
            rc = await _compute_rhythm(client, f, days)
            results.append({
                "pet": f.name, "serial": f.serial, "ok": True,
                "days": days, "eating_visits": rc["eating_visits"],
                "daily_total_portions": rc["total"],
                "current_schedule": [{"time": t, "portions": g} for t, g in rc["current"]],
                "recommended_schedule": [{"time": t, "portions": g} for t, g in rc["target_rows"]],
            })
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tools.py -v`
Expected: PASS (existing `analyze_rhythm` tests unchanged + the new `_compute_rhythm` test)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/tools.py tests/test_tools.py
git commit -m "refactor: extract _compute_rhythm shared by analyze/apply"
```

---

### Task 5: `apply_schedule` tool (dry-run / apply / verify / rollback)

**Files:**
- Modify: `src/petlibro_mcp/tools.py`
- Test: `tests/test_tools.py` (append)

**Interfaces:**
- Consumes: `_compute_rhythm` (Task 4), `schedule_diff.diff_schedule` (Task 3), `client.update_plan`/`add_plan`/`remove_plan`/`feeding_plans` (Task 1).
- Produces:
  - `async def apply_schedule(config, client, pet, days: int = 60, apply: bool = False) -> list[dict]`.
    - Dry-run (default): `{pet, serial, ok, dry_run: True, would_update, would_add, would_remove, target_schedule}`.
    - Applied: `{pet, serial, ok, applied: True, schedule}`.
    - Refused/failed: `{pet, serial, ok: False, error, ...}`.
  - Helpers `_apply_actions(client, serial, actions)` and `_rollback(client, serial, snapshot) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools.py  (append)

class _FakePlanClient:
    """Stateful fake: holds a plan list, mutates it via the write methods."""
    def __init__(self, plans, work, drop_writes=False):
        self._plans = [dict(p) for p in plans]
        self._work = work
        self._next_id = max([p["id"] for p in self._plans], default=0) + 1
        self.drop_writes = drop_writes  # simulate a cloud that silently ignores writes
    async def work_record(self, serial, days=60):
        return self._work
    async def feeding_plans(self, serial):
        return [dict(p) for p in self._plans]
    async def update_plan(self, serial, plan):
        if self.drop_writes:
            return
        for p in self._plans:
            if p["id"] == plan["id"]:
                p.update({"executionTime": plan["executionTime"], "grainNum": plan["grainNum"]})
    async def add_plan(self, serial, plan):
        if self.drop_writes:
            return
        self._plans.append({"id": self._next_id, "enable": True, **plan})
        self._next_id += 1
    async def remove_plan(self, serial, plan_id):
        if self.drop_writes:
            return
        self._plans = [p for p in self._plans if p["id"] != plan_id]


def _plan(id, t, g, enable=True):
    return {"id": id, "executionTime": t, "grainNum": g, "enable": enable,
            "repeatDay": "[1,2,3,4,5,6,7]", "label": "", "enableAudio": False, "audioTimes": 2}


async def test_apply_dry_run_previews_without_writing():
    # 10 visits @ 08:00 (one peak); current 3+2 -> total 5 -> target one meal of 5 @ 08:00
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)], _work_record_days())
    res = await tools.apply_schedule(cfg(), client, "ferris")  # apply defaults False
    r = res[0]
    assert r["ok"] is True and r["dry_run"] is True
    assert sum(x["portions"] for x in r["target_schedule"]) == 5
    # nothing was written: the plan list is unchanged
    plans = await client.feeding_plans("x")
    assert {(p["executionTime"], p["grainNum"]) for p in plans} == {("08:00", 3), ("20:00", 2)}


async def test_apply_writes_verifies_and_reports_applied():
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)], _work_record_days())
    res = await tools.apply_schedule(cfg(), client, "ferris", apply=True)
    r = res[0]
    assert r["ok"] is True and r.get("applied") is True
    # resulting enabled schedule matches the rhythm target (total preserved = 5)
    got = sorted((x["time"], x["portions"]) for x in r["schedule"])
    assert sum(g for _, g in got) == 5


async def test_apply_rolls_back_when_verify_fails():
    # drop_writes: writes are silently ignored -> readback won't match -> rollback path
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)],
                             _work_record_days(), drop_writes=True)
    res = await tools.apply_schedule(cfg(), client, "ferris", apply=True)
    r = res[0]
    assert r["ok"] is False
    assert "erify" in r["error"]  # "Verify failed..."


async def test_apply_refuses_when_target_exceeds_current(monkeypatch):
    client = _FakePlanClient([_plan(1, "08:00", 5)], _work_record_days())
    monkeypatch.setattr(tools, "plan_rows", lambda split, total: [("08:00", total + 5)])
    res = await tools.apply_schedule(cfg(), client, "ferris", apply=True)
    assert res[0]["ok"] is False and "Refusing" in res[0]["error"]


async def test_apply_unknown_pet_errors():
    client = _FakePlanClient([], _work_record_days())
    res = await tools.apply_schedule(cfg(), client, "mittens", apply=True)
    assert res[0]["ok"] is False and "mittens" in res[0]["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_tools.py -k apply_schedule -v`
Expected: FAIL — `AttributeError: module 'petlibro_mcp.tools' has no attribute 'apply_schedule'`

- [ ] **Step 3: Write the implementation**

Add the import near the other tool imports in `src/petlibro_mcp/tools.py`:

```python
from .schedule_diff import diff_schedule
```

Append the helpers and tool:

```python
async def _apply_actions(client, serial: str, actions: dict) -> None:
    for pid in actions["removes"]:
        await client.remove_plan(serial, pid)
    for plan in actions["updates"]:
        await client.update_plan(serial, plan)
    for plan in actions["adds"]:
        await client.add_plan(serial, plan)


def _enabled_pairs(plans) -> list[tuple[str, int]]:
    return sorted((p.get("executionTime"), int(p.get("grainNum") or 0))
                  for p in plans if p.get("enable", True))


async def _rollback(client, serial: str, snapshot: list[dict]) -> bool:
    """Restore `snapshot` by removing current enabled rows and re-adding the snapshot.
    Returns True if the readback matches the snapshot."""
    current = [p for p in await client.feeding_plans(serial) if p.get("enable", True)]
    for p in current:
        await client.remove_plan(serial, p["id"])
    for p in snapshot:
        await client.add_plan(serial, p)
    got = _enabled_pairs(await client.feeding_plans(serial))
    want = sorted((p.get("executionTime"), int(p.get("grainNum") or 0)) for p in snapshot)
    return got == want


async def apply_schedule(config: Config, client: PetLibroClient,
                         pet, days: int = 60, apply: bool = False) -> list[dict]:
    try:
        feeders = config.resolve_feeders([pet] if isinstance(pet, str) else pet)
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]

    results = []
    for f in feeders:
        try:
            rc = await _compute_rhythm(client, f, days)
            enabled, target_rows, total = rc["enabled"], rc["target_rows"], rc["total"]
            target_total = sum(g for _, g in target_rows)
            if target_total > total:
                results.append({"pet": f.name, "serial": f.serial, "ok": False,
                    "error": (f"Refusing: target total {target_total} exceeds "
                              f"current {total} portions.")})
                continue

            actions = diff_schedule(enabled, target_rows)
            if not apply:
                results.append({
                    "pet": f.name, "serial": f.serial, "ok": True, "dry_run": True,
                    "would_update": actions["updates"], "would_add": actions["adds"],
                    "would_remove": actions["removes"],
                    "target_schedule": [{"time": t, "portions": g} for t, g in target_rows]})
                continue

            snapshot = [dict(p) for p in enabled]
            await _apply_actions(client, f.serial, actions)
            got = _enabled_pairs(await client.feeding_plans(f.serial))
            want = sorted((t, g) for t, g in target_rows)
            if got == want:
                results.append({"pet": f.name, "serial": f.serial, "ok": True,
                    "applied": True,
                    "schedule": [{"time": t, "portions": g} for t, g in want]})
            else:
                rolled = await _rollback(client, f.serial, snapshot)
                results.append({"pet": f.name, "serial": f.serial, "ok": False,
                    "error": ("Verify failed after apply; rolled back to snapshot."
                              if rolled else
                              "Verify failed AND rollback failed — restore manually from snapshot."),
                    "expected": want, "got": got,
                    "snapshot": None if rolled else snapshot})
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_tools.py -v`
Expected: PASS (all existing + 5 new apply tests)

- [ ] **Step 5: Commit**

```bash
git add src/petlibro_mcp/tools.py tests/test_tools.py
git commit -m "feat: apply_schedule tool (dry-run/apply/verify/rollback)"
```

---

### Task 6: Register `apply_schedule` on the MCP server

**Files:**
- Modify: `src/petlibro_mcp/server.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `tools.apply_schedule`.
- Produces: MCP tool `apply_schedule` with schema `{pet: string (required), days?: integer, apply?: boolean}`.

- [ ] **Step 1: Update the failing test**

In `tests/test_server.py::test_lists_all_tools`, add `"apply_schedule"` to the expected name set:

```python
    assert names == {"feed", "open_lid", "feeder_status", "fountain_status",
                     "list_devices", "analyze_rhythm", "apply_schedule"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_server.py -k lists_all_tools -v`
Expected: FAIL — assertion mismatch (`apply_schedule` missing)

- [ ] **Step 3: Add the Tool definition**

Append to `TOOL_DEFS` in `src/petlibro_mcp/server.py`:

```python
    Tool(
        name="apply_schedule",
        description=("Write a cat's rhythm-tailored schedule to its feeder. "
                     "Defaults to a DRY-RUN preview (diff only); pass apply=true "
                     "to actually write. Snapshots, verifies, and rolls back on "
                     "mismatch. 'pet' is a single pet name."),
        inputSchema={
            "type": "object",
            "properties": {
                "pet": {"type": "string"},
                "days": {"type": "integer", "default": 60},
                "apply": {"type": "boolean", "default": False},
            },
            "required": ["pet"],
        },
    ),
```

- [ ] **Step 4: Add the dispatch branch**

In `_call_tool`, before the `else`:

```python
        elif name == "apply_schedule":
            result = await T.apply_schedule(config, client, a["pet"],
                                            a.get("days", 60), a.get("apply", False))
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -W error::DeprecationWarning -q`
Expected: PASS (entire suite green, pristine)

- [ ] **Step 6: Commit**

```bash
git add src/petlibro_mcp/server.py tests/test_server.py
git commit -m "feat: register apply_schedule MCP tool"
```

---

## Manual verification (after Task 6)

Dry-run against the live account (no writes) to confirm the previewed diff looks sane
for a couple of cats:

```bash
.venv/bin/python -c "
import asyncio, json
from petlibro_mcp.server import _load_env_file, PETS_TOML
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp import tools
_load_env_file()
async def main():
    cfg = load_config(PETS_TOML)
    client = PetLibroClient(cfg)
    for pet in ('saffron', 'ricco'):
        print(json.dumps(await tools.apply_schedule(cfg, client, pet), indent=2))  # dry-run
asyncio.run(main())
"
```

Expected: `dry_run: True` with `would_update`/`would_add`/`would_remove` and a
`target_schedule` whose portions sum to the cat's current total. The first real
`apply=true` write is a separate, supervised controller step (the human picks the cat).

## Follow-on (not this plan)

- **Phase 3:** `controller.py` self-calibrating daily total + persisted state +
  `tune_amounts` + periodic runner.
- **Phase 4:** the web UI on scott-server.
