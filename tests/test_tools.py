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
