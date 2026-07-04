"""Tests that the MCP server registers all five PetLibro tools."""
from __future__ import annotations

from unittest.mock import AsyncMock

from mcp import types

from petlibro_mcp.server import build_server
from petlibro_mcp.config import Config


def cfg():
    return Config(feeders=[], fountains=[], region="US",
                  max_cups_per_command=4, email="a@b.com", password="pw")


async def test_lists_five_tools():
    server = build_server(cfg(), AsyncMock())

    # sanity: server built and has request handlers registered
    assert server.request_handlers

    # mcp 1.28.1: @server.list_tools() registers a handler under
    # request_handlers[types.ListToolsRequest] that takes a
    # ListToolsRequest and returns a ServerResult wrapping a
    # ListToolsResult (accessible via .root.tools).
    handler = server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest())
    tools = result.root.tools

    names = {t.name for t in tools}
    assert names == {"feed", "open_lid", "feeder_status",
                      "fountain_status", "list_devices"}
