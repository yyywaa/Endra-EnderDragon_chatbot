"""可选 MCP 支持：把任意 MCP server 的工具接进 reAct。

为什么 MCP 客户端要放在 connector 而不是 alive-buddy：
- DeepSeek 的 Responses API **忽略 `mcp` 等内置工具**（只支持 `function`），服务端不会替我们调；
- connector 是 Python 层，持有密钥、代理与出网策略，MCP server 的进程与凭据都留在这里；
- alive-buddy 因此不需要任何新依赖，只通过 `/tools/call` 回调执行。

用法（.env）：
    MCP_SERVERS=[{"name":"fs","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/data"]}]
    MCP_SERVERS=[{"name":"remote","url":"https://example.com/mcp","allow":["search"]}]
需要装 SDK：pip install mcp（未安装时本模块静默跳过，不影响其他工具）。
"""
import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..logger import setup_logger
from .hub import Tool, ToolHub

logger = setup_logger("tools.mcp")

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_NAME_LEN = 64  # OpenAI function 名称上限


@dataclass
class McpServerSpec:
    name: str
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    prefix: bool = True
    allow: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


def parse_specs(raw) -> List[McpServerSpec]:
    """解析 MCP_SERVERS 配置（JSON 数组，或单个对象）。"""
    if isinstance(raw, (list, dict)):
        data = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"[MCP] MCP_SERVERS 不是合法 JSON，已忽略：{e}")
            return []

    if isinstance(data, dict):
        data = [data]

    specs: List[McpServerSpec] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            logger.error("[MCP] 跳过缺少 name 的 server 配置")
            continue
        url = item.get("url")
        command = item.get("command")
        if not url and not command:
            logger.error(f"[MCP] server {name} 既没有 url 也没有 command，已跳过")
            continue
        specs.append(McpServerSpec(
            name=_NAME_SAFE.sub("_", name),
            command=command,
            args=[str(a) for a in (item.get("args") or [])],
            env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
            cwd=item.get("cwd"),
            url=url,
            prefix=bool(item.get("prefix", True)),
            allow=[str(x) for x in (item.get("allow") or [])],
            deny=[str(x) for x in (item.get("deny") or [])],
        ))
    return specs


class McpClient:
    """维持与单个 MCP server 的长连接（MCP 是有会话状态的协议）。"""

    def __init__(self, spec: McpServerSpec, timeout: float = 20.0):
        self.spec = spec
        self.timeout = timeout
        self._session = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.error: Optional[str] = None

    async def start(self) -> bool:
        """建立连接并完成 initialize 握手；失败返回 False（不影响其他工具）。"""
        self._task = asyncio.create_task(self._serve(), name=f"mcp-{self.spec.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self.error = f"连接超时（{self.timeout}s）"
        if self._session is None:
            logger.warning(f"[MCP] server {self.spec.name} 未就绪：{self.error or '未知错误'}")
            return False
        logger.info(f"[MCP] server {self.spec.name} 已连接（{self.spec.transport}）")
        return True

    async def _serve(self):
        try:
            from mcp import ClientSession

            async with AsyncExitStack() as stack:
                if self.spec.url:
                    from mcp.client.streamable_http import streamable_http_client
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.spec.url)
                    )
                else:
                    from mcp.client.stdio import StdioServerParameters, stdio_client
                    params = StdioServerParameters(
                        command=str(self.spec.command),
                        args=list(self.spec.args),
                        env={**os.environ, **self.spec.env},
                        cwd=self.spec.cwd,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))

                session = await stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=self.timeout)
                )
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            logger.warning(f"[MCP] server {self.spec.name} 连接异常：{self.error}")
        finally:
            self._session = None
            self._ready.set()

    async def list_tools(self) -> list:
        if self._session is None:
            return []
        try:
            result = await asyncio.wait_for(self._session.list_tools(), timeout=self.timeout)
        except Exception as e:
            logger.warning(f"[MCP] server {self.spec.name} list_tools 失败：{e}")
            return []
        return list(getattr(result, "tools", None) or [])

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> str:
        if self._session is None:
            return f"MCP server {self.spec.name} 当前未连接，这个工具暂时不可用。"
        result = await asyncio.wait_for(
            self._session.call_tool(name, arguments or {}), timeout=self.timeout
        )
        return _render_result(result)

    async def close(self):
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:
                pass
            self._task = None


def _render_result(result) -> str:
    """把 MCP 的 CallToolResult 摊平成一段文本 observation。"""
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    parts: List[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
            continue
        item_type = getattr(item, "type", "unknown")
        parts.append(f"[{item_type} 类型的内容，无法直接阅读]")

    if not parts:
        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
        if structured:
            parts.append(json.dumps(structured, ensure_ascii=False))

    body = "\n".join(parts).strip() or "（该工具没有返回内容）"
    return f"工具报错：{body}" if is_error else body


def _local_name(spec: McpServerSpec, tool_name: str) -> str:
    raw = f"{spec.name}_{tool_name}" if spec.prefix else tool_name
    return _NAME_SAFE.sub("_", raw)[:_MAX_NAME_LEN]


def _allowed(spec: McpServerSpec, tool_name: str) -> bool:
    if tool_name in spec.deny:
        return False
    if spec.allow and tool_name not in spec.allow:
        return False
    return True


async def register_mcp_tools(hub: ToolHub) -> List[McpClient]:
    """连接配置里的 MCP server，把它们的工具注册进 hub。返回已连接的客户端（供关闭）。"""
    specs = parse_specs(hub.config.get("mcp_servers"))
    if not specs:
        return []

    try:
        import mcp  # noqa: F401
    except ImportError:
        logger.warning("[MCP] 配置了 MCP_SERVERS 但未安装 mcp SDK，已跳过（pip install mcp）")
        return []

    clients: List[McpClient] = []
    for spec in specs:
        client = McpClient(spec, timeout=float(hub.config.get("mcp_timeout") or 20))
        if not await client.start():
            await client.close()
            continue

        registered = 0
        for remote in await client.list_tools():
            remote_name = str(getattr(remote, "name", "") or "")
            if not remote_name or not _allowed(spec, remote_name):
                continue
            local = _local_name(spec, remote_name)
            if hub.get(local) is not None:
                logger.warning(f"[MCP] 工具名冲突，跳过 {local}")
                continue
            description = str(getattr(remote, "description", "") or f"MCP server {spec.name} 提供的工具")
            schema = getattr(remote, "inputSchema", None) or getattr(remote, "input_schema", None)
            hub.register(Tool(
                name=local,
                description=f"[MCP:{spec.name}] {description}"[:1024],
                parameters=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
                handler=_make_handler(client, remote_name),
                # MCP server 的能力未知（可能含写操作），默认交给审查层过一遍
                guarded=True,
            ))
            registered += 1
        logger.info(f"[MCP] server {spec.name} 注册了 {registered} 个工具")
        clients.append(client)
    return clients


def _make_handler(client: McpClient, remote_name: str):
    async def handler(args: dict) -> str:
        return await client.call_tool(remote_name, args)
    return handler
