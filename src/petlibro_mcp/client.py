"""Clean async facade over the vendored PetLibro cloud client."""
from __future__ import annotations
from datetime import datetime, timedelta
from .config import Config
from .vendored.api import PetLibroAPI


class PetLibroClient:
    def __init__(self, config: Config, api: PetLibroAPI | None = None):
        self._config = config
        self._api = api or PetLibroAPI(config.email, config.password, region=config.region)
        self._logged_in = False

    async def ensure_login(self) -> None:
        if not self._logged_in:
            await self._api.login(self._config.email, self._config.password)
            self._logged_in = True

    async def list_devices(self) -> list[dict]:
        await self.ensure_login()
        return await self._api.list_devices()

    async def feed(self, serial: str, portions: int) -> None:
        await self.ensure_login()
        await self._api.set_manual_feed(serial, portions)

    async def open_lid(self, serial: str) -> None:
        await self.ensure_login()
        await self._api.set_manual_lid_open(serial)

    async def real_info(self, serial: str) -> dict:
        await self.ensure_login()
        return await self._api.get_device_real_info(serial)

    async def work_record(self, serial: str, days: int = 60, size: int = 1000) -> list:
        await self.ensure_login()
        now = datetime.utcnow()
        start = int((now - timedelta(days=days)).timestamp() * 1000)
        end = int(now.timestamp() * 1000)
        return await self._api.session.request("POST", "/device/workRecord/list", json={
            "deviceSn": serial, "startTime": start, "endTime": end, "size": size,
        })

    async def feeding_plans(self, serial: str) -> list:
        await self.ensure_login()
        return await self._api.get_feeding_plans(serial)
