"""测试用 MCP server（stdio 传输），用于验证 connector 的 MCP 客户端。

由 tests/test_tools.py 以子进程方式拉起：
    python tests/fixtures/mcp_echo_server.py
"""
import asyncio
import sys

from mcp.server.mcpserver import MCPServer

server = MCPServer("endra-test-server")


@server.tool(description="回声工具：返回传入的文本")
def echo(text: str) -> str:
    return f"echo: {text}"


@server.tool(description="加法工具：返回两数之和")
def add(a: int, b: int) -> str:
    return str(a + b)


@server.tool(description="总是失败的工具")
def boom() -> str:
    raise ValueError("boom")


if __name__ == "__main__":
    try:
        asyncio.run(server.run_stdio_async())
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    sys.exit(0)
