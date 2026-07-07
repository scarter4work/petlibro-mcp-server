"""FastAPI app: rhythm-feeding dashboard over petlibro_mcp."""
from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from petlibro_mcp.config import load_config
from petlibro_mcp.client import PetLibroClient
from petlibro_mcp.server import _load_env_file, PETS_TOML
from webui import rhythm_api

_STATIC = Path(__file__).resolve().parent / "static"


def build_app(config, client) -> FastAPI:
    app = FastAPI(title="PetLibro Rhythm Feeding")

    @app.get("/api/pets")
    async def pets():
        return [{"name": f.name, "serial": f.serial} for f in config.feeders]

    @app.get("/api/pets/{name}/analysis")
    async def analysis(name: str, days: int = 60):
        return await rhythm_api.analysis(config, client, name, days)

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
    _load_env_file()
    config = load_config(PETS_TOML)
    return build_app(config, PetLibroClient(config))
