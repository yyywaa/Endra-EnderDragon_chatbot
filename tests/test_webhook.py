"""webhook 路由测试：发言转发 + 工具回调（含鉴权与降级）。"""
import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from connector.config import TOOLS_CONFIG
from connector.tools.hub import Tool, ToolHub
from connector.webhook import make_webhook_app


def make_hub(**overrides) -> ToolHub:
    config = {**TOOLS_CONFIG, "enabled": True, **overrides}

    async def echo(args):
        return f"echo: {args.get('text')}"

    async def boom(args):
        raise RuntimeError("上游炸了")

    hub = ToolHub(config)
    hub.register(Tool(name="echo", description="回声", parameters={"type": "object", "properties": {}}, handler=echo))
    hub.register(Tool(name="boom", description="报错", parameters={"type": "object", "properties": {}}, handler=boom))
    return hub


async def post(app, path, payload, headers=None):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(path, json=payload, headers=headers or {})
        return resp.status, await resp.json()
    finally:
        await client.close()


class TestWebhookRoutes(unittest.TestCase):
    def test_speech_is_forwarded_and_reports_delivery(self):
        received = []

        async def on_message(content):
            received.append(content)
            return {"delivered": False, "reason": "empty:quiet-room-suppressed"}

        status, body = asyncio.run(post(make_webhook_app(on_message), "/webhook", {"content": "晚上好"}))
        self.assertEqual(status, 200)
        self.assertEqual(received, ["晚上好"])
        self.assertTrue(body["ok"])
        self.assertFalse(body["delivered"])
        self.assertEqual(body["reason"], "empty:quiet-room-suppressed")

    def test_empty_content_rejected(self):
        async def on_message(content):  # pragma: no cover - 不应被调用
            raise AssertionError("不该被调用")

        status, body = asyncio.run(post(make_webhook_app(on_message), "/webhook", {"content": ""}))
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_tool_call_returns_content(self):
        app = make_webhook_app(lambda c: asyncio.sleep(0), tool_hub=make_hub())
        status, body = asyncio.run(post(app, "/tools/call", {"name": "echo", "arguments": {"text": "hi"}}))
        self.assertEqual(status, 200)
        self.assertEqual(body["content"], "echo: hi")

    def test_tool_failure_is_returned_as_text_not_500(self):
        app = make_webhook_app(lambda c: asyncio.sleep(0), tool_hub=make_hub())
        status, body = asyncio.run(post(app, "/tools/call", {"name": "boom", "arguments": {}}))
        self.assertEqual(status, 200)
        self.assertIn("执行失败", body["content"])

    def test_unknown_tool_is_reported(self):
        app = make_webhook_app(lambda c: asyncio.sleep(0), tool_hub=make_hub())
        _, body = asyncio.run(post(app, "/tools/call", {"name": "nope", "arguments": {}}))
        self.assertIn("没有名为 nope 的工具", body["content"])

    def test_missing_name_is_400(self):
        app = make_webhook_app(lambda c: asyncio.sleep(0), tool_hub=make_hub())
        status, body = asyncio.run(post(app, "/tools/call", {"arguments": {}}))
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_token_is_enforced_when_configured(self):
        app = make_webhook_app(lambda c: asyncio.sleep(0), tool_hub=make_hub(api_token="s3cret"))

        async def scenario():
            # aiohttp 的 app 绑定创建它的 loop，两次请求必须在同一个 loop 内
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                denied = await client.post("/tools/call", json={"name": "echo", "arguments": {}})
                allowed = await client.post(
                    "/tools/call", json={"name": "echo", "arguments": {"text": "ok"}},
                    headers={"X-Tool-Token": "s3cret"},
                )
                return denied.status, allowed.status, await allowed.json()
            finally:
                await client.close()

        denied_status, allowed_status, body = asyncio.run(scenario())
        self.assertEqual(denied_status, 401)
        self.assertEqual(allowed_status, 200)
        self.assertEqual(body["content"], "echo: ok")

    def test_tools_disabled_returns_503(self):
        app = make_webhook_app(lambda c: asyncio.sleep(0))
        status, body = asyncio.run(post(app, "/tools/call", {"name": "echo", "arguments": {}}))
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
