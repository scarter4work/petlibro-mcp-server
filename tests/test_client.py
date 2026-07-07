import pytest
from unittest.mock import AsyncMock
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp.config import Config, Feeder


def make_config():
    return Config(
        feeders=[Feeder("ferris", "SN-FERRIS", "", "", 12)],
        fountains=[], region="US", max_cups_per_command=4,
        email="a@b.com", password="pw",
    )


@pytest.fixture
def api():
    m = AsyncMock()
    m.login = AsyncMock(return_value="token123")
    m.set_manual_feed = AsyncMock(return_value={"code": 0})
    m.set_manual_lid_open = AsyncMock(return_value={"code": 0})
    m.get_device_real_info = AsyncMock(return_value={"surplusGrain": True})
    m.list_devices = AsyncMock(return_value=[{"deviceSn": "SN-FERRIS"}])
    return m


async def test_ensure_login_calls_api_once(api):
    c = PetLibroClient(make_config(), api=api)
    await c.ensure_login()
    await c.ensure_login()
    api.login.assert_awaited_once_with("a@b.com", "pw")


async def test_feed_logs_in_then_dispenses(api):
    c = PetLibroClient(make_config(), api=api)
    await c.feed("SN-FERRIS", 36)
    api.login.assert_awaited_once()
    api.set_manual_feed.assert_awaited_once_with("SN-FERRIS", 36)


async def test_open_lid_calls_api(api):
    c = PetLibroClient(make_config(), api=api)
    await c.open_lid("SN-FERRIS")
    api.set_manual_lid_open.assert_awaited_once_with("SN-FERRIS")


async def test_real_info_passthrough(api):
    c = PetLibroClient(make_config(), api=api)
    assert await c.real_info("SN-FERRIS") == {"surplusGrain": True}


def _cfg():
    return Config(feeders=[], fountains=[], region="US",
                  max_cups_per_command=4, email="a@b.com", password="pw")


async def test_work_record_posts_expected_payload():
    api = AsyncMock()
    api.session.request = AsyncMock(return_value=[{"workRecords": []}])
    client = PetLibroClient(_cfg(), api=api)
    out = await client.work_record("SN-1", days=30, size=500)
    assert out == [{"workRecords": []}]
    api.login.assert_awaited()
    args, kwargs = api.session.request.call_args
    assert args[0] == "POST" and args[1] == "/device/workRecord/list"
    body = kwargs["json"]
    assert body["deviceSn"] == "SN-1" and body["size"] == 500
    assert body["startTime"] < body["endTime"]


async def test_feeding_plans_delegates_to_api():
    api = AsyncMock()
    api.get_feeding_plans = AsyncMock(return_value=[{"executionTime": "08:00", "grainNum": 3}])
    client = PetLibroClient(_cfg(), api=api)
    out = await client.feeding_plans("SN-1")
    assert out == [{"executionTime": "08:00", "grainNum": 3}]
    api.login.assert_awaited()
    api.get_feeding_plans.assert_awaited_once_with("SN-1")
