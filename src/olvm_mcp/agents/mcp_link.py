"""Connect an agent to the olvm-mcp server as an MCP client."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client


class ToolCallError(Exception):
    """An MCP tool returned an error result."""


@asynccontextmanager
async def connect(remote_url: str | None = None, token: str | None = None,
                  ca_file: str | None = None) -> AsyncIterator[ClientSession]:
    """Open an MCP session.

    Without remote_url, start the server locally over stdio, always in read-only
    mode, whatever OLVM_MODE says. With remote_url, connect over Streamable HTTP
    with a bearer token; the remote server's own mode applies.
    """
    if remote_url:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx2.AsyncClient(headers=headers, verify=ca_file or True, timeout=120) as http:
            async with streamable_http_client(remote_url, http_client=http) as (read, write, *_):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        return

    env = {**os.environ, "OLVM_TRANSPORT": "stdio", "OLVM_MODE": "read_only", "OLVM_ALLOW_DESTRUCTIVE": ""}
    params = StdioServerParameters(command=sys.executable, args=["-m", "olvm_mcp.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def result_text(result: Any) -> str:
    """The text content of an MCP tool result."""
    return "".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")


async def call_json(session: ClientSession, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool and decode its JSON result; raise ToolCallError on a tool error."""
    result = await session.call_tool(name, arguments or {})
    text = result_text(result)
    if result.is_error:
        raise ToolCallError(f"{name}: {text or 'tool error'}")
    if text:
        return json.loads(text)
    return result.structured_content or {}
