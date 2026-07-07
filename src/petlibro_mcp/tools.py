"""MCP tool logic: resolve, guard, call the facade, shape results."""
from __future__ import annotations
from .config import Config, cups_to_portions, UnknownPetError
from .client import PetLibroClient
from .history import parse_work_record
from .rhythm import circadian_curve, find_peaks, split_at_peaks
from .planner import plan_rows


async def feed(config: Config, client: PetLibroClient, pets, cups: float,
               force: bool = False) -> list[dict]:
    try:
        feeders = config.resolve_feeders(pets)
    except UnknownPetError as e:
        return [{"pet": None, "cups": cups, "portions": None,
                 "ok": False, "error": str(e)}]

    if cups <= 0:
        return [{"pet": None, "cups": cups, "portions": None, "ok": False,
                 "error": f"Refusing non-positive cups ({cups})."}]

    if not force and cups > config.max_cups_per_command:
        offenders = ", ".join(f.name for f in feeders)
        return [{"pet": None, "cups": cups, "portions": None, "ok": False,
                 "error": (f"Refusing {cups} cups (> {config.max_cups_per_command} "
                           f"cap) for {offenders}. Pass force=true to override.")}]

    results = []
    for f in feeders:
        portions = cups_to_portions(f, cups)
        item = {"pet": f.name, "cups": cups, "portions": portions,
                "ok": True, "error": None}
        try:
            await client.feed(f.serial, portions)
        except Exception as exc:  # surface, never swallow
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
    return results


async def open_lid(config: Config, client: PetLibroClient, pets) -> list[dict]:
    try:
        feeders = config.resolve_feeders(pets)
    except UnknownPetError as e:
        return [{"pet": None, "ok": False, "error": str(e)}]
    results = []
    for f in feeders:
        item = {"pet": f.name, "ok": True, "error": None}
        try:
            await client.open_lid(f.serial)
        except Exception as exc:
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
    return results


async def feeder_status(config: Config, client: PetLibroClient,
                        pet=None) -> list[dict]:
    try:
        feeders = config.resolve_feeders("all" if pet is None else [pet])
    except UnknownPetError as e:
        return [{"pet": None, "ok": False, "error": str(e)}]
    results = []
    for f in feeders:
        try:
            info = await client.real_info(f.serial)
            results.append({
                "pet": f.name, "serial": f.serial,
                "online": info.get("online"),
                "food_low": info.get("surplusGrain"),
                "battery": info.get("batteryState"),
                "today_feeding_quantity": info.get("todayFeedingQuantity"),
                "raw": info,
                "ok": True,
            })
        except Exception as exc:
            results.append({"pet": f.name, "serial": f.serial,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


def _shape_fountain(f, info: dict) -> dict:
    """Extract the human-useful fields from a fountain's raw realInfo payload.

    Keys confirmed against live Dockstream RFID Smart Fountain payloads. A
    negative *_days_remaining means the filter/cleaning cycle is overdue.
    """
    return {
        "fountain": f.name,
        "serial": f.serial,
        "near": f.near,
        "ok": True,
        "online": info.get("online"),
        "water_level_percent": info.get("weightPercent"),
        "water_weight_g": info.get("weight"),
        "water_state": info.get("weightState"),          # NORMAL / LACK_WATER
        "running_state": info.get("runningState"),
        "water_dispensed_today_ml": info.get("todayTotalMl"),
        "pump_on": info.get("pumpAirState"),
        "filter_days_remaining": info.get("remainingReplacementDays"),
        "cleaning_days_remaining": info.get("remainingCleaningDays"),
        "battery_state": info.get("batteryState"),
        "error": info.get("errorState"),
        "message": info.get("exceptionMessage"),         # present only on a fault
    }


async def fountain_status(config: Config, client: PetLibroClient,
                          name=None) -> list[dict]:
    try:
        fountains = config.resolve_fountains(name)
    except UnknownPetError as e:
        return [{"fountain": None, "ok": False, "error": str(e)}]
    results = []
    for f in fountains:
        try:
            info = await client.real_info(f.serial)
            results.append(_shape_fountain(f, info))
        except Exception as exc:
            results.append({"fountain": f.name, "serial": f.serial,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


async def analyze_rhythm(config: Config, client: PetLibroClient,
                         pet=None, days: int = 60) -> list[dict]:
    try:
        feeders = config.resolve_feeders("all" if pet is None else [pet])
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]

    results = []
    for f in feeders:
        try:
            raw = await client.work_record(f.serial, days=days)
            eats, _dispenses = parse_work_record(raw)

            plans = await client.feeding_plans(f.serial)
            current = [(p.get("executionTime"), int(p.get("grainNum") or 0))
                       for p in plans if p.get("enable", True)]
            total = sum(g for _, g in current)

            tod = [(minute, max(dur, 1)) for minute, dur in eats]
            curve = circadian_curve(tod)
            split = split_at_peaks(curve, find_peaks(curve))
            recommended = plan_rows(split, total)

            results.append({
                "pet": f.name, "serial": f.serial, "ok": True,
                "days": days, "eating_visits": len(eats),
                "daily_total_portions": total,
                "current_schedule": [{"time": t, "portions": g} for t, g in current],
                "recommended_schedule": [{"time": t, "portions": g} for t, g in recommended],
            })
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


async def list_devices(config: Config, client: PetLibroClient) -> dict:
    cloud = []
    try:
        cloud = await client.list_devices()
    except Exception as exc:
        cloud = [{"ok": False, "error": f"{type(exc).__name__}: {exc}"}]
    return {
        "feeders": [{"pet": f.name, "serial": f.serial} for f in config.feeders],
        "fountains": [{"name": f.name, "serial": f.serial} for f in config.fountains],
        "cloud_devices": cloud,
    }
