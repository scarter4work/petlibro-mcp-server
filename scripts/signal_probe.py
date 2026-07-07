"""Read-only: pull MAX history + test candidate over/under-supply signals. Reads .env."""
import asyncio, os, re, datetime as dt
from collections import defaultdict
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient

for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def parse_seconds(s: str) -> int:
    h = re.search(r"(\d+)h", s); m = re.search(r"(\d+)m", s); sec = re.search(r"(\d+)s", s)
    return (int(h.group(1))*3600 if h else 0)+(int(m.group(1))*60 if m else 0)+(int(sec.group(1)) if sec else 0)


async def big_work_record(api, serial, size=1000):
    """Same endpoint but larger size and no type filter, to pull max history."""
    now = dt.datetime.utcnow()
    start = int((now - dt.timedelta(days=60)).timestamp()*1000)
    end = int(now.timestamp()*1000)
    return await api.session.request("POST", "/device/workRecord/list", json={
        "deviceSn": serial, "startTime": start, "endTime": end, "size": size,
    })


async def main():
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "pets.toml"))
    client = PetLibroClient(cfg); await client.ensure_login(); api = client._api

    for f in cfg.feeders:
        recs = await big_work_record(api, f.serial)
        eats = []       # (ts, dur_s)
        dispenses = []  # (ts, grain)
        days = set()
        for day in recs or []:
            days.add(day.get("recordTime"))
            for w in day.get("workRecords", []):
                ts = (w.get("recordTime") or 0)/1000
                if w.get("eventType") == "PET_IDENTIFY_LEAVE_EVENT_BIND_PET":
                    d = parse_seconds(w.get("params","") or "") or parse_seconds(w.get("content","") or "")
                    eats.append((ts, d))
                elif w.get("type") == "GRAIN_OUTPUT_SUCCESS":
                    dispenses.append((ts, w.get("actualGrainNum",0) or 0))
        eats.sort(); dispenses.sort()
        # span of data
        allts = [t for t,_ in eats] + [t for t,_ in dispenses]
        span_days = (max(allts)-min(allts))/86400 if allts else 0
        # SIGNAL 1: post-dispense uptake within 30 min
        W = 30*60
        got = 0
        for dts,_ in dispenses:
            if any(0 <= ets-dts <= W for ets,_ in eats):
                got += 1
        uptake = got/len(dispenses) if dispenses else float('nan')
        # SIGNAL 2: foraging pressure = share of eat-mass occurring >3h after last dispense
        forage_mass = 0; total_mass = 0
        for ets, edur in eats:
            prev = [dts for dts,_ in dispenses if dts <= ets]
            gap = (ets - max(prev)) if prev else 1e9
            total_mass += edur
            if gap > 3*3600:
                forage_mass += edur
        forage = forage_mass/total_mass if total_mass else float('nan')
        print(f"{f.name:9s} span={span_days:4.1f}d  eats={len(eats):4d}  dispenses={len(dispenses):3d}  "
              f"uptake@30m={uptake:5.2f}  forage>3h={forage:5.2f}")


asyncio.run(main())
