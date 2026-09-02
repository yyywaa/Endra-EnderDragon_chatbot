"""connector 单元测试：消息过滤、批次 silent 标记、冷却窗口、断线重 init。"""
import asyncio
import json
import time
import unittest

from connector.buddy_client import BuddyClient, SessionNotFoundError
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


if __name__ == "__main__":
    unittest.main()
