"""
MCP (Model Context Protocol) management routes.

Provides HTTP API for the frontend to manage MCP servers:
- List configured servers and their status
- Connect/disconnect servers
- View available tools per server
- Add/remove server configs
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_agent(request: Request):
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        return None
    if hasattr(agent, "mcp_client"):
        return agent
    local = getattr(agent, "_local_agent", None)
    if local and hasattr(local, "mcp_client"):
        return local
    return None


def _get_mcp_client(request: Request):
    agent = _get_agent(request)
    return agent.mcp_client if agent else None


def _get_mcp_catalog(request: Request):
    agent = _get_agent(request)
    return agent.mcp_catalog if agent else None


def _refresh_catalog_text(request: Request):
    """刷新 Agent 系统提示中的 MCP 清单文本"""
    agent = _get_agent(request)
    if agent and hasattr(agent, "_mcp_catalog_text"):
        agent._mcp_catalog_text = agent.mcp_catalog.generate_catalog()


def _sync_tools_to_catalog(request: Request, server_name: str, client):
    """连接成功后将运行时工具同步到 catalog 并刷新系统提示"""
    catalog = _get_mcp_catalog(request)
    tools = client.list_tools(server_name)
    if catalog and tools:
        tool_dicts = [
            {"name": t.name, "description": t.description,
             "input_schema": t.input_schema}
            for t in tools
        ]
        catalog.sync_tools_from_client(server_name, tool_dicts, force=True)
    _refresh_catalog_text(request)


class MCPServerAddRequest(BaseModel):
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    description: str = ""
    auto_connect: bool = False


class MCPConnectRequest(BaseModel):
    server_name: str


@router.get("/api/mcp/servers")
async def list_mcp_servers(request: Request):
    """List all MCP servers with their config and connection status."""
    client = _get_mcp_client(request)
    catalog = _get_mcp_catalog(request)

    if client is None:
        return {"error": "Agent not initialized", "servers": []}

    from openakita.config import settings
    if not settings.mcp_enabled:
        return {"mcp_enabled": False, "servers": [], "message": "MCP is disabled"}

    configured = client.list_servers()
    connected = client.list_connected()

    servers = []
    for name in configured:
        server_config = client._servers.get(name)
        tools = client.list_tools(name)

        catalog_info = None
        if catalog:
            for s in catalog.servers:
                if s.identifier == name:
                    catalog_info = s
                    break

        workspace_dir = settings.mcp_config_path / name
        source = "workspace" if workspace_dir.exists() else "builtin"

        servers.append({
            "name": name,
            "description": server_config.description if server_config else "",
            "transport": server_config.transport if server_config else "stdio",
            "url": server_config.url if server_config else "",
            "command": server_config.command if server_config else "",
            "connected": name in connected,
            "tools": [
                {"name": t.name, "description": t.description}
                for t in tools
            ],
            "tool_count": len(tools),
            "has_instructions": bool(
                catalog_info and catalog_info.instructions
            ) if catalog_info else False,
            "catalog_tool_count": len(catalog_info.tools) if catalog_info else 0,
            "source": source,
            "removable": source == "workspace",
        })

    return {
        "mcp_enabled": True,
        "servers": servers,
        "total": len(servers),
        "connected": len(connected),
        "workspace_path": str(settings.mcp_config_path),
    }


@router.post("/api/mcp/connect")
async def connect_mcp_server(request: Request, body: MCPConnectRequest):
    """Connect to a specific MCP server."""
    client = _get_mcp_client(request)
    if client is None:
        return {"error": "Agent not initialized"}

    if body.server_name in client.list_connected():
        tools = client.list_tools(body.server_name)
        return {
            "status": "already_connected",
            "server": body.server_name,
            "tools": [{"name": t.name, "description": t.description} for t in tools],
        }

    result = await client.connect(body.server_name)
    if result.success:
        _sync_tools_to_catalog(request, body.server_name, client)
        tools = client.list_tools(body.server_name)
        return {
            "status": "connected",
            "server": body.server_name,
            "tools": [{"name": t.name, "description": t.description} for t in tools],
            "tool_count": result.tool_count,
        }
    else:
        return {
            "status": "failed",
            "server": body.server_name,
            "error": result.error or "连接失败（未知原因）",
        }


@router.post("/api/mcp/disconnect")
async def disconnect_mcp_server(request: Request, body: MCPConnectRequest):
    """Disconnect from a specific MCP server."""
    client = _get_mcp_client(request)
    if client is None:
        return {"error": "Agent not initialized"}

    if body.server_name not in client.list_connected():
        return {"status": "not_connected", "server": body.server_name}

    await client.disconnect(body.server_name)
    return {"status": "disconnected", "server": body.server_name}


@router.get("/api/mcp/tools")
async def list_mcp_tools(request: Request, server: str | None = None):
    """List all available MCP tools, optionally filtered by server."""
    client = _get_mcp_client(request)
    if client is None:
        return {"error": "Agent not initialized", "tools": []}

    tools = client.list_tools(server)
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ],
        "total": len(tools),
    }


@router.get("/api/mcp/instructions/{server_name}")
async def get_mcp_instructions(request: Request, server_name: str):
    """Get INSTRUCTIONS.md for a specific MCP server."""
    catalog = _get_mcp_catalog(request)
    if catalog is None:
        return {"error": "Agent not initialized"}

    instructions = catalog.get_server_instructions(server_name)
    if instructions:
        return {"server": server_name, "instructions": instructions}
    return {"server": server_name, "instructions": None, "message": "No instructions available"}


@router.post("/api/mcp/servers/add")
async def add_mcp_server(request: Request, body: MCPServerAddRequest):
    """Add a new MCP server config (persisted to workspace data/mcp/servers/)."""
    from openakita.tools.mcp import VALID_TRANSPORTS

    import re
    if not body.name.strip():
        return {"status": "error", "message": "服务器名称不能为空"}
    if not re.match(r'^[a-zA-Z0-9_-]+$', body.name.strip()):
        return {"status": "error", "message": "服务器名称只能包含字母、数字、连字符和下划线"}
    if body.transport not in VALID_TRANSPORTS:
        return {"status": "error", "message": f"不支持的传输协议: {body.transport}（支持: {', '.join(sorted(VALID_TRANSPORTS))}）"}
    if body.transport == "stdio" and not body.command.strip():
        return {"status": "error", "message": "stdio 模式需要填写启动命令"}
    if body.transport in ("streamable_http", "sse") and not body.url.strip():
        return {"status": "error", "message": f"{body.transport} 模式需要填写 URL"}

    from openakita.config import settings

    name = body.name.strip()
    server_dir = settings.mcp_config_path / name
    server_dir.mkdir(parents=True, exist_ok=True)

    # stdio 模式下自动解析相对路径为绝对路径
    resolved_args = list(body.args)
    if body.transport == "stdio":
        from pathlib import Path as _P
        search_bases = [server_dir, settings.project_root, _P.cwd()]
        for i, arg in enumerate(resolved_args):
            if arg.startswith("-") or _P(arg).is_absolute():
                continue
            for base in search_bases:
                candidate = base / arg
                if candidate.is_file():
                    resolved_args[i] = str(candidate.resolve())
                    break

    metadata = {
        "serverIdentifier": name,
        "serverName": body.description or name,
        "command": body.command,
        "args": resolved_args,
        "env": body.env,
        "transport": body.transport,
        "url": body.url,
        "autoConnect": body.auto_connect,
    }

    metadata_file = server_dir / "SERVER_METADATA.json"
    metadata_file.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    client = _get_mcp_client(request)
    catalog = _get_mcp_catalog(request)

    if catalog:
        catalog.scan_mcp_directory(settings.mcp_config_path)
        catalog.invalidate_cache()

    if client:
        from openakita.tools.mcp import MCPServerConfig
        client.add_server(MCPServerConfig(
            name=name,
            command=body.command,
            args=resolved_args,
            env=body.env,
            description=body.description,
            transport=body.transport,
            url=body.url,
            cwd=str(server_dir),
        ))

    _refresh_catalog_text(request)

    # 添加后尝试连接，获取工具信息
    connect_result = None
    if client:
        result = await client.connect(name)
        if result.success:
            _sync_tools_to_catalog(request, name, client)
            connect_result = {"connected": True, "tool_count": result.tool_count}
        else:
            connect_result = {"connected": False, "error": result.error}

    return {
        "status": "ok",
        "server": name,
        "path": str(server_dir),
        "connect_result": connect_result,
    }


@router.delete("/api/mcp/servers/{server_name}")
async def remove_mcp_server(request: Request, server_name: str):
    """Remove an MCP server config (only workspace configs, not built-in)."""
    from openakita.config import settings

    client = _get_mcp_client(request)
    if client and server_name in client.list_connected():
        await client.disconnect(server_name)

    workspace_dir = settings.mcp_config_path / server_name
    builtin_dir = settings.mcp_builtin_path / server_name

    removed = False
    if workspace_dir.exists():
        import shutil
        shutil.rmtree(workspace_dir, ignore_errors=True)
        removed = True
    elif builtin_dir.exists():
        return {"status": "error", "message": f"{server_name} is a built-in server and cannot be removed"}

    if client:
        client._servers.pop(server_name, None)
        client._connections.pop(server_name, None)
        prefix = f"{server_name}:"
        for key in [k for k in client._tools if k.startswith(prefix)]:
            del client._tools[key]
        for key in [k for k in client._resources if k.startswith(prefix)]:
            del client._resources[key]
        for key in [k for k in client._prompts if k.startswith(prefix)]:
            del client._prompts[key]

    catalog = _get_mcp_catalog(request)
    if catalog:
        catalog._servers = [s for s in catalog._servers if s.identifier != server_name]
        catalog.invalidate_cache()

    _refresh_catalog_text(request)

    return {"status": "ok", "server": server_name, "removed": removed}
