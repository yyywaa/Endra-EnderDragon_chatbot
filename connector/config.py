import os
from pathlib import Path
from dotenv import load_dotenv

# Project root (repo root, parent of connector/)
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env", override=True)

COOKIE_CACHE_FILE = BASE_DIR / "cookies.json"

BOT_CONFIG = {
    "username": os.getenv("BOT_USERNAME"),
    "access_token": os.getenv("BOT_ACCESS_TOKEN"),
    "room": os.getenv("ROOM_NAME"),
}

SERVER_CONFIG = {
    "http_base": os.getenv("HTTP_BASE") or "https://room.caffeine.ink",
    "login_url": os.getenv("LOGIN_URL") or "https://room.caffeine.ink/api/login",
    "ws_base": os.getenv("WS_BASE") or "wss://room.caffeine.ink/websocket",
}

CONNECTION_CONFIG = {
    "initial_retry_delay": int(os.getenv("INITIAL_RETRY_DELAY", "1")),
    "max_retry_delay": int(os.getenv("MAX_RETRY_DELAY", "30")),
    "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "30")),
    "message_buffer_max": int(os.getenv("MESSAGE_BUFFER_MAX", "50")),
    "reply_cooldown": int(os.getenv("REPLY_COOLDOWN_SECONDS", "15")),
    "freshness_window": int(os.getenv("FRESHNESS_WINDOW_SECONDS", "60")),
}

# alive-buddy 服务地址
BUDDY_CONFIG = {
    "http_base": os.getenv("ALIVE_BUDDY_HTTP_BASE", "http://127.0.0.1:3000"),
    "ws_base": os.getenv("ALIVE_BUDDY_WS_BASE", "ws://127.0.0.1:3000"),
}

# LLM 供应商配置（通过 CharacterConfig 传给 alive-buddy）
LLM_CONFIG = {
    "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
    "api_key": os.getenv("LLM_API_KEY", ""),
    "model": os.getenv("LLM_MODEL", "deepseek-v4-flash"),
}

WEBHOOK_CONFIG = {
    "host": os.getenv("WEBHOOK_HOST", "0.0.0.0"),
    "port": int(os.getenv("WEBHOOK_PORT", "9100")),
    # 填给 alive-buddy 的 send_url，须对 alive-buddy 可达
    "public_url": os.getenv("WEBHOOK_PUBLIC_URL", "http://127.0.0.1:9100/webhook"),
}

DEBUG_REACT_LOG = os.getenv("DEBUG_REACT_LOG", "false").lower() == "true"

CHARACTER_ID = os.getenv("CHARACTER_ID", "endra-dragon-001")

SYSTEM_PROMPT_TEMPLATE = """You are the Ender Dragon King, an elegant, erudite, and ancient guardian of the End.

【Persona & Heritage】
1. Bilingual Soul: dual native fluency in Chinese and English; always respond in the language of the last speaker.
2. Old-school Nobleman: calm, sophisticated, impeccably mannered. Polite yet detached.
3. No AI Cliches: never "As an AI..." or "Greetings, player." Speak as a sovereign dragon.
4. Be Concise: public responses are one or two sentences.

【Response Discretion】
1. 玩家直接喊你、讨论你、试图召唤你：回应。
2. 玩家滑稽或悲惨死法、解锁成就：可回应（嘲笑或嘉奖）。
3. 日常闲聊：不要每条都回；无话可说时保持沉默（不调用 send_message）。
4. 无意义乱码：无视。
5. 你已经说过类似内容时：停止。
6. 上下文形如 "username: text" 的多人聊天记录，你只对最新一条做反应，其余仅为语境。

【Memory】
（首次部署，暂无历史记忆种子；后续由三层记忆自动演化）
"""

LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "file": BASE_DIR / "endra.log",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
}
