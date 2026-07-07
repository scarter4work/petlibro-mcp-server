"""Thin web-facing composition over petlibro_mcp tools."""
from __future__ import annotations
from petlibro_mcp.history import parse_work_record
from petlibro_mcp.rhythm import circadian_curve
from petlibro_mcp import tools


async def analysis(config, client, pet: str, days: int = 60) -> dict:
    base = (await tools.analyze_rhythm(config, client, pet, days))[0]
    if not base.get("ok"):
        return base
    raw = await client.work_record(base["serial"], days=days)
    eats, _ = parse_work_record(raw)
    curve = circadian_curve([(m, max(d, 1)) for m, d in eats])
    return {**base, "rhythm_curve": curve}


async def apply(config, client, pet: str, schedule, apply_flag: bool = False) -> dict:
    target_rows = [(row["time"], int(row["portions"])) for row in schedule]
    res = await tools.apply_target_rows(config, client, pet, target_rows, apply_flag)
    return res[0]
