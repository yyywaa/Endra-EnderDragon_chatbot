"""coffeeroom 连接层：鉴权/心跳/重连/缓冲/去重/冷却，消息投递给 alive-buddy。"""
import asyncio
import json
import time
from typing import Optional

import websockets

from .buddy_client import BuddyClient
from .conversation import conversation_log
from .config import BOT_CONFIG, CONNECTION_CONFIG, PRESENCE_CONFIG, SERVER_CONFIG, TOOLS_CONFIG
from .logger import setup_logger
from .presence import McPresence, RoomPresence
from .session_manager import session_manager

logger = setup_logger("room_client")


class RoomClient:
    def __init__(self, buddy: BuddyClient, room: Optional[str] = None, clock=None):
        self.buddy = buddy
        self.room = room or BOT_CONFIG["room"]
        self.bot_username = BOT_CONFIG.get("username") or "EnderDragon"
        self.processed_msg_ids = set()
        self.last_trigger_time = 0.0  # 上一次投递非 silent 消息的时间（成本护栏）
        self._clock = clock or time.time

        self.ws_base = SERVER_CONFIG["ws_base"]
        self.heartbeat_interval = CONNECTION_CONFIG["heartbeat_interval"]
        self.buffer_max = CONNECTION_CONFIG["message_buffer_max"]
        self.reply_cooldown = CONNECTION_CONFIG["reply_cooldown"]
        self.freshness_window = CONNECTION_CONFIG["freshness_window"]
        self.base_delay = CONNECTION_CONFIG["initial_retry_delay"]
        self.max_delay = CONNECTION_CONFIG["max_retry_delay"]

        # ---- 在场感知：房间没人时别对着空频道自言自语 ----
        self.presence_config = PRESENCE_CONFIG
        self.presence_enabled = PRESENCE_CONFIG["enabled"]
        self.ignore_users = {u.lower() for u in (PRESENCE_CONFIG.get("ignore_users") or ())}
        self.presence = RoomPresence(
            room=self.room,
            cookie_provider=lambda: session_manager.get_session(force_refresh=False),
            config=PRESENCE_CONFIG,
            bot_username=self.bot_username,
            ignore_users=self.ignore_users,
        )
        # MC 侧在场信号：玩家在游戏里但没开网页时，网页名单是空的，靠 RCON 兜住
        self.mc_presence = McPresence(
            host=PRESENCE_CONFIG.get("mc_host") or TOOLS_CONFIG["mc_rcon_host"],
            port=PRESENCE_CONFIG.get("mc_port") or TOOLS_CONFIG["mc_rcon_port"],
            password=PRESENCE_CONFIG.get("mc_password") or TOOLS_CONFIG["mc_rcon_password"],
            timeout=TOOLS_CONFIG["mc_rcon_timeout"],
            cache_ttl=PRESENCE_CONFIG["cache_ttl"],
            ignore=set(PRESENCE_CONFIG.get("mc_ignore") or ()),
            rcon=PRESENCE_CONFIG.get("mc_rcon"),
            clock=clock,
        )
        self.last_human_message_at = 0.0  # 最近一条真人消息时间（判断发言是不是"回应"）
        self._outbound_times = []  # 最近实际发出的时间戳（出站节流用）
        self._quiet_sends = []  # 静默期已放行的主动发言时间戳
        self._was_quiet = None  # 静默状态，仅用于状态切换时打日志

        self._ws = None  # 当前房间 ws 连接，供 webhook 回头发言用

    # ---- 消息过滤（沿用旧逻辑） ----

    def _is_message_fresh(self, msg: dict) -> bool:
        msg_time_raw = msg.get("timestamp")
        if msg_time_raw is None:
            return False
        msg_time = int(msg_time_raw) // 1000
        return (time.time() - msg_time) <= self.freshness_window

    def _is_self(self, msg: dict) -> bool:
        return msg.get("sender_username") == self.bot_username

    def _is_duplicate(self, msg: dict) -> bool:
        msg_id = msg.get("msg_id")
        return bool(msg_id) and msg_id in self.processed_msg_ids

    def _extract_valid_messages(self, msg_data) -> list:
        if isinstance(msg_data, list):
            return [m for m in msg_data if isinstance(m, dict) and "text" in m]
        if isinstance(msg_data, dict) and "text" in msg_data:
            return [msg_data]
        return []

    def _mark_processed(self, msg: dict):
        msg_id = msg.get("msg_id")
        if msg_id:
            self.processed_msg_ids.add(msg_id)
        if len(self.processed_msg_ids) > 1000:
            self.processed_msg_ids = set(list(self.processed_msg_ids)[-500:])

    def _is_on_cooldown(self) -> bool:
        return (time.time() - self.last_trigger_time) < self.reply_cooldown

    # ---- 在场感知闸门（只拦出站发言，不影响记忆投递） ----

    def _is_human_sender(self, msg: dict) -> bool:
        """真人 = 不是 Endra 自己，也不在忽略名单里的发送者。"""
        name = str(msg.get("sender_username") or "").lower()
        if not name:
            return False
        return name != self.bot_username.lower() and name not in self.ignore_users

    def _is_reactive(self) -> bool:
        """距最近一条真人消息足够近的发言视为"回应"，空房间下也放行。"""
        if self.last_human_message_at <= 0:
            return False
        window = self.presence_config["reactive_window"]
        return (self._clock() - self.last_human_message_at) <= window

    def _take_quiet_quota(self) -> bool:
        """静默期配额：滚动窗口内最多放行 N 条主动发言。"""
        quota = self.presence_config["quiet_daily_quota"]
        if quota <= 0:
            return False
        now = self._clock()
        window = self.presence_config["quiet_window_hours"] * 3600
        self._quiet_sends = [t for t in self._quiet_sends if now - t < window]
        if len(self._quiet_sends) >= quota:
            return False
        self._quiet_sends.append(now)
        return True

    def _set_quiet(self, quiet: bool, detail: str):
        if self._was_quiet == quiet:
            return
        self._was_quiet = quiet
        if quiet:
            logger.info(f"[Presence] {detail}，主动发言进入静默配额模式")
        else:
            logger.info(f"[Presence] {detail}，恢复主动发言")

    async def _gate_outbound(self) -> tuple:
        """决定一条出站发言是否放行，返回 (allowed, reason)。

        两个在场信号取"或"：
          · 聊天室名单（coffeeroom /api/online-users）：反映网页会话；
          · MC 在线人数（RCON list）：反映游戏内实际有谁。
        只要任一信号为真就算"有人"——玩家在游戏里但没开网页时，前者会是空的。
        """
        if not self.presence_enabled:
            return True, "presence-disabled"

        room_humans = await self.presence.human_count()
        mc_humans = None
        if self.presence_config.get("use_mc", True):
            mc_humans = await self.mc_presence.human_count()

        sources = []
        if room_humans:
            sources.append(f"聊天室 {room_humans} 人")
        if mc_humans:
            sources.append(f"游戏内 {mc_humans} 人")
        total = (room_humans or 0) + (mc_humans or 0)

        if total > 0:
            self._set_quiet(False, f"房间有人在（{'；'.join(sources)}）")
            return True, f"present:{total}"

        # 两个信号都没数到人。若都是"未知"（接口挂了/RCON 没配），交给 fail_mode 决定
        if room_humans is None and mc_humans is None:
            if self.presence_config["fail_mode"] == "open":
                return True, "unknown:fail-open"
            prefix = "unknown"
        else:
            prefix = "empty"
        self._set_quiet(True, "房间当前无人在线" if prefix == "empty" else "在线名单未知")

        if self._is_reactive():
            return True, f"{prefix}:reactive"
        if self._take_quiet_quota():
            return True, f"{prefix}:quiet-quota"
        return False, f"{prefix}:quiet-room-suppressed"

    # ---- 批次处理：除最后一条外全部 silent，最后一条受冷却控制 ----

    def _plan_batch(self, batch: list) -> list:
        """返回 [(msg, silent)]。规则：
        - 批次内除最后一条外全部 silent=True（只进记忆）
        - 最后一条：消息过期或处于冷却期时 silent=True，否则 silent=False 触发 reAct
        """
        plan = []
        last_index = len(batch) - 1
        for i, m in enumerate(batch):
            silent = True
            if i == last_index and self._is_message_fresh(m) and not self._is_on_cooldown():
                silent = False
            plan.append((m, silent))
        return plan

    async def _deliver_batch(self, batch: list):
        for m, silent in self._plan_batch(batch):
            if self._is_human_sender(m):
                # 有人在房间里说话 = 最强在场证据（桥接账号不进在线名单也能救回来）
                self.last_human_message_at = self._clock()
                # 供安全审查层判断"这次工具调用是否对得上眼前的对话"
                conversation_log.add(m.get("sender_username"), m.get("text") or "")
            # 显式标出"这条要不要回"：silent 投递（冷却期/批次内非最后一条）对模型来说
            # 本来是看不出区别的，于是它会在几条"没人回过"的话里自己挑一条答——
            # 实测就出现过"被 Cloudrayyy 触发，却先补答 khangai 更早那条"。
            # 标记进 L1 后，"这次该回哪条"就不再需要模型自己猜。
            marker = "【仅语境】" if silent else "【待回应】"
            text = f"{marker}{m.get('sender_username')}: {m.get('text')}"
            try:
                await self.buddy.deliver(text, silent, user_id=m.get("sender_username") or "coffeeroom")
            except Exception as e:
                logger.error(f"[Deliver] 投递失败 (silent={silent}): {e}")
            finally:
                self._mark_processed(m)
            if not silent:
                self.last_trigger_time = time.time()
                logger.info(f"[Trigger] 非静默投递: {text[:80]}")

    # ---- 对聊天室发言（webhook 回调入口） ----

    def _outbound_throttle(self) -> Optional[str]:
        """出站节流：返回拒绝原因，None 表示放行。

        与"触发冷却"（REPLY_COOLDOWN_SECONDS，管要不要回）不同，这一层是**硬兜底**：
        不管什么原因，都不允许它在几秒内连发两条。
        """
        now = self._clock()
        window = 60.0
        self._outbound_times = [t for t in self._outbound_times if now - t < window]

        per_minute = int(self.presence_config.get("outbound_per_minute") or 0)
        if per_minute and len(self._outbound_times) >= per_minute:
            return f"outbound:per-minute({per_minute})"

        min_interval = float(self.presence_config.get("outbound_min_interval") or 0)
        if min_interval and self._outbound_times:
            gap = now - self._outbound_times[-1]
            if gap < min_interval:
                return f"outbound:too-soon({gap:.1f}s<{min_interval:g}s)"

        self._outbound_times.append(now)
        return None

    async def send_reply(self, content: str):
        allowed, reason = await self._gate_outbound()
        if not allowed:
            logger.info(f"[Presence] 抑制发言（{reason}）: {content[:80]}")
            return {"delivered": False, "reason": reason}

        throttle = self._outbound_throttle()
        if throttle:
            logger.warning(f"[Outbound] 节流丢弃发言（{throttle}）: {content[:80]}")
            return {"delivered": False, "reason": throttle}

        ws = self._ws
        if ws is None:
            logger.warning(f"[Bot] 房间未连接，丢弃发言: {content[:80]}")
            return {"delivered": False, "reason": "room-disconnected"}
        try:
            await ws.send(content)
            logger.info(f"[Bot] 发送（{reason}）: {content}")
            return {"delivered": True, "reason": reason}
        except Exception as e:
            logger.error(f"[Bot] 发送失败: {e}")
            return {"delivered": False, "reason": f"send-error: {e}"}

    # ---- 主循环 ----

    async def run(self):
        retry_attempt = 0

        while True:
            cookie = session_manager.get_session(force_refresh=False)
            if cookie is None:
                delay = min(self.base_delay * (2 ** retry_attempt), self.max_delay)
                logger.warning(f"[Connection] 无法获取session，{delay}秒后重试... (attempt {retry_attempt})")
                retry_attempt += 1
                await asyncio.sleep(delay)
                continue

            ws_url = f"{self.ws_base}/{self.room}"
            logger.info(f"[Connection] 连接: {ws_url}")

            try:
                async with websockets.connect(ws_url, additional_headers={"Cookie": cookie}) as ws:
                    logger.info(f"[Connection] 已连接房间: {self.room}")
                    last_ping = time.time()
                    retry_attempt = 0
                    self._ws = ws

                    try:
                        async for raw_msg in ws:
                            try:
                                if time.time() - last_ping > self.heartbeat_interval:
                                    await ws.ping()
                                    last_ping = time.time()

                                msg_data = json.loads(raw_msg)
                                valid_batch = self._extract_valid_messages(msg_data)

                                new_messages = [
                                    m for m in valid_batch
                                    if not self._is_duplicate(m) and not self._is_self(m)
                                ]
                                if not new_messages:
                                    continue

                                await self._deliver_batch(new_messages)

                            except json.JSONDecodeError:
                                logger.error(f"[Parse] JSON错误: {raw_msg[:50]}")
                            except Exception as e:
                                logger.error(f"[Process] 处理异常: {e}")
                                import traceback
                                logger.error(traceback.format_exc())
                    finally:
                        self._ws = None

            except Exception as e:
                delay = min(self.base_delay * (2 ** retry_attempt), self.max_delay)
                logger.error(f"[Connection] 连接断开: {e}，{delay}秒后尝试重连... (attempt {retry_attempt})")
                retry_attempt += 1
                await asyncio.sleep(delay)
