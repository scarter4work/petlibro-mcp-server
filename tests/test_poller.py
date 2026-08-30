"""Poller tests. No network: the client is a stub."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from petlibro_mcp import poller, store


def _rec(rid, at_ms, fmt):
    return {"id": rid, "recordTime": at_ms, "formatRecordTime": fmt,
            "type": "GRAIN_OUTPUT_SUCCESS", "eventType": "FEEDING_PLAN_SUCCESS",
            "expectGrainNum": 1, "actualGrainNum": 1}


class StubClient:
    def __init__(self, records=None, plans=None, fail_for=None):
        self._records = records if records is not None else [_rec("a", 1000, "2026-08-30 07:00")]
        self._plans = plans if plans is not None else [
            {"executionTime": "07:00", "grainNum": 5, "enable": True}]
        self._fail_for = fail_for or set()
        self.days_asked = []

    async def work_record(self, serial, days=60, size=1000):
        if serial in self._fail_for:
            raise RuntimeError("cloud down")
        self.days_asked.append(days)
        return [{"workRecords": self._records}]

    async def feeding_plans(self, serial):
        return self._plans


def _config(*names):
    return SimpleNamespace(feeders=[
        SimpleNamespace(name=n, serial=f"SN-{n}") for n in names])


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    yield c
    c.close()


async def test_poll_once_ingests_and_reports_new_rows(conn):
    n = await poller.poll_once(conn, _config("ferris"), StubClient())
    assert n == 1
    assert len(store.events(conn, pet="ferris")) == 1


async def test_second_poll_adds_nothing_but_still_succeeds(conn):
    c = StubClient()
    await poller.poll_once(conn, _config("ferris"), c)
    assert await poller.poll_once(conn, _config("ferris"), c) == 0


async def test_first_poll_backfills_then_narrows_the_window(conn):
    """Backfill once, then only look back a few days — not 60 every cycle."""
    c = StubClient()
    await poller.poll_once(conn, _config("ferris"), c)
    await poller.poll_once(conn, _config("ferris"), c)
    assert c.days_asked == [poller.BACKFILL_DAYS, poller.LOOKBACK_DAYS]


async def test_one_failing_feeder_does_not_abandon_the_others(conn):
    """A jammed feeder or a flaky cloud call must not cost the other cats."""
    c = StubClient(fail_for={"SN-ferris"})
    n = await poller.poll_once(conn, _config("ferris", "ricco", "zeus"), c)
    assert n == 2
    assert store.events(conn, pet="ferris") == []
    assert len(store.events(conn, pet="ricco")) == 1
    assert len(store.events(conn, pet="zeus")) == 1


async def test_schedule_snapshot_captured(conn):
    await poller.poll_once(conn, _config("ferris"), StubClient())
    snaps = store.schedules(conn, "ferris")
    assert len(snaps) == 1
    assert '"portions": 5' in snaps[0]["rows_json"] or '"portions":5' in snaps[0]["rows_json"]


async def test_disabled_plan_rows_are_not_snapshotted(conn):
    c = StubClient(plans=[
        {"executionTime": "07:00", "grainNum": 5, "enable": True},
        {"executionTime": "22:00", "grainNum": 4, "enable": False},
    ])
    await poller.poll_once(conn, _config("ferris"), c)
    assert "22:00" not in store.schedules(conn, "ferris")[0]["rows_json"]


async def test_analysis_is_cached_when_the_hook_is_supplied(conn):
    async def analyse(pet):
        return {"pet": pet, "ok": True, "eating_visits": 42}
    await poller.poll_once(conn, _config("ferris"), StubClient(), analyse)
    hit = store.get_analysis(conn, "ferris", 60, max_age_s=3600)
    assert hit["eating_visits"] == 42


async def test_failed_analysis_is_not_cached(conn):
    async def analyse(pet):
        return {"pet": pet, "ok": False, "error": "nope"}
    await poller.poll_once(conn, _config("ferris"), StubClient(), analyse)
    assert store.get_analysis(conn, "ferris", 60, max_age_s=3600) is None


async def test_run_loop_records_a_poll_then_stops(conn):
    stop = asyncio.Event()

    async def analyse(pet):
        stop.set()
        return None

    await asyncio.wait_for(
        poller.run(conn, _config("ferris"), StubClient(), analyse,
                   interval_s=0.01, stop=stop),
        timeout=5)
    runs = store.recent_polls(conn)
    assert runs and runs[0]["ok"] == 1
