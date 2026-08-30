"""The dashboard's store-backed behaviour: caching, ?fresh, and /api/health."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from petlibro_mcp import store
from webui import app as webapp


class CountingClient:
    """Stands in for the cloud and counts how often the slow path is taken."""

    def __init__(self):
        self.work_record_calls = 0

    async def work_record(self, serial, days=60, size=1000):
        self.work_record_calls += 1
        return [{"workRecords": []}]

    async def feeding_plans(self, serial):
        return []


def _config():
    return SimpleNamespace(
        feeders=[SimpleNamespace(name="ferris", serial="SN-ferris",
                                 portions_per_cup=1)],
        max_cups_per_command=4)


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "t.db")
    yield c
    c.close()


def _client(conn, monkeypatch, analysis_payload=None):
    payload = analysis_payload or {"pet": "ferris", "ok": True, "eating_visits": 1}

    async def fake_analysis(config, client, pet, days=60):
        client.work_record_calls += 1
        return dict(payload)

    monkeypatch.setattr(webapp.rhythm_api, "analysis", fake_analysis)
    cloud = CountingClient()
    app = webapp.build_app(_config(), cloud, conn=conn, poll=False)
    return TestClient(app), cloud


def test_analysis_is_computed_then_served_from_cache(conn, monkeypatch):
    tc, cloud = _client(conn, monkeypatch)
    first = tc.get("/api/pets/ferris/analysis").json()
    assert first["cached"] is False
    assert cloud.work_record_calls == 1

    second = tc.get("/api/pets/ferris/analysis").json()
    assert second["cached"] is True
    assert second["eating_visits"] == 1
    # The whole point: the second request must not touch the cloud again.
    assert cloud.work_record_calls == 1


def test_fresh_query_param_forces_a_recompute(conn, monkeypatch):
    tc, cloud = _client(conn, monkeypatch)
    tc.get("/api/pets/ferris/analysis")
    out = tc.get("/api/pets/ferris/analysis?fresh=true").json()
    assert out["cached"] is False
    assert cloud.work_record_calls == 2


def test_failed_analysis_is_not_cached(conn, monkeypatch):
    tc, cloud = _client(conn, monkeypatch,
                        {"pet": "ferris", "ok": False, "error": "nope"})
    tc.get("/api/pets/ferris/analysis")
    tc.get("/api/pets/ferris/analysis")
    assert cloud.work_record_calls == 2  # never cached, so always recomputed


def test_analysis_still_works_without_a_store(monkeypatch):
    """The MCP/dev path builds the app with no DB; it must not break."""
    tc, _ = _client(None, monkeypatch)
    out = tc.get("/api/pets/ferris/analysis").json()
    assert out["ok"] is True and out["cached"] is False


def test_health_reports_event_counts_and_poll_history(conn, monkeypatch):
    store.upsert_events(conn, "ferris", "SN-ferris", [{"workRecords": [
        {"id": "a", "recordTime": 1000, "formatRecordTime": "2026-08-30 07:00",
         "type": "GRAIN_OUTPUT_SUCCESS", "eventType": "FEEDING_PLAN_SUCCESS",
         "expectGrainNum": 1, "actualGrainNum": 1},
    ]}])
    store.record_poll(conn, ok=True, new_events=1, error=None)
    tc, _ = _client(conn, monkeypatch)
    out = tc.get("/api/health").json()
    assert out["store"] is True
    assert out["events"]["dispense_plan"] == 1
    assert out["polls"][0]["new_events"] == 1


def test_health_is_honest_when_there_is_no_store(monkeypatch):
    tc, _ = _client(None, monkeypatch)
    assert tc.get("/api/health").json() == {"store": False, "polls": []}
