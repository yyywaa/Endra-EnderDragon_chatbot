"""connector 单元测试：消息过滤、批次 silent 标记、冷却窗口、断线重 init。"""
import asyncio
import json
import time
import unittest
from pathlib import Path

from connector.buddy_client import BuddyClient, SessionNotFoundError
from connector.config import PRESENCE_CONFIG
from connector.presence import RoomPresence
from connector.room_client import RoomClient


def make_msg(sender="player1", text="hello", msg_id=None, age_seconds=0):
    return {
        "sender_username": sender,
        "text": text,
        "msg_id": msg_id or f"msg-{id(text)}-{time.time_ns()}",
        "timestamp": int((time.time() - age_seconds) * 1000),
    }


def make_room():
    return RoomClient(buddy=None, room="test-room")


class TestMessageFilter(unittest.TestCase):
    def test_extract_valid_messages(self):
        room = make_room()
        self.assertEqual(room._extract_valid_messages({"text": "a"}), [{"text": "a"}])
        self.assertEqual(len(room._extract_valid_messages([{"text": "a"}, {"no_text": 1}])), 1)
        self.assertEqual(room._extract_valid_messages("junk"), [])

    def test_self_message_filtered(self):
        room = make_room()
        self.assertTrue(room._is_self(make_msg(sender="EnderDragon")))
        self.assertFalse(room._is_self(make_msg(sender="player1")))

    def test_duplicate_filtered(self):
        room = make_room()
        m = make_msg(msg_id="fixed-id")
        self.assertFalse(room._is_duplicate(m))
        room._mark_processed(m)
        self.assertTrue(room._is_duplicate(m))

    def test_freshness_window(self):
        room = make_room()
        self.assertTrue(room._is_message_fresh(make_msg(age_seconds=10)))
        self.assertFalse(room._is_message_fresh(make_msg(age_seconds=3600)))
        self.assertFalse(room._is_message_fresh({"text": "no timestamp"}))

    def test_processed_ids_trimmed(self):
        room = make_room()
        for i in range(1100):
            room._mark_processed(make_msg(msg_id=f"id-{i}"))
        self.assertLessEqual(len(room.processed_msg_ids), 1000)


class TestBatchSilentPlan(unittest.TestCase):
    def test_only_last_non_silent(self):
        room = make_room()
        batch = [make_msg(msg_id=f"m{i}") for i in range(3)]
        plan = room._plan_batch(batch)
        self.assertEqual([s for _, s in plan], [True, True, False])

    def test_single_message_non_silent(self):
        room = make_room()
        plan = room._plan_batch([make_msg()])
        self.assertEqual([s for _, s in plan], [False])

    def test_stale_last_is_silent(self):
        room = make_room()
        batch = [make_msg(msg_id="fresh"), make_msg(msg_id="stale", age_seconds=3600)]
        plan = room._plan_batch(batch)
        self.assertEqual([s for _, s in plan], [True, True])

    def test_cooldown_makes_all_silent(self):
        room = make_room()
        room.last_trigger_time = time.time()  # 刚触发过，冷却中
        plan = room._plan_batch([make_msg()])
        self.assertEqual([s for _, s in plan], [True])

    def test_cooldown_expired(self):
        room = make_room()
        room.last_trigger_time = time.time() - room.reply_cooldown - 1
        plan = room._plan_batch([make_msg()])
        self.assertEqual([s for _, s in plan], [False])


class StubBuddy:
    def __init__(self):
        self.delivered = []

    async def deliver(self, text, silent, user_id="coffeeroom"):
        self.delivered.append((text, silent, user_id))


class TestDeliverBatch(unittest.TestCase):
    def test_deliver_marks_and_triggers(self):
        buddy = StubBuddy()
        room = RoomClient(buddy=buddy, room="test-room")
        batch = [make_msg(sender="alice", text=f"hi{i}", msg_id=f"d{i}") for i in range(2)]
        asyncio.run(room._deliver_batch(batch))
        self.assertEqual([s for _, s, _ in buddy.delivered], [True, False])
        self.assertTrue(buddy.delivered[0][0].startswith("alice: "))
        self.assertGreater(room.last_trigger_time, 0)
        self.assertIn("d0", room.processed_msg_ids)
        self.assertIn("d1", room.processed_msg_ids)

    def test_deliver_failure_marks_processed(self):
        class FailingBuddy:
            async def deliver(self, text, silent, user_id="coffeeroom"):
                raise ConnectionError("down")

        room = RoomClient(buddy=FailingBuddy(), room="test-room")
        m = make_msg(msg_id="fail-1")
        asyncio.run(room._deliver_batch([m]))
        self.assertIn("fail-1", room.processed_msg_ids)


class FakeReceiptWS:
    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class TestBuddyReceipts(unittest.TestCase):
    def test_placeholder_echo_ignored(self):
        buddy = BuddyClient()
        echo = json.dumps({"msg_id": "x", "payload": {"role": "assistant", "content": []}})
        asyncio.run(buddy._consume_receipts(FakeReceiptWS([echo, "not json"])))  # 正常结束

    def test_session_not_found_raises(self):
        buddy = BuddyClient()
        err = json.dumps({"error": "Session not found"})
        with self.assertRaises(SessionNotFoundError):
            asyncio.run(buddy._consume_receipts(FakeReceiptWS([err])))

    def test_invalid_format_error_does_not_raise_session_error(self):
        buddy = BuddyClient()
        err = json.dumps({"error": "Invalid message format"})
        asyncio.run(buddy._consume_receipts(FakeReceiptWS([err])))  # 不抛 SessionNotFoundError


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class StubPresence:
    """替身在场查询：count=None 表示查询失败/未知。"""

    def __init__(self, count):
        self.count = count
        self.queries = 0

    async def human_count(self):
        self.queries += 1
        return self.count


class StubMcPresence:
    """替身 MC 在场信号（RCON list）：count=None 表示未配置/查询失败。"""

    def __init__(self, count=None):
        self.count = count
        self.queries = 0

    async def human_count(self):
        self.queries += 1
        return self.count


def make_gated_room(count=0, clock=None, **overrides):
    room = RoomClient(buddy=StubBuddy(), room="test-room", clock=clock)
    room.presence_enabled = True
    room.presence_config = {
        **PRESENCE_CONFIG,
        "fail_mode": "quota",
        "reactive_window": 180,
        "quiet_daily_quota": 1,
        "quiet_window_hours": 24,
        # 默认关掉出站节流：本辅助函数用于测在场闸门，节流由 TestOutboundThrottle 专测
        "outbound_min_interval": 0,
        "outbound_per_minute": 0,
        **overrides,
    }
    room.presence = StubPresence(count)
    room.mc_presence = StubMcPresence(overrides.pop("mc_presence_count", None))
    room._ws = FakeWS()
    return room


class TestOutboundPresenceGate(unittest.TestCase):
    def test_present_allows_repeated_proactive(self):
        room = make_gated_room(count=2)
        for i in range(3):
            result = asyncio.run(room.send_reply(f"line {i}"))
            self.assertTrue(result["delivered"], result)
        self.assertEqual(room._ws.sent, ["line 0", "line 1", "line 2"])

    def test_empty_room_keeps_only_daily_quota(self):
        room = make_gated_room(count=0)
        first = asyncio.run(room.send_reply("first"))
        second = asyncio.run(room.send_reply("second"))
        self.assertTrue(first["delivered"])
        self.assertEqual(first["reason"], "empty:quiet-quota")
        self.assertFalse(second["delivered"])
        self.assertEqual(second["reason"], "empty:quiet-room-suppressed")
        self.assertEqual(room._ws.sent, ["first"])

    def test_empty_room_always_allows_reactive_reply(self):
        room = make_gated_room(count=0)
        room.last_human_message_at = time.time()
        for i in range(3):
            result = asyncio.run(room.send_reply(f"reply {i}"))
            self.assertTrue(result["delivered"], result)
            self.assertEqual(result["reason"], "empty:reactive")
        self.assertEqual(len(room._ws.sent), 3)
        self.assertEqual(room._quiet_sends, [])  # 回应不消耗配额

    def test_reactive_window_expires(self):
        room = make_gated_room(count=0)
        room.last_human_message_at = time.time() - room.presence_config["reactive_window"] - 1
        first = asyncio.run(room.send_reply("late reply"))
        second = asyncio.run(room.send_reply("proactive"))
        self.assertEqual(first["reason"], "empty:quiet-quota")  # 过期后按主动处理
        self.assertFalse(second["delivered"])

    def test_unknown_presence_follows_fail_mode(self):
        quiet = make_gated_room(count=None, fail_mode="quota")
        self.assertTrue(asyncio.run(quiet.send_reply("a"))["delivered"])
        self.assertFalse(asyncio.run(quiet.send_reply("b"))["delivered"])

        open_room = make_gated_room(count=None, fail_mode="open")
        for i in range(2):
            result = asyncio.run(open_room.send_reply(f"open {i}"))
            self.assertTrue(result["delivered"], result)
            self.assertEqual(result["reason"], "unknown:fail-open")

    def test_quota_window_rolls_over(self):
        room = make_gated_room(count=0)
        self.assertTrue(asyncio.run(room.send_reply("yesterday"))["delivered"])
        room._quiet_sends = [time.time() - 25 * 3600]
        self.assertTrue(asyncio.run(room.send_reply("today"))["delivered"])

    def test_presence_disabled_passes_through(self):
        room = make_gated_room(count=0)
        room.presence_enabled = False
        self.assertTrue(asyncio.run(room.send_reply("hi"))["delivered"])
        self.assertEqual(room.presence.queries, 0)

    def test_disconnected_room_reports_reason(self):
        room = make_gated_room(count=1)
        room._ws = None
        result = asyncio.run(room.send_reply("hi"))
        self.assertFalse(result["delivered"])
        self.assertEqual(result["reason"], "room-disconnected")

    def test_human_message_marks_activity(self):
        room = make_gated_room(count=0)
        asyncio.run(room._deliver_batch([make_msg(sender="alice", text="hi")]))
        self.assertGreater(room.last_human_message_at, 0)

    def test_self_and_ignored_senders_do_not_mark_activity(self):
        room = make_gated_room(count=0)
        room.ignore_users = {"bridge-bot"}
        asyncio.run(room._deliver_batch([make_msg(sender="EnderDragon", text="mine")]))
        asyncio.run(room._deliver_batch([make_msg(sender="bridge-bot", text="relay")]))
        self.assertEqual(room.last_human_message_at, 0.0)


def make_response(payload, status=200):
    class FakeResponse:
        status_code = status

        def json(self):
            return payload

    return FakeResponse()


class TestRoomPresence(unittest.TestCase):
    def make_presence(self, payload, call_log=None, fail=False, status=200, **overrides):
        clock = overrides.pop("clock", None) or (lambda: 1000.0)

        def http_get(url, headers=None, timeout=None):
            if call_log is not None:
                call_log.append(url)
            if fail:
                raise RuntimeError("boom")
            return make_response(payload, status=status)

        config = {
            **PRESENCE_CONFIG,
            "api_url": "http://room.test/api/online-users",
            "cache_ttl": 60,
            "timeout": 5,
            "ignore_users": [],
            **overrides,
        }
        return RoomPresence(
            room="minecraft",
            cookie_provider=lambda: "session=abc",
            config=config,
            http_get=http_get,
            clock=clock,
            bot_username="EnderDragon",
            ignore_users=config["ignore_users"],
        )

    def test_counts_only_humans_in_own_room(self):
        presence = self.make_presence(
            {
                "success": True,
                "users": [
                    {"username": "alice", "channel": "minecraft"},
                    {"username": "bob", "channel": "minecraft"},
                    {"username": "carol", "channel": "general"},
                    {"username": "EnderDragon", "channel": "minecraft"},
                    {"username": "bridge-bot", "channel": "minecraft"},
                ],
            },
            ignore_users=["bridge-bot"],
        )
        self.assertEqual(asyncio.run(presence.human_count()), 2)

    def test_cache_avoids_repeat_queries(self):
        calls = []
        presence = self.make_presence({"success": True, "users": []}, call_log=calls)
        asyncio.run(presence.human_count())
        asyncio.run(presence.human_count())
        self.assertEqual(len(calls), 1)
        presence.invalidate()
        asyncio.run(presence.human_count())
        self.assertEqual(len(calls), 2)

    def test_failure_returns_none_and_is_cached(self):
        calls = []
        presence = self.make_presence({}, call_log=calls, fail=True)
        self.assertIsNone(asyncio.run(presence.human_count()))
        self.assertIsNone(asyncio.run(presence.human_count()))
        self.assertEqual(len(calls), 1)

    def test_http_error_and_success_false_return_none(self):
        self.assertIsNone(asyncio.run(self.make_presence({}, status=500).human_count()))
        self.assertIsNone(
            asyncio.run(self.make_presence({"success": False, "users": []}).human_count())
        )

    def test_missing_cookie_returns_none(self):
        presence = RoomPresence(
            room="minecraft",
            cookie_provider=lambda: None,
            config={**PRESENCE_CONFIG, "api_url": "http://x", "cache_ttl": 60, "timeout": 5},
            http_get=lambda *a, **k: make_response({"users": []}),
            bot_username="EnderDragon",
        )
        self.assertIsNone(asyncio.run(presence.human_count()))


class TestQuietRoomDailyBehaviour(unittest.TestCase):
    """复现原问题：空频道下 alive-buddy 一天约 90 次主动发言。"""

    def test_day_of_proactive_pulses_yields_one_message(self):
        now = [1_700_000_000.0]
        room = make_gated_room(count=0, clock=lambda: now[0])
        room.presence = StubPresence(0)

        async def run_day():
            for i in range(90):
                now[0] += 16 * 60  # 约 16 分钟一条 = 90 条/天
                await room.send_reply(f"pulse {i}")

        asyncio.run(run_day())
        self.assertEqual(len(room._ws.sent), 1)

    def test_players_joining_restores_full_speech(self):
        now = [1_700_000_000.0]
        room = make_gated_room(count=0, clock=lambda: now[0])
        presence = StubPresence(0)
        room.presence = presence

        async def scenario():
            for i in range(5):  # 空房间：先耗掉配额
                now[0] += 60
                await room.send_reply(f"empty {i}")
            presence.count = 1  # 有人上线
            for i in range(5):
                now[0] += 60
                await room.send_reply(f"present {i}")

        asyncio.run(scenario())
        self.assertEqual(
            room._ws.sent, ["empty 0", "present 0", "present 1", "present 2", "present 3", "present 4"]
        )
        self.assertFalse(room._was_quiet)


if __name__ == "__main__":
    unittest.main()


class TestSystemPromptMandates(unittest.TestCase):
    """人设提示词里的几条硬要求不能在后人改动中被悄悄删掉。"""

    def setUp(self):
        from connector.config import SYSTEM_PROMPT_TEMPLATE
        self.prompt = SYSTEM_PROMPT_TEMPLATE

    def test_forbids_repetition(self):
        self.assertIn("Anti-Repetition", self.prompt)
        self.assertIn("同一个意思绝不换措辞讲第二遍", self.prompt)

    def test_requires_proactive_tool_use(self):
        self.assertIn("主动查，别靠猜", self.prompt)
        self.assertIn("积极使用", self.prompt)
        self.assertIn("先查再说", self.prompt)
        self.assertIn("MCP", self.prompt)

    def test_asks_for_delight(self):
        self.assertIn("Delight", self.prompt)
        self.assertIn("它居然知道这个", self.prompt)

    def test_keeps_persona_anchors(self):
        for anchor in ("Persona & Heritage", "Substance", "Range"):
            self.assertIn(anchor, self.prompt)


class TestPresenceSignals(unittest.TestCase):
    """两个在场信号取"或"：聊天室名单 ∪ 游戏内在线人数。"""

    def test_mc_players_alone_count_as_present(self):
        """玩家在 MC 里玩但没开网页：网页名单为空，也不该判成空房间。"""
        room = make_gated_room(count=0, mc_presence_count=2)
        result = asyncio.run(room.send_reply("主动发言"))
        self.assertTrue(result["delivered"], result)
        self.assertEqual(result["reason"], "present:2")
        self.assertFalse(room._was_quiet)

    def test_room_and_mc_counts_add_up(self):
        room = make_gated_room(count=1, mc_presence_count=3)
        result = asyncio.run(room.send_reply("主动发言"))
        self.assertEqual(result["reason"], "present:4")

    def test_both_empty_enters_quiet_mode(self):
        room = make_gated_room(count=0, mc_presence_count=0)
        first = asyncio.run(room.send_reply("第一条"))
        second = asyncio.run(room.send_reply("第二条"))
        self.assertEqual(first["reason"], "empty:quiet-quota")
        self.assertEqual(second["reason"], "empty:quiet-room-suppressed")

    def test_mc_unknown_does_not_break_room_signal(self):
        room = make_gated_room(count=0, mc_presence_count=None)
        self.assertEqual(asyncio.run(room.send_reply("x"))["reason"], "empty:quiet-quota")

    def test_both_unknown_follows_fail_mode(self):
        room = make_gated_room(count=None, mc_presence_count=None, fail_mode="open")
        self.assertEqual(asyncio.run(room.send_reply("x"))["reason"], "unknown:fail-open")

    def test_mc_signal_can_be_disabled(self):
        room = make_gated_room(count=0, mc_presence_count=5, use_mc=False)
        result = asyncio.run(room.send_reply("x"))
        self.assertEqual(result["reason"], "empty:quiet-quota", "关掉 MC 信号后不应看它")
        self.assertEqual(room.mc_presence.queries, 0)


class TestMcPresenceSignal(unittest.TestCase):
    """McPresence 走真 RCON 协议（假服务器）。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_minecraft import FakeRconServer

        self.server = FakeRconServer(password="secret", players="alice, bob, Bot1")
        self.addCleanup(self.server.close)

    def make(self, **overrides):
        from connector.presence import McPresence

        return McPresence(
            host="127.0.0.1",
            port=self.server.port,
            password="secret",
            timeout=3,
            cache_ttl=60,
            ignore=overrides.pop("ignore", set()),
            clock=overrides.pop("clock", None) or (lambda: 1000.0),
        )

    def test_counts_mc_players(self):
        self.assertEqual(asyncio.run(self.make().human_count()), 3)

    def test_ignore_list_excluded(self):
        self.assertEqual(asyncio.run(self.make(ignore={"bot1"}).human_count()), 2)

    def test_unconfigured_returns_none(self):
        from connector.presence import McPresence

        presence = McPresence(host="127.0.0.1", port=1, password="", timeout=1)
        self.assertFalse(presence.configured)
        self.assertIsNone(asyncio.run(presence.human_count()))

    def test_rcon_failure_returns_none_and_is_cached(self):
        from connector.presence import McPresence

        presence = McPresence(host="127.0.0.1", port=1, password="secret", timeout=0.4,
                              clock=lambda: 1000.0)
        self.assertIsNone(asyncio.run(presence.human_count()))
        self.assertIsNone(asyncio.run(presence.human_count()))

    def test_result_is_cached_within_ttl(self):
        presence = self.make()
        self.assertEqual(asyncio.run(presence.human_count()), 3)
        before = len(self.server.commands)
        self.assertEqual(asyncio.run(presence.human_count()), 3)
        self.assertEqual(len(self.server.commands), before, "TTL 内不应重复查询")


class TestOutboundThrottle(unittest.TestCase):
    """出站硬护栏：不管什么原因，都不允许它在几秒内连发。

    真实场景：玩家每 ≥冷却窗（原 15s）说一句，它就会每句都答一次，
    实测一分钟内发出两条（间隔 26s）。冷却窗调大是"少答"，
    这层是"绝不在几秒内连发"的兜底。
    """

    def make(self, now=None, **overrides):
        clock = (lambda: now[0]) if now else None
        room = make_gated_room(count=5, clock=clock, **overrides)
        room.last_human_message_at = 0
        return room

    def test_second_send_within_min_interval_is_dropped(self):
        now = [1000.0]
        room = self.make(now, outbound_min_interval=8, outbound_per_minute=0)
        first = asyncio.run(room.send_reply("第一条"))
        self.assertTrue(first["delivered"])

        now[0] += 3  # 3 秒后又要发（例如同一轮并行调了两次 send_message）
        second = asyncio.run(room.send_reply("第二条"))
        self.assertFalse(second["delivered"])
        self.assertIn("too-soon", second["reason"])
        self.assertEqual(room._ws.sent, ["第一条"], "第二条不应进入房间")

    def test_send_allowed_after_interval(self):
        now = [1000.0]
        room = self.make(now, outbound_min_interval=8, outbound_per_minute=0)
        asyncio.run(room.send_reply("第一条"))
        now[0] += 9
        self.assertTrue(asyncio.run(room.send_reply("第二条"))["delivered"])

    def test_per_minute_cap(self):
        now = [1000.0]
        room = self.make(now, outbound_min_interval=0, outbound_per_minute=2)
        for i in range(2):
            now[0] += 10
            self.assertTrue(asyncio.run(room.send_reply(f"第{i}条"))["delivered"])
        now[0] += 10
        capped = asyncio.run(room.send_reply("第三条"))
        self.assertFalse(capped["delivered"])
        self.assertIn("per-minute", capped["reason"])

        now[0] += 61  # 窗口滑过后恢复
        self.assertTrue(asyncio.run(room.send_reply("第四条"))["delivered"])

    def test_reactive_sends_are_also_throttled(self):
        """有人说话也不能成为连发的理由。"""
        now = [1000.0]
        room = self.make(now, outbound_min_interval=8, outbound_per_minute=0)
        room.last_human_message_at = now[0]
        self.assertTrue(asyncio.run(room.send_reply("回复1"))["delivered"])
        room.last_human_message_at = now[0] + 1
        now[0] += 1
        self.assertFalse(asyncio.run(room.send_reply("回复2"))["delivered"])

    def test_throttle_disabled_when_zero(self):
        now = [1000.0]
        room = self.make(now, outbound_min_interval=0, outbound_per_minute=0)
        for i in range(5):
            self.assertTrue(asyncio.run(room.send_reply(f"第{i}条"))["delivered"])

    def test_two_turns_30s_apart_are_both_allowed_but_cooldown_can_prevent_the_trigger(self):
        """26~30 秒两条：出站护栏不拦（那属于触发侧冷却的职责），但触发侧窗口调大后不会再触发。"""
        now = [1000.0]
        room = self.make(now, outbound_min_interval=8, outbound_per_minute=0)
        asyncio.run(room.send_reply("第一条"))
        now[0] += 26
        self.assertTrue(asyncio.run(room.send_reply("第二条"))["delivered"])

        # 触发侧：冷却窗 30s 时，30 秒内到达的玩家消息只进记忆、不触发回复
        room2 = make_gated_room(count=5)
        room2.reply_cooldown = 30
        room2.last_trigger_time = time.time()
        batch = [make_msg(sender="alice", text="你还在吗", age_seconds=1)]
        self.assertEqual([s for _, s in room2._plan_batch(batch)], [True], "冷却窗内的消息应为 silent")

        room2.last_trigger_time = time.time() - 31
        self.assertEqual([s for _, s in room2._plan_batch(batch)], [False], "冷却窗过后应触发")


class TestDragonVoice(unittest.TestCase):
    """人设必须把"龙"的坐标写死，防止再退化成学者/讲解员腔。"""

    def setUp(self):
        from connector.config import SYSTEM_PROMPT_TEMPLATE
        self.prompt = SYSTEM_PROMPT_TEMPLATE

    def test_voice_section_exists_with_three_anchors(self):
        self.assertIn("Voice — 要像龙，不要像学者", self.prompt)
        for anchor in ("尺度是你的", "身体是你的", "主权是你的"):
            self.assertIn(anchor, self.prompt)

    def test_lecturer_register_is_banned(self):
        self.assertIn("反面清单", self.prompt)
        for banned in ("值得一提", "事实上", "有趣的是", "讲解员"):
            self.assertIn(banned, self.prompt)
        self.assertIn("不要总结句", self.prompt)

    def test_has_dragon_vs_scholar_contrast_examples(self):
        self.assertIn("同一件事，两种说法", self.prompt)
        self.assertIn("学者腔：", self.prompt)
        self.assertIn("孤独是你们发明的东西", self.prompt, "应给出龙的示范句")

    def test_brevity_is_explicit(self):
        self.assertIn("最多三句", self.prompt)
        self.assertIn("龙不做演讲", self.prompt)

    def test_one_thing_per_utterance(self):
        self.assertIn("只交付一件", self.prompt)

    def test_instructs_breaking_its_own_recent_register(self):
        """自己历史里的长篇学者腔是最强的模仿源，必须显式禁止延续它。"""
        self.assertIn("改掉旧习惯", self.prompt)
        self.assertIn("从这一句开始就不许再那样说", self.prompt)
