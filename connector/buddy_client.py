"""alive-buddy 客户端：session init / chat ws 投递 / 断线重 init。"""
import asyncio
import json
import time
import uuid
from typing import Optional

import requests
import websockets

from .config import (
    BUDDY_CONFIG,
    CHARACTER_ID,
    DEBUG_REACT_LOG,
    LLM_CONFIG,
    LLM_SAMPLING,
    MEMORY_CONFIG,
    SYSTEM_PROMPT_TEMPLATE,
    WEBHOOK_CONFIG,
)
from .logger import setup_logger

logger = setup_logger("buddy_client")


def build_character_config(tool_hub=None) -> dict:
    """组装下发给 alive-buddy 的角色配置。

    工具（萌娘百科/维基/币价/MCP）以 `extend_tool_list` 下发定义，
    实际执行在 connector（见 /tools/call）——buddy 只负责"决定调什么"。
    """
    tool_definitions = tool_hub.definitions() if tool_hub is not None else []
    tool_url = WEBHOOK_CONFIG["tool_url"] or WEBHOOK_CONFIG["public_url"].replace("/webhook", "/tools/call")
    tool_headers = {}
    if tool_definitions:
        from .config import TOOLS_CONFIG
        if TOOLS_CONFIG.get("api_token"):
            tool_headers["X-Tool-Token"] = TOOLS_CONFIG["api_token"]

    return {
        "id": CHARACTER_ID,
        "name": "Endra",
        "bio": "An old enderdragon king.",
        "system_prompt_template": SYSTEM_PROMPT_TEMPLATE,
        "initial_state": {"mood": 50, "energy": 100, "boredom": 0},
        "connection": {
            "base_url": LLM_CONFIG["base_url"],
            "api_key": LLM_CONFIG["api_key"],
            "model": LLM_CONFIG["model"],
            "send_url": WEBHOOK_CONFIG["public_url"],
            "tool_url": tool_url,
            "connect_headers": {},
            "send_headers": tool_headers,
        },
        "llm_setting": {
            "stream": True,
            **LLM_SAMPLING,
        },
        # 记忆窗口：独白预算防止上下文被自己的回声填满，L2 梗概回灌补回长期记忆
        "memory": dict(MEMORY_CONFIG),
        "extend_tool_list": tool_definitions,
        "debug": DEBUG_REACT_LOG,
    }


class SessionNotFoundError(Exception):
    """alive-buddy 重启后旧 session_id 失效，需要重新 init。"""


class BuddyClient:
    def __init__(self, tool_hub=None):
        self.http_base = BUDDY_CONFIG["http_base"].rstrip("/")
        self.ws_base = BUDDY_CONFIG["ws_base"].rstrip("/")
        self.session_id: Optional[str] = None
        self.tool_hub = tool_hub
        self._ws = None
        self._ready = asyncio.Event()

    # ---- session 管理 ----

    def _init_session_sync(self) -> str:
        payload = build_character_config(self.tool_hub)
        if payload["extend_tool_list"]:
            logger.info(
                f"[Buddy] 下发 {len(payload['extend_tool_list'])} 个工具定义: "
                f"{', '.join(t['function']['name'] for t in payload['extend_tool_list'])}"
            )
        resp = requests.post(
            f"{self.http_base}/v1/session/init",
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["session_id"]

    async def init_session(self):
        self.session_id = await asyncio.to_thread(self._init_session_sync)
        logger.info(f"[Buddy] session 初始化完成: {self.session_id}")

    async def get_status(self) -> Optional[dict]:
        if not self.session_id:
            return None
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"{self.http_base}/v1/session/{self.session_id}/status",
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.warning(f"[Buddy] 查询 status 失败: {e}")
        return None

    # ---- 后台主循环：维持 init + chat ws ----

    async def run(self):
        """维持与 alive-buddy 的会话与 chat ws，断线自动重连/重 init。"""
        retry = 0
        need_init = True
        while True:
            try:
                if need_init:
                    await self.init_session()
                    need_init = False
                async with websockets.connect(f"{self.ws_base}/v1/chat") as ws:
                    self._ws = ws
                    self._ready.set()
                    retry = 0
                    logger.info("[Buddy] chat ws 已连接")
                    consumers = [asyncio.create_task(self._consume_receipts(ws))]
                    if DEBUG_REACT_LOG:
                        consumers.append(asyncio.create_task(self._consume_debug()))
                    try:
                        done, pending = await asyncio.wait(
                            consumers, return_when=asyncio.FIRST_COMPLETED
                        )
                        for t in pending:
                            t.cancel()
                        for t in done:
                            exc = t.exception()
                            if isinstance(exc, SessionNotFoundError):
                                need_init = True
                            elif exc:
                                raise exc
                    finally:
                        self._ws = None
                        self._ready.clear()
                    # ws 正常关闭（非异常）时避免立即重连空转
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = min(2 ** retry, 30)
                logger.warning(f"[Buddy] 连接异常: {e}，{delay}s 后重试 (attempt {retry})")
                retry += 1
                await asyncio.sleep(delay)

    async def _consume_receipts(self, ws):
        """消费服务端占位回执；发现 Session not found 时触发重 init。"""
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if isinstance(data, dict) and "error" in data:
                logger.warning(f"[Buddy] 服务端回执错误: {data['error']}")
                if data["error"] == "Session not found":
                    raise SessionNotFoundError()
            # 其余为占位 echo（"[DEBUG] I received your message..."），忽略

    async def _consume_debug(self):
        """订阅 reAct 思考流并写入日志（仅 DEBUG_REACT_LOG 开启时）。"""
        url = f"{self.ws_base}/v1/session/{self.session_id}/debug"
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    # alive-buddy 重启后 session 失效：debug ws 会报错并主动关闭，
                    # 必须抛出以触发重 init，否则外层只重连不重建会话
                    if isinstance(data, dict) and data.get("error") == "Session not found":
                        logger.warning("[Buddy] debug ws 收到 Session not found，触发重 init")
                        raise SessionNotFoundError()
                    entry = data.get("entry")
                    if entry:
                        logger.info(
                            f"[ReAct:{entry.get('type')}] {str(entry.get('content'))[:300]}"
                        )
        except asyncio.CancelledError:
            raise
        except SessionNotFoundError:
            raise
        except Exception as e:
            logger.warning(f"[Buddy] debug ws 断开: {e}")

    # ---- 消息投递 ----

    async def wait_ready(self):
        await self._ready.wait()

    async def deliver(self, text: str, silent: bool, user_id: str = "coffeeroom"):
        """投递一条 UnifiedMessage。silent=True 只写记忆不触发 reAct。"""
        if not self.session_id or self._ws is None:
            raise ConnectionError("alive-buddy 未就绪")
        payload = {
            "msg_id": str(uuid.uuid4()),  # 必须全局唯一，见交接文档 §7.2
            "user_id": user_id,
            "session_id": self.session_id,
            "timestamp": int(time.time() * 1000),
            "silent": silent,
            "payload": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        }
        await self._ws.send(json.dumps(payload))
