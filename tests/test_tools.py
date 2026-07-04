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
