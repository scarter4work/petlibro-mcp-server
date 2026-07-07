from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from petlibro_mcp.config import Config, Feeder
from webui.app import build_app


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
    assert r.json() == [{"name": "ferris", "serial": "SN-F"}]


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
