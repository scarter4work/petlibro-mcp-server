"""Background poller: keeps the SQLite store current without a scheduler.

Runs as a plain asyncio task inside the existing uvicorn process. No
APScheduler, no cron, no second systemd unit — the web service is already the
one thing that runs continuously, and a sleep loop is the whole requirement.

Each cycle fetches a *lookback window* rather than only new records: the cloud
occasionally backfills, and re-ingesting is free because the record id is part
of the primary key.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import store

_LOGGER = logging.getLogger("petlibro_mcp.poller")

POLL_INTERVAL_S = 15 * 60
# First run reaches back this far; steady-state runs use LOOKBACK_DAYS.
BACKFILL_DAYS = 60
LOOKBACK_DAYS = 3
# Analysis is recomputed at most this often; the dashboard serves the cache.
ANALYSIS_MAX_AGE_S = POLL_INTERVAL_S * 2


async def poll_once(conn, config, client, analyse=None) -> int:
    """One sweep across every feeder. Returns the number of new event rows.

    Failure for one pet must not abandon the rest — a single jammed feeder or a
    transient cloud error should never stop the other five being recorded.
    """
    total = 0
    for feeder in config.feeders:
        try:
            first_time = store.last_event_ms(conn, feeder.name) is None
            days = BACKFILL_DAYS if first_time else LOOKBACK_DAYS
            raw = await client.work_record(feeder.serial, days=days)
            total += await asyncio.to_thread(
                store.upsert_events, conn, feeder.name, feeder.serial, raw)

            plans = await client.feeding_plans(feeder.serial)
            rows = sorted(
                ({"time": p.get("executionTime"),
                  "portions": int(p.get("grainNum") or 0)}
                 for p in plans or [] if p.get("enable", True)),
                key=lambda r: str(r["time"]))
            await asyncio.to_thread(store.put_schedule, conn, feeder.name, rows)

            if analyse is not None:
                payload = await analyse(feeder.name)
                if payload and payload.get("ok"):
                    await asyncio.to_thread(
                        store.put_analysis, conn, feeder.name, 60, payload)
        except Exception:  # noqa: BLE001 - one bad feeder must not stop the sweep
            _LOGGER.exception("poll failed for %s", feeder.name)
    return total


async def run(conn, config, client, analyse=None,
              interval_s: int = POLL_INTERVAL_S,
              stop: asyncio.Event | None = None) -> None:
    """Poll forever. Cancelled cleanly at shutdown."""
    while True:
        started = time.time()
        try:
            new = await poll_once(conn, config, client, analyse)
            await asyncio.to_thread(store.record_poll, conn, True, new, None)
            _LOGGER.info("poll ok: %d new events in %.1fs", new, time.time() - started)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _LOGGER.exception("poll cycle failed")
            await asyncio.to_thread(store.record_poll, conn, False, 0, str(e))
        if stop is not None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
                return
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(interval_s)
