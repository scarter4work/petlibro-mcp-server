"""Read-only: what would Reanalyze now propose for every cat, and why?"""
import asyncio, os
from datetime import datetime, timezone
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp import tools

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


async def main():
    cfg = load_config(str(ROOT / "pets.toml"))
    client = PetLibroClient(cfg)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for res in await tools.analyze_rhythm(cfg, client, None, 60, now_ms=now_ms):
        if not res.get("ok"):
            print(f"{res['pet']:9s} ERROR {res.get('error')}")
            continue
        ev, tr = res["evidence"], res["trim"]
        now_total = res["daily_total_portions"]
        rec = sum(r["portions"] for r in res["recommended_schedule"])
        since = ev["days_since_change"]
        print(f"\n{res['pet']:9s} {now_total} -> {rec} portions/day"
              f"   trim={tr['applied']} withheld={tr['withheld_reason']}")
        print(f"  jams {tr['jam_rate']:.2f}/day"
              f"   days_since_change={since if since is None else round(since)}"
              f"   verdict={ev['verdict']}   grain_low={ev['grain_low_since']}")
        print(f"  delivered {ev['delivered_per_day_since']:.1f}/day"
              f" of {ev['scheduled_per_day']} scheduled")
        print(f"  proposed: {[(r['time'], r['portions']) for r in res['recommended_schedule']]}")
        print(f"  jams/meal: {tr['blocks_by_time']}")

asyncio.run(main())
