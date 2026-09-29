"""工具层测试：限流/降级/截断、native provider（含真实网络）、MCP 客户端端到端。

真实网络用例在连不通时自动 skip（部署网络与开发机可达性不同，不应让套件变红）。
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

import requests

from connector.config import TOOLS_CONFIG
from connector.tools import native
from connector.tools.hub import Tool, ToolHub, build_hub, truncate
from connector.tools.mcp import McpClient, McpServerSpec, parse_specs

FIXTURE_SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_echo_server.py"


def tool_config(**overrides) -> dict:
    return {**TOOLS_CONFIG, **overrides}


def network_up(url: str) -> bool:
    try:
        requests.get(url, timeout=6, headers={"User-Agent": "EndraBot/test"})
        return True
    except Exception:
        return False


def run_live(coro):
    """跑一个真实网络调用；网络问题（超时/连接失败）视为 skip 而非失败。"""
    try:
        return asyncio.run(coro)
    except (requests.RequestException, asyncio.TimeoutError, TimeoutError) as e:
        raise unittest.SkipTest(f"网络不可用：{e}")


class TestHubMechanics(unittest.TestCase):
    def make_hub(self, **overrides) -> ToolHub:
        enabled = overrides.pop("enabled", True)
        return ToolHub(tool_config(enabled=enabled, **overrides))

    def test_definitions_shape(self):
        hub = self.make_hub()
        hub.register(Tool(
            name="demo",
            description="示例",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            handler=lambda args: asyncio.sleep(0, result="ok"),
        ))
        definitions = hub.definitions()
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0]["type"], "function")
        self.assertEqual(definitions[0]["function"]["name"], "demo")

    def test_disabled_hub_exports_nothing(self):
        hub = self.make_hub(enabled=False)
        hub.register(Tool(name="demo", description="d", parameters={}, handler=lambda a: asyncio.sleep(0, result="ok")))
        self.assertEqual(hub.definitions(), [])
        self.assertFalse(hub.enabled)

    def test_unknown_tool_is_readable(self):
        hub = self.make_hub()
        hub.register(Tool(name="demo", description="d", parameters={}, handler=lambda a: asyncio.sleep(0, result="ok")))
        result = asyncio.run(hub.call("nope", {}))
        self.assertIn("没有名为 nope 的工具", result)
        self.assertIn("demo", result)

    def test_handler_exception_becomes_observation(self):
        async def boom(args):
            raise RuntimeError("上游炸了")

        hub = self.make_hub()
        hub.register(Tool(name="boom", description="d", parameters={}, handler=boom))
        result = asyncio.run(hub.call("boom", {}))
        self.assertIn("执行失败", result)
        self.assertIn("上游炸了", result)

    def test_result_is_truncated(self):
        async def long(args):
            return "x" * 5000

        hub = self.make_hub(result_max_chars=100)
        hub.register(Tool(name="long", description="d", parameters={}, handler=long))
        result = asyncio.run(hub.call("long", {}))
        self.assertLess(len(result), 200)
        self.assertIn("已截断", result)

    def test_per_minute_rate_limit(self):
        now = [1000.0]
        hub = ToolHub(tool_config(enabled=True, per_minute=2, per_day=0, daily_total=0), clock=lambda: now[0])
        hub.register(Tool(name="t", description="d", parameters={}, handler=lambda a: asyncio.sleep(0, result="ok")))

        self.assertEqual(asyncio.run(hub.call("t", {})), "ok")
        self.assertEqual(asyncio.run(hub.call("t", {})), "ok")
        blocked = asyncio.run(hub.call("t", {}))
        self.assertIn("每分钟最多 2 次", blocked)

        now[0] += 61  # 窗口滑过后恢复
        self.assertEqual(asyncio.run(hub.call("t", {})), "ok")

    def test_daily_total_limit(self):
        now = [1000.0]
        hub = ToolHub(tool_config(enabled=True, per_minute=0, per_day=0, daily_total=1), clock=lambda: now[0])
        hub.register(Tool(name="t", description="d", parameters={}, handler=lambda a: asyncio.sleep(0, result="ok")))
        asyncio.run(hub.call("t", {}))
        blocked = asyncio.run(hub.call("t", {}))
        self.assertIn("今日全部工具调用已达上限", blocked)

    def test_timeout_is_read_as_observation(self):
        async def slow(args):
            await asyncio.sleep(30)
            return "never"

        hub = self.make_hub(timeout=0.05)
        hub.register(Tool(name="slow", description="d", parameters={}, handler=slow))
        result = asyncio.run(hub.call("slow", {}))
        self.assertIn("超时", result)


class TestNativeHelpers(unittest.TestCase):
    def test_symbol_normalization_and_aliases(self):
        self.assertEqual(native._normalize_symbols("btc, 以太坊，eth eth"), ["BTC", "ETH"])
        self.assertEqual(native._normalize_symbols("比特币 sol doge"), ["BTC", "SOL", "DOGE"])
        self.assertEqual(native._normalize_symbols("BTC/ETH"), ["BTC", "ETH"])
        self.assertEqual(len(native._normalize_symbols("a,b,c,d,e,f,g,h,i,j,k,l")), 10, "最多 10 个")

    def test_time_formatting_by_precision(self):
        self.assertEqual(native._format_time({"time": "+2007-08-31T00:00:00Z", "precision": 11}), "2007-08-31")
        self.assertEqual(native._format_time({"time": "+2007-08-01T00:00:00Z", "precision": 10}), "2007-08")
        self.assertEqual(native._format_time({"time": "+2007-01-01T00:00:00Z", "precision": 9}), "2007")

    def test_language_preference(self):
        mapping = {"en": {"value": "Hatsune Miku"}, "zh": {"value": "初音未來"}}
        self.assertEqual(native._pick_lang(mapping, "zh"), "初音未來")
        self.assertEqual(native._pick_lang(mapping, "ja"), "初音未來")  # 退回 zh
        self.assertEqual(native._pick_lang({"en": {"value": "Only"}}, "zh"), "Only")
        self.assertEqual(native._pick_lang({}, "zh"), "")

    def test_truncate_keeps_short_text_intact(self):
        self.assertEqual(truncate("短", 100), "短")

    def test_no_truncation_by_default(self):
        """默认不过滤结果：0 表示原样返回，只有显式配置上限时才裁。"""
        long_text = "y" * 9000
        self.assertEqual(len(truncate(long_text, 0)), 9000)
        self.assertIn("已截断", truncate(long_text, 100))

    def test_hub_does_not_truncate_by_default(self):
        hub = ToolHub(tool_config(enabled=True, result_max_chars=0))
        hub.register(Tool(
            name="big", description="d", parameters={},
            handler=lambda a: asyncio.sleep(0, result="z" * 6000),
        ))
        self.assertEqual(len(asyncio.run(hub.call("big", {}))), 6000)


class TestWikipediaAndCryptoLive(unittest.TestCase):
    """真实网络用例：连不通就 skip。"""

    def test_moegirl_search_and_page(self):
        config = tool_config()
        if not network_up(config["moegirl_api_base"]):
            self.skipTest("萌娘百科不可达")

        found = run_live(native.moegirl_search({"query": "初音未来", "limit": 3}, config))
        self.assertIn("初音未来", found)

        page = run_live(native.moegirl_page({"title": "初音未来"}, config))
        self.assertIn("初音未来", page)
        self.assertGreater(len(page), 80)

    def test_moegirl_missing_page_is_graceful(self):
        config = tool_config()
        if not network_up(config["moegirl_api_base"]):
            self.skipTest("萌娘百科不可达")
        result = run_live(native.moegirl_page({"title": "这个条目肯定不存在zzz123"}, config))
        self.assertTrue("没有" in result)

    def test_wiki_lookup_returns_facts(self):
        config = tool_config()
        if not network_up(config["wiki_api_base"]):
            self.skipTest("Wikidata 不可达")
        result = run_live(native.wiki_lookup({"query": "初音未来"}, config))
        self.assertIn("Q552682", result)
        self.assertIn("类型", result)

    def test_wiki_lookup_unknown_entity(self):
        config = tool_config()
        if not network_up(config["wiki_api_base"]):
            self.skipTest("Wikidata 不可达")
        result = run_live(native.wiki_lookup({"query": "zzz不存在的实体qqq12345"}, config))
        self.assertIn("没有找到", result)

    def test_crypto_price_live(self):
        config = tool_config()
        if not network_up(config["crypto_api_base"]):
            self.skipTest("Gate.io 不可达")
        result = run_live(native.crypto_price({"symbols": "btc,eth"}, config))
        self.assertIn("BTC/USDT", result)
        self.assertIn("ETH/USDT", result)
        self.assertIn("来源", result)

    def test_crypto_price_unknown_pair_is_graceful(self):
        config = tool_config()
        if not network_up(config["crypto_api_base"]):
            self.skipTest("Gate.io 不可达")
        result = run_live(native.crypto_price({"symbols": "ZZZZZZ"}, config))
        self.assertIn("ZZZZZZ/USDT", result)


class TestMcpSpecs(unittest.TestCase):
    def test_parse_object_and_array(self):
        single = parse_specs('{"name":"fs","command":"npx","args":["-y","server-filesystem"]}')
        self.assertEqual(len(single), 1)
        self.assertEqual(single[0].name, "fs")
        self.assertEqual(single[0].transport, "stdio")

        multi = parse_specs(json.dumps([
            {"name": "remote", "url": "https://example.com/mcp", "prefix": False, "allow": ["search"]},
            {"name": "bad name!", "command": "node"},
        ]))
        self.assertEqual(len(multi), 2)
        self.assertEqual(multi[0].transport, "http")
        self.assertFalse(multi[0].prefix)
        self.assertEqual(multi[1].name, "bad_name_", "非法字符应被替换")

    def test_invalid_json_is_ignored(self):
        self.assertEqual(parse_specs("{not json"), [])
        self.assertEqual(parse_specs(""), [])

    def test_entries_without_transport_are_skipped(self):
        self.assertEqual(parse_specs('[{"name":"x"}]'), [])


class TestMcpClientEndToEnd(unittest.TestCase):
    """真拉一个 stdio MCP server 子进程，验证握手/列工具/调用/异常。"""

    @classmethod
    def setUpClass(cls):
        try:
            import mcp  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("未安装 mcp SDK")

        cls.spec = McpServerSpec(
            name="fixture",
            command=sys.executable,
            args=[str(FIXTURE_SERVER)],
            env={},
        )

    def test_handshake_list_and_call(self):
        async def scenario():
            client = McpClient(self.spec, timeout=25)
            started = await client.start()
            self.assertTrue(started, client.error)
            try:
                tools = await client.list_tools()
                names = sorted(getattr(t, "name", "") for t in tools)
                self.assertEqual(names, ["add", "boom", "echo"])

                self.assertEqual(await client.call_tool("echo", {"text": "你好"}), "echo: 你好")
                self.assertEqual(await client.call_tool("add", {"a": 2, "b": 3}), "5")

                failed = await client.call_tool("boom", {})
                self.assertIn("工具报错", failed)
            finally:
                await client.close()

        asyncio.run(scenario())

    def test_tools_are_registered_into_hub_with_prefix(self):
        async def scenario():
            config = tool_config(
                enabled=True,
                guard_enabled=False,  # 本用例只验证 MCP 桥接；审查语义见 test_guard.py
                mcp_servers=json.dumps([{
                    "name": "fx",
                    "command": sys.executable,
                    "args": [str(FIXTURE_SERVER)],
                }]),
                mcp_timeout=25,
            )
            hub = await build_hub(config)
            try:
                names = hub.tool_names()
                self.assertIn("fx_echo", names)
                self.assertIn("fx_add", names)

                # MCP server 能力未知（可能含写操作），默认交给模型审查层过一遍
                self.assertTrue(hub.get("fx_echo").guarded)

                result = await hub.call("fx_echo", {"text": "hi"})
                self.assertEqual(result, "echo: hi")
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_allow_and_deny_filters(self):
        async def scenario():
            config = tool_config(
                enabled=True,
                guard_enabled=False,  # 本用例只验证 MCP 桥接；审查语义见 test_guard.py
                mcp_servers=json.dumps([{
                    "name": "fx",
                    "command": sys.executable,
                    "args": [str(FIXTURE_SERVER)],
                    "deny": ["boom"],
                }]),
                mcp_timeout=25,
            )
            hub = await build_hub(config)
            try:
                self.assertNotIn("fx_boom", hub.tool_names())
                self.assertIn("fx_echo", hub.tool_names())
            finally:
                await hub.aclose()

        asyncio.run(scenario())

    def test_unreachable_server_does_not_break_hub(self):
        async def scenario():
            config = tool_config(
                enabled=True,
                guard_enabled=False,  # 本用例只验证 MCP 桥接；审查语义见 test_guard.py
                mcp_servers=json.dumps([{
                    "name": "ghost",
                    "command": "/nonexistent/definitely-missing-binary",
                    "args": [],
                }]),
                mcp_timeout=8,
            )
            hub = await build_hub(config)
            try:
                # native 工具仍在，坏掉的 MCP server 被跳过
                self.assertIn("moegirl_page", hub.tool_names())
                self.assertNotIn("ghost_echo", hub.tool_names())
            finally:
                await hub.aclose()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
