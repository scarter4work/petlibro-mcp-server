import pytest
from unittest.mock import AsyncMock
from petlibro_mcp import tools
from petlibro_mcp.config import Config, Feeder, Fountain


def cfg():
    return Config(
        feeders=[Feeder("ferris", "SN-F", "", "", 12),
                 Feeder("zeus", "SN-Z", "", "", 12)],
        fountains=[Fountain("dockstream1", "WF-1", "", ["zeus"])],
        region="US", max_cups_per_command=4, email="a@b.com", password="pw",
    )


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    """Never let a test write the repo's real journal file.

    tools._default_journal_path() honours PETLIBRO_JOURNAL, so pointing it at
    tmp_path keeps each test's applied-schedule history to itself. Without this,
    an apply test stamps a real-clock entry that later tests read as "the
    schedule changed just now", zeroing days_since_change.
    """
    monkeypatch.setenv("PETLIBRO_JOURNAL", str(tmp_path / "journal.jsonl"))


@pytest.fixture
def client():
    m = AsyncMock()
    m.feed = AsyncMock(return_value=None)
    m.open_lid = AsyncMock(return_value=None)
    m.real_info = AsyncMock(return_value={
        "online": True, "surplusGrain": True, "batteryState": "high"})
    return m


async def test_feed_single_pet_converts_and_dispenses(client):
    res = await tools.feed(cfg(), client, ["ferris"], 3)
    assert res == [{"pet": "ferris", "cups": 3, "portions": 36,
                    "ok": True, "error": None}]
    client.feed.assert_awaited_once_with("SN-F", 36)


async def test_feed_all_hits_every_feeder(client):
    res = await tools.feed(cfg(), client, "all", 1)
    assert {r["pet"] for r in res} == {"ferris", "zeus"}
    assert client.feed.await_count == 2


async def test_feed_overfeed_refused_without_force(client):
    res = await tools.feed(cfg(), client, ["ferris"], 10)
    assert res[0]["ok"] is False
    assert "10" in res[0]["error"] and "ferris" in res[0]["error"]
    client.feed.assert_not_awaited()


async def test_feed_overfeed_allowed_with_force(client):
    res = await tools.feed(cfg(), client, ["ferris"], 10, force=True)
    assert res[0]["ok"] is True
    client.feed.assert_awaited_once()


async def test_feed_unknown_pet_reports_error(client):
    res = await tools.feed(cfg(), client, ["mittens"], 1)
    assert res[0]["ok"] is False
    assert "mittens" in res[0]["error"]
    client.feed.assert_not_awaited()


async def test_feed_partial_failure_is_reported(client):
    client.feed = AsyncMock(side_effect=[None, RuntimeError("offline")])
    res = await tools.feed(cfg(), client, ["ferris", "zeus"], 1)
    oks = {r["pet"]: r["ok"] for r in res}
    assert oks == {"ferris": True, "zeus": False}
    assert any("offline" in (r["error"] or "") for r in res)


async def test_open_lid_all(client):
    res = await tools.open_lid(cfg(), client, "all")
    assert {r["pet"] for r in res} == {"ferris", "zeus"}
    assert client.open_lid.await_count == 2


async def test_feeder_status_single(client):
    res = await tools.feeder_status(cfg(), client, "ferris")
    assert res[0]["pet"] == "ferris"
    assert res[0]["online"] is True
    # surplusGrain=True (fallback path) => hopper NOT low
    assert res[0]["food_low"] is False


async def test_food_low_uses_hopper_enum_when_present(client):
    client.real_info = AsyncMock(return_value={
        "online": True, "warehouseSurplusGrain": "GOOD", "surplusGrain": True})
    assert (await tools.feeder_status(cfg(), client, "ferris"))[0]["food_low"] is False
    client.real_info = AsyncMock(return_value={
        "online": True, "warehouseSurplusGrain": "SHORTAGE", "surplusGrain": True})
    assert (await tools.feeder_status(cfg(), client, "ferris"))[0]["food_low"] is True


async def test_food_low_none_when_no_signal(client):
    client.real_info = AsyncMock(return_value={"online": True})
    assert (await tools.feeder_status(cfg(), client, "ferris"))[0]["food_low"] is None


async def test_feeder_status_reports_failure(client):
    client.real_info = AsyncMock(side_effect=RuntimeError("offline"))
    res = await tools.feeder_status(cfg(), client, "ferris")
    assert len(res) == 1
    assert res[0]["ok"] is False
    assert "offline" in res[0]["error"]


async def test_fountain_status_reports_failure(client):
    client.real_info = AsyncMock(side_effect=RuntimeError("offline"))
    res = await tools.fountain_status(cfg(), client, "dockstream1")
    assert len(res) == 1
    assert res[0]["ok"] is False
    assert "offline" in res[0]["error"]


async def test_fountain_status_maps_named_fields(client):
    # payload shape confirmed against a live Dockstream fountain
    client.real_info = AsyncMock(return_value={
        "online": True, "weightPercent": 23, "weight": 669.0,
        "weightState": "LACK_WATER", "runningState": "LACK_WATER",
        "todayTotalMl": 0, "pumpAirState": True,
        "remainingReplacementDays": -139, "remainingCleaningDays": -94,
        "batteryState": "low", "errorState": False,
        "exceptionMessage": "Insufficient water",
    })
    r = (await tools.fountain_status(cfg(), client, "dockstream1"))[0]
    assert r["ok"] is True
    assert r["water_level_percent"] == 23
    assert r["water_state"] == "LACK_WATER"
    assert r["filter_days_remaining"] == -139
    assert r["pump_on"] is True
    assert r["message"] == "Insufficient water"
    assert "raw" not in r


async def test_feeder_status_unknown_pet_reports_error(client):
    res = await tools.feeder_status(cfg(), client, "mittens")
    assert res[0]["ok"] is False
    assert "mittens" in res[0]["error"]
    client.real_info.assert_not_awaited()


async def test_fountain_status_unknown_name_reports_error(client):
    res = await tools.fountain_status(cfg(), client, "nope")
    assert res[0]["ok"] is False
    assert "nope" in res[0]["error"]
    client.real_info.assert_not_awaited()


async def test_feed_rejects_nonpositive_cups(client):
    res = await tools.feed(cfg(), client, ["ferris"], 0)
    assert res[0]["ok"] is False
    client.feed.assert_not_awaited()

    res = await tools.feed(cfg(), client, ["ferris"], -3)
    assert res[0]["ok"] is False
    client.feed.assert_not_awaited()


def _work_record_days():
    # 10 eating visits all clustered at 08:00 (device wall-clock)
    recs = {"workRecords": []}
    for _ in range(10):
        recs["workRecords"].append({
            "eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
            "formatRecordTime": "2026-07-07 08:00",
            "params": '{"seconds":"02m00s"}',
        })
    return [recs]


def _client_for_analyze():
    m = AsyncMock()
    m.work_record = AsyncMock(return_value=_work_record_days())
    m.feeding_plans = AsyncMock(return_value=[
        {"executionTime": "08:00", "grainNum": 3, "enable": True},
        {"executionTime": "20:00", "grainNum": 2, "enable": True},
        {"executionTime": "23:00", "grainNum": 1, "enable": False},  # disabled: excluded
    ])
    return m


async def test_analyze_rhythm_reports_current_and_recommended():
    client = _client_for_analyze()
    res = await tools.analyze_rhythm(cfg(), client, "ferris")
    assert len(res) == 1
    r = res[0]
    assert r["ok"] is True and r["pet"] == "ferris"
    assert r["eating_visits"] == 10
    # current total = 3 + 2 (disabled row excluded) = 5
    assert r["daily_total_portions"] == 5
    assert sum(x["portions"] for x in r["recommended_schedule"]) == 5
    assert {"time", "portions"} <= set(r["recommended_schedule"][0])


async def test_analyze_rhythm_unknown_pet_reports_error():
    res = await tools.analyze_rhythm(cfg(), _client_for_analyze(), "mittens")
    assert res[0]["ok"] is False and "mittens" in res[0]["error"]


async def test_analyze_rhythm_surfaces_fetch_failure():
    client = _client_for_analyze()
    client.work_record = AsyncMock(side_effect=RuntimeError("offline"))
    res = await tools.analyze_rhythm(cfg(), client, "ferris")
    assert res[0]["ok"] is False and "offline" in res[0]["error"]


async def test_analyze_rhythm_treats_missing_enable_as_enabled():
    # a plan row with no "enable" key at all must still count toward the
    # daily total (p.get("enable", True) default), not be silently dropped.
    client = _client_for_analyze()
    client.feeding_plans = AsyncMock(return_value=[
        {"executionTime": "09:00", "grainNum": 4},
    ])
    res = await tools.analyze_rhythm(cfg(), client, "ferris")
    assert res[0]["ok"] is True
    assert res[0]["daily_total_portions"] == 4


async def test_compute_rhythm_returns_enabled_and_target(tmp_path):
    client = _client_for_analyze()  # existing helper: 10 visits @ 08:00, plans 3+2 enabled, 1 disabled
    feeder = cfg().resolve_feeders(["ferris"])[0]
    rc = await tools._compute_rhythm(client, feeder, 60, _NOW_MS, tmp_path / "j.jsonl")
    assert rc["total"] == 5                 # enabled 3+2, disabled row excluded
    assert rc["eating_visits"] == 10
    assert len(rc["enabled"]) == 2          # disabled row excluded
    assert sum(g for _, g in rc["target_rows"]) == 5


class _FakePlanClient:
    """Stateful fake: holds a plan list, mutates it via the write methods.

    Fault injection is write-generic on purpose. Which primitive a given diff uses is
    an implementation detail — a schedule whose times all move is now expressed as
    removes+adds with zero updates — so hooks bound only to update_plan would silently
    stop firing and leave the rollback tests asserting nothing.
    """
    def __init__(self, plans, work, drop_writes=False, corrupt=0, raise_on_write=0):
        self._plans = [dict(p) for p in plans]
        self._work = work
        self._next_id = max([p["id"] for p in self._plans], default=0) + 1
        self.drop_writes = drop_writes  # simulate a cloud that silently ignores writes
        # Countdowns, not flags: the fault must hit the apply and then stop, because
        # _rollback re-adds rows through these same methods and has to be able to win.
        self.corrupt = corrupt
        self.raise_on_write = raise_on_write

    def _maybe_raise(self, what):
        if self.raise_on_write > 0:
            self.raise_on_write -= 1
            raise RuntimeError(f"{what} failed")

    def _grain(self, plan):
        if self.corrupt > 0:
            self.corrupt -= 1
            return plan["grainNum"] + 100
        return plan["grainNum"]

    async def work_record(self, serial, days=60):
        return self._work
    async def feeding_plans(self, serial):
        return [dict(p) for p in self._plans]
    async def update_plan(self, serial, plan):
        self._maybe_raise("update")
        if self.drop_writes:
            return
        grain = self._grain(plan)
        for p in self._plans:
            if p["id"] == plan["id"]:
                p.update({"executionTime": plan["executionTime"], "grainNum": grain})
    async def add_plan(self, serial, plan):
        self._maybe_raise("add")
        if self.drop_writes:
            return
        grain = self._grain(plan)
        self._plans.append({"id": self._next_id, "enable": True, **plan, "grainNum": grain})
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


async def test_preview_returns_the_devices_actual_current_schedule():
    """The UI's "Now, on the feeder" column can go stale (phone app, another session).
    Every preview re-reads the device, so hand that fresh state back to the caller."""
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)], _work_record_days())
    r = (await tools.apply_schedule(cfg(), client, "ferris"))[0]
    assert r["dry_run"] is True
    assert r["current_schedule"] == [{"time": "08:00", "portions": 3},
                                     {"time": "20:00", "portions": 2}]


async def test_applied_result_reports_the_new_current_schedule():
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)], _work_record_days())
    r = (await tools.apply_schedule(cfg(), client, "ferris", apply=True))[0]
    assert r["applied"] is True
    assert r["current_schedule"] == r["schedule"]  # what the device now holds


async def test_rolled_back_result_reports_the_restored_schedule():
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)],
                             _work_record_days(), raise_on_write=1)
    r = (await tools.apply_schedule(cfg(), client, "ferris", apply=True))[0]
    assert r["ok"] is False and "rolled back" in r["error"]
    # the snapshot is what the device actually holds again — don't leave the UI
    # showing the schedule we failed to write
    assert r["current_schedule"] == [{"time": "08:00", "portions": 3},
                                     {"time": "20:00", "portions": 2}]


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


async def test_apply_rollback_restores_genuinely_mutated_state():
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)],
                             _work_record_days(), corrupt=1)
    res = await tools.apply_schedule(cfg(), client, "ferris", apply=True)
    r = res[0]
    assert r["ok"] is False and "erify" in r["error"] and "rolled back" in r["error"]
    restored = sorted((p["executionTime"], p["grainNum"])
                      for p in await client.feeding_plans("x") if p.get("enable", True))
    assert restored == [("08:00", 3), ("20:00", 2)]


async def test_apply_rolls_back_when_write_raises():
    client = _FakePlanClient([_plan(1, "08:00", 3), _plan(2, "20:00", 2)],
                             _work_record_days(), raise_on_write=1)
    res = await tools.apply_schedule(cfg(), client, "ferris", apply=True)
    r = res[0]
    assert r["ok"] is False and "mid-apply" in r["error"]
    restored = sorted((p["executionTime"], p["grainNum"])
                      for p in await client.feeding_plans("x") if p.get("enable", True))
    assert restored == [("08:00", 3), ("20:00", 2)]


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


# ---------- jam-aware trimming ----------
from petlibro_mcp.evidence import MS_PER_DAY  # noqa: E402

_NOW_MS = 100 * MS_PER_DAY


def _jammy_client(blocks=30, days_since=30, total_rows=(("14:00", 5), ("22:00", 7))):
    """A feeder whose schedule changed `days_since` ago and jammed `blocks` times since."""
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
    res = await tools.analyze_rhythm(cfg(), client, "ferris", days=60, now_ms=_NOW_MS)
    out = res[0]
    assert out["ok"] is True
    assert out["daily_total_portions"] == 12   # the real fleet's flat default
    recommended = sum(r["portions"] for r in out["recommended_schedule"])
    assert recommended < 12, "a jammy feeder must be recommended LESS than it gets now"
    assert out["trim"]["applied"] > 0


async def test_analyze_rhythm_leaves_a_clean_feeder_alone():
    client = _jammy_client(blocks=1, days_since=30)
    out = (await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                      now_ms=_NOW_MS))[0]
    assert out["trim"]["applied"] == 0
    assert sum(r["portions"] for r in out["recommended_schedule"]) == 12


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
                                        apply=True, now_ms=_NOW_MS, journal_path=jpath)
    assert res[0]["ok"] is True
    assert journal.last_at_ms(jpath, "SN-F") == _NOW_MS


async def test_dry_run_does_not_journal(tmp_path):
    from petlibro_mcp import journal
    client = _jammy_client(blocks=1, days_since=30)
    jpath = tmp_path / "j.jsonl"
    await tools.apply_target_rows(cfg(), client, "ferris", [("14:00", 1)],
                                  apply=False, now_ms=_NOW_MS, journal_path=jpath)
    assert journal.last_at_ms(jpath, "SN-F") is None


async def test_dry_run_resolves_removed_plan_ids_to_times():
    """A preview must be able to name the meal it would delete, not just its row id."""
    client = _jammy_client(blocks=1, days_since=30)
    client.feeding_plans = AsyncMock(return_value=[
        {"id": 41, "executionTime": "14:00", "grainNum": 2, "enable": True},
        {"id": 42, "executionTime": "22:00", "grainNum": 4, "enable": True},
    ])
    res = await tools.apply_target_rows(cfg(), client, "ferris", [("22:00", 4)],
                                        apply=False, now_ms=_NOW_MS)
    out = res[0]
    assert out["would_remove"] == [41]
    assert out["would_remove_times"] == [{"id": 41, "time": "14:00"}]


async def test_jam_rate_denominator_is_clamped_to_available_history():
    """ferris's case: the schedule changed 26d ago but the cloud returns only 13d of
    records. Dividing blocks by 26 halves the true rate. The gate still uses the real
    26-day age; only the rate denominator is clamped to the data we actually have."""
    change_at = _NOW_MS - 26 * MS_PER_DAY
    history_start = _NOW_MS - 13 * MS_PER_DAY
    records = []
    for i in range(13):  # 13 blocks spread over the 13 days we have data for
        records.append({"type": "GRAIN_OUTLET_BLOCK_REMOVED",
                        "eventType": "GRAIN_OUTLET_BLOCK_REMOVED",
                        "recordTime": history_start + i * MS_PER_DAY,
                        "formatRecordTime": "2026-08-01 14:05"})
    records.append({"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
                    "recordTime": history_start, "formatRecordTime": "2026-08-01 14:00",
                    "params": '{"seconds":"02m00s"}'})
    client = AsyncMock()
    client.work_record = AsyncMock(return_value=[{"workRecords": records}])
    client.feeding_plans = AsyncMock(return_value=[
        {"id": 1, "executionTime": "14:00", "grainNum": 12, "enable": True,
         "updateTime": change_at}])

    out = (await tools.analyze_rhythm(cfg(), client, "ferris", days=60,
                                      now_ms=_NOW_MS))[0]
    # 13 blocks over 13 days of data == 1.0/day, NOT 13/26 == 0.5/day
    assert out["trim"]["jam_rate"] == 1.0
    # the eligibility gate still sees the change's true age
    assert round(out["evidence"]["days_since_change"]) == 26
