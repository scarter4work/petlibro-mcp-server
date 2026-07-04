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
