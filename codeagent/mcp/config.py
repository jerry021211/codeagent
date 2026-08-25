"""Small Claude Code-compatible MCP configuration loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def load_mcp_servers(path: str | Path) -> list[McpServerConfig]:
    """Read ``mcpServers`` from a JSON file; a missing file means MCP is off."""

    config_path = Path(path)
    if not config_path.is_file():
        return []

    payload = load_mcp_document(config_path)
    servers: list[McpServerConfig] = []
    for name, raw in payload.get("mcpServers", {}).items():
        data: dict[str, Any] = dict(raw)
        transport = str(data.get("type") or ("http" if data.get("url") else "stdio"))
        cwd = Path(data["cwd"]) if data.get("cwd") else None
        if cwd is not None and not cwd.is_absolute():
            cwd = config_path.parent / cwd
        servers.append(
            McpServerConfig(
                name=name,
                transport=transport,
                command=str(data.get("command", "")),
                args=tuple(str(item) for item in data.get("args", [])),
                env={str(key): str(value) for key, value in data.get("env", {}).items()},
                cwd=cwd,
                url=str(data.get("url", "")),
                headers={
                    str(key): str(value)
                    for key, value in data.get("headers", {}).items()
                },
            )
        )
    return servers


def load_mcp_document(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_file():
        return {"mcpServers": {}}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers", {}), dict):
        raise ValueError("mcp.json must contain an mcpServers object")
    return payload


def save_mcp_server(path: str | Path, name: str, server: dict[str, Any]) -> None:
    config_path = Path(path)
    payload = load_mcp_document(config_path)
    payload.setdefault("mcpServers", {})[name] = server
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def delete_mcp_server(path: str | Path, name: str) -> bool:
    config_path = Path(path)
    payload = load_mcp_document(config_path)
    servers = payload.setdefault("mcpServers", {})
    if name not in servers:
        return False
    del servers[name]
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True
