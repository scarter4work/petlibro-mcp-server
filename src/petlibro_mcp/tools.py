"""MCP tool logic: resolve, guard, call the facade, shape results."""
from __future__ import annotations
from .config import Config, cups_to_portions, UnknownPetError
from .client import PetLibroClient
from .history import parse_work_record
from .rhythm import circadian_curve, find_peaks, split_at_peaks
from .planner import plan_rows
from .schedule_diff import diff_schedule


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


async def _compute_rhythm(client, feeder, days: int) -> dict:
    """Compute rhythm analysis for a feeder: enabled plans, current schedule, target rows.

    Returns dict with keys: enabled, current, total, eating_visits, target_rows.
    """
    raw = await client.work_record(feeder.serial, days=days)
    eats, _dispenses = parse_work_record(raw)
    plans = await client.feeding_plans(feeder.serial)
    enabled = [p for p in plans if p.get("enable", True)]
    current = [(p.get("executionTime"), int(p.get("grainNum") or 0)) for p in enabled]
    total = sum(g for _, g in current)
    tod = [(minute, max(dur, 1)) for minute, dur in eats]
    curve = circadian_curve(tod)
    split = split_at_peaks(curve, find_peaks(curve))
    target_rows = plan_rows(split, total)
    return {"enabled": enabled, "current": current, "total": total,
            "eating_visits": len(eats), "target_rows": target_rows}


async def analyze_rhythm(config: Config, client: PetLibroClient,
                         pet=None, days: int = 60) -> list[dict]:
    try:
        feeders = config.resolve_feeders("all" if pet is None else [pet])
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]

    results = []
    for f in feeders:
        try:
            rc = await _compute_rhythm(client, f, days)
            results.append({
                "pet": f.name, "serial": f.serial, "ok": True,
                "days": days, "eating_visits": rc["eating_visits"],
                "daily_total_portions": rc["total"],
                "current_schedule": [{"time": t, "portions": g} for t, g in rc["current"]],
                "recommended_schedule": [{"time": t, "portions": g} for t, g in rc["target_rows"]],
            })
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


async def _apply_actions(client, serial: str, actions: dict) -> None:
    for pid in actions["removes"]:
        await client.remove_plan(serial, pid)
    for plan in actions["updates"]:
        await client.update_plan(serial, plan)
    for plan in actions["adds"]:
        await client.add_plan(serial, plan)


def _enabled_pairs(plans) -> list[tuple[str, int]]:
    return sorted((p.get("executionTime"), int(p.get("grainNum") or 0))
                  for p in plans if p.get("enable", True))


async def _rollback(client, serial: str, snapshot: list[dict]) -> bool:
    """Restore `snapshot` by wiping current enabled rows and re-adding the snapshot.
    Returns True iff the readback matches the snapshot. May raise if a call fails —
    the caller treats a raised exception as a failed rollback."""
    current = [p for p in await client.feeding_plans(serial) if p.get("enable", True)]
    for p in current:
        await client.remove_plan(serial, p["id"])
    for p in snapshot:
        await client.add_plan(serial, p)
    got = _enabled_pairs(await client.feeding_plans(serial))
    want = sorted((p.get("executionTime"), int(p.get("grainNum") or 0)) for p in snapshot)
    return got == want


async def _apply_with_rollback(client, serial: str, actions: dict,
                               snapshot: list[dict], target_rows) -> dict:
    """Apply the diff and verify; on ANY failure — a write exception OR a verify
    mismatch — attempt rollback to `snapshot`. Always surface the snapshot for
    manual restore if rollback cannot be confirmed."""
    want = sorted((t, g) for t, g in target_rows)
    apply_error = None
    got = None
    try:
        await _apply_actions(client, serial, actions)
        got = _enabled_pairs(await client.feeding_plans(serial))
    except Exception as exc:
        apply_error = f"{type(exc).__name__}: {exc}"
    else:
        if got == want:
            return {"ok": True, "applied": True,
                    "schedule": [{"time": t, "portions": g} for t, g in want]}
    try:
        rolled = await _rollback(client, serial, snapshot)
        rollback_error = None
    except Exception as exc:
        rolled = False
        rollback_error = f"{type(exc).__name__}: {exc}"
    base = (f"Write failed mid-apply ({apply_error})" if apply_error
            else f"Verify failed after apply (expected {want}, got {got})")
    if rolled:
        return {"ok": False, "error": f"{base}; rolled back to snapshot.",
                "expected": want, "got": got}
    tail = (f" rollback also raised ({rollback_error})" if rollback_error
            else " rollback did not restore snapshot")
    return {"ok": False, "error": f"{base};{tail} — restore manually from snapshot.",
            "expected": want, "got": got, "snapshot": snapshot}


async def apply_schedule(config: Config, client: PetLibroClient,
                         pet, days: int = 60, apply: bool = False) -> list[dict]:
    try:
        feeders = config.resolve_feeders(pet)
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]
    results = []
    for f in feeders:
        try:
            rc = await _compute_rhythm(client, f, days)
            enabled, target_rows, total = rc["enabled"], rc["target_rows"], rc["total"]
            target_total = sum(g for _, g in target_rows)
            if target_total > total:
                results.append({"pet": f.name, "serial": f.serial, "ok": False,
                    "error": (f"Refusing: target total {target_total} exceeds "
                              f"current {total} portions.")})
                continue
            actions = diff_schedule(enabled, target_rows)
            if not apply:
                results.append({
                    "pet": f.name, "serial": f.serial, "ok": True, "dry_run": True,
                    "would_update": actions["updates"], "would_add": actions["adds"],
                    "would_remove": actions["removes"],
                    "target_schedule": [{"time": t, "portions": g} for t, g in target_rows]})
                continue
            snapshot = [dict(p) for p in enabled]
            res = await _apply_with_rollback(client, f.serial, actions, snapshot, target_rows)
            results.append({"pet": f.name, "serial": f.serial, **res})
        except Exception as exc:  # pre-write failures (compute/diff) — device untouched
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
