"""Unit tests for the SQLite event store. No network, no cloud."""
from __future__ import annotations

import json
import time

import pytest

from petlibro_mcp import store


def _rec(rid, at_ms, fmt, *, type_="GRAIN_OUTPUT_SUCCESS",
         event_type="FEEDING_PLAN_SUCCESS", expect=1, actual=1, content=""):
    return {"id": rid, "recordTime": at_ms, "formatRecordTime": fmt,
            "type": type_, "eventType": event_type,
            "expectGrainNum": expect, "actualGrainNum": actual, "content": content}


def _days(records):
    return [{"workRecords": records}]


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    yield c
    c.close()


# ---- schema ---------------------------------------------------------------


def test_connect_creates_schema_and_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    store.connect(p).close()
    c = store.connect(p)  # second call must not blow up on existing tables
    names = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "analysis_cache", "poll_runs", "schedule_snapshots"} <= names
    c.close()


# ---- ingestion ------------------------------------------------------------


def test_upsert_stores_one_row_per_event(conn):
    n = store.upsert_events(conn, "ferris", "SN1", _days([
        _rec("a", 1000, "2026-08-30 07:00"),
        _rec("b", 2000, "2026-08-30 08:00", type_="DETECTION_EVENT",
             event_type="PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
             content="ate for 01m30s"),
    ]))
    assert n == 2
    kinds = {r["kind"] for r in store.events(conn, pet="ferris")}
    assert kinds == {"dispense_plan", "eat"}


def test_reingesting_the_same_records_adds_nothing(conn):
    days = _days([_rec("a", 1000, "2026-08-30 07:00")])
    assert store.upsert_events(conn, "ferris", "SN1", days) == 1
    assert store.upsert_events(conn, "ferris", "SN1", days) == 0
    assert len(store.events(conn, pet="ferris")) == 1


def test_grain_low_dispense_yields_two_rows_from_one_record(conn):
    """One record, two events — the composite key must keep both."""
    store.upsert_events(conn, "ferris", "SN1", _days([
        _rec("a", 1000, "2026-08-30 07:00",
             event_type="FEEDING_PLAN_GRAIN_NOT_ENOUGH"),
    ]))
    kinds = sorted(r["kind"] for r in store.events(conn, pet="ferris"))
    assert kinds == ["dispense_plan", "grain_low"]


def test_unparseable_records_are_skipped_not_stored(conn):
    n = store.upsert_events(conn, "ferris", "SN1", _days([
        _rec("a", 1000, "no-timestamp-here"),
    ]))
    assert n == 0
    assert store.events(conn, pet="ferris") == []


def test_raw_json_is_retained_for_rederivation(conn):
    store.upsert_events(conn, "ferris", "SN1", _days([
        _rec("a", 1000, "2026-08-30 07:00", expect=5, actual=5),
    ]))
    row = store.events(conn, pet="ferris")[0]
    assert json.loads(row["raw"])["expectGrainNum"] == 5
    assert row["expect_grain"] == 5 and row["actual_grain"] == 5


def test_events_filter_by_pet_kind_and_time(conn):
    store.upsert_events(conn, "ferris", "SN1", _days([
        _rec("a", 1000, "2026-08-30 07:00"),
        _rec("b", 5000, "2026-08-30 09:00", type_="GRAIN_OUTLET_BLOCK_REMOVED",
             event_type="GRAIN_OUTLET_BLOCK_REMOVED"),
    ]))
    store.upsert_events(conn, "ricco", "SN2", _days([
        _rec("c", 1000, "2026-08-30 07:00"),
    ]))
    assert len(store.events(conn, pet="ferris")) == 2
    assert len(store.events(conn, pet="ricco")) == 1
    assert len(store.events(conn, pet="ferris", kind="block")) == 1
    assert len(store.events(conn, pet="ferris", since_ms=3000)) == 1


# ---- rebuilding the parser's view ----------------------------------------


def test_stored_events_rebuild_the_day_grouped_shape(conn):
    """The store must be able to hand analysis code the same shape the cloud
    returns, so nothing downstream has to know where the data came from."""
    records = [
        _rec("a", 1000, "2026-08-30 07:00"),
        _rec("b", 2000, "2026-08-30 08:00", type_="DETECTION_EVENT",
             event_type="PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
             content="ate for 02m00s"),
    ]
    store.upsert_events(conn, "ferris", "SN1", _days(records))
    rebuilt = store.work_record_shape(conn, "ferris")
    from petlibro_mcp.history import parse_events
    assert len(parse_events(rebuilt)) == len(parse_events(_days(records)))


# ---- analysis cache -------------------------------------------------------


def test_analysis_cache_round_trips(conn):
    store.put_analysis(conn, "ferris", 60, {"pet": "ferris", "ok": True})
    hit = store.get_analysis(conn, "ferris", 60, max_age_s=3600)
    assert hit["pet"] == "ferris"


def test_stale_analysis_is_a_miss(conn):
    store.put_analysis(conn, "ferris", 60, {"pet": "ferris"},
                       computed_at=int(time.time()) - 10_000)
    assert store.get_analysis(conn, "ferris", 60, max_age_s=60) is None


def test_analysis_cache_is_keyed_by_days_window(conn):
    store.put_analysis(conn, "ferris", 60, {"w": 60})
    assert store.get_analysis(conn, "ferris", 14, max_age_s=3600) is None


# ---- poll bookkeeping -----------------------------------------------------


def test_poll_run_records_success_and_failure(conn):
    store.record_poll(conn, ok=True, new_events=7, error=None)
    store.record_poll(conn, ok=False, new_events=0, error="boom")
    runs = store.recent_polls(conn, limit=5)
    assert runs[0]["error"] == "boom" and runs[0]["ok"] == 0
    assert runs[1]["new_events"] == 7


def test_last_event_ms_drives_incremental_fetch(conn):
    assert store.last_event_ms(conn, "ferris") is None
    store.upsert_events(conn, "ferris", "SN1", _days([
        _rec("a", 1000, "2026-08-30 07:00"),
        _rec("b", 9000, "2026-08-30 09:00"),
    ]))
    assert store.last_event_ms(conn, "ferris") == 9000


# ---- schedule snapshots ---------------------------------------------------


def test_schedule_snapshot_keeps_only_changes(conn):
    rows = [{"time": "07:00", "portions": 5}]
    assert store.put_schedule(conn, "ferris", rows) is True
    assert store.put_schedule(conn, "ferris", rows) is False  # unchanged
    assert store.put_schedule(conn, "ferris",
                              [{"time": "07:00", "portions": 2}]) is True
    assert len(store.schedules(conn, "ferris")) == 2
