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
