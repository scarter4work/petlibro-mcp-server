"""Recompute every cat's rhythm plan and WRITE it to the feeder.

Previews first, then applies. Each write is snapshotted, verified by readback, and
rolled back on mismatch by tools._apply_with_rollback. Pass --dry to preview only.
"""
import asyncio, os, sys
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

DRY = "--dry" in sys.argv


def rows_of(sched):
    return [(r["time"], r["portions"]) for r in sched]


async def main():
    cfg = load_config(str(ROOT / "pets.toml"))
    client = PetLibroClient(cfg)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    analyses = await tools.analyze_rhythm(cfg, client, None, 60, now_ms=now_ms)
    plan = {}
    print("=" * 70)
    for a in analyses:
        if not a.get("ok"):
            print(f"{a['pet']:9s} ANALYSIS FAILED: {a.get('error')}")
            continue
        cur = sum(r["portions"] for r in a["current_schedule"])
        tgt = sum(r["portions"] for r in a["recommended_schedule"])
        plan[a["pet"]] = rows_of(a["recommended_schedule"])
        print(f"{a['pet']:9s} {cur} -> {tgt} portions/day   "
              f"trim={a['trim']['applied']}  jams={a['trim']['jam_rate']:.2f}/day")
        print(f"          now:      {rows_of(a['current_schedule'])}")
        print(f"          proposed: {plan[a['pet']]}")
        assert tgt >= 8, f"{a['pet']}: target {tgt} below the 8/day floor"
        assert all(g >= 1 for _, g in plan[a["pet"]]), f"{a['pet']}: a meal is below 1"
    print("=" * 70)

    if DRY:
        print("\n--dry: nothing written.")
        return

    print("\nWRITING to feeders (verify + rollback on mismatch)\n")
    failures = 0
    for pet, rows in plan.items():
        res = (await tools.apply_target_rows(cfg, client, pet, rows, apply=True,
                                            now_ms=now_ms))[0]
        if res.get("ok") and res.get("applied"):
            print(f"  OK    {pet:9s} -> {rows_of(res['schedule'])}")
            if res.get("journal_warning"):
                print(f"        (journal note: {res['journal_warning']})")
        else:
            failures += 1
            print(f"  FAIL  {pet:9s} {res.get('error')}")
            if res.get("snapshot"):
                print(f"        MANUAL RESTORE NEEDED from: {res['snapshot']}")
    print(f"\n{len(plan) - failures}/{len(plan)} feeders written."
          f"{'  ALL OK' if failures == 0 else '  SOME FAILED'}")

asyncio.run(main())
