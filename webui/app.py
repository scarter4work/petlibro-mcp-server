"""FastAPI app: rhythm-feeding dashboard over petlibro_mcp."""
from __future__ import annotations
import asyncio
import contextlib
import logging
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp.server import _load_env_file, PETS_TOML
from petlibro_mcp import poller, store
from webui import rhythm_api

_STATIC = Path(__file__).resolve().parent / "static"
_LOGGER = logging.getLogger("webui")


def configure_logging(level: str | None = None) -> logging.Logger:
    """Send our own loggers to stderr, which is where journald reads.

    uvicorn configures only its own loggers and leaves the root logger bare, so
    without this every _LOGGER call in the poller vanishes — the service can be
    failing every 15 minutes with an empty `journalctl`. Attaching to the
    package logger rather than root leaves uvicorn's access-log formatting
    alone.

    Idempotent: repeated calls (reload, tests) must not stack handlers and
    duplicate every line.
    """
    lvl = (level or os.environ.get("PETLIBRO_LOG_LEVEL") or "INFO").upper()
    for name in ("petlibro_mcp", "webui"):
        log = logging.getLogger(name)
        log.setLevel(lvl)
        if not any(getattr(h, "_petlibro", False) for h in log.handlers):
            handler = logging.StreamHandler()  # stderr -> journald
            handler.setFormatter(logging.Formatter(
                "%(levelname)s %(name)s: %(message)s"))
            handler._petlibro = True  # type: ignore[attr-defined]
            log.addHandler(handler)
        log.propagate = False
    return logging.getLogger("petlibro_mcp")


def db_path() -> Path:
    """Where the event store lives.

    Defaults beside the deployment because the systemd unit runs with
    ProtectSystem=strict and ReadWritePaths=/opt/petlibro-webui — nowhere else
    is writable.
    """
    override = os.environ.get("PETLIBRO_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / store.DEFAULT_DB_NAME


def build_app(config, client, conn=None, poll: bool = False) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if poll and conn is not None:
            async def analyse(pet):
                return await rhythm_api.analysis(config, client, pet, 60)
            task = asyncio.create_task(poller.run(conn, config, client, analyse))
            _LOGGER.info("poller started (every %ds)", poller.POLL_INTERVAL_S)
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="PetLibro Rhythm Feeding", lifespan=lifespan)
    app.state.store = conn

    @app.get("/api/pets")
    async def pets():
        return [{"name": f.name, "serial": f.serial,
                 "max_portions": rhythm_api.max_portions(config, f)}
                for f in config.feeders]

    @app.post("/api/pets/{name}/feed")
    async def feed(name: str, body: dict):
        # No force from the wire: the cap is the only guard between a stray request
        # and an emptied hopper, so the web path must never be able to switch it off.
        return await rhythm_api.feed(config, client, name,
                                     int(body.get("portions", 0)))

    @app.get("/api/pets/{name}/analysis")
    async def analysis(name: str, days: int = 60, fresh: bool = False):
        """Cached by default; `?fresh=true` forces a recompute.

        The uncached path takes ~20s per cat because it re-fetches the whole
        history from the cloud — long enough that the dashboard's reanalyze
        button was intermittently timing out. Serving the poller's cache makes
        the normal case immediate.
        """
        conn = app.state.store
        if conn is not None and not fresh:
            hit = store.get_analysis(conn, name, days,
                                     max_age_s=poller.ANALYSIS_MAX_AGE_S)
            if hit is not None:
                return {**hit, "cached": True}
        out = await rhythm_api.analysis(config, client, name, days)
        if conn is not None and out.get("ok"):
            store.put_analysis(conn, name, days, out)
        return {**out, "cached": False}

    @app.get("/api/health")
    async def health():
        """Poller liveness — is the store actually being kept up to date?"""
        conn = app.state.store
        if conn is None:
            return {"store": False, "polls": []}
        runs = [dict(r) for r in store.recent_polls(conn, limit=5)]
        counts = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")}
        return {"store": True, "events": counts, "polls": runs}

    @app.post("/api/pets/{name}/apply")
    async def apply(name: str, body: dict):
        return await rhythm_api.apply(config, client, name,
                                      body.get("schedule", []), bool(body.get("apply", False)))

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "index.html")

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    return app


def create_app() -> FastAPI:
    configure_logging()
    _load_env_file()
    config = load_config(PETS_TOML)
    return build_app(config, PetLibroClient(config),
                     conn=store.connect(db_path()), poll=True)
