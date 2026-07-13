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
        {"id": 1, "executionTime": "08:00", "grainNum": 3, "enable": True},
        {"id": 2, "executionTime": "20:00", "grainNum": 2, "enable": True}])
    return m


async def test_analysis_includes_curve_and_schedules():
    out = await rhythm_api.analysis(_cfg(), _client(), "ferris")
    assert out["ok"] is True and out["pet"] == "ferris"
    assert out["daily_total_portions"] == 5
    assert len(out["rhythm_curve"]) == 48
    assert sum(x["portions"] for x in out["recommended_schedule"]) == 5


async def test_feed_converts_portions_to_cups():
    """portions_per_cup=12, so 6 portions is half a cup — the device still gets 6."""
    client = _client()
    out = await rhythm_api.feed(_cfg(), client, "ferris", 6)
    assert out["ok"] is True and out["portions"] == 6
    client.feed.assert_awaited_once_with("SN-F", 6)


async def test_feed_over_cap_refused():
    """Cap is 4 cups = 48 portions at portions_per_cup=12."""
    client = _client()
    out = await rhythm_api.feed(_cfg(), client, "ferris", 49)
    assert out["ok"] is False and "cap" in out["error"]
    client.feed.assert_not_awaited()


async def test_feed_nonpositive_refused():
    client = _client()
    out = await rhythm_api.feed(_cfg(), client, "ferris", 0)
    assert out["ok"] is False
    client.feed.assert_not_awaited()


async def test_feed_unknown_pet_reports_error():
    out = await rhythm_api.feed(_cfg(), _client(), "mittens", 1)
    assert out["ok"] is False and "Unknown pet" in out["error"]


async def test_feed_surfaces_device_error():
    client = _client()
    client.feed = AsyncMock(side_effect=RuntimeError("offline"))
    out = await rhythm_api.feed(_cfg(), client, "ferris", 2)
    assert out["ok"] is False and "offline" in out["error"]


async def test_apply_delegates_to_target_rows():
    client = _client()
    out = await rhythm_api.apply(_cfg(), client, "ferris",
                                 [{"time": "09:00", "portions": 4}], apply_flag=False)
    assert out["ok"] is True and out["dry_run"] is True
    assert out["target_schedule"] == [{"time": "09:00", "portions": 4}]
