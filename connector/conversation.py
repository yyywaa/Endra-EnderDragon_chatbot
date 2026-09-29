"""最近对话的环形缓冲。

模型审查层需要知道"这次工具调用是不是对得上眼前的对话"——被注入的探测行为通常
与当前话题无关。RoomClient 把入站消息写进来，审查器按需读取最近若干条。
刻意做成极简：只保留最近 N 条短文本，不落盘、不含任何凭据。
"""
import threading
from collections import deque
from typing import Deque, List, Optional, Tuple


class ConversationLog:
    def __init__(self, max_items: int = 40, max_chars: int = 200):
        self._items: Deque[Tuple[str, str]] = deque(maxlen=max_items)
        self._max_chars = max_chars
        self._lock = threading.Lock()

    def add(self, sender: Optional[str], text: str):
        text = (text or "").strip().replace("\n", " ")
        if not text:
            return
        with self._lock:
            self._items.append(((sender or "?"), text[: self._max_chars]))

    def recent(self, count: int = 6) -> List[str]:
        if count <= 0:
            return []
        with self._lock:
            items = list(self._items)[-count:]
        return [f"{sender}: {text}" for sender, text in items]

    def clear(self):
        with self._lock:
            self._items.clear()


conversation_log = ConversationLog()
