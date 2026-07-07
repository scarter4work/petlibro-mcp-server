"""Read-only: dump each feeder's recurring feeding plan. Reads .env."""
import asyncio, os, json
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient

for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


async def main():
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "pets.toml"))
    client = PetLibroClient(cfg)
    await client.ensure_login()
    api = client._api
    for f in cfg.feeders:
        try:
            plans = await api.get_feeding_plans(f.serial)
        except Exception as e:
            print(f"\n== {f.name} ({f.serial}) -> ERROR: {e}")
            continue
        print(f"\n== {f.name} ({f.serial}) : {len(plans)} plan rows")
        print(json.dumps(plans, indent=2))


asyncio.run(main())
