import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from petlibro_mcp.config import Config, Feeder
from webui.app import build_app


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    """Keep the applied-schedule journal out of the repo (see test_tools.py)."""
    monkeypatch.setenv("PETLIBRO_JOURNAL", str(tmp_path / "journal.jsonl"))




def _cfg():
    return Config(feeders=[Feeder("ferris", "SN-F", "", "", 12)], fountains=[],
                  region="US", max_cups_per_command=4, email="a@b.com", password="pw")


def _client():
    m = AsyncMock()
    m.work_record = AsyncMock(return_value=[{"workRecords": [
        {"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
         "formatRecordTime": "2026-07-07 08:00", "params": '{"seconds":"02m00s"}'}]}])
    m.feeding_plans = AsyncMock(return_value=[
        {"id": 1, "executionTime": "08:00", "grainNum": 3, "enable": True}])
    return m


def test_pets_endpoint():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.get("/api/pets")
    assert r.status_code == 200
    assert r.json() == [{"name": "ferris", "serial": "SN-F", "max_portions": 48}]


def test_pets_endpoint_exposes_feed_cap():
    """max_portions lets the UI bound its spinner by the same guard the server enforces."""
    c = TestClient(build_app(_cfg(), _client()))
    assert c.get("/api/pets").json()[0]["max_portions"] == 48


def test_feed_endpoint():
    client = _client()
    c = TestClient(build_app(_cfg(), client))
    r = c.post("/api/pets/ferris/feed", json={"portions": 6})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["portions"] == 6
    client.feed.assert_awaited_once_with("SN-F", 6)


def test_feed_endpoint_refuses_over_cap():
    client = _client()
    c = TestClient(build_app(_cfg(), client))
    r = c.post("/api/pets/ferris/feed", json={"portions": 400})
    assert r.status_code == 200 and r.json()["ok"] is False
    client.feed.assert_not_awaited()


def test_feed_endpoint_ignores_client_supplied_force():
    """The cap is the only thing standing between a stray request and a whole hopper.
    The web endpoint must not let the caller switch it off."""
    client = _client()
    c = TestClient(build_app(_cfg(), client))
    r = c.post("/api/pets/ferris/feed", json={"portions": 400, "force": True})
    assert r.status_code == 200 and r.json()["ok"] is False
    client.feed.assert_not_awaited()


def test_analysis_endpoint():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.get("/api/pets/ferris/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and len(body["rhythm_curve"]) == 48


def test_apply_dry_run_endpoint():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.post("/api/pets/ferris/apply",
               json={"schedule": [{"time": "08:00", "portions": 3}], "apply": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["dry_run"] is True


def test_index_served():
    c = TestClient(build_app(_cfg(), _client()))
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_analysis_endpoint_includes_trim_and_evidence():
    c = TestClient(build_app(_cfg(), _client()))
    body = c.get("/api/pets/ferris/analysis").json()
    assert body["ok"] is True
    assert "trim" in body and "evidence" in body
