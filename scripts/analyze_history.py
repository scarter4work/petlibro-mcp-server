"""Read-only: aggregate 30d per-cat eating + dispense history. Reads .env."""
import asyncio, os, re
from collections import defaultdict
from pathlib import Path
from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient

for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def parse_seconds(s: str) -> int:
    # formats like "01m37s", "24s", "1h02m03s"
    h = re.search(r"(\d+)h", s)
    m = re.search(r"(\d+)m", s)
    sec = re.search(r"(\d+)s", s)
    return (int(h.group(1)) * 3600 if h else 0) + (int(m.group(1)) * 60 if m else 0) + (int(sec.group(1)) if sec else 0)


async def main():
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "pets.toml"))
    client = PetLibroClient(cfg)
    await client.ensure_login()
    api = client._api

    print(f"{'cat':9s} {'days':>4s} {'eat_s/day':>9s} {'visits/day':>10s} {'avg_visit_s':>11s} {'disp_g/day':>10s} {'hour_histogram (visits by hour)'}")
    for f in cfg.feeders:
        recs = await api.get_device_work_record(f.serial)
        days = set()
        eat_seconds = 0
        visits = 0
        dispensed = 0
        by_hour = defaultdict(int)
        for day in recs or []:
            days.add(day.get("recordTime"))
            for w in day.get("workRecords", []):
                t = w.get("type")
                et = w.get("eventType")
                ts = w.get("recordTime", 0) / 1000
                import datetime as _dt
                hour = _dt.datetime.fromtimestamp(ts).hour if ts else 0
                if et == "PET_IDENTIFY_LEAVE_EVENT_BIND_PET":
                    secs = parse_seconds(w.get("params", "") or "")
                    if secs == 0:
                        # try content
                        secs = parse_seconds(w.get("content", "") or "")
                    eat_seconds += secs
                    visits += 1
                    by_hour[hour] += 1
                elif t == "GRAIN_OUTPUT_SUCCESS":
                    dispensed += w.get("actualGrainNum", 0) or 0
        nd = max(len(days), 1)
        hist = "".join(str(min(by_hour.get(h, 0), 9)) for h in range(24))
        print(f"{f.name:9s} {nd:4d} {eat_seconds/nd:9.0f} {visits/nd:10.1f} {(eat_seconds/visits if visits else 0):11.0f} {dispensed/nd:10.1f}  h0>{hist}<h23")


asyncio.run(main())
