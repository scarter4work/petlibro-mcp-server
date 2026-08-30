"""Logging config: our loggers must reach stderr, where journald reads.

uvicorn configures only its own loggers and leaves root bare, so without this
the poller can fail every 15 minutes with nothing in `journalctl`.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from petlibro_mcp import poller, store
from webui import app as webapp


@pytest.fixture(autouse=True)
def _clean_handlers():
    """Leave the package loggers as we found them."""
    saved = {n: (list(logging.getLogger(n).handlers),
                 logging.getLogger(n).level,
                 logging.getLogger(n).propagate)
             for n in ("petlibro_mcp", "webui")}
    yield
    for n, (handlers, level, prop) in saved.items():
        log = logging.getLogger(n)
        log.handlers = handlers
        log.setLevel(level)
        log.propagate = prop


def test_configure_logging_attaches_a_handler():
    webapp.configure_logging("INFO")
    log = logging.getLogger("petlibro_mcp")
    assert any(getattr(h, "_petlibro", False) for h in log.handlers)
    assert log.level == logging.INFO


def test_configure_logging_is_idempotent():
    """Reload or repeated startup must not duplicate every log line."""
    webapp.configure_logging("INFO")
    webapp.configure_logging("INFO")
    webapp.configure_logging("INFO")
    log = logging.getLogger("petlibro_mcp")
    ours = [h for h in log.handlers if getattr(h, "_petlibro", False)]
    assert len(ours) == 1


def test_level_is_overridable_by_env(monkeypatch):
    monkeypatch.setenv("PETLIBRO_LOG_LEVEL", "warning")
    webapp.configure_logging()
    assert logging.getLogger("petlibro_mcp").level == logging.WARNING


def test_configure_logging_does_not_touch_uvicorn_loggers():
    before = list(logging.getLogger("uvicorn.access").handlers)
    webapp.configure_logging("INFO")
    assert logging.getLogger("uvicorn.access").handlers == before


# ---- the case the logging exists for -------------------------------------


class FailingClient:
    async def work_record(self, serial, days=60, size=1000):
        raise RuntimeError("cloud down")

    async def feeding_plans(self, serial):
        return []


async def test_a_failing_feeder_is_logged_with_its_name(tmp_path, caplog):
    """A silent failure is the thing we are protecting against: the log must
    name which cat's feeder failed, not just that something did."""
    conn = store.connect(tmp_path / "t.db")
    config = SimpleNamespace(feeders=[SimpleNamespace(name="ferris",
                                                      serial="SN-ferris")])
    with caplog.at_level(logging.ERROR, logger="petlibro_mcp.poller"):
        await poller.poll_once(conn, config, FailingClient())
    assert any("ferris" in r.getMessage() for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)  # traceback preserved
    conn.close()
