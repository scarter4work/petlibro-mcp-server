"""Read-only: probe what history/consumption data the API exposes. Reads .env."""
import asyncio, os, json
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient

for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def show(label, data, limit=2000):
    s = json.dumps(data, indent=2, default=str)
    print(f"\n----- {label} -----")
    print(s[:limit])
    if len(s) > limit:
        print(f"...[truncated, {len(s)} chars total]")


async def main():
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "pets.toml"))
    client = PetLibroClient(cfg)
    await client.ensure_login()
    api = client._api
    # probe against saffron (alleged over-eater) and ricco (fine) for contrast
    targets = {f.name: f.serial for f in cfg.feeders if f.name in ("saffron", "ricco")}
    for name, sn in targets.items():
        print(f"\n############ {name} ({sn}) ############")
        for label, coro in [
            ("workRecord (GRAIN_OUTPUT_SUCCESS, 30d)", api.get_device_work_record(sn)),
            ("deviceEventsV2", api.get_device_events(sn)),
            ("grainStatus", api.device_grain_status(sn)),
            ("data/realInfo (intake/weight)", api.get_device_data_real_info(sn)),
            ("feedingPlan/todayNew", api.device_feeding_plan_today_new(sn)),
        ]:
            try:
                show(label, await coro)
            except Exception as e:
                print(f"\n----- {label} -----\nERROR: {e}")


asyncio.run(main())
