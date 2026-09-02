"""webhook：接收 alive-buddy 的 agent 发言，转发进 coffeeroom。"""
from typing import Awaitable, Callable

from aiohttp import web

from .logger import setup_logger

logger = setup_logger("webhook")

OnMessage = Callable[[str], Awaitable[None]]


def make_webhook_app(on_message: OnMessage) -> web.Application:
    async def handle_webhook(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        content = data.get("content")
        if not content:
            return web.json_response({"ok": False, "error": "empty content"}, status=400)
        logger.info(f"[Webhook] 收到 agent 发言: {str(content)[:100]}")
        await on_message(str(content))
        return web.json_response({"ok": True})

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
    return app


async def start_webhook(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"[Webhook] 监听 http://{host}:{port}/webhook")
    return runner
