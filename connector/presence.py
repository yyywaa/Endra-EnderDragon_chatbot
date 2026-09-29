"""在场感知：查询 coffeeroom 本房间的在线真人数量。

数据来源：`GET /api/online-users`（coffeeroom 消息模块，返回所有房间的在线用户聚合列表，
每项形如 `{"username": "...", "uid": "...", "channel": "..."}`）。复用 session_manager
已持有的 session cookie，不需要 Minecraft 侧 RCON 之类的额外凭据。

只统计 `channel == 本房间`、且不在忽略名单里的账号；Endra 自己不计入。
查询失败时返回 None（"未知"），由 RoomClient 按 PRESENCE_FAIL_MODE 决定如何处置。
"""
import asyncio
import time
from typing import Callable, Optional

import requests

from .config import PRESENCE_CONFIG
from .logger import setup_logger

logger = setup_logger("presence")


class RoomPresence:
    """带 TTL 缓存的房间在线人数查询器。

    缓存的语义是"最近一次查询结果"，失败也会缓存 TTL，避免名单接口挂掉时
    每条发言都去打一次 HTTP。
    """

    def __init__(
        self,
        room: str,
        cookie_provider: Optional[Callable[[], Optional[str]]] = None,
        config: Optional[dict] = None,
        http_get: Optional[Callable] = None,
        clock: Optional[Callable[[], float]] = None,
        bot_username: Optional[str] = None,
        ignore_users: Optional[set] = None,
    ):
        self.room = room or ""
        self.cookie_provider = cookie_provider
        self.config = config or PRESENCE_CONFIG
        self._http_get = http_get or requests.get
        self._clock = clock or time.time
        self.bot_username = (bot_username or "").lower()
        if ignore_users is None:
            ignore_users = set(self.config.get("ignore_users") or ())
        self.ignore_users = {str(u).lower() for u in ignore_users}

        self._cache_count: Optional[int] = None  # None = 未知（查询失败/无 cookie）
        self._cache_at = 0.0
        self._cache_valid = False
        self._last_names: Optional[frozenset] = None
        self._lock = asyncio.Lock()

    # ---- 缓存 ----

    def _cache_fresh(self) -> bool:
        if not self._cache_valid:
            return False
        return (self._clock() - self._cache_at) < self.config["cache_ttl"]

    def invalidate(self):
        """丢弃缓存，下次调用强制重新查询。"""
        self._cache_valid = False
        self._cache_count = None

    # ---- 对外接口 ----

    async def human_count(self) -> Optional[int]:
        """返回本房间在线真人数；None 表示未知（查询失败/无 cookie）。"""
        if self._cache_fresh():
            return self._cache_count
        async with self._lock:
            # 并发调用时，等锁期间别人可能已经刷新过
            if self._cache_fresh():
                return self._cache_count
            count = await asyncio.to_thread(self._fetch_sync)
            self._cache_count = count
            self._cache_at = self._clock()
            self._cache_valid = True
            return count

    # ---- 实际查询（阻塞，跑在线程里） ----

    def _fetch_sync(self) -> Optional[int]:
        cookie = self.cookie_provider() if self.cookie_provider else None
        if not cookie:
            logger.warning("[Presence] 无可用 session cookie，在线名单未知")
            return None

        try:
            resp = self._http_get(
                self.config["api_url"],
                headers={"Cookie": cookie},
                timeout=self.config["timeout"],
            )
        except Exception as e:
            logger.warning(f"[Presence] 查询在线名单异常: {e}")
            return None

        if resp.status_code != 200:
            logger.warning(f"[Presence] 查询在线名单失败: HTTP {resp.status_code}")
            return None

        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"[Presence] 在线名单 JSON 解析失败: {e}")
            return None

        if not isinstance(data, dict):
            logger.warning("[Presence] 在线名单结构异常，忽略")
            return None
        if data.get("success") is False:
            logger.warning(f"[Presence] 在线名单接口返回 success=false: {str(data)[:120]}")
            return None

        users = data.get("users")
        if not isinstance(users, list):
            users = []

        names = []
        room_key = self.room.lower()
        for user in users:
            if not isinstance(user, dict):
                continue
            channel = str(user.get("channel") or "").lower()
            if channel != room_key:
                continue
            name = str(user.get("username") or "")
            key = name.lower()
            if not key or key == self.bot_username or key in self.ignore_users:
                continue
            names.append(name)

        current = frozenset(names)
        if current != self._last_names:
            shown = "、".join(sorted(names)) if names else "无"
            logger.info(
                f"[Presence] 房间 {self.room} 在线真人（{len(names)}）: {shown}"
                f"（全站在线账号共 {len(users)}）"
            )
            self._last_names = current
        else:
            logger.debug(f"[Presence] 房间 {self.room} 在线真人: {len(names)}")

        return len(names)
