"""webhook：接收 alive-buddy 的 agent 发言，转发进 coffeeroom；并承接工具调用回调。"""
from typing import Awaitable, Callable, Optional

from aiohttp import web

from .config import TOOLS_CONFIG
from .logger import setup_logger

logger = setup_logger("webhook")

OnMessage = Callable[[str], Awaitable[Optional[dict]]]


def make_webhook_app(on_message: OnMessage, tool_hub=None) -> web.Application:
    async def handle_webhook(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        content = data.get("content")
        if not content:
            return web.json_response({"ok": False, "error": "empty content"}, status=400)
        logger.info(f"[Webhook] 收到 agent 发言: {str(content)[:100]}")
        # 始终回 200：被在场闸门抑制不算"发送失败"，否则 alive-buddy 会把
        # 抑制当成错误写回 reAct 上下文。投递结果只放在响应体里供观测。
        result = await on_message(str(content))
        return web.json_response({"ok": True, **(result or {})})

    async def handle_tool_call(request: web.Request) -> web.Response:
        """alive-buddy 的远程工具回调：{name, arguments} → {content}。"""
        if tool_hub is None:
            return web.json_response({"ok": False, "error": "tools disabled"}, status=503)

        # 口令优先取 hub 自己的配置（与工具执行同源，便于测试与多实例）
        token = getattr(tool_hub, "config", TOOLS_CONFIG).get("api_token")
        if token and request.headers.get("X-Tool-Token") != token:
            logger.warning("[Tool] 拒绝未授权调用")
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        name = str(data.get("name") or "")
        arguments = data.get("arguments")
        if not name:
            return web.json_response({"ok": False, "error": "missing name"}, status=400)
        if not isinstance(arguments, dict):
            arguments = {}

        content = await tool_hub.call(name, arguments)
        return web.json_response({"ok": True, "content": content})

    async def handle_health(request: web.Request) -> web.Response:
        payload = {"ok": True}
        if tool_hub is not None:
            payload["tools"] = tool_hub.tool_names()
        return web.json_response(payload)

    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_post("/tools/call", handle_tool_call)
    app.router.add_get("/health", handle_health)
    return app


async def start_webhook(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"[Webhook] 监听 http://{host}:{port}/webhook")
    return runner
