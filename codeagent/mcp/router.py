"""Connect external MCP servers to the existing synchronous tool registry."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx2
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from codeagent.mcp.config import McpServerConfig, load_mcp_servers
from codeagent.tools import ToolDefinition, ToolRegistry


class McpTool:
    """Adapter from one remote MCP tool to CodeAgent's Tool protocol."""

    def __init__(self, router: "McpRouter", server: str, remote_tool: Any) -> None:
        self.router = router
        self.server = server
        self.remote_name = remote_tool.name
        self.definition = ToolDefinition(
            name=_public_tool_name(server, remote_tool.name),
            description=remote_tool.description or f"MCP tool from {server}",
            input_schema=remote_tool.input_schema,
        )

    def run(self, **kwargs: Any) -> str:
        return self.router.call_tool(self.server, self.remote_name, kwargs)


class McpRouter:
    """Own MCP connections and expose their tools through ``ToolRegistry``."""

    def __init__(self, config_path: str | Path = "mcp.json") -> None:
        self.servers = load_mcp_servers(config_path)
        self._clients: dict[str, Client] = {}
        self._tools: list[McpTool] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None

    def start(self) -> None:
        if not self.servers or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name="codeagent-mcp",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise RuntimeError(
                f"MCP startup failed: {self._startup_error}"
            ) from self._startup_error

    def register_tools(self, registry: ToolRegistry) -> None:
        self.start()
        for tool in self._tools:
            registry.register(tool)

    def list_tools(self) -> list[str]:
        self.start()
        return [tool.definition.name for tool in self._tools]

    def call_tool(self, server: str, name: str, arguments: dict[str, Any]) -> str:
        if self._loop is None:
            raise RuntimeError("MCP router is not running")
        future = asyncio.run_coroutine_threadsafe(
            self._clients[server].call_tool(name, arguments),
            self._loop,
        )
        result = future.result()
        output = _tool_result_text(result)
        return f"Error: {output}" if result.is_error else output

    def close(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join()
        self._thread = None
        self._loop = None

    def __enter__(self) -> "McpRouter":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        async with AsyncExitStack() as stack:
            for server in self.servers:
                client = await stack.enter_async_context(
                    Client(await self._transport(server, stack))
                )
                self._clients[server.name] = client
                cursor = None
                while True:
                    listing = await client.list_tools(cursor=cursor)
                    self._tools.extend(
                        McpTool(self, server.name, remote_tool)
                        for remote_tool in listing.tools
                    )
                    cursor = listing.next_cursor
                    if cursor is None:
                        break
            self._ready.set()
            await self._stop_event.wait()

    async def _transport(self, server: McpServerConfig, stack: AsyncExitStack):
        if server.transport == "stdio":
            params = StdioServerParameters(
                command=_expand(server.command),
                args=[_expand(item) for item in server.args],
                env={key: _expand(value) for key, value in server.env.items()},
                cwd=server.cwd,
            )
            return stdio_client(params)
        if server.transport in {"http", "streamable-http"}:
            if not server.headers:
                return _expand(server.url)
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={
                        key: _expand(value)
                        for key, value in server.headers.items()
                    }
                )
            )
            return streamable_http_client(
                _expand(server.url), http_client=http_client
            )
        raise ValueError(f"Unsupported MCP transport: {server.transport}")


def _public_tool_name(server: str, tool: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool}")
    return safe[:64]


def _expand(value: str) -> str:
    return os.path.expandvars(value)


def _tool_result_text(result: Any) -> str:
    parts: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(json.dumps(item.model_dump(mode="json"), ensure_ascii=False))
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(parts)
