"""MCP tool logic: resolve, guard, call the facade, shape results."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from .config import Config, cups_to_portions, UnknownPetError
from .client import PetLibroClient
from .history import parse_events, parse_work_record
from .rhythm import circadian_curve, find_peaks, split_at_peaks
from .planner import plan_rows
from .schedule_diff import diff_schedule
from . import evidence as EV
from . import journal as J
from . import trim as TR


def _default_journal_path() -> Path:
    """Beside pets.toml, which is where .env already lives.

    ``PETLIBRO_JOURNAL`` overrides it, so a test or a deployment with a
    read-only install dir can point the journal somewhere writable.
    """
    import os
    override = os.environ.get("PETLIBRO_JOURNAL")
    if override:
        return Path(override)
    from .server import PETS_TOML
    return Path(PETS_TOML).parent / J.DEFAULT_JOURNAL_NAME


def _journal_write(journal_path, serial: str, rows, now_ms: int) -> str | None:
    """Record an applied schedule. Returns an error string instead of raising.

    The device write has already succeeded and been verified by the time this
    runs, so a journalling failure must not be reported as a failed apply — it
    degrades how well we can date the next evidence window, nothing more.
    """
    try:
        J.append(journal_path, serial, rows, sum(g for _, g in rows), now_ms)
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


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


def _food_low(info: dict) -> bool | None:
    """True when the hopper is running low.

    Prefer the explicit hopper enum ``warehouseSurplusGrain`` ("GOOD" = plenty;
    any other value = running out). Fall back to negating the top-level
    ``surplusGrain`` boolean if the enum is absent. Returns None if neither
    field is present, rather than guessing. NOTE: this used to map straight to
    ``surplusGrain`` (inverted — reported food_low=True when food was present).
    """
    warehouse = info.get("warehouseSurplusGrain")
    if warehouse is not None:
        return warehouse != "GOOD"
    surplus = info.get("surplusGrain")
    if surplus is not None:
        return not surplus
    return None


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
                "food_low": _food_low(info),
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


async def _compute_rhythm(client, feeder, days: int, now_ms: int,
                          journal_path) -> dict:
    """Rhythm analysis for a feeder: current schedule, evidence, trim, target rows.

    The daily total is NO LONGER simply the total already on the device. It is
    that total minus a jam-driven trim, which is what lets a recompute lower an
    over-aggressive schedule. The trim is withheld until the current schedule has
    had MIN_EVIDENCE_DAYS to prove itself, so repeated clicks cannot ratchet the
    total down before the last change has had a chance to show an effect.
    """
    raw = await client.work_record(feeder.serial, days=days)
    events = parse_events(raw)
    eats, _dispenses = parse_work_record(raw)

    plans = await client.feeding_plans(feeder.serial)
    enabled = [p for p in plans if p.get("enable", True)]
    current = [(p.get("executionTime"), int(p.get("grainNum") or 0)) for p in enabled]
    total = sum(g for _, g in current)

    change_at = EV.last_change_at(enabled, J.last_at_ms(journal_path, feeder.serial))
    if change_at is None:
        days_since = None
        since = events
        rate_before = None
    else:
        days_since = EV.window_days(change_at, now_ms)
        before, since = EV.split_events(events, change_at)
        history_start = min((e.at_ms for e in events), default=change_at)
        before_days = EV.window_days(history_start, change_at)
        rate_before = (TR.jam_rate(len([e for e in before if e.kind == "block"]),
                                   before_days)
                       if before_days > 0 else None)

    since_days = days_since if days_since else EV.window_days(
        min((e.at_ms for e in since), default=now_ms), now_ms)
    blocks_since = [e for e in since if e.kind == "block"]
    rate_since = TR.jam_rate(len(blocks_since), since_days)

    # Meal TIMES come from the rhythm; the trim then lands on the jammiest of them.
    tod = [(minute, max(dur, 1)) for minute, dur in eats]
    curve = circadian_curve(tod)
    split = split_at_peaks(curve, find_peaks(curve))
    blocks_by_time = TR.attribute_blocks(
        [e.minute for e in blocks_since],
        [f"{m // 60:02d}:{m % 60:02d}" for m, _ in split])

    allowed = EV.may_trim(days_since)
    want = TR.trim_amount(rate_since, total) if allowed else 0
    withheld = None if allowed else ("too_early" if days_since is not None
                                     else "no_known_change")

    target_rows = plan_rows(split, total)
    if want > 0:
        target_rows = TR.trim_schedule(target_rows, blocks_by_time, want)
    applied = total - sum(g for _, g in target_rows)

    delivered = sum(e.amount for e in since if e.kind == "dispense_plan")
    return {
        "enabled": enabled, "current": current, "total": total,
        "eating_visits": len(eats), "target_rows": target_rows,
        "trim": {"applied": applied, "jam_rate": rate_since,
                 "blocks_by_time": blocks_by_time, "withheld_reason": withheld},
        "evidence": {
            "days_since_change": days_since,
            "jam_rate_before": rate_before,
            "jam_rate_since": rate_since,
            "delivered_per_day_since": (delivered / since_days
                                        if since_days > 0 else 0.0),
            "scheduled_per_day": total,
            "grain_low_since": len([e for e in since if e.kind == "grain_low"]),
            "verdict": EV.verdict(rate_before, rate_since,
                                  days_since if days_since is not None else 0.0),
        },
    }


async def analyze_rhythm(config: Config, client: PetLibroClient,
                         pet=None, days: int = 60, now_ms: int | None = None,
                         journal_path=None) -> list[dict]:
    try:
        feeders = config.resolve_feeders("all" if pet is None else [pet])
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]

    now_ms = _now_ms() if now_ms is None else now_ms
    journal_path = _default_journal_path() if journal_path is None else journal_path

    results = []
    for f in feeders:
        try:
            rc = await _compute_rhythm(client, f, days, now_ms, journal_path)
            results.append({
                "pet": f.name, "serial": f.serial, "ok": True,
                "days": days, "eating_visits": rc["eating_visits"],
                "daily_total_portions": rc["total"],
                "current_schedule": [{"time": t, "portions": g} for t, g in rc["current"]],
                "recommended_schedule": [{"time": t, "portions": g} for t, g in rc["target_rows"]],
                "trim": rc["trim"],
                "evidence": rc["evidence"],
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


def _as_schedule(pairs) -> list[dict]:
    return [{"time": t, "portions": g} for t, g in pairs]


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
                    "schedule": _as_schedule(want),
                    "current_schedule": _as_schedule(want)}
    try:
        rolled = await _rollback(client, serial, snapshot)
        rollback_error = None
    except Exception as exc:
        rolled = False
        rollback_error = f"{type(exc).__name__}: {exc}"
    base = (f"Write failed mid-apply ({apply_error})" if apply_error
            else f"Verify failed after apply (expected {want}, got {got})")
    if rolled:
        # Rollback confirmed: the device holds the snapshot again, so that — not the
        # schedule we failed to write — is what the caller should be showing.
        return {"ok": False, "error": f"{base}; rolled back to snapshot.",
                "expected": want, "got": got,
                "current_schedule": _as_schedule(_enabled_pairs(snapshot))}
    tail = (f" rollback also raised ({rollback_error})" if rollback_error
            else " rollback did not restore snapshot")
    return {"ok": False, "error": f"{base};{tail} — restore manually from snapshot.",
            "expected": want, "got": got, "snapshot": snapshot}


async def _apply_one(client, serial: str, enabled: list[dict], total: int,
                     target_rows, apply: bool) -> dict:
    """Per-feeder apply core: total guard -> diff -> dry-run preview OR
    apply+verify+rollback. Returns a result dict without pet/serial."""
    # `enabled` was read from the device moments ago, so this is what it really holds —
    # the caller's own view of the schedule may be older than that.
    current_schedule = _as_schedule(_enabled_pairs(enabled))
    target_total = sum(g for _, g in target_rows)
    if target_total > total:
        return {"ok": False,
                "error": (f"Refusing: target total {target_total} exceeds "
                          f"current {total} portions."),
                "current_schedule": current_schedule}
    actions = diff_schedule(enabled, target_rows)
    if not apply:
        # `would_remove` stays a list of plan ids because that is what the write path
        # needs. A preview reading "row id 5534603 removed" tells a human nothing, and
        # the id->time map exists only here, so resolve it for display alongside.
        by_id = {p.get("id"): p.get("executionTime") for p in enabled}
        return {"ok": True, "dry_run": True,
                "would_update": actions["updates"], "would_add": actions["adds"],
                "would_remove": actions["removes"],
                "would_remove_times": [{"id": pid, "time": by_id.get(pid)}
                                       for pid in actions["removes"]],
                "current_schedule": current_schedule,
                "target_schedule": [{"time": t, "portions": g} for t, g in target_rows]}
    snapshot = [dict(p) for p in enabled]
    return await _apply_with_rollback(client, serial, actions, snapshot, target_rows)


async def apply_target_rows(config: Config, client: PetLibroClient, pet,
                            target_rows, apply: bool = False,
                            now_ms: int | None = None,
                            journal_path=None) -> list[dict]:
    """Apply an explicit (e.g. user-edited) target schedule to a feeder."""
    try:
        feeders = config.resolve_feeders(pet)
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]
    now_ms = _now_ms() if now_ms is None else now_ms
    journal_path = _default_journal_path() if journal_path is None else journal_path
    rows = list(target_rows)
    results = []
    for f in feeders:
        try:
            enabled = [p for p in await client.feeding_plans(f.serial) if p.get("enable", True)]
            total = sum(int(p.get("grainNum") or 0) for p in enabled)
            res = await _apply_one(client, f.serial, enabled, total, rows, apply)
            # Journal only a CONFIRMED write: a dry run changed nothing, and a
            # rolled-back failure left the old schedule in force.
            if apply and res.get("ok") and res.get("applied"):
                warn = _journal_write(journal_path, f.serial, rows, now_ms)
                if warn:
                    res = {**res, "journal_warning": warn}
            results.append({"pet": f.name, "serial": f.serial, **res})
        except Exception as exc:  # surface, never swallow
            results.append({"pet": f.name, "serial": f.serial, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    return results


async def apply_schedule(config: Config, client: PetLibroClient,
                         pet, days: int = 60, apply: bool = False,
                         now_ms: int | None = None,
                         journal_path=None) -> list[dict]:
    try:
        feeders = config.resolve_feeders(pet)
    except UnknownPetError as e:
        return [{"pet": None, "serial": None, "ok": False, "error": str(e)}]
    now_ms = _now_ms() if now_ms is None else now_ms
    journal_path = _default_journal_path() if journal_path is None else journal_path
    results = []
    for f in feeders:
        try:
            rc = await _compute_rhythm(client, f, days, now_ms, journal_path)
            res = await _apply_one(client, f.serial, rc["enabled"], rc["total"],
                                   rc["target_rows"], apply)
            if apply and res.get("ok") and res.get("applied"):
                warn = _journal_write(journal_path, f.serial,
                                      rc["target_rows"], now_ms)
                if warn:
                    res = {**res, "journal_warning": warn}
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
