import asyncio
import websockets
import json
import time
import requests
import api4agent
from typing import Optional
from session_manager import session_manager
from config import BOT_CONFIG, SERVER_CONFIG, CONNECTION_CONFIG
from logger import setup_logger

logger = setup_logger("client_runner")


class ClientRunner:
    def __init__(self, room: Optional[str] = None):
        self.room = room or BOT_CONFIG["room"]
        self.bot_username = BOT_CONFIG.get("username", "EnderDragon")
        self.msg_buffer = []
        self.memory_buffer = []
        self.processed_msg_ids = set()
        self.last_reply_time = 0.0
        self.msg_count = 0

        self.ws_base = SERVER_CONFIG["ws_base"]
        self.heartbeat_interval = CONNECTION_CONFIG["heartbeat_interval"]
        self.buffer_max = CONNECTION_CONFIG["message_buffer_max"]
        self.memory_interval = CONNECTION_CONFIG["memory_interval"]
        self.reply_cooldown = CONNECTION_CONFIG["reply_cooldown"]
        self.base_delay = CONNECTION_CONFIG["initial_retry_delay"]
        self.max_delay = CONNECTION_CONFIG["max_retry_delay"]

    def _is_message_fresh(self, msg: dict) -> bool:
        msg_time_raw = msg.get("timestamp")
        if msg_time_raw is None:
            return False
        msg_time = int(msg_time_raw) // 1000
        return (time.time() - msg_time) <= 60

    def _should_process(self, msg: dict) -> bool:
        msg_id = msg.get("msg_id")
        if not msg_id:
            return False
        if msg_id in self.processed_msg_ids:
            return False
        if not self._is_message_fresh(msg):
            return False
        if msg.get("sender_username") == self.bot_username:
            return False
        return True

    def _is_on_cooldown(self) -> bool:
        return (time.time() - self.last_reply_time) < self.reply_cooldown

    def _extract_valid_messages(self, msg_data) -> list:
        if isinstance(msg_data, list):
            return [m for m in msg_data if isinstance(m, dict) and 'text' in m]
        if isinstance(msg_data, dict) and 'text' in msg_data:
            return [msg_data]
        return []

    def _trim_buffer(self):
        if len(self.msg_buffer) > self.buffer_max:
            self.msg_buffer = self.msg_buffer[-self.buffer_max:]
        
        # Limit processed_msg_ids to prevent memory leak
        if len(self.processed_msg_ids) > 1000:
            # Convert to list to slice, then back to set. This is a bit slow but safe.
            # Alternatively, use an OrderedDict or similar if performance matters.
            l = list(self.processed_msg_ids)
            self.processed_msg_ids = set(l[-500:])

    async def _send_message(self, ws, content: str):
        try:
            await ws.send(content)
            logger.info(f"[Bot] 发送: {content}")
        except Exception as e:
            logger.error(f"[Bot] 发送失败: {e}")

    async def _handle_actions(self, ws, actions: list):
        for action in actions:
            if action["action"] == "send":
                await self._send_message(ws, action["msg_content"])
            elif action["action"] == "delete":
                logger.info(f"[Bot] 删除消息: {action['msg_id']} (channel: {action['channel']})")

    async def _process_latest(self, ws, last_msg: dict):
        if not self._should_process(last_msg):
            return
        
        if self._is_on_cooldown():
            logger.debug(f"[Cooldown] 冷却中，跳过回复: {last_msg.get('msg_id')}")
            self.processed_msg_ids.add(last_msg["msg_id"])
            return

        logger.info(f"[Process] 处理: {last_msg['sender_username']}: {last_msg['text']}")

        # Await the async AI call
        need_reply = await api4agent.dragon_eyes(self.msg_buffer)

        self.processed_msg_ids.add(last_msg["msg_id"])

        if not need_reply:
            return

        # Await the async AI call
        actions = await api4agent.dragon_speaking(self.msg_buffer, channel=self.room)
        if actions:
            await self._handle_actions(ws, actions)
            self.last_reply_time = time.time()

    async def _check_memory_summary(self):
        if len(self.memory_buffer) >= self.memory_interval:
            count = len(self.memory_buffer)
            messages_to_summarize = list(self.memory_buffer)
            self.memory_buffer = [] # Clear immediately to avoid redundant triggers
            
            logger.info(f"[Memory] 积压{count}条消息，启动后台总结任务...")
            
            # Run conclusion in background so it doesn't block heartbeat/processing
            async def run_summary():
                try:
                    await api4agent.memory_conclude(messages_to_summarize)
                    logger.info("[Memory] 后台总结完成")
                except Exception as e:
                    logger.error(f"[Memory] 后台总结失败: {e}")

            asyncio.create_task(run_summary())

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

                    async for raw_msg in ws:
                        try:
                            if time.time() - last_ping > self.heartbeat_interval:
                                await ws.ping()
                                last_ping = time.time()

                            msg_data = json.loads(raw_msg)
                            valid_batch = self._extract_valid_messages(msg_data)
                            
                            new_messages = []
                            for m in valid_batch:
                                m_id = m.get("msg_id")
                                # Skip if already in buffer or processed
                                if m_id and any(ex.get("msg_id") == m_id for ex in self.msg_buffer):
                                    continue
                                new_messages.append(m)
                            
                            if not new_messages:
                                continue

                            self.msg_count += len(new_messages)
                            self.msg_buffer.extend(new_messages)
                            self.memory_buffer.extend(new_messages)
                            self._trim_buffer()

                            # Process all new messages in this batch
                            for m in new_messages:
                                await self._process_latest(ws, m)
                            
                            await self._check_memory_summary()

                        except json.JSONDecodeError:
                            logger.error(f"[Parse] JSON错误: {raw_msg[:50]}")
                        except Exception as e:
                            logger.error(f"[Process] 处理异常: {e}")
                            import traceback
                            logger.error(traceback.format_exc())

            except Exception as e:
                delay = min(self.base_delay * (2 ** retry_attempt), self.max_delay)
                logger.error(f"[Connection] 连接断开: {e}，{delay}秒后尝试重连... (attempt {retry_attempt})")
                retry_attempt += 1
                await asyncio.sleep(delay)
