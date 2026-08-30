"""SQLite event store: local, durable history for the feeders.

Why this exists
---------------
Every analysis used to re-fetch 60 days of workRecords from the PetLibro cloud
and recompute from scratch — roughly 20 seconds per cat, synchronously, which
is why the dashboard's reanalyze intermittently timed out. It also meant the
history was only ever as long as the cloud's retention window.

Storing the records locally fixes both: analysis reads from disk in
milliseconds, and history accumulates past whatever the cloud keeps.

The raw record JSON is retained deliberately. When the derivation rules change
— and they do; `hopper_warned_on_last_dispense` should arguably ignore manual
feeds — we re-derive from stored rows instead of re-fetching months of history.

Plain stdlib sqlite3, not aiosqlite: the write volume is a few hundred rows a
day, so callers hop to a thread with `asyncio.to_thread` rather than take on a
dependency.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .history import classify, iter_records

# The systemd unit runs with ProtectSystem=strict and
# ReadWritePaths=/opt/petlibro-webui, so the database has to live inside the
# deployment directory. It is also excluded from deploy.sh's --delete rsync;
# without that exclusion every deploy would wipe the accumulated history.
DEFAULT_DB_NAME = "petlibro.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id            TEXT    NOT NULL,
  kind          TEXT    NOT NULL,
  pet           TEXT    NOT NULL,
  device_sn     TEXT    NOT NULL,
  at_ms         INTEGER NOT NULL,
  minute        INTEGER NOT NULL,
  amount        INTEGER NOT NULL DEFAULT 0,
  type          TEXT,
  event_type    TEXT,
  expect_grain  INTEGER,
  actual_grain  INTEGER,
  raw           TEXT    NOT NULL,
  -- One record can yield two events (a dispense that also warns the hopper is
  -- low), so the record id alone is not unique. device_sn is in the key too:
  -- PetLibro ids look globally unique, but staking silent data loss on another
  -- system's id space is not worth the one extra column.
  PRIMARY KEY (device_sn, id, kind)
);
CREATE INDEX IF NOT EXISTS ix_events_pet_at ON events(pet, at_ms);
CREATE INDEX IF NOT EXISTS ix_events_kind_at ON events(kind, at_ms);

CREATE TABLE IF NOT EXISTS analysis_cache (
  pet         TEXT    NOT NULL,
  days        INTEGER NOT NULL,
  computed_at INTEGER NOT NULL,
  json        TEXT    NOT NULL,
  PRIMARY KEY (pet, days)
);

CREATE TABLE IF NOT EXISTS poll_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  INTEGER NOT NULL,
  ok          INTEGER NOT NULL,
  new_events  INTEGER NOT NULL DEFAULT 0,
  error       TEXT
);

-- What the feeding plan WAS at a point in time. Without this, a scheduled feed
-- that produced no record at all is invisible: delivery is only ever measured
-- from records that exist, so a dispense that never fired cannot be counted.
CREATE TABLE IF NOT EXISTS schedule_snapshots (
  -- Surrogate key, not (pet, at_ms): two snapshots can land in the same
  -- millisecond, and a natural key on the timestamp silently overwrites the
  -- earlier one -- losing exactly the change we are here to record.
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  pet       TEXT    NOT NULL,
  at_ms     INTEGER NOT NULL,
  rows_json TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sched_pet_at ON schedule_snapshots(pet, id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the store at `path`. Safe to call repeatedly."""
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ---- ingestion ------------------------------------------------------------


def upsert_events(conn: sqlite3.Connection, pet: str, device_sn: str,
                  days: list[dict] | None) -> int:
    """Store every classifiable record. Returns the count of genuinely new rows.

    Idempotent: (device_sn, record id, kind) is the primary key, so re-polling
    an overlapping window is free.
    """
    rows: list[tuple] = []
    for w in iter_records(days):
        rid = w.get("id")
        if not rid:
            continue
        raw = json.dumps(w, separators=(",", ":"), ensure_ascii=False)
        for ev in classify(w):
            rows.append((
                rid, ev.kind, pet, device_sn, ev.at_ms, ev.minute, ev.amount,
                w.get("type"), w.get("eventType"),
                _int_or_none(w.get("expectGrainNum")),
                _int_or_none(w.get("actualGrainNum")),
                raw,
            ))
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO events "
        "(id, kind, pet, device_sn, at_ms, minute, amount, type, event_type,"
        " expect_grain, actual_grain, raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn.total_changes - before


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---- reads ----------------------------------------------------------------


def events(conn: sqlite3.Connection, pet: str | None = None,
           kind: str | None = None, since_ms: int | None = None,
           limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM events WHERE 1=1"
    args: list[Any] = []
    if pet is not None:
        sql += " AND pet = ?"
        args.append(pet)
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    if since_ms is not None:
        sql += " AND at_ms >= ?"
        args.append(since_ms)
    sql += " ORDER BY at_ms DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return list(conn.execute(sql, args))


def last_event_ms(conn: sqlite3.Connection, pet: str) -> int | None:
    """Newest stored event for `pet`, so polls can fetch only what is new."""
    row = conn.execute(
        "SELECT MAX(at_ms) AS m FROM events WHERE pet = ?", (pet,)).fetchone()
    return row["m"] if row and row["m"] is not None else None


def work_record_shape(conn: sqlite3.Connection, pet: str,
                      since_ms: int | None = None) -> list[dict]:
    """Stored rows in the cloud's own day-grouped shape.

    Lets every existing analysis path — which all speak workRecords — read from
    the store without knowing it exists. Deduplicated back to one entry per
    source record, since a record may have produced two event rows.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for r in events(conn, pet=pet, since_ms=since_ms):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(json.loads(r["raw"]))
    return [{"workRecords": out}]


# ---- analysis cache -------------------------------------------------------


def put_analysis(conn: sqlite3.Connection, pet: str, days: int,
                 payload: dict, computed_at: int | None = None) -> None:
    conn.execute(
        "INSERT INTO analysis_cache (pet, days, computed_at, json) "
        "VALUES (?,?,?,?) ON CONFLICT(pet, days) DO UPDATE SET "
        "computed_at = excluded.computed_at, json = excluded.json",
        (pet, days, int(computed_at if computed_at is not None else time.time()),
         json.dumps(payload)))
    conn.commit()


def get_analysis(conn: sqlite3.Connection, pet: str, days: int,
                 max_age_s: int) -> dict | None:
    """Cached analysis, or None when absent or older than `max_age_s`."""
    row = conn.execute(
        "SELECT computed_at, json FROM analysis_cache WHERE pet = ? AND days = ?",
        (pet, days)).fetchone()
    if row is None:
        return None
    if time.time() - row["computed_at"] > max_age_s:
        return None
    return json.loads(row["json"])


# ---- poll bookkeeping -----------------------------------------------------


def record_poll(conn: sqlite3.Connection, ok: bool, new_events: int,
                error: str | None) -> None:
    conn.execute(
        "INSERT INTO poll_runs (started_at, ok, new_events, error) VALUES (?,?,?,?)",
        (int(time.time()), 1 if ok else 0, int(new_events), error))
    conn.commit()


def recent_polls(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM poll_runs ORDER BY id DESC LIMIT ?", (limit,)))


# ---- schedule snapshots ---------------------------------------------------


def put_schedule(conn: sqlite3.Connection, pet: str,
                 rows: Iterable[dict]) -> bool:
    """Record the plan if it differs from the last snapshot. True when stored.

    Only changes are kept — an unchanged plan polled every 15 minutes would
    otherwise bury the handful of rows that actually mark a change.
    """
    payload = json.dumps(list(rows), sort_keys=True)
    prev = conn.execute(
        "SELECT rows_json FROM schedule_snapshots WHERE pet = ? "
        "ORDER BY id DESC LIMIT 1", (pet,)).fetchone()
    if prev is not None and prev["rows_json"] == payload:
        return False
    conn.execute(
        "INSERT INTO schedule_snapshots (pet, at_ms, rows_json) "
        "VALUES (?,?,?)", (pet, int(time.time() * 1000), payload))
    conn.commit()
    return True


def schedules(conn: sqlite3.Connection, pet: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM schedule_snapshots WHERE pet = ? ORDER BY id DESC",
        (pet,)))
