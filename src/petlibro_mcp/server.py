"""MCP server exposing PetLibro feeder/fountain tools over stdio."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from . import tools as T
from .client import PetLibroClient
from .config import load_config

PETS_TOML = os.environ.get(
    "PETLIBRO_PETS_TOML",
    str(Path(__file__).resolve().parent.parent.parent / "pets.toml"),
)

TOOL_DEFS = [
    Tool(
        name="feed",
        description=("Dispense food. 'pets' is a list of pet names or \"all\". "
                     "'cups' is cups of food; refused above the overfeed cap "
                     "unless force=true."),
        inputSchema={
            "type": "object",
            "properties": {
                "pets": {"oneOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "string"}]},
                "cups": {"type": "number"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["pets", "cups"],
        },
    ),
    Tool(
        name="open_lid",
        description="Force the RFID lid open. 'pets' is a list of names or \"all\".",
        inputSchema={
            "type": "object",
            "properties": {"pets": {"oneOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "string"}]}},
            "required": ["pets"],
        },
    ),
    Tool(
        name="feeder_status",
        description="Feeder status (food/battery/online/today). Omit 'pet' for all.",
        inputSchema={"type": "object",
                     "properties": {"pet": {"type": "string"}}},
    ),
    Tool(
        name="fountain_status",
        description="Fountain status. Omit 'name' for all fountains.",
        inputSchema={"type": "object",
                     "properties": {"name": {"type": "string"}}},
    ),
    Tool(
        name="list_devices",
        description="List configured feeders/fountains plus live cloud devices.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="analyze_rhythm",
        description=("Recommend-only: compute each cat's natural eating rhythm "
                     "from ~60d of history and report the current vs. a "
                     "rhythm-timed schedule (same daily total). No writes. "
                     "Omit 'pet' for all."),
        inputSchema={
            "type": "object",
            "properties": {
                "pet": {"type": "string"},
                "days": {"type": "integer", "default": 60},
            },
        },
    ),
]


def build_server(config, client: PetLibroClient) -> Server:
    server = Server("petlibro-mcp")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOL_DEFS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        a = arguments or {}
        if name == "feed":
            result = await T.feed(config, client, a["pets"], a["cups"],
                                  a.get("force", False))
        elif name == "open_lid":
            result = await T.open_lid(config, client, a["pets"])
        elif name == "feeder_status":
            result = await T.feeder_status(config, client, a.get("pet"))
        elif name == "fountain_status":
            result = await T.fountain_status(config, client, a.get("name"))
        elif name == "list_devices":
            result = await T.list_devices(config, client)
        elif name == "analyze_rhythm":
            result = await T.analyze_rhythm(config, client, a.get("pet"),
                                            a.get("days", 60))
        else:
            raise ValueError(f"Unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    return server


def _load_env_file() -> None:
    """Load KEY=VALUE lines from a .env file into the environment if present.

    Real environment variables win (``setdefault``), so an explicit env
    override always beats the file. Path is ``PETLIBRO_ENV`` if set, else
    ``.env`` beside pets.toml. This lets the MCP server read credentials from
    the git-ignored .env instead of duplicating the password into a launcher
    config.
    """
    env_path = Path(os.environ.get("PETLIBRO_ENV", Path(PETS_TOML).parent / ".env"))
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


async def _run() -> None:
    config = load_config(PETS_TOML)
    client = PetLibroClient(config)
    server = build_server(config, client)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    _load_env_file()
    asyncio.run(_run())
